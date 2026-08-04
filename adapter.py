"""Adaptateur de plateforme Pulse Chat pour Hermes Agent.

Adaptateur MINCE : il traduit et transporte, aucune logique metier (elle vit
dans l'app Nuxt — cf. CLAUDE.md et spec §6).

Flux :
- app -> plugin : WebSocket ``ws(s)://<PULSE_CHAT_URL>/ws/hermes?token=...``
  (librairie ``websockets`` — ``pip install websockets``). Evenements
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

from .classification import classify_outbound, parse_tool  # noqa: F401  (re-export)

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


def _ws_url(base_url: str, token: str) -> str:
    """``http(s)://host[/path]`` -> ``ws(s)://host[/path]/ws/hermes?token=...``."""
    parts = urllib.parse.urlsplit(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    path = parts.path.rstrip("/")
    query = urllib.parse.urlencode({"token": token})
    return f"{scheme}://{parts.netloc}{path}/ws/hermes?{query}"


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

        channels = _get_secret("PULSE_CHAT_CHANNELS") or extra.get("channels", "")
        if isinstance(channels, str):
            self.channels = {c.strip() for c in channels.split(",") if c.strip()}
        else:
            self.channels = {str(c).strip() for c in (channels or []) if str(c).strip()}

        # Etat runtime
        self._ws = None
        self._recv_task: Optional[asyncio.Task] = None
        self._media_dir: Optional[str] = None
        # Cache borne (FIFO) des derniers message ids traites — dedup du rejeu.
        self._seen_message_ids: "OrderedDict[str, None]" = OrderedDict()

    @property
    def name(self) -> str:
        return "Pulse Chat"

    # ── Cycle de vie ─────────────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Ouvre le WebSocket vers l'app et demarre la boucle de reception."""
        if not self.base_url or not self.token:
            self._set_fatal_error(
                "config_missing",
                "PULSE_CHAT_URL et PULSE_CHAT_TOKEN doivent etre definis",
                retryable=False,
            )
            return False

        try:
            import websockets
        except ImportError:
            self._set_fatal_error(
                "missing_dependency",
                "librairie manquante — pip install websockets",
                retryable=False,
            )
            return False

        url = _ws_url(self.base_url, self.token)
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(url, max_size=_WS_MAX_SIZE),
                timeout=_WS_CONNECT_TIMEOUT,
            )
        except Exception as exc:
            logger.error("Pulse Chat: echec de connexion WS a %s — %s", self.base_url, exc)
            self._set_fatal_error("connect_failed", str(exc), retryable=True)
            return False

        self._recv_task = asyncio.create_task(self._receive_loop())
        self._mark_connected()
        logger.info(
            "Pulse Chat: connecte a %s (%s)",
            self.base_url,
            "reconnexion" if is_reconnect else "connexion initiale",
        )
        return True

    async def disconnect(self) -> None:
        """Arret propre : marque deconnecte, annule la boucle, ferme le WS."""
        self._mark_disconnected()

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
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Pulse Chat: erreur boucle de reception — %s", exc)
        finally:
            # Coupure inattendue (pas un disconnect() volontaire) : le gateway
            # pilote la reconnexion via connect(is_reconnect=True).
            if self.is_connected:
                self._set_fatal_error(
                    "connection_lost",
                    "WebSocket Pulse Chat ferme de maniere inattendue",
                    retryable=True,
                )
                await self._notify_fatal_error()

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
        event = MessageEvent(
            text=message.get("text") or "",
            message_type=MessageType.TEXT,
            source=source,
            message_id=str(message_id),
            media_urls=media_urls,
            media_types=media_types,
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
