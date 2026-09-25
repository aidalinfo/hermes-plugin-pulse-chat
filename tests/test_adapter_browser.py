# -*- coding: utf-8 -*-
"""Rendu de main sur le navigateur, cote adaptateur (sans hermes installe).

Le mode d'echec vise est MUET : un humain rend la main alors que
``pulse_browser_handoff`` n'attend plus (fenetre de 270 s depassee, ou tour
deja fini) — sans message entrant, personne ne relance l'agent et il ne
reprend jamais sa tache.

Style du depot : ``asyncio.run`` plutot que pytest-asyncio.
"""

import asyncio
import json
import threading
from unittest import mock

import pytest

from test_adapter_dedup import _load_adapter_module

adapter_module = _load_adapter_module()
browser = adapter_module.browser_provider

SESSIONS = "/api/agent/browser/sessions"


class _Config:
    extra = {"url": "http://pulse-chat.test", "token": "token-test"}


def _adapter():
    adapter = adapter_module.PulseChatAdapter(_Config())
    dispatched = []

    async def fake_handle_message(event):
        dispatched.append(event)

    adapter.handle_message = fake_handle_message
    return adapter, dispatched


def _rendu(event="released", turn_ended=True, slug="compta"):
    return {
        "type": "browser.control",
        "sessionId": "s1",
        "controller": "agent",
        "by": "Killian",
        "event": event,
        "turnEnded": turn_ended,
        "channelSlug": slug,
        "channelName": "Compta",
    }


def _provider(wait_seconds=5.0):
    posted = threading.Event()

    def http(method, path, payload, timeout):
        if method == "POST" and path == SESSIONS:
            return 200, {"sessionId": "s1", "cdpUrl": "wss://chat.test/ws/browser-cdp/j"}
        if path.endswith("/handoff"):
            posted.set()
        return 200, {"ok": True}

    provider = browser.PulseBrowserProvider(
        mock.Mock(side_effect=http),
        configured=lambda: True,
        session_env=lambda name: {"HERMES_SESSION_CHAT_ID": "compta"}.get(name, ""),
        wait_seconds=wait_seconds,
    )
    browser.activate(provider)
    return provider, posted


@pytest.fixture(autouse=True)
def _aucun_fournisseur_residuel():
    browser.deactivate()
    yield
    browser.deactivate()


def _receive(adapter, *frames):
    class _Ws:
        def __aiter__(self):
            async def gen():
                for frame in frames:
                    yield json.dumps(frame)

            return gen()

        async def close(self):
            return None

    adapter._ws = _Ws()
    asyncio.run(adapter._receive_loop())


class TestRelance:
    def test_rendu_tour_fini_sans_attente_relance_l_agent(self):
        adapter, dispatched = _adapter()
        _receive(adapter, _rendu("released", True))
        assert len(dispatched) == 1
        event = dispatched[0]
        assert event.text.startswith("[Navigateur] Killian t'a rendu la main")
        assert event.source["chat_id"] == "compta"
        assert event.source["chat_name"] == "Compta"
        assert event.message_id.startswith("browser:s1:")
        assert event.message_id in adapter._seen_message_ids

    def test_deux_rendus_deux_messages_distincts(self):
        adapter, dispatched = _adapter()
        asyncio.run(adapter._handle_browser_control(_rendu("released", True)))
        asyncio.run(adapter._handle_browser_control(_rendu("auto_released", True)))
        assert len(dispatched) == 2
        assert dispatched[0].message_id != dispatched[1].message_id
        assert dispatched[1].text.startswith("[Navigateur] La main t'a ete rendue d'office")

    def test_la_derniere_source_du_canal_est_reprise(self):
        adapter, dispatched = _adapter()
        known = {"chat_id": "compta", "chat_name": "Compta", "user_name": "Killian", "known": True}
        adapter._last_source["compta"] = known
        asyncio.run(adapter._handle_browser_control(_rendu("released", True)))
        assert dispatched[0].source is known

    def test_un_outil_en_attente_ne_produit_pas_de_message(self):
        adapter, dispatched = _adapter()
        provider, posted = _provider()
        provider.create_session("t1")
        box = {}
        thread = threading.Thread(
            target=lambda: box.update(result=provider.handoff({"reason": "x"}, task_id="t1")),
            daemon=True,
        )
        thread.start()
        assert posted.wait(2)
        _receive(adapter, _rendu("released", True))
        thread.join(2)
        assert json.loads(box["result"])["status"] == "done"
        assert dispatched == []

    def test_session_fermee_ne_produit_pas_de_message(self):
        adapter, dispatched = _adapter()
        _receive(adapter, _rendu("closed", True))
        assert dispatched == []

    def test_tour_en_cours_sans_pending_ne_l_interrompt_pas(self):
        adapter, dispatched = _adapter()
        _receive(adapter, _rendu("released", False))
        assert dispatched == []

    def test_une_erreur_ne_casse_pas_la_boucle(self, monkeypatch):
        adapter, dispatched = _adapter()

        def boom(frame):
            raise RuntimeError("boum")

        calls = []

        def boom_then_ok(frame):
            calls.append(frame)
            if len(calls) == 1:
                raise RuntimeError("boum")
            return real(frame)

        real = browser.handle_control
        monkeypatch.setattr(browser, "handle_control", boom_then_ok)
        # DEUX trames : la seconde prouve que la boucle a survecu a la premiere
        # (une seule trame passerait aussi par le except de toute la boucle).
        _receive(adapter, _rendu("released", True), _rendu("released", True))
        assert len(calls) == 2
        assert len(dispatched) == 1


class TestConfigAgent:
    def test_le_tour_relance_emporte_la_config_du_dernier_message_du_canal(self):
        """Ton, consignes et surtout `disabledTools` voyagent PAR MESSAGE : un
        tour injecte sans eux utiliserait des outils que l'admin a retires."""
        adapter, dispatched = _adapter()
        adapter._last_agent_config["compta"] = {"agentConfig": {"disabledTools": ["terminal"]}}
        _receive(adapter, _rendu("released", True))
        assert len(dispatched) == 1
        assert dispatched[0].metadata == {"agentConfig": {"disabledTools": ["terminal"]}}
