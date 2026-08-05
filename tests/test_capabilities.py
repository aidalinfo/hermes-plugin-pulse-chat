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


def test_build_capabilities_model_objet_avec_secret_ne_fuite_pas():
    # Cas courant Hermes : `model` stocke comme objet {name, provider, api_key,
    # base_url} plutot que comme simple chaine. Seul un nom lisible doit
    # franchir le WS, jamais l'objet (et surtout jamais la cle d'API).
    class ModelConfig:
        def __init__(self):
            self.name = "hermes-4-70b"
            self.provider = "openai-compatible"
            self.api_key = "sk-super-secret-model-key"
            self.base_url = "https://api.example/v1"

    result = build_capabilities(
        skills=[],
        disabled=set(),
        hub_names=set(),
        bundled_names=set(),
        usage={},
        mcp_servers={},
        config={"model": ModelConfig(), "identity": "Assistant"},
        hermes_version=None,
        plugin_version="1.0.0",
    )

    assert result["model"] == "hermes-4-70b"
    dumped = repr(result)
    assert "sk-super-secret-model-key" not in dumped
    assert "api_key" not in dumped
    assert "base_url" not in dumped


def test_build_capabilities_model_dict_avec_secret_ne_fuite_pas():
    result = build_capabilities(
        skills=[],
        disabled=set(),
        hub_names=set(),
        bundled_names=set(),
        usage={},
        mcp_servers={},
        config={
            "model": {
                "name": "hermes-4-70b",
                "provider": "openai-compatible",
                "api_key": "sk-super-secret-model-key",
            }
        },
        hermes_version=None,
        plugin_version="1.0.0",
    )

    assert result["model"] == "hermes-4-70b"
    dumped = repr(result)
    assert "sk-super-secret-model-key" not in dumped
    assert "api_key" not in dumped


def test_build_capabilities_model_sans_nom_lisible_retombe_a_none():
    result = build_capabilities(
        skills=[],
        disabled=set(),
        hub_names=set(),
        bundled_names=set(),
        usage={},
        mcp_servers={},
        config={"model": {"api_key": "sk-secret"}},
        hermes_version=None,
        plugin_version="1.0.0",
    )

    assert result["model"] is None
    assert "sk-secret" not in repr(result)


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


# ── Bornes de taille (hello = seule voie d'enregistrement du peer) ───────


def test_build_capabilities_skills_plafonnees_a_100_les_plus_utilisees():
    skills = [{"name": f"skill-{i}", "description": ""} for i in range(150)]
    usage = {f"skill-{i}": 150 - i for i in range(150)}  # skill-0 la plus utilisee

    result = build_capabilities(
        skills=skills,
        disabled=set(),
        hub_names=set(),
        bundled_names=set(),
        usage=usage,
        mcp_servers={},
        config={},
        hermes_version=None,
        plugin_version="1.0.0",
    )

    assert len(result["skills"]) == 100
    names = [s["name"] for s in result["skills"]]
    assert names[0] == "skill-0"
    assert "skill-149" not in names


def test_build_capabilities_mcp_servers_plafonnes_a_100():
    mcp_servers = {f"server-{i}": {"transport": "sse"} for i in range(150)}

    result = build_capabilities(
        skills=[],
        disabled=set(),
        hub_names=set(),
        bundled_names=set(),
        usage={},
        mcp_servers=mcp_servers,
        config={},
        hermes_version=None,
        plugin_version="1.0.0",
    )

    assert len(result["mcpServers"]) == 100


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


# ── Verrou de non-regression sur les symboles Hermes reels ───────────────
#
# Ces deux noms ont ete verifies dans NousResearch/hermes-agent (cf. revue
# fix round 1) : `load_usage` (pas `_read_usage_stats`, qui n'existe pas) et
# l'abandon de `_profile_scope` (fonction imbriquee, non importable) au
# profit de `gateway.run._profile_runtime_scope` + repli `nullcontext()`. Un
# grep source suffit ici : on ne peut pas executer contre un vrai Hermes
# dans cet environnement, donc ce test empeche seulement une regression
# silencieuse (retour a un nom fantome) sans pretendre verifier l'API reelle.


def test_collect_capabilities_utilise_load_usage_et_pas_un_nom_fantome():
    source = _MODULE_PATH.read_text(encoding="utf-8")
    assert "from tools.skill_usage import load_usage" in source
    assert "_read_usage_stats" not in source


def test_collect_capabilities_n_importe_jamais_profile_scope_directement():
    source = _MODULE_PATH.read_text(encoding="utf-8")
    # `_profile_scope` est une fonction imbriquee cote Hermes (non
    # importable) : seul le commentaire explicatif peut mentionner son nom,
    # aucun `import` ne doit le cibler.
    assert "import _profile_scope" not in source
    assert "web_deps" not in source
    assert "_profile_runtime_scope" in source
    assert "nullcontext" in source
