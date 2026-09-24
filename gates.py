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
        {kind: "gate_request", channelSlug, requestId, title, body?,
         reason?, steps?, scope?, impact?, risk?, reversible?, attachments?}

    app -> plugin   trame WS
        {type: "gate.reply", channel: {...},
         gate: {requestId, title, decision, comment, decidedBy, decidedAt}}

``decision`` vaut ``approved`` | ``changes_requested`` | ``denied``.

── LES RUBRIQUES STRUCTUREES (plugin >= 1.9.0) ─────────────────────────────

``reason``, ``steps``, ``scope``, ``impact``, ``risk``, ``reversible`` et
``attachments`` sont FACULTATIFS. Le plugin les TRANSPORTE : il ne garde que
ceux qui ont la bonne forme et coupe aux bornes de l'app (pour ne pas envoyer
ce qu'elle refuserait), mais c'est l'app qui valide — elle refuse une
reference de piece jointe sans prefixe ``vault:`` / ``message:``, ou qui ne se
resout pas, et ce refus revient a l'agent avec son code.

Une app ANTERIEURE a ces rubriques refuse la trame en 400 (schema strict,
``unrecognized_keys``). Le plugin la REPOSTE alors une fois en titre + corps,
les rubriques repliees en Markdown dans le corps (``legacy_payload``) : la
demande arrive, moins bien presentee, plutot que pas du tout. Les pieces
jointes n'y sont plus que NOMMEES — une app ancienne ne sait pas les servir.

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
from typing import Any, Dict, List, Optional

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
MAX_REASON_LENGTH = 2000
MAX_IMPACT_LENGTH = 1000
MAX_STEPS = 12
MAX_STEP_LABEL_LENGTH = 300
MAX_STEP_COMMAND_LENGTH = 500
MAX_SCOPE_ITEMS = 10
MAX_SCOPE_LABEL_LENGTH = 60
MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_REF_LENGTH = 1000

RISKS = ("low", "medium", "high")

#: Champs structures, dans l'ordre ou ils sont rendus a l'approbateur.
STRUCTURED_FIELDS = ("reason", "steps", "scope", "impact", "risk", "reversible", "attachments")

DECISIONS = ("approved", "changes_requested", "denied")

