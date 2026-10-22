# -*- coding: utf-8 -*-
"""Outil ``pulse_vault_read`` (issue #47), sans hermes installe.

Ce qui est verifie ici, et chaque point est un mode d'echec MUET :

  - un binaire du coffre arrive OCTET POUR OCTET, et le sha256 rendu est celui
    des octets servis (essai sur un vrai serveur HTTP local) ;
  - aucun octet ni base64 ne revient au modele ;
  - le canal vient du contexte de session, jamais d'un argument ;
  - la destination reste sous les racines autorisees : ni ``..``, ni lien
    symbolique, ni ecrasement sans ``overwrite`` ;
  - un refus HTTP, un depassement de plafond ou un transfert coupe ne laisse
    AUCUN fichier final (ni ``.part`` oublie).

Style du depot : ``asyncio.run`` plutot que pytest-asyncio.
"""

import asyncio
import hashlib
import http.server
import io
import json
import os
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


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    """Racines autorisees sous ``tmp_path``, contexte de session installe, et
    aucun adaptateur residuel « connecte » d'un autre test."""
    _install_session_context()
    monkeypatch.setenv(
        workspace.VAULT_READ_ROOTS_ENV,
        os.pathsep.join([str(tmp_path / "workspace"), str(tmp_path / "scratch")]),
    )

    def _deconnecter():
        for a in list(adapter_module._LIVE_ADAPTERS):
            a.is_connected = False

    _deconnecter()
    yield
    _deconnecter()


def _run(coro):
    return json.loads(asyncio.run(coro))


def _files(root):
    return sorted(
        os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(root) for f in fs
    )


class _Coffre:
    """Vrai serveur HTTP qui sert le coffre : ``files`` = chemin d'URL -> octets."""

    def __init__(self, files=None, *, status=200, body=b"", truncate=None, headers=None):
        self.files = files or {}
        self.status = status
        self.body = body
        self.truncate = truncate
        self.headers = headers or {}
        self.requests = []

    def __enter__(self):
        coffre = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                coffre.requests.append(
                    {"path": self.path, "auth": self.headers.get("Authorization")}
                )
                data = coffre.files.get(self.path)
                if data is None or coffre.status != 200:
                    status = 404 if data is None and coffre.status == 200 else coffre.status
                    body = coffre.body or json.dumps({"statusMessage": "absent"}).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                for name, value in coffre.headers.items():
                    self.send_header(name, value)
                self.end_headers()
                # Transfert coupe : on annonce tout, on n'envoie qu'une partie.
                self.wfile.write(data if coffre.truncate is None else data[: coffre.truncate])

            def log_message(self, *a):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def adapter(self):
        port = self.server.server_port

        class Cfg:
            extra = {"url": f"http://127.0.0.1:{port}", "token": "tk"}

        return adapter_module.PulseChatAdapter(Cfg())


ZIP = b"PK\x03\x04" + bytes(range(256)) * 4096  # ~1 Mio de binaire
ZIP_URL = "/api/agent/vault/c/archives/export.zip"


class TestNoms:
    def test_le_nom_est_PREFIXE(self):
        assert workspace.VAULT_READ_TOOL_NAME == "pulse_vault_read"

    def test_le_schema_ne_prend_pas_de_canal(self):
        props = workspace.VAULT_READ_SCHEMA["parameters"]["properties"]
        assert not {"channel", "channel_slug", "chat_id"} & set(props)
        assert workspace.VAULT_READ_SCHEMA["parameters"]["required"] == ["path"]

    def test_les_racines_par_defaut_sont_celles_de_limage(self, monkeypatch):
        monkeypatch.delenv(workspace.VAULT_READ_ROOTS_ENV)
        assert workspace.vault_read_roots() == ("/opt/data/workspace", "/opt/data/cache/scratch")

    def test_une_racine_relative_est_ignoree(self, monkeypatch, tmp_path):
        monkeypatch.setenv(workspace.VAULT_READ_ROOTS_ENV, f"relatif{os.pathsep}{tmp_path}")
        assert workspace.vault_read_roots() == (str(tmp_path),)


