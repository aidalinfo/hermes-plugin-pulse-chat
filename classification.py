"""Classification pure des contenus sortants de l'agent (Pulse Chat).

Module VOLONTAIREMENT sans aucune dependance hermes/gateway : il doit rester
importable (et testable via pytest) sans hermes installe.

L'agent Hermes livre trois natures de contenu sur le meme canal :

1. Reponse finale             -> kind ``message``    (table Message cote app)
2. Tool progress              -> kind ``tool_event`` (phase ``progress``)
   Formats reels (gateway/run.py) : ``{emoji} {tool}: "{preview}"``,
   ``{emoji} {verb}{connector}{preview}`` (ex ``🔍 Searching the web for ...``),
   ``💻 terminal\n```...````, ``{emoji} {tool}({keys})\n{json}`` en verbose.
   Et le bloc terminal NU (`````\n<commande>\n`````, sans ``💻``) — voir
   ``_is_bare_terminal_bubble``.
3. Interims a prefixes emoji  -> kind ``tool_event`` (phase ``interim``)
   Prefixes litteraux : ⚡ ⏳ ⏩ ↪ ♻️ ♻ 🔄 ✅ ❌ 💬 💻

Regles (decision 3 du plan) :
- ``is_edit=True``  => TOUJOURS ``tool_event`` (le tool progress arrive via
  ``edit_message`` — bulle initiale puis editions, upsert par message_id).
- prefixe reserve en debut de contenu => ``tool_event``.
- motif tool-progress ``^<emoji court> <mot>...`` => ``tool_event``.
- sinon => ``message``. En cas de doute => ``tool_event`` (jamais Message),
  le contenu original part toujours dans ``raw`` cote app.
"""

from __future__ import annotations

import re
from typing import Dict, Literal, Optional

Kind = Literal["message", "tool_event"]

#: Prefixes reserves des interims du gateway (phase ``interim``).
INTERIM_PREFIXES: tuple = (
    "⚡",   # Interrupting current task...
    "⏳",   # Queued / Subagent working / Compressing context / Working — N min
    "⏩",   # Steered into current run / Steer queued
    "↪",   # Redirected current run
    "♻️",  # Recovered reply / Gateway online
    "♻",   # Gateway restarted (variante sans variation selector)
    "🔄",   # Background task started
    "✅",   # Background task complete
    "❌",   # Background task failed
    "💬",   # thinking relaye en tool progress
)

#: Prefixe du rendu terminal (``💻 terminal\n```...````) — phase ``progress``.
TERMINAL_PREFIX = "💻"

# Premier jeton (1-4 chars, sans espace) suivi d'un mot : candidat tool-progress.
_TOOL_TOKEN_RE = re.compile(r"^(\S{1,4})\s+([A-Za-z_][\w\-]*)")

# Ligne de fermeture d'un bloc terminal court, avec le suffixe de dedup d'Hermes.
_FENCE_CLOSE_RE = re.compile(r"^```(?: \(×\d+\))?$")

# Caracteres "transparents" dans un cluster emoji.
_EMOJI_JOINERS = frozenset((0xFE0F, 0x200D))  # variation selector-16, ZWJ


def _looks_like_emoji(token: str) -> bool:
    """Vrai si ``token`` ressemble a un emoji/symbole court (pas un mot).

    Heuristique volontairement stricte pour ne PAS matcher du texte normal
    (``Le total``, ``1. Premier``, ``« Bonjour »``) : tout caractere ASCII ou
    latin (ord < 0x2000) disqualifie le jeton.
    """
    if not token or len(token) > 4:
        return False
    seen_symbol = False
    for ch in token:
        code = ord(ch)
        if code in _EMOJI_JOINERS:
            continue
        if code < 0x2000:
            return False
        seen_symbol = True
    return seen_symbol


def _is_progress_line(line: str) -> bool:
    """Vrai si ``line`` est une ligne de tool progress a prefixe (``💻``, interim, emoji + mot)."""
    if line.startswith(TERMINAL_PREFIX) or line.startswith(INTERIM_PREFIXES):
        return True
    match = _TOOL_TOKEN_RE.match(line)
    return bool(match and _looks_like_emoji(match.group(1)))


