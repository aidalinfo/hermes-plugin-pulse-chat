# -*- coding: utf-8 -*-
"""Rubriques STRUCTUREES d'une demande deliberee (plugin >= 1.9.0) — partie
PURE : mise en forme des parametres, payload, repli pour une app ancienne,
lecture des refus.

Ce qui est borne ici, et chaque point est un mode d'echec MUET :

  - sans rubrique, la trame est EXACTEMENT celle d'avant (une app ancienne ne
    doit rien remarquer) ;
  - le plugin reste MINCE : il ne deduit rien, n'invente aucun risque, et
    laisse les references de pieces jointes telles quelles (l'app les juge) ;
  - seul un refus « champ inconnu » declenche le repli en titre + corps — un
    refus de piece jointe introuvable ne doit JAMAIS etre contourne ;
  - le repli ne fait pas descendre un chemin de coffre dans le corps.
"""

import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gates = _load("gates")

FULL = {
    "reason": " Le builder est bloque. ",
    "steps": [
        {"label": "Redemarrer le builder", "command": "docker restart buildx_buildkit_multiarch0"},
        {"label": "Relancer la CI"},
        "Verifier les images",
        {"command": "sans libelle"},
    ],
    "scope": ["CI", "CI", " Builder ", 3],
    "impact": "30 s d'indisponibilite.",
    "risk": "MEDIUM",
    "reversible": False,
    "attachments": ["vault:rapports/incident.pdf", "message:ck123"],
}


class TestSchemaDeLoutil:
    def test_seul_le_titre_est_requis(self):
        """Le corps devient facultatif avec les rubriques ; l'app refuse une
        demande qui n'a ni l'un ni les autres."""
        assert gates.GATE_TOOL_SCHEMA["parameters"]["required"] == ["title"]

    def test_la_description_de_loutil_enseigne_les_rubriques_et_le_prefixe(self):
        """Le skill n'est jamais annonce au modele : c'est la description qui
        enseigne."""
        desc = gates.GATE_TOOL_DESCRIPTION
        for word in ("reason", "steps", "attachments", "vault:<chemin>", "message:<id>"):
            assert word in desc

    def test_chaque_rubrique_a_une_description(self):
        props = gates.GATE_TOOL_SCHEMA["parameters"]["properties"]
        for key in gates.STRUCTURED_FIELDS:
            assert len(props[key]["description"]) > 40, key
        assert "PREFIXE est obligatoire" in props["attachments"]["description"]
        assert props["risk"]["enum"] == ["low", "medium", "high"]

    def test_les_bornes_sont_celles_de_lapp(self):
        """Miroir de shared/gates.ts : couper ici evite d'envoyer ce que l'app
        refuserait."""
        assert (gates.MAX_STEPS, gates.MAX_SCOPE_ITEMS, gates.MAX_ATTACHMENTS) == (12, 10, 5)
        assert (gates.MAX_REASON_LENGTH, gates.MAX_IMPACT_LENGTH) == (2000, 1000)


class TestStructuredFields:
    def test_met_en_forme_sans_rien_deduire(self):
        out = gates.structured_fields(FULL)
        assert out["reason"] == "Le builder est bloque."
        assert out["steps"] == [
            {"label": "Redemarrer le builder", "command": "docker restart buildx_buildkit_multiarch0"},
            {"label": "Relancer la CI"},
            {"label": "Verifier les images"},
        ]
        assert out["scope"] == ["CI", "Builder"]
        assert out["risk"] == "medium"
        assert out["reversible"] is False
        # Transmises TELLES QUELLES : l'app juge le prefixe et la resolution.
        assert out["attachments"] == ["vault:rapports/incident.pdf", "message:ck123"]

    def test_rien_fourni_rien_envoye(self):
        assert gates.structured_fields({"title": "t", "body": "b"}) == {}

    def test_ne_devine_ni_risque_ni_reversibilite(self):
        out = gates.structured_fields({"risk": "critique", "reversible": "false"})
        assert "risk" not in out
        assert "reversible" not in out

    def test_coupe_aux_bornes(self):
        out = gates.structured_fields(
            {
                "steps": [{"label": f"e{i}"} for i in range(30)],
                "attachments": [f"vault:f{i}.pdf" for i in range(9)],
                "reason": "r" * 5000,
            }
        )
        assert len(out["steps"]) == gates.MAX_STEPS
        assert len(out["attachments"]) == gates.MAX_ATTACHMENTS
        assert len(out["reason"]) == gates.MAX_REASON_LENGTH

    def test_ne_garde_pas_une_reference_sans_prefixe_en_silence(self):
        """Le plugin ne la corrige pas (ce serait DEVINER le magasin) : il la
        transmet, et l'app la refuse avec un message que le modele lit."""
        out = gates.structured_fields({"attachments": ["rapports/incident.pdf"]})
        assert out["attachments"] == ["rapports/incident.pdf"]


