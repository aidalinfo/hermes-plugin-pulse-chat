# -*- coding: utf-8 -*-
"""Outils de coffre et de publication offerts au MODELE — partie PURE.

Pourquoi ce module existe : ``vault_write`` et ``publish_artifact`` sont des
methodes de l'ADAPTATEUR. Le modele ne voit pas les methodes d'un adaptateur,
il ne voit que des outils — et aucun ne les appelait. Un agent pouvait donc
fabriquer un PDF sans aucun moyen de le poser dans la conversation, et chercher
en vain un « point d'entree » que la documentation lui promettait (« le coffre
passe par ton adaptateur »). Le support ``kind: "file"`` de la 1.9.1 publiait
dans une methode que rien ne declenchait.

Deux outils, enregistres par ``register`` a cote de ``pulse_request_approval`` :

    pulse_vault_write(path, local_path | content)
        -> PUT /api/agent/vault/<canal>/<path>          (octets EN FLUX)
    pulse_publish_artifact(kind, title, path | content, artifact_id?)
        -> [PUT du contenu texte] puis POST /api/agent/messages {kind: "artifact"}

Trois choix qui ne se devinent pas :

- **``local_path``, jamais des octets en base64.** Le plugin tourne dans le
  process de la passerelle, la ou l'agent a genere son fichier : il le lit sur
  le disque et l'envoie en flux. Un binaire en base64 dans un argument d'outil
  sature la fenetre de contexte sans rien apprendre au modele — c'est la raison
  pour laquelle ces outils vivent ICI et non dans le MCP de l'app.
- **Le canal n'est pas un parametre** (meme regle que ``pulse_request_approval``) :
  il est lu dans ``gateway.session_context``. Le modele ne peut pas ecrire dans
  le coffre d'une conversation ou il n'est pas.
- **Aucune liste noire de fichiers locaux.** Lire ``~/.hermes/.env`` et le
  deposer au coffre est possible — mais l'agent a deja un terminal qui l'affiche
  en une commande. Une liste noire ne fermerait rien et donnerait l'illusion du
  contraire ; la frontiere reelle est la configuration du terminal de l'agent.

Un troisieme outil fait le chemin inverse (issue #47) :

    pulse_vault_read(path, local_path?, overwrite?)
        -> GET /api/agent/vault/<canal>/<path>          (octets EN FLUX vers le disque)

Le MCP ``channel_vault_read`` ne rend d'un binaire que ses metadonnees : sans
cet outil, restaurer une archive du coffre demandait d'appeler l'API a la main.
La, c'est le plugin qui ECRIT sur le disque de la passerelle — d'ou une regle
inverse de celle de l'ecriture au coffre : la destination est BORNEE a des
racines de travail (``VAULT_READ_ROOTS``), sans ``..`` ni lien symbolique, et
n'ecrase jamais un fichier sans ``overwrite``. Le fichier arrive dans un
``.part`` puis prend son nom d'un seul geste : aucun fichier final n'existe
apres un refus HTTP, un depassement ou un transfert coupe.

Ce module ne fait pas d'I/O reseau : il valide, resout le fichier local et
compose ce que les outils rendent au modele. L'app reste la seule autorite
(chemin, quota, plafond, canal archive) : ses refus reviennent au modele avec
leur message et un conseil.
"""

import errno
import hashlib
import json
import mimetypes
import os
import secrets
from typing import Any, Dict, Optional, Sequence, Tuple

from .artifacts import MAX_ARTIFACT_CONTENT_LENGTH, PUBLISHABLE_ARTIFACT_KINDS

VAULT_WRITE_TOOL_NAME = "pulse_vault_write"
VAULT_READ_TOOL_NAME = "pulse_vault_read"
PUBLISH_TOOL_NAME = "pulse_publish_artifact"

#: Racines ou ``pulse_vault_read`` a le droit d'ecrire, separees par ``:``.
#: Par defaut, le workspace et le brouillon de l'image Hermes de Pulse.
VAULT_READ_ROOTS_ENV = "PULSE_CHAT_VAULT_READ_ROOTS"
DEFAULT_VAULT_READ_ROOTS = ("/opt/data/workspace", "/opt/data/cache/scratch")
#: Sous-dossier de la PREMIERE racine ou arrive un fichier sans ``local_path``.
VAULT_READ_INBOX = "inbox"
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024

