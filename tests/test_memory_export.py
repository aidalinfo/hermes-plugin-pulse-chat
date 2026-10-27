# -*- coding: utf-8 -*-
"""Export de la memoire de l'agent (``memory_export.py``, docs/68 de l'app).

S'executent SANS hermes installe : le HERMES_HOME est un dossier temporaire,
Hindsight un faux client HTTP qui reproduit l'API (``/v1/default/banks…``).
Ce qui est verifie :

  - memoire integree, ``state.db`` sauvegardee de facon COHERENTE sous WAL ;
  - reglages Hindsight dans l'ordre d'Hermes (fichier du profil, partage, env) ;
  - banques du gabarit, pagination, banque inexistante ignoree, Bearer ;
  - une partie en echec n'emporte pas les autres, et le manifeste ne porte que
    les cles que le schema ``.strict()`` de l'app accepte ;
  - l'adaptateur : un seul export a la fois, lien signe SANS l'``Authorization``
    de l'app, ``complete`` apres le depot, ``fail`` avec un code ferme.
"""

import asyncio
import importlib
import json
import logging
import sqlite3
import tarfile
from pathlib import Path

from test_adapter_dedup import _load_adapter_module

adapter_module = _load_adapter_module()
memory_export = importlib.import_module("pulse_chat_plugin_under_test.memory_export")

SECRET_MEMORY = "Le client prefere les devis en PDF — sentinelle"
PART_KEYS = {"state", "files", "count", "reason"}


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "hermes"
    (home / "memories").mkdir(parents=True)
    (home / "memories" / "MEMORY.md").write_text(SECRET_MEMORY, encoding="utf-8")
    (home / "memories" / "USER.md").write_text("Prenom : Alex", encoding="utf-8")
    return home


def _state_db(home: Path, sessions: int = 3) -> sqlite3.Connection:
    """Base EN WAL, connexion laissee OUVERTE (la passerelle ecrit encore)."""
    conn = sqlite3.connect(str(home / "state.db"))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO sessions VALUES (?)", [("s%d" % i,) for i in range(sessions)])
    conn.commit()
    return conn


class _Hindsight:
    """Faux Hindsight : banques, souvenirs pagines, documents."""

    def __init__(self, banks):
        self.banks = banks  # {bank_id: {"memories": [...], "documents": [...]}}
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        path, _, query = url.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if p)
        limit, offset = int(params.get("limit", 100)), int(params.get("offset", 0))
        rest = path.split("/v1/default/banks", 1)[1]
        if rest == "":
            items = [{"bank_id": b} for b in self.banks]
            return 200, {"banks": items[offset : offset + limit], "total": len(items)}
        bank, _, sub = rest.lstrip("/").partition("/")
        if bank not in self.banks:
            return 404, None
        data = self.banks[bank]
        if sub == "export":
            return 200, {"version": "1", "mental_models": [], "directives": []}
        if sub == "memories/list":
            rows = data["memories"]
            return 200, {"items": rows[offset : offset + limit], "total": len(rows), "limit": limit, "offset": offset}
        if sub == "documents":
            rows = [{"id": d["id"]} for d in data["documents"]]
            return 200, {"items": rows[offset : offset + limit], "total": len(rows)}
        if sub.startswith("documents/"):
            doc_id = sub.split("/", 1)[1]
            return 200, next(d for d in data["documents"] if d["id"] == doc_id)
        return 404, None


# ── Parties ──────────────────────────────────────────────────────────────────


def test_memoire_integree(tmp_path):
    home = _home(tmp_path)
    staging = tmp_path / "staging"
    report = memory_export.collect_memory(home, staging)
    assert report == {"state": "included", "files": ["memory/MEMORY.md", "memory/USER.md"]}
    assert (staging / "memory" / "MEMORY.md").read_text(encoding="utf-8") == SECRET_MEMORY


def test_memoire_absente(tmp_path):
    report = memory_export.collect_memory(tmp_path, tmp_path / "staging")
    assert report == {"state": "absent", "reason": "no_memory_files"}


