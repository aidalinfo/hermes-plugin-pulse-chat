# -*- coding: utf-8 -*-
"""Contrat de streaming audio (#60671) — trames pures + cablage adaptateur.

Ce que ces tests protegent, dans l'ordre d'importance :

1. **Un plugin deploye avant l'app ne change rien au produit.** Tant que l'app
   n'annonce pas sa capacite audio, `supports_streaming_tts` repond False et
   Hermes garde son repli (audio complet en fin de tour). Sans cet invariant, il
   faudrait synchroniser deux deploiements a la minute pres.
2. **Un morceau en retard apres une interruption est jete, jamais joue.**
   L'idempotence de `abort_streaming_tts` est exigee par le contrat : c'est elle
   qui evite d'entendre la fin du tour precedent par-dessus le suivant.
3. **Le format binaire a une seule definition**, testee ici, et que l'app
   reimplemente en TypeScript d'apres `decode_audio_frame`.
"""

import asyncio
import importlib.util
import json
import sys
import types

import pytest

from test_adapter_dedup import _load_adapter_module

adapter_module = _load_adapter_module()


def _load_pure(module_name):
    from pathlib import Path

    plugin_dir = Path(__file__).resolve().parents[1]
    pkg_name = "pulse_chat_pure_under_test"
    if pkg_name not in sys.modules:
        package = types.ModuleType(pkg_name)
        package.__path__ = [str(plugin_dir)]
        sys.modules[pkg_name] = package
    full = "%s.%s" % (pkg_name, module_name)
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, plugin_dir / ("%s.py" % module_name))
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


audio_stream = _load_pure("audio_stream")


class TestCapaciteAnnoncee:
    def test_une_app_muette_n_ouvre_rien(self):
        assert audio_stream.audio_capability_from_ack({}) is None
        assert audio_stream.audio_capability_from_ack({"audio": {}}) is None
        assert audio_stream.audio_capability_from_ack({"audio": {"streaming": False}}) is None
        # On ne DEVINE jamais que l'app sait jouer du PCM.
        assert audio_stream.audio_capability_from_ack({"audio": "oui"}) is None

    def test_annonce_valide(self):
        got = audio_stream.audio_capability_from_ack(
            {"audio": {"streaming": True, "sampleRate": 24000}}
        )
        assert got == {"streaming": True, "sampleRate": 24000}

    def test_frequence_aberrante_refusee(self):
        assert (
            audio_stream.audio_capability_from_ack({"audio": {"streaming": True, "sampleRate": 0}})
            is None
        )

    def test_frequence_annoncee_engage_l_app(self):
        capability = {"streaming": True, "sampleRate": 24000}
        assert audio_stream.capability_accepts(capability, 24000) is True
        # Envoyer du 16 kHz a un lecteur cable en 24 kHz donne une voix
        # ralentie : un defaut qu'on entend mais qu'on ne mesure pas.
        assert audio_stream.capability_accepts(capability, 16000) is False

    def test_sans_frequence_annoncee_on_accepte_ce_que_demande_hermes(self):
        assert audio_stream.capability_accepts({"streaming": True, "sampleRate": None}, 48000)

    def test_pas_de_capacite_pas_de_flux(self):
        assert audio_stream.capability_accepts(None, 24000) is False


