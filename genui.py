# -*- coding: utf-8 -*-
"""Cartes interactives (``pulse_ui``) — partie PURE (docs/66 de l'app).

L'agent COMPOSE une carte a partir d'un catalogue FERME de composants
(tableau, chiffres cles, graphique, comparatif, formulaire, boutons...) ;
l'app la rend en composants natifs, et ce que la personne soumet revient a
l'agent comme un MESSAGE (repere ``[Interface : ...]``).

L'adaptateur reste mince : il TRANSPORTE. Contrat (app >= 0.91.0) :

    plugin -> app   POST /api/agent/messages
        {channelSlug, kind: "ui_surface", op, surfaceId, text?, document?, patch?}

    2xx  {id, surfaceId, version, status, created?}
    refus h3 {statusMessage, data: {code, issues?, hint?}}

Trois choix qui ne se devinent pas :

- **Aucune validation de fond ici.** Le catalogue vit dans l'app
  (``shared/genui/schema.ts``) et rend des refus LISIBLES PAR LE MODELE
  (chemin dans le document envoye + valeurs permises). Les redire ici ferait
  deux catalogues qui finiraient par diverger. Seule la TAILLE est bornee, pour
  ne pas envoyer 5 Mo pour rien.
- **Le canal n'est pas un parametre** (meme regle que ``pulse_podcast``) : il
  vient de ``gateway.session_context``.
- **Le refus rend ``issues`` TELLES QUELLES** (au plus 20) : c'est ce qui permet
  au modele de corriger sa carte seul, sans demander a l'humain.
"""

import json
import re
from typing import Any, Dict, List

from .gates import parse_error_body

#: Nom de l'outil, PREFIXE (cf. ``PODCAST_TOOL_NAME``).
UI_TOOL_NAME = "pulse_ui"

#: Skill qui porte le catalogue, les regles et les exemples.
UI_SKILL_NAME = "interface"

#: Version minimale de l'app. Documentaire : une app plus ancienne rend 400
#: (``kind`` inconnu), relaye tel quel au modele.
APP_MIN_VERSION = "0.91.0"

#: Bornes MIROIR de ``shared/genui/limits.ts`` — seule la taille est jugee ici.
MAX_DOCUMENT_BYTES = 340 * 1024
MAX_PATCH_OPS = 100
MAX_TEXT_LENGTH = 4000
SURFACE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_ISSUES = 20

UI_OPS = ("render", "patch", "close")

#: Types du catalogue, ANNONCES au modele. Jumeau de ``GENUI_COMPONENT_TYPES``
#: (``shared/genui/schema.ts``) : un type absent ici reste utilisable (l'app
#: fait foi), il n'est simplement pas suggere.
COMPONENT_TYPES = (
    "stack", "grid", "card", "tabs", "accordion", "separator",
    "heading", "text", "badge", "alert", "code", "kv",
    "stat", "table", "list", "timeline", "progress", "comparison", "chart",
    "input", "textarea", "select", "radio", "segmented", "checkbox", "switch", "slider", "date",
    "button", "actions", "link", "copy",
)

UI_TOOL_DESCRIPTION = (
    "Affiche une CARTE INTERACTIVE dans la conversation Pulse Chat en cours : "
    "tableau triable, chiffres cles, graphique, comparatif d'options, frise, "
    "formulaire, simulateur avec formules, boutons. A utiliser quand une carte "
    "sert MIEUX que du texte : comparer plusieurs options sur plusieurs "
    "criteres, montrer des chiffres qui evoluent, faire un calcul avec des "
    "parametres, recueillir 3 champs ou plus. Sinon, reponds en texte (cas par "
    "defaut). JAMAIS pour demander une validation avant d'agir "
    "(pulse_request_approval) ni pour un document a garder (pulse_publish_artifact). "
    "op='render' pose (ou remplace, meme surface_id) la carte : `document` = "
    "{title, description?, root, components: [{id, type, ...}] (liste PLATE, "
    "parents -> `children` par id), data?, computed?, submit?: 'once'|'each'}. "
    "op='patch' la modifie : `patch` = operations JSON Patch sur le document "
    "(ex. {op:'replace', path:'/data/total', value:42}). op='close' la ferme. "
    "Ce que la personne soumet te revient comme un message `[Interface : ...]` "
    "avec les valeurs. Types : " + ", ".join(COMPONENT_TYPES) + ". "
    "CHARGE LE SKILL pulse-chat:interface AVANT ta premiere carte (catalogue, "
    "regles, exemples). Un refus porte `issues` (chemin + valeurs permises) : "
    "corrige et renvoie la carte toi-meme. Le canal est celui de la "
    "conversation : tu ne le passes pas."
)

