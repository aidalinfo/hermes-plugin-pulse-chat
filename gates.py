# -*- coding: utf-8 -*-
"""Demandes d'approbation DELIBEREES de l'agent — partie PURE du transport.

L'adaptateur reste mince : il traduit et transporte, il ne decide pas. Ce
module ne connait ni le WebSocket ni le HTTP — il construit les payloads, lit
la trame de reponse et compose ce que l'outil rend au modele.

A ne pas confondre avec ``approvals.py`` : celui-la transporte les demandes du
GARDE-FOU d'Hermes (une commande juge dangereuse, options ``once|session|
always|deny``). Ici, c'est l'agent LUI-MEME qui soumet un plan ou un livrable
aux approbateurs que l'app a designes pour lui — trois issues, dont « demander
des ajustements », et un commentaire qui lui revient comme consigne.

Contrat (miroir de app/pulse-chat/shared/gates.ts) :

    plugin -> app   POST /api/agent/messages
        {kind: "gate_request", channelSlug, requestId, title, body}

    app -> plugin   trame WS
        {type: "gate.reply", channel: {...},
         gate: {requestId, title, decision, comment, decidedBy, decidedAt}}

``decision`` vaut ``approved`` | ``changes_requested`` | ``denied``.

── L'ATTENTE EST BORNEE PAR HERMES, PAS PAR NOUS ───────────────────────────

La demande elle-meme n'expire JAMAIS cote app. Mais l'outil ne peut pas
attendre indefiniment : ``model_tools._run_async`` plafonne a 300 s l'execution
d'un outil asynchrone lance depuis la passerelle (``future.result(timeout=300)``),
et un depassement ferait echouer l'appel d'outil — l'agent croirait sa demande
perdue. L'outil attend donc ``GATE_WAIT_SECONDS`` (sous ce plafond), puis rend
``pending`` avec la consigne de s'arreter la. Une decision qui arrive APRES
revient a l'agent comme un MESSAGE entrant ordinaire (``decision_message_text``)
— le meme chemin qu'apres un redemarrage du bot, ou l'attente a disparu avec
le process.
"""

import json
from typing import Any, Dict, Optional

#: Nom de l'outil. PREFIXE, jamais ``request_approval`` : le coeur d'Hermes
#: construit sa propre machinerie d'approbation, et ``register_tool`` sans
#: ``override=True`` face a un nom deja pris journalise un avertissement et rend
#: None — l'outil disparaitrait sans erreur visible.
GATE_TOOL_NAME = "pulse_request_approval"

#: Nom du skill enregistre par le plugin, resolu en ``pulse-chat:approvals``.
GATE_SKILL_NAME = "approvals"

#: Attente dans l'outil, SOUS le plafond de 300 s de ``_run_async``.
GATE_WAIT_SECONDS = 270

#: Bornes (miroir de shared/gates.ts). L'app les reapplique : le plugin est une
#: source non fiable pour elle. Les appliquer ici evite d'envoyer ce qu'elle
#: refuserait.
MAX_TITLE_LENGTH = 200
MAX_BODY_LENGTH = 4000

DECISIONS = ("approved", "changes_requested", "denied")

GATE_TOOL_DESCRIPTION = (
    "Soumet ton PLAN ou ton LIVRABLE a l'approbation des personnes designees pour "
    "toi dans Pulse Chat, et ATTEND leur decision avant de continuer. A appeler : "
    "(1) AVANT d'engager une action couteuse, irreversible ou visible par des tiers "
    "(envoyer, publier, supprimer, modifier des donnees reelles) — soumets le plan ; "
    "(2) quand tu remets un livrable qu'on t'a demande de faire valider (compte "
    "rendu, document, proposition) — soumets le livrable. Le canal est celui de la "
    "conversation en cours : tu ne le passes pas. Issue : `approved` (continue), "
    "`changes_requested` (applique `comment` puis resoumets), `denied` (arrete-toi "
    "et dis-le), `pending` (personne n'a encore tranche : ARRETE-TOI, dis a l'humain "
    "que tu attends ; la decision t'arrivera plus tard comme un message, et tu "
    "reprendras a ce moment-la). N'agis JAMAIS sur ce que tu as soumis tant que "
    "l'issue n'est pas `approved`. Pour le detail (ecrire un bon titre et un bon "
    "corps, quoi soumettre), charge le skill `pulse-chat:approvals`."
)

GATE_TOOL_SCHEMA: Dict[str, Any] = {
    "name": GATE_TOOL_NAME,
    "description": GATE_TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "maxLength": MAX_TITLE_LENGTH,
                "description": (
                    "Ce que tu demandes de valider, en une ligne (ex. « Plan "
                    "d'extraction des factures de septembre »). C'est ce que "
                    "l'approbateur voit dans sa file et dans la notification."
                ),
            },
            "body": {
                "type": "string",
                "maxLength": MAX_BODY_LENGTH,
                "description": (
                    "Le plan ou le livrable a trancher, en Markdown : les etapes "
                    "numerotees et ce qu'elles touchent, ou le contenu du "
                    "livrable. L'approbateur n'a peut-etre PAS acces a la "
                    "conversation : ce texte doit se suffire a lui-meme."
                ),
            },
        },
        "required": ["title", "body"],
    },
}


