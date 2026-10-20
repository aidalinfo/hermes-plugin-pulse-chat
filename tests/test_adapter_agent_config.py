# -*- coding: utf-8 -*-
"""Le bloc ``agentConfig`` n'est PLUS transmis a Hermes (``MessageEvent.metadata``).

Le serveur serialise `agentConfig` dans chaque frame ``message.created``. Le
plugin le recopiait dans ``MessageEvent.metadata`` en croyant qu'Hermes
l'appliquait (ton, consignes, ``disabledTools``). Aucune version d'Hermes ne lit
ce bloc (``v2026.9.24``, ``v0.22.2``, ``main`` d'octobre 2026), et Hermes
``main`` REFUSE desormais a l'admission native tout message dont ``metadata``
est non vide hors WhatsApp (``invalid_params (metadata)``) : tous les messages
de Pulse Chat etaient refuses. Doctrine verifiee ici : aucun evenement transmis
a Hermes ne porte de ``metadata``.

Chargement identique a test_adapter_dedup.py : stubs ``gateway.*`` dans
``sys.modules`` puis chargement d'``adapter.py`` par chemin.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

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

        async def handle_message(self, event):
            return None

        def _mark_connected(self):
            self.is_connected = True

        def _mark_disconnected(self):
            self.is_connected = False

        def _set_fatal_error(self, kind, message, retryable=False):
            self.last_error = (kind, message, retryable)

        async def _notify_fatal_error(self):
            return None

        async def play_tts(self, chat_id, audio_path, **kwargs):
            """Repli d'Hermes : l'auto-TTS delegue a ``send_voice``."""
            return await self.send_voice(
                chat_id=chat_id, audio_path=audio_path, **kwargs
            )

    class MessageEvent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class MessageType:
        TEXT = "text"
        # Voir le commentaire jumeau dans test_adapter_dedup.py : les stubs
        # gateway sont partages par toute la session pytest, les deux copies
        # doivent donc rester identiques.
        VOICE = "voice"

    class SendResult:
        # Tous les champs sont conserves : les stubs gateway sont partages par
        # TOUTE la session pytest (premier module charge = stub gagnant), donc
        # un double incomplet ici casse les tests des autres fichiers.
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


AGENT_CONFIG = {
    "tone": "pedagogique",
    "autonomy": "auto",
    "instructions": "Reponds en francais.",
    "disabledTools": ["search"],
}


def _make_adapter():
    adapter = adapter_module.PulseChatAdapter(_Config())
    seen = []

    async def fake_handle_message(event):
        seen.append(event)

    async def fake_send_ack(message_id):
        return None

    adapter.handle_message = fake_handle_message
    adapter._send_ack = fake_send_ack
    return adapter, seen


def _frame(message_id="msg-1", **extra):
    frame = {
        "type": "message.created",
        "channel": {"slug": "canal-test", "name": "Canal Test"},
        "message": {"id": message_id, "text": "bonjour", "userId": "u1", "userName": "Alice"},
    }
    frame.update(extra)
    return frame


def _dispatch(frame):
    adapter, seen = _make_adapter()
    asyncio.run(adapter._handle_message_created(frame))
    assert len(seen) == 1
    return seen[0]


# --- Chemin complet ----------------------------------------------------------

def _no_metadata(event):
    """Aucun ``metadata`` pose : le stub ne garde que les champs transmis."""
    return getattr(event, "metadata", None) in (None, {})


def test_message_event_never_carries_agent_config():
    event = _dispatch(_frame(agentConfig=AGENT_CONFIG))
    assert _no_metadata(event)
    # Le reste de la traduction est inchange.
    assert event.text == "bonjour"
    assert event.message_id == "msg-1"


def test_unknown_agent_config_fields_are_not_forwarded_either():
    event = _dispatch(_frame(agentConfig=dict(AGENT_CONFIG, futurReglage={"a": 1})))
    assert _no_metadata(event)


def test_frame_without_agent_config_sets_no_metadata():
    event = _dispatch(_frame())
    assert _no_metadata(event)


def test_metadata_transport_helpers_are_gone():
    """Le transport n'existe plus : rien ne doit pouvoir le reposer par megarde."""
    for name in ("build_message_event", "agent_config_metadata"):
        assert not hasattr(adapter_module, name), name
    adapter, _ = _make_adapter()
    assert not hasattr(adapter, "_last_agent_config")
