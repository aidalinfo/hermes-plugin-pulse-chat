# -*- coding: utf-8 -*-
"""Sous-sessions du Tete-a-tete — partie PURE.

L'adaptateur reste mince : il TRANSPORTE. Ouvrir la sous-session (canal,
membres, agents, carte dans le fil), y poster la consigne, livrer le rapport
a la conversation principale : tout est fait par l'app Nuxt. Ce module ne
connait ni le WebSocket ni le HTTP — il compose les corps et ce que les
outils rendent au modele.

Contrat (app >= 0.51.0, docs/47) :

    plugin -> app   POST /api/agent/subsessions/open
        {channel, agents, title, brief, subsession?}
    200  {subsession, slug, status}

    plugin -> app   POST /api/agent/subsessions/report
        {channel, text}
    200  {subsession, status, delivered}

    refus h3 {statusMessage, data: {code, ...}} — ou data = {issues} Zod sur
    un 400 de schema. Une app trop ancienne rend 404 SANS code sur la route.

Quatre choix qui ne se devinent pas :

- **Le canal n'est pas un parametre** (meme regle que ``pulse_request_approval``,
  ``pulse_podcast`` et les outils de coffre) : il vient de
  ``gateway.session_context``. Pour l'ouverture, c'est le fil Tete-a-tete ;
  pour le rapport, c'est la SOUS-SESSION en cours. Un parametre ferait du
  choix du canal une sortie de LLM.
- **Aucune borne appliquee ici.** 1 a 5 agents, titre <= 120, consigne et
  rapport <= 8 000 caracteres sont ANNONCES dans le schema et la description,
  jamais appliques par coupe : une consigne tronquee serait le SEUL contexte
  des autres agents, amputee sans que personne ne le sache. L'app refuse avec
  un code nomme ; le modele reecrit.
- **``emitterProfile`` n'est jamais envoye**, comme partout dans ce plugin :
  l'en-tete de session (``x-hermes-session``) dit deja a l'app quel agent
  parle, et le schema de l'app est ``strict()``.
- **Les outils rendent AUSSITOT** : l'app repond sans attendre les autres
  agents (borne de 300 s d'Hermes). Les reponses arrivent dans la
  sous-session comme des messages ordinaires ; le rapport revient a la
  conversation principale comme un message entrant marque
  ``[Sous-session « titre » · rapport]`` — aucune trame nouvelle a gerer.
"""

import json
from typing import Any, Dict, List

from .gates import parse_error_body

#: Noms des outils. PREFIXES : ``register_tool`` sans ``override=True`` face a
#: un nom deja pris rend None sans lever — l'outil disparaitrait sans erreur.
OPEN_TOOL_NAME = "pulse_open_subsession"
REPORT_TOOL_NAME = "pulse_subsession_report"

#: Version minimale de l'app qui connait les routes. Documentaire : une app
#: plus ancienne rend 404 sans code, relaye en ``subsessions_unavailable``.
APP_MIN_VERSION = "0.51.0"

#: Bornes MIROIR de l'app (``shared/subsession.ts``), annoncees au modele —
#: JAMAIS appliquees par coupe ici, cf. docstring du module.
MAX_CALLEES = 5
MAX_TITLE_LENGTH = 120
MAX_BRIEF_LENGTH = 8_000
MAX_REPORT_LENGTH = 8_000

