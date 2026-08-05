# -*- coding: utf-8 -*-
"""Frame ``hello`` du pont WebSocket Pulse Chat — module PUR (0 dependance
hermes, teste pytest sans hermes installe).

Apres l'auth du WS, le plugin annonce les profils Hermes qu'il sert, son nom
d'affichage et (facultatif) sa fiche de capacites :
``{"type": "hello", "profiles": [...], "agentName": "...", "capabilities": {...}}``.
Le serveur enregistre le peer pour CES profils (last-wins par profil) et
rejoue les messages non livres de ces profils uniquement.

``capabilities`` est recolte par un module impur (``capabilities.py``, cote
adapter) : ce module reste PUR, il ne le recolte jamais lui-meme — il se
contente d'inserer (ou non) la valeur recue en parametre. Cle ABSENTE de la
trame quand ``capabilities`` vaut ``None`` (pas de ``"capabilities": null``
inutile sur le fil).
"""

from typing import Any, Dict, Iterable, List, Optional, Union

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
    capabilities: Optional[Dict[str, Any]] = None,
) -> dict:
    """Construit la frame hello ; agent_name vide => nom du premier profil.

    ``capabilities`` est ajoute a la trame seulement s'il n'est pas ``None`` —
    ni recolte ni interprete ici (module pur), simplement transporte.
    """
    profiles = parse_profiles(raw_profiles)
    name = (agent_name or "").strip() or profiles[0]
    frame: Dict[str, Any] = {"type": "hello", "profiles": profiles, "agentName": name}
    if capabilities is not None:
        frame["capabilities"] = capabilities
    return frame
