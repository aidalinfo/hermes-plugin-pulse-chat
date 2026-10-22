# -*- coding: utf-8 -*-
"""Partie PURE de ``pulse_ui`` (sans hermes installe).

Ce qui est verifie ici, et chaque point est un mode d'echec MUET :

  - le canal n'est jamais un parametre (choisi par la session, pas par le LLM) ;
  - la forme de l'appel est jugee AVANT tout envoi (op, nom, document, patch) ;
  - le fond n'est PAS juge ici : un document quelconque part tel quel, c'est
    l'app qui sait dire quoi corriger ;
  - un refus de l'app arrive avec SON code, ses ``issues`` et son ``hint`` —
    c'est ce qui permet au modele de corriger sa carte seul ;
  - aucun ``rendered`` sans 2xx.
"""

import json
import sys

import pytest

from test_adapter_dedup import _load_adapter_module

_load_adapter_module()
genui = sys.modules["pulse_chat_plugin_under_test.genui"]


class TestSchema:
    def test_le_nom_est_PREFIXE(self):
        assert genui.UI_TOOL_NAME == "pulse_ui"

    def test_aucun_parametre_de_canal(self):
        props = genui.UI_TOOL_SCHEMA["parameters"]["properties"]
        assert not {"channel", "channel_slug", "chat_id"} & set(props)

    def test_la_description_nomme_le_skill_et_les_types(self):
        assert "pulse-chat:interface" in genui.UI_TOOL_DESCRIPTION
        for kind in ("table", "chart", "comparison", "slider", "button"):
            assert kind in genui.UI_TOOL_DESCRIPTION

    def test_le_skill_existe(self):
        import pathlib

        path = pathlib.Path(genui.__file__).resolve().parent / "skills" / genui.UI_SKILL_NAME / "SKILL.md"
        assert path.is_file()
        assert path.read_text(encoding="utf-8").startswith("---\nname: interface\n")


class TestPayload:
    def test_render(self):
        doc = {"title": "x", "root": "r", "components": [{"id": "r", "type": "nimporte"}]}
        payload = genui.build_ui_payload("demo", {"op": "render", "surface_id": "devis", "document": doc, "text": "  Voici.  "})
        assert payload == {
            "channelSlug": "demo",
            "kind": "ui_surface",
            "op": "render",
            "surfaceId": "devis",
            "document": doc,
            "text": "Voici.",
        }

    def test_patch_et_close(self):
        patch = [{"op": "replace", "path": "/data/x", "value": 1}]
        assert genui.build_ui_payload("demo", {"op": "patch", "surface_id": "a", "patch": patch})["patch"] == patch
        assert "document" not in genui.build_ui_payload("demo", {"op": "close", "surface_id": "a"})

    @pytest.mark.parametrize(
        "args",
        [
            {"op": "delete", "surface_id": "a"},
            {"op": "render", "surface_id": "Avec Espace", "document": {}},
            {"op": "render", "surface_id": "a"},
            {"op": "patch", "surface_id": "a", "patch": []},
            {"op": "patch", "surface_id": "a", "patch": [{}] * 101},
        ],
    )
    def test_forme_refusee_avant_envoi(self, args):
        with pytest.raises(genui.UiToolError) as exc:
            genui.build_ui_payload("demo", args)
        assert exc.value.code == "invalid_request"

    def test_carte_trop_lourde(self):
        doc = {"title": "x", "data": {"blob": "x" * (genui.MAX_DOCUMENT_BYTES + 1)}}
        with pytest.raises(genui.UiToolError) as exc:
            genui.build_ui_payload("demo", {"op": "render", "surface_id": "a", "document": doc})
        assert exc.value.code == "ui_spec_too_large"


class TestResults:
    def test_succes(self):
        out = json.loads(genui.success_result("render", {"surfaceId": "a", "version": 1}))
        assert out["status"] == "rendered"
        assert out["surface_id"] == "a"
        assert "Interface" in out["next"]

    def test_refus_de_l_app_porte_issues_et_hint(self):
        body = {
            "statusMessage": "Carte refusée : document invalide",
            "data": {
                "code": "ui_invalid",
                "issues": [{"path": "components[0].type", "message": "type inconnu", "hint": "types permis : stack"}],
                "hint": "corrige chaque path",
            },
        }
        out = json.loads(genui.http_refusal(400, body))
        assert out["status"] == "refused"
        assert out["code"] == "ui_invalid"
        assert out["issues"][0]["path"] == "components[0].type"
        assert out["next"] == "corrige chaque path"

    def test_refus_sans_code_derive_du_statut(self):
        assert json.loads(genui.http_refusal(404, {"statusMessage": "Canal introuvable"}))["code"] == "channel_not_found"
        assert json.loads(genui.http_refusal(0, None))["code"] == "app_unavailable"
