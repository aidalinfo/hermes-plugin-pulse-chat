# -*- coding: utf-8 -*-
"""Partie PURE des questions : payloads sortants et lecture de la reponse.

Aucune I/O, aucun hermes — ``questions.py`` est importable seul, comme
``approvals.py`` et ``classification.py``.
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


questions = _load("questions")


class TestNormalizeChoices:
    def test_conserve_lordre(self):
        """Hermes documente « put your recommended option FIRST », et c'est le
        premier qui porte le suffixe : trier deplacerait la recommandation."""
        assert questions.normalize_choices(["c", "a", "b"]) == ["c", "a", "b"]

    def test_borne_le_nombre_a_celui_dHermes(self):
        assert len(questions.normalize_choices([f"c{i}" for i in range(10)])) == 4

    def test_borne_la_longueur(self):
        assert len(questions.normalize_choices(["x" * 500])[0]) == 200

    def test_jette_ce_qui_nest_pas_une_chaine_exploitable(self):
        assert questions.normalize_choices([None, 3, "  ", "ok", {}]) == ["ok"]

    def test_absence_de_choix_rend_une_liste_vide(self):
        """Question OUVERTE : un des deux regimes, pas une anomalie."""
        assert questions.normalize_choices(None) == []


class TestBuildQuestionPayload:
    def test_forme_du_payload(self):
        payload = questions.build_question_payload(
            channel_slug="support",
            request_id="clr-1",
            question="  Quel niveau ?  ",
            choices=["A (Recommended)", "B"],
        )
        assert payload == {
            "channelSlug": "support",
            "kind": "question_request",
            "requestId": "clr-1",
            "question": "Quel niveau ?",
            "choices": ["A (Recommended)", "B"],
        }

    def test_le_suffixe_recommended_nest_PAS_retire(self):
        """C'est la chaine BRUTE qui repartira a Hermes.

        ``resolve_gateway_clarify`` se resout sur le libelle tel que l'agent
        l'a ecrit (ce que fait l'adaptateur Slack) ; nettoyer ici ferait passer
        un clic de bouton pour de la prose libre. Le suffixe n'est retire qu'a
        l'AFFICHAGE, cote app.
        """
        payload = questions.build_question_payload(
            "support", "clr-1", "Q", ["Lecture seule (Recommended)"]
        )
        assert payload["choices"] == ["Lecture seule (Recommended)"]

    def test_omet_choices_quand_il_est_vide(self):
        payload = questions.build_question_payload("support", "clr-1", "Q", None)
        assert "choices" not in payload

    def test_borne_lenonce(self):
        payload = questions.build_question_payload("support", "clr-1", "x" * 5000)
        assert len(payload["question"]) == 1000


class TestBuildQuestionRetirePayload:
    def test_forme_du_payload(self):
        assert questions.build_question_retire_payload("support", "clr-1") == {
            "channelSlug": "support",
            "kind": "question_retire",
            "requestId": "clr-1",
        }


class TestParseQuestionReply:
    def _frame(self, **over):
        question = {
            "requestId": "clr-1",
            "answer": "Lecture seule",
            "answeredBy": {"userId": "u1", "userName": "Alice"},
            "answeredAt": "2026-09-22T08:00:00.000Z",
        }
        question.update(over)
        return {"type": "question.reply", "question": question}

    def test_lit_une_trame_valide(self):
        parsed = questions.parse_question_reply(self._frame())
        assert parsed["requestId"] == "clr-1"
        assert parsed["answer"] == "Lecture seule"
        assert parsed["answeredBy"]["userName"] == "Alice"

    def test_refuse_une_reponse_VIDE(self):
        """Elle debloquerait l'agent sur une chaine vide : il reprendrait son
        tour en croyant avoir ete repondu, sans qu'aucune erreur n'apparaisse."""
        assert questions.parse_question_reply(self._frame(answer="   ")) is None
        assert questions.parse_question_reply(self._frame(answer=None)) is None

    def test_refuse_une_trame_dun_autre_type(self):
        assert questions.parse_question_reply({"type": "approval.reply"}) is None
        assert questions.parse_question_reply("pas un dict") is None
        assert questions.parse_question_reply({"type": "question.reply"}) is None

    def test_tolere_un_answeredBy_absent(self):
        """Une reponse automatique n'a pas d'auteur humain — le deblocage ne
        doit pas en dependre."""
        parsed = questions.parse_question_reply(self._frame(answeredBy=None))
        assert parsed["answeredBy"] == {"userId": "", "userName": ""}
