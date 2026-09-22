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


def _connected(adapter, loop):
    adapter._loop = loop
    adapter.is_connected = True


class TestOutil:
    def test_la_decision_atteint_loutil_depuis_la_boucle_du_ws(self):
        adapter, _ = _adapter()
        with _WsLoop() as ws_loop:
            _connected(adapter, ws_loop)
            seen = {}

            async def fake_open(chat_id, request_id, title, body):
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

            async def fake_open(*_a):
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

            async def fake_open(*_a):
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
        calls = {"tool": None, "skill": None, "platform": None}

        class Ctx:
            def register_platform(self, **kw):
                calls["platform"] = kw

            def register_tool(self, **kw):
                calls["tool"] = kw
                return object()

            def register_skill(self, name, path, description=""):
                calls["skill"] = (name, path)
                return object()

        return Ctx(), calls

    def test_enregistre_loutil_prefixe_dans_le_toolset_de_la_plateforme(self):
        ctx, calls = self._ctx()
        adapter_module.register(ctx)
        tool = calls["tool"]
        assert tool["name"] == "pulse_request_approval"
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