UI_TOOL_SCHEMA: Dict[str, Any] = {
    "name": UI_TOOL_NAME,
    "description": UI_TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "enum": list(UI_OPS),
                "description": "render (poser/remplacer), patch (modifier), close (fermer).",
            },
            "surface_id": {
                "type": "string",
                "pattern": SURFACE_ID_PATTERN.pattern,
                "description": (
                    "Nom stable de la carte, [a-z0-9-] (ex. 'devis-comparatif'). "
                    "Le meme nom met a jour la meme carte tant qu'elle est ouverte."
                ),
            },
            "text": {
                "type": "string",
                "maxLength": MAX_TEXT_LENGTH,
                "description": (
                    "op=render seulement : 1-2 phrases d'accompagnement dans ta bulle "
                    "(sans paraphraser la carte). Facultatif."
                ),
            },
            "document": {
                "type": "object",
                "description": "op=render : le document complet de la carte (cf. skill pulse-chat:interface).",
            },
            "patch": {
                "type": "array",
                "maxItems": MAX_PATCH_OPS,
                "items": {"type": "object"},
                "description": "op=patch : operations JSON Patch (RFC 6902) sur le document.",
            },
        },
        "required": ["op", "surface_id"],
    },
}


class UiToolError(Exception):
    """Refus LOCAL (avant tout envoi) : ``code`` + message lisible."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def build_ui_payload(channel_slug: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Corps du POST, a partir des arguments du modele. PURE — aucune I/O.

    Ne juge que la FORME de l'appel (op, nom, presence du document ou du patch,
    taille) : le fond est juge par l'app, qui sait dire QUOI corriger.
    """
    args = args if isinstance(args, dict) else {}
    op = args.get("op")
    if op not in UI_OPS:
        raise UiToolError("invalid_request", f"`op` doit valoir {', '.join(UI_OPS)}")
    surface_id = args.get("surface_id")
    if not isinstance(surface_id, str) or not SURFACE_ID_PATTERN.match(surface_id):
        raise UiToolError(
            "invalid_request",
            "`surface_id` : [a-z0-9-], commence par une lettre ou un chiffre, 64 caracteres au plus",
        )
    payload: Dict[str, Any] = {
        "channelSlug": channel_slug,
        "kind": "ui_surface",
        "op": op,
        "surfaceId": surface_id,
    }
    if op == "render":
        document = args.get("document")
        if not isinstance(document, dict):
            raise UiToolError("invalid_request", "op=render exige `document` (un objet)")
        if _size(document) > MAX_DOCUMENT_BYTES:
            raise UiToolError(
                "ui_spec_too_large",
                "Carte trop lourde : resume les donnees ou decoupe en plusieurs cartes",
            )
        payload["document"] = document
        text = args.get("text")
        if isinstance(text, str) and text.strip():
            payload["text"] = text.strip()[:MAX_TEXT_LENGTH]
    elif op == "patch":
        patch = args.get("patch")
        if not isinstance(patch, list) or not patch:
            raise UiToolError("invalid_request", "op=patch exige `patch` (liste d'operations JSON Patch)")
        if len(patch) > MAX_PATCH_OPS:
            raise UiToolError("invalid_request", f"Au plus {MAX_PATCH_OPS} operations par patch")
        if _size(patch) > MAX_DOCUMENT_BYTES:
            raise UiToolError("ui_spec_too_large", "Patch trop lourd : renvoie plutot la carte entiere")
        payload["patch"] = patch
    return payload


