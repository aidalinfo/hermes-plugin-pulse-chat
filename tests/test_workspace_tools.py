# -*- coding: utf-8 -*-
"""Outils ``pulse_vault_write`` et ``pulse_publish_artifact`` (sans hermes installe).

Ce qui est verifie ici, et chaque point est un mode d'echec MUET :

  - les deux outils sont ENREGISTRES et nommes par ``platform_hint`` — sans eux,
    ``vault_write`` / ``publish_artifact`` n'avaient aucun appelant, et un agent
    qui avait produit un PDF ne pouvait pas le poser dans la conversation ;
  - le canal vient du contexte de session, jamais d'un argument ;
  - un fichier local part EN FLUX, avec sa taille annoncee, et arrive intact
    (essai sur un vrai serveur HTTP local) ;
  - aucun succes n'est annonce sans 2xx de l'app, et un refus arrive avec son
    message ;
  - ``kind='file'`` ne passe jamais par ``content`` (un binaire y serait corrompu).

Style du depot : ``asyncio.run`` plutot que pytest-asyncio.
"""

import asyncio
import http.server
import json
import sys
import threading
import types

import pytest

from test_adapter_dedup import _load_adapter_module

adapter_module = _load_adapter_module()
workspace = sys.modules["pulse_chat_plugin_under_test.workspace"]

SESSION = {"HERMES_SESSION_CHAT_ID": "projet-oxboarding"}


def _install_session_context():
    mod = types.ModuleType("gateway.session_context")
    mod.get_session_env = lambda name, default="": SESSION.get(name, default)
    sys.modules["gateway.session_context"] = mod


class _Config:
    extra = {"url": "http://pulse-chat.test", "token": "token-test"}


@pytest.fixture(autouse=True)
def _isolation():
    """Contexte de session installe, et aucun adaptateur residuel « connecte »
    d'un autre test (``_live_adapter`` prend le premier du ``WeakSet``)."""
    _install_session_context()

    def _deconnecter():
        for a in list(adapter_module._LIVE_ADAPTERS):
            a.is_connected = False

    _deconnecter()
    yield
    _deconnecter()


def _adapter(responses=None):
    """Adaptateur dont les appels HTTP sont enregistres, pas emis."""
    adapter = adapter_module.PulseChatAdapter(_Config())
    calls = []
    queue = list(responses or [])

    def fake_http(method, url, *, body=None, headers=None, timeout=None):
        data = body.read() if hasattr(body, "read") else body
        calls.append({"method": method, "url": url, "body": data, "headers": headers or {}})
        return queue.pop(0) if queue else (201, {})

    adapter._http_detailed = fake_http
    return adapter, calls


def _run(coro):
    return json.loads(asyncio.run(coro))


class TestNoms:
    def test_les_noms_sont_PREFIXES(self):
        assert workspace.VAULT_WRITE_TOOL_NAME.startswith("pulse_")
        assert workspace.PUBLISH_TOOL_NAME.startswith("pulse_")

    def test_aucun_schema_ne_prend_de_canal(self):
        for schema in (workspace.VAULT_WRITE_SCHEMA, workspace.PUBLISH_SCHEMA):
            props = schema["parameters"]["properties"]
            assert not {"channel", "channel_slug", "chat_id"} & set(props)

    def test_le_schema_de_publication_connait_le_type_file(self):
        assert "file" in workspace.PUBLISH_SCHEMA["parameters"]["properties"]["kind"]["enum"]

    def test_le_delai_denvoi_reste_SOUS_le_plafond_dhermes(self):
        assert workspace.UPLOAD_TIMEOUT_SECONDS < 300

    def test_la_description_decriture_dit_que_ca_naffiche_rien(self):
        assert "pulse_publish_artifact" in workspace.VAULT_WRITE_DESCRIPTION
        assert "N'AFFICHE RIEN" in workspace.VAULT_WRITE_DESCRIPTION


class TestPartiePure:
    def test_exactement_une_source(self):
        with pytest.raises(workspace.WorkspaceToolError):
            workspace.exclusive_source({}, "local_path")
        with pytest.raises(workspace.WorkspaceToolError):
            workspace.exclusive_source({"local_path": "/a", "content": "x"}, "local_path")
        assert workspace.exclusive_source({"local_path": "/a", "content": ""}, "local_path") == ("/a", None)

    def test_fichier_local_introuvable(self, tmp_path):
        with pytest.raises(workspace.WorkspaceToolError) as err:
            workspace.resolve_local_file(str(tmp_path / "absent.pdf"))
        assert err.value.code == "local_file_not_found"

    def test_un_dossier_est_refuse(self, tmp_path):
        with pytest.raises(workspace.WorkspaceToolError) as err:
            workspace.resolve_local_file(str(tmp_path))
        assert err.value.code == "local_file_invalid"

    def test_le_plafond_est_verifie_AVANT_lenvoi(self, tmp_path, monkeypatch):
        big = tmp_path / "gros.bin"
        big.write_bytes(b"x" * 11)
        monkeypatch.setattr(workspace, "MAX_VAULT_FILE_BYTES", 10)
        with pytest.raises(workspace.WorkspaceToolError) as err:
            workspace.resolve_local_file(str(big))
        assert err.value.code == "file_too_large"

    def test_type_annonce_depuis_lextension(self):
        assert workspace.content_type_for("artifacts/devis.pdf") == "application/pdf"
        assert workspace.content_type_for("sans-extension") == "application/octet-stream"

    def test_un_refus_http_porte_le_message_de_lapp(self):
        out = json.loads(workspace.http_refusal(409, {"statusMessage": "Canal archivé (lecture seule)"}))
        assert out["status"] == "refused"
        assert out["code"] == "conflict"
        assert "Canal archivé" in out["message"]

    def test_aucune_reponse_nest_une_panne_de_lapp(self):
        assert json.loads(workspace.http_refusal(0, None))["code"] == "app_unavailable"


