# -*- coding: utf-8 -*-
"""Reglages d'un agent APPLIQUES par le plugin (#432 de Pulse Chat).

Ton, consignes et outils retires se reglent dans Pulse Chat, mais AUCUNE
version d'Hermes ne lit le bloc ``agentConfig`` (v2026.9.24, v0.22.x, ``main``
d'octobre 2026) : jusqu'a la 1.23.0, ces reglages n'atteignaient jamais le
modele, et un admin qui retirait un outil croyait l'avoir retire.

Le plugin les applique par deux crochets d'Hermes, branches dans
``adapter.py`` :

- ``pre_tool_call`` : l'appel d'un outil retire est REFUSE
  (``{"action": "block", "message": ...}``, le message devient le resultat de
  l'outil) ;
- ``pre_llm_call`` : la NOTE de reglages est ajoutee au contexte du tour
  (``{"context": ...}``), une fois par session, puis a chaque changement.

Le plugin reste MINCE : la note est ECRITE par l'app (``contextNote``), il ne
fait que la poser. Seuls les deux textes ci-dessous (refus, note retiree) sont
d'ici, faute d'app pour les dire au moment ou ils servent.

Module PUR (aucune dependance Hermes), sur ce que l'app envoie : la trame
``agent.settings`` (connexion, modification) et ``agentConfig`` de chaque
``message.created``.
"""

import re
import threading
from collections import OrderedDict
from typing import Any, Dict, FrozenSet, Iterable, Optional, Tuple

#: Outil qui CHARGE un skill (son SKILL.md) : un skill retire n'y est plus lu.
SKILL_READ_TOOLS = frozenset({"skill_view"})

#: Prefixe des outils MCP d'Hermes : ``mcp__<serveur>__<outil>``
#: (``tools/mcp_tool_schema.py``, depuis v2026.9 ; v0.22 compris).
MCP_TOOL_PREFIX = "mcp__"

#: Sessions dont on retient la derniere note posee — au-dela, la plus ancienne
#: est oubliee (et recevra de nouveau la note a son prochain tour).
MAX_TRACKED_SESSIONS = 2000

#: Note posee quand l'admin a tout retire alors qu'une note avait ete donnee
#: dans cette session : sans elle, l'ancienne resterait la consigne en vigueur.
CLEARED_NOTE = (
    "[Réglages de l’agent : retirés par un administrateur dans Pulse Chat — "
    "les réglages annoncés plus tôt dans cette conversation ne s’appliquent plus]"
)


def refusal_message(retired: str) -> str:
    """Resultat d'outil rendu au modele a la place de l'appel refuse."""
    return (
        f"Refusé : « {retired} » a été retiré à cet agent par un administrateur "
        "dans Pulse Chat (réglages de l'agent). Ne le rappelle pas ; si la tâche "
        "en dépend, dis-le à ton interlocuteur."
    )


def sanitize_mcp_component(value: str) -> str:
    """Nom de serveur tel qu'Hermes l'ecrit dans le nom de l'outil."""
    return re.sub(r"[^A-Za-z0-9_]", "_", str(value or ""))


def retired_match(tool_name: str, args: Any, retired: Iterable[str]) -> Optional[str]:
    """Le reglage retire que vise cet appel, ou ``None``.

    Les cles de ``toolPolicy`` sont les capacites annoncees au ``hello`` : des
    noms de SKILLS et de SERVEURS MCP. Un serveur retire bloque tous ses outils,
    un skill retire bloque sa lecture ; un nom d'outil exact est bloque tel quel.
    """
    name = str(tool_name or "")
    if not name:
        return None
    skill = ""
    if name in SKILL_READ_TOOLS and isinstance(args, dict):
        skill = str(args.get("name") or "").strip()
    for entry in retired:
        if not entry:
            continue
        if name == entry:
            return entry
        server = sanitize_mcp_component(entry)
        if server and name.startswith(f"{MCP_TOOL_PREFIX}{server}__"):
            return entry
        if skill and (skill == entry or skill.rsplit("/", 1)[-1] == entry):
            return entry
    return None


def parse_settings(raw: Any) -> Optional[Tuple[FrozenSet[str], Optional[str]]]:
    """``(outils retires, note)`` d'une trame ou d'un ``agentConfig``.

    ``None`` quand le bloc ne dit RIEN des reglages (app anterieure) : l'etat
    connu est alors garde tel quel, jamais efface par erreur.
    """
    if not isinstance(raw, dict):
        return None
    has_tools = isinstance(raw.get("disabledTools"), list)
    has_note = "contextNote" in raw
    if not has_tools and not has_note:
        return None
    tools = frozenset(str(t) for t in (raw.get("disabledTools") or []) if isinstance(t, str) and t)
    note = raw.get("contextNote")
    note = note.strip() if isinstance(note, str) and note.strip() else None
    return tools, note


class AgentSettingsStore:
    """Reglages connus par profil, et notes deja posees par session.

    Lu par les crochets (threads d'Hermes), ecrit par la boucle WebSocket :
    tout passe sous un verrou.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_profile: Dict[str, Tuple[FrozenSet[str], Optional[str]]] = {}
        self._profile_of_channel: Dict[str, str] = {}
        self._session_notes: "OrderedDict[str, Optional[str]]" = OrderedDict()

    def reset(self) -> None:
        """Nouvelle connexion : l'app renvoie ce qui s'applique (trame ``agent.settings``)."""
        with self._lock:
            self._by_profile.clear()
            self._profile_of_channel.clear()

    def update(self, profile: str, raw: Any, channel: str = "") -> None:
        parsed = parse_settings(raw)
        with self._lock:
            if channel and profile:
                self._profile_of_channel[channel] = profile
            if parsed is not None and profile:
                self._by_profile[profile] = parsed

    def _resolve(self, channel: str) -> Tuple[FrozenSet[str], Optional[str]]:
        """Reglages de la conversation ; inconnue => TOUT ce qui est retire
        (le refus ne depend pas de savoir d'ou vient l'appel), note seulement
        s'il n'y a qu'un profil."""
        profile = self._profile_of_channel.get(channel or "")
        if profile in self._by_profile:
            return self._by_profile[profile]
        retired: FrozenSet[str] = frozenset().union(*(t for t, _ in self._by_profile.values()))
        notes = [n for _, n in self._by_profile.values()]
        return retired, (notes[0] if len(notes) == 1 else None)

    def blocked(self, tool_name: str, args: Any, channel: str = "") -> Optional[str]:
        with self._lock:
            retired, _ = self._resolve(channel)
        return retired_match(tool_name, args, retired) if retired else None

    def note_for_turn(self, session_id: str, channel: str = "") -> Optional[str]:
        """La note a poser sur CE tour, ou ``None`` : seulement si elle n'a pas
        encore ete posee dans cette session, ou si elle a change depuis."""
        if not session_id:
            return None
        with self._lock:
            _, note = self._resolve(channel)
            known = session_id in self._session_notes
            previous = self._session_notes.get(session_id)
            if known and previous == note:
                self._session_notes.move_to_end(session_id)
                return None
            if note is None and not known:
                return None
            self._session_notes[session_id] = note
            self._session_notes.move_to_end(session_id)
            while len(self._session_notes) > MAX_TRACKED_SESSIONS:
                self._session_notes.popitem(last=False)
        return note if note is not None else CLEARED_NOTE