class TestTrames:
    def test_aller_retour_binaire(self):
        frame = audio_stream.encode_audio_frame("demo", "s1", 7, b"\x01\x02\x03\x04")
        header, pcm = audio_stream.decode_audio_frame(frame)
        assert header == {"chatId": "demo", "streamId": "s1", "seq": 7}
        assert pcm == b"\x01\x02\x03\x04"

    def test_le_pcm_n_est_pas_recopie_dans_l_en_tete(self):
        # L'en-tete reste minuscule quelle que soit la taille du morceau : c'est
        # ce qui rend le format utilisable a 48 Ko/s.
        frame = audio_stream.encode_audio_frame("demo", "s1", 0, b"x" * 50_000)
        header_len = int.from_bytes(frame[2:4], "little")
        assert header_len < 100
        assert len(frame) == 4 + header_len + 50_000

    def test_trame_illisible_refusee_explicitement(self):
        with pytest.raises(ValueError):
            audio_stream.decode_audio_frame(b"\x01")
        with pytest.raises(ValueError):
            audio_stream.decode_audio_frame(b"\x09\x01\x00\x00")  # version inconnue

    def test_bornes_json_lisibles(self):
        begin = audio_stream.begin_frame("demo", "s1", None)
        assert begin["type"] == "audio.begin"
        assert begin["format"]["sampleRate"] == audio_stream.DEFAULT_SAMPLE_RATE
        assert audio_stream.end_frame("demo", "s1", True)["interrupted"] is True
        assert audio_stream.abort_frame("demo", "s1", "boum")["error"] == "boum"


class _Config:
    extra = {"url": "http://pulse-chat.test", "token": "token-test"}


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, frame):
        self.sent.append(frame)

    def json_frames(self):
        return [json.loads(f) for f in self.sent if isinstance(f, str)]

    def binary_frames(self):
        return [f for f in self.sent if isinstance(f, (bytes, bytearray))]


class _Format:
    sample_rate = 24000
    channels = 1
    sample_width = 2


def _adapter_with_audio(sample_rate=24000):
    adapter = adapter_module.PulseChatAdapter(_Config())
    ws = _FakeWS()
    adapter._ws = ws
    adapter._handle_hello_ack(
        {"sessionToken": "tok", "audio": {"streaming": True, "sampleRate": sample_rate}}
    )
    return adapter, ws


class TestNegociation:
    def test_sans_annonce_de_l_app_le_flux_reste_ferme(self):
        # Cas du plugin deploye AVANT l'app : rien ne change pour le produit.
        adapter = adapter_module.PulseChatAdapter(_Config())
        adapter._ws = _FakeWS()
        adapter._handle_hello_ack({"sessionToken": "tok"})
        assert adapter.supports_streaming_tts("demo", _Format()) is False
        assert asyncio.run(adapter.begin_streaming_tts("demo", _Format())) is None

    def test_avec_annonce_le_flux_s_ouvre(self):
        adapter, ws = _adapter_with_audio()
        assert adapter.supports_streaming_tts("demo", _Format()) is True
        handle = asyncio.run(adapter.begin_streaming_tts("demo", _Format()))
        assert handle is not None
        begin = ws.json_frames()[0]
        assert begin["type"] == "audio.begin"
        assert begin["format"] == {"sampleRate": 24000, "channels": 1, "sampleWidth": 2}

    def test_frequence_incompatible_refusee(self):
        adapter, _ = _adapter_with_audio(sample_rate=16000)
        assert adapter.supports_streaming_tts("demo", _Format()) is False

    def test_sans_websocket_aucun_flux(self):
        adapter, _ = _adapter_with_audio()
        adapter._ws = None
        assert adapter.supports_streaming_tts("demo", _Format()) is False

    def test_un_nouveau_hello_oublie_la_capacite(self):
        # Une app redeployee sans le support audio ne doit pas continuer de
        # recevoir du PCM sur la foi de l'ancienne session.
        adapter, _ = _adapter_with_audio()
        adapter._forget_session_token()
        assert adapter.supports_streaming_tts("demo", _Format()) is False


