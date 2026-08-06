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
import json
import logging
import mimetypes
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from .approvals import build_approval_payload, parse_approval_reply, refusal
from .artifacts import (
    build_artifact_payload,
    default_artifact_id,
    default_artifact_path,
)
from .capabilities import collect_capabilities
from .vault import vault_url
from .classification import classify_outbound, parse_tool  # noqa: F401  (re-export)
from .hello import build_hello, parse_profiles
from .metrics import extract_metrics
from .reconnect import is_retryable_ws_error, reconnect_delay

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.config import Platform

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15.0
_WS_CONNECT_TIMEOUT = 30.0
_WS_MAX_SIZE = 10 * 1024 * 1024
_MEDIA_MAX_BYTES = 20 * 1024 * 1024
_MEDIA_MAX_COUNT = 10
# Dedup du rejeu serveur : cache borne des derniers message ids traites (un
# ``message.created`` deja vu est re-acke mais PAS re-dispatche a l'agent).
_DEDUP_MAX_IDS = 500


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

    @property
    def name(self) -> str:
        return "Pulse Chat"

    # ── Cycle de vie ─────────────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Ouvre le WebSocket vers l'app et demarre la boucle de reception."""
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
        headers = {"Authorization": f"Bearer {self.token}"}
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
                if data.get("type") == "message.created":
                    try:
                        await self._handle_message_created(data)
                    except Exception:
                        logger.exception("Pulse Chat: erreur de traitement message.created")
                elif data.get("type") == "approval.reply":
                    try:
                        self._handle_approval_reply(data)
                    except Exception:
                        logger.exception("Pulse Chat: erreur de traitement approval.reply")
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
        event = build_message_event(
            MessageEvent,
            {
                "text": message.get("text") or "",
                "message_type": MessageType.TEXT,
                "source": source,
                "message_id": str(message_id),
                "media_urls": media_urls,
                "media_types": media_types,
            },
            agent_config_metadata(data),
        )

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
        try:
            await self._ws.send(json.dumps({"type": "ack", "messageId": message_id}))
        except Exception as exc:
            logger.warning("Pulse Chat: echec envoi ack %s — %s", message_id, exc)

    # ── Media (images -> chemins locaux pour la vision) ──────────────────

    async def _download_media(
        self, urls: List[str]
    ) -> Tuple[List[str], List[str]]:
        """Telecharge localement les images de ``mediaUrls``.

        Seules les images sont materialisees (vision) ; les autres documents
        restent des URLs presignees dans le texte du message (l'agent les lit
        avec ses outils) — decision 2 du plan.
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
            if not mime.startswith("image/"):
                return None, None
            data = response.read(_MEDIA_MAX_BYTES + 1)
            if len(data) > _MEDIA_MAX_BYTES:
                logger.warning("Pulse Chat: image trop volumineuse ignoree (%s)", url)
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

        ``kind`` : mermaid | markdown | svg | html | drawio.

        Un artifact est un POINTEUR vers un fichier du coffre : le contenu
        n'existe qu'une fois, dans le coffre du canal. Deux usages :

        - ``content=`` : le contenu est ECRIT dans le coffre puis publie, en un
          seul appel. C'est le cas courant — l'agent a son contenu en memoire,
          lui demander deux appels serait un piege d'ergonomie.
        - ``path=``    : le fichier est deja dans le coffre, on le designe.

        L'``artifact_id`` porte l'IDENTITE : le republier met a jour la carte
        existante au lieu d'en ajouter une. Par defaut il est derive du TITRE,
        ce qui rend le comportement attendu automatique.

        Retourne l'``artifact_id`` utilise, ou ``None`` en cas d'echec.
        """
        if (content is None) == (path is None):
            raise ValueError("fournir soit `content`, soit `path` — jamais les deux")

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
        headers = {"Authorization": "Bearer %s" % self.token}
        if content_type:
            headers["Content-Type"] = content_type
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
    ) -> Dict[str, Any]:
        """Soumet une action sensible a l'approbation humaine et ATTEND la reponse.

        Retourne TOUJOURS un dict de la meme forme, avec un booleen
        ``granted`` : ``True`` seulement si un humain a explicitement
        autorise. Un echec de POST ou un ``timeout`` donne
        ``granted=False`` (et ``status`` vaut alors ``not_sent`` ou
        ``timeout``) — il n'y a aucun chemin par lequel un incident technique
        puisse ressembler a une autorisation.

        L'attente survit a une coupure du WebSocket : la decision prise
        pendant la coupure est rejouee par l'app a la reconnexion (hello).
        """
        request_id = request_id or f"req-{uuid.uuid4().hex}"
        payload = build_approval_payload(
            channel_slug=chat_id,
            request_id=request_id,
            tool=tool,
            command=command,
            reason=reason,
            options=options,
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

    def _handle_approval_reply(self, data: Dict[str, Any]) -> None:
        """Trame ``approval.reply`` -> resolution de l'attente correspondante."""
        reply = parse_approval_reply(data)
        if reply is None:
            logger.warning("Pulse Chat: trame approval.reply inexploitable ignoree")
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

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """No-op V1 (pas d'indicateur de frappe cote app)."""
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "group"}

    # ── HTTP sortant ─────────────────────────────────────────────────────

    async def _post_agent_message(
        self, payload: Dict[str, Any], hermes_id: str
    ) -> SendResult:
        url = f"{self.base_url}/api/agent/messages"
        try:
            status = await asyncio.to_thread(self._post_json, url, payload)
        except urllib.error.HTTPError as exc:
            kind = self._error_kind_for_status(exc.code)
            retryable = exc.code == 429 or exc.code >= 500
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
                retryable=status == 429 or status >= 500,
                error_kind=self._error_kind_for_status(status),
            )
        return SendResult(success=True, message_id=hermes_id)

    def _post_json(self, url: str, payload: Dict[str, Any]) -> int:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
            response.read()
            return int(response.status)

    @staticmethod
    def _error_kind_for_status(status: int) -> str:
        if status in (401, 403):
            return "forbidden"
        if status == 404:
            return "not_found"
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

def check_requirements() -> bool:
    """Env minimale presente (chemin `hermes setup` / requirements check)."""
    return bool(os.getenv("PULSE_CHAT_URL") and os.getenv("PULSE_CHAT_TOKEN"))


def validate_config(config) -> bool:
    """La config (env > extra) permet-elle de se connecter ?"""
    extra = getattr(config, "extra", {}) or {}
    url = os.getenv("PULSE_CHAT_URL") or extra.get("url", "")
    token = os.getenv("PULSE_CHAT_TOKEN") or extra.get("token", "")
    return bool(url and token)


def register(ctx):
    """Point d'entree plugin : appele par le systeme de plugins Hermes."""
    ctx.register_platform(
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
            "them promptly). Keep a professional, helpful tone with clients."
        ),
    )
