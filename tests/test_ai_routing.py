# -*- coding: utf-8 -*-
"""Acces IA affecte dans Pulse Chat, APPLIQUE par le plugin (trame ``agent.ai``).

Le plugin ecrit la couche geree d'Hermes (``$HERMES_MANAGED_DIR/config.yaml``,
fusionnee par-dessus le ``config.yaml`` du bot) : fournisseur nomme
``pulse-ai`` vers la passerelle de Pulse, ``model`` et ``fallback_providers``.
Il ne touche qu'a ses quatre cles, restaure ce qu'il a remplace au retrait
(``routing: null``), et n'ecrit rien sur une trame qu'il ne comprend pas.
"""

import os
import sys

import pytest
import yaml

from test_adapter_dedup import _load_adapter_module

adapter_module = _load_adapter_module()
ai = sys.modules["pulse_chat_plugin_under_test.ai_routing"]

BASE = "https://agents.pulsemyit.fr/"
PRIMARY = {"model": "gpt-6.1-terra", "apiMode": "codex_responses", "path": "/api/agent/ai-gateway/zephyr/v1"}
FALLBACK = {
    "model": "claude-sonnet-5-5",
    "apiMode": "anthropic_messages",
    "path": "/api/agent/ai-gateway/zephyr/fallback/v1",
}


def _frame(primary=PRIMARY, fallback=FALLBACK, profile="zephyr"):
    return {"type": "agent.ai", "profile": profile, "routing": {"primary": primary, "fallback": fallback}}


# --- Trame ---------------------------------------------------------------------

class TestParseFrame:
    def test_valid_with_fallback(self):
        routing = ai.parse_frame(_frame())
        assert routing.primary == ai.Route("gpt-6.1-terra", "codex_responses", "/api/agent/ai-gateway/zephyr/v1")
        assert routing.fallback.api_mode == "anthropic_messages"

    def test_fallback_null(self):
        assert ai.parse_frame(_frame(fallback=None)).fallback is None

    def test_routing_null_means_remove(self):
        assert ai.parse_frame({"type": "agent.ai", "profile": "zephyr", "routing": None}) is None

    @pytest.mark.parametrize(
        "frame",
        [
            _frame(primary={**PRIMARY, "apiMode": "bedrock_converse"}),
            _frame(fallback={**FALLBACK, "apiMode": "responses"}),
            _frame(primary={**PRIMARY, "path": "api/agent/ai-gateway/zephyr/v1"}),
            _frame(primary={**PRIMARY, "path": "/api/agent/ai-gateway/zephyr"}),
            _frame(primary={**PRIMARY, "path": "//evil.test/v1"}),
            _frame(primary={**PRIMARY, "model": ""}),
            _frame(primary=None),
            {"type": "agent.ai", "profile": "zephyr"},
            {"type": "agent.ai", "profile": "zephyr", "routing": "pulse"},
        ],
    )
    def test_invalid_is_rejected(self, frame):
        with pytest.raises(ai.RoutingError):
            ai.parse_frame(frame)


# --- Rendu et fusion (pur) -----------------------------------------------------

def _apply(doc, state, frame, base=BASE):
    return ai.apply_layer(doc, state, ai.render_layer(ai.parse_frame(frame), base))


