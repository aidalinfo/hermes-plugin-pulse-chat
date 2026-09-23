# -*- coding: utf-8 -*-
"""Partie PURE des demandes deliberees : payloads, lecture de la reponse, texte
rendu au modele. Aucune I/O, aucun hermes."""

import importlib.util
import json
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


def _frame(**gate):
    base = {
        "requestId": "gate-1",
        "title": "Plan d'extraction",
        "decision": "approved",
        "comment": None,
        "decidedBy": {"userId": "u1", "userName": "Killian"},
        "decidedAt": "2026-09-22T10:00:00.000Z",
    }
    base.update(gate)
    return {"type": "gate.reply", "channel": {"slug": "compta", "name": "Compta"}, "gate": base}


class TestNom:
    def test_le_nom_est_PREFIXE(self):
        """Un nom nu entrerait en collision avec le coeur d'Hermes, et
        ``register_tool`` rendrait None sans erreur visible."""
        assert gates.GATE_TOOL_NAME == "pulse_request_approval"
        assert gates.GATE_TOOL_SCHEMA["name"] == gates.GATE_TOOL_NAME

    def test_le_schema_ne_prend_PAS_de_canal(self):
        """Le canal vient du contexte de session : en faire un parametre ferait
        du choix du canal une sortie de LLM."""
        props = gates.GATE_TOOL_SCHEMA["parameters"]["properties"]
        assert set(props) == {"title", "body", *gates.STRUCTURED_FIELDS}
        assert "channel" not in json.dumps(gates.GATE_TOOL_SCHEMA["parameters"])

    def test_lattente_reste_SOUS_le_plafond_dhermes(self):
        """``_run_async`` coupe un outil asynchrone a 300 s sur le chemin de la
        passerelle : attendre plus ferait echouer l'appel au lieu de rendre
        ``pending``."""
        assert gates.GATE_WAIT_SECONDS < 300

    def test_la_description_nomme_le_skill(self):
        """Un skill de plugin n'est pas annonce au modele : il n'est utile que
        parce que l'outil le nomme."""
        assert "pulse-chat:approvals" in gates.GATE_TOOL_DESCRIPTION


class TestPayload:
    def test_forme(self):
        assert gates.build_gate_payload(
            channel_slug="compta", request_id="gate-1", title=" Plan ", body=" 1. a "
        ) == {
            "kind": "gate_request",
            "channelSlug": "compta",
            "requestId": "gate-1",
            "title": "Plan",
            "body": "1. a",
        }

    def test_borne_les_champs(self):
        p = gates.build_gate_payload(
            channel_slug="c", request_id="r", title="t" * 500, body="b" * 9000
        )
        assert len(p["title"]) == gates.MAX_TITLE_LENGTH
        assert len(p["body"]) == gates.MAX_BODY_LENGTH


class TestParseReply:
    def test_lit_les_trois_issues(self):
        for decision in ("approved", "changes_requested", "denied"):
            assert gates.parse_gate_reply(_frame(decision=decision))["decision"] == decision

    def test_rejette_une_issue_inconnue(self):
        """La deviner ferait dire a l'humain ce qu'il n'a pas dit."""
        assert gates.parse_gate_reply(_frame(decision="once")) is None

    def test_rejette_une_trame_incomplete(self):
        assert gates.parse_gate_reply({"type": "gate.reply"}) is None
        assert gates.parse_gate_reply(_frame(requestId="")) is None
        assert gates.parse_gate_reply({"type": "approval.reply", "gate": {}}) is None

    def test_garde_le_commentaire_et_le_decideur(self):
        r = gates.parse_gate_reply(_frame(decision="changes_requested", comment="Pas 2025"))
        assert r["comment"] == "Pas 2025"
        assert r["decidedBy"] == "Killian"
        assert r["channelSlug"] == "compta"


class TestResultats:
    def test_seule_une_approbation_accorde(self):
        for decision, granted in (("approved", True), ("changes_requested", False), ("denied", False)):
            out = json.loads(gates.tool_result(gates.parse_gate_reply(_frame(decision=decision))))
            assert out["granted"] is granted
            assert out["status"] == decision

    def test_pending_naccorde_rien_et_dit_de_sarreter(self):
        out = json.loads(gates.pending_result("gate-1"))
        assert out["granted"] is False
        assert "ARRETE-TOI" in out["next"]

    def test_un_refus_naccorde_jamais_rien(self):
        out = json.loads(gates.refused_result("no_approver_configured", "Aucun approbateur"))
        assert out["granted"] is False
        assert "Reglages" in out["next"]

    def test_un_code_inconnu_retombe_sur_un_conseil_prudent(self):
        out = json.loads(gates.refused_result("mystere", "?"))
        assert "n'execute pas" in out["next"]

    def test_lit_le_code_dun_refus_de_lapp(self):
        body = {"statusMessage": "Aucun approbateur", "data": {"code": "no_approver_configured"}}
        assert gates.parse_error_body(422, body) == {
            "code": "no_approver_configured",
            "message": "Aucun approbateur",
        }
        assert gates.parse_error_body(500, None)["code"] == "not_sent"


class TestMessageTardif:
    def test_se_suffit_a_lui_meme(self):
        """L'agent reprend sur CE SEUL message : il nomme la demande, l'issue,
        la consigne et la suite."""
        text = gates.decision_message_text(
            gates.parse_gate_reply(_frame(decision="changes_requested", comment="Pas 2025"))
        )
        assert "Plan d'extraction" in text
        assert "AJUSTEMENTS" in text
        assert "Pas 2025" in text
        assert "resoumets" in text