class TestDestination:
    def _roots(self, tmp_path):
        return (str(tmp_path / "workspace"), str(tmp_path / "scratch"))

    def test_par_defaut_dans_linbox_du_workspace(self, tmp_path):
        target = workspace.resolve_download_target("a/b.zip", None, roots=self._roots(tmp_path))
        assert target == str(tmp_path / "workspace" / "inbox" / "a" / "b.zip")
        assert (tmp_path / "workspace" / "inbox" / "a").is_dir()

    def test_un_chemin_relatif_part_du_workspace(self, tmp_path):
        target = workspace.resolve_download_target("x", "out/y.pdf", roots=self._roots(tmp_path))
        assert target == str(tmp_path / "workspace" / "out" / "y.pdf")

    def test_la_seconde_racine_est_autorisee(self, tmp_path):
        dest = str(tmp_path / "scratch" / "y.pdf")
        assert workspace.resolve_download_target("x", dest, roots=self._roots(tmp_path)) == dest

    @pytest.mark.parametrize(
        "local",
        ["../evade.zip", "sub/../../evade.zip", "/etc/passwd", "~/.hermes/.env", "/opt/data/.env"],
    )
    def test_hors_des_racines_est_refuse(self, tmp_path, local):
        with pytest.raises(workspace.WorkspaceToolError) as err:
            workspace.resolve_download_target("x", local, roots=self._roots(tmp_path))
        assert err.value.code == "local_path_forbidden"

    def test_un_prefixe_voisin_nest_pas_la_racine(self, tmp_path):
        with pytest.raises(workspace.WorkspaceToolError):
            workspace.resolve_download_target(
                "x", str(tmp_path / "workspace-evil" / "a"), roots=self._roots(tmp_path)
            )

    def test_un_dossier_lien_symbolique_est_refuse(self, tmp_path):
        outside = tmp_path / "dehors"
        outside.mkdir()
        (tmp_path / "workspace").mkdir()
        os.symlink(outside, tmp_path / "workspace" / "lien")
        with pytest.raises(workspace.WorkspaceToolError) as err:
            workspace.resolve_download_target("x", "lien/a.zip", roots=self._roots(tmp_path))
        assert err.value.code == "local_path_forbidden"
        assert list(outside.iterdir()) == []

    def test_un_fichier_lien_symbolique_est_refuse_meme_avec_overwrite(self, tmp_path):
        secret = tmp_path / ".env"
        secret.write_text("TOKEN=1")
        (tmp_path / "workspace").mkdir()
        os.symlink(secret, tmp_path / "workspace" / "a.zip")
        with pytest.raises(workspace.WorkspaceToolError) as err:
            workspace.resolve_download_target(
                "x", "a.zip", overwrite=True, roots=self._roots(tmp_path)
            )
        assert err.value.code == "local_path_forbidden"

    def test_un_fichier_existant_nest_pas_ecrase_sans_overwrite(self, tmp_path):
        (tmp_path / "workspace").mkdir()
        (tmp_path / "workspace" / "a.zip").write_bytes(b"ancien")
        with pytest.raises(workspace.WorkspaceToolError) as err:
            workspace.resolve_download_target("x", "a.zip", roots=self._roots(tmp_path))
        assert err.value.code == "local_file_exists"
        target = workspace.resolve_download_target(
            "x", "a.zip", overwrite=True, roots=self._roots(tmp_path)
        )
        assert target == str(tmp_path / "workspace" / "a.zip")

    def test_la_destination_par_defaut_prend_un_nom_libre(self, tmp_path):
        inbox = tmp_path / "workspace" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "a.zip").write_bytes(b"ancien")
        target = workspace.resolve_download_target("a.zip", None, roots=self._roots(tmp_path))
        assert target == str(inbox / "a-1.zip")

    def test_un_dossier_nest_pas_une_destination(self, tmp_path):
        (tmp_path / "workspace" / "d").mkdir(parents=True)
        with pytest.raises(workspace.WorkspaceToolError) as err:
            workspace.resolve_download_target("x", "d", overwrite=True, roots=self._roots(tmp_path))
        assert err.value.code == "local_file_invalid"


class _Coupe(io.BytesIO):
    """Source qui casse apres un premier bloc, comme une socket reinitialisee."""

    def __init__(self, data):
        super().__init__(data)
        self.calls = 0

    def read(self, n=-1):
        self.calls += 1
        if self.calls > 1:
            raise ConnectionResetError("coupe")
        return super().read(n)