class TestEcritureCoffre:
    def test_un_fichier_local_part_intact_avec_sa_taille(self, tmp_path):
        pdf = tmp_path / "devis.pdf"
        pdf.write_bytes(b"%PDF-1.7\x00\xff binaire")
        adapter, calls = _adapter()
        out = _run(
            adapter.tool_vault_write(
                "projet-oxboarding", {"path": "artifacts/devis.pdf", "local_path": str(pdf)}
            )
        )
        assert out["status"] == "written"
        assert out["reference"] == "vault:artifacts/devis.pdf"
        assert "pulse_publish_artifact" in out["next"]
        assert calls[0]["method"] == "PUT"
        assert calls[0]["url"] == "http://pulse-chat.test/api/agent/vault/projet-oxboarding/artifacts/devis.pdf"
        assert calls[0]["body"] == pdf.read_bytes()
        assert calls[0]["headers"]["Content-Type"] == "application/pdf"
        assert calls[0]["headers"]["Content-Length"] == str(len(pdf.read_bytes()))

    def test_un_texte_part_en_utf8(self):
        adapter, calls = _adapter()
        out = _run(adapter.tool_vault_write("c", {"path": "notes/a.md", "content": "Été"}))
        assert out["status"] == "written"
        assert calls[0]["body"] == "Été".encode("utf-8")

    def test_un_chemin_remontant_est_refuse_sans_appel(self):
        adapter, calls = _adapter()
        out = _run(adapter.tool_vault_write("c", {"path": "../x.pdf", "content": "a"}))
        assert out["code"] == "invalid_request"
        assert calls == []

    def test_un_refus_de_lapp_nest_JAMAIS_un_succes(self, tmp_path):
        pdf = tmp_path / "a.pdf"
        pdf.write_bytes(b"x")
        adapter, _ = _adapter([(507, {"statusMessage": "Quota de fichiers atteint"})])
        out = _run(adapter.tool_vault_write("c", {"path": "a.pdf", "local_path": str(pdf)}))
        assert out["status"] == "refused"
        assert out["code"] == "vault_full"


class TestPublication:
    def test_un_fichier_se_publie_par_son_chemin_sans_rien_reecrire(self):
        adapter, calls = _adapter()
        out = _run(
            adapter.tool_publish_artifact(
                "projet-oxboarding",
                {"kind": "file", "path": "artifacts/devis.pdf", "title": "Devis V4", "artifact_id": "devis-client"},
            )
        )
        assert out == {**out, "status": "published", "artifactId": "devis-client", "kind": "file"}
        assert [c["method"] for c in calls] == ["POST"]
        payload = json.loads(calls[0]["body"])
        assert payload["kind"] == "artifact"
        assert payload["artifactKind"] == "file"
        assert payload["path"] == "artifacts/devis.pdf"
        assert payload["channelSlug"] == "projet-oxboarding"

    def test_file_refuse_content(self):
        adapter, calls = _adapter()
        out = _run(adapter.tool_publish_artifact("c", {"kind": "file", "title": "T", "content": "x"}))
        assert out["code"] == "invalid_request"
        assert calls == []

    def test_un_contenu_texte_est_ecrit_PUIS_publie(self):
        adapter, calls = _adapter()
        out = _run(adapter.tool_publish_artifact("c", {"kind": "markdown", "title": "Note", "content": "# Hi"}))
        assert out["status"] == "published"
        assert [c["method"] for c in calls] == ["PUT", "POST"]
        assert json.loads(calls[1]["body"])["path"] == out["path"]

    def test_une_ecriture_ratee_ne_publie_pas(self):
        adapter, calls = _adapter([(413, {"statusMessage": "Trop gros"})])
        out = _run(adapter.tool_publish_artifact("c", {"kind": "markdown", "title": "N", "content": "x"}))
        assert out["code"] == "file_too_large"
        assert [c["method"] for c in calls] == ["PUT"]

    def test_un_fichier_absent_du_coffre_revient_avec_un_conseil(self):
        adapter, _ = _adapter([(404, {"statusMessage": "Fichier introuvable"})])
        out = _run(adapter.tool_publish_artifact("c", {"kind": "file", "title": "T", "path": "a.pdf"}))
        assert out["code"] == "not_found"
        assert "pulse_vault_write" in out["next"]

    def test_type_inconnu_et_titre_absent(self):
        adapter, calls = _adapter()
        assert _run(adapter.tool_publish_artifact("c", {"kind": "pdf", "title": "T", "path": "a"}))["code"] == "invalid_request"
        assert _run(adapter.tool_publish_artifact("c", {"kind": "file", "path": "a.pdf"}))["code"] == "invalid_request"
        assert calls == []

    def test_lidentifiant_par_defaut_est_STABLE(self):
        adapter, _ = _adapter()
        a = _run(adapter.tool_publish_artifact("c", {"kind": "file", "title": "Devis", "path": "a.pdf"}))
        b = _run(adapter.tool_publish_artifact("c", {"kind": "file", "title": "Devis", "path": "a.pdf"}))
        assert a["artifactId"] == b["artifactId"]


