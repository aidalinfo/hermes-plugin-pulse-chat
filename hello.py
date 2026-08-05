# -*- coding: utf-8 -*-
"""Frame ``hello`` du pont WebSocket Pulse Chat — module PUR (0 dependance
hermes, teste pytest sans hermes installe).

Apres l'auth du WS, le plugin annonce les profils Hermes qu'il sert et son nom
d'affichage : ``{"type": "hello", "profiles": [...], "agentName": "..."}``.
Le serveur enregistre le peer pour CES profils (last-wins par profil) et
rejoue les messages non livres de ces profils uniquement.
"""

from typing import Iterable, List, Optional, Union

DEFAULT_PROFILE = "default"


def parse_profiles(raw: Union[str, Iterable[str], None]) -> List[str]:
    """``"a, b"`` ou liste -> profils nettoyes, dedupliques, ordre conserve.

    Vide / None / uniquement des blancs => ``["default"]``.
    """
    if raw is None:
        items: Iterable[str] = []
    elif isinstance(raw, str):
        items = raw.split(",")
    else:
        items = raw

    profiles: List[str] = []
    for item in items:
        value = str(item).strip()
        if value and value not in profiles:
            profiles.append(value)
    return profiles or [DEFAULT_PROFILE]


def build_hello(
    raw_profiles: Union[str, Iterable[str], None],
    agent_name: Optional[str] = None,
) -> dict:
    """Construit la frame hello ; agent_name vide => nom du premier profil."""
    profiles = parse_profiles(raw_profiles)
    name = (agent_name or "").strip() or profiles[0]
    return {"type": "hello", "profiles": profiles, "agentName": name}