class TestEcritureAtomique:
    def test_copie_intacte_et_empreinte(self, tmp_path):
        final = str(tmp_path / "a.zip")
        size, digest = workspace.stream_to_file(io.BytesIO(ZIP), final, expected_size=len(ZIP))
        assert (size, digest) == (len(ZIP), hashlib.sha256(ZIP).hexdigest())
        assert open(final, "rb").read() == ZIP
        assert _files(tmp_path) == ["a.zip"]

    def test_le_plafond_interrompt_sans_fichier(self, tmp_path):
        with pytest.raises(workspace.WorkspaceToolError) as err:
            workspace.stream_to_file(io.BytesIO(ZIP), str(tmp_path / "a.zip"), limit=1000)
        assert err.value.code == "file_too_large"
        assert _files(tmp_path) == []

    def test_une_taille_differente_de_lannonce_ne_publie_rien(self, tmp_path):
        with pytest.raises(workspace.WorkspaceToolError) as err:
            workspace.stream_to_file(
                io.BytesIO(ZIP[:10]), str(tmp_path / "a.zip"), expected_size=len(ZIP)
            )
        assert err.value.code == "incomplete_transfer"
        assert _files(tmp_path) == []

    def test_une_lecture_coupee_ne_laisse_rien(self, tmp_path):
        with pytest.raises(ConnectionResetError):
            workspace.stream_to_file(_Coupe(ZIP * 2), str(tmp_path / "a.zip"))
        assert _files(tmp_path) == []

    def test_un_fichier_apparu_pendant_le_transfert_nest_pas_ecrase(self, tmp_path):
        final = tmp_path / "a.zip"
        final.write_bytes(b"arrive entre-temps")
        with pytest.raises(workspace.WorkspaceToolError) as err:
            workspace.stream_to_file(io.BytesIO(ZIP), str(final))
        assert err.value.code == "local_file_exists"
        assert final.read_bytes() == b"arrive entre-temps"
        assert _files(tmp_path) == ["a.zip"]

    def test_overwrite_remplace(self, tmp_path):
        final = tmp_path / "a.zip"
        final.write_bytes(b"ancien")
        workspace.stream_to_file(io.BytesIO(ZIP), str(final), overwrite=True)
        assert final.read_bytes() == ZIP