def test_state_db_sauvegardee_coherente_sous_wal(tmp_path):
    home = _home(tmp_path)
    live = _state_db(home, sessions=3)
    # Ecriture encore dans le WAL (pas de checkpoint) : une copie brute du
    # fichier la perdrait, la sauvegarde SQLite la garde.
    live.execute("INSERT INTO sessions VALUES ('s-wal')")
    live.commit()
    staging = tmp_path / "staging"
    report = memory_export.collect_sessions(home, staging)
    live.close()
    assert report == {"state": "included", "files": ["sessions/state.db"], "count": 4}
    copy = sqlite3.connect(str(staging / "sessions" / "state.db"))
    assert copy.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 4
    copy.close()


def test_state_db_absente(tmp_path):
    assert memory_export.collect_sessions(tmp_path, tmp_path / "s") == {"state": "absent", "reason": "no_state_db"}


# ── Hindsight ────────────────────────────────────────────────────────────────


def test_reglages_hindsight_fichier_du_profil_puis_partage_puis_env(tmp_path):
    home = tmp_path / "hermes"
    shared = tmp_path / "user"
    env = {"HINDSIGHT_API_KEY": "env-key", "HINDSIGHT_BANK_ID": "env-bank", "HINDSIGHT_MODE": "local_external"}

    # Rien : variables d'environnement (mode compris, faute de fichier).
    s = memory_export.hindsight_settings(home, env, shared)
    assert (s["mode"], s["api_url"], s["api_key"], s["bank_id"]) == ("local_external", "http://localhost:8888", "env-key", "env-bank")

    # Fichier partage : il gagne, le mode de l'env ne s'applique plus.
    (shared / ".hindsight").mkdir(parents=True)
    (shared / ".hindsight" / "config.json").write_text(json.dumps({"bank_id": "partage"}), encoding="utf-8")
    s = memory_export.hindsight_settings(home, env, shared)
    assert (s["mode"], s["api_url"], s["bank_id"], s["api_key"]) == ("cloud", "https://api.hindsight.vectorize.io", "partage", "env-key")

    # Fichier du profil : il gagne sur le partage.
    (home / "hindsight").mkdir(parents=True)
    (home / "hindsight" / "config.json").write_text(
        json.dumps({"mode": "local_external", "api_url": "http://localhost:9999/", "bank_id_template": "hermes-{profile}", "additional_banks": ["equipe"]}),
        encoding="utf-8",
    )
    s = memory_export.hindsight_settings(home, env, shared)
    assert s["api_url"] == "http://localhost:9999"
    assert s["bank_id_template"] == "hermes-{profile}"
    assert s["extra_banks"] == ["equipe"]


def test_mode_embarque_sans_url_est_un_echec_nomme(tmp_path):
    home = tmp_path / "hermes"
    (home / "hindsight").mkdir(parents=True)
    (home / "hindsight" / "config.json").write_text(json.dumps({"mode": "local_embedded"}), encoding="utf-8")
    settings = memory_export.hindsight_settings(home, {}, None)
    try:
        memory_export.collect_hindsight(settings, tmp_path / "s", _Hindsight({}), "bot")
    except memory_export.PartError as exc:
        assert exc.reason == "hindsight_embedded_unreachable"
    else:  # pragma: no cover
        raise AssertionError("echec attendu")


def test_gabarit_de_banque():
    pattern = memory_export.template_pattern("hermes-{profile}-{user}", "bot")
    assert pattern.match("hermes-bot-alex")
    assert not pattern.match("hermes-autre-alex")
    assert not pattern.match("hermes-bot-")


