# -*- coding: utf-8 -*-
"""Politique de reconnexion du plugin Pulse Chat — module PUR (testable seul).

Incident de reference (2026-08-05) : un redeploiement de l'app coupe le WS et
repond 502 pendant quelques secondes ; le gateway Hermes ne retente qu'une
fois, immediatement — tous les agents restaient deconnectes jusqu'a un restart
manuel des bots. L'adaptateur pilote donc lui-meme sa reconnexion, avec le
backoff defini ici.
"""

from __future__ import annotations

import random
from typing import Callable, Optional

# Backoff exponentiel (le delai double a chaque echec) avec plafond : court au
# debut (un deploiement dure quelques secondes), puis une tentative par minute
# au plus, indefiniment — la boucle insiste sans marteler.
_BASE_DELAY = 2.0
_DELAY_CAP = 60.0

# Handshake refuse en 401/403 pendant une reconnexion : jeton invalide OU
# intermediaire (incident 2026-11-11 : bouncer CrowdSec de Traefik, IP de
# sortie des bots bannie 4 h, 403 renvoye avant l'app). On ne sait pas les
# distinguer de facon fiable : on retente avec un backoff plus lent, plafonne a
# 10 min — un handshake par bot toutes les 10 min ne ressemble pas a du brute
# force, et le bot revient au plus 10 min apres la levee du ban.
_AUTH_BASE_DELAY = 30.0
_AUTH_DELAY_CAP = 600.0
_AUTH_STATUSES = (401, 403)


def _exponential(n: int, base: float, cap: float) -> float:
    if n <= 0:
        return 0.0
    # Exposant borne : evite un flottant gigantesque apres des jours d'echecs.
    return min(cap, base * 2 ** min(n - 1, 32))


def reconnect_delay(attempt: int) -> float:
    """Delai nominal (secondes) avant la tentative ``attempt`` (1-based).

    2, 4, 8, 16, 32, puis 60 s. ``attempt <= 0`` vaut 0 (tolerance aux appels
    degeneres).
    """
    return _exponential(attempt, _BASE_DELAY, _DELAY_CAP)


def auth_retry_delay(rejections: int) -> float:
    """Delai nominal (secondes) apres ``rejections`` refus 401/403 consecutifs.

    30 s, 1, 2, 4, 8, puis 10 min. ``rejections <= 0`` vaut 0.
    """
    return _exponential(rejections, _AUTH_BASE_DELAY, _AUTH_DELAY_CAP)


def jittered(delay: float, rng: Callable[[], float] = random.random) -> float:
    """Tire le delai reel dans ``[delay / 2, delay]``.

    Les bots partagent une IP de sortie et sont coupes en meme temps (deploiement,
    ban) : sans gigue, ils se reconnecteraient tous a la meme seconde.
    """
    return delay * (0.5 + 0.5 * rng())


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


def is_auth_rejection(exc: BaseException) -> bool:
    """Le handshake WS a-t-il ete refuse en 401/403 ?"""
    return handshake_status(exc) in _AUTH_STATUSES


def is_retryable_ws_error(exc: BaseException) -> bool:
    """Faut-il retenter (avec le backoff normal) apres cet echec de connexion WS ?

    401/403 = token invalide probable : non-retryable au demarrage (erreur de
    configuration visible tout de suite). En reconnexion, la boucle les retente
    quand meme, plus lentement (``auth_retry_delay``) : le 403 peut venir d'un
    intermediaire. Tout le reste (502 de deploiement, 5xx, erreur reseau sans
    statut) est transitoire.
    """
    return not is_auth_rejection(exc)
