# -*- coding: utf-8 -*-
"""Publication d'artifacts — partie PURE du transport.

Un artifact est un contenu riche que l'agent publie DANS la conversation :
diagramme, document, page. Il s'affiche dans un panneau lateral cote app.

Miroir de app/pulse-chat/shared/artifacts.ts. L'app refiltre tout de son cote
(elle ne fait pas confiance au plugin) : filtrer ici evite juste un
aller-retour pour un payload qui serait refuse.

    plugin -> app   POST /api/agent/messages
        {kind: "artifact", channelSlug, artifactId, artifactKind, title,
         content}

Republier le MEME ``artifactId`` cree une nouvelle version (v2, v3...) plutot
qu'une nouvelle carte : c'est ainsi que l'agent itere sur un diagramme sans
polluer le fil.
"""

import hashlib
from typing import Any, Dict, Optional

#: Types reconnus par l'app.
ARTIFACT_KINDS = ("mermaid", "markdown", "svg", "html", "drawio")

#: Bornes miroir de shared/artifacts.ts.
MAX_ARTIFACT_CONTENT_LENGTH = 400_000
MAX_ARTIFACT_TITLE_LENGTH = 120


def is_artifact_kind(value: Any) -> bool:
    return value in ARTIFACT_KINDS


def normalize_title(raw: Any) -> Optional[str]:
    """Une seule ligne, longueur bornee ; vide -> None."""
    if not isinstance(raw, str):
        return None
    cleaned = " ".join(raw.split())
    if not cleaned:
        return None
    return cleaned[:MAX_ARTIFACT_TITLE_LENGTH]


def default_artifact_id(kind: str, title: Any) -> Optional[str]:
    """Identifiant STABLE derive du (type, titre) — ou ``None`` sans titre.

    C'est le correctif d'un piege d'ergonomie : avec un id aleatoire par
    defaut, un agent qui republie « le meme » diagramme sans penser a fournir
    un id creait une carte par iteration et noyait le fil. En derivant l'id du
    titre, le comportement par defaut devient celui qu'on attend — republier
    « Architecture reseau » met a jour la carte existante en v2, v3...

    Sans titre, l'artifact n'a aucune identite stable : on renvoie ``None`` et
    l'appelant genere un id unique (une carte par publication, seule lecture
    sensible dans ce cas).

    Le canal n'entre pas dans le calcul : la cle d'unicite cote app est
    (canal, artifactId), deux canaux ne peuvent donc pas se collisionner.
    """
    normalized = normalize_title(title)
    if not normalized:
        return None
    seed = "%s|%s" % (kind, normalized.casefold())
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    return "art-%s-%s" % (kind, digest)


def build_artifact_payload(
    channel_slug: str,
    artifact_id: str,
    kind: str,
    content: str,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    """Construit le POST sortant. PURE — aucune I/O.

    Leve ``ValueError`` sur un type inconnu ou un contenu hors bornes : mieux
    vaut echouer ici, cote agent, qu'obtenir un 400 opaque apres un aller-retour.
    """
    if not is_artifact_kind(kind):
        raise ValueError(
            "type d'artifact inconnu: %r (attendu: %s)" % (kind, ", ".join(ARTIFACT_KINDS))
        )
    if not isinstance(content, str) or not content:
        raise ValueError("contenu d'artifact vide")
    if len(content) > MAX_ARTIFACT_CONTENT_LENGTH:
        raise ValueError(
            "contenu d'artifact trop volumineux (%d > %d)"
            % (len(content), MAX_ARTIFACT_CONTENT_LENGTH)
        )
    if not artifact_id:
        raise ValueError("artifactId requis")

    return {
        "channelSlug": channel_slug,
        "kind": "artifact",
        "artifactId": artifact_id,
        "artifactKind": kind,
        "title": normalize_title(title),
        "content": content,
    }
