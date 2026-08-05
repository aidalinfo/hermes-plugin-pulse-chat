# -*- coding: utf-8 -*-
"""Politique de reconnexion du plugin Pulse Chat — module PUR (testable seul).

Incident de reference (2026-08-05) : un redeploiement de l'app coupe le WS et
repond 502 pendant quelques secondes ; le gateway Hermes ne retente qu'une
fois, immediatement — tous les agents restaient deconnectes jusqu'a un restart
manuel des bots. L'adaptateur pilote donc lui-meme sa reconnexion, avec le
backoff defini ici.
"""

from __future__ import annotations

from typing import Optional

# Progression volontairement courte au debut (un deploiement dure quelques
# secondes) puis plafonnee : la boucle insiste indefiniment sans marteler.
_DELAYS = (2.0, 5.0, 10.0, 30.0, 60.0)
_DELAY_CAP = 120.0


def reconnect_delay(attempt: int) -> float:
    """Delai (secondes) avant la tentative ``attempt`` (1-based).

    ``attempt <= 0`` vaut 0 (tolerance aux appels degeneres).
    """
    if attempt <= 0:
        return 0.0
    if attempt <= len(_DELAYS):
        return _DELAYS[attempt - 1]
    return _DELAY_CAP


def handshake_status(exc: BaseException) -> Optional[int]:
    """Code HTTP d'un rejet de handshake WS, sinon ``None``.

    Couvre ``websockets`` moderne (``InvalidStatus`` porte ``response.status_code``)
    et legacy (``InvalidStatusCode`` porte ``status_code``) par duck-typing —
    aucune dependance a la librairie ici.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    return None


def is_retryable_ws_error(exc: BaseException) -> bool:
    """Faut-il retenter apres cet echec de connexion WS ?

    401/403 = token de service invalide : retenter ne changera rien, on
    remonte au gateway. Tout le reste (502 de deploiement, 5xx, erreur
    reseau sans statut) est transitoire.
    """
    return handshake_status(exc) not in (401, 403)
