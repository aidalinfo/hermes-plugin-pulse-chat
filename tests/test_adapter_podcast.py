# -*- coding: utf-8 -*-
"""Outil ``pulse_podcast`` cote adaptateur (sans hermes installe).

Ce qui est verifie ici, et chaque point est un mode d'echec MUET :

  - le canal vient du contexte de session, jamais d'un argument ;
  - le handler tourne sur une AUTRE boucle que celle du WebSocket (c'est ce
    que fait ``model_tools._run_async``) et atteint quand meme l'adaptateur ;
  - l'outil rend AUSSITOT ``queued`` sur un 202, sans attendre la synthese ;
  - aucun ``queued`` sans 2xx : un refus arrive avec son code et son message ;
  - l'outil est enregistre sous un nom PREFIXE, et un nom deja pris se dit.

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

SESSION = {"HERMES_SESSION_CHAT_ID": "point-hebdo"}

CHAPTERS = [
    {"title": "Les chiffres", "summary": "", "text": "  Ce mois-ci, le chiffre d'affaires progresse.  "},
    {"title": "La suite", "text": "Nous allons ensuite ouvrir deux agences."},
]


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


def _adapter(responses=None):
    adapter = adapter_module.PulseChatAdapter(_Config())
    calls = []
    queue = list(responses or [])

    def fake_http(method, url, *, body=None, headers=None, timeout=None):
        calls.append({"method": method, "url": url, "body": body, "headers": headers or {}})
        return queue.pop(0) if queue else (202, {"podcastId": "pod_1", "status": "queued"})

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


class TestMethode:
    def test_202_rend_queued_aussitot(self):
        adapter, calls = _adapter()
        out = _run(adapter.tool_podcast("point-hebdo", {"title": " Point ", "chapters": CHAPTERS}))
        assert out["status"] == "queued"
        assert out["podcastId"] == "pod_1"
        assert "Ne le recopie pas" in out["next"]
        assert len(calls) == 1
        call = calls[0]
        assert call["method"] == "POST"
        assert call["url"] == "http://pulse-chat.test/api/agent/podcasts/point-hebdo"
        assert call["headers"]["Content-Type"] == "application/json"
        sent = json.loads(call["body"])
        assert sent == {
            "title": "Point",
            "chapters": [
                {"title": "Les chiffres", "text": "Ce mois-ci, le chiffre d'affaires progresse."},
                {"title": "La suite", "text": "Nous allons ensuite ouvrir deux agences."},
            ],
        }

    def test_aucune_troncature_meme_hors_bornes(self):
        adapter, calls = _adapter()
        text = "Une phrase. " * 3000
        _run(adapter.tool_podcast("c", {"title": "x", "chapters": [{"title": "a", "text": text}]}))
        assert json.loads(calls[0]["body"])["chapters"][0]["text"] == text.strip()

    def test_le_slug_est_encode(self):
        adapter, calls = _adapter()
        _run(adapter.tool_podcast("a b", {"title": "x", "chapters": CHAPTERS}))
        assert calls[0]["url"].endswith("/api/agent/podcasts/a%20b")

    @pytest.mark.parametrize(
        "status,code",
        [
            (422, "voice_disabled"),
            (409, "podcast_in_progress"),
            (413, "podcast_text_too_long"),
            (400, "podcast_text_too_short"),
            (404, "channel_not_found"),
        ],
    )
    def test_refus_relaye_avec_code_et_message(self, status, code):
        adapter, _ = _adapter([(status, {"statusMessage": f"motif {code}", "data": {"code": code}})])
        out = _run(adapter.tool_podcast("c", {"title": "x", "chapters": CHAPTERS}))
        assert out["status"] == "refused"
        assert out["code"] == code
        assert out["message"] == f"motif {code}"

    def test_400_zod(self):
        adapter, _ = _adapter(
            [(400, {"statusMessage": "Payload invalide", "data": [{"path": ["title"], "message": "Too long"}]})]
        )
        out = _run(adapter.tool_podcast("c", {"title": "x", "chapters": CHAPTERS}))
        assert out["code"] == "invalid_request"
        assert "title : Too long" in out["message"]

    def test_501_synthese_indisponible(self):
        adapter, _ = _adapter([(501, {"statusMessage": "x", "data": {"code": "podcast_synthesis_unavailable"}})])
        out = _run(adapter.tool_podcast("c", {"title": "x", "chapters": CHAPTERS}))
        assert out["code"] == "podcast_synthesis_unavailable"
        assert "Synthèse indisponible sur cette instance" in out["message"]

    @pytest.mark.parametrize("status", [0, 302, 500, 503])
    def test_jamais_de_queued_sans_2xx(self, status):
        adapter, _ = _adapter([(status, {"podcastId": "pod_x", "status": "queued"})])
        out = _run(adapter.tool_podcast("c", {"title": "x", "chapters": CHAPTERS}))
        assert out["status"] == "refused"

    def test_une_exception_devient_un_refus(self):
        adapter, _ = _adapter()

        def boom(*a, **kw):
            raise RuntimeError("reseau")

        adapter._http_detailed = boom
        out = _run(adapter.tool_podcast("c", {"title": "x", "chapters": CHAPTERS}))
        assert out["status"] == "refused"
        assert out["code"] == "not_sent"


class TestHandler:
    def test_le_canal_vient_de_la_session_sur_la_boucle_du_ws(self):
        adapter, calls = _adapter()
        with _WsLoop() as ws_loop:
            adapter._loop = ws_loop
            adapter.is_connected = True
            out = _run(
                adapter_module._pulse_podcast(
                    {"title": "Point", "chapters": CHAPTERS, "channel": "autre-canal"}
                )
            )
        assert out["status"] == "queued"
        # Le canal vient de la SESSION ; un `channel` glisse dans les arguments
        # n'est ni lu ni transporte.
        assert calls[0]["url"].endswith("/api/agent/podcasts/point-hebdo")
        assert "channel" not in json.loads(calls[0]["body"])

    def test_refuse_sans_conversation(self):
        SESSION.pop("HERMES_SESSION_CHAT_ID")
        try:
            out = _run(adapter_module._pulse_podcast({"title": "x", "chapters": CHAPTERS}))
        finally:
            SESSION["HERMES_SESSION_CHAT_ID"] = "point-hebdo"
        assert out["code"] == "no_channel"
        podcast = sys.modules["pulse_chat_plugin_under_test.podcast"]
        assert out["next"] == podcast._ADVICE["no_channel"]

    def test_refuse_sans_adaptateur_connecte(self):
        out = _run(adapter_module._pulse_podcast({"title": "x", "chapters": CHAPTERS}))
        assert out["code"] == "not_connected"
        assert "podcast" in out["next"]


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

    def test_enregistre_dans_le_toolset_de_la_plateforme(self):
        ctx = self._Ctx()
        adapter_module.register(ctx)
        tool = ctx.tools["pulse_podcast"]
        assert tool["toolset"] == "pulse_chat"
        assert tool["is_async"] is True
        assert tool["emoji"] == "🎙️"
        assert tool["handler"] is adapter_module._pulse_podcast
        assert tool["schema"]["name"] == "pulse_podcast"

    def test_platform_hint_nomme_loutil(self):
        ctx = self._Ctx()
        adapter_module.register(ctx)
        hint = ctx.platform["platform_hint"]
        assert "pulse_podcast" in hint
        assert "spoken prose" in hint

    def test_un_nom_deja_pris_est_signale(self, monkeypatch):
        warned = []
        monkeypatch.setattr(adapter_module, "_warn_once", lambda key, msg: warned.append(key))
        ctx = self._Ctx(shadow="pulse_podcast")
        adapter_module.register(ctx)
        assert "podcast_tool_shadowed" in warned
        assert ctx.platform is not None

    def test_son_echec_nempeche_pas_les_autres(self, monkeypatch):
        warned = []
        monkeypatch.setattr(adapter_module, "_warn_once", lambda key, msg: warned.append(key))
        ctx = self._Ctx(fail_on="pulse_podcast")
        adapter_module.register(ctx)
        assert "podcast_tool_failed" in warned
        assert "pulse_publish_artifact" in ctx.tools
        assert "pulse_request_approval" in ctx.tools