OPEN_TOOL_DESCRIPTION = (
    "Ouvre une SOUS-SESSION : une conversation separee, visible de la personne "
    "dans son fil, ou tu reunis 1 a "
    f"{MAX_CALLEES} autres agents pour un travail qui demande leurs competences. "
    "Tu decides seul de l'ouvrir quand le travail l'exige. Uniquement depuis une "
    "conversation Tete-a-tete (pas depuis une autre conversation, ni depuis une "
    "sous-session). Tu ne peux appeler QUE les agents que ton administrateur t'a "
    "autorise a appeler (reglage « Peut appeler ») : n'invente jamais un profil "
    "d'agent — en cas de refus `callee_not_allowed`, la liste `allowed` te donne "
    "ceux que tu peux appeler. La consigne (`brief`) est le SEUL contexte de la "
    "sous-session : les autres agents n'ont PAS lu la conversation, donc ecris-la "
    "COMPLETE (objectif, faits utiles, contraintes, livrable attendu). Les "
    "fichiers passent par le coffre, partage avec la conversation : ecris-les "
    "avec pulse_vault_write et cite leur chemin dans la consigne. Tu PARTICIPES a "
    "la sous-session : tu y recois les reponses des autres agents, comme dans une "
    "conversation separee, et la personne peut y ecrire. Quand le travail est "
    f"fait, appelle {REPORT_TOOL_NAME} DEPUIS la sous-session : c'est le seul "
    "chemin vers la conversation principale. L'outil rend aussitot (il n'attend "
    "pas les reponses). Pour relancer une sous-session deja ouverte, rappelle-le "
    "avec `subsession` = son slug (`ss-…`) et la nouvelle consigne, sans changer "
    f"les agents. Bornes : 1 a {MAX_CALLEES} agents, titre <= {MAX_TITLE_LENGTH} "
    f"caracteres, consigne <= {MAX_BRIEF_LENGTH} caracteres (rien n'est coupe : "
    "hors bornes, la demande est refusee et tu reecris). Le canal est celui de la "
    "conversation en cours : tu ne le passes pas. Issue : `opened` / `relaunched` "
    "(dis brievement a la personne que la sous-session est lancee), `refused` "
    "(lis `code`, `message` et `next`)."
)

OPEN_TOOL_SCHEMA: Dict[str, Any] = {
    "name": OPEN_TOOL_NAME,
    "description": OPEN_TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "agents": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_CALLEES,
                "items": {"type": "string"},
                "description": (
                    "Profils des agents a reunir (ex. [\"atlas\"]), 1 a "
                    f"{MAX_CALLEES}, sans toi-meme ni doublon. Seulement des agents "
                    "que tu as le droit d'appeler. Pour une relance : vide, ou la "
                    "meme liste."
                ),
            },
            "title": {
                "type": "string",
                "maxLength": MAX_TITLE_LENGTH,
                "description": (
                    "Titre de la sous-session, en une ligne (ex. « Chiffrage du "
                    "devis Martin »). Affiche a la personne."
                ),
            },
            "brief": {
                "type": "string",
                "maxLength": MAX_BRIEF_LENGTH,
                "description": (
                    "Consigne COMPLETE : le seul contexte des autres agents. "
                    "Objectif, faits utiles, contraintes, livrable attendu, chemins "
                    "de coffre des fichiers utiles."
                ),
            },
            "subsession": {
                "type": "string",
                "description": (
                    "Facultatif : slug (`ss-…`) d'une sous-session que tu as deja "
                    "ouverte depuis cette conversation, pour la RELANCER avec une "
                    "nouvelle consigne au lieu d'en ouvrir une autre."
                ),
            },
        },
        "required": ["agents", "title", "brief"],
    },
}

REPORT_TOOL_DESCRIPTION = (
    "Rapporte le resultat d'une sous-session a la conversation principale. A "
    "appeler DEPUIS la sous-session que tu as ouverte avec "
    f"{OPEN_TOOL_NAME}, quand le travail est fait : c'est le SEUL chemin vers la "
    "conversation principale (ce que tu ecris dans la sous-session n'y arrive "
    "pas). Ecris une synthese qui se suffit (resultat, decisions, chemins de "
    "coffre des fichiers produits). Le rapport t'est livre dans la conversation "
    "principale comme un message entrant marque « [Sous-session « titre » · "
    "rapport] » : tu y reponds alors normalement a la personne. Plusieurs "
    "rapports successifs sont permis. Borne : texte <= "
    f"{MAX_REPORT_LENGTH} caracteres (rien n'est coupe : au-dela, refuse et tu "
    "resserres). Le canal est la sous-session en cours : tu ne le passes pas. "
    "Issue : `reported` (`delivered: false` n'est PAS un echec : l'app le "
    "livrera des que possible), `refused` (lis `code`, `message` et `next`)."
)

