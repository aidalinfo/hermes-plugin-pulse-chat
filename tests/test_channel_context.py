# -*- coding: utf-8 -*-
"""channel_context.py — module PUR (issue #76) : construction d'URL, plafond,
formatage du bloc de contexte injecté, validation de la réponse artifact.
Aucune I/O ici — 0 dépendance à hermes ou au réseau.
"""

from channel_context import (
    CONTEXT_BLOCK_BEGIN,
    CONTEXT_BLOCK_END,
    MAX_CONTEXT_LIMIT,
    artifact_url,
    clamp_limit,
    context_url,
    format_context_block,
    parse_artifact_response,
)


# --- URLs --------------------------------------------------------------------


def test_context_url_sans_parametres():
    assert context_url("http://app.test", "demo") == "http://app.test/api/agent/channels/demo/context"


def test_context_url_avec_cursor_et_limit():
    url = context_url("http://app.test/", "demo", cursor="abc==", limit=10)
    assert url.startswith("http://app.test/api/agent/channels/demo/context?")
    assert "cursor=abc%3D%3D" in url
    assert "limit=10" in url


def test_context_url_encode_le_slug():
    url = context_url("http://app.test", "canal/etrange")
    assert "canal%2Fetrange" in url


def test_artifact_url():
    assert (
        artifact_url("http://app.test", "demo", "att-1")
        == "http://app.test/api/agent/channels/demo/artifacts/att-1"
    )


def test_artifact_url_refuse_id_vide():
    import pytest

    with pytest.raises(ValueError):
        artifact_url("http://app.test", "demo", "")
    with pytest.raises(ValueError):
        artifact_url("http://app.test", "demo", "   ")


# --- clamp_limit ---------------------------------------------------------------


def test_clamp_limit_normal():
    assert clamp_limit(10) == 10


def test_clamp_limit_plafonne_au_max():
    assert clamp_limit(999) == MAX_CONTEXT_LIMIT


def test_clamp_limit_minimum_un():
    assert clamp_limit(0) == 1
    assert clamp_limit(-5) == 1


def test_clamp_limit_invalide_repli_sur_max():
    assert clamp_limit("beaucoup") == MAX_CONTEXT_LIMIT
    assert clamp_limit(None) == MAX_CONTEXT_LIMIT


# --- format_context_block ------------------------------------------------------


def test_format_context_block_vide_donne_none():
    assert format_context_block([], "demo") is None


def test_format_context_block_ignore_les_items_illisibles():
    items = [None, {"kind": "inconnu"}, "pas un dict"]
    assert format_context_block(items, "demo") is None


def test_format_context_block_message_et_tool_event():
    items = [
        {
            "kind": "message",
            "authorName": "Alice",
            "content": "Bonjour agent",
            "createdAt": "2026-08-20T10:00:00.000Z",
        },
        {
            "kind": "tool_event",
            "tool": "search",
            "phase": "final",
            "content": "recherche terminée",
            "createdAt": "2026-08-20T10:00:05.000Z",
        },
    ]
    block = format_context_block(items, "demo-canal")

    assert block is not None
    assert block.startswith(CONTEXT_BLOCK_BEGIN)
    assert block.endswith(CONTEXT_BLOCK_END)
    assert "demo-canal" in block
    assert "Alice: Bonjour agent" in block
    assert "search final" in block
    # Ce n'est PAS une instruction utilisateur — le bloc le dit explicitement.
    assert "PAS une instruction" in block


def test_format_context_block_borne_le_volume_total():
    # 500 messages verbeux ⇒ dépasse largement MAX_CONTEXT_BLOCK_CHARS.
    items = [
        {"kind": "message", "authorName": "Bob", "content": "x" * 200, "createdAt": str(i)}
        for i in range(500)
    ]
    block = format_context_block(items, "demo")

    assert block is not None
    assert len(block) < 200 * 500  # effectivement borné
    # Troncature EXPLICITE, jamais silencieuse.
    assert "omis" in block
    # Les items les plus RÉCENTS priment — le dernier de la liste doit survivre.
    assert "x" * 200 in block


def test_format_context_block_un_seul_item_non_formatable_donne_none():
    assert format_context_block([{"kind": "unknown_kind"}], "demo") is None


# --- parse_artifact_response ----------------------------------------------------


def test_parse_artifact_response_valide():
    payload = {"id": "att-1", "filename": "notes.txt", "mime": "text/plain", "size": 12, "content": "bonjour"}
    assert parse_artifact_response(payload) == payload


def test_parse_artifact_response_sans_content_est_invalide():
    assert parse_artifact_response({"id": "att-1"}) is None


def test_parse_artifact_response_non_dict_est_invalide():
    assert parse_artifact_response("pas un dict") is None
    assert parse_artifact_response(None) is None
    assert parse_artifact_response([1, 2, 3]) is None