class TestEcriture:
    def test_numerote_les_morceaux_dans_l_ordre(self):
        adapter, ws = _adapter_with_audio()

        async def run():
            handle = await adapter.begin_streaming_tts("demo", _Format())
            for chunk in (b"aa", b"bb", b"cc"):
                await adapter.write_streaming_tts(handle, chunk)
            await adapter.finish_streaming_tts(handle)

        asyncio.run(run())
        seqs = [audio_stream.decode_audio_frame(f)[0]["seq"] for f in ws.binary_frames()]
        assert seqs == [0, 1, 2]
        assert [audio_stream.decode_audio_frame(f)[1] for f in ws.binary_frames()] == [
            b"aa",
            b"bb",
            b"cc",
        ]
        assert ws.json_frames()[-1]["type"] == "audio.end"

    def test_un_morceau_apres_abandon_est_jete(self):
        adapter, ws = _adapter_with_audio()

        async def run():
            handle = await adapter.begin_streaming_tts("demo", _Format())
            await adapter.write_streaming_tts(handle, b"aa")
            await adapter.abort_streaming_tts(handle, "coupe")
            # Morceau en retard : jete en silence, JAMAIS joue par-dessus le
            # tour suivant, et surtout pas une exception.
            await adapter.write_streaming_tts(handle, b"bb")
            return handle

        handle = asyncio.run(run())
        assert handle.aborted is True
        assert len(ws.binary_frames()) == 1
        assert ws.json_frames()[-1]["type"] == "audio.abort"

    def test_abandon_idempotent(self):
        adapter, ws = _adapter_with_audio()

        async def run():
            handle = await adapter.begin_streaming_tts("demo", _Format())
            await adapter.abort_streaming_tts(handle)
            await adapter.abort_streaming_tts(handle)
            await adapter.abort_streaming_tts(handle)

        asyncio.run(run())
        aborts = [f for f in ws.json_frames() if f["type"] == "audio.abort"]
        assert len(aborts) == 1

    def test_websocket_ferme_en_plein_flux_leve(self):
        # Le contrat veut une exception : le consumer la journalise et retombe
        # sur l'audio complet tant que rien n'a ete audible.
        adapter, _ = _adapter_with_audio()

        async def run():
            handle = await adapter.begin_streaming_tts("demo", _Format())
            adapter._ws = None
            with pytest.raises(Exception):
                await adapter.write_streaming_tts(handle, b"aa")

        asyncio.run(run())

    def test_fin_de_flux_ne_leve_jamais(self):
        # Le tour a deja ete dit : une borne de fin perdue ne doit pas remonter.
        adapter, _ = _adapter_with_audio()

        async def run():
            handle = await adapter.begin_streaming_tts("demo", _Format())
            adapter._ws = None
            await adapter.finish_streaming_tts(handle)
            await adapter.abort_streaming_tts(handle)

        asyncio.run(run())

    def test_morceau_vide_ignore(self):
        adapter, ws = _adapter_with_audio()

        async def run():
            handle = await adapter.begin_streaming_tts("demo", _Format())
            await adapter.write_streaming_tts(handle, b"")

        asyncio.run(run())
        assert ws.binary_frames() == []


class TestFluxOrphelins:
    def test_la_deconnexion_coupe_les_flux_ouverts(self):
        # Sans ca, un `write` tardif reprendrait sur une nouvelle connexion avec
        # un streamId que l'app ne connait plus : du son sorti de nulle part.
        adapter, _ = _adapter_with_audio()

        async def run():
            handle = await adapter.begin_streaming_tts("demo", _Format())
            adapter._abort_open_audio_streams()
            return handle

        handle = asyncio.run(run())
        assert handle.aborted is True
        assert adapter._audio_streams == {}

    def test_les_flux_ouverts_sont_bornes(self):
        adapter, _ = _adapter_with_audio()

        async def run():
            handles = []
            for _ in range(adapter_module._AUDIO_STREAMS_MAX + 3):
                handles.append(await adapter.begin_streaming_tts("demo", _Format()))
            return handles

        handles = asyncio.run(run())
        assert len(adapter._audio_streams) <= adapter_module._AUDIO_STREAMS_MAX
        # Les plus anciens sont coupes, pas oublies en silence.
        assert handles[0].aborted is True