REPORT_TOOL_SCHEMA: Dict[str, Any] = {
    "name": REPORT_TOOL_NAME,
    "description": REPORT_TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "maxLength": MAX_REPORT_LENGTH,
                "description": (
                    "La synthese du travail de la sous-session, qui se suffit a "
                    "elle-meme."
                ),
            },
        },
        "required": ["text"],
    },
}


def open_url(base_url: str) -> str:
    return "%s/api/agent/subsessions/open" % base_url.rstrip("/")


def report_url(base_url: str) -> str:
    return "%s/api/agent/subsessions/report" % base_url.rstrip("/")


def _text(value: Any) -> str:
    # ``strip`` seulement : aucune coupe (cf. docstring du module).
    return value.strip() if isinstance(value, str) else ""


def _agents(value: Any) -> List[str]:
    """Liste des profils, nettoyee de ses blancs. Mise en forme, jamais
    jugement : un doublon ou l'appelant lui-meme PARTENT (l'app refuse avec un
    code nomme). Une chaine seule devient une liste d'un element ; une entree
    vide ou non textuelle est du bruit, jetee."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [_text(v) for v in value if _text(v)]


def build_open_payload(channel: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Corps de ``POST /api/agent/subsessions/open``. PURE — aucune I/O.

    ``channel`` vient du contexte de session, JAMAIS de ``args`` : un
    ``channel`` glisse par le modele n'est pas lu. ``subsession`` est omis
    quand il est vide (ouverture) plutot qu'envoye a ``""``, que l'app
    refuserait.
    """
    args = args if isinstance(args, dict) else {}
    payload: Dict[str, Any] = {
        "channel": channel,
        "agents": _agents(args.get("agents")),
        "title": _text(args.get("title")),
        "brief": _text(args.get("brief")),
    }
    subsession = _text(args.get("subsession"))
    if subsession:
        payload["subsession"] = subsession
    return payload


