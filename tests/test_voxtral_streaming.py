# -*- coding: utf-8 -*-
"""Streamer TTS Voxtral — parties pures, testees sans hermes ni reseau.

Le piege que ces tests verrouillent : le flux `pcm` de Mistral est du
**float32**, alors que le contrat d'Hermes veut de l'**int16**. Lire ces octets
en int16 donne un audio deux fois plus long et inintelligible — un defaut qui
ressemble a un probleme de codec pendant tout un apres-midi.

Les durees et le format annonces ici ont ete verifies contre l'API le
2026-08-14 : meme phrase, sortie `wav` a 3,600 s (int16 24 kHz) et flux `pcm`
a 3,360 s en float32 24 kHz (6,720 s si on le lisait en int16).
"""

import array
import base64
import importlib.util
import json
import struct
import sys
import types
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load(module_name):
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


voxtral = _load("voxtral_streaming")


def _sse_delta(samples):
    """Une trame SSE `speech.audio.delta` portant des echantillons float32."""
    raw = array.array("f", samples).tobytes()
    body = json.dumps(
        {"type": "speech.audio.delta", "audio_data": base64.b64encode(raw).decode()}
    )
    return b"data: " + body.encode("utf-8")


class TestLectureSSE:
    def test_extrait_l_audio_d_une_trame_delta(self):
        line = _sse_delta([0.5])
        assert voxtral.parse_sse_audio_line(line) == array.array("f", [0.5]).tobytes()

    def test_ignore_ce_qui_n_est_pas_de_l_audio(self):
        # Lignes d'evenement, separateurs, fin de flux et bloc d'usage : rien de
        # tout cela n'est une erreur, le flux peut gagner un type d'evenement.
        assert voxtral.parse_sse_audio_line(b"event: speech.audio.delta") is None
        assert voxtral.parse_sse_audio_line(b"") is None
        assert voxtral.parse_sse_audio_line(b"data: [DONE]") is None
        assert (
            voxtral.parse_sse_audio_line(
                b'data: {"type":"speech.audio.done","usage":{"prompt_tokens":125}}'
            )
            is None
        )

    def test_ligne_illisible_ignoree_plutot_que_fatale(self):
        assert voxtral.parse_sse_audio_line(b"data: {pas du json") is None
        assert (
            voxtral.parse_sse_audio_line(b'data: {"type":"speech.audio.delta","audio_data":"!!"}')
            is None
        )


class TestConversionFloat32:
    def test_convertit_en_int16(self):
        pcm, rest = voxtral.pcm_float32_to_int16(array.array("f", [0.0, 1.0, -1.0]).tobytes())
        assert rest == b""
        assert array.array("h", pcm).tolist() == [0, 32767, -32767]

    def test_sature_au_lieu_de_boucler(self):
        # Une valeur au-dela de 1.0 doit saturer : la faire boucler
        # transformerait un pic un peu fort en claquement negatif.
        pcm, _ = voxtral.pcm_float32_to_int16(array.array("f", [1.5, -1.5]).tobytes())
        assert array.array("h", pcm).tolist() == [32767, -32768]

    def test_garde_l_echantillon_coupe_entre_deux_trames(self):
        # Un echantillon a cheval sur deux deltas : le convertir tel quel
        # injecterait un clic, le jeter ferait deriver le flux.
        whole = array.array("f", [0.25, 0.5]).tobytes()
        first, rest = voxtral.pcm_float32_to_int16(whole[:6])
        assert array.array("h", first).tolist() == [8191]
        assert rest == whole[4:6]
        second, rest2 = voxtral.pcm_float32_to_int16(rest + whole[6:])
        assert array.array("h", second).tolist() == [16383]
        assert rest2 == b""

    def test_moins_d_un_echantillon_ne_produit_rien(self):
        pcm, rest = voxtral.pcm_float32_to_int16(b"\x00\x00")
        assert pcm == b""
        assert rest == b"\x00\x00"


class TestFluxComplet:
    def test_assemble_les_deltas_en_pcm_int16(self):
        response = [
            b"event: speech.audio.delta",
            _sse_delta([0.0, 0.5]),
            b"",
            _sse_delta([-0.5]),
            b"event: speech.audio.done",
            b'data: {"type":"speech.audio.done","usage":{}}',
        ]
        pcm = b"".join(voxtral.iter_pcm_chunks(response))
        assert array.array("h", pcm).tolist() == [0, 16383, -16383]

    def test_le_debit_reel_correspond_a_24_kHz_mono(self):
        # 24 000 echantillons float32 = 1 s d'audio ; en int16 la sortie doit
        # peser exactement 48 000 octets (24 000 x 2).
        pcm = b"".join(voxtral.iter_pcm_chunks([_sse_delta([0.1] * voxtral.SAMPLE_RATE)]))
        assert len(pcm) == voxtral.SAMPLE_RATE * 2

    def test_plafonne_une_phrase_qui_ne_finit_pas(self):
        # Meme invariant que `_STREAM_SENTENCE_BYTE_CAP` en amont : une phrase
        # ne doit jamais approcher 16 Mio, donc au-dela on arrete de tirer.
        delta = _sse_delta([0.1] * 1000)
        infinite = iter(lambda: delta, None)
        pcm = b"".join(voxtral.iter_pcm_chunks(infinite, cap=4000))
        assert len(pcm) <= 4000 + 2000


