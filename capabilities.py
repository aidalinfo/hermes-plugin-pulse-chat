# -*- coding: utf-8 -*-
"""Capacites annoncees par l'agent (frame hello) — module PUR (teste pytest
sans hermes installe).

La fiche d'un agent dans /admin doit dire ce qu'il sait faire : modele, SOUL,
skills, serveurs MCP. Ces donnees vivent cote Hermes, dans des
symboles PRIVES qui peuvent changer de version en version — toute la recolte
est donc enveloppee dans un try/except large (``collect_capabilities``) : en
cas d'echec, le plugin retourne ``None`` et journalise un avertissement, mais
**poursuit sa connexion** ; la cle ``capabilities`` est alors ABSENTE de la
trame hello (jamais ``"capabilities": null`` sur le fil, voir ``hello.py``).
Le chat ne doit jamais tomber parce qu'une fiche d'administration est
incomplete.

Secret — invariant : seuls le NOM et le TRANSPORT d'un serveur MCP franchissent
le WS. Les champs ``env``, ``headers``, ``args``, ``url`` et ``command``
peuvent contenir des cles d'API : la selection se fait par LISTE BLANCHE
stricte (jamais par liste noire).
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

_DESCRIPTION_MAX_LEN = 200
_MAX_LIST_LEN = 100
#: Le SOUL est de la prose libre, parfois longue : la fiche n'en montre qu'une
#: amorce (meme borne que les descriptions de skills).
_SOUL_MAX_LEN = 200


def _truncate(value: Optional[str], max_len: int) -> str:
    """Chaine vide si absente ; tronquee sans depasser ``max_len``."""
    text = str(value) if value is not None else ""
    return text[:max_len]


#: Cles portant le nom du modele dans le bloc ``model:`` de ``config.yaml``,
#: par ordre de priorite. Hermes documente ``default`` ET ``model`` comme
#: interchangeables (« Both "default" and "model" work as the key name here »),
#: et le bloc porte aussi ``provider``, ``api_key``, ``base_url`` — d'ou la
#: liste blanche. ``name`` ferme la marche : c'est la forme historique, la
#: SEULE que ce module lisait, et le bloc reel ne l'emploie jamais — donc
#: ``model`` restait vide sur toutes les fiches.
_MODEL_NAME_KEYS = ("default", "model", "name")


def _readable_name(value: Any) -> Optional[str]:
    """Extrait un nom lisible d'une valeur de config potentiellement porteuse
    de secrets (ex: ``model`` sous forme d'objet ``{default, provider, api_key,
    base_url}``). N'accepte QUE des chaines en sortie : jamais l'objet brut,
    qui pourrait contenir une cle d'API — le filtrage cote serveur intervient
    trop tard, le secret aurait deja franchi le WS."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in _MODEL_NAME_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        return None
    for key in _MODEL_NAME_KEYS:
        candidate = getattr(value, key, None)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return None


def _mcp_transport(conf: Dict[str, Any]) -> Optional[str]:
    """Transport d'un serveur MCP, sous forme d'ETIQUETTE (``stdio`` / ``http``
    / ``sse``) — jamais l'URL ni la commande.

    Hermes ne stocke PAS de champ ``transport`` en general : il le DEDUIT de la
    forme de l'entree (``url`` ⇒ Streamable HTTP, ``command`` ⇒ stdio), la cle
    ``transport: sse`` n'existant que pour forcer le SSE sur une entree ``url``
    (``tools/mcp_tool.py``). Lire ``conf["transport"]`` seul rendait donc
    ``null`` pour la quasi-totalite des serveurs.

    Deduire, pas recopier : la liste blanche du module interdit de faire
    franchir le WS a ``url`` (jeton dans le chemin) comme a ``command``/``args``
    (chemins locaux) — on ne rend que le NOM du transport."""
    declared = conf.get("transport")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    if conf.get("url"):
        return "http"
    if conf.get("command"):
        return "stdio"
    return None


def _mcp_enabled(conf: Dict[str, Any]) -> bool:
    """``enabled: false`` (booleen ou chaine) ⇒ serveur exclu de la fiche.

    Meme regime que les skills desactivees : la fiche dit ce dont l'agent
    DISPOSE. Tolere la forme chaine, comme ``hermes mcp list``."""
    enabled = conf.get("enabled", True)
    if isinstance(enabled, str):
        return enabled.strip().lower() in {"true", "1", "yes"}
    return bool(enabled)


