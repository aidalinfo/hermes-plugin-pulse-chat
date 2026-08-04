# -*- coding: utf-8 -*-
"""Tests de la dedup du rejeu cote adaptateur Pulse Chat.

S'executent SANS hermes installe : des stubs minimalistes de ``gateway.*``
sont injectes dans ``sys.modules`` avant de charger ``adapter.py`` (le paquet
``pulse-chat`` contient un tiret — chargement par chemin, avec un paquet
synthetique pour que ``from .classification import ...`` se resolve).
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _install_gateway_stubs() -> None:
    if "gateway" in sys.modules:
        return

    gateway = types.ModuleType("gateway")
    platforms = types.ModuleType("gateway.platforms")
    base = types.ModuleType("gateway.platforms.base")
    config_mod = types.ModuleType("gateway.config")

    class BasePlatformAdapter:
        def __init__(self, config=None, platform=None):
            self.config = config
            self.platform = platform
            self.is_connected = False

        def build_source(self, **kwargs):
            return dict(kwargs)

        async def handle_message(self, event):  # remplace dans les tests
            return None

        def _mark_connected(self):
            self.is_connected = True

        def _mark_disconnected(self):
            self.is_connected = False

        def _set_fatal_error(self, kind, message, retryable=False):
            self.last_error = (kind, message, retryable)

        async def _notify_fatal_error(self):
            return None

    class MessageEvent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class MessageType:
        TEXT = "text"

    class SendResult:
        def __init__(self, success=False, message_id=None, error=None,
                     retryable=False, error_kind=None):
            self.success = success
            self.message_id = message_id
            self.error = error
            self.retryable = retryable
            self.error_kind = error_kind

    base.BasePlatformAdapter = BasePlatformAdapter
    base.MessageEvent = MessageEvent
    base.MessageType = MessageType
    base.SendResult = SendResult
    config_mod.Platform = lambda name: name

    gateway.platforms = platforms
    platforms.base = base
    gateway.config = config_mod

    sys.modules["gateway"] = gateway
    sys.modules["gateway.platforms"] = platforms
    sys.modules["gateway.platforms.base"] = base
    sys.modules["gateway.config"] = config_mod


def _load_adapter_module():
    _install_gateway_stubs()
    pkg_name = "pulse_chat_plugin_under_test"
    if pkg_name not in sys.modules:
        package = types.ModuleType(pkg_name)
        package.__path__ = [str(_PLUGIN_DIR)]
        sys.modules[pkg_name] = package
    module_name = f"{pkg_name}.adapter"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name, _PLUGIN_DIR / "adapter.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


adapter_module = _load_adapter_module()


class _Config:
    extra = {"url": "http://pulse-chat.test", "token": "token-test"}


def _make_adapter():
    adapter = adapter_module.PulseChatAdapter(_Config())
    dispatched = []
    acked = []

    async def fake_handle_message(event):
        dispatched.append(event.message_id)

    async def fake_send_ack(message_id):
        acked.append(message_id)

    adapter.handle_message = fake_handle_message
    adapter._send_ack = fake_send_ack
    return adapter, dispatched, acked


def _event(message_id, text="bonjour"):
    return {
        "type": "message.created",
        "channel": {"slug": "canal-test", "name": "Canal Test"},
        "message": {"id": message_id, "text": text, "userId": "u1", "userName": "Alice"},
    }


def test_same_message_id_twice_dispatches_once_acks_twice():
    adapter, dispatched, acked = _make_adapter()

    async def run():
        await adapter._handle_message_created(_event("msg-1"))
        await adapter._handle_message_created(_event("msg-1"))

    asyncio.run(run())

    assert dispatched == ["msg-1"], "un seul dispatch attendu pour un id deja vu"
    assert acked == ["msg-1", "msg-1"], "chaque reception doit etre ackee"


def test_distinct_message_ids_all_dispatched():
    adapter, dispatched, acked = _make_adapter()

    async def run():
        await adapter._handle_message_created(_event("msg-a"))
        await adapter._handle_message_created(_event("msg-b"))

    asyncio.run(run())

    assert dispatched == ["msg-a", "msg-b"]
    assert acked == ["msg-a", "msg-b"]


def test_cache_is_bounded_fifo():
    adapter, dispatched, acked = _make_adapter()
    limit = adapter_module._DEDUP_MAX_IDS

    async def run():
        for i in range(limit + 10):
            await adapter._handle_message_created(_event(f"msg-{i}"))

    asyncio.run(run())

    assert len(adapter._seen_message_ids) == limit
    # Les plus anciens sont evinces, les plus recents restent.
    assert "msg-0" not in adapter._seen_message_ids
    assert f"msg-{limit + 9}" in adapter._seen_message_ids


def test_failed_dispatch_is_not_remembered():
    """Si handle_message echoue (pas d'ack), le rejeu doit re-dispatcher."""
    adapter, dispatched, acked = _make_adapter()
    calls = {"n": 0}

    async def flaky_handle_message(event):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("agent indisponible")
        dispatched.append(event.message_id)

    adapter.handle_message = flaky_handle_message

    async def run():
        with pytest.raises(RuntimeError):
            await adapter._handle_message_created(_event("msg-retry"))
        await adapter._handle_message_created(_event("msg-retry"))

    asyncio.run(run())

    assert dispatched == ["msg-retry"]
    assert acked == ["msg-retry"]