class TestLayer:
    def test_render_with_fallback(self):
        doc, _ = _apply({}, {}, _frame())
        assert doc == {
            "providers": {
                "pulse-ai": {
                    "base_url": "https://agents.pulsemyit.fr/api/agent/ai-gateway/zephyr/v1",
                    "api_mode": "codex_responses",
                    "key_env": "PULSE_CHAT_AGENT_TOKEN",
                },
                "pulse-ai-fallback": {
                    "base_url": "https://agents.pulsemyit.fr/api/agent/ai-gateway/zephyr/fallback/v1",
                    "api_mode": "anthropic_messages",
                    "key_env": "PULSE_CHAT_AGENT_TOKEN",
                },
            },
            "model": {
                "provider": "pulse-ai",
                "default": "gpt-6.1-terra",
                "base_url": "https://agents.pulsemyit.fr/api/agent/ai-gateway/zephyr/v1",
                "api_mode": "codex_responses",
                # Neutralise un ``model.api_key`` de l'utilisateur (fusion feuille a feuille).
                "api_key": "",
            },
            "fallback_providers": [{"provider": "pulse-ai-fallback", "model": "claude-sonnet-5-5"}],
        }

    def test_no_fallback_writes_empty_list(self):
        doc, _ = _apply({}, {}, _frame(fallback=None))
        assert doc["fallback_providers"] == []
        assert "pulse-ai-fallback" not in doc["providers"]

    def test_never_writes_a_secret(self):
        doc, state = _apply({}, {}, _frame())
        text = yaml.safe_dump(doc) + repr(state)
        assert "pca_" not in text and "${" not in text

    def test_third_party_keys_preserved(self):
        operator = {
            "terminal": {"backend": "docker"},
            "providers": {"interne": {"base_url": "http://llm.local/v1"}},
            "model": {"default": "operateur-model"},
            "fallback_providers": [{"provider": "openrouter", "model": "x"}],
        }
        doc, state = _apply(operator, {}, _frame())
        assert doc["terminal"] == {"backend": "docker"}
        assert doc["providers"]["interne"] == {"base_url": "http://llm.local/v1"}
        assert doc["model"]["provider"] == "pulse-ai"

        # Retrait : ce que l'exploitant avait pose revient, a l'identique.
        restored, state = ai.remove_layer(doc, state)
        assert restored == operator
        assert state == {}

    def test_remove_from_empty_leaves_nothing(self):
        doc, state = _apply({}, {}, _frame())
        restored, state = ai.remove_layer(doc, state)
        assert restored == {} and state == {}

    def test_reapply_keeps_original_previous(self):
        operator = {"model": {"default": "operateur-model"}}
        doc, state = _apply(operator, {}, _frame())
        doc, state = _apply(doc, state, _frame(primary={**PRIMARY, "model": "autre"}, fallback=None))
        assert doc["model"]["default"] == "autre"
        assert "pulse-ai-fallback" not in doc.get("providers", {})
        restored, _ = ai.remove_layer(doc, state)
        assert restored == operator

    def test_key_changed_by_third_party_is_left_alone(self):
        doc, state = _apply({}, {}, _frame())
        doc["model"] = {"default": "change-par-l-exploitant"}
        restored, _ = ai.remove_layer(doc, state)
        assert restored == {"model": {"default": "change-par-l-exploitant"}}

    def test_idempotent(self):
        doc, state = _apply({}, {}, _frame())
        again, state2 = _apply(doc, state, _frame())
        assert again == doc and state2 == state


# --- E/S -----------------------------------------------------------------------

class TestWriter:
    def test_apply_then_remove(self, tmp_path):
        writer = ai.ManagedConfigWriter(tmp_path)
        routing = ai.parse_frame(_frame())
        assert writer.apply(routing, BASE) is True
        on_disk = yaml.safe_load(writer.config_path.read_text())
        assert on_disk["model"]["provider"] == "pulse-ai"
        assert on_disk["model"]["api_key"] == ""
        assert writer.state_path.exists()

        mtime = writer.config_path.stat().st_mtime_ns
        assert writer.apply(routing, BASE) is False  # contenu identique : pas de reecriture
        assert writer.config_path.stat().st_mtime_ns == mtime

        assert writer.remove() is True
        assert not writer.config_path.exists()
        assert not writer.state_path.exists()
        assert writer.remove() is False

    def test_operator_file_survives(self, tmp_path):
        (tmp_path / "config.yaml").write_text("terminal:\n  backend: docker\nmodel: {default: m}\n")
        writer = ai.ManagedConfigWriter(tmp_path)
        writer.apply(ai.parse_frame(_frame(fallback=None)), BASE)
        writer.remove()
        assert yaml.safe_load((tmp_path / "config.yaml").read_text()) == {
            "terminal": {"backend": "docker"},
            "model": {"default": "m"},
        }

    def test_no_temp_file_left(self, tmp_path):
        ai.ManagedConfigWriter(tmp_path).apply(ai.parse_frame(_frame()), BASE)
        assert sorted(p.name for p in tmp_path.iterdir()) == [".pulse-chat-ai.json", "config.yaml"]