def _is_bare_terminal_bubble(content: str) -> bool:
    """Vrai si ``content`` est une bulle de progression qui s'ouvre sur un bloc terminal NU.

    Hermes omet l'en-tete ``💻 terminal`` quand deux commandes terminal se
    suivent (``header = "" if last_was_terminal_block``), et n'oublie PAS ce
    drapeau quand une phrase de l'agent referme la bulle de progression
    (``_reset_progress_bubble``). La bulle suivante commence donc par
    `````\n<commande>\n````` sans aucun emoji, et passait en ``message`` : une
    commande ``infra-ssh`` affichee en bulle au milieu du fil, puis repetee en
    activite d'outil par l'edition qui suit.

    Reconnaissance STRICTE de la forme exacte qu'Hermes produit : bloc court
    (fence sans langage, UNE ligne de commande, fence de fermeture avec
    ``(×N)`` eventuel), et rien d'autre que d'autres blocs ou des lignes de
    progression a prefixe. La moindre ligne de prose ⇒ ``False`` : une reponse
    qui commence par une commande puis l'explique reste un message.
    """
    lines = (content or "").rstrip().split("\n")
    if not lines or lines[0] != "```":
        return False
    i, saw_block = 0, False
    while i < len(lines):
        line = lines[i]
        if line == "```":
            if i + 2 >= len(lines):
                return False
            command = lines[i + 1]
            if not command.strip() or command.startswith("```"):
                return False
            if not _FENCE_CLOSE_RE.match(lines[i + 2]):
                return False
            i += 3
            saw_block = True
            continue
        if not _is_progress_line(line):
            return False
        i += 1
    return saw_block


def parse_tool(content: str) -> Dict[str, Optional[str]]:
    """Extrait ``{"tool": ..., "phase": ...}`` d'un contenu sortant.

    - prefixe ``💻``            -> ``{"tool": "<mot>"|"terminal", "phase": "progress"}``
    - prefixe interim reserve  -> ``{"tool": None, "phase": "interim"}``
    - motif ``^<emoji> <mot>`` -> ``{"tool": "<mot>", "phase": "progress"}``
    - sinon                    -> ``{"tool": None, "phase": None}`` (reponse normale)

    Fonction pure, sans dependance gateway.
    """
    content = content or ""

    # 💻 terminal\n```...``` — tool progress avec code fence.
    if content.startswith(TERMINAL_PREFIX):
        match = _TOOL_TOKEN_RE.match(content)
        return {"tool": match.group(2) if match else "terminal", "phase": "progress"}

    # Interims (acks du gateway) — prefixes litteraux.
    for prefix in INTERIM_PREFIXES:
        if content.startswith(prefix):
            return {"tool": None, "phase": "interim"}

    # Motif tool-progress generique : emoji court + mot.
    match = _TOOL_TOKEN_RE.match(content)
    if match and _looks_like_emoji(match.group(1)):
        return {"tool": match.group(2), "phase": "progress"}

    # Bloc terminal nu : suite d'une serie de commandes terminal apres une phrase.
    if _is_bare_terminal_bubble(content):
        return {"tool": "terminal", "phase": "progress"}

    return {"tool": None, "phase": None}


def classify_outbound(content: str, *, is_edit: bool) -> Kind:
    """Classe un contenu sortant en ``'message'`` ou ``'tool_event'``.

    - ``is_edit=True`` => toujours ``tool_event`` (upsert par message_id).
    - contenu vide / blanc => ``tool_event`` (doute => tool_event).
    - prefixe reserve ou motif tool-progress => ``tool_event``.
    - sinon => ``message``.
    """
    if is_edit:
        return "tool_event"
    if not (content or "").strip():
        return "tool_event"  # doute => tool_event, jamais Message
    if parse_tool(content)["phase"] is not None:
        return "tool_event"
    return "message"
