# -*- coding: utf-8 -*-
"""Export de la memoire de l'agent vers Pulse Chat (docs/68 de l'app, Data Act).

L'app envoie une trame ``memory.export`` (``exportId``, ``parts``) ; ce module
ASSEMBLE l'archive dans le bot, l'adaptateur la depose sur le stockage objet par
le lien d'ecriture signe que l'app lui rend, puis declare la fin. La memoire ne
transite jamais par l'app : elle ne fait que la trace.

Parties (liste FERMEE, meme ordre que ``MEMORY_EXPORT_PARTS`` de l'app) :

  - ``memory``    : ``$HERMES_HOME/memories/MEMORY.md`` et ``USER.md`` ;
  - ``sessions``  : ``$HERMES_HOME/state.db``, copiee par l'API de SAUVEGARDE de
                    SQLite (``Connection.backup``) — jamais une copie brute du
                    fichier, incoherente sous WAL pendant que la passerelle
                    ecrit ;
  - ``hindsight`` : la (ou les) banque(s) Hindsight du bot, quand
                    ``memory.provider`` vaut ``hindsight`` : configuration,
                    modeles mentaux et directives (``GET …/export``), souvenirs
                    (``memories/list``) et documents avec leur texte d'origine
                    (``documents`` puis ``documents/{id}``), en JSONL.

Invariants :
  - une partie en echec n'empeche pas les autres : le manifeste dit ce qui est
    ``included`` / ``absent`` / ``failed``, avec un code court ;
  - AUCUN contenu de memoire dans un journal, un message d'erreur ou le
    manifeste (le schema de l'app est ``.strict()`` et refuse toute cle de
    contenu) ;
  - fonctions SYNCHRONES (lecture disque, SQLite, HTTP) : l'adaptateur les
    appelle par ``asyncio.to_thread``, jamais sur la boucle du WebSocket.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import tarfile
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

#: Cle de fonction annoncee dans ``capabilities.features`` du ``hello``.
MEMORY_EXPORT_FEATURE = "memory_export"

#: Parties connues — une autre est ignoree.
PARTS = ("memory", "sessions", "hindsight")

#: Codes d'echec que l'app accepte sur ``…/fail`` (liste FERMEE cote app).
FAIL_BUILD = "build_failed"
FAIL_TOO_LARGE = "too_large"
FAIL_UPLOAD = "upload_failed"
FAIL_BUSY = "busy"

#: Plafond de l'archive — celui de l'app (``MEMORY_EXPORT_MAX_BYTES``).
MAX_ARCHIVE_BYTES = 2_000_000_000
#: Type impose au depot ET au telechargement.
CONTENT_TYPE = "application/gzip"

#: Taille de page des listes Hindsight, et plafond d'elements par liste : une
#: banque demesuree ne doit pas faire tourner l'export indefiniment.
HINDSIGHT_PAGE = 200
HINDSIGHT_MAX_ITEMS = 200_000
#: Plafond de banques exportees (gabarit ``bank_id_template`` + banques
#: additionnelles).
HINDSIGHT_MAX_BANKS = 50

_MEMORY_FILES = ("MEMORY.md", "USER.md")
_REASON = re.compile(r"[^a-z0-9_.-]")

#: ``fetch_json(url, headers) -> (statut, corps JSON ou None)`` — injecte par
#: l'adaptateur (ouvreur garde du plugin), remplace dans les tests.
FetchJson = Callable[[str, Mapping[str, str]], Tuple[int, Any]]


class PartError(Exception):
    """Echec d'UNE partie — porte un code court, jamais de contenu."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def memory_export_available() -> bool:
    """Hermes est-il la ? Decide l'annonce du ``hello``.

    L'export lit le HERMES_HOME par ``hermes_constants.get_hermes_home`` : sans
    lui (hors d'Hermes), la capacite n'est PAS annoncee — meme regle que les
    identifiants, et le ``hello`` garde sa forme sans fiche.
    """
    try:
        import hermes_constants  # noqa: F401
    except Exception:
        return False
    return True


def reason_code(value: str) -> str:
    """Code court accepte par l'app (``[a-z0-9_.-]{1,64}``)."""
    code = _REASON.sub("_", (value or "error").lower())[:64]
    return code or "error"


