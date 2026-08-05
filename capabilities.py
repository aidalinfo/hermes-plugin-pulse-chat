# -*- coding: utf-8 -*-
"""Capacites annoncees par l'agent (frame hello) — module PUR (teste pytest
sans hermes installe).

La fiche d'un agent dans /admin doit dire ce qu'il sait faire : modele,
identite, skills, serveurs MCP. Ces donnees vivent cote Hermes, dans des
symboles PRIVES qui peuvent changer de version en version — toute la recolte
est donc enveloppee dans un try/except large (``collect_capabilities``) : en
cas d'echec, le plugin envoie ``capabilities: null`` et journalise un
avertissement, mais **poursuit sa connexion**. Le chat ne doit jamais tomber
parce qu'une fiche d'administration est incomplete.

Secret — invariant : seuls le NOM et le TRANSPORT d'un serveur MCP franchissent
le WS. Les champs ``env``, ``headers``, ``args``, ``url`` et ``command``
peuvent contenir des cles d'API : la selection se fait par LISTE BLANCHE
stricte (jamais par liste noire).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

_DESCRIPTION_MAX_LEN = 200


def _truncate(value: Optional[str], max_len: int) -> str:
    """Chaine vide si absente ; tronquee sans depasser ``max_len``."""
    text = str(value) if value is not None else ""
    return text[:max_len]


def _provenance(name: str, hub_names: Set[str], bundled_names: Set[str]) -> str:
    """``hub`` > ``bundled`` > ``agent`` (ordre de priorite de la spec)."""
    if name in hub_names:
        return "hub"
    if name in bundled_names:
        return "bundled"
    return "agent"


def build_capabilities(
    skills: List[Dict[str, Any]],
    disabled: Set[str],
    hub_names: Set[str],
    bundled_names: Set[str],
    usage: Dict[str, Any],
    mcp_servers: Dict[str, Any],
    config: Dict[str, Any],
    hermes_version: Optional[str],
    plugin_version: str,
) -> Dict[str, Any]:
    """Assemble le dictionnaire du contrat a partir de valeurs deja recoltees.

    Fonction pure : aucune dependance hermes, aucune levee d'exception liee a
    des valeurs absentes (toute cle manquante ⇒ ``None``/``[]``, jamais de
    ``KeyError``).
    """
    disabled = disabled or set()
    hub_names = hub_names or set()
    bundled_names = bundled_names or set()
    usage = usage or {}
    mcp_servers = mcp_servers or {}
    config = config or {}

    # ── Skills : desactivees exclues, tri usage desc puis nom asc ────────
    kept: List[Dict[str, Any]] = []
    for raw_skill in skills or []:
        name = str((raw_skill or {}).get("name") or "")
        if not name or name in disabled:
            continue
        raw_usage = usage.get(name, 0)
        try:
            usage_count = int(raw_usage)
        except (TypeError, ValueError):
            usage_count = 0
        kept.append(
            {
                "name": name,
                "description": _truncate(
                    (raw_skill or {}).get("description"), _DESCRIPTION_MAX_LEN
                ),
                "provenance": _provenance(name, hub_names, bundled_names),
                "usage": usage_count,
            }
        )
    kept.sort(key=lambda item: (-item["usage"], item["name"]))

    # ── Serveurs MCP : liste blanche stricte (name + transport) ──────────
    mcp_list: List[Dict[str, Any]] = []
    for name, raw_conf in (mcp_servers or {}).items():
        conf = raw_conf if isinstance(raw_conf, dict) else {}
        transport = conf.get("transport")
        mcp_list.append(
            {
                "name": str(name),
                "transport": str(transport) if transport is not None else None,
            }
        )
    mcp_list.sort(key=lambda item: item["name"])

    return {
        "model": config.get("model"),
        "identity": config.get("identity"),
        "hermesVersion": hermes_version,
        "pluginVersion": plugin_version,
        "skills": kept,
        "mcpServers": mcp_list,
    }


def collect_capabilities(profile: str) -> Optional[Dict[str, Any]]:
    """Recolte reelle cote Hermes (imports internes) puis delegue au module pur.

    Invariant : ne leve JAMAIS — la connexion WS du plugin en depend. Toute
    erreur (symbole absent, changement de version Hermes, profil inconnu…)
    donne un ``warning`` journalise et ``None`` en retour (``capabilities:
    null`` cote frame hello).
    """
    try:
        from importlib.metadata import version as _pkg_version
    except ImportError:  # pragma: no cover - py < 3.8, hors cible (3.9+)
        _pkg_version = None  # type: ignore[assignment]

    try:
        try:
            from hermes_cli.web_deps import _profile_scope
        except ImportError:
            from hermes_cli import _profile_scope  # type: ignore[attr-defined]

        with _profile_scope(profile):
            from tools.skills_tool import _find_all_skills
            from hermes_cli.skills_config import get_disabled_skills
            from tools.skill_usage import (
                _read_hub_installed_names,
                _read_bundled_manifest_names,
                activity_count,
            )
            from hermes_cli.mcp_config import _get_mcp_servers

            try:
                from hermes_cli.config import load_config
            except ImportError:
                from hermes_cli.skills_config import load_config  # type: ignore[no-redef]

            config = load_config() or {}
            skills = _find_all_skills() or []
            disabled = set(get_disabled_skills(config) or [])
            hub_names = set(_read_hub_installed_names() or [])
            bundled_names = set(_read_bundled_manifest_names() or [])
            mcp_servers = _get_mcp_servers() or {}

            # L'usage brut par skill est un detail d'implementation Hermes qui
            # n'est pas garanti stable — echec isole (pas de fonction/attribut
            # de recolte disponible) => usage 0 pour chaque skill plutot que
            # d'abandonner toute la fiche de capacites.
            raw_usage: Dict[str, Any] = {}
            try:
                from tools.skill_usage import _read_usage_stats

                stats = _read_usage_stats() or {}
                for skill in skills:
                    name = str((skill or {}).get("name") or "")
                    if name:
                        raw_usage[name] = activity_count(stats.get(name, {}))
            except Exception:
                logger.warning(
                    "Pulse Chat: usage des skills indisponible (profil %s), "
                    "compteurs a 0",
                    profile,
                )

            hermes_version = None
            if _pkg_version is not None:
                try:
                    hermes_version = _pkg_version("hermes-agent")
                except Exception:  # pragma: no cover - nom de paquet variable
                    hermes_version = None

            plugin_version = _read_plugin_version()

            return build_capabilities(
                skills=skills,
                disabled=disabled,
                hub_names=hub_names,
                bundled_names=bundled_names,
                usage=raw_usage,
                mcp_servers=mcp_servers,
                config=_normalize_config(config),
                hermes_version=hermes_version,
                plugin_version=plugin_version,
            )
    except Exception as exc:  # pragma: no cover - filet, jamais cense manquer
        logger.warning(
            "Pulse Chat: recolte des capacites impossible (profil %s) — %s",
            profile,
            exc,
        )
        return None


def _normalize_config(config: Any) -> Dict[str, Any]:
    """``load_config()`` reel : dict la plupart du temps, objet parfois selon
    la version Hermes — on ne veut perdre ni ``model`` ni ``identity`` pour
    autant (contrat pur attend un dict)."""
    if isinstance(config, dict):
        return config
    return {
        "model": getattr(config, "model", None),
        "identity": getattr(config, "identity", None),
    }


def _read_plugin_version() -> str:
    """Version du plugin depuis ``plugin.yaml`` (defaut ``"0.0.0"`` si illisible)."""
    from pathlib import Path

    plugin_yaml = Path(__file__).resolve().parent / "plugin.yaml"
    try:
        text = plugin_yaml.read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("version:"):
            return stripped.split(":", 1)[1].strip().strip("'\"") or "0.0.0"
    return "0.0.0"