class TestPayload:
    def test_sans_rubrique_la_trame_est_celle_davant(self):
        assert gates.build_gate_payload(
            channel_slug="c", request_id="r", title="t", body="b", structured={}
        ) == {"kind": "gate_request", "channelSlug": "c", "requestId": "r", "title": "t", "body": "b"}

    def test_porte_les_rubriques(self):
        p = gates.build_gate_payload(
            channel_slug="c", request_id="r", title="t", body="", structured=gates.structured_fields(FULL)
        )
        assert p["risk"] == "medium"
        assert p["body"] == ""
        assert gates.has_structured(p)


class TestRepli:
    def test_ne_se_declenche_QUE_sur_un_champ_inconnu(self):
        old_app = {"statusMessage": "Payload agent invalide", "data": [{"code": "unrecognized_keys", "keys": ["reason"]}]}
        assert gates.is_unknown_fields_refusal(400, old_app) is True
        # Un refus de piece jointe porte un code : le contourner livrerait un
        # dossier ampute a l'approbateur.
        assert gates.is_unknown_fields_refusal(400, {"data": {"code": "gate_attachment_not_found"}}) is False
        assert gates.is_unknown_fields_refusal(400, {"data": [{"code": "invalid_string"}]}) is False
        assert gates.is_unknown_fields_refusal(422, old_app) is False
        assert gates.is_unknown_fields_refusal(400, None) is False

    def test_replie_les_rubriques_dans_le_corps_sans_chemin_de_coffre(self):
        p = gates.build_gate_payload(
            channel_slug="c", request_id="r", title="t", body="Detail", structured=gates.structured_fields(FULL)
        )
        legacy = gates.legacy_payload(p)
        assert set(legacy) == {"kind", "channelSlug", "requestId", "title", "body"}
        body = legacy["body"]
        assert "Le builder est bloque." in body
        assert "1. Redemarrer le builder — `docker restart buildx_buildkit_multiarch0`" in body
        assert "IRREVERSIBLE" in body
        assert "risque moyen" in body
        assert body.endswith("Detail")
        # Les pieces sont NOMMEES, jamais par leur chemin.
        assert "incident.pdf" in body
        assert "rapports/" not in body

    def test_le_repli_dune_demande_sans_corps_nest_pas_vide(self):
        p = gates.build_gate_payload(
            channel_slug="c", request_id="r", title="t", body="", structured={"steps": [{"label": "a"}]}
        )
        assert gates.legacy_payload(p)["body"].strip()

    def test_le_repli_reste_sous_la_borne_du_corps(self):
        p = gates.build_gate_payload(
            channel_slug="c",
            request_id="r",
            title="t",
            body="b" * gates.MAX_BODY_LENGTH,
            structured={"reason": "r" * gates.MAX_REASON_LENGTH},
        )
        assert len(gates.legacy_payload(p)["body"]) == gates.MAX_BODY_LENGTH


class TestLectureDesRefus:
    def test_resume_les_erreurs_de_schema_pour_le_modele(self):
        body = {
            "statusMessage": "Payload agent invalide",
            "data": [{"code": "invalid_string", "path": ["attachments", 0], "message": "reference attendue"}],
        }
        err = gates.parse_error_body(400, body)
        assert err["code"] == "invalid_request"
        assert "attachments.0 : reference attendue" in err["message"]

    def test_garde_le_code_dun_refus_metier(self):
        body = {"statusMessage": "Aucun fichier", "data": {"code": "gate_attachment_not_found"}}
        assert gates.parse_error_body(400, body)["code"] == "gate_attachment_not_found"

    def test_chaque_refus_de_piece_a_un_conseil(self):
        for code in ("gate_attachment_not_found", "gate_attachment_invalid_ref", "gate_attachment_pending", "gate_body_required", "invalid_request"):
            assert code in gates._ADVICE
