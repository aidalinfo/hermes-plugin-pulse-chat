# -*- coding: utf-8 -*-
"""Tests de la reconnexion automatique du plugin Pulse Chat.

Contexte (incident 2026-08-05) : un redeploiement de l'app coupe le WS ; le
gateway Hermes ne retente qu'une fois, immediatement, pendant que l'app rend
encore 502 — les agents restaient deconnectes jusqu'a un restart manuel.
L'adaptateur porte desormais SA PROPRE boucle de reconnexion avec backoff.

S'executent SANS hermes installe (stubs gateway.* de test_adapter_dedup).
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

from test_adapter_dedup import _load_adapter_module

_PLUGIN_DIR = Path(__file__).resolve().parents[1]

adapter_module = _load_adapter_module()


def _load_reconnect_module():
    name = "pulse_chat_plugin_under_test.reconnect"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / "reconnect.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reconnect = _load_reconnect_module()


# ── Politique pure ───────────────────────────────────────────────────────


def test_reconnect_delay_progression_puis_plafond():
    delays = [reconnect.reconnect_delay(n) for n in range(1, 9)]
    assert delays == [2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 120.0, 120.0]


def test_reconnect_delay_attempt_invalide():
    assert reconnect.reconnect_delay(0) == 0.0
    assert reconnect.reconnect_delay(-3) == 0.0


def test_handshake_status_websockets_moderne():
    response = types.SimpleNamespace(status_code=502)
    exc = types.SimpleNamespace(response=response)
    assert reconnect.handshake_status(exc) == 502


def test_handshake_status_websockets_legacy():
    exc = types.SimpleNamespace(status_code=401)
    assert reconnect.handshake_status(exc) == 401


def test_handshake_status_erreur_reseau_sans_statut():
    assert reconnect.handshake_status(ConnectionError("refused")) is None


def test_retryable_selon_statut():
    # 401/403 = token invalide : inutile de s'acharner.
    for status in (401, 403):
        exc = types.SimpleNamespace(status_code=status)
        assert reconnect.is_retryable_ws_error(exc) is False
    # 502 (deploiement en cours), 500, ou erreur reseau : on repart.
    for status in (500, 502, 503):
        exc = types.SimpleNamespace(status_code=status)
        assert reconnect.is_retryable_ws_error(exc) is True
    assert reconnect.is_retryable_ws_error(ConnectionError("reset")) is True


# ── Comportement adaptateur ──────────────────────────────────────────────


class _Config:
    extra = {"url": "http://pulse-chat.test", "token": "token-test"}


def _make_adapter():
    return adapter_module.PulseChatAdapter(_Config())


def test_boucle_reconnecte_jusqu_au_succes(monkeypatch):
    """3 echecs retryables puis succes : la boucle insiste et s'arrete au vert."""
    adapter = _make_adapter()
    attempts = []

    async def fake_connect(*, is_reconnect=False):
        attempts.append(is_reconnect)
        adapter._last_connect_retryable = True
        return len(attempts) >= 4

    adapter.connect = fake_connect
    monkeypatch.setattr(adapter_module, "reconnect_delay", lambda n: 0.0)

    asyncio.run(adapter._reconnect_loop())

    assert len(attempts) == 4
    assert all(attempts), "toutes les tentatives doivent etre marquees is_reconnect"


def test_boucle_abandonne_sur_non_retryable(monkeypatch):
    """Token invalide (401) : une tentative, fatal notifie au gateway, stop."""
    adapter = _make_adapter()
    attempts = []
    notified = []

    async def fake_connect(*, is_reconnect=False):
        attempts.append(1)
        adapter._last_connect_retryable = False
        return False

    async def fake_notify():
        notified.append(1)

    adapter.connect = fake_connect
    adapter._notify_fatal_error = fake_notify
    monkeypatch.setattr(adapter_module, "reconnect_delay", lambda n: 0.0)

    asyncio.run(adapter._reconnect_loop())

    assert len(attempts) == 1, "pas d'acharnement sur une erreur non-retryable"
    assert notified == [1], "le gateway doit etre prevenu quand on abandonne"


def test_coupure_ws_declenche_reconnexion_sans_fatal(monkeypatch):
    """Fin inattendue de la boucle de reception : reconnexion locale, PAS de
    _notify_fatal_error (c'est lui qui poussait le gateway a abandonner)."""
    adapter = _make_adapter()
    adapter._mark_connected()
    launched = []
    notified = []

    async def fake_reconnect_loop():
        launched.append(1)

    async def fake_notify():
        notified.append(1)

    adapter._reconnect_loop = fake_reconnect_loop
    adapter._notify_fatal_error = fake_notify

    class _DeadWs:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise ConnectionError("no close frame received or sent")

        async def close(self):
            return None

    adapter._ws = _DeadWs()

    asyncio.run(adapter._receive_loop())
    # La tache de reconnexion est creee dans la boucle d'evenements du run() —
    # on verifie qu'elle a ete lancee via le recorder.
    assert launched == [1], "la boucle de reconnexion doit etre lancee"
    assert notified == [], "pas de fatal gateway sur une coupure retryable"
    assert adapter.is_connected is False


def test_disconnect_volontaire_annule_la_reconnexion():
    """disconnect() (arret propre) ne doit pas laisser une boucle zombie."""
    adapter = _make_adapter()

    async def run():
        started = asyncio.Event()

        async def endless_loop():
            started.set()
            await asyncio.sleep(3600)

        adapter._reconnect_task = asyncio.get_running_loop().create_task(endless_loop())
        await started.wait()
        await adapter.disconnect()
        assert adapter._reconnect_task is None

    asyncio.run(run())