#: Plafond d'un fichier du coffre, MIROIR de l'app (100 Mo, applique pendant le
#: transfert). Verifie ici AVANT d'ouvrir la connexion : sinon on enverrait des
#: dizaines de Mo pour recevoir un 413 a la fin.
MAX_VAULT_FILE_BYTES = 100 * 1024 * 1024

#: Delai d'une ecriture relayee, SOUS le plafond de 300 s que
#: ``model_tools._run_async`` impose a un outil asynchrone. C'est un delai PAR
#: OPERATION de socket (urllib), pas une duree totale : un envoi qui avance ne
#: le declenche pas.
UPLOAD_TIMEOUT_SECONDS = 240.0

VAULT_WRITE_DESCRIPTION = (
    "Ecrit un fichier dans le COFFRE de la conversation Pulse Chat en cours (espace "
    "de travail persistant du canal, versionne : un fichier remplace est archive, "
    "pas perdu). Donne `local_path` pour un fichier que tu as produit sur le disque "
    "(PDF, tableur, image, archive — n'importe quel type, jusqu'a 100 Mo), ou "
    "`content` pour un petit texte. ECRIRE N'AFFICHE RIEN dans la conversation : "
    "pour que les humains voient et telechargent le fichier, appelle ENSUITE "
    "pulse_publish_artifact avec kind='file' et le meme `path`. Le canal est celui "
    "de la conversation en cours : tu ne le passes pas. Le resultat donne "
    "`reference` (`vault:<chemin>`), utilisable comme piece jointe de "
    "pulse_request_approval ou d'un connecteur."
)

VAULT_READ_DESCRIPTION = (
    "Telecharge un fichier du COFFRE de la conversation Pulse Chat en cours vers ton "
    "disque, octet pour octet (ZIP, PDF, tableur, image... jusqu'a 100 Mo), pour le "
    "traiter avec tes outils. Le resultat donne `local_path`, `size`, `sha256` et "
    "`mime` — JAMAIS le contenu : lis ensuite le fichier local. Sans `local_path`, il "
    "arrive sous /opt/data/workspace/inbox/<path>. `local_path` doit rester sous "
    "/opt/data/workspace ou /opt/data/cache/scratch ; un fichier existant n'est "
    "remplace qu'avec `overwrite: true`. Trouve le `path` exact avec l'outil MCP "
    "channel_vault_list. Pour simplement LIRE un petit texte, channel_vault_read "
    "suffit. Le canal est celui de la conversation en cours : tu ne le passes pas."
)

PUBLISH_DESCRIPTION = (
    "Publie une CARTE dans la conversation Pulse Chat en cours, qui pointe vers un "
    "fichier du coffre. `kind='file'` pour un fichier quelconque (PDF, tableur, "
    "image, archive) : il doit avoir ete ecrit AVANT avec pulse_vault_write, et tu "
    "donnes son `path` — la carte se telecharge, et un PDF ou une image s'ouvre en "
    "apercu. `kind='motion'` pour un FILM : une page HTML dont chaque image est "
    "une fonction de `t`, qui expose `window.__duration` (secondes) et "
    "`window.__renderAt(t)`, polices integrees en `data:` (aucune ressource "
    "distante), <= 2 Mo — ecris-le d'abord avec pulse_vault_write (`local_path`), "
    "puis publie avec `path` : il se lit dans la carte. Pour un contenu TEXTE "
    "rendu dans le panneau (`markdown`, `mermaid`, "
    "`svg`, `html`, `drawio`), donne directement `content` : il est ecrit au coffre "
    "et publie en un seul appel. L'identite de la carte est `artifact_id` (par "
    "defaut derive du type et du titre) : republier le meme identifiant cree une "
    "NOUVELLE VERSION de la carte au lieu d'en ajouter une. Le canal est celui de "
    "la conversation en cours : tu ne le passes pas."
)

VAULT_WRITE_SCHEMA: Dict[str, Any] = {
    "name": VAULT_WRITE_TOOL_NAME,
    "description": VAULT_WRITE_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "maxLength": 400,
                "description": (
                    "Chemin RELATIF dans le coffre, extension comprise (ex. "
                    "`artifacts/devis-v4.pdf`, `rapports/2026-09.xlsx`). C'est "
                    "l'extension qui decide du type affiche : garde la vraie."
                ),
            },
            "local_path": {
                "type": "string",
                "description": (
                    "Chemin du fichier a envoyer sur TON disque (absolu de "
                    "preference, ex. `/tmp/devis-v4.pdf`). Exclusif de `content`."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "Contenu TEXTE a ecrire (UTF-8). Pour un binaire, utilise "
                    "`local_path` : un binaire passe en texte arrive corrompu. "
                    "Exclusif de `local_path`."
                ),
            },
        },
        "required": ["path"],
    },
}