class _WsLoop:
    """Boucle du WebSocket dans son propre thread — comme en vrai."""

    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self.loop

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


class TestHandler:
    def test_le_handler_atteint_ladaptateur_sur_la_boucle_du_ws(self):
        adapter, calls = _adapter()
        with _WsLoop() as ws_loop:
            adapter._loop = ws_loop
            adapter.is_connected = True
            out = _run(
                adapter_module._pulse_publish_artifact({"kind": "file", "title": "Devis", "path": "a.pdf"})
            )
        assert out["status"] == "published"
        # Le canal vient de la SESSION, pas des arguments.
        assert json.loads(calls[0]["body"])["channelSlug"] == "projet-oxboarding"

    def test_refuse_sans_conversation(self):
        SESSION.pop("HERMES_SESSION_CHAT_ID")
        try:
            out = _run(adapter_module._pulse_vault_write({"path": "a", "content": "x"}))
        finally:
            SESSION["HERMES_SESSION_CHAT_ID"] = "projet-oxboarding"
        assert out["code"] == "no_channel"

    def test_refuse_sans_adaptateur_connecte(self):
        out = _run(adapter_module._pulse_vault_write({"path": "a", "content": "x"}))
        assert out["code"] == "not_connected"


class TestEnregistrement:
    class _Ctx:
        def __init__(self, fail_on=None):
            self.tools = {}
            self.platform = None
            self.fail_on = fail_on

        def register_platform(self, **kw):
            self.platform = kw

        def register_tool(self, **kw):
            if kw["name"] == self.fail_on:
                raise RuntimeError("boom")
            self.tools[kw["name"]] = kw
            return object()

        def register_skill(self, *a, **kw):
            pass

    def test_les_deux_outils_sont_enregistres_dans_le_toolset_de_la_plateforme(self):
        ctx = self._Ctx()
        adapter_module.register(ctx)
        for name in ("pulse_vault_write", "pulse_publish_artifact"):
            assert ctx.tools[name]["toolset"] == "pulse_chat"
            assert ctx.tools[name]["is_async"] is True

    def test_platform_hint_nomme_les_deux_outils(self):
        ctx = self._Ctx()
        adapter_module.register(ctx)
        hint = ctx.platform["platform_hint"]
        assert "pulse_vault_write" in hint and "pulse_publish_artifact" in hint

    def test_lechec_dun_outil_nempeche_pas_lautre(self):
        ctx = self._Ctx(fail_on="pulse_vault_write")
        adapter_module.register(ctx)
        assert "pulse_publish_artifact" in ctx.tools
        assert "pulse_request_approval" in ctx.tools
        assert ctx.platform is not None


class TestEnvoiReel:
    """Un VRAI serveur HTTP : c'est le seul moyen de savoir qu'urllib envoie un
    fichier ouvert en flux avec la taille annoncee, et non un corps vide ou
    decoupe en ``chunked`` que l'app lirait autrement."""

    def test_le_fichier_arrive_intact_avec_son_content_length(self, tmp_path):
        received = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_PUT(self):
                length = int(self.headers["Content-Length"])
                received.update(
                    body=self.rfile.read(length),
                    length=length,
                    chunked=self.headers.get("Transfer-Encoding"),
                    auth=self.headers.get("Authorization"),
                    ctype=self.headers.get("Content-Type"),
                )
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"path":"a.pdf"}')

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            content = bytes(range(256)) * 4096  # 1 Mio de binaire
            pdf = tmp_path / "devis.pdf"
            pdf.write_bytes(content)

            class Cfg:
                extra = {"url": f"http://127.0.0.1:{server.server_port}", "token": "tk"}

            adapter = adapter_module.PulseChatAdapter(Cfg())
            out = _run(adapter.tool_vault_write("c", {"path": "a.pdf", "local_path": str(pdf)}))
        finally:
            server.shutdown()
        assert out["status"] == "written"
        assert received["body"] == content
        assert received["length"] == len(content)
        assert received["chunked"] is None
        assert received["auth"] == "Bearer tk"
        assert received["ctype"] == "application/pdf"
