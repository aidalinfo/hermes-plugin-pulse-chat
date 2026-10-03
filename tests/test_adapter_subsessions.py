# -*- coding: utf-8 -*-
"""Outils de sous-session cote adaptateur (sans hermes installe).

Ce qui est verifie ici, et chaque point est un mode d'echec MUET :

  - le canal vient du contexte de session, jamais d'un argument — le fil
    Tete-a-tete pour l'ouverture, la SOUS-SESSION pour le rapport ;
  - le handler tourne sur une AUTRE boucle que celle du WebSocket (c'est ce
    que fait ``model_tools._run_async``) et atteint quand meme l'adaptateur ;
  - l'outil rend AUSSITOT la reponse de l'app, sans rien attendre ;
  - aucun succes sans 2xx : un refus arrive avec son code, son message et ses
    donnees (``allowed``) ;
  - les outils sont enregistres sous des noms PREFIXES, chacun isole.

Style du depot : ``asyncio.run`` plutot que pytest-asyncio.
"""

import asyncio
import json
import sys
import threading
import types

import pytest

from test_adapter_dedup import _load_adapter_module

adapter_module = _load_adapter_module()

SESSION = {"HERMES_SESSION_CHAT_ID": "tt-atlas"}

OPEN_ARGS = {"agents": ["nova"], "title": "Devis Martin", "brief": "Chiffre le devis Martin."}
OPENED = (200, {"subsession": "sub_1", "slug": "ss-ab12cd34", "status": "open"})
REPORTED = (200, {"subsession": "sub_1", "status": "reported", "delivered": True})


def _install_session_context():
    mod = types.ModuleType("gateway.session_context")
    mod.get_session_env = lambda name, default="": SESSION.get(name, default)
    sys.modules["gateway.session_context"] = mod


class _Config:
    extra = {"url": "http://pulse-chat.test", "token": "token-test"}


@pytest.fixture(autouse=True)
def _isolation():
    _install_session_context()

    def _deconnecter():
        for a in list(adapter_module._LIVE_ADAPTERS):
            a.is_connected = False

    _deconnecter()
    yield
    _deconnecter()
    SESSION["HERMES_SESSION_CHAT_ID"] = "tt-atlas"


def _adapter(responses=None, default=OPENED):
    adapter = adapter_module.PulseChatAdapter(_Config())
    calls = []
    queue = list(responses or [])

    def fake_http(method, url, *, body=None, headers=None, timeout=None):
        calls.append({"method": method, "url": url, "body": body, "headers": headers or {}})
        return queue.pop(0) if queue else default

    adapter._http_detailed = fake_http
    return adapter, calls


def _run(coro):
    return json.loads(asyncio.run(coro))


class _WsLoop:
    """Boucle du WebSocket dans son propre thread — comme en vrai."""

    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self.loop

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


def _h3(status, code, message="motif", **data):
    return status, {"statusCode": status, "statusMessage": message, "data": dict(code=code, **data)}


