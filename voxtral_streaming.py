# -*- coding: utf-8 -*-
"""Chunked streaming TTS provider for Mistral Voxtral.

Hermes ships four registered streamers (``elevenlabs``, ``openai``, ``gemini``,
``xai``). Mistral is not among them, and ``resolve_streaming_provider`` refuses
to swap providers behind the user's back — so an agent configured with
``tts.provider: mistral`` speaks only once the whole reply is written. This
module fills that hole: with it, Voxtral streams like the others.

Written in upstream shape (English, no third-party imports, same contract and
byte cap as ``tools/tts_streaming.py``) so it can be proposed to
NousResearch/hermes-agent as-is. Until then it registers itself from this
plugin, which is enough: ``_REGISTRY`` is a plain module dict and the lookup
happens per turn, long after plugin import.

API shape, verified against the live API on 2026-08-14::

    POST https://api.mistral.ai/v1/audio/speech
    {"model": …, "input": …, "voice_id": …, "response_format": "pcm",
     "stream": true}
    → text/event-stream
      event: speech.audio.delta   data: {"audio_data": "<base64>"}
      event: speech.audio.done    data: {"usage": {...}}

⚠️ The streamed ``pcm`` payload is **float32 little-endian**, mono, 24 kHz —
*not* int16, unlike the ``wav`` output of the same endpoint (which is int16 at
the same rate). The contract wants int16, hence the conversion below. Reading
those bytes as int16 yields audio twice as long and unintelligible, which is
exactly the kind of bug that "sounds like a codec problem" for an afternoon.
"""

from __future__ import annotations

import array
import base64
import json
import logging
import sys
import urllib.request
from typing import Dict, Iterator, Optional, Tuple

logger = logging.getLogger(__name__)

SPEECH_URL = "https://api.mistral.ai/v1/audio/speech"
DEFAULT_MODEL = "voxtral-mini-tts-2603"
#: Voix par defaut : ``fr_marie_neutral``.
#:
#: Le defaut precedent (``en_paul_neutral``) etait une voix ANGLAISE. Le modele
#: prononce bien le francais avec, mais avec l'accent de la voix — signale en
#: usage comme « il a un accent quebecois ». Pulse Chat est un produit
#: francais-d'abord (l'app est en FR par defaut) : son defaut doit l'etre aussi.
#:
#: Le catalogue Voxtral compte 6 voix ``fr_fr`` (toutes « Marie », feminines) ;
#: ``voice_id`` dans la configuration du bot reste prioritaire sur ce defaut.
DEFAULT_VOICE_ID = "5a271406-039d-46fe-835b-fbbb00eaf08d"  # fr_marie_neutral
#: Mirrors ``_STREAM_SENTENCE_BYTE_CAP``: one sentence of PCM never approaches
#: 16 MiB, so exceeding it means a runaway upstream — stop pulling.
SENTENCE_BYTE_CAP = 16 * 1024 * 1024
HTTP_TIMEOUT = 30.0
SAMPLE_RATE = 24000


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without hermes installed)
# ---------------------------------------------------------------------------

def parse_sse_audio_line(line: bytes) -> Optional[bytes]:
    """Return the raw audio payload of one SSE ``data:`` line, else ``None``.

    Everything that is not an audio delta — the ``event:`` lines, the blank
    separators, ``speech.audio.done`` and its usage block — is skipped rather
    than treated as an error: a stream that grows a new event type must not
    break playback.
    """
    if not line.startswith(b"data:"):
        return None
    raw = line[5:].strip()
    if not raw or raw == b"[DONE]":
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        logger.debug("Voxtral streaming: undecodable SSE line, skipped")
        return None
    if payload.get("type") not in (None, "speech.audio.delta"):
        return None
    encoded = payload.get("audio_data")
    if not encoded:
        return None
    try:
        # ``validate=True``: without it base64 silently DROPS characters outside
        # its alphabet, so a corrupted payload decodes to a shorter buffer and
        # slips into the audio as a glitch instead of being skipped.
        return base64.b64decode(encoded, validate=True)
    except Exception:
        logger.debug("Voxtral streaming: undecodable base64 payload, skipped")
        return None


def pcm_float32_to_int16(data: bytes) -> Tuple[bytes, bytes]:
    """Convert little-endian float32 PCM to int16. Returns (pcm, remainder).

    ``remainder`` holds the trailing bytes of a sample split across two SSE
    deltas: converting them as-is would inject a click into the audio, and
    dropping them would drift the stream. The caller feeds them back in.

    Samples are clamped rather than wrapped — a value slightly above 1.0 must
    saturate, not become a loud negative spike.
    """
    usable = len(data) - (len(data) % 4)
    if usable <= 0:
        return b"", data

    floats = array.array("f")
    floats.frombytes(data[:usable])
    if sys.byteorder != "little":
        floats.byteswap()

    out = array.array("h", bytes(2 * len(floats)))
    for index, value in enumerate(floats):
        scaled = int(value * 32767.0)
        out[index] = 32767 if scaled > 32767 else (-32768 if scaled < -32768 else scaled)
    if sys.byteorder != "little":
        out.byteswap()

    return out.tobytes(), data[usable:]


