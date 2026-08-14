# -*- coding: utf-8 -*-
"""Notes vocales — partie PURE (types audio, URL, en-tetes).

Deux sens, deux chemins, et ils ne se ressemblent pas :

- ENTRANT (humain -> agent) : l'app pousse l'audio dans ``mediaUrls`` et marque
  la frame ``messageType: "voice"``. Le plugin materialise le fichier puis
  transmet un ``MessageEvent`` de type ``VOICE`` — c'est le coeur du gateway
  Hermes qui transcrit (``_enrich_message_with_transcription``), pas nous.
  La condition exacte cote Hermes est « MIME ``audio/`` OU message_type
  VOICE/AUDIO » : on remplit les DEUX, un seul suffirait mais l'audio arriverait
  alors muet si l'app cessait un jour d'annoncer le type.

- SORTANT (agent -> humain) : ``POST /api/agent/voice/<slug>``, le corps EST
  l'audio brut. Ni JSON ni multipart — meme raison que le depot de piece jointe
  cote humain (``POST /api/channels/:slug/attachments``) : un base64 ou un
  ``readMultipartFormData`` materialisent le fichier en memoire.

Pourquoi PAS le coffre-fort pour l'audio sortant, alors que les artifacts y
vivent : le coffre est un espace de TRAVAIL, borne par un quota de fichiers
(``VAULT_MAX_FILES_PER_CHANNEL``, 500 par defaut). Une note vocale par reponse
le remplirait en quelques semaines de conversation et ferait echouer, un jour,
l'ecriture d'un artifact — pour des fichiers dont personne ne veut la liste. Une
note vocale est un MESSAGE, pas un document de travail : elle suit donc le
chemin des pieces jointes (S3, presign 15 min, audit).
"""

import mimetypes
import uuid
from typing import Any, Optional
from urllib.parse import quote

#: Plafond d'une note vocale sortante. La limite qui FAIT foi est celle du
#: serveur ; celle-ci evite d'envoyer 100 Mo pour se faire refuser au bout.
MAX_VOICE_BYTES = 25 * 1024 * 1024

#: Extension par type, quand ``mimetypes`` ne sait pas repondre. Les formats que
#: produisent les fournisseurs TTS d'Hermes (mp3 par defaut, opus natif chez
#: Mistral/OpenAI/ElevenLabs/Gemini, wav chez les locaux).
_AUDIO_EXTENSIONS = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
}

#: Type par extension — chemin inverse, pour un fichier sans MIME annonce.
_AUDIO_MIMES = {
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
}


def is_audio_mime(value: Any) -> bool:
    """Vrai pour un type audio annonce. Insensible aux parametres (``; codecs=``)."""
    if not isinstance(value, str):
        return False
    return value.split(";", 1)[0].strip().lower().startswith("audio/")


def guess_audio_mime(path: str) -> str:
    """Type d'un fichier audio local, deduit de son extension.

    Repli sur ``audio/mpeg`` : le defaut d'Hermes est le mp3, et un type
    generique (``application/octet-stream``) ferait tomber la bulle audio du
    navigateur en simple lien de telechargement.
    """
    lowered = path.lower()
    for extension, mime in _AUDIO_MIMES.items():
        if lowered.endswith(extension):
            return mime
    guessed, _ = mimetypes.guess_type(path)
    if is_audio_mime(guessed):
        return str(guessed)
    return "audio/mpeg"


def audio_extension(mime: str) -> str:
    """Extension de fichier conseillee pour un type audio."""
    normalized = (mime or "").split(";", 1)[0].strip().lower()
    if normalized in _AUDIO_EXTENSIONS:
        return _AUDIO_EXTENSIONS[normalized]
    guessed = mimetypes.guess_extension(normalized or "")
    return guessed or ".mp3"


def voice_filename(mime: str) -> str:
    """Nom de fichier d'une note vocale sortante.

    Le nom est ce que l'humain verra si son navigateur telecharge le fichier au
    lieu de le jouer : il doit rester lisible, et ne porte donc AUCUN horodatage
    technique. L'unicite est assuree par la cle S3 cote serveur, pas par ce nom.
    """
    return "note-vocale-%s%s" % (uuid.uuid4().hex[:8], audio_extension(mime))


def voice_url(base_url: str, channel_slug: str) -> str:
    """URL de depot d'une note vocale de l'agent."""
    return "%s/api/agent/voice/%s" % (
        base_url.rstrip("/"),
        quote(channel_slug, safe=""),
    )


def caption_header(caption: Optional[str]) -> Optional[str]:
    """Legende encodee pour l'en-tete ``x-caption``, ou ``None`` si vide.

    Un en-tete HTTP ne transporte pas d'accents en clair : la legende est
    percent-encodee (le serveur applique ``decodeURIComponent`` en miroir, meme
    convention que ``x-filename`` sur le depot humain).
    """
    if not isinstance(caption, str):
        return None
    cleaned = " ".join(caption.split())
    if not cleaned:
        return None
    return quote(cleaned[:2000], safe="")
