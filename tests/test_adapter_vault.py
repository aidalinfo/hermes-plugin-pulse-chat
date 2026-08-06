# -*- coding: utf-8 -*-
"""Methodes artifacts + coffre-fort cote adaptateur (sans hermes installe).

Les I/O reseau sont remplacees : ce qui est verifie ici, c'est le cablage
(bonne URL, bonne methode, bon en-tete) et le fait qu'un echec de coffre ne
remonte JAMAIS en exception — une operation de fichier ratee ne doit pas faire
tomber le tour de parole de l'agent.
"""

import asyncio

from test_adapter_dedup import _load_adapter_module

adapter_module = _load_adapter_module()


class _Config:
    extra = {"url": "http://pulse-chat.test", "token": "token-test"}


def _make_adapter(post_ok=True):
    adapter = adapter_module.PulseChatAdapter(_Config())
    posted = []

    async def fake_post(payload, hermes_id):
        posted.append(payload)
        return adapter_module.SendResult(success=post_ok, message_id=hermes_id)

    adapter._post_agent_message = fake_post
    return adapter, posted


def _stub_vault(adapter, response=b"", fail=False):
    calls = []

    def fake_request(method, url, body=None, content_type=None):
        calls.append(
            {"method": method, "url": url, "body": body, "content_type": content_type}
        )
        return None if fail else response

    adapter._vault_request = fake_request
    return calls


def test_publish_artifact_poste_le_bon_payload():
    async def run():
        adapter, posted = _make_adapter()
        artifact_id = await adapter.publish_artifact(
            chat_id="demo", kind="mermaid", content="graph TD; A-->B;", title="Archi"
        )
        assert artifact_id is not None
        assert posted[0]["kind"] == "artifact"
        assert posted[0]["artifactKind"] == "mermaid"
        assert posted[0]["artifactId"] == artifact_id
        assert posted[0]["title"] == "Archi"

    asyncio.run(run())


def test_publish_artifact_reutilise_l_id_pour_versionner():
    async def run():
        adapter, posted = _make_adapter()
        first = await adapter.publish_artifact("demo", "markdown", "v1", artifact_id="doc")
        second = await adapter.publish_artifact("demo", "markdown", "v2", artifact_id="doc")
        assert first == second == "doc"
        assert [p["content"] for p in posted] == ["v1", "v2"]

    asyncio.run(run())


def test_publish_artifact_renvoie_none_si_le_post_echoue():
    async def run():
        adapter, _ = _make_adapter(post_ok=False)
        assert await adapter.publish_artifact("demo", "html", "<p>x</p>") is None

    asyncio.run(run())


def test_meme_titre_versionne_la_meme_carte_sans_id_explicite():
    """Le piege corrige : un id aleatoire par defaut noyait le fil."""

    async def run():
        adapter, posted = _make_adapter()
        first = await adapter.publish_artifact(
            "demo", "mermaid", "graph TD; A-->B;", title="Architecture reseau"
        )
        second = await adapter.publish_artifact(
            "demo", "mermaid", "graph TD; A-->C;", title="Architecture reseau"
        )
        assert first == second
        assert posted[0]["artifactId"] == posted[1]["artifactId"]

    asyncio.run(run())


def test_le_titre_est_normalise_avant_derivation():
    async def run():
        adapter, _ = _make_adapter()
        a = await adapter.publish_artifact("demo", "markdown", "x", title="  Mon  Doc ")
        b = await adapter.publish_artifact("demo", "markdown", "y", title="mon doc")
        assert a == b

    asyncio.run(run())


def test_titres_ou_types_differents_donnent_des_cartes_distinctes():
    async def run():
        adapter, _ = _make_adapter()
        a = await adapter.publish_artifact("demo", "mermaid", "x", title="Archi")
        b = await adapter.publish_artifact("demo", "mermaid", "x", title="Reseau")
        c = await adapter.publish_artifact("demo", "markdown", "x", title="Archi")
        assert len({a, b, c}) == 3

    asyncio.run(run())


def test_sans_titre_chaque_publication_a_sa_carte():
    async def run():
        adapter, _ = _make_adapter()
        a = await adapter.publish_artifact("demo", "html", "<p>1</p>")
        b = await adapter.publish_artifact("demo", "html", "<p>2</p>")
        # Sans titre, aucune identite stable : une carte par publication.
        assert a != b

    asyncio.run(run())


def test_un_id_explicite_prime_sur_le_titre():
    async def run():
        adapter, _ = _make_adapter()
        got = await adapter.publish_artifact(
            "demo", "mermaid", "x", artifact_id="mon-id", title="Archi"
        )
        assert got == "mon-id"

    asyncio.run(run())


def test_vault_list_parse_la_reponse():
    async def run():
        adapter, _ = _make_adapter()
        calls = _stub_vault(adapter, b'{"files":[{"path":"a.md"},{"path":"out/b.txt"}]}')
        assert await adapter.vault_list("demo") == ["a.md", "out/b.txt"]
        assert calls[0]["method"] == "GET"
        assert calls[0]["url"].endswith("/api/agent/vault/demo")

    asyncio.run(run())


def test_vault_list_tolere_une_reponse_illisible():
    async def run():
        adapter, _ = _make_adapter()
        _stub_vault(adapter, b"pas du json")
        assert await adapter.vault_list("demo") == []

    asyncio.run(run())


def test_vault_write_envoie_put_avec_content_type():
    async def run():
        adapter, _ = _make_adapter()
        calls = _stub_vault(adapter, b"{}")
        ok = await adapter.vault_write("demo", "out/r.md", b"# titre", "text/markdown")
        assert ok is True
        assert calls[0]["method"] == "PUT"
        assert calls[0]["url"].endswith("/api/agent/vault/demo/out/r.md")
        assert calls[0]["content_type"] == "text/markdown"
        assert calls[0]["body"] == b"# titre"

    asyncio.run(run())


def test_vault_delete_et_read():
    async def run():
        adapter, _ = _make_adapter()
        calls = _stub_vault(adapter, b"contenu")
        assert await adapter.vault_read("demo", "a.md") == b"contenu"
        assert await adapter.vault_delete("demo", "a.md") is True
        assert [c["method"] for c in calls] == ["GET", "DELETE"]

    asyncio.run(run())


def test_un_echec_de_coffre_ne_leve_jamais():
    async def run():
        adapter, _ = _make_adapter()
        _stub_vault(adapter, fail=True)
        assert await adapter.vault_list("demo") == []
        assert await adapter.vault_read("demo", "a.md") is None
        assert await adapter.vault_write("demo", "a.md", b"x") is False
        assert await adapter.vault_delete("demo", "a.md") is False

    asyncio.run(run())