def requested_parts(raw: Any) -> List[str]:
    """Parties demandees par la trame, dans l'ordre canonique, sans inconnues."""
    wanted = {p for p in raw if isinstance(p, str)} if isinstance(raw, list) else set()
    return [p for p in PARTS if p in wanted]


# ── Mémoire intégrée ─────────────────────────────────────────────────────────


def collect_memory(home: Path, staging: Path) -> Dict[str, Any]:
    """``memories/MEMORY.md`` et ``USER.md`` du HERMES_HOME, s'ils existent."""
    source = home / "memories"
    files: List[str] = []
    for name in _MEMORY_FILES:
        path = source / name
        if path.is_file():
            target = staging / "memory" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
            files.append("memory/%s" % name)
    if not files:
        return {"state": "absent", "reason": "no_memory_files"}
    return {"state": "included", "files": files}


# ── Historique des sessions (SQLite) ─────────────────────────────────────────


def collect_sessions(home: Path, staging: Path) -> Dict[str, Any]:
    """Sauvegarde EN LIGNE de ``state.db`` (coherente meme sous WAL)."""
    source = home / "state.db"
    if not source.is_file():
        return {"state": "absent", "reason": "no_state_db"}
    target = staging / "sessions" / "state.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Lecture seule cote source : l'export ne doit jamais ecrire dans la base
    # vivante de la passerelle.
    src = sqlite3.connect("file:%s?mode=ro" % source.as_posix(), uri=True)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
        count = _count_sessions(src)
    finally:
        src.close()
    report: Dict[str, Any] = {"state": "included", "files": ["sessions/state.db"]}
    if count is not None:
        report["count"] = count
    return report


def _count_sessions(conn: sqlite3.Connection) -> Optional[int]:
    """Nombre de sessions, si la table existe (schema d'Hermes, sinon rien)."""
    try:
        row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row else None


# ── Hindsight ────────────────────────────────────────────────────────────────


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def memory_provider(config: Mapping[str, Any]) -> Optional[str]:
    """``memory.provider`` du ``config.yaml`` d'Hermes, ou ``None``."""
    memory = config.get("memory") if isinstance(config, Mapping) else None
    provider = memory.get("provider") if isinstance(memory, Mapping) else None
    return str(provider).strip().lower() if provider else None


def hindsight_settings(
    home: Path, env: Mapping[str, str], shared_home: Optional[Path] = None
) -> Dict[str, Any]:
    """Reglages de connexion du fournisseur Hindsight d'Hermes.

    Meme ordre que le plugin d'Hermes : ``$HERMES_HOME/hindsight/config.json``,
    puis ``~/.hindsight/config.json`` (partage par les profils), puis les
    variables ``HINDSIGHT_*``. Chaque cle de connexion absente du fichier retombe
    sur sa variable.
    """
    config = _read_json(home / "hindsight" / "config.json")
    if config is None and shared_home is not None:
        config = _read_json(shared_home / ".hindsight" / "config.json")
    from_file = config is not None
    config = config or {}
    mode = str(config.get("mode") or (env.get("HINDSIGHT_MODE") if not from_file else "") or "cloud")
    default_url = "http://localhost:8888" if mode == "local_external" else "https://api.hindsight.vectorize.io"
    api_url = str(config.get("api_url") or env.get("HINDSIGHT_API_URL") or "")
    if not api_url and mode != "local_embedded":
        api_url = default_url
    api_key = config.get("api_key") or config.get("apiKey") or env.get("HINDSIGHT_API_KEY") or None
    bank_id = str(config.get("bank_id") or env.get("HINDSIGHT_BANK_ID") or "hermes")
    template = config.get("bank_id_template")
    extra: List[str] = []
    for key in ("additional_banks", "recall_additional_banks"):
        value = config.get(key)
        if isinstance(value, list):
            extra.extend(str(v) for v in value if v)
        elif isinstance(value, str) and value.strip():
            extra.extend(v.strip() for v in value.split(",") if v.strip())
    return {
        "mode": mode,
        "api_url": api_url.rstrip("/"),
        "api_key": str(api_key) if api_key else None,
        "bank_id": bank_id,
        "bank_id_template": str(template) if template else None,
        "extra_banks": extra,
    }


