# -*- coding: utf-8 -*-
"""Transport du bloc ``agentConfig`` (app -> plugin -> Hermes).

Le serveur serialise `agentConfig` dans chaque frame ``message.created``
(`server/lib/hermesGateway.ts`). L'adaptateur reconstruisait un ``MessageEvent``
a partir de cles nommees et ne lisait jamais `data["agentConfig"]` : ton,
autonomie, consignes et politique d'outils etaient sans effet de bout en bout.

Doctrine verifiee ici : le plugin TRANSPORTE et n'INTERPRETE pas — le bloc est
reporte tel quel, y compris ses champs inconnus.

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

    class MessageEvent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class MessageType:
        TEXT = "text"

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


# --- Fonction pure -----------------------------------------------------------

def test_agent_config_metadata_reports_block_verbatim():
    assert adapter_module.agent_config_metadata(_frame(agentConfig=AGENT_CONFIG)) == {
        "agentConfig": AGENT_CONFIG
    }


def test_agent_config_metadata_keeps_unknown_fields():
    """Transporte, n'interprete pas : un champ ajoute cote app doit passer."""
    block = dict(AGENT_CONFIG, futurReglage={"a": 1})
    assert adapter_module.agent_config_metadata({"agentConfig": block}) == {
        "agentConfig": block
    }


def test_agent_config_metadata_absent_or_invalid_gives_none():
    assert adapter_module.agent_config_metadata(_frame()) is None
    assert adapter_module.agent_config_metadata({"agentConfig": None}) is None
    assert adapter_module.agent_config_metadata({"agentConfig": ["direct"]}) is None


# --- Chemin complet ----------------------------------------------------------

def test_message_event_carries_agent_config():
    event = _dispatch(_frame(agentConfig=AGENT_CONFIG))
    assert event.metadata == {"agentConfig": AGENT_CONFIG}
    # Le reste de la traduction est inchange.
    assert event.text == "bonjour"
    assert event.message_id == "msg-1"


def test_frame_without_agent_config_sets_no_metadata():
    """Compat : une app anterieure au contrat ne pose aucune cle `metadata`."""
    event = _dispatch(_frame())
    assert not hasattr(event, "metadata")


def test_message_event_without_metadata_kwarg_still_carries_block(caplog):
    """MessageEvent d'une version d'Hermes sans champ `metadata` : repli attribut.

    Le repli doit LAISSER UNE TRACE : sans elle, la configuration voyagerait sur
    un attribut qu'Hermes ne lit pas, sans que personne ne le sache.
    """
    adapter_module._WARNED_ONCE.clear()

    class LegacyMessageEvent:
        def __init__(self, text, message_type, source, message_id, media_urls, media_types):
            self.text = text
            self.message_id = message_id

    event = adapter_module.build_message_event(
        LegacyMessageEvent,
        {
            "text": "bonjour",
            "message_type": "text",
            "source": {},
            "message_id": "msg-1",
            "media_urls": [],
            "media_types": [],
        },
        {"agentConfig": AGENT_CONFIG},
    )
    assert event.metadata == {"agentConfig": AGENT_CONFIG}
    assert any("metadata" in r.message for r in caplog.records if r.levelname == "WARNING")


def test_metadata_fallback_warns_only_once(caplog):
    """Une degradation structurelle se repete a chaque message : une trace suffit."""

    class LegacyMessageEvent:
        def __init__(self, text, message_type, source, message_id, media_urls, media_types):
            self.message_id = message_id

    adapter_module._WARNED_ONCE.clear()
    kwargs = {
        "text": "bonjour",
        "message_type": "text",
        "source": {},
        "message_id": "msg-1",
        "media_urls": [],
        "media_types": [],
    }
    for _ in range(3):
        adapter_module.build_message_event(
            LegacyMessageEvent, dict(kwargs), {"agentConfig": AGENT_CONFIG}
        )

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, "un seul avertissement attendu pour trois messages"


def test_message_is_never_lost_when_block_cannot_be_attached():
    """Un evenement qui refuse `metadata` part quand meme — le message prime."""

    class FrozenMessageEvent:
        __slots__ = ("text", "message_id")

        def __init__(self, text, message_type, source, message_id, media_urls, media_types):
            self.text = text
            self.message_id = message_id

    event = adapter_module.build_message_event(
        FrozenMessageEvent,
        {
            "text": "bonjour",
            "message_type": "text",
            "source": {},
            "message_id": "msg-1",
            "media_urls": [],
            "media_types": [],
        },
        {"agentConfig": AGENT_CONFIG},
    )
    assert event.message_id == "msg-1"
    assert not hasattr(event, "metadata")
