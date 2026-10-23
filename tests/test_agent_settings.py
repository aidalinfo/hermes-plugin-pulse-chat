# -*- coding: utf-8 -*-
"""Reglages de l'agent APPLIQUES par le plugin (#432 de Pulse Chat).

Aucun Hermes ne lit ``agentConfig`` : ton, consignes et outils retires dans
Pulse Chat n'avaient aucun effet. Le plugin les applique :

  - ``pre_tool_call`` REFUSE l'appel d'un outil retire (serveur MCP => tous ses
    outils ``mcp__<serveur>__...`` ; skill => ``skill_view`` sur lui ; nom exact) ;
  - ``pre_llm_call`` pose la note ecrite par l'app, une fois par session puis a
    chaque changement (et une note de retrait quand tout a ete enleve) ;
  - la trame ``agent.settings`` et ``agentConfig`` de chaque ``message.created``
    tiennent l'etat a jour ; une reconnexion repart d'un etat vide.
"""

import asyncio
import sys
import types

import pytest

from test_adapter_dedup import _load_adapter_module

adapter_module = _load_adapter_module()
settings_module = sys.modules["pulse_chat_plugin_under_test.agent_settings"]
AgentSettingsStore = settings_module.AgentSettingsStore
retired_match = settings_module.retired_match
parse_settings = settings_module.parse_settings
CLEARED_NOTE = settings_module.CLEARED_NOTE

NOTE = "[Réglages de l’agent : fixés par un administrateur dans Pulse Chat]\nConsignes :\nSois bref."
SESSION = {"HERMES_SESSION_PLATFORM": "pulse_chat", "HERMES_SESSION_CHAT_ID": "compta"}


@pytest.fixture(autouse=True)
def _session(monkeypatch):
    mod = types.ModuleType("gateway.session_context")
    mod.get_session_env = lambda name, default="": SESSION.get(name, default)
    monkeypatch.setitem(sys.modules, "gateway.session_context", mod)
    monkeypatch.setattr(adapter_module, "_AGENT_SETTINGS", AgentSettingsStore())
    yield


# --- Module pur ---------------------------------------------------------------

class TestOutilsRetires:
    def test_nom_exact(self):
        assert retired_match("terminal", {}, ["terminal"]) == "terminal"
        assert retired_match("terminal", {}, ["web_search"]) is None

    def test_serveur_mcp_retire_bloque_tous_ses_outils(self):
        assert retired_match("mcp__github__create_issue", {}, ["github"]) == "github"
        # Nom de serveur avec tiret : Hermes l'ecrit avec un souligne.
        assert retired_match("mcp__pulse_chat__channel_context", {}, ["pulse-chat"]) == "pulse-chat"
        # Un serveur au nom PREFIXE d'un autre n'est pas touche.
        assert retired_match("mcp__github_enterprise__list", {}, ["github"]) is None

    def test_skill_retire_bloque_sa_lecture(self):
        assert retired_match("skill_view", {"name": "axolotl"}, ["axolotl"]) == "axolotl"
        assert retired_match("skill_view", {"name": "03-fine-tuning/axolotl"}, ["axolotl"]) == "axolotl"
        assert retired_match("skill_view", {"name": "pulse-chat:interface"}, ["pulse-chat:interface"]) == "pulse-chat:interface"
        assert retired_match("skill_view", {"name": "autre"}, ["axolotl"]) is None
        # Un autre outil qui porterait un argument ``name`` n'est pas une lecture de skill.
        assert retired_match("web_search", {"name": "axolotl"}, ["axolotl"]) is None

    def test_rien_de_retire(self):
        assert retired_match("mcp__github__x", {}, []) is None
        assert retired_match("", {}, ["github"]) is None


class TestLectureDesReglages:
    def test_trame_complete(self):
        assert parse_settings({"disabledTools": ["a", "", 3], "contextNote": NOTE}) == (frozenset({"a"}), NOTE)

    def test_note_vide_vaut_aucune_note(self):
        assert parse_settings({"disabledTools": [], "contextNote": "  "}) == (frozenset(), None)

    def test_bloc_sans_reglages_ne_dit_rien(self):
        # App anterieure : on garde l'etat connu, on ne l'efface pas par erreur.
        assert parse_settings({"tone": "direct"}) is None
        assert parse_settings(None) is None


