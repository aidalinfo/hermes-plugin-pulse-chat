# -*- coding: utf-8 -*-
"""Questions posees par l'agent — partie PURE du transport.

L'adaptateur reste mince : il traduit et transporte, il ne decide pas. Ce
module ne connait ni le WebSocket ni le HTTP — il ne fait que construire les
payloads sortants et interpreter la trame de reponse.

C'est le pendant d'``approvals.py`` pour la primitive ``clarify`` d'Hermes
(``tools/clarify_gateway.py``), dont le point d'extension d'adaptateur est
``BasePlatformAdapter.send_clarify`` : sans override, Hermes retombe sur une
invite TEXTE (« ❓ <question> / 1. … / 2. … / Reply with the number, the option
text, or your own answer. ») pensee pour Telegram.

Sur Pulse Chat, ce repli ne produisait meme pas un message lisible : la
classification du plugin (``classification.parse_tool``) lit « emoji court +
mot » comme un debut de tool progress, si bien que « ❓ Quel niveau d'action… »
devenait un ``ToolEvent`` intitule « Quel », replie sous « 1 activite d'outil ».
Une question a laquelle on ne pouvait que repondre en retapant son numero dans
le composeur — quand on pensait a deplier l'activite.

Contrat (miroir de app/pulse-chat/shared/questions.ts) :

    plugin -> app   POST /api/agent/messages
        {kind: "question_request", channelSlug, requestId, question, choices}
        {kind: "question_retire",  channelSlug, requestId}

    app -> plugin   trame WS
        {type: "question.reply", channel: {...},
         question: {requestId, answer, answeredBy, answeredAt}}

``requestId`` EST le ``clarify_id`` d'Hermes : c'est lui qui debloque le thread
agent via ``resolve_gateway_clarify``, et c'est pour cela qu'il n'est pas
regenere ici (contrairement au ``request_id`` d'une approbation du garde-fou).

``answer`` est la chaine EXACTE a passer a ``resolve_gateway_clarify`` : le
libelle BRUT du choix, suffixe « (Recommended) » compris — c'est ce que fait
l'adaptateur Slack, et ``_match_label`` d'en face ne rattrape qu'un ecart de
casse ou de suffixe. Aucun index ne voyage : le plugin n'a plus la liste au
moment ou la reponse arrive.
"""

from typing import Any, Dict, Iterable, List, Optional

#: Bornes d'affichage (miroir de shared/questions.ts). L'app refait ce bornage
#: de son cote : le faire ici evite d'envoyer pour rien ce qu'elle coupera.
#:
#: ``MAX_CHOICES`` vaut 4 des deux cotes parce que c'est deja la borne d'Hermes
#: (``tools/clarify_tool.MAX_CHOICES``) : on ne RESSERRE rien, on refuse
#: seulement d'elargir en silence.
MAX_CHOICES = 4
MAX_CHOICE_LENGTH = 200
MAX_QUESTION_LENGTH = 1000


def normalize_choices(choices: Optional[Iterable[Any]]) -> List[str]:
    """Borne les choix proposes, en conservant l'ORDRE.

    L'ordre n'est pas cosmetique : Hermes documente « put your recommended
    option FIRST », et c'est le premier qui porte le suffixe
    « (Recommended) ». Trier ou dedoublonner ici deplacerait la recommandation.

    Rend ``[]`` pour une question OUVERTE (Hermes en emet : ``choices`` vaut
    alors ``None``), qui se repond en texte libre. Ce n'est pas une anomalie,
    c'est un des deux regimes.
    """
    if not choices:
        return []
    kept: List[str] = []
    for entry in choices:
        if not isinstance(entry, str):
            continue
        trimmed = entry.strip()
        if not trimmed:
            continue
        kept.append(trimmed[:MAX_CHOICE_LENGTH])
        if len(kept) == MAX_CHOICES:
            break
    return kept


def build_question_payload(
    channel_slug: str,
    request_id: str,
    question: str,
    choices: Optional[Iterable[Any]] = None,
) -> Dict[str, Any]:
    """Construit le POST d'une question. PURE — aucune I/O.

    ``choices`` est OMIS quand il est vide plutot qu'envoye a ``[]`` : meme
    parti pris que ``summary``/``risks`` d'``approvals.py``. Le schema d'entree
    de l'app est ``strict()`` et accepte l'absence (``nullish``), mais une
    liste vide et une absence disent la meme chose — n'en envoyer qu'une seule
    evite d'avoir deux formes a tenir d'accord.
    """
    payload: Dict[str, Any] = {
        "channelSlug": channel_slug,
        "kind": "question_request",
        "requestId": request_id,
        "question": (question or "").strip()[:MAX_QUESTION_LENGTH],
    }
    cleaned = normalize_choices(choices)
    if cleaned:
        payload["choices"] = cleaned
    return payload


def build_question_retire_payload(channel_slug: str, request_id: str) -> Dict[str, Any]:
    """Construit le POST de RETRAIT : Hermes a relache son attente sans reponse.

    Le ``notice`` d'Hermes n'est deliberement PAS transmis : c'est une phrase
    anglaise pensee pour etre reecrite dans une bulle Slack (« This prompt
    expired… »), alors que la carte de Pulse Chat affiche son propre libelle
    localise. Transmettre les deux ferait dependre le texte d'un ecran francais
    de la locale d'Hermes.
    """
    return {
        "channelSlug": channel_slug,
        "kind": "question_retire",
        "requestId": request_id,
    }


def parse_question_reply(frame: Any) -> Optional[Dict[str, Any]]:
    """Extrait la reponse d'une trame ``question.reply``.

    Retourne ``None`` si la trame n'en est pas une ou si elle est inexploitable
    (source reseau : on ne suppose jamais la forme recue).

    Une reponse VIDE est rejetee : ``resolve_gateway_clarify`` l'accepterait et
    debloquerait l'agent avec une chaine vide — il reprendrait son tour en
    croyant avoir ete repondu, sans qu'aucune erreur n'apparaisse. C'est le
    pendant exact du refus d'une decision hors liste blanche cote approbation.
    """
    if not isinstance(frame, dict) or frame.get("type") != "question.reply":
        return None
    question = frame.get("question")
    if not isinstance(question, dict):
        return None
    request_id = question.get("requestId")
    answer = question.get("answer")
    if not isinstance(request_id, str) or not request_id:
        return None
    if not isinstance(answer, str) or not answer.strip():
        return None
    answered_by = question.get("answeredBy")
    if not isinstance(answered_by, dict):
        answered_by = {}
    return {
        "requestId": request_id,
        "answer": answer,
        "answeredBy": {
            "userId": answered_by.get("userId") or "",
            "userName": answered_by.get("userName") or "",
        },
        "answeredAt": question.get("answeredAt"),
    }
