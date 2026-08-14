# -*- coding: utf-8 -*-
"""Notes vocales : module pur ``voice.py`` + cablage cote adaptateur.

Deux invariants tiennent tout le reste :

1. Un message entrant porteur d'audio est de type VOICE. C'est ce type qui
   declenche, cote Hermes, la transcription automatique ET l'auto-TTS de la
   reponse — un message d'audio type TEXT donne un agent sourd et muet, sans
   aucune erreur visible.
2. Un echec d'envoi audio ne fait JAMAIS tomber le tour de parole, et ne laisse
   JAMAIS fuiter un chemin de conteneur dans le fil du client (c'est ce que
   ferait le repli de ``BasePlatformAdapter``).
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

from test_adapter_dedup import _load_adapter_module

_PLUGIN_DIR = Path(__file__).resolve().parents[1]

adapter_module = _load_adapter_module()


def _load_pure(module_name):
    pkg_name = "pulse_chat_pure_under_test"
    if pkg_name not in sys.modules:
        package = types.ModuleType(pkg_name)
        package.__path__ = [str(_PLUGIN_DIR)]
        sys.modules[pkg_name] = package
    full = "%s.%s" % (pkg_name, module_name)
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _PLUGIN_DIR / ("%s.py" % module_name))
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


voice = _load_pure("voice")


class TestTypesAudio:
    def test_mime_audio_reconnu_avec_parametres(self):
        # Le navigateur annonce `audio/webm;codecs=opus` : le parametre ne doit
        # pas empecher la reconnaissance, sinon la note vocale du client Safari
        # ou Chrome arrive comme un document quelconque.
        assert voice.is_audio_mime("audio/webm;codecs=opus")
        assert voice.is_audio_mime("AUDIO/MPEG")
        assert voice.is_audio_mime("audio/ogg")

    def test_non_audio_refuse(self):
        assert not voice.is_audio_mime("image/png")
        assert not voice.is_audio_mime("application/octet-stream")
        assert not voice.is_audio_mime(None)
        assert not voice.is_audio_mime(42)

    def test_type_devine_depuis_l_extension(self):
        assert voice.guess_audio_mime("/tmp/reponse.mp3") == "audio/mpeg"
        assert voice.guess_audio_mime("/tmp/reponse.ogg") == "audio/ogg"
        assert voice.guess_audio_mime("/tmp/reponse.opus") == "audio/opus"
        assert voice.guess_audio_mime("/tmp/reponse.wav") == "audio/wav"

    def test_repli_mp3_plutot_qu_octet_stream(self):
        # Un type generique ferait tomber la bulle audio du navigateur en simple
        # lien de telechargement : le defaut d'Hermes est le mp3, on l'assume.
        assert voice.guess_audio_mime("/tmp/audio_cache/1234") == "audio/mpeg"

    def test_extension_par_type(self):
        assert voice.audio_extension("audio/mpeg") == ".mp3"
        assert voice.audio_extension("audio/ogg") == ".ogg"
        assert voice.audio_extension("audio/mp4") == ".m4a"
        assert voice.audio_extension("audio/inconnu-xyz") == ".mp3"

    def test_nom_de_fichier_lisible_et_unique(self):
        first = voice.voice_filename("audio/mpeg")
        second = voice.voice_filename("audio/mpeg")
        assert first.startswith("note-vocale-") and first.endswith(".mp3")
        assert first != second


class TestUrlEtEnTetes:
    def test_url_encode_le_slug(self):
        assert (
            voice.voice_url("http://app.test/", "canal/etrange")
            == "http://app.test/api/agent/voice/canal%2Fetrange"
        )

    def test_legende_percent_encodee(self):
        # Un en-tete HTTP ne transporte pas d'accents en clair.
        assert voice.caption_header("Résumé du jour") == "R%C3%A9sum%C3%A9%20du%20jour"

    def test_legende_vide_absente(self):
        assert voice.caption_header("   ") is None
        assert voice.caption_header(None) is None

    def test_legende_bornee(self):
        assert len(voice.caption_header("a" * 5000) or "") == 2000


class _Config:
    extra = {"url": "http://pulse-chat.test", "token": "token-test"}


def _make_adapter(status=201, body=b'{"id":"msg-42"}'):
    adapter = adapter_module.PulseChatAdapter(_Config())
    calls = []

    def fake_post_bytes(url, data, headers):
        calls.append({"url": url, "data": data, "headers": headers})
        return status, body

    adapter._post_bytes = fake_post_bytes
    adapter.calls = calls
    return adapter


class TestEnvoiNoteVocale:
    def test_poste_l_audio_avec_type_et_legende(self, tmp_path):
        audio = tmp_path / "reponse.mp3"
        audio.write_bytes(b"ID3fake-audio")
        adapter = _make_adapter()

        result = asyncio.run(
            adapter.send_voice(
                chat_id="demo", audio_path=str(audio), caption="Voilà le résumé"
            )
        )

        assert result.success is True
        # L'id rendu est celui du MESSAGE cote app : c'est la cible d'une
        # reponse ulterieure.
        assert result.message_id == "msg-42"
        call = adapter.calls[0]
        assert call["url"] == "http://pulse-chat.test/api/agent/voice/demo"
        assert call["data"] == b"ID3fake-audio"
        assert call["headers"]["Content-Type"] == "audio/mpeg"
        assert call["headers"]["x-caption"] == "Voil%C3%A0%20le%20r%C3%A9sum%C3%A9"
        assert call["headers"]["x-filename"].startswith("note-vocale-")

    def test_sans_legende_pas_d_en_tete(self, tmp_path):
        audio = tmp_path / "reponse.ogg"
        audio.write_bytes(b"OggS-fake")
        adapter = _make_adapter()

        asyncio.run(adapter.send_voice(chat_id="demo", audio_path=str(audio)))

        headers = adapter.calls[0]["headers"]
        assert "x-caption" not in headers
        assert headers["Content-Type"] == "audio/ogg"

    def test_fichier_absent_echoue_sans_lever_et_sans_fuite_de_chemin(self):
        adapter = _make_adapter()
        posted_text = []
        adapter.send = lambda **kwargs: posted_text.append(kwargs)

        result = asyncio.run(
            adapter.send_voice(chat_id="demo", audio_path="/tmp/nexiste-pas.mp3")
        )

        assert result.success is False
        assert result.error_kind == "permanent"
        # Le repli de la classe de base posterait « 🔊 Audio: /chemin » : un
        # chemin de conteneur dans le fil d'un client. Jamais.
        assert posted_text == []
        assert adapter.calls == []

    def test_fichier_vide_refuse_avant_l_appel_reseau(self, tmp_path):
        audio = tmp_path / "vide.mp3"
        audio.write_bytes(b"")
        adapter = _make_adapter()

        result = asyncio.run(adapter.send_voice(chat_id="demo", audio_path=str(audio)))

        assert result.success is False
        assert adapter.calls == []

    def test_audio_trop_volumineux_refuse_localement(self, tmp_path):
        audio = tmp_path / "gros.mp3"
        audio.write_bytes(b"x" * (voice.MAX_VOICE_BYTES + 10))
        adapter = _make_adapter()

        result = asyncio.run(adapter.send_voice(chat_id="demo", audio_path=str(audio)))

        assert result.success is False
        assert adapter.calls == []

    def test_erreur_serveur_rejouable(self, tmp_path):
        audio = tmp_path / "reponse.mp3"
        audio.write_bytes(b"ID3")
        adapter = _make_adapter(status=503, body=b"")

        result = asyncio.run(adapter.send_voice(chat_id="demo", audio_path=str(audio)))

        assert result.success is False
        assert result.retryable is True

    def test_play_tts_passe_par_send_voice(self, tmp_path):
        # L'auto-TTS d'Hermes appelle `play_tts` ; le repli de la classe de base
        # delegue a `send_voice`. Si cette chaine casse, l'agent redevient muet.
        audio = tmp_path / "reponse.mp3"
        audio.write_bytes(b"ID3")
        adapter = _make_adapter()
        seen = []

        async def spy(chat_id, audio_path, **kwargs):
            seen.append((chat_id, audio_path))
            return adapter_module.SendResult(success=True, message_id="msg-1")

        adapter.send_voice = spy
        result = asyncio.run(adapter.play_tts(chat_id="demo", audio_path=str(audio)))

        assert result.success is True
        assert seen == [("demo", str(audio))]


def _fake_download(paths, types):
    async def download(urls):
        return list(paths), list(types)

    return download


def _handle(adapter, message, channel=None):
    """Joue un ``message.created`` et rend l'evenement transmis a Hermes."""
    captured = []

    async def capture(event):
        captured.append(event)

    adapter.handle_message = capture
    adapter._send_ack = lambda message_id: asyncio.sleep(0)
    asyncio.run(
        adapter._handle_message_created(
            {
                "channel": channel or {"slug": "demo", "name": "Démo"},
                "message": message,
            }
        )
    )
    return captured[0] if captured else None