class TestManagedDir:
    def test_explicit_env_wins(self, tmp_path):
        target = tmp_path / "geree"
        env = {"HERMES_MANAGED_DIR": str(target)}
        path, _ = ai.resolve_managed_dir(tmp_path / "home", create=True, environ=env, system_dir=tmp_path / "etc")
        assert path == target and target.is_dir()

    def test_system_dir_is_never_written(self, tmp_path):
        (tmp_path / "etc").mkdir()
        env = {}
        path, reason = ai.resolve_managed_dir(tmp_path / "home", create=True, environ=env, system_dir=tmp_path / "etc")
        assert path is None and "exploitant" in reason
        assert "HERMES_MANAGED_DIR" not in env

    def test_plugin_dir_under_hermes_home(self, tmp_path):
        env = {}
        home = tmp_path / "home"
        path, _ = ai.resolve_managed_dir(home, create=False, environ=env, system_dir=tmp_path / "etc")
        assert path is None and "HERMES_MANAGED_DIR" not in env  # rien pose tant qu'aucune trame
        path, _ = ai.resolve_managed_dir(home, create=True, environ=env, system_dir=tmp_path / "etc")
        assert path == home / "pulse-managed" and path.is_dir()
        assert env["HERMES_MANAGED_DIR"] == str(path)


# --- Branchement dans l'adaptateur ---------------------------------------------

class _Config:
    def __init__(self, **extra):
        self.extra = {
            "url": "https://agents.pulsemyit.fr",
            "token": "token-test",
            "agent_token": "pca_test",
            "profile": "zephyr",
            **extra,
        }


@pytest.fixture
def managed(tmp_path, monkeypatch):
    target = tmp_path / "geree"
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(target))
    return target


class TestAdapter:
    def test_applies_and_removes(self, managed):
        adapter = adapter_module.PulseChatAdapter(_Config())
        adapter._handle_agent_ai(_frame())
        doc = yaml.safe_load((managed / "config.yaml").read_text())
        assert doc["providers"]["pulse-ai"]["base_url"] == (
            "https://agents.pulsemyit.fr/api/agent/ai-gateway/zephyr/v1"
        )
        adapter._handle_agent_ai({"type": "agent.ai", "profile": "zephyr", "routing": None})
        assert not (managed / "config.yaml").exists()

    def test_unknown_api_mode_writes_nothing(self, managed):
        adapter = adapter_module.PulseChatAdapter(_Config())
        adapter._handle_agent_ai(_frame(primary={**PRIMARY, "apiMode": "inconnu"}))
        assert not (managed / "config.yaml").exists()

    def test_several_profiles_write_nothing(self, managed):
        adapter = adapter_module.PulseChatAdapter(_Config(profile="zephyr,vulcain"))
        adapter._handle_agent_ai(_frame())
        assert not (managed / "config.yaml").exists()

    def test_other_profile_writes_nothing(self, managed):
        adapter = adapter_module.PulseChatAdapter(_Config())
        adapter._handle_agent_ai(_frame(profile="vulcain"))
        assert not (managed / "config.yaml").exists()

    def test_without_agent_secret_writes_nothing(self, managed, monkeypatch):
        monkeypatch.delenv("PULSE_CHAT_AGENT_TOKEN", raising=False)
        adapter = adapter_module.PulseChatAdapter(_Config(agent_token=""))
        adapter._handle_agent_ai(_frame())
        assert not (managed / "config.yaml").exists()

    def test_unwritable_dir_is_logged_not_raised(self, managed, caplog):
        managed.mkdir()
        os.chmod(managed, 0o500)
        try:
            if os.access(managed, os.W_OK):
                pytest.skip("execute en root : le dossier reste inscriptible")
            adapter = adapter_module.PulseChatAdapter(_Config())
            adapter._handle_agent_ai(_frame())
            assert "ecriture impossible" in caplog.text
        finally:
            os.chmod(managed, 0o700)
