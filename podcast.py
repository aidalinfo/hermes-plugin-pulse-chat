# -*- coding: utf-8 -*-
"""Podcast fabrique PAR L'APP a partir des chapitres de l'agent — partie PURE.

L'adaptateur reste mince : il TRANSPORTE. La synthese (voix Voxtral), le
depot au coffre et la carte dans le fil sont faits par l'app Nuxt ; ce module
ne connait ni le WebSocket ni le HTTP — il compose le payload et ce que
l'outil rend au modele.

Contrat (app >= 0.44.0) :

    plugin -> app   POST /api/agent/podcasts/<canal>
        {title, chapters: [{title, summary?, text}]}

    202  {podcastId, status: "queued"}
    refus h3 {statusMessage, data: {code}} — ou data = issues Zod sur un 400
    de schema.

Trois choix qui ne se devinent pas :

- **Aucune troncature, aucune borne appliquee ici.** Les bornes (titre,
  nombre de chapitres, volume de texte) sont ANNONCEES au modele dans le
  schema et la description, jamais appliquees par coupe : un podcast tronque
  ferait s'arreter la voix au milieu d'une phrase, sans que personne ne sache
  pourquoi. L'app refuse avec un code nomme (``podcast_text_too_long``...) et
  la borne dans son message ; le modele reecrit plus court. C'est l'inverse de
  ``gates.py``, qui coupe un corps d'approbation — la ou une coupe se lit,
  une coupe ne s'entend pas.
- **Le canal n'est pas un parametre** (meme regle que ``pulse_request_approval``
  et les outils de coffre) : il vient de ``gateway.session_context``. Un
  parametre ferait du choix du canal une sortie de LLM.
- **L'outil rend AUSSITOT ``queued``**, il n'attend pas la synthese : elle
  prend plusieurs minutes, au-dela du plafond de 300 s que
  ``model_tools._run_async`` impose a un outil asynchrone. La carte apparait
  d'elle-meme dans le fil quand l'app a fini — l'agent n'a rien a rappeler.
"""

import json
from typing import Any, Dict, List
from urllib.parse import quote

from .gates import parse_error_body

#: Nom de l'outil. PREFIXE : ``register_tool`` sans ``override=True`` face a un
#: nom deja pris (un ``podcast`` d'un autre plugin, un jour) rend None sans
#: lever — l'outil disparaitrait sans erreur visible.
PODCAST_TOOL_NAME = "pulse_podcast"

#: Version minimale de l'app qui connait la route. Documentaire : une app plus
#: ancienne rend 404 sur la route, relaye tel quel au modele.
APP_MIN_VERSION = "0.44.0"

#: Bornes MIROIR de l'app, annoncees au modele (``maxLength`` / ``maxItems``) —
#: JAMAIS appliquees par coupe ici, cf. docstring du module.
MAX_PODCAST_TITLE_LENGTH = 120
MAX_CHAPTERS = 20
MAX_CHAPTER_TITLE_LENGTH = 120
MAX_CHAPTER_SUMMARY_LENGTH = 200
MIN_PODCAST_TEXT_LENGTH = 200
MAX_PODCAST_TEXT_LENGTH = 15_000

PODCAST_TOOL_DESCRIPTION = (
    "Fabrique un PODCAST (monologue lu par ta voix) a partir de chapitres de "
    "texte que tu ecris, dans la conversation Pulse Chat en cours. A appeler "
    "quand un humain demande un podcast, un resume audio, ou quelque chose « a "
    "ecouter ». C'est l'app qui fait la synthese vocale : le podcast apparait "
    "DE LUI-MEME dans la conversation (une carte avec lecteur, chapitres et "
    "transcription) quand il est pret, en quelques minutes. NE recopie PAS le "
    "texte en message et ne le lis pas a voix haute : la carte le porte deja. "
    "Tu peux ecrire une courte phrase d'accompagnement (« je te prepare ca »). "
    "Ecris chaque chapitre en PROSE PARLEE, comme on le dirait a voix haute : "
    "pas de Markdown, pas de listes a puces, pas de tableaux, pas d'URL — tout "
    "ce qui s'ecrit se lit mot pour mot. Bornes : titre <= "
    f"{MAX_PODCAST_TITLE_LENGTH} caracteres, 1 a {MAX_CHAPTERS} chapitres, "
    f"titre de chapitre <= {MAX_CHAPTER_TITLE_LENGTH}, resume <= "
    f"{MAX_CHAPTER_SUMMARY_LENGTH}, et le TOTAL des `text` entre "
    f"{MIN_PODCAST_TEXT_LENGTH} et {MAX_PODCAST_TEXT_LENGTH} caracteres (rien "
    "n'est coupe : hors bornes, la demande est refusee et tu reecris). Le canal "
    "est celui de la conversation en cours : tu ne le passes pas. Reprendre le "
    "MEME titre cree une nouvelle version de la meme carte. Issue : `queued` "
    "(en preparation, rien d'autre a faire), `refused` (lis `code` et "
    "`message` ; corrige si c'est un parametre, sinon explique-le a l'humain)."
)