def build_payload(text: str, section: Dict) -> Dict:
    """Body of the streaming speech request. PURE."""
    return {
        "model": section.get("model") or DEFAULT_MODEL,
        "input": text,
        "voice_id": section.get("voice_id") or DEFAULT_VOICE_ID,
        "response_format": "pcm",
        "stream": True,
    }


def _api_key() -> str:
    """Mistral secret, through the house lookup when it is available.

    ``tools.tts_streaming._resolve_key`` is the documented path for streaming
    providers (config > env/.env > credential pool); the bare env read is only
    the fallback for an older gateway that lacks it.
    """
    try:
        from tools.tts_streaming import _resolve_key

        return _resolve_key("MISTRAL_API_KEY", "mistral") or ""
    except Exception:
        try:
            from tools.tts_tool import get_env_value

            return get_env_value("MISTRAL_API_KEY") or ""
        except Exception:
            import os

            return os.environ.get("MISTRAL_API_KEY", "")


def iter_sse_lines(reader, read_size: int = 65536) -> Iterator[bytes]:
    """Split a byte reader into lines WITHOUT ``readline()``.

    Not a detail: ``http.client.HTTPResponse`` inherits ``readline`` from
    ``IOBase``, which walks the buffer a byte at a time. An SSE audio delta is
    ~50 KB on one line, so ``for line in response`` spends seconds in that loop
    while the socket has long since delivered everything — measured 5.1 s to
    first audio where the API had answered in 0.47 s. Reading in blocks and
    splitting here costs nothing and gives back those five seconds.
    """
    buffer = b""
    while True:
        block = reader.read(read_size)
        if not block:
            break
        buffer += block
        while True:
            index = buffer.find(b"\n")
            if index < 0:
                break
            line, buffer = buffer[:index], buffer[index + 1:]
            yield line.rstrip(b"\r")
    if buffer:
        yield buffer.rstrip(b"\r")


def iter_pcm_chunks(response, cap: int = SENTENCE_BYTE_CAP) -> Iterator[bytes]:
    """Yield int16 PCM from an iterable of SSE lines. Separated for testability."""
    remainder = b""
    total = 0
    for line in response:
        payload = parse_sse_audio_line(line)
        if payload is None:
            continue
        chunk, remainder = pcm_float32_to_int16(remainder + payload)
        if not chunk:
            continue
        total += len(chunk)
        if total > cap:
            logger.warning(
                "Voxtral streaming TTS exceeded %d bytes for one sentence; truncating",
                cap,
            )
            return
        yield chunk


# ---------------------------------------------------------------------------
# Registration (no-op when the gateway predates the streaming contract)
# ---------------------------------------------------------------------------

def install() -> bool:
    """Register the streamer. Returns False on a gateway without the contract.

    Idempotent: registering twice simply overwrites the same registry entry.
    """
    try:
        from tools.tts_streaming import StreamingTTSProvider, register
    except Exception:
        # Hermes < v0.20 (no streaming contract), or the plugin is loaded
        # outside a gateway (tests of the pure helpers). Whole-file TTS keeps
        # working; only the per-sentence streaming is absent.
        return False

    @register("mistral")
    class VoxtralStreamer(StreamingTTSProvider):  # noqa: D401 - upstream shape
        """Mistral Voxtral TTS, chunked: SSE deltas -> int16 PCM, 24 kHz mono."""

        sample_rate = SAMPLE_RATE

        @staticmethod
        def available() -> bool:
            return bool(_api_key())

        def stream(self, text: str) -> Iterator[bytes]:
            request = urllib.request.Request(
                SPEECH_URL,
                data=json.dumps(build_payload(text, self.section)).encode("utf-8"),
                headers={
                    "Authorization": "Bearer %s" % _api_key(),
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                method="POST",
            )
            # Failures RAISE, per the provider contract: the consumer logs them
            # and falls back to whole-file TTS when nothing was audible yet.
            #
            # They are ALSO logged here, because the consumer's own log is not
            # always reachable: a turn that opens an audio track and produces
            # nothing looks, from the app, exactly like a turn that succeeded
            # and had nothing to say. One line here names the difference.
            emitted = 0
            try:
                with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                    for chunk in iter_pcm_chunks(iter_sse_lines(response)):
                        emitted += 1
                        yield chunk
            except Exception as exc:
                logger.warning(
                    "Voxtral streaming: synthese en echec apres %s morceau(x) — %s",
                    emitted,
                    exc,
                )
                raise
            if emitted == 0:
                logger.warning(
                    "Voxtral streaming: aucune donnee audio pour %s caracteres de texte",
                    len(text),
                )

    return True
