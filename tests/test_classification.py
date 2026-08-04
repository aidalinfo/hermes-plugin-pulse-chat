# -*- coding: utf-8 -*-
"""Tests de la classification message / tool_event du plugin Pulse Chat.

S'executent SANS hermes installe : le module pur ``classification.py`` est
charge directement par chemin de fichier (le paquet ``pulse-chat`` contient un
tiret et son ``__init__``/``adapter`` importent ``gateway.*``).
"""

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "classification.py"
_spec = importlib.util.spec_from_file_location("pulse_chat_classification", _MODULE_PATH)
classification = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(classification)

classify_outbound = classification.classify_outbound
parse_tool = classification.parse_tool


# ── Famille 1 : reponses normales -> message ─────────────────────────────

NORMAL_RESPONSES = [
    "Bonjour ! Voici le recapitulatif de votre commande.",
    "Le total est de 42 EUR, TVA incluse.",
    "1. Premier point\n2. Deuxieme point",
    "Voici le code demande :\n```python\nprint('bonjour')\n```",
    "Sure — I can help with that. What would you like to change?",
    "OK.",
    "Etape suivante : je vous envoie le devis par email.",
]


@pytest.mark.parametrize("content", NORMAL_RESPONSES)
def test_normal_response_is_message(content):
    assert classify_outbound(content, is_edit=False) == "message"


@pytest.mark.parametrize("content", NORMAL_RESPONSES)
def test_normal_response_has_no_tool_phase(content):
    assert parse_tool(content) == {"tool": None, "phase": None}


# ── Famille 2 : tool progress -> tool_event (phase progress, tool extrait) ─

TOOL_PROGRESS = [
    ('\U0001f50d Searching the web for "prix cuivre 2026"', "Searching"),
    ('⚙️ code_interpreter: "df.head()"', "code_interpreter"),
    ("⚙️ browse...", "browse"),
    ('⚙️ terminal(command)\n{"command": "ls"}', "terminal"),
    ("\U0001f4bb terminal\n```\nls -la\n```", "terminal"),
]


@pytest.mark.parametrize("content,tool", TOOL_PROGRESS)
def test_tool_progress_is_tool_event(content, tool):
    assert classify_outbound(content, is_edit=False) == "tool_event"


@pytest.mark.parametrize("content,tool", TOOL_PROGRESS)
def test_tool_progress_parse(content, tool):
    parsed = parse_tool(content)
    assert parsed["phase"] == "progress"
    assert parsed["tool"] == tool


def test_terminal_prefix_alone_falls_back_to_terminal():
    parsed = parse_tool("\U0001f4bb")
    assert parsed == {"tool": "terminal", "phase": "progress"}
    assert classify_outbound("\U0001f4bb", is_edit=False) == "tool_event"


# ── Famille 3 : interims a prefixes reserves -> tool_event (phase interim) ─

INTERIMS = [
    "⚡ Interrupting current task. I'll respond to your message shortly.",
    "⏳ Queued for the next turn — 1 message ahead.",
    "⏳ Subagent working...",
    "⏳ Compressing context...",
    "⏳ Working — 3 min",
    "⏩ Steered into current run. I'll fold this into what I'm doing.",
    "⏩ Steer queued — will apply next step.",
    "↪ Redirected current run.",
    "♻️ Recovered reply — the gateway restarted mid-turn.",
    "♻️ Gateway online",
    "♻ Gateway restarted",
    "\U0001f504 Background task started: pytest",
    "✅ Background task complete",
    "❌ Background task failed",
    "\U0001f4ac Let me think about this for a moment...",
]


@pytest.mark.parametrize("content", INTERIMS)
def test_interim_is_tool_event(content):
    assert classify_outbound(content, is_edit=False) == "tool_event"


@pytest.mark.parametrize("content", INTERIMS)
def test_interim_parse(content):
    parsed = parse_tool(content)
    assert parsed["phase"] == "interim"
    assert parsed["tool"] is None


# ── is_edit=True : TOUJOURS tool_event, meme un texte banal ──────────────

@pytest.mark.parametrize(
    "content",
    ["Bonjour, tout va bien.", "⚙️ browse...", "", "Reponse finale complete."],
)
def test_edit_is_always_tool_event(content):
    assert classify_outbound(content, is_edit=True) == "tool_event"


# ── Cas limites ──────────────────────────────────────────────────────────

def test_empty_content_is_tool_event():
    # Doute => tool_event (jamais Message) ; raw conserve cote app.
    assert classify_outbound("", is_edit=False) == "tool_event"


def test_whitespace_content_is_tool_event():
    assert classify_outbound("   \n ", is_edit=False) == "tool_event"


def test_none_like_empty_parse():
    assert parse_tool("") == {"tool": None, "phase": None}


def test_emoji_in_middle_of_text_is_message():
    assert classify_outbound("C'est fait ✅ tout est pret.", is_edit=False) == "message"
    assert classify_outbound("Parfait \U0001f50d regardons cela.", is_edit=False) == "message"


def test_short_word_then_word_is_message():
    # "Le" matche \S{1,4} mais n'est pas un emoji -> message.
    assert classify_outbound("Le monde est vaste.", is_edit=False) == "message"


def test_quoted_text_is_message():
    assert classify_outbound('"Bonjour" disait-il.', is_edit=False) == "message"


def test_reserved_prefix_without_space_is_tool_event():
    # Prefixe exact en debut de contenu, meme sans espace.
    assert classify_outbound("✅Done", is_edit=False) == "tool_event"


def test_unknown_emoji_prefix_is_tool_event():
    # Emoji non liste mais motif tool-progress -> doute => tool_event.
    parsed = parse_tool("\U0001f9ee compute: \"2+2\"")
    assert parsed["phase"] == "progress"
    assert parsed["tool"] == "compute"
    assert classify_outbound("\U0001f9ee compute", is_edit=False) == "tool_event"


def test_classify_returns_only_known_kinds():
    for content, is_edit in [("hello", False), ("hello", True), ("⏳ x", False)]:
        assert classify_outbound(content, is_edit=is_edit) in ("message", "tool_event")