GATE_TOOL_DESCRIPTION = (
    "Soumet ton LIVRABLE ou ton PLAN a l'approbation des personnes designees pour "
    "toi dans Pulse Chat, et ATTEND leur decision avant de continuer. A appeler : "
    "(1) quand tu remets un livrable a faire valider (compte rendu, document, "
    "proposition, rapport) — soumets le livrable ; (2) AVANT d'executer un plan "
    "couteux ou difficile a defaire (ecraser des documents, retraiter des donnees "
    "reelles, redemarrer un service, traitement long) — soumets le plan. Ce N'EST "
    "PAS l'accord d'un envoi exterieur par connecteur (courriel, GitHub…) : "
    "celui-la appartient au proprietaire du compte, et un `approved` ici ne leve "
    "pas un refus `connector_approval_required`. Le canal est celui de la "
    "conversation en cours : tu ne le passes pas. REMPLIS LES RUBRIQUES plutot "
    "qu'un long `body` : l'approbateur les voit en premier (`reason` = pourquoi, "
    "`steps` = ce que tu vas faire, dans l'ordre, avec la commande exacte s'il y "
    "en a une, `scope` = ce qui est touche, `impact`, `risk`, `reversible`), et "
    "joins les documents a lire avec `attachments` (`vault:<chemin>` ou "
    "`message:<id>`, jamais le contenu). `body` devient alors facultatif. Issue : "
    "`approved` (continue), `changes_requested` (applique `comment` puis "
    "resoumets), `denied` (arrete-toi et dis-le), `pending` (personne n'a encore "
    "tranche : ARRETE-TOI, dis a l'humain que tu attends ; la decision t'arrivera "
    "plus tard comme un message, et tu reprendras a ce moment-la), `refused` (la "
    "demande n'a pas ete ouverte : lis `code` et `message`, corrige si c'est un "
    "parametre, sinon dis-le a l'humain). N'agis JAMAIS sur ce que tu as soumis "
    "tant que l'issue n'est pas `approved`. Pour le detail (quoi soumettre, "
    "exemples de rubriques bien remplies), charge le skill `pulse-chat:approvals`."
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
                    "Ce que tu demandes de valider, en une ligne (ex. « Redemarrer "
                    "le builder multi-arch », « Compte rendu du comite du 20/09 »). "
                    "C'est ce que l'approbateur voit dans sa file et dans la "
                    "notification."
                ),
            },
            "body": {
                "type": "string",
                "maxLength": MAX_BODY_LENGTH,
                "description": (
                    "Facultatif si tu remplis au moins une rubrique. Le contenu "
                    "du livrable, ou le detail qui ne rentre dans aucune rubrique, "
                    "en Markdown. L'approbateur n'a peut-etre PAS acces a la "
                    "conversation : ce que tu soumets doit se suffire a lui-meme. "
                    "Ne recopie pas ici ce que disent deja `reason` et `steps`."
                ),
            },
            "reason": {
                "type": "string",
                "maxLength": MAX_REASON_LENGTH,
                "description": (
                    "POURQUOI tu le demandes, en un court paragraphe : le probleme "
                    "constate ou la demande de l'humain, et pourquoi cette action "
                    "y repond. Ex. « Le builder buildx est bloque depuis 9 h : "
                    "trois pipelines echouent sur l'etape d'image. »"
                ),
            },
            "steps": {
                "type": "array",
                "maxItems": MAX_STEPS,
                "description": (
                    "CE QUE TU VAS FAIRE, une entree par etape, dans l'ordre "
                    "d'execution. Une etape = une action que l'approbateur peut "
                    "juger ; mets la commande EXACTE dans `command` quand il y en "
                    "a une (c'est elle qu'il valide). Pour un livrable, omets ce "
                    "champ ou decris ce que tu feras une fois approuve (« envoyer "
                    "a la compta »)."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {
                            "type": "string",
                            "maxLength": MAX_STEP_LABEL_LENGTH,
                            "description": "L'action, en une phrase (ex. « Redemarrer le builder »).",
                        },
                        "command": {
                            "type": "string",
                            "maxLength": MAX_STEP_COMMAND_LENGTH,
                            "description": (
                                "Facultatif : la commande exacte, sur une ligne "
                                "(ex. `docker restart buildx_buildkit_multiarch0`)."
                            ),
                        },
                    },
                    "required": ["label"],
                },
            },
            "scope": {
                "type": "array",
                "maxItems": MAX_SCOPE_ITEMS,
                "items": {"type": "string", "maxLength": MAX_SCOPE_LABEL_LENGTH},
                "description": (
                    "PERIMETRE TOUCHE : de courtes etiquettes pour ce que l'action "
                    "modifie (ex. [\"CI\", \"Builder multi-arch\", \"12 fichiers "
                    "du dossier Contrats\"]). Deux ou trois mots chacune."
                ),
            },
            "impact": {
                "type": "string",
                "maxLength": MAX_IMPACT_LENGTH,
                "description": (
                    "IMPACT attendu, en une ou deux phrases : qui ou quoi est "
                    "affecte, et combien de temps (ex. « Les builds en cours "
                    "echouent une fois ; 30 s d'indisponibilite. »)."
                ),
            },
            "risk": {
                "type": "string",
                "enum": list(RISKS),
                "description": (
                    "Ton estimation HONNETE du risque : `low` (sans consequence "
                    "notable), `medium` (derangement limite, rattrapable), `high` "
                    "(donnees, argent, clients ou production en jeu). Omets-le si "
                    "tu ne sais pas — ne le devine pas a la baisse."
                ),
            },
            "reversible": {
                "type": "boolean",
                "description": (
                    "`true` si l'action se defait simplement, `false` si elle ne "
                    "se rattrape pas (suppression definitive, envoi, paiement). "
                    "Omets-le si tu ne sais pas : l'ecran n'affiche alors rien, "
                    "plutot qu'une promesse."
                ),
            },
            "attachments": {
                "type": "array",
                "maxItems": MAX_ATTACHMENTS,
                "items": {"type": "string", "maxLength": MAX_ATTACHMENT_REF_LENGTH},
                "description": (
                    "Documents que l'approbateur doit pouvoir OUVRIR pour trancher "
                    "(le livrable lui-meme, un rapport d'incident, un devis). Des "
                    "REFERENCES, jamais le contenu : `vault:<chemin>` pour un "
                    "fichier du coffre de la conversation (ecris-le d'abord avec "
                    "pulse_vault_write), `message:<id>` pour une piece jointe "
                    "deja envoyee dans la conversation. Le PREFIXE est "
                    "obligatoire. Une reference introuvable fait refuser toute la "
                    "demande (`gate_attachment_not_found`) : corrige-la et "
                    "resoumets."
                ),
            },
        },
        "required": ["title"],
    },
}


