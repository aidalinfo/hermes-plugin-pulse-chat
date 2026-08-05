# -*- coding: utf-8 -*-
"""Tests de la frame hello du plugin Pulse Chat (multi-profils).

S'executent SANS hermes installe : le module pur ``hello.py`` est charge par
chemin de fichier ; les tests adaptateur reutilisent les stubs ``gateway.*``
de ``test_adapter_dedup``.
"""

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

from test_adapter_dedup import _load_adapter_module

_MODULE_PATH = Path(__file__).resolve().parents[1] / "hello.py"
_spec = importlib.util.spec_from_file_location("pulse_chat_hello", _MODULE_PATH)
hello = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hello)

parse_profiles = hello.parse_profiles
build_hello = hello.build_hello


# ── parse_profiles ───────────────────────────────────────────────────────

def test_parse_profiles_empty_defaults_to_default():
    assert parse_profiles(None) == ["default"]
    assert parse_profiles("") == ["default"]
    assert parse_profiles("  ,  ") == ["default"]
    assert parse_profiles([]) == ["default"]


def test_parse_profiles_single():
    assert parse_profiles("client-a") == ["client-a"]


def test_parse_profiles_multiple_trimmed_deduped_order_kept():
    assert parse_profiles("client-a, client-b ,client-a,default") == [
        "client-a",
        "client-b",
        "default",
    ]


def test_parse_profiles_accepts_iterables():
    assert parse_profiles(["a", " b ", "a"]) == ["a", "b"]


# ── build_hello ──────────────────────────────────────────────────────────

def test_build_hello_defaults():
    assert build_hello(None) == {
        "type": "hello",
        "profiles": ["default"],
        "agentName": "default",
    }


def test_build_hello_multiple_profiles_and_explicit_name():
    assert build_hello("client-a,client-b", "Bot Client") == {
        "type": "hello",
        "profiles": ["client-a", "client-b"],
        "agentName": "Bot Client",
    }


def test_build_hello_blank_name_falls_back_to_first_profile():
    frame = build_hello("client-a", "   ")
    assert frame["agentName"] == "client-a"


def test_build_hello_capabilities_none_key_absent():
    """capabilities=None (defaut, ou recolte en echec) => cle ABSENTE de la
    trame, jamais presente a ``null`` (contrat verrouille — pas de bruit
    inutile sur le fil pour les plugins qui ne recoltent rien)."""
    frame = build_hello("client-a", "Bot Client")
    assert "capabilities" not in frame

    frame_explicit_none = build_hello("client-a", "Bot Client", None)
    assert "capabilities" not in frame_explicit_none


def test_build_hello_capabilities_present_when_given():
    caps = {"model": "hermes-4-70b", "skills": [], "mcpServers": []}
    frame = build_hello("client-a", "Bot Client", caps)
    assert frame["capabilities"] == caps
    assert frame == {
        "type": "hello",
        "profiles": ["client-a"],
        "agentName": "Bot Client",
        "capabilities": caps,
    }


# ── Adaptateur : lecture env/extra + envoi de la frame a la connexion ────

adapter_module = _load_adapter_module()


class _Config:
    def __init__(self, extra):
        self.extra = extra


def test_adapter_reads_profiles_and_agent_name_from_extra():
    adapter = adapter_module.PulseChatAdapter(
        _Config({"url": "http://x", "token": "t", "profile": "client-a, client-b"})
    )
    assert adapter.profiles == ["client-a", "client-b"]
    assert adapter.agent_name == "client-a"  # defaut : nom du premier profil


def test_adapter_reads_profile_and_name_from_env(monkeypatch):
    monkeypatch.setenv("PULSE_CHAT_PROFILE", "client-x")
    monkeypatch.setenv("PULSE_CHAT_AGENT_NAME", "Agent X")
    adapter = adapter_module.PulseChatAdapter(_Config({"url": "http://x", "token": "t"}))
    assert adapter.profiles == ["client-x"]
    assert adapter.agent_name == "Agent X"