def template_pattern(template: str, profile: str) -> "re.Pattern[str]":
    """Gabarit ``bank_id_template`` -> motif : ``{profile}`` vaut le profil, les
    autres jokers (``{user}``, ``{session}``…) n'importe quel segment."""
    parts = re.split(r"(\{[a-z_]+\})", template)
    regex = ""
    for part in parts:
        if part == "{profile}":
            regex += re.escape(profile)
        elif re.fullmatch(r"\{[a-z_]+\}", part or ""):
            regex += ".+"
        else:
            regex += re.escape(part)
    return re.compile("^%s$" % regex)


def _bank_path(api_url: str, bank: str, suffix: str = "") -> str:
    return "%s/v1/default/banks/%s%s" % (api_url, urllib.parse.quote(bank, safe=""), suffix)


def _paged(
    fetch: FetchJson, url: str, headers: Mapping[str, str], key: str = "items"
) -> Iterator[Any]:
    """Toutes les pages d'une liste Hindsight (``items`` — ``banks`` pour la
    liste des banques —, ``total``)."""
    offset = 0
    while offset < HINDSIGHT_MAX_ITEMS:
        query = {"limit": HINDSIGHT_PAGE, "offset": offset}
        status, body = fetch("%s?%s" % (url, urllib.parse.urlencode(query)), headers)
        if status != 200 or not isinstance(body, dict):
            raise PartError("hindsight_http_%s" % status)
        items = body.get(key) or []
        for item in items:
            yield item
        offset += len(items)
        total = body.get("total")
        if not items or (isinstance(total, int) and offset >= total):
            return
    raise PartError("hindsight_too_many_items")


def _list_banks(fetch: FetchJson, settings: Mapping[str, Any], headers: Mapping[str, str], profile: str) -> List[str]:
    banks = [settings["bank_id"]] + list(settings["extra_banks"])
    template = settings.get("bank_id_template")
    if template:
        pattern = template_pattern(template, profile)
        for bank in _paged(fetch, "%s/v1/default/banks" % settings["api_url"], headers, "banks"):
            bank_id = bank.get("bank_id") or bank.get("id") if isinstance(bank, dict) else None
            if isinstance(bank_id, str) and pattern.match(bank_id):
                banks.append(bank_id)
    unique: List[str] = []
    for bank in banks:
        if bank and bank not in unique:
            unique.append(bank)
    return unique[:HINDSIGHT_MAX_BANKS]


def _write_jsonl(path: Path, rows: Iterator[Any]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False))
            handle.write("\n")
            count += 1
    return count


def collect_hindsight(
    settings: Mapping[str, Any], staging: Path, fetch: FetchJson, profile: str
) -> Dict[str, Any]:
    """Exporte chaque banque sous ``hindsight/<banque>/``."""
    if not settings.get("api_url"):
        # Mode embarque sans URL connue : le demon local n'expose pas d'adresse
        # stable que le plugin puisse joindre.
        raise PartError("hindsight_embedded_unreachable")
    headers: Dict[str, str] = {"Accept": "application/json"}
    if settings.get("api_key"):
        headers["Authorization"] = "Bearer %s" % settings["api_key"]
    banks = _list_banks(fetch, settings, headers, profile)
    files: List[str] = []
    memories = 0
    exported = 0
    for bank in banks:
        status, template = fetch(_bank_path(settings["api_url"], bank, "/export"), headers)
        if status == 404:
            continue  # banque declaree mais jamais creee
        if status != 200:
            raise PartError("hindsight_http_%s" % status)
        folder = staging / "hindsight" / reason_code(bank)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "bank.json").write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        memories += _write_jsonl(
            folder / "memories.jsonl",
            _paged(fetch, _bank_path(settings["api_url"], bank, "/memories/list"), headers),
        )

        def documents() -> Iterator[Any]:
            for doc in _paged(fetch, _bank_path(settings["api_url"], bank, "/documents"), headers):
                doc_id = doc.get("id") if isinstance(doc, dict) else None
                if not doc_id:
                    yield doc
                    continue
                st, detail = fetch(
                    _bank_path(settings["api_url"], bank, "/documents/%s" % urllib.parse.quote(str(doc_id), safe="")),
                    headers,
                )
                yield detail if st == 200 and isinstance(detail, dict) else doc

        _write_jsonl(folder / "documents.jsonl", documents())
        prefix = "hindsight/%s" % folder.name
        files.extend(["%s/bank.json" % prefix, "%s/memories.jsonl" % prefix, "%s/documents.jsonl" % prefix])
        exported += 1
    if exported == 0:
        return {"state": "absent", "reason": "no_hindsight_bank"}
    return {"state": "included", "files": files, "count": memories}