def _bounded(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _clean_steps(raw: Any) -> List[Dict[str, str]]:
    if not isinstance(raw, list):
        return []
    steps: List[Dict[str, str]] = []
    for entry in raw:
        if isinstance(entry, str):
            entry = {"label": entry}
        if not isinstance(entry, dict):
            continue
        label = _bounded(entry.get("label"), MAX_STEP_LABEL_LENGTH)
        if not label:
            continue
        step = {"label": label}
        command = _bounded(entry.get("command"), MAX_STEP_COMMAND_LENGTH)
        if command:
            step["command"] = command
        steps.append(step)
        if len(steps) == MAX_STEPS:
            break
    return steps


def _clean_strings(raw: Any, limit: int, max_items: int) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        value = _bounded(entry, limit)
        if value and value not in out:
            out.append(value)
        if len(out) == max_items:
            break
    return out


def structured_fields(args: Dict[str, Any]) -> Dict[str, Any]:
    """Rubriques FOURNIES par le modele, mises en forme — et elles seules.

    Mince par construction : on ne garde que ce qui a la bonne forme et on coupe
    aux bornes, rien n'est deduit ni complete. Une rubrique absente ou vide
    n'est pas envoyee du tout (l'app la lit alors comme « non annoncee »). Les
    references de pieces jointes partent TELLES QUELLES : c'est l'app qui exige
    le prefixe et les resout — la regle vit en un seul endroit.
    """
    out: Dict[str, Any] = {}
    reason = _bounded(args.get("reason"), MAX_REASON_LENGTH)
    if reason:
        out["reason"] = reason
    steps = _clean_steps(args.get("steps"))
    if steps:
        out["steps"] = steps
    scope = _clean_strings(args.get("scope"), MAX_SCOPE_LABEL_LENGTH, MAX_SCOPE_ITEMS)
    if scope:
        out["scope"] = scope
    impact = _bounded(args.get("impact"), MAX_IMPACT_LENGTH)
    if impact:
        out["impact"] = impact
    risk = args.get("risk")
    if isinstance(risk, str) and risk.strip().lower() in RISKS:
        out["risk"] = risk.strip().lower()
    reversible = args.get("reversible")
    # Un booleen STRICT : « "false" » (chaine) ou 0 ne sont pas une annonce.
    if isinstance(reversible, bool):
        out["reversible"] = reversible
    attachments = _clean_strings(args.get("attachments"), MAX_ATTACHMENT_REF_LENGTH, MAX_ATTACHMENTS)
    if attachments:
        out["attachments"] = attachments
    return out


def build_gate_payload(
    *,
    channel_slug: str,
    request_id: str,
    title: str,
    body: str,
    structured: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Payload de ``POST /api/agent/messages`` pour une demande deliberee.

    Sans rubrique, la trame est EXACTEMENT celle d'avant (titre + corps) : une
    app anterieure la recoit sans rien remarquer.
    """
    payload: Dict[str, Any] = {
        "kind": "gate_request",
        "channelSlug": channel_slug,
        "requestId": request_id,
        "title": _bounded(title, MAX_TITLE_LENGTH),
        "body": _bounded(body, MAX_BODY_LENGTH),
    }
    for key in STRUCTURED_FIELDS:
        if structured and key in structured:
            payload[key] = structured[key]
    return payload


_RISK_LABELS = {"low": "faible", "medium": "moyen", "high": "eleve"}


def _attachment_name(ref: str) -> str:
    """Nom lisible d'une reference, SANS son chemin : le corps descend dans le
    fil, lu aussi par un visiteur de lien de partage, a qui l'on ne prete pas le
    coffre."""
    value = ref.split(":", 1)[1] if ":" in ref else ref
    return value.rstrip("/").rsplit("/", 1)[-1] or value


def legacy_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """La meme demande pour une app ANTERIEURE aux rubriques : titre + corps.

    Les rubriques sont repliees en Markdown dans le corps, dans l'ordre de
    l'ecran — l'approbateur lit la meme chose, moins bien rangee. Les pieces
    jointes n'y sont que NOMMEES : une app ancienne ne sait pas les servir, et
    le dire vaut mieux que les perdre sans trace.
    """
    lines: List[str] = []
    if payload.get("reason"):
        lines += ["**Pourquoi**", str(payload["reason"]), ""]
    steps = payload.get("steps") or []
    if steps:
        lines.append("**Ce que l'agent va faire**")
        for index, step in enumerate(steps, start=1):
            text = f"{index}. {step.get('label', '')}"
            if step.get("command"):
                text += f" — `{step['command']}`"
            lines.append(text)
        lines.append("")
    if payload.get("scope"):
        lines += ["**Perimetre touche** : " + ", ".join(payload["scope"]), ""]
    if payload.get("impact"):
        lines += ["**Impact** : " + str(payload["impact"]), ""]
    flags = []
    if payload.get("risk") in _RISK_LABELS:
        flags.append(f"risque {_RISK_LABELS[payload['risk']]}")
    if payload.get("reversible") is True:
        flags.append("reversible")
    elif payload.get("reversible") is False:
        flags.append("IRREVERSIBLE")
    if flags:
        lines += ["**Evaluation** : " + ", ".join(flags), ""]
    if payload.get("attachments"):
        names = ", ".join(_attachment_name(r) for r in payload["attachments"])
        lines += [f"**Pieces jointes (non transmises par cette version de Pulse Chat)** : {names}", ""]
    folded = "\n".join(lines).strip()
    body = str(payload.get("body") or "").strip()
    combined = "\n\n".join(part for part in (folded, body) if part)
    return {
        "kind": payload["kind"],
        "channelSlug": payload["channelSlug"],
        "requestId": payload["requestId"],
        "title": payload["title"],
        "body": _bounded(combined, MAX_BODY_LENGTH),
    }


def has_structured(payload: Dict[str, Any]) -> bool:
    return any(key in payload for key in STRUCTURED_FIELDS)


def is_unknown_fields_refusal(status: int, body: Any) -> bool:
    """L'app a-t-elle refuse la trame parce qu'elle ne CONNAIT PAS un champ ?

    C'est le 400 d'une app anterieure aux rubriques (schema Zod strict,
    ``data`` = la liste des erreurs, dont une ``unrecognized_keys``). Tout
    autre 400 — reference introuvable, prefixe manquant, corps vide — porte un
    ``data.code`` et ne doit SURTOUT PAS declencher le repli : reposter sans les
    pieces jointes contournerait le refus d'une reference irresoluble, et
    l'approbateur trancherait sur un dossier ampute.
    """
    if status != 400 or not isinstance(body, dict):
        return False
    issues = body.get("data")
    if not isinstance(issues, list):
        return False
    return any(isinstance(i, dict) and i.get("code") == "unrecognized_keys" for i in issues)


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
    """Code et message d'un refus HTTP de l'app, pour les rendre tels quels.

    Un 400 de SCHEMA porte la liste des erreurs Zod au lieu d'un code : elle
    est resumee (champ : raison) sous ``invalid_request``, pour que le modele
    sache QUEL parametre corriger — « Payload agent invalide » ne le lui dirait
    pas.
    """
    code = "not_sent"
    message = f"HTTP {status}"
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, dict) and isinstance(data.get("code"), str):
            code = data["code"]
        text = body.get("statusMessage") or body.get("message")
        if isinstance(text, str) and text.strip():
            message = text.strip()
        if isinstance(data, list) and status == 400:
            code = "invalid_request"
            details = []
            for issue in data[:5]:
                if not isinstance(issue, dict):
                    continue
                path = ".".join(str(p) for p in issue.get("path") or []) or "(demande)"
                details.append(f"{path} : {issue.get('message') or issue.get('code') or '?'}")
            if details:
                message = f"{message} — " + " ; ".join(details)
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
    "invalid_request": (
        "Un parametre de ta demande est invalide (voir `message`). Corrige-le et "
        "rappelle l'outil. N'execute pas ce que tu voulais faire valider."
    ),
    "gate_body_required": (
        "La demande est vide : donne un `body` ou au moins une rubrique (`reason`, "
        "`steps`…), puis rappelle l'outil. N'execute pas ce que tu voulais faire valider."
    ),
    "gate_attachment_not_found": (
        "Une piece jointe est introuvable (voir `message`). Verifie le chemin avec tes "
        "outils de coffre, ou l'identifiant de la piece du message, puis resoumets. "
        "N'execute pas ce que tu voulais faire valider."
    ),
    "gate_attachment_invalid_ref": (
        "Une reference de piece jointe est mal formee : `vault:<chemin>` ou "
        "`message:<id>`, prefixe obligatoire. Corrige-la puis resoumets."
    ),
    "gate_attachment_pending": (
        "Une piece jointe n'est pas encore envoyee dans la conversation. Demande a "
        "l'humain d'envoyer son message, ou retire la piece, puis resoumets."
    ),
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
