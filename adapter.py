"""Adaptateur de plateforme Pulse Chat pour Hermes Agent.

Adaptateur MINCE : il traduit et transporte, aucune logique metier (elle vit
dans l'app Nuxt — cf. CLAUDE.md et spec §6).

Flux :
- app -> plugin : WebSocket ``ws(s)://<PULSE_CHAT_URL>/ws/hermes`` avec
  ``Authorization: Bearer <PULSE_CHAT_TOKEN>`` (librairie ``websockets`` —
  ``pip install websockets``). Evenements
  ``message.created`` -> ``MessageEvent`` -> ``self.handle_message`` -> ack
  ``{"type": "ack", "messageId": ...}`` (le serveur marque ``agentDeliveredAt``
  et rejoue les messages non ackes a la reconnexion).
- plugin -> app : POST ``<PULSE_CHAT_URL>/api/agent/messages`` (Bearer
  ``PULSE_CHAT_TOKEN``), body ``{channelSlug, kind, content, raw,
  hermesMessageId, tool, phase, replyToHermesId}``. HTTP via urllib stdlib
  dans ``asyncio.to_thread`` (pas de dependance supplementaire).

La classification message / tool_event est deleguee au module pur
``classification`` (testable sans hermes installe).

Configuration (env > config.extra) :
    PULSE_CHAT_URL              URL de base de l'app (http(s)://...)
    PULSE_CHAT_TOKEN            token de service (WS + API)
    PULSE_CHAT_PROFILE          optionnel — profil(s) Hermes servis par ce bot,
                                separes par des virgules (defaut: "default")
    PULSE_CHAT_AGENT_NAME       optionnel — nom d'affichage envoye dans la frame
                                hello (defaut: nom du premier profil)
    PULSE_CHAT_CHANNELS         optionnel — slugs autorises, separes par des virgules
    PULSE_CHAT_ALLOW_ALL_USERS  optionnel — true (l'app filtre deja via ChannelMember)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import mimetypes
import os
import pathlib
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import weakref
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from .approvals import (
    GATEWAY_APPROVAL_TOOL,
    build_approval_payload,
    gateway_options,
    parse_approval_reply,
    refusal,
)
from .gates import (
    GATE_SKILL_NAME,
    GATE_TOOL_DESCRIPTION,
    GATE_TOOL_NAME,
    GATE_TOOL_SCHEMA,
    GATE_WAIT_SECONDS,
    build_gate_payload,
    has_structured,
    is_unknown_fields_refusal,
    legacy_payload,
    structured_fields,
    decision_message_text,
    parse_error_body,
    parse_gate_reply,
    pending_result,
    refused_result,
    tool_result,
)
from .questions import (
    build_question_payload,
    build_question_retire_payload,
    parse_question_reply,
)
from .artifacts import (
    build_artifact_payload,
    is_artifact_kind,
    normalize_title,
    default_artifact_id,
    default_artifact_path,
)
from .audio_stream import (
    abort_frame,
    audio_capability_from_ack,
    begin_frame,
    capability_accepts,
    encode_audio_frame,
    end_frame,
)
from . import browser as browser_provider
from .capabilities import collect_capabilities
from .connectors import (
    ConnectorCapabilityError,
    build_connector_payload,
    connector_url,
    parse_connector_error,
)
from .vault import VaultPathError, normalize_vault_path, vault_url
from .workspace import (
    PUBLISH_DESCRIPTION,
    PUBLISH_SCHEMA,
    PUBLISH_TOOL_NAME,
    UPLOAD_TIMEOUT_SECONDS,
    VAULT_WRITE_DESCRIPTION,
    VAULT_WRITE_SCHEMA,
    VAULT_WRITE_TOOL_NAME,
    WorkspaceToolError,
    content_type_for,
    exclusive_source,
    http_refusal,
    published_result,
    refused_result as workspace_refused,
    resolve_local_file,
    written_result,
)
from .voice import (
    MAX_VOICE_BYTES,
    caption_header,
    guess_audio_mime,
    is_audio_mime,
    voice_filename,
    voice_url,
)
from .classification import classify_outbound, parse_tool  # noqa: F401  (re-export)
from .hello import build_hello, parse_profiles
from .metrics import extract_metrics
from .todos import (
    TODO_TOOL_NAMES,
    build_todo_payload,
    parse_todo_result,
    raw_of,
    todo_message_id,
)
from .reconnect import is_retryable_ws_error, reconnect_delay

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.config import Platform

try:  # Hermes >= v0.20 — contrat de streaming audio (#60671)
    from gateway.platforms.base import StreamingTTSHandle

    _HAS_STREAMING_CONTRACT = True
except ImportError:  # pragma: no cover - Hermes anterieur au contrat
    # Import SEPARE du bloc ci-dessus, et c'est volontaire : le regrouper
    # ferait echouer tout l'import du plugin sur une version d'Hermes qui ne
    # connait pas encore le contrat — le bot perdrait le chat, pas seulement la
    # voix. Ici, les 5 methodes restent definies mais ne servent jamais : sans
    # `StreamingTTSConsumer` en face, personne ne les appelle.
    from dataclasses import dataclass, field

    @dataclass
    class StreamingTTSHandle:  # type: ignore[no-redef]
        chat_id: str = ""
        audio_format: Any = None
        audible: bool = False
        aborted: bool = False

    _HAS_STREAMING_CONTRACT = False

try:  # File d'attente du garde-fou d'Hermes (approbation de commande dangereuse)
    from tools.approval import resolve_gateway_approval
except ImportError:  # pragma: no cover - Hermes trop ancien / hors gateway
    # Import GARDE et non dur : sans lui, tout l'import du plugin echouerait et
    # le bot perdrait le chat entier pour une fonction d'appoint. L'absence est
    # traitee dans ``send_exec_approval``, qui rend alors la main a Hermes pour
    # qu'il reprenne son invite texte — cf. le commentaire la-bas.
    resolve_gateway_approval = None  # type: ignore[assignment]

try:  # Primitive ``clarify`` d'Hermes (question posee a l'humain)
    from tools.clarify_gateway import resolve_gateway_clarify
except ImportError:  # pragma: no cover - Hermes trop ancien / hors gateway
    # Import GARDE, meme raison que ``resolve_gateway_approval`` juste au-dessus :
    # sans lui tout l'import du plugin echouerait et le bot perdrait le chat
    # entier. L'absence est traitee dans ``send_clarify``, qui rend alors la
    # main a Hermes pour qu'il reprenne son invite texte numerotee.
    resolve_gateway_clarify = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: Correlations ``requestId -> session_key`` retenues en attendant la decision.
#: Bornee : le garde-fou d'Hermes abandonne au bout de son propre delai (300 s
#: par defaut) et la trame correspondante n'arrivera parfois jamais.
_MAX_GATEWAY_APPROVALS = 100

#: Correlations ``clarify_id -> channel_slug`` des questions posees en carte.
#: Bornee pour la meme raison : Hermes relache son attente de son cote (delai,
#: /new, prose libre qui supplante la question) et la reponse n'arrivera
#: parfois jamais. ``retire_clarify_card`` ne recoit PAS le chat_id — c'est
#: cette table qui le retrouve, sans quoi le retrait ne saurait pas ou poster.
_MAX_CLARIFY_CARDS = 100

_HTTP_TIMEOUT = 15.0
_WS_CONNECT_TIMEOUT = 30.0
_WS_MAX_SIZE = 10 * 1024 * 1024
_MEDIA_MAX_BYTES = 20 * 1024 * 1024
_MEDIA_MAX_COUNT = 10
# Dedup du rejeu serveur : cache borne des derniers message ids traites (un
# ``message.created`` deja vu est re-acke mais PAS re-dispatche a l'agent).
_DEDUP_MAX_IDS = 500

#: Adaptateurs vivants de ce process. Le handler de ``pulse_request_approval``
#: est une fonction de MODULE (``register_tool`` le recoit avant qu'aucun
#: adaptateur n'existe) : c'est par ici qu'il retrouve celui qui tient le
#: WebSocket. Faible, pour ne rien retenir d'un adaptateur que Hermes a jete.
_LIVE_ADAPTERS: "weakref.WeakSet[Any]" = weakref.WeakSet()
# Identite de l'emetteur : le serveur repond au ``hello`` par une trame
# ``hello.ack`` portant un jeton OPAQUE lie a cette connexion. Le plugin le
# memorise et le REJOINT a ses appels sortants (en-tete HTTP ci-dessous + champ
# ``sessionToken`` de l'ack WS). Il ne le lit pas, n'en derive rien et n'arbitre
# jamais « ce message m'est-il destine » : il transporte.
_HELLO_ACK_TYPE = "hello.ack"
_SESSION_HEADER = "x-hermes-session"
# Format audio par defaut du contrat Hermes, et borne sur les flux ouverts
# simultanement (un par tour de parole ; au-dela, une fin de flux s'est perdue).
DEFAULT_SAMPLE_RATE = 24000
_AUDIO_STREAMS_MAX = 8

#: Tours dont une carte de plan a deja ete postee (cf. ``_forward_todo_plan``).
#: Borne : un tour ne se « referme » jamais explicitement cote plugin.
_MAX_TODO_TURNS = 200

#: Le streamer Voxtral a-t-il fini par s'enregistrer ? `None` = pas encore su.
_voxtral_registered: Optional[bool] = None


def _ensure_voxtral_streamer() -> bool:
    """Enregistre le streamer Voxtral s'il ne l'est pas deja. Idempotent.

    L'enregistrement a lieu normalement a l'import du plugin (`__init__.py`).
    Il peut echouer pour une raison qui n'a rien de definitif : le contrat de
    streaming (`tools.tts_streaming`) n'etait pas encore importable a cet
    instant. Le symptome est alors invisible et couteux — Hermes ouvre une
    piste audio, ne trouve aucun fournisseur pour la remplir, et la referme
    sans un octet. Vu en production.

    On retente donc UNE FOIS au premier tour parle, et on journalise le
    resultat : un silence de plus a cet endroit ne serait pas acceptable.
    """
    global _voxtral_registered
    if _voxtral_registered:
        return True
    try:
        from .voxtral_streaming import install as _install

        _voxtral_registered = bool(_install())
    except Exception as exc:
        _voxtral_registered = False
        logger.warning("Pulse Chat: enregistrement tardif du streamer en echec — %s", exc)
        return False
    if _voxtral_registered:
        logger.info("Pulse Chat: streamer TTS Voxtral enregistre (tentative tardive)")
    else:
        logger.warning(
            "Pulse Chat: streamer TTS Voxtral indisponible — l'agent ouvrira une "
            "piste audio que rien ne remplira (contrat de streaming absent ?)"
        )
    return _voxtral_registered


class _AudioStreamHandle(StreamingTTSHandle):
    """``StreamingTTSHandle`` + ce dont le transport a besoin.

    Le contrat autorise explicitement les adaptateurs a etendre le handle avec
    leur etat de plateforme. Ici : l'identifiant du flux (que l'app utilise pour
    jeter les morceaux d'un tour interrompu) et le numero de sequence (qui rend
    visible une perte d'ordre, laquelle s'entendrait sinon comme un hoquet).
    """

    def __init__(self, chat_id: str, audio_format: Any, stream_id: str) -> None:
        super().__init__(chat_id=chat_id, audio_format=audio_format)
        self.stream_id = stream_id
        self.seq = 0


def _get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    """Lecture credential compatible profils multiples (fallback os.environ)."""
    try:
        from agent.secret_scope import UnscopedSecretError, get_secret
        try:
            value = get_secret(name, default)
        except UnscopedSecretError:
            value = os.getenv(name)
    except ImportError:
        value = os.getenv(name)
    return value if value is not None else default


# Avertissements deja emis (cles arbitraires) : une degradation structurelle se
# repete a CHAQUE message, la signaler une fois suffit et evite d'inonder les logs.
_WARNED_ONCE: set = set()


def _warn_once(key: str, message: str) -> None:
    if key in _WARNED_ONCE:
        return
    _WARNED_ONCE.add(key)
    logger.warning("%s", message)


def agent_config_metadata(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Bloc ``agentConfig`` de la frame, repris TEL QUEL (fonction pure).

    L'app pousse dans chaque ``message.created`` la configuration comportementale
    du profil (``tone``, ``autonomy``, ``instructions``, ``disabledTools``). Le
    plugin TRANSPORTE ce bloc et ne l'interprete pas : aucune valeur n'est lue,
    validee, renommee ni filtree ici — c'est Hermes qui decide quoi en faire. Un
    champ ajoute plus tard cote app arrive donc jusqu'a l'agent sans toucher au
    plugin.

    Renvoie ``None`` quand la cle est absente ou n'est pas un objet : une app
    anterieure a ce contrat continue de fonctionner a l'identique (aucune cle
    ``metadata`` n'est alors posee sur l'evenement).
    """
    block = data.get("agentConfig")
    if not isinstance(block, dict):
        return None
    return {"agentConfig": block}


def session_token_from_ack(data: Dict[str, Any]) -> Optional[str]:
    """Jeton de session porte par une trame ``hello.ack`` (fonction pure).

    Renvoie ``None`` des que la trame ne porte pas de jeton exploitable (cle
    absente, vide, blancs, type inattendu) : le plugin repart alors sans
    identite, exactement comme face a un serveur anterieur a ce contrat. Aucune
    interpretation du contenu — c'est une chaine opaque, transportee telle
    quelle.
    """
    token = data.get("sessionToken")
    if not isinstance(token, str):
        return None
    token = token.strip()
    return token or None


def build_message_event(factory, kwargs: Dict[str, Any], metadata: Optional[Dict[str, Any]]):
    """``MessageEvent`` porteur de ``metadata``, quelle que soit sa signature.

    Le champ ``metadata`` n'existe pas forcement sur le ``MessageEvent`` de
    toutes les versions d'Hermes (c'est une API interne — cf. 08-hermes-plugin).
    On tente donc le mot-cle, puis on retombe sur l'attribut ; en dernier
    recours l'evenement part sans son bloc plutot que de perdre le message.

    Le repli est JOURNALISE (une seule fois, pour ne pas noyer les logs) : sur un
    Hermes qui ne connait pas ce champ, la configuration voyagerait sur un
    attribut que personne ne lit — exactement la panne silencieuse qu'on vient de
    corriger, sous une autre forme. Sans cette trace, rien ne la signalerait.
    """
    if metadata is None:
        return factory(**kwargs)
    try:
        return factory(metadata=metadata, **kwargs)
    except TypeError:
        _warn_once(
            "metadata_kwarg",
            "Pulse Chat: MessageEvent n'accepte pas 'metadata' — agentConfig "
            "transporte en attribut ; verifier qu'Hermes le lit bien, sinon la "
            "configuration de l'agent (ton, autonomie, consignes, outils) reste "
            "sans effet.",
        )
        event = factory(**kwargs)
        try:
            setattr(event, "metadata", metadata)
        except Exception as exc:  # dataclass figee, __slots__…
            logger.warning("Pulse Chat: agentConfig non transportable — %s", exc)
        return event


