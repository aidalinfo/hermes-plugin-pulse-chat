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
    assert delays == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]
    assert reconnect.reconnect_delay(10_000) == 60.0


def test_reconnect_delay_attempt_invalide():
    assert reconnect.reconnect_delay(0) == 0.0
    assert reconnect.reconnect_delay(-3) == 0.0


def test_auth_retry_delay_long_puis_plafond():
    delays = [reconnect.auth_retry_delay(n) for n in range(1, 8)]
    assert delays == [30.0, 60.0, 120.0, 240.0, 480.0, 600.0, 600.0]
    assert reconnect.auth_retry_delay(0) == 0.0
    assert reconnect.auth_retry_delay(10_000) == 600.0


def test_jittered_reste_entre_la_moitie_et_le_delai():
    assert reconnect.jittered(60.0, rng=lambda: 0.0) == 30.0
    assert reconnect.jittered(60.0, rng=lambda: 0.999999) <= 60.0
    for _ in range(200):
        assert 15.0 <= reconnect.jittered(30.0) <= 30.0


def test_is_auth_rejection():
    for status in (401, 403):
        assert reconnect.is_auth_rejection(types.SimpleNamespace(status_code=status))
    assert not reconnect.is_auth_rejection(types.SimpleNamespace(status_code=502))
    assert not reconnect.is_auth_rejection(ConnectionError("reset"))


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
    # 401/403 : pas de backoff normal (la boucle les retente lentement).
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


def test_boucle_retente_un_403_passager_sans_abandon(monkeypatch):
    """Incident 2026-11-11 : 403 du bouncer CrowdSec (IP bannie) puis levee du
    ban. La boucle ne doit pas abandonner : backoff plus lent, puis reconnexion."""
    adapter = _make_adapter()
    attempts = []
    notified = []
    delays = []

    async def fake_connect(*, is_reconnect=False):
        attempts.append(1)
        if len(attempts) <= 3:
            adapter._last_connect_retryable = False
            adapter._last_connect_auth_rejected = True
            return False
        adapter._last_connect_retryable = True
        adapter._last_connect_auth_rejected = False
        return True

    async def fake_notify():
        notified.append(1)

    async def fake_sleep(delay):
        delays.append(delay)

    adapter.connect = fake_connect
    adapter._notify_fatal_error = fake_notify
    monkeypatch.setattr(adapter_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(adapter_module, "jittered", lambda d: d)

    asyncio.run(adapter._reconnect_loop())

    assert len(attempts) == 4, "le 403 passager ne doit pas arreter la boucle"
    assert notified == [], "pas de fatal gateway sur un 401/403 en reconnexion"
    # 1re tentative au backoff normal, puis delais longs apres chaque refus.
    assert delays == [2.0, 30.0, 60.0, 120.0]


def test_boucle_revient_au_backoff_normal_apres_un_403(monkeypatch):
    """403 puis 502 : le refus d'auth ne fige pas le backoff lent."""
    adapter = _make_adapter()
    outcomes = [(False, True), (True, False), (True, None)]
    delays = []

    async def fake_connect(*, is_reconnect=False):
        retryable, auth_rejected = outcomes.pop(0)
        adapter._last_connect_retryable = retryable
        adapter._last_connect_auth_rejected = bool(auth_rejected)
        return auth_rejected is None

    async def fake_sleep(delay):
        delays.append(delay)

    adapter.connect = fake_connect
    monkeypatch.setattr(adapter_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(adapter_module, "jittered", lambda d: d)

    asyncio.run(adapter._reconnect_loop())

    assert delays == [2.0, 30.0, 8.0]


def test_connect_marque_le_refus_403(monkeypatch):
    """Un handshake rejete en 403 est marque comme refus d'auth (non-retryable
    au demarrage, retente lentement par la boucle)."""
    adapter = _make_adapter()
    adapter.base_url = "https://pulse-chat.test"

    class _Rejected(Exception):
        def __init__(self):
            super().__init__("server rejected WebSocket connection: HTTP 403")
            self.response = types.SimpleNamespace(status_code=403)

    async def _refuse():
        raise _Rejected()

    fake_ws = types.SimpleNamespace(connect=lambda *a, **kw: _refuse())
    monkeypatch.setitem(sys.modules, "websockets", fake_ws)

    assert asyncio.run(adapter.connect(is_reconnect=True)) is False
    assert adapter._last_connect_auth_rejected is True
    assert adapter._last_connect_retryable is False