class TestLectureReelle:
    """Un VRAI serveur HTTP : seul moyen de savoir que les octets traversent
    urllib sans etre decodes, tronques ou charges entiers en memoire."""

    def test_un_binaire_arrive_bit_pour_bit(self, tmp_path):
        with _Coffre({ZIP_URL: ZIP}, headers={"Content-Type": "application/zip"}) as coffre:
            out = _run(coffre.adapter().tool_vault_read("c", {"path": "archives/export.zip"}))
        assert out["status"] == "downloaded"
        local = tmp_path / "workspace" / "inbox" / "archives" / "export.zip"
        assert out["local_path"] == str(local)
        assert local.read_bytes() == ZIP
        assert out["sha256"] == hashlib.sha256(ZIP).hexdigest()
        assert out["size"] == len(ZIP)
        assert out["mime"] == "application/zip"
        assert coffre.requests == [{"path": ZIP_URL, "auth": "Bearer tk"}]

    def test_aucun_octet_ne_revient_au_modele(self):
        with _Coffre({ZIP_URL: ZIP}) as coffre:
            raw = asyncio.run(coffre.adapter().tool_vault_read("c", {"path": "archives/export.zip"}))
        assert len(raw) < 2000
        assert set(json.loads(raw)) == {"status", "path", "local_path", "size", "sha256", "mime", "next"}

    def test_un_texte_et_une_destination_explicite(self, tmp_path):
        texte = "Compte rendu — été\n".encode("utf-8")
        dest = tmp_path / "scratch" / "cr.md"
        with _Coffre({"/api/agent/vault/c/notes/cr.md": texte}) as coffre:
            out = _run(
                coffre.adapter().tool_vault_read("c", {"path": "notes/cr.md", "local_path": str(dest)})
            )
        assert out["status"] == "downloaded"
        assert dest.read_bytes() == texte

    def test_un_fichier_absent_ne_cree_rien(self, tmp_path):
        with _Coffre({}) as coffre:
            out = _run(coffre.adapter().tool_vault_read("c", {"path": "absent.zip"}))
        assert out["status"] == "refused"
        assert out["code"] == "not_found"
        assert "channel_vault_list" in out["next"]
        assert _files(tmp_path) == []

    @pytest.mark.parametrize("status,code", [(403, "not_authorized"), (500, "app_unavailable")])
    def test_un_refus_http_nest_jamais_un_succes(self, tmp_path, status, code):
        with _Coffre({ZIP_URL: ZIP}, status=status) as coffre:
            out = _run(coffre.adapter().tool_vault_read("c", {"path": "archives/export.zip"}))
        assert out["status"] == "refused"
        assert out["code"] == code
        assert _files(tmp_path) == []

    def test_un_transfert_coupe_ne_laisse_aucun_fichier(self, tmp_path):
        with _Coffre({ZIP_URL: ZIP}, truncate=1000) as coffre:
            out = _run(coffre.adapter().tool_vault_read("c", {"path": "archives/export.zip"}))
        assert out["status"] == "refused"
        assert out["code"] == "incomplete_transfer"
        assert _files(tmp_path) == []

    def test_une_taille_annoncee_trop_grande_est_refusee_avant_ecriture(self, tmp_path, monkeypatch):
        monkeypatch.setattr(adapter_module, "MAX_VAULT_FILE_BYTES", 1000)
        with _Coffre({ZIP_URL: ZIP}) as coffre:
            out = _run(coffre.adapter().tool_vault_read("c", {"path": "archives/export.zip"}))
        assert out["code"] == "file_too_large"
        assert _files(tmp_path) == []

    def test_un_ecrasement_non_demande_est_refuse_sans_appel(self, tmp_path):
        dest = tmp_path / "workspace" / "a.zip"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"ancien")
        with _Coffre({ZIP_URL: ZIP}) as coffre:
            out = _run(
                coffre.adapter().tool_vault_read(
                    "c", {"path": "archives/export.zip", "local_path": str(dest)}
                )
            )
            assert coffre.requests == []
        assert out["code"] == "local_file_exists"
        assert dest.read_bytes() == b"ancien"

    def test_overwrite_remplace_le_fichier(self, tmp_path):
        dest = tmp_path / "workspace" / "a.zip"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"ancien")
        with _Coffre({ZIP_URL: ZIP}) as coffre:
            out = _run(
                coffre.adapter().tool_vault_read(
                    "c", {"path": "archives/export.zip", "local_path": "a.zip", "overwrite": True}
                )
            )
        assert out["status"] == "downloaded"
        assert dest.read_bytes() == ZIP

    @pytest.mark.parametrize("path", ["../autre/a.zip", "/abs.zip", ""])
    def test_un_chemin_de_coffre_invalide_est_refuse_sans_appel(self, path):
        with _Coffre({}) as coffre:
            out = _run(coffre.adapter().tool_vault_read("c", {"path": path}))
            assert coffre.requests == []
        assert out["code"] == "invalid_request"

    def test_une_destination_hors_racine_est_refusee_sans_appel(self):
        with _Coffre({ZIP_URL: ZIP}) as coffre:
            out = _run(
                coffre.adapter().tool_vault_read(
                    "c", {"path": "archives/export.zip", "local_path": "/etc/cron.d/x"}
                )
            )
            assert coffre.requests == []
        assert out["code"] == "local_path_forbidden"


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
    def test_le_canal_vient_de_la_session(self, tmp_path):
        url = "/api/agent/vault/projet-oxboarding/a.zip"
        with _Coffre({url: ZIP}) as coffre, _WsLoop() as ws_loop:
            adapter = coffre.adapter()
            adapter._loop = ws_loop
            adapter.is_connected = True
            # Un `channel` glisse par le modele est ignore : seul le contexte compte.
            out = _run(adapter_module._pulse_vault_read({"path": "a.zip", "channel": "autre"}))
            adapter.is_connected = False
        assert out["status"] == "downloaded"
        assert [r["path"] for r in coffre.requests] == [url]

    def test_refuse_sans_conversation(self):
        SESSION.pop("HERMES_SESSION_CHAT_ID")
        try:
            out = _run(adapter_module._pulse_vault_read({"path": "a.zip"}))
        finally:
            SESSION["HERMES_SESSION_CHAT_ID"] = "projet-oxboarding"
        assert out["code"] == "no_channel"

    def test_refuse_sans_adaptateur_connecte(self):
        out = _run(adapter_module._pulse_vault_read({"path": "a.zip"}))
        assert out["code"] == "not_connected"
        assert "recuperer" in out["next"]


class TestEnregistrement:
    class _Ctx:
        def __init__(self):
            self.tools = {}
            self.platform = None

        def register_platform(self, **kw):
            self.platform = kw

        def register_tool(self, **kw):
            self.tools[kw["name"]] = kw
            return object()

        def register_skill(self, *a, **kw):
            pass

    def test_loutil_est_enregistre_et_nomme_par_le_hint(self):
        ctx = self._Ctx()
        adapter_module.register(ctx)
        tool = ctx.tools["pulse_vault_read"]
        assert tool["toolset"] == "pulse_chat"
        assert tool["is_async"] is True
        assert "pulse_vault_read" in ctx.platform["platform_hint"]