def _ws_url(base_url: str) -> str:
    """``http(s)://host[/path]`` -> ``ws(s)://host[/path]/ws/hermes``.

    Le token de service passe par le header ``Authorization: Bearer …`` —
    jamais en query string (les URLs finissent dans les logs de proxies).
    """
    parts = urllib.parse.urlsplit(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    path = parts.path.rstrip("/")
    return f"{scheme}://{parts.netloc}{path}/ws/hermes"


class PulseChatAdapter(BasePlatformAdapter):
    """Adaptateur async Pulse Chat (WS entrant + POST HTTP sortant)."""

    # L'app rend le markdown (code fences inclus) — le tool progress terminal
    # est livre en bloc code.
    supports_code_blocks = True

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform("pulse_chat"))
        extra = getattr(config, "extra", {}) or {}
        self.base_url: str = (
            _get_secret("PULSE_CHAT_URL") or extra.get("url", "")
        ).rstrip("/")
        self.token: str = _get_secret("PULSE_CHAT_TOKEN") or extra.get("token", "")

        # Secret PAR AGENT (`pca_...`), prioritaire sur le jeton de service pour
        # l'authentification du WebSocket. C'est lui qui rend l'identite annoncee
        # au `hello` VERIFIABLE : le jeton de service, unique pour toute
        # l'instance, permet a n'importe quel porteur d'annoncer le profil d'un
        # autre client. Sans ce secret, la session reste en regime `declared` et
        # les connecteurs tiers sont refuses (403 agent_credential_required).
        #
        # Absent ⇒ repli sur le jeton de service avec un avertissement, JAMAIS un
        # echec : un plugin deja deploye doit continuer a fonctionner.
        self.agent_token: str = (
            _get_secret("PULSE_CHAT_AGENT_TOKEN") or extra.get("agent_token", "")
        )
        if not self.agent_token:
            logger.warning(
                "Pulse Chat: PULSE_CHAT_AGENT_TOKEN absent — identite auto-declaree "
                "(regime `declared`). Les connecteurs tiers seront refuses."
            )

        # Profils Hermes servis par ce bot + identite annoncee (frame hello).
        self.profiles: List[str] = parse_profiles(
            _get_secret("PULSE_CHAT_PROFILE") or extra.get("profile", "")
        )
        self.agent_name: str = (
            _get_secret("PULSE_CHAT_AGENT_NAME") or extra.get("agent_name", "") or ""
        ).strip() or self.profiles[0]

        channels = _get_secret("PULSE_CHAT_CHANNELS") or extra.get("channels", "")
        if isinstance(channels, str):
            self.channels = {c.strip() for c in channels.split(",") if c.strip()}
        else:
            self.channels = {str(c).strip() for c in (channels or []) if str(c).strip()}

        # Etat runtime
        # Jeton de session recu dans ``hello.ack`` (identite de l'emetteur).
        # ``None`` tant qu'aucun ack n'est arrive : le plugin fonctionne alors
        # a l'identique, sans en-tete de session (serveur anterieur, ou trame
        # perdue) — la compatibilite prime.
        self._session_token: Optional[str] = None
        # Derniere source par canal — cle de session d'une interruption.
        self._last_source: Dict[str, Any] = {}
        # Dernier bloc ``agentConfig`` recu par canal : un tour INJECTE par le
        # plugin (relance apres un rendu de main) n'a pas de trame d'origine, et
        # partir sans lui priverait l'agent de son ton, de ses consignes et
        # surtout de ``disabledTools`` — des outils que l'admin a retires.
        self._last_agent_config: Dict[str, Optional[Dict[str, Any]]] = {}
        # Capacite audio annoncee par l'app au `hello.ack` (None = pas de flux).
        self._audio_capability: Optional[Dict[str, Any]] = None
        # Flux audio ouverts, par streamId — bornes pour ne jamais fuir si une
        # fin de flux se perd (deconnexion en plein tour).
        self._audio_streams: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._ws = None
        self._recv_task: Optional[asyncio.Task] = None
        # Reconnexion auto-pilotee (le gateway ne retente qu'une fois — cf.
        # reconnect.py) : tache de boucle + retryabilite du dernier echec.
        self._reconnect_task: Optional[asyncio.Task] = None
        self._last_connect_retryable: bool = True
        self._media_dir: Optional[str] = None
        # Cache borne (FIFO) des derniers message ids traites — dedup du rejeu.
        self._seen_message_ids: "OrderedDict[str, None]" = OrderedDict()
        # Approbations en attente : requestId -> Future resolue par la trame
        # ``approval.reply``. Elles SURVIVENT a une coupure WS : l'app rejoue
        # les decisions non livrees au prochain hello.
        self._pending_approvals: Dict[str, "asyncio.Future[Dict[str, Any]]"] = {}
        # Approbations issues du GARDE-FOU d'Hermes : requestId -> session_key.
        # Rien a debloquer ici — c'est Hermes qui tient le thread agent bloque,
        # et c'est ``resolve_gateway_approval`` qui le relache. On ne garde donc
        # que de quoi retrouver la session au retour de la decision.
        self._gateway_approvals: "OrderedDict[str, str]" = OrderedDict()
        # Questions posees en CARTE : clarify_id -> channel_slug. Rien a
        # debloquer ici non plus — l'attente vit dans le process Hermes
        # (``tools/clarify_gateway``), et c'est ``resolve_gateway_clarify`` qui
        # la relache depuis ``_handle_question_reply``.
        self._clarify_cards: "OrderedDict[str, str]" = OrderedDict()
        # Demandes d'approbation DELIBEREES (outil ``pulse_request_approval``) :
        # requestId -> Future THREAD-SAFE. Un ``concurrent.futures.Future`` et
        # non un ``asyncio.Future`` : le handler d'un outil asynchrone tourne
        # sur une AUTRE boucle que celle du WebSocket (``model_tools._run_async``
        # lui ouvre un thread et une boucle a lui), et une Future asyncio ne
        # s'attend pas depuis une autre boucle que la sienne.
        self._pending_gates: Dict[str, "concurrent.futures.Future[Dict[str, Any]]"] = {}
        # Plan de taches (hook ``post_tool_call``) : tours deja dotes d'une
        # carte, et verrou qui SERIALISE les envois. Deux ecritures du meme tour
        # partent dans l'ordre ou l'agent les a faites ; sans le verrou, deux
        # POST en vol (``asyncio.to_thread``) pouvaient arriver dans le
        # desordre, et la carte finissait sur l'etat ANCIEN — un plan qui
        # recule sans que rien ne le dise. Cree paresseusement, SUR la boucle.
        self._todo_turns: "OrderedDict[str, None]" = OrderedDict()
        self._todo_lock: Optional[asyncio.Lock] = None
        # Boucle du WebSocket, capturee a la connexion : c'est sur elle que le
        # handler fait poster la demande.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        _LIVE_ADAPTERS.add(self)

    @property
    def name(self) -> str:
        return "Pulse Chat"

    # ── Identite de session (hello.ack) ──────────────────────────────────

    @property
    def session_token(self) -> Optional[str]:
        """Jeton de la session courante, ou ``None`` si l'app n'en emet pas."""
        return self._session_token

    def _forget_session_token(self) -> None:
        """Oublie le jeton courant (envoi d'un nouveau ``hello``).

        Un nouveau ``hello`` revoque le jeton precedent cote serveur : le garder
        reviendrait a presenter une identite morte le temps que l'ack arrive.
        """
        self._session_token = None
        # La capacite audio est portee par la session : un nouveau `hello` la
        # remet a zero tant que l'app ne l'a pas re-annoncee. Sans ca, une app
        # redeployee sans le support audio continuerait de recevoir du PCM.
        self._audio_capability = None

    async def _handle_call_interrupt(self, data: Dict[str, Any]) -> None:
        """``call.interrupt`` -> arrete le tour en cours pour ce canal.

        L'humain a repris la parole pendant que l'agent parlait. Couper le SON
        cote navigateur ne suffit pas : sans cet arret, Hermes finit de rediger,
        ouvre un nouveau flux a la phrase suivante, et la voix repart par-dessus
        celle de l'humain — pour un texte que plus personne n'ecoute.

        Best effort, jamais bloquant : un canal jamais vu (aucun message recu
        depuis la connexion) n'a pas de session a arreter.
        """
        chat_id = str(data.get("chatId") or "")
        if not chat_id:
            return
        source = self._last_source.get(chat_id)
        if source is None:
            logger.info(
                "Pulse Chat: interruption demandee pour %s, aucune session connue", chat_id
            )
            return
        try:
            from gateway.session import build_session_key

            session_key = build_session_key(
                source,
                group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
                thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
            )
        except Exception as exc:
            logger.warning("Pulse Chat: cle de session introuvable pour %s — %s", chat_id, exc)
            return
        await self.interrupt_session_activity(session_key, chat_id)
        logger.info("Pulse Chat: tour interrompu a la demande de l'humain (%s)", chat_id)

    def _handle_hello_ack(self, data: Dict[str, Any]) -> None:
        """Trame ``hello.ack`` -> memorisation du jeton (sans interpretation)."""
        token = session_token_from_ack(data)
        if token is None:
            # Serveur qui n'emet pas de jeton : on continue sans identite.
            logger.debug("Pulse Chat: hello.ack sans jeton — session non identifiee")
            return
        self._session_token = token
        logger.info("Pulse Chat: jeton de session recu (identite de l'agent active)")
        # Capacite audio : c'est l'APP qui ouvre la voie parlee en flux. Une app
        # qui n'annonce rien laisse le plugin dans son comportement d'avant
        # (audio complet en fin de tour) — le plugin peut donc etre deploye en
        # premier sans rien changer au produit.
        self._audio_capability = audio_capability_from_ack(data)
        if self._audio_capability:
            logger.info(
                "Pulse Chat: l'app accepte l'audio en flux (%s Hz)",
                self._audio_capability.get("sampleRate") or "libre",
            )

    def _auth_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """En-tetes des appels sortants VERS L'APP.

        Le Bearer de service reste inchange (il dit « ce plugin a le droit de
        parler a l'app ») ; l'en-tete de session s'y AJOUTE quand elle existe
        (elle dit « quel agent parle »).

        Reservee aux appels qui s'authentifient par ce Bearer :
        ``/api/agent/messages`` et le coffre. Le telechargement d'un media en
        est exclu — les ``mediaUrls`` pointent pourtant bien vers le domaine
        public de l'app (``/api/attachments/:id/download``, cf.
        ``server/lib/signedDownload.ts``), mais cette route a son PROPRE schema
        d'authentification : une signature HMAC portee par l'URL, verifiee par
        ``verifyDownload``. Elle ne lit ni Bearer ni en-tete de session ; les y
        poser n'aurait aucun effet.
        """
        headers = {"Authorization": f"Bearer {self.token}"}
        if self._session_token:
            headers[_SESSION_HEADER] = self._session_token
        if extra:
            headers.update(extra)
        return headers

    # ── Cycle de vie ─────────────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Ouvre le WebSocket vers l'app et demarre la boucle de reception."""
        self._loop = asyncio.get_running_loop()
        if not self.base_url or not self.token:
            self._last_connect_retryable = False
            self._set_fatal_error(
                "config_missing",
                "PULSE_CHAT_URL et PULSE_CHAT_TOKEN doivent etre definis",
                retryable=False,
            )
            return False

        try:
            import websockets
        except ImportError:
            self._last_connect_retryable = False
            self._set_fatal_error(
                "missing_dependency",
                "librairie manquante — pip install websockets",
                retryable=False,
            )
            return False

        url = _ws_url(self.base_url)
        # Secret PAR AGENT si disponible, sinon jeton de service. Le serveur
        # distingue les deux au prefixe (`pca_`) et emet une session `verified`
        # dans le premier cas, `declared` dans le second. L'en-tete HTTP, lui,
        # reste toujours sur le jeton de service : le credential authentifie la
        # CONNEXION, la session transporte l'identite jusqu'aux appels HTTP.
        headers = {"Authorization": f"Bearer {self.agent_token or self.token}"}
        try:
            # websockets >= 14 : ``additional_headers`` ; anciennes versions
            # (legacy) : ``extra_headers``. On tente le nom moderne d'abord.
            try:
                connection = websockets.connect(
                    url, max_size=_WS_MAX_SIZE, additional_headers=headers
                )
            except TypeError:
                connection = websockets.connect(
                    url, max_size=_WS_MAX_SIZE, extra_headers=headers
                )
            self._ws = await asyncio.wait_for(connection, timeout=_WS_CONNECT_TIMEOUT)
        except Exception as exc:
            retryable = is_retryable_ws_error(exc)
            logger.error("Pulse Chat: echec de connexion WS a %s — %s", self.base_url, exc)
            self._last_connect_retryable = retryable
            self._set_fatal_error("connect_failed", str(exc), retryable=retryable)
            return False

        # Capacites de l'agent (fiche /admin) : recolte impure isolee ici — le
        # module hello.py reste pur. La recolte ne doit JAMAIS empecher le
        # hello : self.profiles[0] choisi arbitrairement (un bot peut servir
        # plusieurs profils, la fiche colle au premier), et toute exception
        # imprevue degrade silencieusement vers "pas de capacites" plutot que
        # de faire echouer la connexion (la connexion prime toujours sur la
        # fiche d'administration).
        capabilities: Optional[Dict[str, Any]] = None
        try:
            capabilities = collect_capabilities(self.profiles[0])
        except Exception as exc:
            logger.warning("Pulse Chat: collecte des capacites en echec — %s", exc)
            capabilities = None

        # Frame hello AVANT la boucle de reception : le serveur enregistre le
        # peer pour ces profils (last-wins par profil) puis rejoue les messages
        # non livres de ces profils. Reconnexion => re-hello (meme chemin).
        try:
            hello_frame = build_hello(self.profiles, self.agent_name, capabilities)
            # Le hello revoque le jeton precedent cote serveur : on l'oublie ici
            # aussi, le nouveau arrivera par ``hello.ack``.
            self._forget_session_token()
            await self._ws.send(json.dumps(hello_frame))
        except Exception as exc:
            logger.error("Pulse Chat: echec envoi hello — %s", exc)
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
            self._last_connect_retryable = True
            self._set_fatal_error("connect_failed", str(exc), retryable=True)
            return False

        self._last_connect_retryable = True
        self._recv_task = asyncio.create_task(self._receive_loop())
        self._mark_connected()
        logger.info(
            "Pulse Chat: connecte a %s (%s) — profils %s, agent '%s'",
            self.base_url,
            "reconnexion" if is_reconnect else "connexion initiale",
            ",".join(self.profiles),
            self.agent_name,
        )
        return True

    async def disconnect(self) -> None:
        """Arret propre : marque deconnecte, annule les boucles, ferme le WS."""
        self._mark_disconnected()
        self._abort_open_audio_streams()

        reconnect_task, self._reconnect_task = self._reconnect_task, None
        if reconnect_task and not reconnect_task.done():
            reconnect_task.cancel()
            try:
                await reconnect_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        task, self._recv_task = self._recv_task, None
        ws, self._ws = self._ws, None

        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    # ── Reception (app -> agent) ─────────────────────────────────────────

    async def _receive_loop(self) -> None:
        """Boucle de reception WS : dispatch des ``message.created``."""
        ws = self._ws
        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except (TypeError, ValueError):
                    logger.warning("Pulse Chat: trame WS non-JSON ignoree")
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("type") == _HELLO_ACK_TYPE:
                    try:
                        self._handle_hello_ack(data)
                    except Exception:
                        logger.exception("Pulse Chat: erreur de traitement hello.ack")
                elif data.get("type") == "message.created":
                    try:
                        await self._handle_message_created(data)
                    except Exception:
                        logger.exception("Pulse Chat: erreur de traitement message.created")
                elif data.get("type") == "call.interrupt":
                    try:
                        await self._handle_call_interrupt(data)
                    except Exception:
                        logger.exception("Pulse Chat: erreur de traitement call.interrupt")
                elif data.get("type") == "approval.reply":
                    try:
                        self._handle_approval_reply(data)
                    except Exception:
                        logger.exception("Pulse Chat: erreur de traitement approval.reply")
                elif data.get("type") == "question.reply":
                    try:
                        self._handle_question_reply(data)
                    except Exception:
                        logger.exception("Pulse Chat: erreur de traitement question.reply")
                elif data.get("type") == "gate.reply":
                    try:
                        await self._handle_gate_reply(data)
                    except Exception:
                        logger.exception("Pulse Chat: erreur de traitement gate.reply")
                elif data.get("type") == "browser.control":
                    # Resout la Future d'un outil ``pulse_browser_handoff`` qui
                    # attend dans un AUTRE fil — ou, si plus personne n'attend,
                    # relance l'agent par un message entrant.
                    try:
                        await self._handle_browser_control(data)
                    except Exception:
                        logger.exception("Pulse Chat: erreur de traitement browser.control")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Pulse Chat: erreur boucle de reception — %s", exc)
        finally:
            # Coupure inattendue (pas un disconnect() volontaire) : l'adaptateur
            # pilote SA reconnexion (backoff, cf. reconnect.py). On ne notifie
            # PAS le gateway ici : son unique retry immediat tombait sur le 502
            # de redeploiement et il abandonnait definitivement (incident
            # 2026-08-05). Le fatal ne remonte qu'a l'abandon (non-retryable).
            if self.is_connected:
                self._mark_disconnected()
                logger.warning(
                    "Pulse Chat: WebSocket ferme de maniere inattendue — "
                    "reconnexion automatique engagee"
                )
                ws, self._ws = self._ws, None
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                self._ensure_reconnect_task()

    def _ensure_reconnect_task(self) -> None:
        """Demarre la boucle de reconnexion si elle ne tourne pas deja."""
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Retente ``connect(is_reconnect=True)`` avec backoff, indefiniment.

        S'arrete : au succes, ou sur un echec non-retryable (token invalide,
        config) — seul cas ou le fatal est notifie au gateway.
        """
        attempt = 0
        while True:
            attempt += 1
            delay = reconnect_delay(attempt)
            if delay:
                await asyncio.sleep(delay)
            logger.warning(
                "Pulse Chat: tentative de reconnexion %d (delai %.0fs)", attempt, delay
            )
            try:
                if await self.connect(is_reconnect=True):
                    logger.info(
                        "Pulse Chat: reconnecte apres %d tentative(s)", attempt
                    )
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                # connect() ne devrait pas lever — ceinture : on traite comme
                # un echec retryable et on continue le backoff.
                logger.exception("Pulse Chat: erreur inattendue pendant la reconnexion")
                continue
            if not self._last_connect_retryable:
                logger.error(
                    "Pulse Chat: echec de reconnexion non-retryable — abandon"
                )
                await self._notify_fatal_error()
                return

    async def _handle_message_created(self, data: Dict[str, Any]) -> None:
        """``message.created`` -> MessageEvent -> handle_message -> ack."""
        channel = data.get("channel") or {}
        message = data.get("message") or {}
        slug = str(channel.get("slug") or "")
        message_id = message.get("id")
        if not slug or message_id is None:
            logger.warning("Pulse Chat: message.created incomplet ignore")
            return

        # Dedup du rejeu : deja traite => re-ack (l'ack initial a pu se perdre)
        # mais PAS de second handle_message (pas de double dispatch agent).
        dedup_key = str(message_id)
        if dedup_key in self._seen_message_ids:
            logger.debug("Pulse Chat: message %s deja traite, re-ack sans dispatch", dedup_key)
            await self._send_ack(message_id)
            return

        # Filtre optionnel PULSE_CHAT_CHANNELS (transport, pas metier) :
        # on acke quand meme pour ne pas faire boucler le replay serveur.
        if self.channels and slug not in self.channels:
            logger.debug("Pulse Chat: canal %s hors PULSE_CHAT_CHANNELS, ignore", slug)
            await self._send_ack(message_id)
            return

        media_urls, media_types = await self._download_media(
            message.get("mediaUrls") or []
        )

        # Source MEMORISEE par canal : c'est elle qui permet de reconstruire la
        # cle de session au moment d'une interruption (`call.interrupt`), sans
        # quoi on ne saurait pas QUELLE session arreter.
        source = self.build_source(
            chat_id=slug,
            chat_name=channel.get("name") or slug,
            chat_type="group",
            user_id=str(message.get("userId")) if message.get("userId") else None,
            user_name=message.get("userName"),
        )
        # Le bloc `agentConfig` de la frame est REPORTE dans l'evenement transmis
        # a Hermes, sans interpretation : ton, autonomie, consignes et politique
        # d'outils sont sans effet de bout en bout si le plugin les jette.
        # Note vocale : le type du message decide de DEUX comportements du
        # coeur Hermes que l'adaptateur ne peut pas obtenir autrement —
        # la transcription automatique de l'audio entrant, et l'auto-TTS de la
        # reponse (qui ne se declenche QUE sur un message entrant de type VOICE,
        # cf. `_should_auto_tts_for_chat` dans gateway/platforms/base.py).
        # L'annonce de l'app fait foi ; le MIME telecharge sert de repli pour
        # rester correct si un jour elle ne l'annonce plus.
        is_voice = str(message.get("messageType") or "").lower() == "voice" or any(
            is_audio_mime(mime) for mime in media_types
        )
        event = build_message_event(
            MessageEvent,
            {
                "text": message.get("text") or "",
                "message_type": MessageType.VOICE if is_voice else MessageType.TEXT,
                "source": source,
                "message_id": str(message_id),
                "media_urls": media_urls,
                "media_types": media_types,
            },
            agent_config_metadata(data),
        )

        self._last_source[slug] = source
        self._last_agent_config[slug] = agent_config_metadata(data)
        await self.handle_message(event)
        self._remember_message_id(dedup_key)
        await self._send_ack(message_id)

    def _remember_message_id(self, key: str) -> None:
        """Enregistre un id traite dans le cache borne (eviction FIFO)."""
        self._seen_message_ids[key] = None
        self._seen_message_ids.move_to_end(key)
        while len(self._seen_message_ids) > _DEDUP_MAX_IDS:
            self._seen_message_ids.popitem(last=False)

    async def _send_ack(self, message_id: Any) -> None:
        if self._ws is None:
            return
        # Le jeton accompagne l'ack : le serveur sait ainsi QUEL agent acquitte
        # quel message. Cle omise tant qu'aucun jeton n'a ete recu (trame
        # strictement identique a celle d'avant ce contrat).
        frame: Dict[str, Any] = {"type": "ack", "messageId": message_id}
        if self._session_token:
            frame["sessionToken"] = self._session_token
        try:
            await self._ws.send(json.dumps(frame))
        except Exception as exc:
            logger.warning("Pulse Chat: echec envoi ack %s — %s", message_id, exc)

    # ── Media (images -> chemins locaux pour la vision) ──────────────────

    async def _download_media(
        self, urls: List[str]
    ) -> Tuple[List[str], List[str]]:
        """Telecharge localement les images et les notes vocales de ``mediaUrls``.

        Images (vision) et audio (transcription) sont materialises ; les autres
        documents restent des URLs presignees dans le texte du message (l'agent
        les lit avec ses outils) — decision 2 du plan.

        L'audio DOIT etre un fichier local : la transcription automatique du
        gateway ouvre le chemin avec le fournisseur STT, elle ne suit pas une
        URL. Une note vocale laissee en URL arriverait donc muette.
        """
        paths: List[str] = []
        types: List[str] = []
        for url in urls[:_MEDIA_MAX_COUNT]:
            try:
                path, mime = await asyncio.to_thread(self._download_one, url)
            except Exception as exc:
                logger.warning("Pulse Chat: echec telechargement media — %s", exc)
                continue
            if path:
                paths.append(path)
                types.append(mime or "application/octet-stream")
        return paths, types

    def _download_one(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        request = urllib.request.Request(
            url, headers={"User-Agent": "hermes-pulse-chat-plugin"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            mime = response.headers.get_content_type() or ""
            audio = is_audio_mime(mime)
            if not mime.startswith("image/") and not audio:
                return None, None
            # L'audio a son propre plafond, plus bas : au-dela, aucun
            # fournisseur STT n'accepte le fichier (25 Mo cote Hermes), le
            # telecharger serait du transfert pour rien.
            cap = MAX_VOICE_BYTES if audio else _MEDIA_MAX_BYTES
            data = response.read(cap + 1)
            if len(data) > cap:
                logger.warning(
                    "Pulse Chat: media trop volumineux ignore (%s, %s)", mime, url
                )
                return None, None
        if self._media_dir is None:
            self._media_dir = tempfile.mkdtemp(prefix="pulse-chat-media-")
        extension = mimetypes.guess_extension(mime) or ".bin"
        path = os.path.join(self._media_dir, f"{uuid.uuid4().hex}{extension}")
        with open(path, "wb") as handle:
            handle.write(data)
        return path, mime

    # ── Emission (agent -> app) ──────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Classe le contenu puis poste vers l'app (kind message|tool_event)."""
        content = content if content is not None else ""
        kind = classify_outbound(content, is_edit=False)
        info = parse_tool(content)
        hermes_id = uuid.uuid4().hex
        payload = {
            "channelSlug": str(chat_id),
            "kind": kind,
            "content": content,
            "raw": content,
            "hermesMessageId": hermes_id,
            "tool": info["tool"] if kind == "tool_event" else None,
            # Doute (contenu vide, etc.) => tool_event sans phase detectee : interim.
            "phase": (info["phase"] or "interim") if kind == "tool_event" else None,
            "replyToHermesId": str(reply_to) if reply_to else None,
        }
        # Metriques d'execution (modele, tokens, duree) si Hermes les expose —
        # l'app ne peut pas les deviner. Champ omis quand il n'y a rien.
        metrics = extract_metrics(metadata)
        if metrics is not None:
            payload["metrics"] = metrics
        return await self._post_agent_message(payload, hermes_id)

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """Edition = TOUJOURS tool_event, upsert serveur par hermesMessageId.

        Implementation OBLIGATOIRE : sans elle, le gateway draine silencieusement
        tout le tool progress (bulle initiale + editions accumulees).
        """
        content = content if content is not None else ""
        info = parse_tool(content)
        payload = {
            "channelSlug": str(chat_id),
            "kind": "tool_event",
            "content": content,
            "raw": content,
            "hermesMessageId": str(message_id),
            "tool": info["tool"],
            "phase": info["phase"] or "progress",
            "replyToHermesId": None,
        }
        return await self._post_agent_message(payload, str(message_id))

    # ── Notes vocales (l'agent parle) ─────────────────────────────────────

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Poste un fichier audio comme note vocale dans le canal.

        Point d'entree OBLIGATOIRE pour que l'agent parle : sans cette methode,
        le repli de ``BasePlatformAdapter`` poste le texte ``🔊 Audio: <chemin>``
        — donc un chemin de conteneur, dans le fil d'un client, et aucun son.

        Appelee par deux chemins d'Hermes, et c'est voulu :
        - l'auto-TTS de la reponse (via ``play_tts``, qui delegue ici) ;
        - le tool ``text_to_speech`` de l'agent, dont le ``MEDIA:<path>`` est
          route vers l'envoi audio de la plateforme.

        Un echec ne leve JAMAIS : le texte de la reponse est envoye separement
        par le gateway, une note vocale perdue ne doit pas emporter le tour de
        parole. Il est journalise et rendu en ``SendResult`` non concluant.
        """
        mime = guess_audio_mime(audio_path)
        try:
            data = await asyncio.to_thread(self._read_audio, audio_path)
        except FileNotFoundError:
            logger.warning("Pulse Chat: audio introuvable — %s", audio_path)
            return SendResult(
                success=False, error="audio introuvable", error_kind="permanent"
            )
        except ValueError as exc:
            logger.warning("Pulse Chat: audio refuse — %s", exc)
            return SendResult(success=False, error=str(exc), error_kind="permanent")
        except Exception as exc:
            logger.warning("Pulse Chat: lecture audio en echec — %s", exc)
            return SendResult(
                success=False, error=str(exc), retryable=True, error_kind="transient"
            )

        headers = {
            "Content-Type": mime,
            "x-filename": urllib.parse.quote(voice_filename(mime), safe=""),
        }
        encoded_caption = caption_header(caption)
        if encoded_caption:
            headers["x-caption"] = encoded_caption
        if reply_to:
            headers["x-reply-to"] = urllib.parse.quote(str(reply_to), safe="")

        url = voice_url(self.base_url, str(chat_id))
        try:
            status, body = await asyncio.to_thread(
                self._post_bytes, url, data, headers
            )
        except urllib.error.HTTPError as exc:
            logger.warning("Pulse Chat: POST /api/agent/voice -> HTTP %s", exc.code)
            return SendResult(
                success=False,
                error="HTTP %s: %s" % (exc.code, exc.reason),
                retryable=self._is_retryable_status(exc.code),
                error_kind=self._error_kind_for_status(exc.code),
            )
        except Exception as exc:
            logger.warning("Pulse Chat: POST /api/agent/voice en echec — %s", exc)
            return SendResult(
                success=False, error=str(exc), retryable=True, error_kind="transient"
            )
        if status >= 400:
            return SendResult(
                success=False,
                error="HTTP %s" % status,
                retryable=self._is_retryable_status(status),
                error_kind=self._error_kind_for_status(status),
            )

        # L'id rendu est celui du MESSAGE cote app : c'est lui qui sert de
        # cible a une reponse ulterieure, pas l'id de la piece audio.
        message_id = None
        if body:
            try:
                message_id = (json.loads(body) or {}).get("id")
            except Exception:
                message_id = None
        return SendResult(success=True, message_id=message_id or uuid.uuid4().hex)

    @staticmethod
    def _read_audio(path: str) -> bytes:
        """Lit un fichier audio en refusant ce que le serveur refuserait."""
        with open(path, "rb") as handle:
            data = handle.read(MAX_VOICE_BYTES + 1)
        if not data:
            raise ValueError("fichier audio vide")
        if len(data) > MAX_VOICE_BYTES:
            raise ValueError("note vocale trop volumineuse")
        return data

    def _post_bytes(
        self, url: str, body: bytes, headers: Dict[str, str]
    ) -> Tuple[int, bytes]:
        """POST d'un corps binaire (bloquant — via ``asyncio.to_thread``)."""
        request = urllib.request.Request(
            url, data=body, headers=self._auth_headers(headers), method="POST"
        )
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
            return int(response.status), response.read()

    # ── Audio en flux : l'agent parle pendant qu'il redige ────────────────
    #
    # Contrat d'adaptateur d'Hermes (#60671). Le decoupage en phrases, la
    # synthese au fil de l'eau, la suppression du doublon « audio complet » et
    # l'annulation sont tenus par `gateway/streaming_tts_consumer.py` : ces cinq
    # methodes ne sont qu'un TUYAU vers l'app. Toute logique ajoutee ici serait
    # une logique de plus a maintenir contre une API interne.

    def supports_streaming_tts(self, chat_id: str, audio_format: Any) -> bool:
        """L'app peut-elle jouer du PCM en flux pour ce canal ?

        Trois refus, tous silencieux et tous rattrapes par le repli natif
        d'Hermes (audio complet en fin de tour) : pas de WebSocket, pas de
        capacite annoncee par l'app, ou une frequence que son lecteur ne joue
        pas. C'est ce qui permet de deployer ce plugin AVANT l'app.
        """
        if self._ws is None:
            return False
        sample_rate = getattr(audio_format, "sample_rate", None) or DEFAULT_SAMPLE_RATE
        return capability_accepts(self._audio_capability, sample_rate)

    async def begin_streaming_tts(
        self,
        chat_id: str,
        audio_format: Any,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[StreamingTTSHandle]:
        """Ouvre une piste audio. ``None`` = on decline, Hermes replie."""
        if not self.supports_streaming_tts(chat_id, audio_format):
            return None

        # Filet de securite sur l'ORDRE DE CHARGEMENT : si le streamer Voxtral
        # n'a pas pu s'enregistrer a l'import du plugin (le contrat de streaming
        # n'etait pas encore importable), Hermes ouvrirait cette piste et
        # n'aurait aucun fournisseur pour la remplir — une piste ouverte, zero
        # octet ecrit, fermeture propre. Vu en production. `install()` est
        # idempotent : le rappeler ici ne coute rien et supprime la fenetre.
        _ensure_voxtral_streamer()

        stream_id = uuid.uuid4().hex
        try:
            await self._ws.send(json.dumps(begin_frame(str(chat_id), stream_id, audio_format)))
        except Exception as exc:
            # Decliner plutot que lever : le consumer retombe proprement sur
            # l'audio complet, et le tour de parole n'est pas perdu.
            logger.warning("Pulse Chat: ouverture du flux audio en echec — %s", exc)
            return None

        logger.info(
            "Pulse Chat: piste audio ouverte (chat=%s, stream=%s)", chat_id, stream_id
        )
        handle = _AudioStreamHandle(str(chat_id), audio_format, stream_id)
        self._audio_streams[stream_id] = handle
        # Borne de securite : une fin de flux perdue (deconnexion en plein tour)
        # ne doit pas faire grossir ce dictionnaire indefiniment.
        while len(self._audio_streams) > _AUDIO_STREAMS_MAX:
            _, stale = self._audio_streams.popitem(last=False)
            stale.aborted = True
        return handle

    async def write_streaming_tts(self, handle: StreamingTTSHandle, chunk: bytes) -> None:
        """Pousse un morceau de PCM. LEVE en cas d'echec (contrat)."""
        if handle is None or getattr(handle, "aborted", False) or not chunk:
            # Morceau en retard apres une interruption : jete en silence, comme
            # l'exige l'idempotence de `abort_streaming_tts`.
            return
        if self._ws is None:
            raise RuntimeError("WebSocket Pulse Chat ferme")

        stream_id = getattr(handle, "stream_id", "")
        seq = getattr(handle, "seq", 0)
        # `await` sur l'envoi : c'est aussi ce qui exerce la contre-pression
        # quand le lien est plus lent que la synthese.
        await self._ws.send(encode_audio_frame(handle.chat_id, stream_id, seq, chunk))
        handle.seq = seq + 1

    async def finish_streaming_tts(
        self, handle: StreamingTTSHandle, *, interrupted: bool = False
    ) -> None:
        """Fin normale du flux. Ne leve jamais : le tour est deja dit."""
        if handle is None:
            return
        stream_id = getattr(handle, "stream_id", "")
        # Compte des morceaux REELLEMENT ecrits. `0` dit que le consumer a ouvert
        # une piste et n'a rien synthetise — la panne la plus difficile a voir,
        # et celle qu'on a passe une soiree a chercher : cote app, le tour
        # s'ouvrait et se fermait proprement, sans un octet entre les deux.
        logger.info(
            "Pulse Chat: piste audio fermee (stream=%s, morceaux=%s, interrompu=%s)",
            stream_id,
            getattr(handle, "seq", 0),
            interrupted,
        )
        self._audio_streams.pop(stream_id, None)
        if self._ws is None:
            return
        try:
            await self._ws.send(
                json.dumps(end_frame(handle.chat_id, stream_id, interrupted))
            )
        except Exception as exc:
            logger.debug("Pulse Chat: fin de flux audio non transmise — %s", exc)

    async def abort_streaming_tts(
        self, handle: StreamingTTSHandle, error: Optional[str] = None
    ) -> None:
        """Abandon — IDEMPOTENT : les morceaux en retard sont jetes, pas levees."""
        if handle is None or getattr(handle, "aborted", False):
            return
        handle.aborted = True
        stream_id = getattr(handle, "stream_id", "")
        self._audio_streams.pop(stream_id, None)
        if self._ws is None:
            return
        try:
            await self._ws.send(
                json.dumps(abort_frame(handle.chat_id, stream_id, error))
            )
        except Exception as exc:
            logger.debug("Pulse Chat: abandon de flux audio non transmis — %s", exc)

    def _abort_open_audio_streams(self) -> None:
        """Coupe les flux restes ouverts (deconnexion en plein tour).

        Sans ca, un `write` tardif reprendrait sur une nouvelle connexion avec
        un `streamId` que l'app ne connait plus : du son sorti de nulle part,
        par-dessus le tour suivant.
        """
        for handle in self._audio_streams.values():
            handle.aborted = True
        self._audio_streams.clear()

    # ── Artifacts (contenus riches publies dans le fil) ───────────────────

    async def publish_artifact(
        self,
        chat_id: str,
        kind: str,
        content: Optional[str] = None,
        artifact_id: Optional[str] = None,
        title: Optional[str] = None,
        path: Optional[str] = None,
    ) -> Optional[str]:
        """Publie un artifact dans la conversation.

        ``kind`` : mermaid | markdown | svg | html | drawio | file | motion.

        Un artifact est un POINTEUR vers un fichier du coffre : le contenu
        n'existe qu'une fois, dans le coffre du canal. Deux usages :

        - ``content=`` : le contenu est ECRIT dans le coffre puis publie, en un
          seul appel. C'est le cas courant — l'agent a son contenu en memoire,
          lui demander deux appels serait un piege d'ergonomie.
        - ``path=``    : le fichier est deja dans le coffre, on le designe.

        L'``artifact_id`` porte l'IDENTITE : le republier met a jour la carte
        existante au lieu d'en ajouter une. Par defaut il est derive du TITRE,
        ce qui rend le comportement attendu automatique.

        ``kind="file"`` est le cas du FICHIER QUELCONQUE (PDF, tableur, archive,
        image...) : sa carte se telecharge (PDF et images s'apercoivent en
        panneau). Il exige ``path=`` — un fichier binaire ne se passe pas par
        ``content=``, qui est encode en UTF-8 avant ecriture. Ecrire d'abord avec
        ``vault_write``, publier ensuite. Republier le MEME ``artifact_id`` sur
        un fichier reecrit cree une nouvelle version : l'app archive l'etat
        precedent (versionnement du coffre).

        ``kind="motion"`` est un FILM : une page HTML dont chaque image est une
        fonction de ``t``, exposant ``window.__duration`` (secondes) et
        ``window.__renderAt(t)``, polices integrees en ``data:`` (aucune
        ressource distante), <= 2 Mo. ``path=`` est la voie RECOMMANDEE — un
        film volumineux ne passe pas par ``content=``, plafonne comme les
        autres types texte a 400 000 caracteres : ecrire d'abord avec
        ``vault_write``, publier ensuite. Un petit film peut encore passer par
        ``content=``.

        Retourne l'``artifact_id`` utilise, ou ``None`` en cas d'echec.
        """
        if (content is None) == (path is None):
            raise ValueError("fournir soit `content`, soit `path` — jamais les deux")
        if kind == "file" and content is not None:
            # Refuse ICI plutot que cote app : `content` part en UTF-8, donc un
            # binaire y arriverait corrompu SANS erreur — l'agent croirait avoir
            # publie son PDF, et le telechargement rendrait un fichier illisible.
            raise ValueError(
                "kind='file' attend `path=` (ecrire d'abord le fichier avec vault_write)"
            )

        artifact_id = (
            artifact_id
            or default_artifact_id(kind, title)
            or "art-%s" % uuid.uuid4().hex
        )

        if content is not None:
            path = path or default_artifact_path(kind, title, artifact_id)
            written = await self.vault_write(
                chat_id, path, content.encode("utf-8"), "text/plain; charset=utf-8"
            )
            if not written:
                logger.warning(
                    "Pulse Chat: artifact %s — ecriture du coffre en echec (%s)",
                    artifact_id,
                    path,
                )
                return None

        payload = build_artifact_payload(
            channel_slug=chat_id,
            artifact_id=artifact_id,
            kind=kind,
            path=path,
            title=title,
        )
        result = await self._post_agent_message(payload, artifact_id)
        if not result.success:
            logger.warning(
                "Pulse Chat: artifact %s non publie — %s", artifact_id, result.error
            )
            return None
        return artifact_id

    # ── Coffre-fort (espace de travail par canal) ─────────────────────────

    async def vault_list(self, chat_id: str) -> List[str]:
        """Chemins des fichiers du coffre du canal (liste vide si echec)."""
        data = await asyncio.to_thread(
            self._vault_request, "GET", vault_url(self.base_url, chat_id)
        )
        if not data:
            return []
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            logger.warning("Pulse Chat: reponse de listing du coffre illisible")
            return []
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, list):
            return []
        return [f.get("path") for f in files if isinstance(f, dict) and f.get("path")]

    async def vault_read(self, chat_id: str, path: str) -> Optional[bytes]:
        """Contenu d'un fichier du coffre, ou ``None`` s'il est introuvable."""
        return await asyncio.to_thread(
            self._vault_request, "GET", vault_url(self.base_url, chat_id, path)
        )

    async def vault_write(
        self,
        chat_id: str,
        path: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> bool:
        """Ecrit (ou remplace) un fichier du coffre."""
        result = await asyncio.to_thread(
            self._vault_request,
            "PUT",
            vault_url(self.base_url, chat_id, path),
            content,
            content_type,
        )
        return result is not None

    async def vault_delete(self, chat_id: str, path: str) -> bool:
        result = await asyncio.to_thread(
            self._vault_request, "DELETE", vault_url(self.base_url, chat_id, path)
        )
        return result is not None

    def _vault_request(
        self,
        method: str,
        url: str,
        body: Optional[bytes] = None,
        content_type: Optional[str] = None,
    ) -> Optional[bytes]:
        """Appel HTTP du coffre (bloquant — appele via ``asyncio.to_thread``).

        Retourne le corps de la reponse, ou ``None`` en cas d'echec. Aucune
        exception ne remonte : une operation de coffre en echec ne doit pas
        faire tomber le tour de parole de l'agent.
        """
        headers = self._auth_headers(
            {"Content-Type": content_type} if content_type else None
        )
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            logger.warning(
                "Pulse Chat: coffre %s %s -> HTTP %s", method, url, exc.code
            )
        except Exception as exc:
            logger.warning("Pulse Chat: coffre %s en echec — %s", method, exc)
        return None

    # ── Outils offerts au MODELE (coffre + publication, cf. workspace.py) ──
    #
    # Distincts de ``vault_write`` / ``publish_artifact`` : ceux-la rendent un
    # booleen et journalisent l'echec, ce qui suffit a du code. Un MODELE a
    # besoin du MOTIF du refus pour savoir s'il corrige un chemin ou previent un
    # humain — d'ou des variantes qui rendent le statut et le message de l'app.

    def _http_detailed(
        self,
        method: str,
        url: str,
        *,
        body: Any = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = _HTTP_TIMEOUT,
    ) -> Tuple[int, Any]:
        """``(statut, corps JSON ou None)`` — jamais d'exception. Bloquant.

        ``body`` peut etre des octets ou un fichier ouvert : urllib l'envoie
        alors EN FLUX, sans le materialiser (``Content-Length`` fourni par
        l'appelant). Statut ``0`` = l'app n'a pas repondu du tout.
        """
        request = urllib.request.Request(
            url, data=body, headers=self._auth_headers(headers), method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                raw = exc.read()
            except Exception:
                raw = b""
            logger.warning("Pulse Chat: %s %s -> HTTP %s", method, url, status)
        except Exception as exc:
            logger.warning("Pulse Chat: %s %s en echec — %s", method, url, exc)
            return 0, {"message": str(exc)}
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            parsed = None
        return status, parsed

    def _vault_put_for_tool(
        self, chat_id: str, path: str, *, local_path: Optional[str], content: Optional[str]
    ) -> Tuple[int, Any, int]:
        """PUT du coffre, octets d'un fichier local (EN FLUX) ou d'un texte."""
        url = vault_url(self.base_url, chat_id, path)
        content_type = content_type_for(path)
        if local_path is not None:
            real, size = resolve_local_file(local_path)
            with open(real, "rb") as handle:
                status, body = self._http_detailed(
                    "PUT",
                    url,
                    body=handle,
                    headers={"Content-Type": content_type, "Content-Length": str(size)},
                    timeout=UPLOAD_TIMEOUT_SECONDS,
                )
            return status, body, size
        data = (content or "").encode("utf-8")
        if content_type == "application/octet-stream":
            content_type = "text/plain; charset=utf-8"
        status, body = self._http_detailed(
            "PUT",
            url,
            body=data,
            headers={"Content-Type": content_type, "Content-Length": str(len(data))},
            timeout=UPLOAD_TIMEOUT_SECONDS,
        )
        return status, body, len(data)

    async def tool_vault_write(self, chat_id: str, args: Dict[str, Any]) -> str:
        """Corps de ``pulse_vault_write`` — rend TOUJOURS une chaine JSON."""
        try:
            path = normalize_vault_path(args.get("path"))
            local_path, content = exclusive_source(args, "local_path")
            status, body, size = await asyncio.to_thread(
                self._vault_put_for_tool, chat_id, path, local_path=local_path, content=content
            )
        except VaultPathError as exc:
            return workspace_refused("invalid_request", f"Chemin de coffre refuse : {exc}")
        except WorkspaceToolError as exc:
            return workspace_refused(exc.code, exc.message)
        if not 200 <= status < 300:
            return http_refusal(status, body)
        return written_result(path, size)

    async def tool_publish_artifact(self, chat_id: str, args: Dict[str, Any]) -> str:
        """Corps de ``pulse_publish_artifact`` — rend TOUJOURS une chaine JSON.

        Aucun succes n'est annonce sans reponse 2xx de l'app : un modele qui
        croit avoir publie le dit a l'humain, et c'est exactement la panne que
        cet outil existe pour supprimer.
        """
        kind = args.get("kind")
        if not is_artifact_kind(kind):
            return workspace_refused("invalid_request", f"Type d'artifact inconnu : {kind!r}")
        title = normalize_title(args.get("title"))
        if not title:
            return workspace_refused("invalid_request", "Le titre de la carte est requis")
        try:
            path, content = exclusive_source(args, "path")
            if kind == "file" and content is not None:
                # Meme refus que ``publish_artifact`` : `content` part en UTF-8,
                # un binaire y arriverait corrompu sans erreur.
                raise WorkspaceToolError(
                    "invalid_request",
                    "kind='file' attend `path` : ecris d'abord le fichier avec pulse_vault_write",
                )
            raw_id = args.get("artifact_id")
            artifact_id = (
                raw_id.strip()
                if isinstance(raw_id, str) and raw_id.strip()
                else default_artifact_id(kind, title)
            )
            if path is None:
                path = default_artifact_path(kind, title, artifact_id)
            path = normalize_vault_path(path)
            if content is not None:
                status, body, _size = await asyncio.to_thread(
                    self._vault_put_for_tool, chat_id, path, local_path=None, content=content
                )
                if not 200 <= status < 300:
                    return http_refusal(status, body)
        except VaultPathError as exc:
            return workspace_refused("invalid_request", f"Chemin de coffre refuse : {exc}")
        except WorkspaceToolError as exc:
            return workspace_refused(exc.code, exc.message)

        payload = build_artifact_payload(
            channel_slug=chat_id, artifact_id=artifact_id, kind=kind, path=path, title=title
        )
        data = json.dumps(payload).encode("utf-8")
        status, body = await asyncio.to_thread(
            self._http_detailed,
            "POST",
            f"{self.base_url}/api/agent/messages",
            body=data,
            headers={"Content-Type": "application/json"},
        )
        if not 200 <= status < 300:
            return http_refusal(status, body)
        return published_result(artifact_id, kind, path)

    # ── Connecteurs tiers (Outlook, Teams, agenda) ───────────────────────

    async def call_connector(
        self,
        chat_id: str,
        capability: str,
        params: Optional[Dict[str, Any]] = None,
        on_behalf_of: Optional[str] = None,
        grant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Appelle une capacite de connecteur via l'app.

        Le plugin ne connait AUCUN identifiant OAuth, aucun scope, aucune URL
        Microsoft : il demande une capacite, l'app choisit le compte, resout le
        jeton et audite. Meme principe que le coffre-fort.

        Renvoie toujours un dict. En cas de succes : ``{"ok": True, ...}``. En cas
        d'echec : la forme de ``parse_connector_error``, avec ``ok`` a ``False``,
        un ``hint`` actionnable et ``retryable``. Aucune exception ne remonte —
        un connecteur en echec ne doit pas faire tomber le tour de parole.

        ⚠️ Sur ``connector_grant_ambiguous``, l'agent doit DEMANDER a l'humain
        lequel de ses comptes utiliser, puis rappeler avec ``grant_id``. Choisir
        soi-meme enverrait un courriel depuis la mauvaise boite.
        """
        try:
            url = connector_url(self.base_url, capability)
        except ConnectorCapabilityError as exc:
            # Refuse localement : inutile d'aller au reseau pour une capacite
            # que le serveur rejettera de toute facon.
            return {
                "ok": False,
                "status": 400,
                "code": "connector_unknown_capability",
                "message": str(exc),
                "hint": "Utiliser une capacite du catalogue.",
                "retryable": False,
                "ambiguous_options": [],
            }

        payload = build_connector_payload(chat_id, params, on_behalf_of, grant_id)
        return await asyncio.to_thread(self._connector_request, url, payload)

    def _connector_request(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Appel HTTP d'un connecteur (bloquant — via ``asyncio.to_thread``)."""
        body = json.dumps(payload).encode("utf-8")
        headers = self._auth_headers({"Content-Type": "application/json"})
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
                raw = response.read()
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
            return {"ok": True, **(parsed if isinstance(parsed, dict) else {"data": parsed})}
        except urllib.error.HTTPError as exc:
            detail: Any = None
            try:
                detail = json.loads(exc.read().decode("utf-8"))
            except Exception:
                detail = None
            result = parse_connector_error(exc.code, detail)
            logger.warning(
                "Pulse Chat: connecteur -> HTTP %s (%s)", exc.code, result["code"]
            )
            return result
        except Exception as exc:
            logger.warning("Pulse Chat: connecteur en echec — %s", exc)
            return parse_connector_error(0, None)

    # ── Approbations d'actions sensibles ─────────────────────────────────

    async def request_approval(
        self,
        chat_id: str,
        tool: str,
        command: str,
        reason: Optional[str] = None,
        options: Optional[List[str]] = None,
        timeout: Optional[float] = None,
        request_id: Optional[str] = None,
        summary: Optional[str] = None,
        risks: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Soumet une action sensible a l'approbation humaine et ATTEND la reponse.

        Retourne TOUJOURS un dict de la meme forme, avec un booleen
        ``granted`` : ``True`` seulement si un humain a explicitement
        autorise. Un echec de POST ou un ``timeout`` donne
        ``granted=False`` (et ``status`` vaut alors ``not_sent`` ou
        ``timeout``) — il n'y a aucun chemin par lequel un incident technique
        puisse ressembler a une autorisation.

        L'attente survit a une coupure du WebSocket : la decision prise
        pendant la coupure est rejouee par l'app a la reconnexion (hello). Une
        reemission du meme ``request_id`` sur une demande DEJA tranchee se fait
        repondre la decision telle quelle — elle ne rouvre rien et ne bloque
        pas.

        ``summary`` (une phrase : ce que l'agent veut faire) et ``risks`` (ce
        que ca touche, une ligne par point) sont facultatifs mais ce sont eux
        que l'humain lit avant de cliquer : un script de trente lignes ne se
        lit pas, une liste de trois impacts si.
        """
        request_id = request_id or f"req-{uuid.uuid4().hex}"
        payload = build_approval_payload(
            channel_slug=chat_id,
            request_id=request_id,
            tool=tool,
            command=command,
            reason=reason,
            options=options,
            summary=summary,
            risks=risks,
        )

        loop = asyncio.get_running_loop()
        future: "asyncio.Future[Dict[str, Any]]" = loop.create_future()
        self._pending_approvals[request_id] = future
        try:
            result = await self._post_agent_message(payload, request_id)
            if not result.success:
                logger.warning(
                    "Pulse Chat: demande d'approbation non postee (%s) — %s",
                    request_id,
                    getattr(result, "error", None),
                )
                return refusal(request_id, "not_sent")
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Pulse Chat: aucune reponse d'approbation pour %s en %ss — refus",
                request_id,
                timeout,
            )
            return refusal(request_id, "timeout")
        finally:
            self._pending_approvals.pop(request_id, None)

    @classmethod
    def supports_exec_approval_buttons(cls) -> bool:
        """Sonde du runner d'Hermes >= v2026.9.14 : « cet adaptateur rend-il une carte ? »

        Depuis ``ad305bead5`` (« send_exec_approval is a base template method »),
        ``BasePlatformAdapter`` definit lui-meme ``send_exec_approval``, et le
        runner (``gateway/run_turn_runner.py::_renders_exec_approval_buttons``)
        consulte CETTE sonde avant tout. Sa version de base ne dit oui que si
        ``_send_exec_approval_prompt`` est surcharge — ce qu'on ne fait pas :
        on surcharge ``send_exec_approval`` en entier. Heritee, elle repondait
        non, et Hermes postait son invite texte « Reply /approve… » sans
        qu'aucune erreur n'apparaisse.

        On repond oui plutot que d'implementer ``_send_exec_approval_prompt`` :
        un Hermes anterieur n'appelle pas ce crochet, et le plugin doit tourner
        sur les deux. Oui sans condition est sur : un echec d'envoi de la carte
        (``success=False``) fait reprendre l'invite texte par le runner.
        """
        return True

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[Dict[str, Any]] = None,
        allow_permanent: bool = True,
        allow_session: bool = True,
        smart_denied: bool = False,
    ) -> SendResult:
        """Rend le garde-fou d'Hermes sous forme de CARTE, pas d'invite texte.

        Point d'extension du gateway : il regarde si la CLASSE de l'adaptateur
        definit cette methode (depuis v2026.9.14, par la sonde
        ``supports_exec_approval_buttons`` ci-dessus). Sans elle, il retombe sur
        un message texte « tapez /approve » — lisible sur Telegram, absurde ici,
        ou l'app a une carte a boutons et une table ``approvalRequest``. C'est
        exactement ce qui se passait : la chaine d'approbation cote app etait
        intacte et n'etait jamais sollicitee.

        Elle POSTE et rend la main — elle n'attend PAS la decision, a l'inverse
        de ``request_approval()``. Hermes borne cet envoi a 15 s puis bloque le
        thread agent de son cote ; c'est ``resolve_gateway_approval`` qui le
        relache, depuis ``_handle_approval_reply``.

        Tout echec est rendu en ``SendResult(success=False)`` plutot qu'en
        exception : Hermes reprend alors SON invite texte. C'est la seule
        degradation acceptable — se taire laisserait l'humain devant un agent
        muet jusqu'a l'expiration du garde-fou.
        """
        if resolve_gateway_approval is None:
            # Poster la carte sans pouvoir debloquer le thread agent serait pire
            # que l'invite texte : des boutons sans effet, et l'agent fige
            # jusqu'au delai du garde-fou.
            return SendResult(
                success=False,
                error="tools.approval indisponible — pas de deblocage possible",
            )

        command = command or description or "(commande non transmise)"
        request_id = f"req-{uuid.uuid4().hex}"
        payload = build_approval_payload(
            channel_slug=str(chat_id),
            request_id=request_id,
            tool=GATEWAY_APPROVAL_TOOL,
            command=command,
            reason=description,
            options=gateway_options(
                allow_permanent=allow_permanent,
                allow_session=allow_session,
                smart_denied=smart_denied,
            ),
        )
        # Correle AVANT de poster : l'app peut repondre avant que le POST ne
        # retourne (demande deja tranchee, rejouee telle quelle) et la trame
        # arriverait sans destinataire.
        self._gateway_approvals[request_id] = str(session_key)
        while len(self._gateway_approvals) > _MAX_GATEWAY_APPROVALS:
            self._gateway_approvals.popitem(last=False)

        result = await self._post_agent_message(payload, request_id)
        if not result.success:
            self._gateway_approvals.pop(request_id, None)
            logger.warning(
                "Pulse Chat: carte d'approbation non postee (%s) — %s ; "
                "Hermes reprend l'invite texte",
                request_id,
                getattr(result, "error", None),
            )
        return result

    def _handle_approval_reply(self, data: Dict[str, Any]) -> None:
        """Trame ``approval.reply`` -> resolution de l'attente correspondante."""
        reply = parse_approval_reply(data)
        if reply is None:
            logger.warning("Pulse Chat: trame approval.reply inexploitable ignoree")
            return
        # Deux origines possibles, jamais les deux pour un meme requestId : le
        # garde-fou du gateway (l'attente vit chez Hermes) ou un appel explicite
        # a ``request_approval`` (l'attente vit ici).
        session_key = self._gateway_approvals.pop(reply["requestId"], None)
        if session_key is not None:
            resolved = resolve_gateway_approval(session_key, reply["decision"])
            if not resolved:
                # Le garde-fou d'Hermes a son propre delai (300 s par defaut) :
                # passe ce delai il a deja refuse, et le clic arrive trop tard.
                # On le trace plutot que de le taire — c'est la seule trace
                # qu'un humain a bien tranche, mais apres la fin.
                logger.info(
                    "Pulse Chat: decision %s pour %s sans attente cote Hermes "
                    "(garde-fou deja expire)",
                    reply["decision"],
                    reply["requestId"],
                )
            return
        future = self._pending_approvals.get(reply["requestId"])
        if future is None:
            # Rejeu d'une decision dont l'attente est deja retombee (timeout,
            # redemarrage du plugin) : rien a debloquer, on trace seulement.
            logger.info(
                "Pulse Chat: decision %s recue pour %s sans attente active",
                reply["decision"],
                reply["requestId"],
            )
            return
        if not future.done():
            future.set_result(reply)

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[List[Any]],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Rend la question d'Hermes sous forme de CARTE, pas d'invite texte.

        Point d'extension du gateway (``BasePlatformAdapter.send_clarify``, le
        jumeau de ``send_exec_approval``). Sans override, Hermes envoie sa liste
        numerotee « tapez le numero » — lisible sur Telegram, et ici pire que ca :
        ``classification.parse_tool`` lit « ❓ » + un mot comme un debut de tool
        progress, si bien que la question atterrissait en ``ToolEvent`` intitule
        « Quel », replie sous « 1 activite d'outil ». La seule facon de repondre
        etait de retaper le numero dans le composeur.

        Elle POSTE et rend la main — elle n'attend PAS la reponse, comme
        ``send_exec_approval``. L'attente vit dans le process HERMES
        (``tools/clarify_gateway``), et c'est ``resolve_gateway_clarify`` qui la
        relache depuis ``_handle_question_reply``.

        Tout echec est rendu en ``SendResult(success=False)`` plutot qu'en
        exception : le runner d'Hermes replanifie ALORS LUI-MEME l'invite texte
        (``run_turn_runner_clarify_delivery.text_fallback_coro``, qui teste que
        notre ``send_clarify`` n'est pas celui de la base). On ne rappelle donc
        surtout pas ``super()`` a la main sur ce chemin-la : ce serait DEUX
        invites pour une question.

        ⚠️ MULTI-SELECT exclu volontairement, et ce n'est pas un echec : la
        carte ne sait cocher qu'un choix, et Hermes attend alors un tableau JSON
        que seul son analyseur de texte (``_coerce_multi_select_text``) sait
        construire. On DELEGUE a la base — qui envoie la liste numerotee ET
        arme la capture de texte —, et on rend son resultat : un succes, pas un
        repli. Rendre ``success=False`` ferait envoyer la meme liste deux fois.
        """
        if resolve_gateway_clarify is None:
            # Poster la carte sans pouvoir debloquer le thread agent serait pire
            # que l'invite texte : des boutons sans effet, et l'agent fige
            # jusqu'au delai d'Hermes.
            return SendResult(
                success=False,
                error="tools.clarify_gateway indisponible — pas de deblocage possible",
            )

        if choices and self._clarify_is_multi_select(clarify_id):
            return await super().send_clarify(
                chat_id=chat_id,
                question=question,
                choices=choices,
                clarify_id=clarify_id,
                session_key=session_key,
                metadata=metadata,
            )

        payload = build_question_payload(
            channel_slug=str(chat_id),
            # Le ``clarify_id`` d'Hermes est repris TEL QUEL : c'est lui qui
            # debloque le thread agent. En regenerer un (comme le fait
            # ``send_exec_approval`` pour le garde-fou, dont Hermes ne donne
            # aucun identifiant) rendrait la reponse irresoluble.
            request_id=str(clarify_id),
            question=question,
            choices=choices,
        )
        # Correle AVANT de poster : l'app peut repondre avant que le POST ne
        # retourne (question deja repondue, rejouee telle quelle) et la trame
        # arriverait sans destinataire. Le slug sert aussi au RETRAIT, qui ne
        # recoit pas de chat_id.
        self._clarify_cards[str(clarify_id)] = str(chat_id)
        while len(self._clarify_cards) > _MAX_CLARIFY_CARDS:
            self._clarify_cards.popitem(last=False)

        result = await self._post_agent_message(payload, str(clarify_id))
        if not result.success:
            self._clarify_cards.pop(str(clarify_id), None)
            logger.warning(
                "Pulse Chat: carte de question non postee (%s) — %s ; "
                "Hermes reprend l'invite texte",
                clarify_id,
                getattr(result, "error", None),
            )
        return result

    @staticmethod
    def _clarify_is_multi_select(clarify_id: str) -> bool:
        """Le drapeau multi-select vit sur l'entree, pas dans la signature.

        ``send_clarify`` ne le recoit pas : Hermes l'a range sur
        ``_ClarifyEntry.multi_select`` pour garder la signature compatible avec
        les adaptateurs existants, et sa propre implementation de base va le
        relire la. On fait pareil, avec la meme garde large — un interne de
        module qui bouge ne doit pas faire perdre la question, seulement son
        rendu en carte.
        """
        try:
            from tools import clarify_gateway as _cg

            with _cg._lock:  # type: ignore[attr-defined]
                entry = _cg._entries.get(clarify_id)  # type: ignore[attr-defined]
                return bool(getattr(entry, "multi_select", False))
        except Exception:
            return False

    async def retire_clarify_card(self, clarify_id: str, notice: str) -> None:
        """Referme une carte dont la question s'est eteinte SANS clic.

        Appelee par le gateway quand il relache l'attente (delai, ``/new``,
        prose libre qui supplante la question). Sans elle, la carte continuerait
        d'afficher ses boutons : un chemin de reponse que la question relachee
        ne peut plus accepter, et un clic qui ne dirait rien —
        ``resolve_gateway_clarify`` rendant simplement ``False``. C'est
        exactement la panne muette que cette carte existe pour supprimer.

        ⚠️ Ne leve JAMAIS : le gateway la planifie sans attendre son resultat, et
        une exception ici remonterait dans une tache detachee. Une carte qu'on
        n'a pas su refermer reste affichee en attente — soit le comportement
        d'avant cette methode.

        ``notice`` n'est pas transmis : cf. ``build_question_retire_payload``.
        """
        channel_slug = self._clarify_cards.pop(str(clarify_id), None)
        if channel_slug is None:
            # Jamais posee en carte (multi-select, repli texte), ou deja
            # refermee : rien a retirer.
            return
        try:
            result = await self._post_agent_message(
                build_question_retire_payload(channel_slug, str(clarify_id)),
                str(clarify_id),
            )
            if not result.success:
                logger.info(
                    "Pulse Chat: retrait de la carte de question %s non poste — %s",
                    clarify_id,
                    getattr(result, "error", None),
                )
        except Exception:
            logger.exception("Pulse Chat: retrait de la carte de question en echec")

    def _handle_question_reply(self, data: Dict[str, Any]) -> None:
        """Trame ``question.reply`` -> deblocage du thread agent chez Hermes."""
        reply = parse_question_reply(data)
        if reply is None:
            logger.warning("Pulse Chat: trame question.reply inexploitable ignoree")
            return
        # La carte est terminale des qu'une reponse arrive : plus rien a
        # retirer. Depile AVANT de resoudre — un retrait planifie entre-temps ne
        # doit pas poster un « expire » sur une question repondue.
        self._clarify_cards.pop(reply["requestId"], None)
        if resolve_gateway_clarify is None:  # pragma: no cover - garde d'import
            logger.warning(
                "Pulse Chat: reponse recue pour %s mais tools.clarify_gateway absent",
                reply["requestId"],
            )
            return
        if not resolve_gateway_clarify(reply["requestId"], reply["answer"]):
            # Deux causes, indistinguables ici et sans consequence : Hermes a
            # deja relache son attente (delai, /new), ou le bot a redemarre
            # depuis — l'entree vit en MEMOIRE de process, elle ne survit pas.
            # C'est la seule trace qu'un humain a bien repondu, mais apres la
            # fin. Le TEXTE de la reponse n'est pas journalise : il peut porter
            # du contenu client.
            logger.info(
                "Pulse Chat: reponse pour %s sans attente cote Hermes "
                "(question deja relachee ou bot redemarre)",
                reply["requestId"],
            )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """No-op V1 (pas d'indicateur de frappe cote app)."""
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "group"}

    # ── HTTP sortant ─────────────────────────────────────────────────────

    # ── Demandes d'approbation DELIBEREES (pulse_request_approval) ──────

    async def open_gate(
        self,
        chat_id: str,
        request_id: str,
        title: str,
        body: str,
        structured: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """POSTe la demande. Rend ``None`` si elle est ouverte, sinon le JSON de refus.

        S'execute sur la boucle du WebSocket. Un refus de l'app est lu dans son
        CORPS et rendu tel quel : ``no_approver_configured`` doit arriver a
        l'agent avec sa raison, pas comme un « HTTP 422 » qu'il ne saurait pas
        expliquer a l'humain.

        Une app ANTERIEURE aux rubriques structurees refuse leurs champs en 400
        (``unrecognized_keys``) : on reposte alors UNE fois la meme demande en
        titre + corps (``legacy_payload``), rubriques repliees en Markdown. Seul
        ce refus-la declenche le repli — un 400 portant un ``code`` (piece
        jointe introuvable, prefixe manquant) est rendu a l'agent tel quel.
        """
        payload = build_gate_payload(
            channel_slug=chat_id,
            request_id=request_id,
            title=title,
            body=body,
            structured=structured,
        )
        url = f"{self.base_url}/api/agent/messages"
        outcome = await self._post_gate(url, payload, request_id)
        if outcome is None:
            return None
        status, detail = outcome
        if has_structured(payload) and is_unknown_fields_refusal(status, detail):
            logger.warning(
                "Pulse Chat: l'app ne connait pas les rubriques d'approbation (%s) — "
                "repli en titre + corps ; mettre l'app a jour",
                request_id,
            )
            outcome = await self._post_gate(url, legacy_payload(payload), request_id)
            if outcome is None:
                return None
            status, detail = outcome
        if status < 0:
            return refused_result("not_sent", str(detail))
        err = parse_error_body(status, detail)
        logger.warning(
            "Pulse Chat: demande d'approbation refusee (%s) — HTTP %s %s",
            request_id,
            status,
            err["code"],
        )
        return refused_result(err["code"], err["message"])

    async def _post_gate(self, url: str, payload: Dict[str, Any], request_id: str):
        """Un POST. ``None`` si accepte, sinon ``(status, corps_json)`` — status
        ``-1`` pour un echec de transport (le « corps » est alors le message)."""
        try:
            status = await asyncio.to_thread(self._post_json, url, payload)
        except urllib.error.HTTPError as exc:
            detail: Any = None
            try:
                detail = json.loads(exc.read().decode("utf-8"))
            except Exception:
                detail = None
            return exc.code, detail
        except Exception as exc:
            logger.warning("Pulse Chat: demande d'approbation non postee (%s) — %s", request_id, exc)
            return -1, str(exc)
        if status >= 400:
            return status, None
        return None

    async def _handle_gate_reply(self, data: Dict[str, Any]) -> None:
        """Trame ``gate.reply`` : debloque l'outil qui attend, ou PREVIENT l'agent.

        Deux cas, et le second n'est pas une anomalie :
          - l'outil attend encore -> on resout sa Future, il rend la decision ;
          - personne n'attend (fenetre de l'outil depassee, ou bot redemarre)
            -> la decision est injectee comme un MESSAGE entrant ordinaire.
            Se contenter de la journaliser laisserait l'agent arrete pour
            toujours sur une demande que quelqu'un a pourtant tranchee — et
            cette famille n'a delibere aucune echeance.
        """
        reply = parse_gate_reply(data)
        if reply is None:
            logger.warning("Pulse Chat: trame gate.reply inexploitable ignoree")
            return
        future = self._pending_gates.pop(reply["requestId"], None)
        if future is not None and not future.done():
            future.set_result(reply)
            return

        slug = reply["channelSlug"]
        if not slug:
            logger.warning("Pulse Chat: decision %s sans canal, ignoree", reply["requestId"])
            return
        # Dedup : le rejeu au hello peut renvoyer la meme decision. Deux
        # messages « approuve » feraient executer deux fois le meme plan.
        dedup_key = f"gate:{reply['requestId']}"
        if dedup_key in self._seen_message_ids:
            return
        source = self._last_source.get(slug) or self.build_source(
            chat_id=slug,
            chat_name=reply["channelName"] or slug,
            chat_type="group",
            user_id=None,
            user_name=reply["decidedBy"] or None,
        )
        event = build_message_event(
            MessageEvent,
            {
                "text": decision_message_text(reply),
                "message_type": MessageType.TEXT,
                "source": source,
                "message_id": dedup_key,
                "media_urls": [],
                "media_types": [],
            },
            None,
        )
        self._remember_message_id(dedup_key)
        logger.info(
            "Pulse Chat: decision %s pour %s sans attente active — remise a l'agent en message",
            reply["decision"],
            reply["requestId"],
        )
        await self.handle_message(event)

    async def _handle_browser_control(self, data: Dict[str, Any]) -> None:
        """Trame ``browser.control`` : reveille l'outil qui attend, ou RELANCE l'agent.

        Meme raison que ``_handle_gate_reply`` : un humain qui rend la main
        apres les 270 s de ``pulse_browser_handoff`` (l'outil a rendu
        ``pending``), ou alors que le tour de l'agent est deja fini, ne
        reveille personne — sans message entrant, l'agent ne reprend jamais sa
        tache. La decision vit dans ``browser.handle_control`` (pure, testee
        seule) ; ici, on ne fait que l'injecter.
        """
        notice = browser_provider.handle_control(data)
        if notice is None:
            return
        slug = notice["channelSlug"]
        source = self._last_source.get(slug) or self.build_source(
            chat_id=slug,
            chat_name=notice["channelName"] or slug,
            chat_type="group",
            user_id=None,
            user_name=notice["by"],
        )
        # Unique par rendu : chaque rendu est un fait nouveau (pas de rejeu au
        # hello pour cette trame), et deux rendus successifs doivent tous deux
        # relancer l'agent.
        message_id = f"browser:{notice['sessionId']}:{uuid.uuid4().hex[:12]}"
        event = build_message_event(
            MessageEvent,
            {
                "text": notice["text"],
                "message_type": MessageType.TEXT,
                "source": source,
                "message_id": message_id,
                "media_urls": [],
                "media_types": [],
            },
            self._last_agent_config.get(slug),
        )
        self._remember_message_id(message_id)
        logger.info(
            "Pulse Chat: main rendue (%s) sur le navigateur %s sans attente active — agent relance dans %s",
            notice["event"],
            notice["sessionId"],
            slug,
        )
        await self.handle_message(event)

    async def _post_agent_message(
        self, payload: Dict[str, Any], hermes_id: str
    ) -> SendResult:
        url = f"{self.base_url}/api/agent/messages"
        try:
            status = await asyncio.to_thread(self._post_json, url, payload)
        except urllib.error.HTTPError as exc:
            kind = self._error_kind_for_status(exc.code)
            retryable = self._is_retryable_status(exc.code)
            logger.warning(
                "Pulse Chat: POST /api/agent/messages -> HTTP %s (%s)", exc.code, kind
            )
            return SendResult(
                success=False,
                error=f"HTTP {exc.code}: {exc.reason}",
                retryable=retryable,
                error_kind=kind,
            )
        except Exception as exc:
            logger.warning("Pulse Chat: POST /api/agent/messages en echec — %s", exc)
            return SendResult(
                success=False, error=str(exc), retryable=True, error_kind="transient"
            )
        if status >= 400:
            return SendResult(
                success=False,
                error=f"HTTP {status}",
                retryable=self._is_retryable_status(status),
                error_kind=self._error_kind_for_status(status),
            )
        return SendResult(success=True, message_id=hermes_id)

    async def _post_todo_plan(self, payload: Dict[str, Any], message_id: str) -> None:
        """Poste une carte de plan, dans l'ORDRE des ecritures de l'agent.

        Jamais d'exception vers l'appelant : c'est un filet d'affichage, pas un
        envoi que l'agent attend. Un echec se journalise ; un 400 dit le plus
        souvent « app anterieure au champ ``todos`` » (schema ``.strict()``).
        """
        if self._todo_lock is None:
            self._todo_lock = asyncio.Lock()
        async with self._todo_lock:
            try:
                result = await self._post_agent_message(payload, message_id)
            except Exception as exc:  # pragma: no cover - filet
                logger.debug("Pulse Chat: plan de taches non poste — %s", exc)
                return
        if not getattr(result, "success", False):
            error = getattr(result, "error", "") or ""
            if "400" in error:
                _warn_once(
                    "todo_plan_400",
                    "Pulse Chat: plan de taches refuse (HTTP 400) — l'app est "
                    "probablement anterieure au champ `todos` ; la carte "
                    "n'apparaitra qu'apres sa mise a jour.",
                )
            else:
                logger.debug("Pulse Chat: plan de taches non poste — %s", error)

    def _post_json(self, url: str, payload: Dict[str, Any]) -> int:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._auth_headers({"Content-Type": "application/json"}),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
            response.read()
            return int(response.status)

    @staticmethod
    def _is_retryable_status(status: int) -> bool:
        """L'envoi vaut-il d'etre rejoue plus tard ?

        Le 409 de ``POST /api/agent/messages`` est TRANSITOIRE : sur cette
        route, il ne signifie qu'une chose — « emetteur indeterminable »,
        c'est-a-dire un canal multi-agents servi sans jeton de session. Or la
        fermeture du WebSocket revoque la session cote serveur et le plugin
        n'en retrouve une qu'au prochain ``hello.ack``, apres tout le backoff
        de reconnexion. Pendant cette fenetre (un redeploiement de l'app, par
        exemple), le classer en definitif jetait la reponse en cours de
        l'agent avec un simple avertissement : elle disparaissait.
        """
        return status in (409, 429) or status >= 500

    @staticmethod
    def _error_kind_for_status(status: int) -> str:
        if status in (401, 403):
            return "forbidden"
        if status == 404:
            return "not_found"
        if status == 409:
            # Session revoquee / pas encore rouverte — cf. _is_retryable_status.
            return "transient"
        if status == 413:
            return "too_long"
        if status == 429:
            return "rate_limited"
        if status >= 500:
            return "transient"
        return "unknown"


# ---------------------------------------------------------------------------
# Enregistrement du plugin
# ---------------------------------------------------------------------------

def parse_target_ref(ref: str):
    """Cible native de Pulse Chat : un ``Channel.slug``, rendu TEL QUEL.

    Hermes resout la cible d'un envoi (``send_message``, livraison d'un cron
    ``deliver=``, ``react``/``unreact``) en trois temps : le parseur declare par
    le plugin, puis les regles generiques d'Hermes, puis l'annuaire de canaux.
    Un slug Pulse Chat (``general``, ``rt-1a2b3c4d``, ``ch-...``) n'est ni
    numerique ni l'une des syntaxes natives qu'Hermes connait : il tombait donc
    dans l'annuaire, qui ne contient AUCUNE entree Pulse Chat, et l'envoi
    echouait sur un « Could not resolve » alors que le canal existe. Declarer ce
    parseur est ce qui remplace le patch qui reecrivait
    ``/opt/hermes/tools/send_message_tool.py`` a l'installation : la regle de
    routage vit chez le plugin qui possede la syntaxe, pas dans une copie
    modifiee du coeur d'Hermes qu'il faut reappliquer a chaque version.

    Le slug est rendu SANS ETRE VALIDE, et c'est delibere : l'app est la seule
    autorite sur l'existence d'un canal et sur le droit d'y ecrire (elle repond
    404, comme partout ailleurs dans ce plugin). Un motif de slug code ici
    serait une seconde regle a tenir d'accord avec celle de l'app — et le jour
    ou l'app ouvrirait une nouvelle forme de slug, l'envoi echouerait ici sans
    qu'aucune erreur ne designe la cause.

    Toujours ``thread_id = None`` : un canal Pulse Chat n'a pas de
    sous-conversation.

    ``None`` sur une chaine vide, jamais ``("", None)`` : Hermes n'appelle ce
    parseur qu'avec une cible EXPLICITE (une cible absente part sur le canal
    d'accueil avant d'arriver ici), donc le cas ne se presente pas — mais rendre
    ``("", None)`` ferait poster dans un canal que personne n'a nomme, la ou
    ``None`` laisse Hermes poursuivre sa resolution.
    """
    slug = (ref or "").strip()
    return (slug, None) if slug else None


def check_requirements() -> bool:
    """Env minimale presente (chemin `hermes setup` / requirements check)."""
    return bool(os.getenv("PULSE_CHAT_URL") and os.getenv("PULSE_CHAT_TOKEN"))


def validate_config(config) -> bool:
    """La config (env > extra) permet-elle de se connecter ?"""
    extra = getattr(config, "extra", {}) or {}
    url = os.getenv("PULSE_CHAT_URL") or extra.get("url", "")
    token = os.getenv("PULSE_CHAT_TOKEN") or extra.get("token", "")
    return bool(url and token)


def _live_adapter() -> Optional["PulseChatAdapter"]:
    """L'adaptateur qui tient le WebSocket, ou ``None``."""
    for adapter in list(_LIVE_ADAPTERS):
        if getattr(adapter, "is_connected", False) and getattr(adapter, "_loop", None) is not None:
            return adapter
    return None


async def _pulse_request_approval(args: Dict[str, Any], **_kwargs: Any) -> str:
    """Handler de ``pulse_request_approval`` — rend TOUJOURS une chaine JSON.

    Aucun chemin n'aboutit a ``granted: true`` sans une decision ``approved``
    venue de l'app : un incident technique rend un refus, jamais un accord.

    Le CANAL vient du contexte de session d'Hermes (``ContextVar`` task-local,
    propagee au thread de l'outil), jamais d'un argument : le modele ne peut
    pas soumettre une demande au nom d'une conversation ou il n'est pas.
    """
    try:
        from gateway.session_context import get_session_env

        chat_id = str(get_session_env("HERMES_SESSION_CHAT_ID", "") or "")
    except Exception:
        chat_id = ""
    if not chat_id:
        return refused_result("no_channel", "Aucune conversation Pulse Chat en cours")

    adapter = _live_adapter()
    if adapter is None or adapter._loop is None:
        return refused_result("not_connected", "Pulse Chat est injoignable")

    title = str(args.get("title") or "").strip()
    body = str(args.get("body") or "").strip()
    # Transportees, pas jugees : l'app valide (prefixe des pieces jointes,
    # references resolubles, bornes). Le plugin ne fait que les mettre en forme.
    structured = structured_fields(args)
    if not title:
        return refused_result("not_sent", "Le titre de la demande est requis")
    if not body and not structured:
        return refused_result(
            "gate_body_required",
            "Donne un corps (`body`) ou au moins une rubrique (`reason`, `steps`…)",
        )

    request_id = f"gate-{uuid.uuid4().hex}"
    decision: "concurrent.futures.Future[Dict[str, Any]]" = concurrent.futures.Future()
    # Armee AVANT le POST : une demande approuvee d'office recoit sa decision
    # par le WebSocket avant meme la reponse HTTP.
    adapter._pending_gates[request_id] = decision
    try:
        posted = asyncio.run_coroutine_threadsafe(
            adapter.open_gate(chat_id, request_id, title, body, structured=structured), adapter._loop
        )
        refusal_json = await asyncio.wrap_future(posted)
        if refusal_json is not None:
            return refusal_json
        try:
            reply = await asyncio.wait_for(asyncio.wrap_future(decision), timeout=GATE_WAIT_SECONDS)
        except asyncio.TimeoutError:
            # La demande RESTE ouverte cote app ; la decision arrivera plus tard
            # en message (``_handle_gate_reply``), puisqu'on retire l'attente.
            return pending_result(request_id)
        return tool_result(reply)
    finally:
        adapter._pending_gates.pop(request_id, None)


def _is_delegated_child() -> bool:
    """Vrai pendant l'execution d'un enfant de ``delegate_task``."""
    try:
        from agent.delegation_context import is_delegated_child_context

        return bool(is_delegated_child_context())
    except Exception:
        return False


def _forward_todo_plan(result: Any, *, turn_id: str, task_id: str, session_id: str) -> None:
    """Relaie le plan de taches de l'agent vers la carte du fil.

    Tout ce qui ne va pas s'arrete ici EN SILENCE (journal debug) : ce chemin
    est un affichage, il ne doit jamais faire echouer un tour d'Hermes.

    Le canal vient du contexte de session (``ContextVar`` copiee jusqu'au
    thread du hook, cf. docstring de ``_on_post_tool_call``), jamais d'un
    argument : meme regle que ``pulse_request_approval``.
    """
    try:
        from gateway.session_context import get_session_env

        platform = str(get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip().lower()
        chat_id = str(get_session_env("HERMES_SESSION_CHAT_ID", "") or "").strip()
    except Exception:
        return
    # STRICT : une plateforme vide ne prouve pas qu'on parle a Pulse Chat (CLI,
    # cron), et un identifiant de chat Telegram poste ici ferait un 404 de plus.
    if platform != "pulse_chat" or not chat_id:
        return
    # Le plan d'un sous-agent n'est pas celui que la conversation suit : il
    # ferait apparaitre une seconde carte qui ne dit pas qui la tient.
    if _is_delegated_child():
        return
    steps = parse_todo_result(result)
    if steps is None:
        logger.debug("Pulse Chat: resultat todo_list illisible — aucune carte")
        return
    message_id = todo_message_id(turn_id=turn_id, task_id=task_id, session_id=session_id)
    if message_id is None:
        logger.debug("Pulse Chat: ni turn_id ni task_id ni session_id — aucune carte de plan")
        return
    adapter = _live_adapter()
    if adapter is None or adapter._loop is None:
        logger.debug("Pulse Chat: plan de taches non relaye (adaptateur deconnecte)")
        return
    turns = adapter._todo_turns
    if not steps and message_id not in turns:
        # Une lecture d'un plan vide, sans carte ce tour-ci : rien a montrer.
        # Mais si ce tour a DEJA une carte, l'agent vient de vider son plan et
        # la carte doit le dire, sinon elle resterait sur l'etat d'avant.
        return
    turns[message_id] = None
    turns.move_to_end(message_id)
    while len(turns) > _MAX_TODO_TURNS:
        turns.popitem(last=False)
    payload = build_todo_payload(chat_id, steps, raw_of(result), message_id)
    # Planifie sur la boucle du WebSocket, SANS attendre : le thread du hook
    # rend la main aussitot. L'ordre des envois est garanti par le verrou de
    # ``_post_todo_plan`` (``asyncio.Lock`` est FIFO, et les hooks d'un meme
    # agent sont appeles l'un apres l'autre).
    asyncio.run_coroutine_threadsafe(adapter._post_todo_plan(payload, message_id), adapter._loop)


def _on_post_tool_call(
    tool_name: str = "",
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    turn_id: str = "",
    **_kwargs: Any,
) -> None:
    """Hook ``post_tool_call`` d'Hermes — ne regarde QUE ``todo_list``/``todo``.

    Hermes (v2026.9.24, ``hermes_cli/plugins_dispatch.py``) fait tourner ce
    rappel sur un thread demon ``hermes-hook-*``, borne a 30 s
    (``plugins.hook_callback_timeout``), sous ``contextvars.copy_context()`` du
    thread qui a execute l'outil — la boucle d'agent, elle-meme lancee par la
    passerelle sous ``copy_context`` apres ``set_session_vars``. Le contexte
    de session y est donc VISIBLE. La valeur de retour est ignoree.

    Appele pour CHAQUE outil : le nom est teste en premier, et tout autre outil
    ressort sans rien lire. Aucune exception ne remonte dans Hermes.
    """
    if tool_name not in TODO_TOOL_NAMES:
        return None
    try:
        _forward_todo_plan(
            result,
            turn_id=str(turn_id or ""),
            task_id=str(task_id or ""),
            session_id=str(session_id or ""),
        )
    except Exception as exc:
        logger.debug("Pulse Chat: plan de taches ignore — %s", exc)
    return None


def _register_todo_hook(ctx) -> None:
    """Hook du plan de taches. Isole : un echec ne fait tomber ni la
    plateforme ni les outils."""
    register_hook = getattr(ctx, "register_hook", None)
    if register_hook is None:
        _warn_once(
            "todo_hook_missing",
            "Pulse Chat: cet Hermes ne connait pas register_hook — le plan de "
            "taches de l'agent n'apparaitra pas dans le fil.",
        )
        return
    try:
        register_hook("post_tool_call", _on_post_tool_call)
    except Exception as exc:
        _warn_once("todo_hook_failed", f"Pulse Chat: hook du plan de taches non enregistre — {exc}")


def _session_chat_id() -> str:
    """Canal de la conversation en cours, lu dans le contexte de session."""
    try:
        from gateway.session_context import get_session_env

        return str(get_session_env("HERMES_SESSION_CHAT_ID", "") or "")
    except Exception:
        return ""


async def _run_workspace_tool(method_name: str, args: Dict[str, Any]) -> str:
    """Execute un outil de coffre sur la boucle du WEBSOCKET.

    Meme raison que ``pulse_request_approval`` : ``model_tools._run_async`` fait
    tourner le handler sur une AUTRE boucle, dans un thread ; l'adaptateur (son
    jeton de session, qui dit a l'app quel agent ecrit) vit sur la sienne.
    """
    chat_id = _session_chat_id()
    if not chat_id:
        return workspace_refused("no_channel", "Aucune conversation Pulse Chat en cours")
    adapter = _live_adapter()
    if adapter is None or adapter._loop is None:
        return workspace_refused("not_connected", "Pulse Chat est injoignable")
    try:
        future = asyncio.run_coroutine_threadsafe(
            getattr(adapter, method_name)(chat_id, dict(args or {})), adapter._loop
        )
        return await asyncio.wrap_future(future)
    except Exception as exc:
        # Filet : un outil qui leve ferait croire au modele a une panne d'outil
        # sans motif. On rend un refus lisible, jamais un succes.
        logger.warning("Pulse Chat: outil %s en echec — %s", method_name, exc)
        return workspace_refused("not_sent", str(exc))


async def _pulse_vault_write(args: Dict[str, Any], **_kwargs: Any) -> str:
    """Handler de ``pulse_vault_write``."""
    return await _run_workspace_tool("tool_vault_write", args)


async def _pulse_publish_artifact(args: Dict[str, Any], **_kwargs: Any) -> str:
    """Handler de ``pulse_publish_artifact``."""
    return await _run_workspace_tool("tool_publish_artifact", args)


def _register_workspace_tools(ctx) -> None:
    """Outils de coffre et de publication. Chacun isole : l'echec de l'un
    n'empeche ni l'autre ni la plateforme."""
    for name, schema, handler, description, emoji in (
        (VAULT_WRITE_TOOL_NAME, VAULT_WRITE_SCHEMA, _pulse_vault_write, VAULT_WRITE_DESCRIPTION, "🗄️"),
        (PUBLISH_TOOL_NAME, PUBLISH_SCHEMA, _pulse_publish_artifact, PUBLISH_DESCRIPTION, "📎"),
    ):
        try:
            registered = ctx.register_tool(
                name=name,
                toolset="pulse_chat",
                schema=schema,
                handler=handler,
                is_async=True,
                description=description,
                emoji=emoji,
            )
            if registered is None:
                _warn_once(
                    f"{name}_shadowed",
                    f"Pulse Chat: l'outil {name} n'a pas ete enregistre (nom deja pris ?)",
                )
        except Exception as exc:
            _warn_once(f"{name}_failed", f"Pulse Chat: outil {name} non enregistre — {exc}")


def _register_gate_tool(ctx) -> None:
    """Outil + skill d'approbation. Jamais bloquant pour la plateforme."""
    try:
        registered = ctx.register_tool(
            name=GATE_TOOL_NAME,
            # Le NOM de la plateforme : c'est ce qui verse l'outil dans le
            # toolset ``hermes-pulse_chat`` a cote des outils coeur, sans
            # configuration d'operateur (``toolsets.py``, vue derivee du registre).
            toolset="pulse_chat",
            schema=GATE_TOOL_SCHEMA,
            handler=_pulse_request_approval,
            is_async=True,
            description=GATE_TOOL_DESCRIPTION,
            emoji="🛂",
        )
        if registered is None:
            # ``register_tool`` rend None quand le nom est deja pris : le dire,
            # sinon l'outil manque sans qu'aucune trace ne l'explique.
            _warn_once(
                "gate_tool_shadowed",
                f"Pulse Chat: l'outil {GATE_TOOL_NAME} n'a pas ete enregistre (nom deja pris ?)",
            )
    except Exception as exc:
        _warn_once("gate_tool_failed", f"Pulse Chat: outil {GATE_TOOL_NAME} non enregistre — {exc}")

    try:
        skill_path = pathlib.Path(__file__).resolve().parent / "skills" / GATE_SKILL_NAME / "SKILL.md"
        ctx.register_skill(
            GATE_SKILL_NAME,
            skill_path,
            description="Quand et comment soumettre un plan ou un livrable a l'approbation (pulse_request_approval).",
        )
    except Exception as exc:
        _warn_once("gate_skill_failed", f"Pulse Chat: skill {GATE_SKILL_NAME} non enregistre — {exc}")


#: Skill GENERAL (``skills/guide/SKILL.md``, resolu en ``pulse-chat:guide``).
#: Il vivait a la racine du depot sous le nom ``pulse-chat`` et n'etait
#: enregistre NULLE PART : seul ``approvals`` passait par ``register_skill``,
#: donc le mode d'emploi des outils MCP (lecture de canal, plan de travail,
#: coffre) n'atteignait aucun agent — sans qu'aucune erreur ne le dise.
GUIDE_SKILL_NAME = "guide"


def _register_guide_skill(ctx) -> None:
    """Isole, comme les autres : un echec ne fait tomber ni la plateforme ni
    les outils."""
    try:
        skill_path = pathlib.Path(__file__).resolve().parent / "skills" / GUIDE_SKILL_NAME / "SKILL.md"
        ctx.register_skill(
            GUIDE_SKILL_NAME,
            skill_path,
            description=(
                "Se servir de Pulse Chat : lire un canal (channels_list, channel_context, "
                "channel_members), le plan de travail plan_*, le coffre et la publication "
                "d'un fichier, les connecteurs delegues, et quoi faire d'un refus."
            ),
        )
    except Exception as exc:
        _warn_once("guide_skill_failed", f"Pulse Chat: skill {GUIDE_SKILL_NAME} non enregistre — {exc}")


def _browser_adapter() -> Optional["PulseChatAdapter"]:
    """Adaptateur qui porte les appels du navigateur.

    Connecte de preference (son jeton de session, a jour, dit a l'app QUEL
    agent parle). A defaut, n'importe quel adaptateur vivant : une liberation
    lancee a ``atexit`` ou pendant une reconnexion tente sa chance plutot que
    d'abandonner la session a l'inactivite.
    """
    adapter = _live_adapter()
    if adapter is not None:
        return adapter
    for candidate in list(_LIVE_ADAPTERS):
        if getattr(candidate, "base_url", "") and getattr(candidate, "token", ""):
            return candidate
    return None


def _browser_http(
    method: str, path: str, payload: Optional[Dict[str, Any]], timeout: float
) -> Tuple[int, Any]:
    """Transport du fournisseur ``pulse`` : l'aide HTTP commune de l'adaptateur,
    donc les MEMES en-tetes que le coffre et les approbations (Bearer +
    ``x-hermes-session``). Sans ce dernier, l'app ne saurait pas quel agent
    ouvre un navigateur, et refuserait (session ``verified`` exigee)."""
    adapter = _browser_adapter()
    if adapter is None or not adapter.base_url:
        return 0, {"message": "Pulse Chat injoignable (aucun adaptateur actif)"}
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else None
    return adapter._http_detailed(
        method, f"{adapter.base_url}{path}", body=body, headers=headers, timeout=timeout
    )


def _browser_configured() -> bool:
    """URL et jeton poses — lecture de configuration seulement, aucun reseau.

    Appele au moment ou Hermes construit le schema des outils, donc souvent
    AVANT que l'adaptateur n'existe : l'environnement fait foi, l'adaptateur
    vivant (configuration par ``extra``) n'est qu'un complement."""
    try:
        if _get_secret("PULSE_CHAT_URL") and _get_secret("PULSE_CHAT_TOKEN"):
            return True
    except Exception:
        pass
    return any(
        getattr(a, "base_url", "") and getattr(a, "token", "") for a in list(_LIVE_ADAPTERS)
    )


def _browser_session_env(name: str) -> str:
    """Variable de session de la passerelle (``ContextVar``), ``""`` hors Hermes."""
    try:
        from gateway.session_context import get_session_env

        return str(get_session_env(name, "") or "")
    except Exception:
        return ""


def _browser_cloud_provider_setting() -> Optional[str]:
    """``browser.cloud_provider`` du config.yaml, ``""`` s'il n'est pas pose,
    ``None`` si la configuration est ILLISIBLE (hors Hermes, fichier casse).

    Lecture sans copie (``read_raw_config_readonly``, v2026.9.24) : appelee par
    un ``check_fn`` a chaque construction de schema, elle ne doit pas payer une
    copie profonde de toute la configuration. Repli sur ``read_raw_config``
    pour un Hermes qui ne l'a pas. Le resultat n'est JAMAIS modifie.
    """
    try:
        from hermes_cli import config as hermes_config

        reader = getattr(hermes_config, "read_raw_config_readonly", None) or hermes_config.read_raw_config
        browser_cfg = (reader() or {}).get("browser", {})
    except Exception:
        return None
    if isinstance(browser_cfg, dict) and "cloud_provider" in browser_cfg:
        return str(browser_cfg.get("cloud_provider") or "").strip().lower()
    return ""


def _handoff_tool_available() -> bool:
    """L'outil n'a de sens que si les outils de navigation passent par Pulse.

    Sans ``browser.cloud_provider: pulse``, il repondrait toujours « ouvre
    d'abord le navigateur » a un agent qui navigue deja — localement. Une
    configuration illisible ne cache rien : mieux vaut un outil de trop qu'une
    prise de main impossible.
    """
    setting = _browser_cloud_provider_setting()
    return setting is None or setting == browser_provider.PROVIDER_NAME


#: Paragraphe de ``platform_hint`` sur le navigateur. Le navigateur Pulse est VU
#: en direct par le canal : sans cette phrase, un agent bloque sur une page de
#: connexion tente le mot de passe, ou abandonne, au lieu de demander la main.
BROWSER_HINT = (
    "Browser: your browser is shown live in the conversation. When a page "
    "needs the human (login, captcha, SMS or 2FA code), call "
    "pulse_browser_handoff with a one-sentence reason and wait; on done, "
    "look at the page again before continuing; on pending, stop and tell "
    "the human in writing what you are waiting for — a [Navigateur] message "
    "will also wake you up when the hand is given back."
)


def _browser_hint(ctx) -> str:
    """Le paragraphe navigateur, SEULEMENT si le bot navigue vraiment par Pulse.

    ``platform_hint`` atteint TOUT agent Pulse Chat : sans cette condition,
    chaque bot existant — aucun n'a ``browser.cloud_provider: pulse`` le jour
    ou il recoit cette version — s'entendrait dire que son navigateur est vu
    en direct, et serait envoye vers un outil que son ``check_fn`` cache
    (« Unknown tool »). Plus strict que ``_handoff_tool_available`` : une
    configuration illisible n'ajoute RIEN — une affirmation fausse au modele
    coute plus qu'une phrase absente (la description de l'outil suffit a
    l'expliquer quand il est la). Evalue UNE fois, a l'enregistrement : changer
    le reglage demande un redemarrage de la passerelle, comme pour tout plugin.
    """
    if getattr(ctx, "register_browser_provider", None) is None:
        return ""
    if _browser_cloud_provider_setting() != browser_provider.PROVIDER_NAME:
        return ""
    return "\n\n" + BROWSER_HINT


def _register_browser(ctx) -> None:
    """Fournisseur ``pulse`` + outil de prise de main. Jamais bloquant.

    Un Hermes anterieur aux fournisseurs de navigateur n'a pas
    ``register_browser_provider`` : on le dit une fois et on s'arrete — l'outil
    seul ne servirait a rien, aucun navigateur Pulse ne pouvant s'ouvrir.
    """
    register_provider = getattr(ctx, "register_browser_provider", None)
    if register_provider is None:
        _warn_once(
            "browser_provider_missing",
            "Pulse Chat: cet Hermes ne connait pas register_browser_provider — "
            "le navigateur Pulse est indisponible (Hermes >= v2026.9.24 requis).",
        )
        return
    provider = browser_provider.PulseBrowserProvider(
        _browser_http,
        configured=_browser_configured,
        session_env=_browser_session_env,
    )
    try:
        registered = register_provider(provider)
    except Exception as exc:
        _warn_once("browser_provider_failed", f"Pulse Chat: fournisseur de navigateur non enregistre — {exc}")
        return
    if registered is None:
        _warn_once(
            "browser_provider_shadowed",
            f"Pulse Chat: fournisseur de navigateur '{browser_provider.PROVIDER_NAME}' non enregistre (nom deja pris ?)",
        )
    # Inscrit meme si l'enregistrement a rendu None : il ne peut alors avoir
    # ouvert aucune session, donc aucune trame ne le concerne — la lui
    # transmettre est sans effet.
    browser_provider.activate(provider)
    try:
        tool = ctx.register_tool(
            name=browser_provider.HANDOFF_TOOL_NAME,
            toolset="pulse_chat",
            schema=browser_provider.HANDOFF_TOOL_SCHEMA,
            handler=provider.handoff,
            check_fn=_handoff_tool_available,
            # SYNCHRONE : HTTP bloquant puis attente d'une Future thread-safe,
            # dans le fil d'outil (ContextVar propagees) — pas de boucle a
            # emprunter, contrairement a ``pulse_request_approval``.
            is_async=False,
            description=browser_provider.HANDOFF_TOOL_DESCRIPTION,
            emoji="🤝",
        )
        if tool is None:
            _warn_once(
                "handoff_tool_shadowed",
                f"Pulse Chat: l'outil {browser_provider.HANDOFF_TOOL_NAME} n'a pas ete enregistre (nom deja pris ?)",
            )
    except Exception as exc:
        _warn_once(
            "handoff_tool_failed",
            f"Pulse Chat: outil {browser_provider.HANDOFF_TOOL_NAME} non enregistre — {exc}",
        )


def register(ctx):
    """Point d'entree plugin : appele par le systeme de plugins Hermes."""
    entry = dict(
        name="pulse_chat",
        label="Pulse Chat",
        adapter_factory=lambda cfg: PulseChatAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["PULSE_CHAT_URL", "PULSE_CHAT_TOKEN"],
        install_hint="pip install websockets",
        # L'acces aux canaux est deja filtre cote app via ChannelMember —
        # ne pas dupliquer la regle ici (invariant CLAUDE.md).
        allow_all_env="PULSE_CHAT_ALLOW_ALL_USERS",
        # Pas de limite de longueur cote app.
        max_message_length=0,
        emoji="💬",
        pii_safe=False,
        platform_hint=(
            "You are chatting with external clients through Pulse Chat, a web "
            "chat front-end. Markdown formatting is fully supported, including "
            "fenced code blocks. When users share documents, they arrive as "
            "presigned URLs embedded in the message text — fetch them with your "
            "tools when needed (links expire after about 15 minutes, so read "
            "them promptly). Keep a professional, helpful tone with clients.\n\n"
            # Le SEUL texte qui atteint tout agent Pulse Chat sans
            # configuration (v2026.8.3) : ``register_system_prompt_section``
            # n'existe pas a cette version. Il nomme l'outil ET le skill, qu'un
            # plugin ne peut pas annoncer autrement (``register_skill`` = chargement
            # explicite seulement).
            "Approvals: when you hand in a deliverable to be validated (report, "
            "document, proposal), and before running a costly or hard-to-undo "
            "plan (overwriting documents, reprocessing real data, long jobs), "
            "call pulse_request_approval with a one-line title and a "
            "self-contained Markdown body, and act ONLY once it returns approved. "
            "It is NOT the consent for sending outside through a connector "
            "(email, GitHub...): that belongs to the account owner, and approved "
            "here never lifts connector_approval_required. On "
            "changes_requested, apply the comment and resubmit; on denied, stop; "
            "on pending, stop and tell the human you are waiting — the decision "
            "will reach you later as a message. Details: load the skill "
            "pulse-chat:approvals.\n\n"
            # Sans cette phrase, un agent qui a produit un PDF cherche un moyen
            # de le « joindre » et n'en trouve aucun : les deux outils existent
            # mais rien ne dit qu'ils vont ensemble, ni qu'ecrire n'affiche rien.
            "Files: to share a file you produced (PDF, spreadsheet, image, "
            "archive) in the conversation, call pulse_vault_write with its "
            "local_path, THEN pulse_publish_artifact with kind='file' and the "
            "same path. Writing alone shows nothing in the conversation. Never "
            "tell the human a file is available before pulse_publish_artifact "
            "returned published. If those two tools are missing, the Pulse Chat "
            "MCP server offers channel_vault_upload_url (then PUT the file) and "
            "channel_artifact_publish instead.\n\n"
            # Le seul texte qui atteint TOUT agent : sans lui, le skill guide
            # ci-dessous n'existe pour personne (``register_skill`` = chargement
            # explicite), et un agent a qui une publication a ete refusee
            # finissait par ecrire son propre client WebSocket.
            "Pulse Chat MCP tools (when connected): channels_list gives the "
            "`channel` slugs every other tool needs; channel_context reads what "
            "was said before you were called; channel_members tells who is here; "
            "plan_* is YOUR task board (never connector_tasks_*). Never open your "
            "own WebSocket to Pulse Chat nor call its /api/agent routes from a "
            "script: use these tools, and on a refusal follow its `hint`. "
            "Details: load the skill pulse-chat:guide."
            + _browser_hint(ctx)
        ),
    )
    # ``parse_target_ref_fn`` n'existe pas sur les Hermes anterieurs a
    # v2026.8.13 (absent de ``PlatformEntry`` en v2026.8.3, la version minimale
    # que ce plugin annonce), et ``register_platform`` fait remonter les kwargs
    # inconnus a ``PlatformEntry(**kwargs)`` — donc un ``TypeError``. Le passer
    # sans repli ne degraderait pas le routage : il ferait echouer
    # l'enregistrement de la PLATEFORME entiere, et le bot perdrait Pulse Chat
    # d'un coup. Le rejeu est sans danger : le ``TypeError`` est leve a la
    # construction de l'entree, avant toute inscription au registre.
    try:
        ctx.register_platform(parse_target_ref_fn=parse_target_ref, **entry)
    except TypeError:
        _warn_once(
            "parse_target_ref_kwarg",
            "Pulse Chat: cet Hermes ne connait pas 'parse_target_ref_fn' — la "
            "plateforme est enregistree sans parseur de cible. Un envoi vers "
            "'pulse_chat:<slug>' (send_message, livraison d'un cron 'deliver=') "
            "echouera sur un 'Could not resolve' ; mettre Hermes a jour "
            "(>= v2026.8.13). Les reponses dans le canal ne sont pas affectees.",
        )
        ctx.register_platform(**entry)
    _register_gate_tool(ctx)
    _register_workspace_tools(ctx)
    _register_guide_skill(ctx)
    _register_browser(ctx)
    _register_todo_hook(ctx)