PODCAST_TOOL_SCHEMA: Dict[str, Any] = {
    "name": PODCAST_TOOL_NAME,
    "description": PODCAST_TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "maxLength": MAX_PODCAST_TITLE_LENGTH,
                "description": (
                    "Titre du podcast, en une ligne (ex. « Le point de la semaine "
                    "— 30 septembre »). Le meme titre met a jour la meme carte."
                ),
            },
            "chapters": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_CHAPTERS,
                "description": (
                    "Les chapitres, dans l'ordre d'ecoute. Chacun devient un repere "
                    "du lecteur."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "maxLength": MAX_CHAPTER_TITLE_LENGTH,
                            "description": "Titre du chapitre (ex. « Les chiffres du mois »).",
                        },
                        "summary": {
                            "type": "string",
                            "maxLength": MAX_CHAPTER_SUMMARY_LENGTH,
                            "description": (
                                "Facultatif : une phrase qui resume le chapitre, "
                                "affichee sous son titre (non lue)."
                            ),
                        },
                        "text": {
                            "type": "string",
                            "description": (
                                "Ce qui sera DIT, en prose parlee : phrases completes, "
                                "sans Markdown, puces, tableaux ni URL. Le total des "
                                f"`text` va de {MIN_PODCAST_TEXT_LENGTH} a "
                                f"{MAX_PODCAST_TEXT_LENGTH} caracteres."
                            ),
                        },
                    },
                    "required": ["title", "text"],
                },
            },
        },
        "required": ["title", "chapters"],
    },
}


def podcast_url(base_url: str, channel_slug: str) -> str:
    """URL de la route de podcast, slug ENCODE (un slug n'a pas a en avoir
    besoin, mais une URL composee a la main finit toujours par en recevoir un)."""
    return "%s/api/agent/podcasts/%s" % (base_url.rstrip("/"), quote(channel_slug, safe=""))


def _text(value: Any) -> str:
    # ``strip`` seulement : aucune coupe (cf. docstring du module).
    return value.strip() if isinstance(value, str) else ""


def build_podcast_payload(args: Dict[str, Any]) -> Dict[str, Any]:
    """Corps du POST, a partir des arguments du modele. PURE — aucune I/O.

    Mise en forme, jamais jugement : les chaines sont nettoyees de leurs
    blancs de bord, un ``summary`` vide n'est pas envoye (l'app le lit alors
    comme absent, plutot qu'une ligne vide sous le titre), et une entree qui
    n'est pas un objet est jetee — c'est du bruit, pas un chapitre. Un titre ou
    un texte vide PART tel quel : c'est l'app qui refuse, avec un code que le
    modele sait lire.
    """
    raw_chapters = args.get("chapters") if isinstance(args, dict) else None
    chapters: List[Dict[str, str]] = []
    for entry in raw_chapters if isinstance(raw_chapters, list) else []:
        if not isinstance(entry, dict):
            continue
        chapter = {"title": _text(entry.get("title")), "text": _text(entry.get("text"))}
        summary = _text(entry.get("summary"))
        if summary:
            chapter["summary"] = summary
        chapters.append(chapter)
    return {"title": _text(args.get("title") if isinstance(args, dict) else None), "chapters": chapters}


