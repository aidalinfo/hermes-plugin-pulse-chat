# -*- coding: utf-8 -*-
"""Cablage adaptateur du contexte de canal + artifacts bornes (issue #76).

Verifie :
- get_channel_context / read_channel_artifact : bonne URL, bon en-tete
  (x-hermes-profile), tolerance a un echec/reponse illisible (jamais
  d'exception — meme contrat que le coffre-fort) ;
- desactive par defaut (`context_window` absent/0) : AUCUN appel reseau, texte
  du message strictement inchange (non-regression d'un plugin deja deploye) ;
- active : le bloc de contexte est prefixe, delimite, jamais fondu dans le
  texte du declencheur, et un echec de lecture n'empeche jamais la reception
  du message (best effort, non bloquant) ;
- aucune regression de `adapter.send()` / `adapter.publish_artifact()`.
"""

import asyncio
import urllib.error
import urllib.request

from channel_context import CONTEXT_BLOCK_BEGIN, CONTEXT_BLOCK_END
from test_adapter_dedup import _load_adapter_module
from test_session_token import _capture_http, _header

adapter_module = _load_adapter_module()


class _Config:
    extra = {"url": "http://pulse-chat.test", "token": "token-test", "profile": "support"}


class _ContextConfig:
    extra = {
        "url": "http://pulse-chat.test",
        "token": "token-test",
        "profile": "support",
        "context_window": 5,
    }


def _make_adapter(config=None):
    return adapter_module.PulseChatAdapter(config or _Config())


def _stub_channel_get(adapter, response=b"", fail=False):
    calls = []

    def fake_request(url):
        calls.append(url)
        return None if fail else response

    adapter._channel_get_request = fake_request
    return calls


# --- Configuration -------------------------------------------------------------


def test_context_window_defaut_desactive():
    adapter = _make_adapter()
    assert adapter.context_window == 0


def test_context_window_plafonne_au_max_serveur():
    adapter = _make_adapter(
        _config_with(context_window=999)
    )
    assert adapter.context_window == adapter_module.MAX_CONTEXT_LIMIT


def test_context_window_invalide_repli_sur_zero():
    adapter = _make_adapter(_config_with(context_window="beaucoup"))
    assert adapter.context_window == 0


def _config_with(**extra):
    class _C:
        pass

    cfg = _C()
    cfg.extra = {"url": "http://pulse-chat.test", "token": "token-test", "profile": "support"}
    cfg.extra.update(extra)
    return cfg


# --- get_channel_context ---------------------------------------------------------


def test_get_channel_context_parse_la_reponse():
    async def run():
        adapter = _make_adapter()
        calls = _stub_channel_get(
            adapter, b'{"items":[{"kind":"message","content":"hi"}],"nextCursor":null}'
        )
        result = await adapter.get_channel_context("demo")
        assert result == {"items": [{"kind": "message", "content": "hi"}], "nextCursor": None}
        assert calls[0].endswith("/api/agent/channels/demo/context")

    asyncio.run(run())


def test_get_channel_context_transmet_cursor_et_limit():
    async def run():
        adapter = _make_adapter()
        calls = _stub_channel_get(adapter, b'{"items":[],"nextCursor":null}')
        await adapter.get_channel_context("demo", cursor="curs==", limit=7)
        assert "cursor=curs" in calls[0]
        assert "limit=7" in calls[0]

    asyncio.run(run())


def test_get_channel_context_tolere_un_echec_reseau():
    async def run():
        adapter = _make_adapter()
        _stub_channel_get(adapter, fail=True)
        assert await adapter.get_channel_context("demo") is None

    asyncio.run(run())


def test_get_channel_context_tolere_une_reponse_illisible():
    async def run():
        adapter = _make_adapter()
        _stub_channel_get(adapter, b"pas du json")
        assert await adapter.get_channel_context("demo") is None

    asyncio.run(run())


def test_get_channel_context_rejette_une_forme_inattendue():
    async def run():
        adapter = _make_adapter()
        _stub_channel_get(adapter, b'{"pas_items": true}')
        assert await adapter.get_channel_context("demo") is None

    asyncio.run(run())


# --- read_channel_artifact -------------------------------------------------------


def test_read_channel_artifact_parse_la_reponse():
    async def run():
        adapter = _make_adapter()
        calls = _stub_channel_get(
            adapter,
            b'{"id":"att-1","filename":"notes.txt","mime":"text/plain","size":7,"content":"bonjour"}',
        )
        result = await adapter.read_channel_artifact("demo", "att-1")
        assert result["content"] == "bonjour"
        assert calls[0].endswith("/api/agent/channels/demo/artifacts/att-1")

    asyncio.run(run())


def test_read_channel_artifact_id_vide_ne_fait_aucun_appel_reseau():
    async def run():
        adapter = _make_adapter()
        calls = _stub_channel_get(adapter, b"{}")
        assert await adapter.read_channel_artifact("demo", "") is None
        assert calls == []

    asyncio.run(run())


def test_read_channel_artifact_refus_serveur_explicite_415_413_404():
    """Le serveur renvoie un statut d'erreur (binaire/trop volumineux/hors canal) —
    le plugin ne fabrique jamais un contenu a partir de rien."""

    async def run():
        adapter = _make_adapter()
        _stub_channel_get(adapter, fail=True)  # simule un GET en echec HTTP
        assert await adapter.read_channel_artifact("demo", "att-bin") is None

    asyncio.run(run())


