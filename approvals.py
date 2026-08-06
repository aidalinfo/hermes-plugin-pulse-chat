# -*- coding: utf-8 -*-
"""Demandes d'approbation d'actions sensibles — partie PURE du transport.

L'adaptateur reste mince : il traduit et transporte, il ne decide pas. Ce
module ne connait ni le WebSocket ni le HTTP — il ne fait que construire le
payload sortant et interpreter la trame de reponse. La logique « quelle action
exige une approbation » vit cote Hermes ; la decision vit cote app Nuxt.

Contrat (miroir de app/pulse-chat/shared/approvals.ts) :

    plugin -> app   POST /api/agent/messages
        {kind: "approval_request", channelSlug, requestId, tool, command,
         reason, options}

    app -> plugin   trame WS
        {type: "approval.reply", channel: {...},
         approval: {requestId, decision, decidedBy, decidedAt}}

``decision`` vaut ``once`` | ``session`` | ``always`` | ``deny``. Seul ``deny``
refuse : tout le reste autorise l'execution.
"""

from typing import Any, Dict, Iterable, List, Optional

#: Options reconnues par l'app, dans l'ordre d'affichage.
APPROVAL_OPTIONS = ("once", "session", "always", "deny")

#: Repli quand l'appelant n'en propose aucune d'exploitable.
DEFAULT_APPROVAL_OPTIONS = ("once", "deny")


def normalize_options(options: Optional[Iterable[Any]]) -> List[str]:
    """Filtre les options contre la liste blanche, ordre canonique, dedoublonne.

    L'app refait ce filtrage de son cote (elle ne fait pas confiance au
    plugin) : le faire ici evite juste un aller-retour pour rien.
    """
    if not options:
        return list(DEFAULT_APPROVAL_OPTIONS)
    kept = {opt for opt in options if opt in APPROVAL_OPTIONS}
    ordered = [opt for opt in APPROVAL_OPTIONS if opt in kept]
    if not ordered:
        return list(DEFAULT_APPROVAL_OPTIONS)
    # Une demande sans issue negative ne serait pas une approbation.
    if "deny" not in ordered:
        ordered.append("deny")
    return ordered


def build_approval_payload(
    channel_slug: str,
    request_id: str,
    tool: str,
    command: str,
    reason: Optional[str] = None,
    options: Optional[Iterable[Any]] = None,
) -> Dict[str, Any]:
    """Construit le POST sortant. PURE — aucune I/O."""
    return {
        "channelSlug": channel_slug,
        "kind": "approval_request",
        "requestId": request_id,
        "tool": tool,
        "command": command,
        "reason": reason,
        "options": normalize_options(options),
    }


def parse_approval_reply(frame: Any) -> Optional[Dict[str, Any]]:
    """Extrait la decision d'une trame ``approval.reply``.

    Retourne ``None`` si la trame n'en est pas une ou si elle est inexploitable
    (source reseau : on ne suppose jamais la forme recue). Une decision hors
    liste blanche est rejetee — on ne debloque pas une execution sur un mot
    qu'on ne sait pas interpreter.
    """
    if not isinstance(frame, dict) or frame.get("type") != "approval.reply":
        return None
    approval = frame.get("approval")
    if not isinstance(approval, dict):
        return None
    request_id = approval.get("requestId")
    decision = approval.get("decision")
    if not isinstance(request_id, str) or not request_id:
        return None
    if decision not in APPROVAL_OPTIONS:
        return None
    decided_by = approval.get("decidedBy")
    if not isinstance(decided_by, dict):
        decided_by = {}
    channel = frame.get("channel")
    return {
        "requestId": request_id,
        "decision": decision,
        "granted": decision != "deny",
        "decidedBy": {
            "userId": decided_by.get("userId") or "",
            "userName": decided_by.get("userName") or "",
        },
        "decidedAt": approval.get("decidedAt"),
        "channelSlug": channel.get("slug") if isinstance(channel, dict) else None,
    }


def is_granting(decision: Optional[str]) -> bool:
    """``deny`` est le seul refus ; l'absence de decision n'accorde rien."""
    return bool(decision) and decision != "deny"