def _dump(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def queued_result(podcast_id: Any) -> str:
    """L'app a accepte la demande : la synthese est en file."""
    return _dump(
        {
            "status": "queued",
            "podcastId": podcast_id if isinstance(podcast_id, str) and podcast_id else None,
            "next": (
                "Le podcast est en préparation ; il apparaîtra de lui-même dans la "
                "conversation. Ne le recopie pas."
            ),
        }
    )


def refused_result(code: str, message: str) -> str:
    """La demande n'a pas ete acceptee. Jamais de ``queued`` sur ce chemin."""
    return _dump(
        {
            "status": "refused",
            "code": code,
            "message": message,
            "next": _ADVICE.get(code, _ADVICE["not_sent"]),
        }
    )


#: Statuts SANS ``data.code`` (403 de garde de session, pannes) : le code est
#: derive du statut, sinon « HTTP 403 » seul ne dirait pas au modele s'il doit
#: corriger un parametre ou prevenir un humain.
_STATUS_CODES = {
    403: "not_authorized",
    404: "channel_not_found",
    501: "podcast_synthesis_unavailable",
}


def http_refusal(status: int, body: Any) -> str:
    """Refus HTTP de l'app, rendu avec son code et son MESSAGE.

    Le code de l'app (``data.code``) prime ; un 400 de schema est resume par
    ``parse_error_body`` (champ : raison) sous ``invalid_request`` — la meme
    lecture que pour une demande d'approbation, pour que le modele sache QUEL
    parametre corriger.
    """
    parsed = parse_error_body(status, body)
    code = parsed["code"]
    message = parsed["message"]
    if code == "not_sent":
        code = _STATUS_CODES.get(status) or (
            "app_unavailable" if status == 0 or status >= 500 else "not_sent"
        )
    if code == "podcast_synthesis_unavailable":
        # Le message de l'app peut etre technique (variable d'env absente) :
        # celui-ci est ce que le modele peut redire tel quel a l'humain.
        message = "Synthèse indisponible sur cette instance : aucun moteur de voix n'y est configuré."
    return refused_result(code, message)


_ADVICE = {
    "invalid_request": (
        "Un parametre est invalide (voir `message`) : corrige-le (titre, chapitres "
        "avec `title` et `text`) et rappelle l'outil."
    ),
    "podcast_text_too_short": (
        "Le texte total est trop court (la borne est dans `message`) : developpe les "
        "chapitres en prose parlee, puis rappelle l'outil."
    ),
    "podcast_text_too_long": (
        "Le texte total est trop long (la borne est dans `message`) : resserre ou "
        "retire des chapitres, puis rappelle l'outil. Ne decoupe pas en plusieurs "
        "podcasts sans le proposer a l'humain."
    ),
    "voice_disabled": (
        "Ta voix est desactivee dans Pulse Chat : aucun podcast ne peut etre lu. "
        "Dis-le a l'humain — un administrateur peut l'activer dans les reglages de "
        "l'agent. Ne recopie pas le texte a la place sans le lui proposer."
    ),
    "podcast_in_progress": (
        "Un de tes podcasts est deja en preparation dans cette conversation. Attends "
        "qu'il apparaisse avant d'en demander un autre, et dis-le a l'humain."
    ),
    "podcast_synthesis_unavailable": (
        "Cette instance de Pulse Chat ne sait pas fabriquer de podcast. Dis-le a "
        "l'humain ; ne reessaie pas."
    ),
    "channel_not_found": (
        "La conversation est introuvable pour l'app (ou l'app est trop ancienne pour "
        f"connaitre les podcasts, < {APP_MIN_VERSION}). Dis-le a l'humain."
    ),
    "not_authorized": (
        "Ton agent ne sert pas cette conversation pour l'app. Dis-le a l'humain : un "
        "administrateur doit le rattacher au canal."
    ),
    "agent_session_required": (
        "Ton identite d'agent n'est pas encore etablie (reconnexion en cours ?). "
        "Reessaie une fois dans quelques secondes ; sinon dis-le a l'humain."
    ),
    "emitter_ambiguous": (
        "L'app ne sait pas quel agent tu es dans cette conversation (plusieurs "
        "profils possibles). Dis-le a l'humain : c'est un reglage du bot."
    ),
    "no_channel": "Cet outil ne marche que dans une conversation Pulse Chat.",
    "not_connected": (
        "Pulse Chat est injoignable pour l'instant. Dis a l'humain que le podcast n'a "
        "pas pu etre demande ; ne pretends pas qu'il arrive."
    ),
    "app_unavailable": (
        "L'app Pulse Chat a echoue. Reessaie une fois ; sinon dis a l'humain que le "
        "podcast n'a pas pu etre demande. Ne pretends pas qu'il arrive."
    ),
    "not_sent": (
        "La demande n'a pas abouti. Dis-le a l'humain ; ne pretends pas que le "
        "podcast arrive."
    ),
}
