# -*- coding: utf-8 -*-
"""Formes REELLES de la configuration Hermes — non-regression des fiches vides.

Les cas de ce fichier viennent d'une confrontation du contrat au code d'Hermes
(NousResearch/hermes-agent) : `capabilities.py` lisait des formes que Hermes
n'emploie pas, et la fiche d'un agent parfaitement configure s'affichait
« aucune competence · aucun MCP », sans modele et sans SOUL — **sans qu'aucune
erreur ne soit levee** ni cote plugin, ni cote app.

Separe de ``test_capabilities.py`` (qui couvre le contrat et les invariants de
secret) parce que ces tests disent autre chose : ce que Hermes ecrit VRAIMENT
dans ``config.yaml``. Meme chargement par chemin de fichier : ils s'executent
sans hermes installe.
"""

import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "capabilities.py"
_spec = importlib.util.spec_from_file_location("pulse_chat_capabilities", _MODULE_PATH)
capabilities = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(capabilities)

build_capabilities = capabilities.build_capabilities


def _build(**overrides):
    """`build_capabilities` avec des valeurs neutres, sauf ce qui est teste."""
    kwargs = {
        "skills": [],
        "disabled": set(),
        "hub_names": set(),
        "bundled_names": set(),
        "usage": {},
        "mcp_servers": {},
        "config": {},
        "hermes_version": None,
        "plugin_version": "1.0.0",
    }
    kwargs.update(overrides)
    return build_capabilities(**kwargs)


# ── Modele : le bloc reel porte `default`, jamais `name` ──────────────────


def test_model_bloc_reel_avec_cle_default():
    # `cli-config.yaml.example` : le bloc `model:` porte `default` (Hermes
    # documente `default` ET `model` comme interchangeables), JAMAIS `name` —
    # que ce module etait pourtant seul a lire.
    result = _build(
        config={
            "model": {
                "default": "anthropic/claude-opus-4.6",
                "provider": "anthropic",
                "api_key": "sk-super-secret-model-key",
            }
        }
    )

    assert result["model"] == "anthropic/claude-opus-4.6"
    assert "sk-super-secret-model-key" not in repr(result)


def test_model_cle_model_acceptee_comme_default():
    assert _build(config={"model": {"model": "openai/gpt-5"}})["model"] == "openai/gpt-5"


# ── Transport MCP : deduit de la forme de l'entree ────────────────────────


def test_transport_deduit_de_la_forme_de_l_entree():
    # Hermes ne stocke pas `transport` : il le deduit (`url` => Streamable
    # HTTP, `command` => stdio) et la cle n'existe que pour forcer le SSE.
    result = _build(
        mcp_servers={
            "notion": {"url": "https://mcp.notion.example/mcp?token=sk-secret"},
            "time": {"command": "uvx", "args": ["mcp-server-time"]},
            "legacy": {"transport": "sse", "url": "https://sse.example/mcp"},
            "vide": {},
        }
    )

    assert result["mcpServers"] == [
        {"name": "legacy", "transport": "sse"},
        {"name": "notion", "transport": "http"},
        {"name": "time", "transport": "stdio"},
        {"name": "vide", "transport": None},
    ]
    # L'etiquette est DEDUITE, jamais recopiee : ni URL ni commande sur le fil.
    dumped = repr(result)
    assert "sk-secret" not in dumped
    assert "notion.example" not in dumped
    assert "uvx" not in dumped


def test_serveur_mcp_desactive_exclu():
    # Meme regime que les skills desactivees : la fiche dit ce dont l'agent
    # DISPOSE.
    result = _build(
        mcp_servers={
            "actif": {"command": "uvx"},
            "eteint": {"command": "uvx", "enabled": False},
            "eteint-chaine": {"command": "uvx", "enabled": "false"},
        }
    )

    assert [s["name"] for s in result["mcpServers"]] == ["actif"]


# ── SOUL : la premiere fente du prompt systeme, pas un champ de config ────


def test_soul_amorce_sans_titre_ni_commentaire():
    # Les anciens installeurs d'Hermes semaient un SOUL.md fait d'un titre et
    # d'un commentaire HTML : sa premiere ligne ne dit rien de l'agent.
    result = _build(
        soul=(
            "# Hermes Agent Persona\n"
            "\n"
            "<!--\n"
            "Ce commentaire explique comment editer le fichier.\n"
            "-->\n"
            "\n"
            "Tu es l'assistant de l'atelier, direct et concis.\n"
            "Tu reponds en francais.\n"
        )
    )

    assert result["soul"] == "Tu es l'assistant de l'atelier, direct et concis."


def test_soul_gabarit_sans_persona_retombe_a_none():
    result = _build(soul="# Hermes Agent Persona\n\n<!-- rien d'ecrit ici -->\n")

    assert result["soul"] is None


def test_soul_absente_retombe_a_none():
    assert _build()["soul"] is None


def test_soul_tronquee_a_200():
    assert len(_build(soul="y" * 500)["soul"]) == 200


# ── Scope de profil : un NOM n'est pas un HERMES_HOME ─────────────────────


def test_profile_scope_nom_inconnu_ne_scope_pas_et_ne_leve_pas():
    # C'est le defaut qui vidait les fiches : `_profile_runtime_scope` attend
    # un repertoire et appelle `set_hermes_home_override(str(path))` sans rien
    # valider. Recevant "default", il faisait pointer HERMES_HOME sur un
    # repertoire RELATIF inexistant => configuration vide, zero skill, zero
    # serveur MCP, aucun modele.
    for profile in ("un-profil-pulse-chat", "default", "Nom Invalide", ""):
        with capabilities._profile_scope(profile):
            pass


def test_profile_scope_passe_un_repertoire_jamais_un_nom():
    # Verrou de source : on ne peut pas executer contre un vrai Hermes ici, ce
    # test empeche seulement le retour silencieux au nom de profil.
    source = _MODULE_PATH.read_text(encoding="utf-8")
    assert "from hermes_cli.profiles import get_profile_dir" in source
    assert "_profile_runtime_scope(profile_home)" in source
    assert "_profile_runtime_scope(profile)" not in source


def test_soul_lue_depuis_le_hermes_home_actif():
    # Le SOUL suit le HERMES_HOME (donc le scope de profil) : meme chemin
    # qu'Hermes (`get_hermes_home() / "SOUL.md"`, agent/prompt_builder.py).
    source = _MODULE_PATH.read_text(encoding="utf-8")
    assert 'get_hermes_home() / "SOUL.md"' in source