VAULT_READ_SCHEMA: Dict[str, Any] = {
    "name": VAULT_READ_TOOL_NAME,
    "description": VAULT_READ_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "maxLength": 400,
                "description": (
                    "Chemin RELATIF du fichier dans le coffre, tel que "
                    "channel_vault_list le rend (ex. `archives/export-2026-09.zip`)."
                ),
            },
            "local_path": {
                "type": "string",
                "description": (
                    "Facultatif : fichier de destination sur TON disque, sous "
                    "/opt/data/workspace ou /opt/data/cache/scratch (un chemin "
                    "relatif part de /opt/data/workspace). Les dossiers manquants "
                    "sont crees. Par defaut : /opt/data/workspace/inbox/<path>."
                ),
            },
            "overwrite": {
                "type": "boolean",
                "description": (
                    "Remplacer le fichier de destination s'il existe deja. Par "
                    "defaut false : un `local_path` existant est refuse, et la "
                    "destination par defaut prend un nom libre (`nom-1.zip`)."
                ),
            },
        },
        "required": ["path"],
    },
}

PUBLISH_SCHEMA: Dict[str, Any] = {
    "name": PUBLISH_TOOL_NAME,
    "description": PUBLISH_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": list(PUBLISHABLE_ARTIFACT_KINDS),
                "description": (
                    "`file` pour un fichier deja ecrit au coffre (PDF, tableur...) ; "
                    "`motion` pour un FILM (page HTML animee par `t`, <= 2 Mo, "
                    "publie par `path` comme `file`) ; sinon le type du contenu "
                    "texte rendu dans le panneau."
                ),
            },
            "title": {
                "type": "string",
                "maxLength": 120,
                "description": "Titre de la carte, en une ligne (ex. « Devis client — V4 »).",
            },
            "path": {
                "type": "string",
                "maxLength": 400,
                "description": (
                    "Chemin du fichier dans le coffre, OBLIGATOIRE pour `kind='file'`, "
                    "RECOMMANDE pour `kind='motion'` (un film volumineux ne passe pas "
                    "par `content`, plafonne a 400 000 caracteres) — le meme que celui "
                    "passe a pulse_vault_write. Exclusif de `content`."
                ),
            },
            "content": {
                "type": "string",
                "maxLength": MAX_ARTIFACT_CONTENT_LENGTH,
                "description": (
                    "Contenu texte (Markdown, source Mermaid, SVG, HTML, XML draw.io, "
                    "ou un petit film HTML). Interdit pour `kind='file'`. Exclusif de "
                    "`path`."
                ),
            },
            "artifact_id": {
                "type": "string",
                "maxLength": 120,
                "description": (
                    "Facultatif : identifiant STABLE de la carte. Reprends le meme "
                    "pour publier une nouvelle version (V2, V3...) de la meme carte."
                ),
            },
        },
        "required": ["kind", "title"],
    },
}


