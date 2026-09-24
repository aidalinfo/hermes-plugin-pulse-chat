# -*- coding: utf-8 -*-
"""Demandes d'approbation DELIBEREES cote adaptateur (sans hermes installe).

Ce qui est verifie ici, et chaque point est un mode d'echec MUET :

  - l'outil attend sur une AUTRE boucle que celle du WebSocket (c'est ce que
    fait ``model_tools._run_async``) et la decision l'atteint quand meme ;
  - une decision qui arrive sans attente active (fenetre depassee, bot
    redemarre) est REMISE a l'agent en message, pas seulement journalisee ;
  - la meme decision rejouee au hello ne produit qu'UN message ;
  - un refus de l'app arrive a l'agent avec son code, jamais comme un accord ;
  - l'outil et le skill sont enregistres, sous un nom PREFIXE.

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

SESSION = {"HERMES_SESSION_CHAT_ID": "compta"}


def _install_session_context():
    mod = types.ModuleType("gateway.session_context")
    mod.get_session_env = lambda name, default="": SESSION.get(name, default)
    sys.modules["gateway.session_context"] = mod


_install_session_context()


class _Config:
    extra = {"url": "http://pulse-chat.test", "token": "token-test"}


def _reply(request_id, decision="approved", comment=None, slug="compta"):
    return {
        "type": "gate.reply",
        "channel": {"slug": slug, "name": "Compta"},
        "gate": {
            "requestId": request_id,
            "title": "Plan d'extraction",
            "decision": decision,
            "comment": comment,
            "decidedBy": {"userId": "u1", "userName": "Killian"},
            "decidedAt": "2026-09-22T10:00:00.000Z",
        },
    }


def _adapter():
    adapter = adapter_module.PulseChatAdapter(_Config())
    dispatched = []

    async def fake_handle_message(event):
        dispatched.append(event)

    adapter.handle_message = fake_handle_message
    return adapter, dispatched


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


@pytest.fixture(autouse=True)
def _aucun_adaptateur_residuel():
    """Isole les tests : ``_live_adapter()`` prend le PREMIER adaptateur
    connecte du ``WeakSet``. Un adaptateur d'un test precedent, garde en vie par
    un cycle (``adapter.open_gate = fake_open`` capture ``adapter``) jusqu'au
    prochain passage du ramasse-miettes, restait « connecte » sur une boucle
    deja arretee : l'outil y planifiait son POST et attendait pour toujours. Le
    blocage dependait du moment ou le GC passait — donc de l'ordre et du nombre
    de tests."""

    def _deconnecter():
        for a in list(adapter_module._LIVE_ADAPTERS):
            a.is_connected = False

    _deconnecter()
    yield
    _deconnecter()


def _connected(adapter, loop):
    adapter._loop = loop
    adapter.is_connected = True


class TestOutil:
    def test_la_decision_atteint_loutil_depuis_la_boucle_du_ws(self):
        adapter, _ = _adapter()
        with _WsLoop() as ws_loop:
            _connected(adapter, ws_loop)
            seen = {}

            async def fake_open(chat_id, request_id, title, body, structured=None):
                seen.update(chat_id=chat_id, title=title)
                # La decision arrive par le WebSocket, sur SA boucle.
                asyncio.get_running_loop().call_soon(
                    lambda: asyncio.ensure_future(
                        adapter._handle_gate_reply(_reply(request_id, "changes_requested", "Pas 2025"))
                    )
                )
                return None

            adapter.open_gate = fake_open
            out = json.loads(
                asyncio.run(adapter_module._pulse_request_approval({"title": "Plan", "body": "1. a"}))
            )
        assert seen == {"chat_id": "compta", "title": "Plan"}
        assert out["status"] == "changes_requested"
        assert out["granted"] is False
        assert out["comment"] == "Pas 2025"
        assert adapter._pending_gates == {}

    def test_rend_pending_passe_la_fenetre_et_retire_lattente(self, monkeypatch):
        monkeypatch.setattr(adapter_module, "GATE_WAIT_SECONDS", 0.05)
        adapter, _ = _adapter()
        with _WsLoop() as ws_loop:
            _connected(adapter, ws_loop)

            async def fake_open(*_a, **_kw):
                return None

            adapter.open_gate = fake_open
            out = json.loads(
                asyncio.run(adapter_module._pulse_request_approval({"title": "Plan", "body": "b"}))
            )
        assert out["status"] == "pending"
        assert out["granted"] is False
        # Retiree : la decision tardive prendra le chemin du message.
        assert adapter._pending_gates == {}

    def test_un_refus_de_lapp_arrive_tel_quel(self):
        adapter, _ = _adapter()
        with _WsLoop() as ws_loop:
            _connected(adapter, ws_loop)

            async def fake_open(*_a, **_kw):
                return adapter_module.refused_result("no_approver_configured", "Aucun approbateur")

            adapter.open_gate = fake_open
            out = json.loads(
                asyncio.run(adapter_module._pulse_request_approval({"title": "Plan", "body": "b"}))
            )
        assert out == {**out, "status": "refused", "granted": False, "code": "no_approver_configured"}

    def test_refuse_sans_conversation(self):
        SESSION.pop("HERMES_SESSION_CHAT_ID")
        try:
            out = json.loads(
                asyncio.run(adapter_module._pulse_request_approval({"title": "Plan", "body": "b"}))
            )
        finally:
            SESSION["HERMES_SESSION_CHAT_ID"] = "compta"
        assert out["code"] == "no_channel"
        assert out["granted"] is False

    def test_refuse_sans_adaptateur_connecte(self):
        for a in list(adapter_module._LIVE_ADAPTERS):
            a.is_connected = False
        out = json.loads(
            asyncio.run(adapter_module._pulse_request_approval({"title": "Plan", "body": "b"}))
        )
        assert out["code"] == "not_connected"


class TestDecisionTardive:
    def test_est_remise_a_lagent_en_message(self):
        adapter, dispatched = _adapter()
        asyncio.run(adapter._handle_gate_reply(_reply("gate-late", "approved")))
        assert len(dispatched) == 1
        event = dispatched[0]
        assert event.message_id == "gate:gate-late"
        assert event.source["chat_id"] == "compta"
        assert "APPROUVE" in event.text
        assert "Plan d'extraction" in event.text

    def test_un_rejeu_ne_produit_quun_message(self):
        """Deux « approuve » feraient executer deux fois le meme plan."""
        adapter, dispatched = _adapter()

        async def run():
            await adapter._handle_gate_reply(_reply("gate-dup"))
            await adapter._handle_gate_reply(_reply("gate-dup"))

        asyncio.run(run())
        assert len(dispatched) == 1

    def test_une_trame_inexploitable_ne_produit_rien(self):
        adapter, dispatched = _adapter()
        asyncio.run(adapter._handle_gate_reply({"type": "gate.reply", "gate": {"decision": "once"}}))
        assert dispatched == []


class TestEnregistrement:
    def _ctx(self):
        # Les outils par NOM : le plugin en enregistre plusieurs, et ne garder
        # que le dernier ferait tester l'outil de coffre a la place de celui-ci.
        calls = {"tools": {}, "skill": None, "platform": None}

        class Ctx:
            def register_platform(self, **kw):
                calls["platform"] = kw

            def register_tool(self, **kw):
                calls["tools"][kw["name"]] = kw
                return object()

            def register_skill(self, name, path, description=""):
                calls["skill"] = (name, path)
                return object()

        return Ctx(), calls

    def test_enregistre_loutil_prefixe_dans_le_toolset_de_la_plateforme(self):
        ctx, calls = self._ctx()
        adapter_module.register(ctx)
        tool = calls["tools"]["pulse_request_approval"]
        assert tool["toolset"] == "pulse_chat"
        assert tool["is_async"] is True

    def test_enregistre_le_skill_et_son_fichier_existe(self):
        ctx, calls = self._ctx()
        adapter_module.register(ctx)
        name, path = calls["skill"]
        assert name == "approvals"
        assert path.exists(), "register_skill leve FileNotFoundError sur un chemin absent"

    def test_platform_hint_nomme_loutil_et_le_skill(self):
        ctx, calls = self._ctx()
        adapter_module.register(ctx)
        hint = calls["platform"]["platform_hint"]
        assert "pulse_request_approval" in hint
        assert "pulse-chat:approvals" in hint

    def test_un_echec_denregistrement_ne_fait_pas_tomber_la_plateforme(self):
        class Ctx:
            platform = None

            def register_platform(self, **kw):
                Ctx.platform = kw

            def register_tool(self, **kw):
                raise TypeError("ancien Hermes")

            def register_skill(self, *a, **kw):
                raise AttributeError("ancien Hermes")

        adapter_module.register(Ctx())
        assert Ctx.platform is not None


class TestRubriquesEtRepli:
    """Le transport des rubriques, et le repli face a une app ANTERIEURE."""

    def _http_error(self, status, body):
        import io
        import urllib.error

        return urllib.error.HTTPError(
            "http://pulse-chat.test/api/agent/messages", status, "err", {}, io.BytesIO(json.dumps(body).encode())
        )

    def _run_open(self, adapter, responses, **kw):
        posted = []

        def fake_post(url, payload):
            posted.append(payload)
            outcome = responses.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        adapter._post_json = fake_post
        body = kw.pop("body", "")
        out = asyncio.run(adapter.open_gate("compta", "gate-1", "Plan", body, **kw))
        return out, posted

    def test_une_app_ancienne_recoit_la_demande_repliee_en_titre_et_corps(self):
        adapter, _ = _adapter()
        old_app = self._http_error(
            400, {"statusMessage": "Payload agent invalide", "data": [{"code": "unrecognized_keys"}]}
        )
        out, posted = self._run_open(
            adapter, [old_app, 200], structured={"steps": [{"label": "Redemarrer"}], "risk": "high"}
        )
        assert out is None
        assert len(posted) == 2
        assert "steps" in posted[0]
        assert set(posted[1]) == {"kind", "channelSlug", "requestId", "title", "body"}
        assert "Redemarrer" in posted[1]["body"]
        # Meme requestId : une reemission reste idempotente cote app.
        assert posted[1]["requestId"] == posted[0]["requestId"]

    def test_une_piece_introuvable_nest_JAMAIS_contournee_par_le_repli(self):
        adapter, _ = _adapter()
        refus = self._http_error(
            400, {"statusMessage": "Aucun fichier", "data": {"code": "gate_attachment_not_found"}}
        )
        out, posted = self._run_open(adapter, [refus], structured={"attachments": ["vault:absent.pdf"]})
        assert len(posted) == 1
        assert json.loads(out)["code"] == "gate_attachment_not_found"

    def test_sans_rubrique_aucun_repli_meme_sur_un_champ_inconnu(self):
        adapter, _ = _adapter()
        old_app = self._http_error(400, {"data": [{"code": "unrecognized_keys"}]})
        out, posted = self._run_open(adapter, [old_app], body="b")
        assert len(posted) == 1
        assert json.loads(out)["granted"] is False

    def test_loutil_transporte_les_rubriques_et_accepte_un_corps_absent(self):
        adapter, _ = _adapter()
        with _WsLoop() as ws_loop:
            _connected(adapter, ws_loop)
            seen = {}

            async def fake_open(chat_id, request_id, title, body, structured=None):
                seen.update(body=body, structured=structured)
                return adapter_module.refused_result("no_approver_configured", "x")

            adapter.open_gate = fake_open
            asyncio.run(
                adapter_module._pulse_request_approval(
                    {"title": "Plan", "steps": [{"label": "a", "command": "ls"}], "reversible": True}
                )
            )
        assert seen["body"] == ""
        assert seen["structured"] == {"steps": [{"label": "a", "command": "ls"}], "reversible": True}

    def test_loutil_refuse_une_demande_vide_sans_rien_poster(self):
        adapter, _ = _adapter()
        with _WsLoop() as ws_loop:
            _connected(adapter, ws_loop)
            called = []

            async def fake_open(*_a, **_kw):
                called.append(1)
                return None

            adapter.open_gate = fake_open
            out = json.loads(asyncio.run(adapter_module._pulse_request_approval({"title": "Plan"})))
        assert called == []
        assert out["code"] == "gate_body_required"
        assert out["granted"] is False