class TestOuverture:
    def test_200_rend_opened_aussitot(self):
        adapter, calls = _adapter()
        out = _run(adapter.tool_open_subsession("tt-atlas", dict(OPEN_ARGS, title=" Devis Martin ")))
        assert out["status"] == "opened"
        assert out["slug"] == "ss-ab12cd34"
        assert out["subsession"] == "sub_1"
        assert len(calls) == 1
        call = calls[0]
        assert call["method"] == "POST"
        assert call["url"] == "http://pulse-chat.test/api/agent/subsessions/open"
        assert call["headers"]["Content-Type"] == "application/json"
        assert json.loads(call["body"]) == {
            "channel": "tt-atlas",
            "agents": ["nova"],
            "title": "Devis Martin",
            "brief": "Chiffre le devis Martin.",
        }

    def test_relance(self):
        adapter, calls = _adapter()
        out = _run(adapter.tool_open_subsession("tt-atlas", {"subsession": "ss-ab12cd34", "brief": "Ajoute la TVA."}))
        assert out["status"] == "relaunched"
        sent = json.loads(calls[0]["body"])
        assert sent["subsession"] == "ss-ab12cd34"
        assert sent["agents"] == []

    def test_aucune_troncature(self):
        adapter, calls = _adapter()
        brief = "Une phrase. " * 2000
        _run(adapter.tool_open_subsession("c", dict(OPEN_ARGS, brief=brief)))
        assert json.loads(calls[0]["body"])["brief"] == brief.strip()

    def test_callee_not_allowed_rend_la_liste(self):
        allowed = [{"profile": "nova", "displayName": "Nova"}]
        adapter, _ = _adapter([_h3(422, "callee_not_allowed", "Agent non autorise : zeus", allowed=allowed, notAllowed=["zeus"])])
        out = _run(adapter.tool_open_subsession("c", dict(OPEN_ARGS, agents=["zeus"])))
        assert out["status"] == "refused"
        assert out["code"] == "callee_not_allowed"
        assert out["allowed"] == allowed
        assert out["notAllowed"] == ["zeus"]
        assert "Peut appeler" in out["next"]

    @pytest.mark.parametrize(
        "status,code",
        [
            (409, "agent_session_required"),
            (404, "channel_not_found"),
            (409, "subsession_not_allowed_here"),
            (422, "callee_unreachable"),
            (400, "too_many_callees"),
            (400, "brief_too_long"),
            (404, "subsession_not_found"),
            (409, "subsession_closed"),
            (400, "subsession_callees_fixed"),
        ],
    )
    def test_refus_relaye_avec_code_message_et_conseil(self, status, code):
        adapter, _ = _adapter([_h3(status, code, f"motif {code}")])
        out = _run(adapter.tool_open_subsession("c", OPEN_ARGS))
        assert out["status"] == "refused"
        assert out["code"] == code
        assert out["message"] == f"motif {code}"
        ss = sys.modules["pulse_chat_plugin_under_test.subsessions"]
        assert out["next"] == ss._ADVICE[code]

    def test_app_ancienne_404_sans_code(self):
        adapter, _ = _adapter([(404, {"statusCode": 404, "statusMessage": "Page not found: /api/agent/subsessions/open"})])
        out = _run(adapter.tool_open_subsession("c", OPEN_ARGS))
        assert out["code"] == "subsessions_unavailable"
        assert "0.51.0" in out["next"]

    @pytest.mark.parametrize("status", [0, 302, 500, 503])
    def test_jamais_de_succes_sans_2xx(self, status):
        adapter, _ = _adapter([(status, {"subsession": "s", "slug": "ss-x", "status": "open"})])
        out = _run(adapter.tool_open_subsession("c", OPEN_ARGS))
        assert out["status"] == "refused"

    def test_app_injoignable(self):
        adapter, _ = _adapter([(0, {"message": "Connection refused"})])
        out = _run(adapter.tool_open_subsession("c", OPEN_ARGS))
        assert out["code"] == "app_unavailable"

    def test_une_exception_devient_un_refus(self):
        adapter, _ = _adapter()

        def boom(*a, **kw):
            raise RuntimeError("reseau")

        adapter._http_detailed = boom
        out = _run(adapter.tool_open_subsession("c", OPEN_ARGS))
        assert out["status"] == "refused"
        assert out["code"] == "not_sent"


class TestRapport:
    def test_200_rend_reported(self):
        adapter, calls = _adapter(default=REPORTED)
        out = _run(adapter.tool_subsession_report("ss-ab12cd34", {"text": " Devis : 12 400 EUR HT. "}))
        assert out["status"] == "reported"
        assert out["delivered"] is True
        assert calls[0]["url"] == "http://pulse-chat.test/api/agent/subsessions/report"
        assert json.loads(calls[0]["body"]) == {"channel": "ss-ab12cd34", "text": "Devis : 12 400 EUR HT."}

    def test_non_livre_nest_pas_un_echec(self):
        adapter, _ = _adapter(default=(200, {"subsession": "s", "status": "reported", "delivered": False}))
        out = _run(adapter.tool_subsession_report("ss-x", {"text": "Fini"}))
        assert out["status"] == "reported"
        assert out["delivered"] is False

    @pytest.mark.parametrize(
        "status,code",
        [
            (409, "not_subsession_opener"),
            (409, "subsession_closed"),
            (400, "text_required"),
            (400, "text_too_long"),
            (409, "agent_session_required"),
        ],
    )
    def test_refus_relaye(self, status, code):
        adapter, _ = _adapter([_h3(status, code)])
        out = _run(adapter.tool_subsession_report("ss-x", {"text": "Fini"}))
        assert out["status"] == "refused"
        assert out["code"] == code