class WorkspaceToolError(Exception):
    """Refus local, avant tout appel reseau. ``code`` est rendu au modele."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def exclusive_source(args: Dict[str, Any], other: str) -> Tuple[Optional[str], Optional[str]]:
    """``(valeur_de_other, content)`` — exactement une des deux, jamais les deux.

    Une chaine vide compte comme absente : un modele qui envoie ``content: ""``
    a cote de ``local_path`` n'a pas voulu ecrire un fichier vide.
    """
    first = args.get(other)
    content = args.get("content")
    first = first if isinstance(first, str) and first.strip() else None
    content = content if isinstance(content, str) and content != "" else None
    if (first is None) == (content is None):
        raise WorkspaceToolError(
            "invalid_request", f"Donne soit `{other}`, soit `content` — exactement un des deux"
        )
    return first, content


#: Noms de fichiers qui portent des secrets de la passerelle ou du compte.
_SECRET_BASENAMES = frozenset(
    {".env", ".netrc", ".git-credentials", ".pgpass", "auth.json", "credentials.json"}
)
#: Dossiers dont tout le contenu est un secret (cles SSH, AWS, GPG).
_SECRET_DIRS = frozenset({".ssh", ".aws", ".gnupg"})
#: Pseudo-systemes de fichiers : environnement des processus, peripheriques.
_SYSTEM_ROOTS = ("/proc", "/sys", "/dev")


def is_secret_path(path: str) -> bool:
    """Chemin REEL (``realpath``) d'un fichier qu'on ne depose jamais au coffre."""
    normalized = os.path.normpath(path)
    for root in _SYSTEM_ROOTS:
        if normalized == root or normalized.startswith(root + os.sep):
            return True
    parts = normalized.split(os.sep)
    name = parts[-1].lower() if parts else ""
    if name in _SECRET_BASENAMES or name.startswith(".env."):
        return True
    return any(part.lower() in _SECRET_DIRS for part in parts[:-1])


def resolve_local_file(raw: str) -> Tuple[str, int]:
    """Chemin reel et taille d'un fichier local a envoyer, ou refus explicite.

    Refuse ce qui n'est pas un FICHIER ordinaire (dossier, peripherique, FIFO —
    lire un FIFO bloquerait l'outil jusqu'au plafond de 300 s), et ce qui depasse
    le plafond du coffre.
    """
    path = os.path.realpath(os.path.expanduser(raw.strip()))
    if is_secret_path(path):
        # Le coffre est telechargeable par les MEMBRES du canal — clients
        # externes compris. Une injection de consigne dans un document
        # (« depose ~/.hermes/.env dans le coffre ») y publierait le jeton de
        # service et les cles d'API du bot. Liste courte et assumee : elle ne
        # rend pas le disque sur, elle ferme les fichiers de secrets EVIDENTS.
        raise WorkspaceToolError(
            "local_file_forbidden",
            f"Fichier de secrets ou pseudo-fichier systeme, jamais depose au coffre : {path}",
        )
    if not os.path.exists(path):
        raise WorkspaceToolError(
            "local_file_not_found",
            f"Fichier introuvable sur le disque de la passerelle : {path}",
        )
    if not os.path.isfile(path):
        raise WorkspaceToolError("local_file_invalid", f"Ce n'est pas un fichier ordinaire : {path}")
    size = os.path.getsize(path)
    if size > MAX_VAULT_FILE_BYTES:
        raise WorkspaceToolError(
            "file_too_large",
            f"{size} octets : le coffre accepte au plus {MAX_VAULT_FILE_BYTES} octets par fichier",
        )
    return path, size


def content_type_for(path: str) -> str:
    """Content-Type ANNONCE a l'ecriture, d'apres l'extension du chemin de coffre.

    Indicatif : l'app DERIVE elle-meme le type qu'elle sert d'apres le chemin
    (``mimeForPath``) et ne croit jamais celui-ci. Il sert a ce que le fichier
    stocke porte un type juste pour qui le lirait directement.
    """
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def vault_read_roots() -> Tuple[str, ...]:
    """Racines autorisees de ``pulse_vault_read``, absolues et normalisees.

    ``PULSE_CHAT_VAULT_READ_ROOTS`` les remplace (installation hors de l'image
    Hermes de Pulse) ; une entree relative y est ignoree, jamais resolue contre
    le repertoire courant de la passerelle.
    """
    raw = os.environ.get(VAULT_READ_ROOTS_ENV, "")
    entries = [e.strip() for e in raw.split(os.pathsep)] if raw.strip() else DEFAULT_VAULT_READ_ROOTS
    roots = tuple(os.path.normpath(e) for e in entries if e and os.path.isabs(e))
    return roots or DEFAULT_VAULT_READ_ROOTS


def _forbidden(message: str) -> WorkspaceToolError:
    return WorkspaceToolError("local_path_forbidden", message)


def _free_name(path: str) -> str:
    """Premier ``nom-<n>.ext`` libre a cote de ``path``."""
    stem, ext = os.path.splitext(path)
    for index in range(1, 1000):
        candidate = f"{stem}-{index}{ext}"
        if not os.path.lexists(candidate):
            return candidate
    raise WorkspaceToolError("local_file_exists", f"Aucun nom libre a cote de {path}")


def resolve_download_target(
    vault_path: str,
    local_path: Optional[str],
    *,
    overwrite: bool = False,
    roots: Optional[Sequence[str]] = None,
) -> str:
    """Chemin final ou deposer un fichier lu au coffre, ou refus explicite.

    ``vault_path`` est deja normalise (``normalize_vault_path``). La destination
    doit tomber sous une racine autorisee ; aucun composant entre la racine et
    le fichier ne peut etre un lien symbolique (un lien vers ``/opt/data/.env``
    ferait ecraser le jeton de service). Les dossiers manquants sont crees un a
    un, sans suivre de lien. Une racine, elle, est une configuration de
    confiance : on la prend telle que ``realpath`` la resout.
    """
    roots = tuple(os.path.normpath(r) for r in (roots or vault_read_roots()))
    default = local_path is None
    if default:
        lexical = os.path.join(roots[0], VAULT_READ_INBOX, *vault_path.split("/"))
    else:
        raw = local_path.strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in raw):
            raise _forbidden("caractere de controle dans local_path")
        if ".." in raw.replace("\\", "/").split("/"):
            raise _forbidden(f"remontee de repertoire refusee : {raw}")
        if raw.startswith("~"):
            raise _forbidden(f"le dossier personnel est hors des racines autorisees : {raw}")
        lexical = os.path.normpath(raw if os.path.isabs(raw) else os.path.join(roots[0], raw))

    root = next((r for r in roots if lexical.startswith(r + os.sep)), None)
    if root is None:
        raise _forbidden(
            f"{lexical} est hors des racines autorisees ({', '.join(roots)})"
        )
    os.makedirs(root, exist_ok=True)
    current = os.path.realpath(root)
    parts = os.path.relpath(lexical, root).split(os.sep)
    for part in parts[:-1]:
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise _forbidden(f"lien symbolique dans le chemin de destination : {current}")
        try:
            os.mkdir(current)
        except FileExistsError:
            # Cree entre-temps : on revérifie ce qui est la, lien compris.
            if os.path.islink(current) or not os.path.isdir(current):
                raise _forbidden(f"{current} n'est pas un dossier") from None

    final = os.path.join(current, parts[-1])
    if os.path.islink(final):
        raise _forbidden(f"la destination est un lien symbolique : {final}")
    if os.path.exists(final):
        if not os.path.isfile(final):
            raise WorkspaceToolError("local_file_invalid", f"Ce n'est pas un fichier ordinaire : {final}")
        if not overwrite:
            if not default:
                raise WorkspaceToolError(
                    "local_file_exists", f"{final} existe deja (overwrite: true pour le remplacer)"
                )
            final = _free_name(final)
    return final


def _commit_part(part: str, final: str, overwrite: bool) -> None:
    """Donne au ``.part`` complet son nom final, d'un seul geste.

    Sans ``overwrite``, ``os.link`` echoue ATOMIQUEMENT si le nom est pris —
    un fichier apparu entre la verification et la fin du transfert n'est donc
    jamais ecrase. Repli sur ``os.replace`` si le systeme de fichiers ne
    connait pas les liens physiques.
    """
    if overwrite:
        if os.path.islink(final):
            raise _forbidden(f"la destination est un lien symbolique : {final}")
        os.replace(part, final)
        return
    try:
        os.link(part, final)
    except FileExistsError:
        raise WorkspaceToolError("local_file_exists", f"{final} existe deja") from None
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK):
            raise
        if os.path.lexists(final):
            raise WorkspaceToolError("local_file_exists", f"{final} existe deja") from None
        os.replace(part, final)
        return
    os.unlink(part)


def stream_to_file(
    source: Any,
    final: str,
    *,
    overwrite: bool = False,
    expected_size: Optional[int] = None,
    limit: int = MAX_VAULT_FILE_BYTES,
) -> Tuple[int, str]:
    """Copie ``source.read`` vers ``final`` via un ``.part`` : ``(taille, sha256)``.

    Le ``.part`` est cree en exclusif sans suivre de lien, voisin du fichier
    final (meme systeme de fichiers, donc renommage atomique). Toute issue
    autre qu'un transfert COMPLET — plafond depasse, taille differente de celle
    annoncee, lecture coupee — le supprime et laisse ``final`` intact.
    """
    directory, name = os.path.split(final)
    part = os.path.join(directory, ".%s.%s.part" % (name, secrets.token_hex(6)))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(part, flags, 0o644)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                block = source.read(_DOWNLOAD_CHUNK_BYTES)
                if not block:
                    break
                size += len(block)
                if size > limit:
                    raise WorkspaceToolError(
                        "file_too_large", f"plus de {limit} octets : transfert interrompu"
                    )
                digest.update(block)
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        if expected_size is not None and size != expected_size:
            raise WorkspaceToolError(
                "incomplete_transfer",
                f"{size} octets recus sur {expected_size} annonces : transfert incomplet",
            )
        _commit_part(part, final, overwrite)
    except BaseException:
        try:
            os.unlink(part)
        except FileNotFoundError:
            pass
        raise
    return size, digest.hexdigest()


def _dump(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def written_result(path: str, size: int) -> str:
    return _dump(
        {
            "status": "written",
            "path": path,
            "size": size,
            "reference": f"vault:{path}",
            "next": (
                "Le fichier est dans le coffre, mais RIEN n'apparait encore dans la "
                "conversation. Pour le montrer, appelle pulse_publish_artifact avec "
                f"kind='file', path='{path}' et un titre."
            ),
        }
    )


def published_result(artifact_id: str, kind: str, path: str) -> str:
    return _dump(
        {
            "status": "published",
            "artifactId": artifact_id,
            "kind": kind,
            "path": path,
            "next": (
                "La carte est visible dans la conversation. Pour une nouvelle version, "
                "reecris le fichier puis republie avec le MEME artifact_id."
            ),
        }
    )


def downloaded_result(path: str, local_path: str, size: int, sha256: str, mime: str) -> str:
    return _dump(
        {
            "status": "downloaded",
            "path": path,
            "local_path": local_path,
            "size": size,
            "sha256": sha256,
            "mime": mime,
            "next": (
                "Le fichier est sur ton disque, intact (sha256 calcule sur les octets "
                f"recus). Travaille directement sur {local_path} avec tes outils."
            ),
        }
    )


def refused_result(code: str, message: str, advice: Optional[Dict[str, str]] = None) -> str:
    table = {**_ADVICE, **(advice or {})}
    return _dump(
        {
            "status": "refused",
            "code": code,
            "message": message,
            "next": table.get(code, table["not_sent"]),
        }
    )


def read_refused(code: str, message: str) -> str:
    """Refus de ``pulse_vault_read`` : memes codes, conseils d'une LECTURE."""
    return refused_result(code, message, _READ_ADVICE)


