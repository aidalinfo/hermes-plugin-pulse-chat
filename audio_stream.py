# -*- coding: utf-8 -*-
"""Transport de l'audio en flux vers l'app — partie PURE (trames, capacite).

Hermes sait parler pendant qu'il redige : son ``StreamingTTSConsumer`` decoupe
la reponse en phrases, les synthetise au fil de l'eau et pousse du PCM a
l'adaptateur via cinq methodes (``supports/begin/write/finish/abort
_streaming_tts``). Ce module porte ce qui traverse le fil ; l'adaptateur ne fait
que brancher les deux bouts.

## Negociation — pourquoi l'app decide, et pas nous

``supports_streaming_tts`` ne repond ``True`` que si l'app l'a **annonce** dans
sa trame ``hello.ack`` :

    {"type": "hello.ack", "sessionToken": "...",
     "audio": {"streaming": true, "sampleRate": 24000}}

Sans ce bloc — donc face a toute app anterieure a cette fonctionnalite — la
reponse est ``False`` et Hermes garde son comportement actuel : audio complet
en fin de tour. Le plugin peut donc etre deploye AVANT l'app sans rien changer
au produit, ce qui est la seule facon de ne pas avoir a synchroniser deux
deploiements.

## Trames

Les bornes du flux sont du JSON (lisibles dans les logs, faciles a router) ;
seul le PCM voyage en binaire :

    {"type": "audio.begin", "chatId", "streamId",
     "format": {"sampleRate", "channels", "sampleWidth"}}
    <trame binaire>  x N
    {"type": "audio.end", "chatId", "streamId", "interrupted": bool}
    {"type": "audio.abort", "chatId", "streamId", "error": str|null}

Trame binaire :

    octet 0        version (1)
    octet 1        type (1 = morceau audio)
    octets 2-3     longueur de l'en-tete JSON (uint16, petit-boutiste)
    en-tete JSON   {"chatId": str, "streamId": str, "seq": int}
    reste          PCM int16 petit-boutiste, mono, au format annonce

Pourquoi un en-tete par morceau plutot qu'un canal par flux : un morceau en
retard apres une interruption doit pouvoir etre **jete** cote app. Le ``seq``
rend aussi visible une perte d'ordre, qui s'entendrait autrement comme un
hoquet inexplicable.
"""

import json
from typing import Any, Dict, Optional, Tuple

#: Version du format binaire — a incrementer si la disposition change.
FRAME_VERSION = 1
#: Seul type binaire pour l'instant. Les bornes restent en JSON.
FRAME_AUDIO_CHUNK = 1

#: Format par defaut du contrat Hermes (``AudioFormat``).
DEFAULT_SAMPLE_RATE = 24000
DEFAULT_CHANNELS = 1
DEFAULT_SAMPLE_WIDTH = 2

_MAX_HEADER_BYTES = 0xFFFF


def audio_capability_from_ack(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Capacite audio annoncee par l'app, ou ``None`` si elle n'en annonce pas.

    Tolerant par construction : une cle absente, un type inattendu ou
    ``streaming: false`` rendent ``None``, ce qui laisse le plugin dans son
    comportement d'avant. On ne devine JAMAIS que l'app sait jouer du PCM.
    """
    block = data.get("audio")
    if not isinstance(block, dict):
        return None
    if block.get("streaming") is not True:
        return None
    rate = block.get("sampleRate")
    if rate is not None and (not isinstance(rate, int) or rate <= 0):
        return None
    return {"streaming": True, "sampleRate": rate}


def capability_accepts(
    capability: Optional[Dict[str, Any]], sample_rate: int
) -> bool:
    """L'app peut-elle jouer un flux a ``sample_rate`` ?

    Une app qui annonce une frequence precise refuse les autres : son lecteur
    est cable dessus, et lui envoyer 16 kHz en croyant faire du 24 kHz donne une
    voix ralentie — un defaut qu'on entend mais qu'on ne mesure pas. Sans
    frequence annoncee, on accepte ce que demande Hermes.
    """
    if not capability:
        return False
    declared = capability.get("sampleRate")
    return declared is None or declared == sample_rate


def begin_frame(chat_id: str, stream_id: str, audio_format: Any) -> Dict[str, Any]:
    """Borne d'ouverture. ``audio_format`` = ``AudioFormat`` d'Hermes (ou None)."""
    return {
        "type": "audio.begin",
        "chatId": chat_id,
        "streamId": stream_id,
        "format": {
            "sampleRate": getattr(audio_format, "sample_rate", DEFAULT_SAMPLE_RATE),
            "channels": getattr(audio_format, "channels", DEFAULT_CHANNELS),
            "sampleWidth": getattr(audio_format, "sample_width", DEFAULT_SAMPLE_WIDTH),
        },
    }


def end_frame(chat_id: str, stream_id: str, interrupted: bool = False) -> Dict[str, Any]:
    """Borne de fin. ``interrupted`` distingue « fini » de « coupe »."""
    return {
        "type": "audio.end",
        "chatId": chat_id,
        "streamId": stream_id,
        "interrupted": bool(interrupted),
    }


def abort_frame(
    chat_id: str, stream_id: str, error: Optional[str] = None
) -> Dict[str, Any]:
    """Borne d'abandon : l'app doit VIDER sa file pour ce ``streamId``."""
    return {
        "type": "audio.abort",
        "chatId": chat_id,
        "streamId": stream_id,
        "error": error,
    }


def encode_audio_frame(chat_id: str, stream_id: str, seq: int, pcm: bytes) -> bytes:
    """Assemble une trame binaire. PURE.

    Leve ``ValueError`` sur un en-tete demesure : mieux vaut echouer ici que
    d'emettre une trame dont la longueur ne tient pas dans son champ.
    """
    header = json.dumps(
        {"chatId": chat_id, "streamId": stream_id, "seq": seq},
        separators=(",", ":"),
    ).encode("utf-8")
    if len(header) > _MAX_HEADER_BYTES:
        raise ValueError("en-tete de trame audio trop long")
    return (
        bytes([FRAME_VERSION, FRAME_AUDIO_CHUNK])
        + len(header).to_bytes(2, "little")
        + header
        + pcm
    )


def decode_audio_frame(frame: bytes) -> Tuple[Dict[str, Any], bytes]:
    """Inverse d'``encode_audio_frame`` — miroir de ce que fera l'app.

    Presente ici pour que le format ait UNE definition executable, testee des
    deux cotes de la meme description plutot que reimplementee de memoire en
    TypeScript.
    """
    if len(frame) < 4:
        raise ValueError("trame audio tronquee")
    version, kind = frame[0], frame[1]
    if version != FRAME_VERSION:
        raise ValueError("version de trame audio inconnue: %d" % version)
    if kind != FRAME_AUDIO_CHUNK:
        raise ValueError("type de trame audio inconnu: %d" % kind)
    header_len = int.from_bytes(frame[2:4], "little")
    if len(frame) < 4 + header_len:
        raise ValueError("en-tete de trame audio tronque")
    header = json.loads(frame[4:4 + header_len].decode("utf-8"))
    return header, frame[4 + header_len:]
