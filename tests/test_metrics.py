# -*- coding: utf-8 -*-
"""Extraction des metriques d'execution (module pur).

Le `metadata` d'Hermes est de forme libre : ces tests verrouillent la
tolerance aux conventions courantes, et surtout le fait qu'une absence de
mesure donne `None` plutot qu'un bloc de nulls.
"""

import importlib.util
import sys
import types
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load():
    pkg = "pulse_chat_metrics_under_test"
    if pkg not in sys.modules:
        package = types.ModuleType(pkg)
        package.__path__ = [str(_PLUGIN_DIR)]
        sys.modules[pkg] = package
    name = "%s.metrics" % pkg
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / "metrics.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


metrics = _load()


class TestRienAExtraire:
    def test_none_quand_metadata_absent_ou_invalide(self):
        for empty in (None, {}, [], "texte", 42):
            assert metrics.extract_metrics(empty) is None

    def test_none_quand_aucune_cle_reconnue(self):
        assert metrics.extract_metrics({"foo": "bar", "count": 3}) is None


class TestSousDictMetrics:
    def test_bloc_metrics_explicite(self):
        got = metrics.extract_metrics(
            {"metrics": {"model": "claude-opus-5", "inputTokens": 120, "outputTokens": 45, "durationMs": 1800}}
        )
        assert got == {
            "model": "claude-opus-5",
            "inputTokens": 120,
            "outputTokens": 45,
            "durationMs": 1800,
        }


class TestConventions:
    def test_convention_anthropic(self):
        got = metrics.extract_metrics(
            {"model": "claude-opus-5", "usage": {"input_tokens": 10, "output_tokens": 20}}
        )
        assert got["inputTokens"] == 10 and got["outputTokens"] == 20
        assert got["model"] == "claude-opus-5"

    def test_convention_openai(self):
        got = metrics.extract_metrics(
            {"usage": {"prompt_tokens": 7, "completion_tokens": 9}, "model_name": "gpt-x"}
        )
        assert got["inputTokens"] == 7 and got["outputTokens"] == 9
        assert got["model"] == "gpt-x"

    def test_duree_sous_plusieurs_noms(self):
        for key in ("duration_ms", "durationMs", "latency_ms", "elapsed_ms"):
            assert metrics.extract_metrics({key: 1234})["durationMs"] == 1234

    def test_lecture_a_plat(self):
        got = metrics.extract_metrics({"model": "m", "input_tokens": 1, "output_tokens": 2})
        assert got == {"model": "m", "inputTokens": 1, "outputTokens": 2, "durationMs": None}


class TestRobustesse:
    def test_les_booleens_ne_sont_pas_des_mesures(self):
        # bool est un int en Python : sans garde, True deviendrait 1 token.
        assert metrics.extract_metrics({"input_tokens": True}) is None

    def test_les_flottants_sont_arrondis(self):
        assert metrics.extract_metrics({"duration_ms": 1500.7})["durationMs"] == 1500

    def test_modele_vide_ignore(self):
        assert metrics.extract_metrics({"model": "   "}) is None

    def test_le_premier_dict_renseigne_gagne(self):
        got = metrics.extract_metrics(
            {"metrics": {"model": "prioritaire"}, "model": "a-plat"}
        )
        assert got["model"] == "prioritaire"