# ── Assemblage ───────────────────────────────────────────────────────────────


def _guard(part: str, collect: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    """Une partie en echec n'emporte pas les autres — ni son contenu au journal."""
    try:
        return collect()
    except PartError as exc:
        logger.warning("Pulse Chat: export de memoire — partie %s en echec (%s)", part, exc.reason)
        return {"state": "failed", "reason": reason_code(exc.reason)}
    except Exception as exc:
        logger.warning("Pulse Chat: export de memoire — partie %s en echec (%s)", part, type(exc).__name__)
        return {"state": "failed", "reason": reason_code(type(exc).__name__)}


def assemble(
    *,
    home: Path,
    config: Mapping[str, Any],
    env: Mapping[str, str],
    parts: List[str],
    workdir: Path,
    fetch: FetchJson,
    profile: str,
    versions: Mapping[str, Optional[str]],
    shared_home: Optional[Path] = None,
) -> Tuple[Optional[Path], Dict[str, Any]]:
    """Construit ``memoire.tar.gz`` dans ``workdir``. Rend ``(archive, manifeste)``.

    ``archive`` vaut ``None`` quand AUCUNE partie n'a pu etre incluse ni
    constatee absente (tout est en echec) : l'adaptateur declare alors
    ``build_failed``.
    """
    staging = workdir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    provider = memory_provider(config)
    reports: Dict[str, Dict[str, Any]] = {}
    for part in parts:
        if part == "memory":
            reports[part] = _guard(part, lambda: collect_memory(home, staging))
        elif part == "sessions":
            reports[part] = _guard(part, lambda: collect_sessions(home, staging))
        elif part == "hindsight":
            if provider != "hindsight":
                reports[part] = {"state": "absent", "reason": "provider_not_hindsight"}
            else:
                settings = hindsight_settings(home, env, shared_home)
                reports[part] = _guard(part, lambda: collect_hindsight(settings, staging, fetch, profile))
    manifest: Dict[str, Any] = {"parts": reports}
    for key in ("hermesVersion", "pluginVersion"):
        if versions.get(key):
            manifest[key] = str(versions[key])[:64]
    manifest["memoryProvider"] = (provider or "builtin")[:64]

    if reports and all(report["state"] == "failed" for report in reports.values()):
        return None, manifest

    (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    archive = workdir / "memoire.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for entry in sorted(staging.iterdir()):
            tar.add(entry, arcname=entry.name)
    return archive, manifest


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ── Côté Hermes (imports internes) ───────────────────────────────────────────


@contextlib.contextmanager
def hermes_context(profile: str) -> Iterator[Tuple[Path, Dict[str, Any]]]:
    """``(HERMES_HOME, config)`` du profil — meme scope que la fiche de capacites."""
    from .capabilities import _profile_scope

    with _profile_scope(profile or ""):
        from hermes_constants import get_hermes_home

        try:
            from hermes_cli.config import load_config
        except ImportError:  # pragma: no cover - depend de la version d'Hermes
            from hermes_cli.skills_config import load_config  # type: ignore[no-redef]
        config = load_config() or {}
        yield Path(get_hermes_home()), config if isinstance(config, dict) else {}


def profile_env(home: Path) -> Dict[str, str]:
    """Environnement du process complete par le ``.env`` du profil (sans l'ecraser)."""
    env = dict(os.environ)
    try:
        text = (home / ".env").read_text(encoding="utf-8")
    except OSError:
        return env
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        env.setdefault(key, value.strip().strip('"').strip("'"))
    return env