def _soul_excerpt(text: Optional[str]) -> Optional[str]:
    """Premiere ligne UTILE d'un ``SOUL.md``, tronquee — ou ``None``.

    Le SOUL est la premiere fente du prompt systeme d'Hermes (« agent identity
    », ``agent/prompt_builder.py``) : c'est LUI que la fiche doit montrer. Le
    champ ``identity`` qu'annonçait ce module n'existe dans AUCUNE version de
    ``config.yaml`` — il partait donc toujours vide.

    Titres Markdown et commentaires HTML sont ignores : les anciens
    installeurs semaient un gabarit fait de ces deux seules choses, dont la
    premiere ligne (« # Hermes Agent Persona ») ne dit rien de l'agent."""
    if not isinstance(text, str):
        return None
    in_comment = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        if line.startswith("<!--"):
            if "-->" not in line:
                in_comment = True
            continue
        if not line or line.startswith("#"):
            continue
        return line[:_SOUL_MAX_LEN]
    return None


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
    soul: Optional[str] = None,
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
    # Borne de taille : le hello est l'unique voie d'enregistrement du peer, on
    # garde les plus utilisees (deja triees usage desc puis nom asc ci-dessus).
    kept = kept[:_MAX_LIST_LEN]

    # ── Serveurs MCP : liste blanche stricte (name + transport) ──────────
    mcp_list: List[Dict[str, Any]] = []
    for name, raw_conf in (mcp_servers or {}).items():
        conf = raw_conf if isinstance(raw_conf, dict) else {}
        if not _mcp_enabled(conf):
            continue
        mcp_list.append({"name": str(name), "transport": _mcp_transport(conf)})
    mcp_list.sort(key=lambda item: item["name"])
    mcp_list = mcp_list[:_MAX_LIST_LEN]

    return {
        "model": _readable_name(config.get("model")),
        "soul": _soul_excerpt(soul),
        "hermesVersion": hermes_version,
        "pluginVersion": plugin_version,
        "skills": kept,
        "mcpServers": mcp_list,
    }


def _profile_scope(profile: str):
    """Contexte de lecture de la configuration pour ``profile``.

    Le nom que porte un bot (``PULSE_CHAT_PROFILE``) est un nom de profil PULSE
    CHAT (``Channel.hermesProfile``) : rien ne garantit qu'il nomme un profil
    HERMES. On ne scope donc que si le repertoire existe reellement, et on
    passe ce REPERTOIRE — pas le nom.

    C'est le defaut qui vidait toutes les fiches : `_profile_runtime_scope`
    attend un HERMES_HOME et appelle `set_hermes_home_override(str(path))` sans
    rien valider. Recevant ``"default"``, il faisait pointer HERMES_HOME sur un
    repertoire RELATIF inexistant — d'ou une configuration vide, zero skill,
    zero serveur MCP et aucun modele, **sans la moindre erreur** (`load_config`
    rend `{}` pour un fichier absent, `_find_all_skills` une liste vide). La
    fiche s'affichait donc « aucune competence · aucun MCP » sur un agent
    parfaitement configure.

    Ne leve jamais : un nom hors du gabarit d'Hermes
    (``[a-z0-9][a-z0-9_-]{0,63}``) fait lever `get_profile_dir`, et l'absence
    de scope est le comportement correct — le process d'un bot mono-profil a
    deja chargé le bon HERMES_HOME."""
    if not profile:
        return contextlib.nullcontext()
    try:
        from hermes_cli.profiles import get_profile_dir, normalize_profile_name

        # `default` designe le HERMES_HOME du process : deja actif, rien a
        # scoper (et le scoper installerait en plus un secret scope inutile).
        if normalize_profile_name(profile) == "default":
            return contextlib.nullcontext()
        profile_home = get_profile_dir(profile)
        if not profile_home.is_dir():
            return contextlib.nullcontext()
        from gateway.run import _profile_runtime_scope

        return _profile_runtime_scope(profile_home)
    except Exception as exc:
        logger.debug(
            "Pulse Chat: pas de scope de profil pour %r (%s) — lecture dans le "
            "HERMES_HOME du process",
            profile,
            exc,
        )
        return contextlib.nullcontext()


def _collect_soul() -> Optional[str]:
    """Contenu de ``SOUL.md`` du HERMES_HOME actif, ou ``None``.

    Lu ICI plutot que par `agent.prompt_builder.load_soul_md()` : cette
    derniere fait bien plus (scan de contenu, troncature avec marqueurs
    d'injection destines au LLM) pour un besoin d'affichage. Le chemin, lui,
    est celui-la meme qu'Hermes emploie (`get_hermes_home() / "SOUL.md"`),
    donc le scope de profil ci-dessus s'y applique aussi."""
    try:
        from hermes_constants import get_hermes_home

        soul_path = get_hermes_home() / "SOUL.md"
        if not soul_path.is_file():
            return None
        return soul_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.debug("Pulse Chat: SOUL.md illisible — %s", exc)
        return None


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
        # `_profile_scope` n'est PAS importable : c'est une fonction imbriquee
        # dans `gateway/platforms/api_server.py` (definie dans le corps d'une
        # autre fonction), et les usages `hermes_cli/web_routers/*.py` passent
        # par du late-binding interne au serveur web, hors de portee ici.
        #
        # A la place : `gateway.run._profile_runtime_scope`, qui attend un
        # REPERTOIRE de profil (HERMES_HOME), jamais un nom.
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

            # Usage brut par skill : `load_usage()` (tools.skill_usage), employe
            # exactement comme dans hermes_cli/web_routers/skills.py —
            # `activity_count(usage.get(nom_skill, {}))`. Isole dans son propre
            # try/except : un changement de forme ne doit degrader que les
            # compteurs (a 0), pas faire echouer toute la fiche de capacites.
            raw_usage: Dict[str, Any] = {}
            try:
                from tools.skill_usage import load_usage

                stats = load_usage() or {}
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
                soul=_collect_soul(),
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
    la version Hermes — on ne veut pas perdre ``model`` pour autant (le
    contrat pur attend un dict)."""
    if isinstance(config, dict):
        return config
    return {"model": getattr(config, "model", None)}


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
