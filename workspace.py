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

Ce module ne fait pas d'I/O reseau : il valide, resout le fichier local et
compose ce que les outils rendent au modele. L'app reste la seule autorite
(chemin, quota, plafond, canal archive) : ses refus reviennent au modele avec
leur message et un conseil.
"""

import json
import mimetypes
import os
from typing import Any, Dict, Optional, Tuple

from .artifacts import ARTIFACT_KINDS, MAX_ARTIFACT_CONTENT_LENGTH

VAULT_WRITE_TOOL_NAME = "pulse_vault_write"
PUBLISH_TOOL_NAME = "pulse_publish_artifact"

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

PUBLISH_DESCRIPTION = (
    "Publie une CARTE dans la conversation Pulse Chat en cours, qui pointe vers un "
    "fichier du coffre. `kind='file'` pour un fichier quelconque (PDF, tableur, "
    "image, archive) : il doit avoir ete ecrit AVANT avec pulse_vault_write, et tu "
    "donnes son `path` — la carte se telecharge, et un PDF ou une image s'ouvre en "
    "apercu. Pour un contenu TEXTE rendu dans le panneau (`markdown`, `mermaid`, "
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

PUBLISH_SCHEMA: Dict[str, Any] = {
    "name": PUBLISH_TOOL_NAME,
    "description": PUBLISH_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": list(ARTIFACT_KINDS),
                "description": (
                    "`file` pour un fichier deja ecrit au coffre (PDF, tableur...) ; "
                    "sinon le type du contenu texte rendu dans le panneau."
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
                    "Chemin du fichier dans le coffre, OBLIGATOIRE pour `kind='file'` "
                    "(le meme que celui passe a pulse_vault_write). Exclusif de `content`."
                ),
            },
            "content": {
                "type": "string",
                "maxLength": MAX_ARTIFACT_CONTENT_LENGTH,
                "description": (
                    "Contenu texte (Markdown, source Mermaid, SVG, HTML, XML draw.io). "
                    "Interdit pour `kind='file'`. Exclusif de `path`."
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


def resolve_local_file(raw: str) -> Tuple[str, int]:
    """Chemin reel et taille d'un fichier local a envoyer, ou refus explicite.

    Refuse ce qui n'est pas un FICHIER ordinaire (dossier, peripherique, FIFO —
    lire un FIFO bloquerait l'outil jusqu'au plafond de 300 s), et ce qui depasse
    le plafond du coffre.
    """
    path = os.path.realpath(os.path.expanduser(raw.strip()))
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


def refused_result(code: str, message: str) -> str:
    return _dump(
        {
            "status": "refused",
            "code": code,
            "message": message,
            "next": _ADVICE.get(code, _ADVICE["not_sent"]),
        }
    )


def http_refusal(status: int, body: Any, fallback: str = "") -> str:
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
    return refused_result(code, message)


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