def read_http_refusal(status: int, body: Any) -> str:
    """``http_refusal`` avec les conseils d'une lecture."""
    return http_refusal(status, body, advice=_READ_ADVICE)


def http_refusal(
    status: int, body: Any, fallback: str = "", advice: Optional[Dict[str, str]] = None
) -> str:
    """Refus HTTP de l'app, rendu avec son MESSAGE (``statusMessage``).

    Le code est derive du statut : ces routes ne portent pas de ``data.code``,
    et « HTTP 409 » seul ne dirait pas au modele s'il doit corriger un chemin ou
    prevenir un humain.
    """
    message = fallback or f"HTTP {status}"
    if isinstance(body, dict):
        text = body.get("statusMessage") or body.get("message")
        if isinstance(text, str) and text.strip():
            message = f"HTTP {status} : {text.strip()}"
    code = _STATUS_CODES.get(status)
    if code is None:
        code = "app_unavailable" if status >= 500 or status == 0 else "not_sent"
    return refused_result(code, message, advice)


_STATUS_CODES = {
    400: "invalid_request",
    401: "not_authorized",
    403: "not_authorized",
    404: "not_found",
    409: "conflict",
    413: "file_too_large",
    415: "unsupported",
    501: "app_unavailable",
    507: "vault_full",
}