class TestRequete:
    def test_demande_du_pcm_en_flux(self):
        payload = voxtral.build_payload("Bonjour", {})
        assert payload["response_format"] == "pcm"
        assert payload["stream"] is True
        assert payload["model"] == voxtral.DEFAULT_MODEL
        assert payload["voice_id"] == voxtral.DEFAULT_VOICE_ID
        assert payload["input"] == "Bonjour"

    def test_la_config_du_canal_prime(self):
        payload = voxtral.build_payload(
            "Bonjour", {"model": "voxtral-mini-tts-next", "voice_id": "abc"}
        )
        assert payload["model"] == "voxtral-mini-tts-next"
        assert payload["voice_id"] == "abc"


class TestInstallation:
    def test_sans_le_contrat_de_streaming_l_installation_est_inerte(self):
        # Hermes < v0.20 : pas de `tools.tts_streaming`. Le plugin doit
        # continuer de fonctionner, seule la voix par phrase manque.
        assert voxtral.install() is False

    def test_s_enregistre_quand_le_contrat_existe(self, monkeypatch):
        registry = {}

        module = types.ModuleType("tools.tts_streaming")

        class StreamingTTSProvider:
            sample_rate = 24000
            channels = 1
            sample_width = 2

            def __init__(self, tts_config, section):
                self.tts_config = tts_config
                self.section = section

        def register(name):
            def wrap(cls):
                registry[name] = cls
                return cls

            return wrap

        module.StreamingTTSProvider = StreamingTTSProvider
        module.register = register
        module._resolve_key = lambda env_var, provider_id: "cle-test"
        tools_pkg = sys.modules.get("tools") or types.ModuleType("tools")
        monkeypatch.setitem(sys.modules, "tools", tools_pkg)
        monkeypatch.setitem(sys.modules, "tools.tts_streaming", module)

        assert voxtral.install() is True
        assert "mistral" in registry
        streamer = registry["mistral"]
        assert streamer.sample_rate == 24000
        # `available()` suit la cle, pas la presence du module.
        assert streamer.available() is True
        module._resolve_key = lambda env_var, provider_id: ""
        monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
        assert streamer.available() is False


class _FakeReader:
    """Lecteur par blocs, comme une réponse HTTP — jamais `readline()`."""

    def __init__(self, payload: bytes, block: int = 7):
        self._payload = payload
        self._block = block
        self._pos = 0
        self.readline_calls = 0

    def read(self, size=-1):
        end = len(self._payload) if size is None or size < 0 else self._pos + size
        out = self._payload[self._pos:end]
        self._pos = min(end, len(self._payload))
        return out

    def readline(self, *args, **kwargs):  # pragma: no cover - piège
        self.readline_calls += 1
        raise AssertionError("readline() est interdit : c'est lui qui coûtait 5 s")


class TestDecoupageEnLignes:
    def test_recompose_les_lignes_a_cheval_sur_deux_blocs(self):
        reader = _FakeReader(b"data: un\ndata: deux\n", block=3)
        assert list(voxtral.iter_sse_lines(reader, read_size=3)) == [b"data: un", b"data: deux"]

    def test_supporte_les_fins_de_ligne_CRLF_et_la_derniere_ligne_sans_saut(self):
        reader = _FakeReader(b"event: x\r\ndata: y")
        assert list(voxtral.iter_sse_lines(reader, read_size=4)) == [b"event: x", b"data: y"]

    def test_n_utilise_JAMAIS_readline(self):
        # `HTTPResponse.readline` avance octet par octet : sur des lignes de
        # 50 Ko, il a coûté 5,1 s de latence là où l'API répondait en 0,47 s.
        reader = _FakeReader(_sse_delta([0.5]) + b"\n")
        list(voxtral.iter_sse_lines(reader, read_size=8))
        assert reader.readline_calls == 0

    def test_chaine_complete_lecteur_vers_pcm(self):
        payload = b"event: speech.audio.delta\n" + _sse_delta([0.0, 1.0]) + b"\n"
        pcm = b"".join(voxtral.iter_pcm_chunks(voxtral.iter_sse_lines(_FakeReader(payload))))
        assert array.array("h", pcm).tolist() == [0, 32767]