def _dump(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


_NEXT_BY_OP = {
    "render": (
        "La carte est visible dans la conversation. Pour la modifier : op=patch (ou "
        "op=render avec le MEME surface_id). La reponse de la personne t'arrivera en "
        "message `[Interface : ...]`. Ne recopie pas la carte en texte."
    ),
    "patch": "La carte est mise a jour dans la conversation.",
    "close": "La carte est close : plus aucune reponse ne peut y etre envoyee.",
}


def success_result(op: str, body: Any) -> str:
    body = body if isinstance(body, dict) else {}
    return _dump(
        {
            "status": {"render": "rendered", "patch": "updated", "close": "closed"}.get(op, "ok"),
            "surface_id": body.get("surfaceId"),
            "version": body.get("version"),
            "next": _NEXT_BY_OP.get(op, ""),
        }
    )


def refused_result(code: str, message: str, issues: List[Dict[str, Any]] = None, hint: str = "") -> str:
    out: Dict[str, Any] = {"status": "refused", "code": code, "message": message}
    if issues:
        out["issues"] = issues[:MAX_ISSUES]
    out["next"] = hint or _ADVICE.get(code, _ADVICE["not_sent"])
    return _dump(out)


def http_refusal(status: int, body: Any) -> str:
    """Refus de l'app : son code, son message, ses ``issues`` et son ``hint``."""
    parsed = parse_error_body(status, body)
    issues: List[Dict[str, Any]] = []
    hint = ""
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        data = body["data"]
        raw = data.get("issues")
        if isinstance(raw, list):
            issues = [i for i in raw if isinstance(i, dict)][:MAX_ISSUES]
        if isinstance(data.get("hint"), str):
            hint = data["hint"]
    code = parsed["code"]
    if code == "not_sent":
        code = {403: "not_authorized", 404: "channel_not_found"}.get(status) or (
            "app_unavailable" if status == 0 or status >= 500 else "not_sent"
        )
    return refused_result(code, parsed["message"], issues, hint)


_ADVICE = {
    "invalid_request": "Un parametre est invalide (voir `message`) : corrige-le et rappelle l'outil.",
    "ui_invalid": (
        "La carte est refusee : corrige CHAQUE `issues[].path` (les valeurs permises "
        "sont dans `hint`), puis renvoie la carte entiere (op=render). Ne demande pas "
        "a l'humain."
    ),
    "ui_invalid_patch": "Le patch est refuse : corrige-le, ou renvoie la carte entiere (op=render).",
    "ui_surface_not_found": "Aucune carte ouverte sous ce nom : pose-la d'abord (op=render).",
    "ui_too_many_surfaces": "Trop de cartes ouvertes : ferme celles qui sont tranchees (op=close).",
    "ui_surface_conflict": "La carte a change entre-temps : renvoie-la entiere (op=render).",
    "ui_spec_too_large": "Carte trop lourde : resume ou decoupe, une carte n'est pas un export.",
    "channel_not_found": (
        "Conversation introuvable pour l'app (ou app trop ancienne pour les cartes, "
        f"< {APP_MIN_VERSION}). Reponds en texte."
    ),
    "not_authorized": "Ton agent ne sert pas cette conversation pour l'app. Reponds en texte.",
    "no_channel": "Cet outil ne marche que dans une conversation Pulse Chat.",
    "not_connected": "Pulse Chat est injoignable : reponds en texte.",
    "app_unavailable": "L'app ne repond pas : reponds en texte, ne reessaie pas en boucle.",
    "not_sent": "La carte n'a pas ete envoyee : reponds en texte.",
}