def _bounded(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def build_gate_payload(
    *, channel_slug: str, request_id: str, title: str, body: str
) -> Dict[str, Any]:
    """Payload de ``POST /api/agent/messages`` pour une demande deliberee."""
    return {
        "kind": "gate_request",
        "channelSlug": channel_slug,
        "requestId": request_id,
        "title": _bounded(title, MAX_TITLE_LENGTH),
        "body": _bounded(body, MAX_BODY_LENGTH),
    }


def parse_gate_reply(frame: Any) -> Optional[Dict[str, Any]]:
    """Extrait la decision d'une trame ``gate.reply``, ou ``None`` si inexploitable.

    Une decision hors des trois issues connues est REJETEE plutot que devinee :
    la lire comme un refus ou un accord ferait dire a l'humain ce qu'il n'a pas
    dit.
    """
    if not isinstance(frame, dict) or frame.get("type") != "gate.reply":
        return None
    gate = frame.get("gate")
    if not isinstance(gate, dict):
        return None
    request_id = gate.get("requestId")
    decision = gate.get("decision")
    if not isinstance(request_id, str) or not request_id or decision not in DECISIONS:
        return None
    decided_by = gate.get("decidedBy") if isinstance(gate.get("decidedBy"), dict) else {}
    channel = frame.get("channel") if isinstance(frame.get("channel"), dict) else {}
    comment = gate.get("comment")
    return {
        "requestId": request_id,
        "title": str(gate.get("title") or ""),
        "decision": decision,
        "comment": comment if isinstance(comment, str) and comment.strip() else None,
        "decidedBy": str(decided_by.get("userName") or ""),
        "decidedAt": gate.get("decidedAt"),
        "channelSlug": str(channel.get("slug") or ""),
        "channelName": str(channel.get("name") or ""),
    }


def tool_result(reply: Dict[str, Any]) -> str:
    """Ce que l'outil rend au modele quand la decision est arrivee A TEMPS."""
    decision = reply["decision"]
    result: Dict[str, Any] = {
        "status": decision,
        "granted": decision == "approved",
        "decidedBy": reply.get("decidedBy") or None,
    }
    if reply.get("comment"):
        result["comment"] = reply["comment"]
    result["next"] = _NEXT[decision]
    return json.dumps(result, ensure_ascii=False)


def pending_result(request_id: str) -> str:
    """Personne n'a tranche dans la fenetre de l'outil — la demande RESTE ouverte."""
    return json.dumps(
        {
            "status": "pending",
            "granted": False,
            "requestId": request_id,
            "next": (
                "Personne n'a encore tranche. ARRETE-TOI ici : dis a l'humain que "
                "tu attends la validation et ne fais RIEN de ce que tu as soumis. "
                "La demande reste ouverte ; la decision t'arrivera plus tard comme "
                "un message, et tu reprendras a ce moment-la. Ne resoumets pas."
            ),
        },
        ensure_ascii=False,
    )


def refused_result(code: str, message: str) -> str:
    """La demande n'a pas pu etre ouverte. ``granted`` est TOUJOURS faux."""
    advice = _ADVICE.get(code, _ADVICE["not_sent"])
    return json.dumps(
        {"status": "refused", "granted": False, "code": code, "message": message, "next": advice},
        ensure_ascii=False,
    )


def parse_error_body(status: int, body: Any) -> Dict[str, str]:
    """Code et message d'un refus HTTP de l'app, pour les rendre tels quels."""
    code = "not_sent"
    message = f"HTTP {status}"
    if isinstance(body, dict):
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        if isinstance(data.get("code"), str):
            code = data["code"]
        text = body.get("statusMessage") or body.get("message")
        if isinstance(text, str) and text.strip():
            message = text.strip()
    return {"code": code, "message": message}


def decision_message_text(reply: Dict[str, Any]) -> str:
    """Texte du message entrant qui porte une decision TARDIVE a l'agent.

    Utilise quand aucune attente n'est active : l'outil a rendu ``pending``
    (fenetre depassee) ou le bot a redemarre. Il doit se suffire : l'agent
    reprend sur ce seul message, donc il nomme la demande, l'issue, la consigne
    et la suite a donner.
    """
    title = reply.get("title") or reply["requestId"]
    who = reply.get("decidedBy") or "un approbateur"
    head = {
        "approved": f"[Approbation] {who} a APPROUVE ta demande « {title} ».",
        "changes_requested": f"[Approbation] {who} demande des AJUSTEMENTS sur « {title} ».",
        "denied": f"[Approbation] {who} a REFUSE ta demande « {title} ».",
    }[reply["decision"]]
    lines = [head]
    if reply.get("comment"):
        lines.append(f"Consigne : {reply['comment']}")
    lines.append(_NEXT[reply["decision"]])
    return "\n".join(lines)


_NEXT = {
    "approved": "Tu peux reprendre et executer ce que tu avais soumis, exactement.",
    "changes_requested": (
        "Applique la consigne, puis resoumets avec pulse_request_approval avant d'agir."
    ),
    "denied": "Ne fais PAS ce que tu avais soumis. Dis-le a l'humain et demande-lui la suite.",
}

_ADVICE = {
    "no_approver_configured": (
        "Aucun approbateur n'est designe pour toi dans cette organisation. Dis-le a "
        "l'humain : un administrateur doit en designer un dans Reglages -> Agents. "
        "N'execute pas ce que tu voulais faire valider."
    ),
    "no_channel": (
        "Cet outil ne marche que dans une conversation Pulse Chat. N'execute pas ce "
        "que tu voulais faire valider."
    ),
    "not_connected": (
        "Pulse Chat est injoignable pour l'instant. Dis a l'humain que tu n'as pas pu "
        "demander la validation, et n'execute pas ce que tu voulais faire valider."
    ),
    "not_sent": (
        "La demande n'a pas pu etre ouverte. Dis-le a l'humain et n'execute pas ce que "
        "tu voulais faire valider."
    ),
}
