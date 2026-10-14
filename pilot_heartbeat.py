# -*- coding: utf-8 -*-
"""Battement de vie du worker pour les cartes de PILOTAGE (CDC §7.5).

Module pur : corps de l'appel et lecture de la reponse. La boucle vit dans
l'adaptateur (``PulseChatAdapter._pilot_heartbeat_loop``).

Le plugin ne sait pas quelle carte est pilotee : il bat pour TOUTES les
executions que ses profils detiennent (``{"all": true}``), toutes les 20 s,
tant que le processus vit. Il presente a chaque battement les jetons appris au
precedent : un jeton perime (prise anterieure) est refuse pour SA carte, sans
toucher aux autres. Un battement refuse ne se rejoue pas a l'aveugle : la carte
est oubliee, la reprise est un geste humain.

Une app anterieure a la route repond 404 : la boucle s'arrete, sans bruit — le
plugin fonctionne exactement comme avant. Un bot sans secret d'agent (session
``declared``, jeton de service partage) recoit 403 ``agent_credential_required`` :
l'app reserve le battement aux sessions verifiees, et la boucle s'arrete aussi —
reessayer toutes les 20 s ne changerait rien et journaliserait un refus a chaque
fois.

Le battement est FACULTATIF (app >= 0.67.0) : l'app juge elle-meme la vivacite
d'une execution (connexion WebSocket + progression de l'agent). Il ne fait que
raccourcir le constat d'un worker mort. D'ou un rythme LENT tant que le plugin
ne detient aucune execution (``IDLE_SECONDS``) : sans carte pilotee, battre
toutes les 20 s ne sert a rien.
"""

from typing import Any, Dict, Optional, Tuple

#: Rythme propose par le CDC §7.5 ; l'app le confirme dans sa reponse.
HEARTBEAT_SECONDS = 20
#: Rythme quand AUCUNE execution n'est detenue. Une carte prise entre deux
#: battements vit sous le bail de l'app sans battement (10 min) : la rejoindre
#: une minute plus tard ne la met jamais en peril.
IDLE_SECONDS = 60
#: Borne cote app (``PILOT_HEARTBEAT_MAX_LEASES``) : au-dela, les jetons ne sont pas envoyes.
MAX_TOKENS = 50
HEARTBEAT_PATH = "/api/agent/pilotage/heartbeat"


def heartbeat_url(base_url: str) -> str:
    return base_url.rstrip("/") + HEARTBEAT_PATH


def build_body(tokens: Dict[str, int]) -> Dict[str, Any]:
    """Corps du battement groupe : les jetons connus, bornes."""
    kept = dict(list(tokens.items())[:MAX_TOKENS])
    return {"all": True, "tokens": kept}


def read_response(status: int, body: Any, tokens: Dict[str, int]) -> Tuple[Dict[str, int], Optional[str], float]:
    """``(jetons, arret, intervalle)`` apres un battement.

    - 200 : les jetons deviennent ceux des baux RENDUS ; une carte refusee ou
      absente est oubliee.
    - 404 : la route n'existe pas (app anterieure) -> ``arret = "unsupported"``.
    - 403 ``agent_credential_required`` : session non verifiee ->
      ``arret = "credential_required"``.
    - autre (session pas encore etablie, panne) : jetons inchanges, on reessaie.
    """
    if status == 404:
        return tokens, "unsupported", HEARTBEAT_SECONDS
    if status == 403 and _error_code(body) == "agent_credential_required":
        return tokens, "credential_required", HEARTBEAT_SECONDS
    if status != 200 or not isinstance(body, dict):
        return tokens, None, HEARTBEAT_SECONDS
    fresh: Dict[str, int] = {}
    for lease in body.get("leases") or []:
        if isinstance(lease, dict):
            task_id = lease.get("taskId")
            token = lease.get("leaseToken")
            if isinstance(task_id, str) and isinstance(token, int) and not isinstance(token, bool):
                fresh[task_id] = token
    if not fresh and not body.get("refused"):
        # Rien de detenu : rythme lent (cf. IDLE_SECONDS).
        return fresh, None, float(IDLE_SECONDS)
    interval = body.get("heartbeatSeconds")
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
        interval = HEARTBEAT_SECONDS
    return fresh, None, float(min(interval, HEARTBEAT_SECONDS))


def _error_code(body: Any) -> Optional[str]:
    """``data.code`` d'une erreur h3 (``{statusCode, statusMessage, data: {code}}``)."""
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        code = body["data"].get("code")
        return code if isinstance(code, str) else None
    return None