class TestNoteVocaleEntrante:
    def test_audio_annonce_par_l_app_donne_un_evenement_VOICE(self):
        adapter = _make_adapter()
        adapter._download_media = _fake_download([], [])

        event = _handle(
            adapter,
            {"id": "m1", "text": "", "messageType": "voice", "mediaUrls": []},
        )

        assert event.message_type == adapter_module.MessageType.VOICE

    def test_audio_deduit_du_mime_telecharge(self):
        # Repli : meme si l'app cessait d'annoncer le type, un media audio
        # materialise suffit a typer l'evenement en VOICE.
        adapter = _make_adapter()
        adapter._download_media = _fake_download(["/tmp/note.ogg"], ["audio/ogg"])

        event = _handle(
            adapter, {"id": "m2", "text": "", "mediaUrls": ["http://app/x"]}
        )

        assert event.message_type == adapter_module.MessageType.VOICE
        assert event.media_urls == ["/tmp/note.ogg"]
        assert event.media_types == ["audio/ogg"]

    def test_message_texte_reste_TEXT(self):
        adapter = _make_adapter()
        adapter._download_media = _fake_download([], [])

        event = _handle(adapter, {"id": "m3", "text": "bonjour", "mediaUrls": []})

        assert event.message_type == adapter_module.MessageType.TEXT

    def test_image_seule_reste_TEXT(self):
        # Une image ne doit PAS declencher l'auto-TTS : la reponse a une capture
        # d'ecran se lit, elle ne s'ecoute pas.
        adapter = _make_adapter()
        adapter._download_media = _fake_download(["/tmp/capture.png"], ["image/png"])

        event = _handle(
            adapter, {"id": "m4", "text": "regarde", "mediaUrls": ["http://app/i"]}
        )

        assert event.message_type == adapter_module.MessageType.TEXT