# --- En-tetes HTTP (x-hermes-profile) ---------------------------------------------


def test_channel_get_request_porte_le_bearer_sans_profil_declare(monkeypatch):
    adapter = _make_adapter()
    requests = _capture_http(monkeypatch)

    adapter._channel_get_request(
        adapter_module.context_url(adapter.base_url, "demo")
    )

    assert len(requests) == 1
    assert _header(requests[0], "x-hermes-profile") is None
    assert _header(requests[0], "Authorization") == "Bearer token-test"


def test_channel_get_request_porte_aussi_la_session_quand_disponible(monkeypatch):
    adapter = _make_adapter()
    adapter._handle_hello_ack({"type": "hello.ack", "sessionToken": "s" * 64})
    requests = _capture_http(monkeypatch)

    adapter._channel_get_request(adapter_module.context_url(adapter.base_url, "demo"))

    assert _header(requests[0], "x-hermes-session") == "s" * 64


# --- Injection dans le message transmis a Hermes (_handle_message_created) -------


def _event(message_id="msg-1", text="Bonjour"):
    return {
        "type": "message.created",
        "channel": {"slug": "canal-test", "name": "Canal Test"},
        "message": {"id": message_id, "text": text, "userId": "u1", "userName": "Alice"},
    }


def _make_dispatch_adapter(config=None):
    adapter = _make_adapter(config)
    dispatched = []

    async def fake_handle_message(event):
        dispatched.append(event)

    async def fake_send_ack(message_id):
        return None

    adapter.handle_message = fake_handle_message
    adapter._send_ack = fake_send_ack
    return adapter, dispatched


def test_desactive_par_defaut_le_texte_est_strictement_inchange():
    """Non-regression : un plugin deja deploye (context_window absent) ne fait
    AUCUN appel reseau supplementaire et ne modifie pas le texte."""
    adapter, dispatched = _make_dispatch_adapter()
    called = {"n": 0}

    async def boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("get_channel_context ne doit pas etre appele")

    adapter.get_channel_context = boom

    asyncio.run(adapter._handle_message_created(_event(text="Bonjour")))

    assert called["n"] == 0
    assert dispatched[0].text == "Bonjour"


def test_actif_le_bloc_est_prefixe_et_delimite_sans_alterer_le_declencheur():
    adapter, dispatched = _make_dispatch_adapter(_ContextConfig())

    async def fake_get_channel_context(chat_id, cursor=None, limit=None):
        assert chat_id == "canal-test"
        assert limit == 5
        return {
            "items": [{"kind": "message", "authorName": "Bob", "content": "salut hier"}],
            "nextCursor": None,
        }

    adapter.get_channel_context = fake_get_channel_context

    asyncio.run(adapter._handle_message_created(_event(text="Et maintenant ?")))

    text = dispatched[0].text
    assert text.startswith(CONTEXT_BLOCK_BEGIN)
    assert CONTEXT_BLOCK_END in text
    assert "Bob: salut hier" in text
    # Le texte du declencheur original est preserve tel quel, a la fin.
    assert text.endswith("Et maintenant ?")


def test_actif_un_echec_de_lecture_de_contexte_ne_bloque_pas_le_message():
    adapter, dispatched = _make_dispatch_adapter(_ContextConfig())

    async def failing_get_channel_context(chat_id, cursor=None, limit=None):
        raise RuntimeError("reseau indisponible")

    adapter.get_channel_context = failing_get_channel_context

    asyncio.run(adapter._handle_message_created(_event(text="Bonjour")))

    # Le message arrive quand meme, SANS bloc de contexte (pas de fausse
    # affirmation d'un contexte lu alors qu'il ne l'a pas ete).
    assert dispatched[0].text == "Bonjour"


def test_actif_aucun_item_de_contexte_laisse_le_texte_inchange():
    adapter, dispatched = _make_dispatch_adapter(_ContextConfig())

    async def empty_context(chat_id, cursor=None, limit=None):
        return {"items": [], "nextCursor": None}

    adapter.get_channel_context = empty_context

    asyncio.run(adapter._handle_message_created(_event(text="Bonjour")))

    assert dispatched[0].text == "Bonjour"


# --- Non-regression : send() / publish_artifact() ---------------------------------


def test_send_et_publish_artifact_fonctionnent_toujours_avec_le_contexte_configure(
    monkeypatch,
):
    """Le nouveau cablage (#76) ne doit rien casser des primitives existantes,
    meme quand `context_window` est actif sur le meme adaptateur."""
    adapter = _make_adapter(_ContextConfig())
    posted = []

    async def fake_post(payload, hermes_id):
        posted.append(payload)
        return adapter_module.SendResult(success=True, message_id=hermes_id)

    written = []

    def fake_vault(method, url, body=None, content_type=None):
        written.append(method)
        return b"{}"

    adapter._post_agent_message = fake_post
    adapter._vault_request = fake_vault

    async def run():
        send_result = await adapter.send("demo", "bonjour")
        artifact_id = await adapter.publish_artifact("demo", "markdown", "contenu", title="T")
        return send_result, artifact_id

    send_result, artifact_id = asyncio.run(run())

    assert send_result.success is True
    assert artifact_id is not None
    assert written == ["PUT"]
    assert len(posted) == 2
