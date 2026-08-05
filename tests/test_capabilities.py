# -*- coding: utf-8 -*-
"""Tests des capacites annoncees par l'agent (frame hello) — module pur
``capabilities.py``.

S'executent SANS hermes installe : le module est charge par chemin de
fichier, comme ``test_hello.py``. Les quatre cas de la spec : assemblage
nominal, non-fuite de secret MCP, source qui leve => None, skills
desactivees exclues + tri.
"""

import importlib.util
import logging
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "capabilities.py"
_spec = importlib.util.spec_from_file_location("pulse_chat_capabilities", _MODULE_PATH)
capabilities = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(capabilities)

build_capabilities = capabilities.build_capabilities
collect_capabilities = capabilities.collect_capabilities


# ── build_capabilities : assemblage nominal ──────────────────────────────


def test_build_capabilities_assemblage_nominal():
    result = build_capabilities(
        skills=[
            {"name": "crm-lookup", "description": "Recherche client"},
            {"name": "outlook-send", "description": "Envoi de mail"},
        ],
        disabled=set(),
        hub_names={"crm-lookup"},
        bundled_names={"outlook-send"},
        usage={"crm-lookup": 42, "outlook-send": 3},
        mcp_servers={"outlook": {"transport": "sse", "url": "https://x"}},
        config={"model": "hermes-4-70b", "identity": "Assistant CRM"},
        hermes_version="0.16.0",
        plugin_version="1.0.0",
    )

    assert result == {
        "model": "hermes-4-70b",
        "identity": "Assistant CRM",
        "hermesVersion": "0.16.0",
        "pluginVersion": "1.0.0",
        "skills": [
            {
                "name": "crm-lookup",
                "description": "Recherche client",
                "provenance": "hub",
                "usage": 42,
            },
            {
                "name": "outlook-send",
                "description": "Envoi de mail",
                "provenance": "bundled",
                "usage": 3,
            },
        ],
        "mcpServers": [{"name": "outlook", "transport": "sse"}],
    }


def test_build_capabilities_valeurs_absentes_sans_keyerror():
    result = build_capabilities(
        skills=[{"name": "solo"}],
        disabled=set(),
        hub_names=set(),
        bundled_names=set(),
        usage={},
        mcp_servers={},
        config={},
        hermes_version=None,
        plugin_version="1.0.0",
    )

    assert result["model"] is None
    assert result["identity"] is None
    assert result["hermesVersion"] is None
    assert result["skills"] == [
        {"name": "solo", "description": "", "provenance": "agent", "usage": 0}
    ]
    assert result["mcpServers"] == []


# ── Non-fuite de secret MCP (liste blanche) ──────────────────────────────


def test_build_capabilities_mcp_liste_blanche_stricte():
    result = build_capabilities(
        skills=[],
        disabled=set(),
        hub_names=set(),
        bundled_names=set(),
        usage={},
        mcp_servers={
            "outlook": {
                "transport": "sse",
                "url": "https://outlook.example/mcp",
                "command": "node",
                "args": ["server.js"],
                "env": {"OUTLOOK_API_KEY": "sk-super-secret"},
                "headers": {"Authorization": "Bearer sk-super-secret"},
            }
        },
        config={},
        hermes_version=None,
        plugin_version="1.0.0",
    )

    assert result["mcpServers"] == [{"name": "outlook", "transport": "sse"}]
    dumped = repr(result)
    assert "sk-super-secret" not in dumped
    assert "url" not in dumped
    assert "command" not in dumped
    assert "args" not in dumped
    assert "env" not in dumped
    assert "headers" not in dumped


def test_build_capabilities_mcp_conf_non_dict_ignoree_sans_lever():
    result = build_capabilities(
        skills=[],
        disabled=set(),
        hub_names=set(),
        bundled_names=set(),
        usage={},
        mcp_servers={"broken": "not-a-dict"},
        config={},
        hermes_version=None,
        plugin_version="1.0.0",
    )

    assert result["mcpServers"] == [{"name": "broken", "transport": None}]


# ── Skills desactivees exclues + tri (usage desc puis nom asc) ───────────


def test_build_capabilities_desactivees_exclues_et_tri():
    result = build_capabilities(
        skills=[
            {"name": "a", "description": "A"},
            {"name": "b", "description": "B"},
            {"name": "c", "description": "C"},
            {"name": "disabled-one", "description": "D"},
        ],
        disabled={"disabled-one"},
        hub_names=set(),
        bundled_names=set(),
        usage={"a": 5, "b": 5, "c": 1},
        mcp_servers={},
        config={},
        hermes_version=None,
        plugin_version="1.0.0",
    )

    names = [skill["name"] for skill in result["skills"]]
    # a et b sont ex-aequo (usage=5) => tri par nom ; c (usage=1) en dernier.
    assert names == ["a", "b", "c"]
    assert "disabled-one" not in names


def test_build_capabilities_usage_absent_traite_comme_zero_deterministe():
    result = build_capabilities(
        skills=[
            {"name": "z", "description": ""},
            {"name": "a", "description": ""},
        ],
        disabled=set(),
        hub_names=set(),
        bundled_names=set(),
        usage={},  # aucune entree d'usage => 0 pour tous
        mcp_servers={},
        config={},
        hermes_version=None,
        plugin_version="1.0.0",
    )

    # Usage egal (0) pour les deux => tri alphabetique stable.
    assert [s["name"] for s in result["skills"]] == ["a", "z"]
    assert all(s["usage"] == 0 for s in result["skills"])


def test_build_capabilities_description_tronquee_a_200():
    long_description = "x" * 500
    result = build_capabilities(
        skills=[{"name": "verbose", "description": long_description}],
        disabled=set(),
        hub_names=set(),
        bundled_names=set(),
        usage={},
        mcp_servers={},
        config={},
        hermes_version=None,
        plugin_version="1.0.0",
    )

    assert len(result["skills"][0]["description"]) == 200


# ── collect_capabilities : source qui leve => None, jamais d'exception ───


def test_collect_capabilities_import_absent_retourne_none(caplog):
    with caplog.at_level(logging.WARNING):
        result = collect_capabilities("default")

    assert result is None
    assert "Pulse Chat" in caplog.text


def test_collect_capabilities_ne_leve_jamais_sur_profil_quelconque():
    # Sans hermes installe, l'import de hermes_cli.web_deps echoue deja et le
    # try/except global l'attrape : le simple appel ne doit jamais lever,
    # quel que soit le profil demande.
    result = collect_capabilities("un-profil-quelconque")
    assert result is None