class TestNoteParSession:
    def test_posee_une_fois_puis_au_changement_seulement(self):
        store = AgentSettingsStore()
        store.update("daniel", {"disabledTools": [], "contextNote": NOTE}, channel="compta")
        assert store.note_for_turn("s1", "compta") == NOTE
        assert store.note_for_turn("s1", "compta") is None
        assert store.note_for_turn("s2", "compta") == NOTE
        store.update("daniel", {"disabledTools": [], "contextNote": NOTE + " Et cite tes sources."})
        assert store.note_for_turn("s1", "compta") == NOTE + " Et cite tes sources."

    def test_tout_retire_apres_une_note_annonce_le_retrait_une_fois(self):
        store = AgentSettingsStore()
        store.update("daniel", {"disabledTools": [], "contextNote": NOTE}, channel="compta")
        assert store.note_for_turn("s1", "compta") == NOTE
        store.update("daniel", {"disabledTools": [], "contextNote": None})
        assert store.note_for_turn("s1", "compta") == CLEARED_NOTE
        assert store.note_for_turn("s1", "compta") is None
        # Une session qui n'a jamais rien recu ne recoit rien.
        assert store.note_for_turn("s9", "compta") is None

    def test_sans_session_rien_n_est_pose(self):
        store = AgentSettingsStore()
        store.update("daniel", {"disabledTools": [], "contextNote": NOTE})
        assert store.note_for_turn("", "compta") is None

    def test_conversation_inconnue_tout_le_retire_s_applique(self):
        store = AgentSettingsStore()
        store.update("daniel", {"disabledTools": ["github"], "contextNote": None}, channel="compta")
        store.update("vulcain", {"disabledTools": ["notion"], "contextNote": NOTE}, channel="chantier")
        assert store.blocked("mcp__notion__search", {}, "compta") is None
        assert store.blocked("mcp__notion__search", {}, "") == "notion"
        assert store.blocked("mcp__github__x", {}, "tache-planifiee") == "github"
        # Deux profils : pas de note devinee hors de leurs conversations.
        assert store.note_for_turn("s1", "ailleurs") is None


# --- Branchement dans l'adaptateur ---------------------------------------------

class _Config:
    extra = {"url": "http://pulse-chat.test", "token": "token-test"}


def _adapter():
    adapter = adapter_module.PulseChatAdapter(_Config())

    async def fake_handle_message(event):
        return None

    async def fake_send_ack(message_id):
        return None

    adapter.handle_message = fake_handle_message
    adapter._send_ack = fake_send_ack
    return adapter


def _message_frame(agent_config, message_id="m1"):
    return {
        "type": "message.created",
        "channel": {"slug": "compta", "name": "Compta", "hermesProfile": "daniel"},
        "message": {"id": message_id, "text": "bonjour", "userId": "u1", "userName": "Alice"},
        "agentConfig": agent_config,
    }


class TestAdaptateur:
    def test_un_message_porte_les_reglages_appliques_au_tour(self):
        asyncio.run(_adapter()._handle_message_created(_message_frame({"disabledTools": ["github"], "contextNote": NOTE})))
        refus = adapter_module._on_pre_tool_call(tool_name="mcp__github__create_issue", args={})
        assert refus["action"] == "block"
        assert "github" in refus["message"]
        assert adapter_module._on_pre_tool_call(tool_name="mcp__notion__search", args={}) is None
        assert adapter_module._on_pre_llm_call(session_id="s1") == {"context": NOTE}
        assert adapter_module._on_pre_llm_call(session_id="s1") is None

    def test_trame_agent_settings_applique_sans_attendre_un_message(self):
        _adapter()._handle_agent_settings({"type": "agent.settings", "profile": "daniel", "disabledTools": ["axolotl"], "contextNote": None})
        assert adapter_module._on_pre_tool_call(tool_name="skill_view", args={"name": "axolotl"})["action"] == "block"
        # Modification : l'outil est rendu, le refus tombe aussitot.
        _adapter()._handle_agent_settings({"type": "agent.settings", "profile": "daniel", "disabledTools": [], "contextNote": None})
        assert adapter_module._on_pre_tool_call(tool_name="skill_view", args={"name": "axolotl"}) is None

    def test_sous_agent_sans_note(self):
        _adapter()._handle_agent_settings({"type": "agent.settings", "profile": "daniel", "disabledTools": [], "contextNote": NOTE})
        assert adapter_module._on_pre_llm_call(session_id="sub", parent_session_id="s1") is None

    def test_rien_de_regle_rien_ne_change(self):
        asyncio.run(_adapter()._handle_message_created(_message_frame({"tone": "direct", "disabledTools": [], "contextNote": None})))
        assert adapter_module._on_pre_tool_call(tool_name="terminal", args={}) is None
        assert adapter_module._on_pre_llm_call(session_id="s1") is None

    def test_les_crochets_sont_enregistres(self):
        hooks = []

        class Ctx:
            def register_hook(self, name, cb):
                hooks.append((name, cb))

        adapter_module._register_settings_hooks(Ctx())
        assert hooks == [
            ("pre_tool_call", adapter_module._on_pre_tool_call),
            ("pre_llm_call", adapter_module._on_pre_llm_call),
        ]

    def test_un_hermes_sans_register_hook_ne_casse_rien(self):
        adapter_module._register_settings_hooks(object())