_ADVICE = {
    "invalid_request": (
        "Un parametre est invalide (voir `message`) : corrige-le (chemin relatif, "
        "sans `..` ni `/` initial ; type d'artifact ; `path` pour kind='file') et rappelle l'outil."
    ),
    "local_file_not_found": (
        "Le fichier n'existe pas la ou la passerelle Pulse Chat le cherche. Verifie le "
        "chemin (absolu de preference). Si ton terminal tourne dans un bac a sable "
        "separe, son disque n'est pas celui de la passerelle : dis-le a l'humain."
    ),
    "local_file_invalid": "Donne le chemin d'un FICHIER, pas d'un dossier, puis rappelle l'outil.",
    "local_file_forbidden": (
        "Ce fichier porte des secrets (jetons, cles) ou n'est pas un vrai fichier : "
        "il n'est jamais depose au coffre, que tous les membres du canal peuvent "
        "telecharger. Ne reessaie pas ; si un humain l'a demande, explique-lui pourquoi."
    ),
    "file_too_large": (
        "Le fichier depasse le plafond du coffre (100 Mo). Compresse-le ou decoupe-le, "
        "ou dis-le a l'humain."
    ),
    "not_found": (
        "Le fichier ou la conversation est introuvable. Pour kind='file', ecris d'abord "
        "le fichier avec pulse_vault_write, avec exactement le meme `path`."
    ),
    "conflict": (
        "L'app refuse pour l'instant (canal archive, ou ton identite d'agent n'est pas "
        "encore etablie apres une reconnexion). Reessaie une fois dans quelques "
        "secondes ; si le refus persiste, dis-le a l'humain."
    ),
    "unsupported": "Ce type de fichier ne peut pas etre publie ainsi. Dis-le a l'humain.",
    "podcast_not_publishable": (
        "Un podcast se demande avec pulse_podcast (titre + chapitres en prose "
        "parlee) : l'app fait la synthese et publie la carte elle-meme."
    ),
    "vault_full": (
        "Le coffre de ce canal est plein (quota de fichiers). Dis-le a l'humain : "
        "il faut faire de la place."
    ),
    "not_authorized": (
        "Ton agent n'est pas autorise a ecrire dans ce canal. Dis-le a l'humain : "
        "un administrateur doit le rattacher au canal."
    ),
    "no_channel": "Cet outil ne marche que dans une conversation Pulse Chat.",
    "not_connected": (
        "Pulse Chat est injoignable pour l'instant. Dis a l'humain que tu n'as pas pu "
        "deposer le fichier ; ne pretends pas l'avoir fait."
    ),
    "app_unavailable": (
        "L'app Pulse Chat a echoue. Reessaie une fois ; sinon dis a l'humain que le "
        "fichier n'a pas pu etre depose. Ne pretends pas l'avoir fait."
    ),
    "not_sent": (
        "L'operation n'a pas abouti. Dis-le a l'humain ; ne pretends pas avoir depose "
        "ou publie le fichier."
    ),
}