def build_report_payload(channel: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Corps de ``POST /api/agent/subsessions/report``. PURE — aucune I/O."""
    args = args if isinstance(args, dict) else {}
    return {"channel": channel, "text": _text(args.get("text"))}


def _dump(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def _str_or_none(value: Any) -> Any:
    return value if isinstance(value, str) and value else None


def opened_result(body: Any, *, relaunch: bool) -> str:
    """L'app a ouvert (ou relance) la sous-session."""
    body = body if isinstance(body, dict) else {}
    slug = _str_or_none(body.get("slug"))
    if relaunch:
        next_step = (
            "La nouvelle consigne est postee dans la sous-session ; les reponses "
            "des autres agents t'y arriveront. Quand le travail est fait, appelle "
            f"{REPORT_TOOL_NAME} depuis la sous-session."
        )
    else:
        next_step = (
            "La sous-session est ouverte ; dis brievement a la personne que tu l'as "
            "lancee. Les reponses des autres agents t'arriveront DANS la "
            "sous-session ; quand le travail est fait, appelle "
            f"{REPORT_TOOL_NAME} depuis la sous-session. Pour la relancer plus "
            f"tard : {OPEN_TOOL_NAME} avec subsession=\"{slug or 'son slug'}\"."
        )
    return _dump(
        {
            "status": "relaunched" if relaunch else "opened",
            "subsession": _str_or_none(body.get("subsession")),
            "slug": slug,
            "subsessionStatus": _str_or_none(body.get("status")),
            "next": next_step,
        }
    )


def reported_result(body: Any) -> str:
    """L'app a enregistre le rapport. ``delivered: false`` n'est pas un echec."""
    body = body if isinstance(body, dict) else {}
    delivered = body.get("delivered") is True
    return _dump(
        {
            "status": "reported",
            "subsession": _str_or_none(body.get("subsession")),
            "subsessionStatus": _str_or_none(body.get("status")),
            "delivered": delivered,
            "next": (
                "Le rapport est enregistre et livre a la conversation principale, "
                "ou tu y repondras a la personne. Rien d'autre a faire ici."
                if delivered
                else "Le rapport est enregistre ; l'app le livrera a la "
                "conversation principale des que possible (ce n'est PAS un echec). "
                "Ne le renvoie pas."
            ),
        }
    )


def refused_result(code: str, message: str, extra: Dict[str, Any] = None) -> str:
    """La demande n'a pas ete acceptee. Jamais de succes sur ce chemin."""
    out: Dict[str, Any] = {"status": "refused", "code": code, "message": message}
    out.update(extra or {})
    out["next"] = _ADVICE.get(code, _ADVICE["not_sent"])
    return _dump(out)


#: Statuts SANS ``data.code`` : le code est derive du statut. Un 404 sans code
#: est la route inconnue d'une app trop ancienne — ``channel_not_found`` et
#: ``subsession_not_found``, eux, portent leur code.
_STATUS_CODES = {
    401: "not_authorized",
    403: "not_authorized",
    404: "subsessions_unavailable",
}

#: Champs de ``data`` que l'app joint a certains refus et que le modele doit
#: LIRE pour corriger au coup suivant (``allowed`` : les agents appelables).
_RELAYED_DATA = ("allowed", "notAllowed", "agent", "max")


def http_refusal(status: int, body: Any) -> str:
    """Refus HTTP de l'app, rendu avec son code, son MESSAGE et ses donnees.

    Le code de l'app (``data.code``) prime. Un 400 de schema porte
    ``data.issues`` (liste Zod) : il est resume par ``parse_error_body`` (champ
    : raison) sous ``invalid_request``, pour que le modele sache QUEL parametre
    corriger.
    """
    data = body.get("data") if isinstance(body, dict) else None
    if (
        status == 400
        and isinstance(data, dict)
        and not isinstance(data.get("code"), str)
        and isinstance(data.get("issues"), list)
    ):
        # ``parse_error_body`` lit les issues Zod a plat dans ``data`` (forme
        # des autres routes agent) ; celle-ci les range sous ``data.issues``.
        parsed = parse_error_body(status, dict(body, data=data["issues"]))
    else:
        parsed = parse_error_body(status, body)
    code = parsed["code"]
    message = parsed["message"]
    if code == "not_sent":
        code = _STATUS_CODES.get(status) or (
            "app_unavailable" if status == 0 or status >= 500 else "not_sent"
        )
    extra: Dict[str, Any] = {}
    if isinstance(data, dict):
        for key in _RELAYED_DATA:
            if key in data:
                extra[key] = data[key]
    if code == "callee_not_allowed" and not isinstance(extra.get("allowed"), list):
        # Sans liste, le modele devinerait un autre nom : on dit qu'il n'y en a
        # aucune plutot que de taire le champ.
        extra["allowed"] = []
    return refused_result(code, message, extra)


_ADVICE = {
    "invalid_request": (
        "Un parametre est invalide (voir `message`) : corrige-le (agents, title, "
        "brief) et rappelle l'outil."
    ),
    "agent_session_required": (
        "Ton identite d'agent n'est pas encore etablie (reconnexion en cours ?). "
        "Reessaie une fois dans quelques secondes ; sinon dis-le a la personne."
    ),
    "channel_not_found": (
        "La conversation est introuvable ou fermee pour l'app. Dis-le a la "
        "personne ; ne reessaie pas."
    ),
    "subsession_not_allowed_here": (
        "Une sous-session ne s'ouvre que depuis une conversation Tete-a-tete que "
        "tu sers, et jamais depuis une sous-session. Fais le travail toi-meme ici, "
        "ou dis a la personne que tu ne peux pas solliciter d'autre agent depuis "
        "cette conversation."
    ),
    "callee_not_allowed": (
        "Tu ne peux appeler que les agents de la liste `allowed` (profils exacts). "
        "Rappelle l'outil avec l'un d'eux s'il convient. Si `allowed` est vide ou "
        "qu'aucun ne convient, n'invente pas de profil : dis a la personne qu'un "
        "administrateur doit t'autoriser a appeler cet agent (reglage « Peut "
        "appeler »)."
    ),
    "callee_unreachable": (
        "L'agent nomme dans `agent` est desactive ou n'est pas ouvert a la personne "
        "de cette conversation. Ne l'appelle pas ; dis-le a la personne, ou "
        "choisis un autre agent autorise."
    ),
    "callees_required": (
        f"Nomme au moins un agent dans `agents` (1 a {MAX_CALLEES}), puis rappelle "
        "l'outil."
    ),
    "too_many_callees": (
        f"Au plus {MAX_CALLEES} agents par sous-session : garde les plus utiles et "
        "rappelle l'outil."
    ),
    "duplicate_callee": "Un agent apparait deux fois dans `agents` : retire le doublon et rappelle l'outil.",
    "opener_cannot_be_callee": (
        "Tu ne peux pas t'appeler toi-meme : retire ton propre profil de `agents` "
        "et rappelle l'outil."
    ),
    "title_required": "Donne un titre d'une ligne (`title`) et rappelle l'outil.",
    "title_too_long": (
        f"Titre trop long (<= {MAX_TITLE_LENGTH} caracteres, voir `max`) : "
        "raccourcis-le et rappelle l'outil."
    ),
    "brief_required": (
        "La consigne (`brief`) est vide : ecris-la COMPLETE (les autres agents "
        "n'ont pas lu la conversation) et rappelle l'outil."
    ),
    "brief_too_long": (
        f"Consigne trop longue (<= {MAX_BRIEF_LENGTH} caracteres, voir `max`) : "
        "resserre-la — mets les longs documents au coffre et cite leur chemin — "
        "puis rappelle l'outil."
    ),
    "subsession_not_found": (
        "Cette sous-session n'existe pas, n'a pas ete ouverte depuis cette "
        "conversation, ou pas par toi. Verifie le slug (`ss-…`) rendu a "
        "l'ouverture, ou ouvre-en une nouvelle sans `subsession`."
    ),
    "subsession_closed": (
        "Cette sous-session est close : elle ne recoit plus ni consigne ni "
        "rapport. Ouvre-en une nouvelle si le travail doit continuer, ou dis-le a "
        "la personne."
    ),
    "subsession_callees_fixed": (
        "Une relance ne change pas les agents : rappelle l'outil avec `agents` "
        "vide (ou la meme liste). Pour d'autres agents, ouvre une nouvelle "
        "sous-session sans `subsession`."
    ),
    "not_subsession_opener": (
        "Le rapport ne s'envoie que DEPUIS une sous-session que tu as ouverte. Si "
        "tu es dans la conversation principale, reponds-y directement a la "
        "personne ; si un autre agent a ouvert cette sous-session, c'est a lui de "
        "rapporter — ecris simplement ta reponse ici."
    ),
    "text_required": "Le rapport (`text`) est vide : ecris la synthese et rappelle l'outil.",
    "text_too_long": (
        f"Rapport trop long (<= {MAX_REPORT_LENGTH} caracteres, voir `max`) : "
        "resserre la synthese — les documents longs vont au coffre, cite leur "
        "chemin — puis rappelle l'outil."
    ),
    "emitter_ambiguous": (
        "L'app ne sait pas quel agent tu es (plusieurs profils possibles). Dis-le "
        "a la personne : c'est un reglage du bot (secret d'agent)."
    ),
    "not_authorized": (
        "L'app refuse l'identite de ce bot. Dis-le a la personne : c'est un "
        "reglage du bot, pas de la demande."
    ),
    "subsessions_unavailable": (
        "Cette instance de Pulse Chat ne connait pas les sous-sessions (app "
        f"< {APP_MIN_VERSION}). Fais le travail toi-meme, ou dis-le a la "
        "personne ; ne reessaie pas."
    ),
    "no_channel": "Cet outil ne marche que dans une conversation Pulse Chat.",
    "not_connected": (
        "Pulse Chat est injoignable pour l'instant. Dis-le a la personne ; ne "
        "pretends pas que la demande est partie."
    ),
    "app_unavailable": (
        "L'app Pulse Chat a echoue. Reessaie une fois ; sinon dis-le a la "
        "personne. Ne pretends pas que la demande est partie."
    ),
    "not_sent": (
        "La demande n'a pas abouti. Dis-le a la personne ; ne pretends pas "
        "qu'elle est partie."
    ),
}