def test_banques_paginees_ignore_l_inexistante_et_porte_le_bearer(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_export, "HINDSIGHT_PAGE", 2)
    fake = _Hindsight(
        {
            "hermes": {"memories": [{"id": "m%d" % i, "text": SECRET_MEMORY} for i in range(5)], "documents": [{"id": "d1", "original_text": "texte"}]},
            "hermes-bot-alex": {"memories": [{"id": "x"}], "documents": []},
            "hermes-autre-alex": {"memories": [{"id": "y"}], "documents": []},
        }
    )
    settings = {
        "mode": "cloud",
        "api_url": "https://hs.test",
        "api_key": "cle",
        "bank_id": "hermes",
        "bank_id_template": "hermes-{profile}-{user}",
        "extra_banks": ["jamais-creee"],
    }
    staging = tmp_path / "s"
    report = memory_export.collect_hindsight(settings, staging, fake, "bot")
    assert report["state"] == "included"
    assert report["count"] == 6
    assert "hindsight/hermes/memories.jsonl" in report["files"]
    assert "hindsight/hermes-bot-alex/bank.json" in report["files"]
    assert not any("hermes-autre-alex" in f for f in report["files"])
    lines = (staging / "hindsight" / "hermes" / "memories.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    doc = json.loads((staging / "hindsight" / "hermes" / "documents.jsonl").read_text(encoding="utf-8"))
    assert doc["original_text"] == "texte"
    assert all(headers.get("Authorization") == "Bearer cle" for _, headers in fake.calls)


def test_banque_en_erreur_fait_echouer_la_partie(tmp_path):
    def broken(url, headers):
        return 500, None

    settings = {"mode": "cloud", "api_url": "https://hs.test", "api_key": None, "bank_id": "hermes", "bank_id_template": None, "extra_banks": []}
    try:
        memory_export.collect_hindsight(settings, tmp_path / "s", broken, "bot")
    except memory_export.PartError as exc:
        assert exc.reason == "hindsight_http_500"
    else:  # pragma: no cover
        raise AssertionError("echec attendu")


# ── Assemblage ───────────────────────────────────────────────────────────────


def _assemble(tmp_path, home, config, parts, fetch=None):
    return memory_export.assemble(
        home=home,
        config=config,
        env={},
        parts=parts,
        workdir=tmp_path / "work",
        fetch=fetch or _Hindsight({}),
        profile="bot",
        versions={"hermesVersion": "v2026.10.1", "pluginVersion": "1.24.0"},
    )


def test_archive_et_manifeste_au_schema_de_l_app(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    home = _home(tmp_path)
    _state_db(home).close()
    archive, manifest = _assemble(tmp_path, home, {}, ["memory", "sessions", "hindsight"])
    assert archive is not None
    assert set(manifest) <= {"parts", "hermesVersion", "pluginVersion", "memoryProvider"}
    assert manifest["memoryProvider"] == "builtin"
    assert manifest["parts"]["hindsight"] == {"state": "absent", "reason": "provider_not_hindsight"}
    for report in manifest["parts"].values():
        assert set(report) <= PART_KEYS
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    assert {"manifest.json", "memory/MEMORY.md", "memory/USER.md", "sessions/state.db"} <= names
    assert SECRET_MEMORY not in json.dumps(manifest)
    assert SECRET_MEMORY not in caplog.text


def test_une_partie_en_echec_n_emporte_pas_les_autres(tmp_path, monkeypatch):
    home = _home(tmp_path)

    def boom(*_args):
        raise sqlite3.DatabaseError("disque")

    monkeypatch.setattr(memory_export, "collect_sessions", boom)
    archive, manifest = _assemble(tmp_path, home, {}, ["memory", "sessions"])
    assert archive is not None
    assert manifest["parts"]["memory"]["state"] == "included"
    assert manifest["parts"]["sessions"] == {"state": "failed", "reason": "databaseerror"}


def test_tout_en_echec_ne_produit_aucune_archive(tmp_path, monkeypatch):
    def boom(*_args):
        raise OSError("droits")

    monkeypatch.setattr(memory_export, "collect_memory", boom)
    archive, manifest = _assemble(tmp_path, tmp_path, {}, ["memory"])
    assert archive is None
    assert manifest["parts"]["memory"]["state"] == "failed"


def test_parties_demandees_ordre_canonique_sans_inconnues():
    assert memory_export.requested_parts(["hindsight", "soul", "memory", 3]) == ["memory", "hindsight"]
    assert memory_export.requested_parts("memory") == []


# ── Adaptateur ───────────────────────────────────────────────────────────────


class _Config:
    extra = {"url": "http://pulse-chat.test", "token": "token-test", "profile": "bot"}


def _adapter(tmp_path, posts, puts, *, put_status=200, upload_status=200):
    adapter = adapter_module.PulseChatAdapter(_Config())
    home = _home(tmp_path)
    _state_db(home).close()

    def build(profile, parts, workdir):
        return _assemble(tmp_path, home, {}, parts)

    def fake_http(method, url, *, body=None, headers=None, timeout=None):
        payload = json.loads(body.decode("utf-8"))
        posts.append((url, payload, dict(headers or {})))
        if url.endswith("/upload-url"):
            return upload_status, {"url": "https://s3.test/put?sig=1", "expiresIn": 900}
        return 200, {}

    def fake_put(url, archive, size):
        puts.append((url, size))
        return put_status

    adapter._build_memory_archive = build
    adapter._http_detailed = fake_http
    adapter._put_presigned = fake_put
    return adapter


def test_export_nominal_depose_puis_declare_la_fin(tmp_path):
    posts, puts = [], []
    adapter = _adapter(tmp_path, posts, puts)
    asyncio.run(adapter._handle_memory_export({"type": "memory.export", "exportId": "e1", "parts": ["memory", "sessions"]}))
    assert [u.rsplit("/", 1)[1] for u, _, _ in posts] == ["upload-url", "complete"]
    _, upload, _ = posts[0]
    assert upload["contentType"] == "application/gzip"
    _, complete, _ = posts[1]
    assert complete["sizeBytes"] == upload["sizeBytes"] == puts[0][1]
    assert len(complete["sha256"]) == 64
    assert complete["manifest"]["parts"]["sessions"]["count"] == 3
    assert puts[0][0] == "https://s3.test/put?sig=1"
    assert adapter._memory_export_busy is False


def test_depot_refuse_declare_upload_failed(tmp_path):
    posts, puts = [], []
    adapter = _adapter(tmp_path, posts, puts, put_status=403)
    asyncio.run(adapter._handle_memory_export({"exportId": "e2", "parts": ["memory"]}))
    assert posts[-1][0].endswith("/e2/fail")
    assert posts[-1][1] == {"code": "upload_failed"}


def test_trop_gros_pour_l_app(tmp_path):
    posts, puts = [], []
    adapter = _adapter(tmp_path, posts, puts, upload_status=413)
    asyncio.run(adapter._handle_memory_export({"exportId": "e3", "parts": ["memory"]}))
    assert posts[-1][1] == {"code": "too_large"}
    assert puts == []


def test_un_seul_export_a_la_fois(tmp_path):
    posts, puts = [], []
    adapter = _adapter(tmp_path, posts, puts)
    adapter._memory_export_busy = True
    asyncio.run(adapter._handle_memory_export({"exportId": "e4", "parts": ["memory"]}))
    assert posts == [("http://pulse-chat.test/api/agent/memory-exports/e4/fail", {"code": "busy"}, {"Content-Type": "application/json"})]


def test_lien_signe_sans_l_authorization_de_l_app(tmp_path, monkeypatch):
    """Le PUT part vers le stockage objet avec SES en-tetes signes seulement."""
    seen = []

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None):
        seen.append({k.lower(): v for k, v in request.header_items()})
        return _Resp()

    monkeypatch.setattr(adapter_module, "_urlopen", fake_urlopen)
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(b"x" * 10)
    status = adapter_module.PulseChatAdapter._put_presigned("https://s3.test/put", archive, 10)
    assert status == 200
    assert "authorization" not in seen[0]
    assert seen[0]["content-type"] == "application/gzip"
    assert seen[0]["content-length"] == "10"


def test_capacite_annoncee_seulement_dans_hermes(monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    assert memory_export.memory_export_available() is False
    monkeypatch.setitem(sys.modules, "hermes_constants", types.ModuleType("hermes_constants"))
    assert memory_export.memory_export_available() is True
