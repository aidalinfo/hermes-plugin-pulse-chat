# -*- coding: utf-8 -*-
"""Partie PURE de ``pulse_podcast`` (sans hermes installe).

Ce qui est verifie ici, et chaque point est un mode d'echec MUET :

  - rien n'est TRONQUE : une coupe ne se lit pas, elle s'entend — la voix
    s'arreterait au milieu d'une phrase ;
  - un ``summary`` vide n'est pas envoye ;
  - un refus de l'app arrive avec SON code et son message, jamais en ``queued`` ;
  - un 501 dit « synthese indisponible », que le modele peut redire tel quel.
"""

import json
import sys

from test_adapter_dedup import _load_adapter_module

_load_adapter_module()
podcast = sys.modules["pulse_chat_plugin_under_test.podcast"]


class TestSchema:
    def test_le_nom_est_PREFIXE(self):
        assert podcast.PODCAST_TOOL_NAME == "pulse_podcast"

    def test_aucun_parametre_de_canal(self):
        props = podcast.PODCAST_TOOL_SCHEMA["parameters"]["properties"]
        assert not {"channel", "channel_slug", "chat_id"} & set(props)

    def test_les_bornes_sont_ANNONCEES(self):
        params = podcast.PODCAST_TOOL_SCHEMA["parameters"]
        assert params["properties"]["title"]["maxLength"] == 120
        chapters = params["properties"]["chapters"]
        assert chapters["maxItems"] == 20
        item = chapters["items"]["properties"]
        assert item["title"]["maxLength"] == 120
        assert item["summary"]["maxLength"] == 200
        assert set(chapters["items"]["required"]) == {"title", "text"}
        assert "200" in podcast.PODCAST_TOOL_DESCRIPTION
        assert "15000" in podcast.PODCAST_TOOL_DESCRIPTION

    def test_la_description_interdit_de_recopier_et_exige_de_la_prose(self):
        text = podcast.PODCAST_TOOL_DESCRIPTION
        assert "NE recopie PAS" in text
        assert "PROSE PARLEE" in text
        assert "Markdown" in text
        assert "tu ne le passes pas" in text
        assert "MEME titre" in text

    def test_url_encode_le_slug(self):
        assert (
            podcast.podcast_url("http://app.test/", "a b/c")
            == "http://app.test/api/agent/podcasts/a%20b%2Fc"
        )


class TestPayload:
    def test_rien_nest_tronque(self):
        long_text = "Une phrase parlee. " * 2000  # bien au-dela de 15 000
        long_title = "T" * 500
        payload = podcast.build_podcast_payload(
            {"title": long_title, "chapters": [{"title": long_title, "text": long_text}]}
        )
        assert payload["title"] == long_title
        assert payload["chapters"][0]["title"] == long_title
        assert payload["chapters"][0]["text"] == long_text.strip()

    def test_strip_et_summary_omis_sil_est_vide(self):
        payload = podcast.build_podcast_payload(
            {
                "title": "  Point hebdo  ",
                "chapters": [
                    {"title": " Chiffres ", "summary": "   ", "text": " Ce mois-ci... "},
                    {"title": "Suite", "summary": " Le plan ", "text": "Ensuite..."},
                ],
            }
        )
        assert payload == {
            "title": "Point hebdo",
            "chapters": [
                {"title": "Chiffres", "text": "Ce mois-ci..."},
                {"title": "Suite", "text": "Ensuite...", "summary": "Le plan"},
            ],
        }

    def test_les_chapitres_non_objets_sont_jetes(self):
        payload = podcast.build_podcast_payload(
            {"title": "x", "chapters": ["texte nu", 3, None, {"title": "a", "text": "b"}]}
        )
        assert payload["chapters"] == [{"title": "a", "text": "b"}]

    def test_des_chapitres_absents_partent_vides_lapp_tranche(self):
        assert podcast.build_podcast_payload({"title": "x"}) == {"title": "x", "chapters": []}


class TestResultats:
    def test_queued(self):
        out = json.loads(podcast.queued_result("pod_1"))
        assert out["status"] == "queued"
        assert out["podcastId"] == "pod_1"
        assert "Ne le recopie pas" in out["next"]

    def _refus(self, status, body):
        return json.loads(podcast.http_refusal(status, body))

    def test_code_de_lapp_relaye_tel_quel(self):
        for status, code in (
            (422, "voice_disabled"),
            (409, "podcast_in_progress"),
            (409, "agent_session_required"),
            (409, "emitter_ambiguous"),
            (400, "podcast_text_too_short"),
            (413, "podcast_text_too_long"),
            (404, "channel_not_found"),
        ):
            out = self._refus(status, {"statusMessage": f"motif {code}", "data": {"code": code}})
            assert out["status"] == "refused"
            assert out["code"] == code
            assert out["message"] == f"motif {code}"
            assert out["next"] == podcast._ADVICE[code]

    def test_la_borne_arrive_dans_le_message(self):
        out = self._refus(
            413,
            {
                "statusMessage": "Texte trop long : 15 000 caracteres au plus",
                "data": {"code": "podcast_text_too_long"},
            },
        )
        assert "15 000" in out["message"]

    def test_un_400_de_schema_resume_les_issues(self):
        out = self._refus(
            400,
            {
                "statusMessage": "Payload invalide",
                "data": [{"path": ["chapters", 0, "text"], "message": "Required"}],
            },
        )
        assert out["code"] == "invalid_request"
        assert "chapters.0.text : Required" in out["message"]

    def test_501_dit_synthese_indisponible(self):
        out = self._refus(501, {"statusMessage": "MISTRAL_API_KEY absente", "data": {"code": "podcast_synthesis_unavailable"}})
        assert out["code"] == "podcast_synthesis_unavailable"
        assert "Synthèse indisponible sur cette instance" in out["message"]
        # Meme sans code dans le corps.
        bare = self._refus(501, None)
        assert bare["code"] == "podcast_synthesis_unavailable"
        assert "Synthèse indisponible" in bare["message"]

    def test_403_sans_code_et_panne(self):
        assert self._refus(403, {"statusMessage": "Forbidden"})["code"] == "not_authorized"
        assert self._refus(0, {"message": "timeout"})["code"] == "app_unavailable"
        assert self._refus(500, None)["code"] == "app_unavailable"

    def test_codes_locaux_ont_un_conseil(self):
        for code in ("no_channel", "not_connected", "not_sent", "invalid_request"):
            assert code in podcast._ADVICE

