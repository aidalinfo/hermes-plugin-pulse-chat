# -*- coding: utf-8 -*-
"""Coffre-fort de l'agent — partie PURE (construction d'URL, validation).

Espace de travail persistant par canal, adosse au S3 de l'app. Le plugin n'a
AUCUNE cle S3 : il passe par l'API, avec le Bearer service token deja en place.
Le serveur impose le prefixe ``workspace/<orgId>/<channelSlug>/`` et normalise
le chemin. Cf. docs/02-architecture.md § « Coffre-fort de l'agent ».

    GET    /api/agent/vault/:channelSlug            -> liste
    GET    /api/agent/vault/:channelSlug/*path      -> lecture
    PUT    /api/agent/vault/:channelSlug/*path      -> ecriture
    DELETE /api/agent/vault/:channelSlug/*path      -> suppression

Ce module ne fait pas d'I/O : il construit les URLs et refuse localement les
chemins que le serveur refuserait de toute facon. Le controle qui FAIT foi
reste celui du serveur — celui-ci n'est qu'un garde-fou de confort, jamais la
frontiere de securite.
"""

from typing import Any, List, Optional
from urllib.parse import quote

MAX_VAULT_PATH_LENGTH = 400
MAX_VAULT_SEGMENT_LENGTH = 120


class VaultPathError(ValueError):
    """Chemin refuse avant meme l'appel reseau."""


def normalize_vault_path(raw: Any) -> str:
    """Valide un chemin relatif et le renvoie normalise.

    Refuse (plutot que de « nettoyer » en silence) : chemin absolu, segment
    ``..``, caracteres de controle, segments vides ou reserves. Un chemin
    reecrit dans le dos de l'agent le ferait ecrire ailleurs que prevu.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise VaultPathError("chemin vide")
    if len(raw) > MAX_VAULT_PATH_LENGTH:
        raise VaultPathError("chemin trop long")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise VaultPathError("caractere de controle dans le chemin")

    unified = raw.replace("\\", "/")
    if unified.startswith("/"):
        raise VaultPathError("chemin absolu refuse")
    if len(unified) > 1 and unified[1] == ":":
        raise VaultPathError("chemin absolu refuse")

    segments: List[str] = [s for s in unified.split("/") if s]
    if not segments:
        raise VaultPathError("chemin vide")
    for segment in segments:
        if segment == "..":
            raise VaultPathError("remontee de repertoire refusee")
        if segment == ".":
            raise VaultPathError("segment reserve")
        if len(segment) > MAX_VAULT_SEGMENT_LENGTH:
            raise VaultPathError("segment trop long")
        if segment != segment.strip():
            raise VaultPathError("espaces en bordure de segment")

    return "/".join(segments)


def vault_url(base_url: str, channel_slug: str, path: Optional[str] = None) -> str:
    """URL d'une opération coffre-fort. ``path`` None ⇒ URL de listing."""
    root = "%s/api/agent/vault/%s" % (base_url.rstrip("/"), quote(channel_slug, safe=""))
    if path is None:
        return root
    normalized = normalize_vault_path(path)
    # `safe="/"` : les separateurs restent des separateurs, le reste est encode.
    return "%s/%s" % (root, quote(normalized, safe="/"))