def test_adapter_defaults_to_default_profile():
    adapter = adapter_module.PulseChatAdapter(_Config({"url": "http://x", "token": "t"}))
    assert adapter.profiles == ["default"]
    assert adapter.agent_name == "default"


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def test_connect_sends_hello_frame_first(monkeypatch):
    """connect() envoie la frame hello (profils + agentName) des l'ouverture."""
    fake_ws = _FakeWS()
    websockets = types.ModuleType("websockets")

    def _connect(url, **kwargs):
        async def _open():
            return fake_ws

        return _open()

    websockets.connect = _connect
    monkeypatch.setitem(sys.modules, "websockets", websockets)

    adapter = adapter_module.PulseChatAdapter(
        _Config(
            {
                "url": "http://x",
                "token": "t",
                "profile": "client-a,client-b",
                "agent_name": "Bot Client",
            }
        )
    )

    async def run():
        ok = await adapter.connect()
        assert ok is True
        await adapter.disconnect()

    asyncio.run(run())

    assert len(fake_ws.sent) >= 1
    assert json.loads(fake_ws.sent[0]) == {
        "type": "hello",
        "profiles": ["client-a", "client-b"],
        "agentName": "Bot Client",
    }


def test_connect_sends_hello_without_capabilities_key_when_collection_fails(monkeypatch):
    """Sans hermes installe, ``collect_capabilities`` echoue silencieusement
    (retourne None) — la trame envoyee ne doit PAS porter de cle
    ``capabilities`` (contrat verrouille : absente, pas ``null``), et la
    connexion doit reussir malgre tout (la connexion prime sur la fiche)."""
    fake_ws = _FakeWS()
    websockets = types.ModuleType("websockets")

    def _connect(url, **kwargs):
        async def _open():
            return fake_ws

        return _open()

    websockets.connect = _connect
    monkeypatch.setitem(sys.modules, "websockets", websockets)

    adapter = adapter_module.PulseChatAdapter(
        _Config({"url": "http://x", "token": "t", "profile": "client-a"})
    )

    async def run():
        ok = await adapter.connect()
        assert ok is True
        await adapter.disconnect()

    asyncio.run(run())

    sent_frame = json.loads(fake_ws.sent[0])
    assert "capabilities" not in sent_frame


def test_connect_sends_hello_with_capabilities_when_collection_succeeds(monkeypatch):
    """Quand ``collect_capabilities`` renvoie un dictionnaire, il doit se
    retrouver tel quel dans la trame hello envoyee sur le WS."""
    fake_ws = _FakeWS()
    websockets = types.ModuleType("websockets")

    def _connect(url, **kwargs):
        async def _open():
            return fake_ws

        return _open()

    websockets.connect = _connect
    monkeypatch.setitem(sys.modules, "websockets", websockets)

    fake_caps = {"model": "hermes-4-70b", "skills": [], "mcpServers": []}
    monkeypatch.setattr(
        adapter_module, "collect_capabilities", lambda profile: fake_caps
    )

    adapter = adapter_module.PulseChatAdapter(
        _Config({"url": "http://x", "token": "t", "profile": "client-a"})
    )

    async def run():
        ok = await adapter.connect()
        assert ok is True
        await adapter.disconnect()

    asyncio.run(run())

    sent_frame = json.loads(fake_ws.sent[0])
    assert sent_frame["capabilities"] == fake_caps


def test_connect_sends_hello_when_capabilities_collection_raises(monkeypatch):
    """Meme si ``collect_capabilities`` levait (elle ne devrait jamais, mais
    l'adaptateur ne lui fait pas confiance aveuglement — cf. adapter.py), le
    hello doit quand meme partir, sans capacites."""
    fake_ws = _FakeWS()
    websockets = types.ModuleType("websockets")

    def _connect(url, **kwargs):
        async def _open():
            return fake_ws

        return _open()

    websockets.connect = _connect
    monkeypatch.setitem(sys.modules, "websockets", websockets)

    def _boom(profile):
        raise RuntimeError("collecte cassee")

    monkeypatch.setattr(adapter_module, "collect_capabilities", _boom)

    adapter = adapter_module.PulseChatAdapter(
        _Config({"url": "http://x", "token": "t", "profile": "client-a"})
    )

    async def run():
        ok = await adapter.connect()
        assert ok is True
        await adapter.disconnect()

    asyncio.run(run())

    sent_frame = json.loads(fake_ws.sent[0])
    assert "capabilities" not in sent_frame
