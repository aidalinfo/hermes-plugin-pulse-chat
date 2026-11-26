"""Jeton de tour Pulse (#755 lot 5, docs/84 § 9.5 de l'app) — transport seulement.

L'app signe, pour chaque tour qu'elle ouvre chez l'agent, un jeton OPAQUE :
champ ``turnContext`` a la racine des trames ``message.created`` (rejeu
compris), ``approval.reply``, ``question.reply`` et ``gate.reply``. Le plugin
ne le lit pas, ne le juge pas, ne le fabrique jamais : il le garde pour CE tour
et le renvoie tel quel dans l'en-tete ``x-pulse-turn-context`` :

- de ses appels HTTP vers l'app pendant ce tour (``headers``) ;
- des appels d'inference qu'Hermes fait pendant ce tour VERS LA PASSERELLE IA
  DE PULSE (``llm_request``) — jamais vers un autre fournisseur : le jeton
  n'est pas secret, mais il ne sort pas de Pulse. La passerelle en deduit
  l'execution de routine du tour et le modele/effort choisis pour elle.

Le jeton vit dans une ``ContextVar`` (task-local), JAMAIS dans une variable de
processus : deux tours simultanes (deux canaux, ou l'un stable et l'autre
canary) ne doivent pas se le voler. Il suit le tour la ou asyncio copie le
contexte : taches creees pendant le tour (``asyncio.create_task`` — la
passerelle cree ainsi la tache du tour), fils d'outil et fil de l'agent
(``asyncio.to_thread``, ``contextvars.copy_context().run`` — la passerelle
lance ainsi la boucle de conversation), coroutines renvoyees sur la boucle
depuis un fil d'outil (``asyncio.run_coroutine_threadsafe`` copie le contexte
du fil appelant).

Message mis en file pendant un tour : Hermes le traite dans une tache creee
DEPUIS la tache du tour en cours (``_spawn_drain_task``), qui en herite le
jeton. L'adaptateur repose donc le jeton de l'evenement au debut de chaque
traitement (``on_processing_start``), ou l'efface s'il n'en a pas.

Repli sur dans tous les cas : un appel sans en-tete garde la regle d'avant
(affectation de l'agent), et un jeton echu ou d'un autre agent est ignore par
l'app.
"""
from __future__ import annotations

import contextvars
import re
from typing import Any, Dict, Mapping, Optional

HEADER = "x-pulse-turn-context"

#: Chemin de la passerelle IA de Pulse sous ``PULSE_CHAT_URL`` (primaire et
#: ``…/fallback/v1``, cf. ``ai_routing``).
GATEWAY_PATH = "/api/agent/ai-gateway/"

# Borne de lecture alignee sur l'app (``MAX_TOKEN_LENGTH``) et alphabet du
# jeton (``ptc1.<base64url>.<base64url>``) : rien d'autre ne part en en-tete.
_MAX_LENGTH = 1024
_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_.-]+$")

_CURRENT: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "pulse_chat_turn_context", default=None
)


def from_frame(data: Mapping[str, Any]) -> Optional[str]:
    """Jeton porte par une trame de l'app, ``None`` s'il est absent ou suspect.

    Une valeur suspecte (autre type, trop longue, caractere hors alphabet —
    un saut de ligne ferait une injection d'en-tete) n'est jamais renvoyee :
    le tour part sans en-tete, comme avec une app qui n'en emet pas.
    """
    token = data.get("turnContext") if isinstance(data, Mapping) else None
    if not isinstance(token, str) or not token or len(token) > _MAX_LENGTH:
        return None
    return token if _TOKEN_SHAPE.match(token) else None


def current() -> Optional[str]:
    """Jeton du tour en cours dans CE contexte, ``None`` hors tour."""
    return _CURRENT.get()


def enter(token: Optional[str]) -> contextvars.Token:
    """Pose le jeton du tour qui commence (``None`` efface celui d'un tour precedent).

    A refermer par ``leave`` dans un ``finally`` : la boucle de reception du
    WebSocket traite les trames l'une apres l'autre dans la MEME tache, et le
    jeton d'une trame ne doit pas rester pose pour la suivante. Seule
    exception : ``on_processing_start``, ou la duree de vie est la tache du
    tour elle-meme.
    """
    return _CURRENT.set(token)


def leave(reset: contextvars.Token) -> None:
    """Retire le jeton pose par ``enter``."""
    _CURRENT.reset(reset)


def headers() -> Dict[str, str]:
    """En-tete a ajouter aux appels vers l'app pendant le tour, ``{}`` hors tour."""
    token = _CURRENT.get()
    return {HEADER: token} if token else {}


def is_gateway_url(url: Any, pulse_base_url: Any) -> bool:
    """``url`` est-elle la passerelle IA de CETTE app Pulse ?

    Comparaison par prefixe ``<PULSE_CHAT_URL>/api/agent/ai-gateway/`` : le
    chemin colle a l'hote interdit ``https://pulse.example.com.autre.tld``, et
    une autre route de l'app (``/api/agent/messages``) ne correspond pas.
    """
    if not isinstance(url, str) or not isinstance(pulse_base_url, str):
        return False
    base = pulse_base_url.strip().rstrip("/")
    if not base:
        return False
    prefix = (base + GATEWAY_PATH).lower()
    # Un profil apres le prefixe : la racine nue ``…/ai-gateway`` n'en est pas une entree.
    return url.strip().rstrip("/").lower().startswith(prefix)


def llm_request(
    request: Any, base_url: Any, pulse_base_url: Any
) -> Optional[Dict[str, Any]]:
    """Requete d'inference a envoyer, avec l'en-tete du tour, ou ``None`` si rien ne change.

    Contrat du middleware ``llm_request`` d'Hermes (``hermes_cli/middleware.py``) :
    ``request`` = les kwargs passes au SDK (``chat_completions``,
    ``codex_responses`` comme ``anthropic_messages`` acceptent
    ``extra_headers``), ``base_url`` = l'adresse de CETTE tentative (primaire
    ou repli). La requete recue n'est jamais modifiee en place ; un
    ``extra_headers`` deja present (Copilot, OpenRouter, beta Anthropic) est
    conserve.
    """
    token = _CURRENT.get()
    if not token or not isinstance(request, dict):
        return None
    if not is_gateway_url(base_url, pulse_base_url):
        return None
    merged = dict(request.get("extra_headers") or {})
    merged[HEADER] = token
    return {**request, "extra_headers": merged}