class TestHandlers:
    def test_ouverture_le_canal_vient_de_la_session_sur_la_boucle_du_ws(self):
        adapter, calls = _adapter()
        with _WsLoop() as ws_loop:
            adapter._loop = ws_loop
            adapter.is_connected = True
            out = _run(adapter_module._pulse_open_subsession(dict(OPEN_ARGS, channel="autre-canal")))
        assert out["status"] == "opened"
        sent = json.loads(calls[0]["body"])
        # Le canal vient de la SESSION ; un `channel` glisse dans les arguments
        # n'est ni lu ni transporte.
        assert sent["channel"] == "tt-atlas"
        assert "emitterProfile" not in sent

    def test_rapport_le_canal_est_la_sous_session_en_cours(self):
        SESSION["HERMES_SESSION_CHAT_ID"] = "ss-ab12cd34"
        adapter, calls = _adapter(default=REPORTED)
        with _WsLoop() as ws_loop:
            adapter._loop = ws_loop
            adapter.is_connected = True
            out = _run(adapter_module._pulse_subsession_report({"text": "Fini", "channel": "tt-atlas"}))
        assert out["status"] == "reported"
        assert json.loads(calls[0]["body"])["channel"] == "ss-ab12cd34"

    @pytest.mark.parametrize(
        "handler", ["_pulse_open_subsession", "_pulse_subsession_report"]
    )
    def test_refuse_sans_conversation(self, handler):
        SESSION.pop("HERMES_SESSION_CHAT_ID")
        out = _run(getattr(adapter_module, handler)(dict(OPEN_ARGS, text="x")))
        assert out["code"] == "no_channel"
        ss = sys.modules["pulse_chat_plugin_under_test.subsessions"]
        assert out["next"] == ss._ADVICE["no_channel"]

    @pytest.mark.parametrize(
        "handler", ["_pulse_open_subsession", "_pulse_subsession_report"]
    )
    def test_refuse_sans_adaptateur_connecte(self, handler):
        out = _run(getattr(adapter_module, handler)(dict(OPEN_ARGS, text="x")))
        assert out["code"] == "not_connected"
        ss = sys.modules["pulse_chat_plugin_under_test.subsessions"]
        assert out["next"] == ss._ADVICE["not_connected"]


class TestEnregistrement:
    class _Ctx:
        def __init__(self, fail_on=None, shadow=None):
            self.tools = {}
            self.platform = None
            self.fail_on = fail_on
            self.shadow = shadow

        def register_platform(self, **kw):
            self.platform = kw

        def register_tool(self, **kw):
            if kw["name"] == self.fail_on:
                raise RuntimeError("boom")
            if kw["name"] == self.shadow:
                return None
            self.tools[kw["name"]] = kw
            return object()

        def register_skill(self, *a, **kw):
            pass

    @pytest.mark.parametrize(
        "name,handler",
        [
            ("pulse_open_subsession", "_pulse_open_subsession"),
            ("pulse_subsession_report", "_pulse_subsession_report"),
        ],
    )
    def test_enregistre_dans_le_toolset_de_la_plateforme(self, name, handler):
        ctx = self._Ctx()
        adapter_module.register(ctx)
        tool = ctx.tools[name]
        assert tool["toolset"] == "pulse_chat"
        assert tool["is_async"] is True
        assert tool["handler"] is getattr(adapter_module, handler)
        assert tool["schema"]["name"] == name

    def test_platform_hint_nomme_les_deux_outils(self):
        ctx = self._Ctx()
        adapter_module.register(ctx)
        hint = ctx.platform["platform_hint"]
        assert "pulse_open_subsession" in hint
        assert "pulse_subsession_report" in hint
        assert "never invent a profile" in hint

    @pytest.mark.parametrize(
        "name,key",
        [
            ("pulse_open_subsession", "subsession_open_tool_shadowed"),
            ("pulse_subsession_report", "subsession_report_tool_shadowed"),
        ],
    )
    def test_un_nom_deja_pris_est_signale(self, monkeypatch, name, key):
        warned = []
        monkeypatch.setattr(adapter_module, "_warn_once", lambda k, msg: warned.append(k))
        ctx = self._Ctx(shadow=name)
        adapter_module.register(ctx)
        assert warned.count(key) == 1
        assert ctx.platform is not None

    def test_son_echec_nempeche_pas_les_autres(self, monkeypatch):
        warned = []
        monkeypatch.setattr(adapter_module, "_warn_once", lambda k, msg: warned.append(k))
        ctx = self._Ctx(fail_on="pulse_open_subsession")
        adapter_module.register(ctx)
        assert "subsession_open_tool_failed" in warned
        assert "pulse_subsession_report" in ctx.tools
        assert "pulse_podcast" in ctx.tools
        assert "pulse_request_approval" in ctx.tools
        assert ctx.platform is not None