#: Conseils propres a ``pulse_vault_read`` : ceux de l'ecriture parleraient de
#: « deposer » un fichier, et enverraient le modele vers pulse_vault_write.
_READ_ADVICE = {
    "not_found": (
        "Ce fichier n'est pas dans le coffre de cette conversation. Liste le coffre "
        "avec l'outil MCP channel_vault_list et reprends le `path` EXACT rendu."
    ),
    "not_authorized": (
        "Ton agent n'est pas autorise a lire le coffre de ce canal. Dis-le a "
        "l'humain : un administrateur doit le rattacher au canal."
    ),
    "file_too_large": (
        "Le fichier depasse le plafond du coffre (100 Mo) : il n'a pas ete ecrit. "
        "Dis-le a l'humain."
    ),
    "local_path_forbidden": (
        "Destination refusee : choisis un `local_path` sous /opt/data/workspace ou "
        "/opt/data/cache/scratch, sans `..` ni lien symbolique — ou omets-le pour "
        "recevoir le fichier dans /opt/data/workspace/inbox/."
    ),
    "local_file_exists": (
        "Un fichier porte deja ce nom. Choisis un autre `local_path`, ou passe "
        "`overwrite: true` si le remplacer est bien voulu."
    ),
    "local_file_invalid": "La destination est un dossier : donne le chemin d'un FICHIER.",
    "incomplete_transfer": (
        "Le transfert s'est interrompu : AUCUN fichier n'a ete ecrit. Reessaie une "
        "fois ; sinon dis a l'humain que la recuperation a echoue."
    ),
    "not_connected": (
        "Pulse Chat est injoignable pour l'instant. Dis a l'humain que tu n'as pas pu "
        "recuperer le fichier ; ne pretends pas l'avoir fait."
    ),
    "app_unavailable": (
        "L'app Pulse Chat a echoue. Reessaie une fois ; sinon dis a l'humain que le "
        "fichier n'a pas pu etre recupere. Ne pretends pas l'avoir fait."
    ),
    "not_sent": (
        "La recuperation n'a pas abouti et aucun fichier n'a ete ecrit. Dis-le a "
        "l'humain ; ne pretends pas avoir le fichier."
    ),
}
