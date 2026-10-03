# -*- coding: utf-8 -*-
"""Partie PURE des sous-sessions (sans hermes installe).

Ce qui est verifie ici, et chaque point est un mode d'echec MUET :

  - aucun parametre de canal : le canal est une sortie du contexte, pas du LLM ;
  - rien n'est TRONQUE : une consigne coupee serait le seul contexte des
    autres agents, amputee sans que personne ne le sache ;
  - ``emitterProfile`` n'est jamais envoye (schema ``strict()`` de l'app) ;
  - chaque refus arrive avec SON code, son message et son conseil, et
    ``callee_not_allowed`` rend la liste des agents appelables ;
  - un 404 sans code (app < 0.51.0) se dit, au lieu d'un « HTTP 404 ».
"""

import json
import sys

import pytest

from test_adapter_dedup import _load_adapter_module

_load_adapter_module()
ss = sys.modules["pulse_chat_plugin_under_test.subsessions"]


class TestSchemas:
    def test_les_noms_sont_PREFIXES(self):
        assert ss.OPEN_TOOL_NAME == "pulse_open_subsession"
        assert ss.REPORT_TOOL_NAME == "pulse_subsession_report"

    @pytest.mark.parametrize("schema", ["OPEN_TOOL_SCHEMA", "REPORT_TOOL_SCHEMA"])
    def test_aucun_parametre_de_canal_ni_demetteur(self, schema):
        props = getattr(ss, schema)["parameters"]["properties"]
        assert not {"channel", "channel_slug", "chat_id", "emitterProfile"} & set(props)

    def test_les_bornes_sont_ANNONCEES(self):
        params = ss.OPEN_TOOL_SCHEMA["parameters"]
        assert params["properties"]["agents"]["minItems"] == 1
        assert params["properties"]["agents"]["maxItems"] == 5
        assert params["properties"]["title"]["maxLength"] == 120
        assert params["properties"]["brief"]["maxLength"] == 8000
        assert set(params["required"]) == {"agents", "title", "brief"}
        assert "subsession" in params["properties"]
        report = ss.REPORT_TOOL_SCHEMA["parameters"]
        assert report["properties"]["text"]["maxLength"] == 8000
        assert report["required"] == ["text"]
        assert "8000" in ss.OPEN_TOOL_DESCRIPTION
        assert "8000" in ss.REPORT_TOOL_DESCRIPTION

    def test_la_description_douverture_dit_lessentiel(self):
        text = ss.OPEN_TOOL_DESCRIPTION
        assert "Peut appeler" in text
        assert "Tete-a-tete" in text
        assert "SEUL contexte" in text
        assert "COMPLETE" in text
        assert "coffre" in text
        assert "PARTICIPES" in text
        assert "pulse_subsession_report" in text
        assert "n'invente jamais" in text
        assert "tu ne le passes pas" in text

    def test_la_description_du_rapport_dit_lessentiel(self):
        text = ss.REPORT_TOOL_DESCRIPTION
        assert "DEPUIS la sous-session" in text
        assert "SEUL chemin" in text
        assert "[Sous-session" in text
        assert "Plusieurs" in text
        assert "`delivered: false` n'est PAS un echec" in text

    def test_urls(self):
        assert ss.open_url("http://app.test/") == "http://app.test/api/agent/subsessions/open"
        assert ss.report_url("http://app.test") == "http://app.test/api/agent/subsessions/report"


class TestPayload:
    def test_ouverture(self):
        out = ss.build_open_payload(
            "tt-1",
            {"agents": [" atlas ", "", 3, "nova"], "title": " Devis ", "brief": "  Fais X.  "},
        )
        assert out == {"channel": "tt-1", "agents": ["atlas", "nova"], "title": "Devis", "brief": "Fais X."}

    def test_le_canal_des_arguments_nest_pas_lu(self):
        out = ss.build_open_payload("tt-1", {"channel": "autre", "agents": ["a"], "title": "t", "brief": "b"})
        assert out["channel"] == "tt-1"
        assert "emitterProfile" not in out

    def test_relance(self):
        out = ss.build_open_payload("tt-1", {"subsession": " ss-12ab ", "brief": "Encore"})
        assert out == {"channel": "tt-1", "agents": [], "title": "", "brief": "Encore", "subsession": "ss-12ab"}

    def test_subsession_vide_est_omis(self):
        assert "subsession" not in ss.build_open_payload("c", {"subsession": "  ", "agents": ["a"]})

    def test_une_chaine_seule_devient_une_liste(self):
        assert ss.build_open_payload("c", {"agents": "atlas"})["agents"] == ["atlas"]

    def test_doublon_et_appelant_PARTENT(self):
        # Mise en forme, jamais jugement : l'app refuse avec un code nomme.
        assert ss.build_open_payload("c", {"agents": ["a", "a"]})["agents"] == ["a", "a"]

    def test_rien_nest_tronque(self):
        brief = "Une phrase. " * 2000
        title = "T" * 500
        out = ss.build_open_payload("c", {"agents": ["a"] * 9, "title": title, "brief": brief})
        assert out["brief"] == brief.strip()
        assert out["title"] == title
        assert len(out["agents"]) == 9
        assert ss.build_report_payload("c", {"text": brief})["text"] == brief.strip()

    def test_rapport(self):
        assert ss.build_report_payload("ss-1", {"text": " Fini ", "channel": "x"}) == {
            "channel": "ss-1",
            "text": "Fini",
        }

    def test_arguments_absents(self):
        assert ss.build_open_payload("c", None) == {"channel": "c", "agents": [], "title": "", "brief": ""}
        assert ss.build_report_payload("c", None) == {"channel": "c", "text": ""}


class TestResultats:
    def test_ouverte(self):
        out = json.loads(ss.opened_result({"subsession": "sub_1", "slug": "ss-ab12", "status": "open"}, relaunch=False))
        assert out["status"] == "opened"
        assert out["slug"] == "ss-ab12"
        assert out["subsession"] == "sub_1"
        assert out["subsessionStatus"] == "open"
        assert "pulse_subsession_report" in out["next"]
        assert 'subsession="ss-ab12"' in out["next"]

    def test_relancee(self):
        out = json.loads(ss.opened_result({"subsession": "sub_1", "slug": "ss-ab12", "status": "open"}, relaunch=True))
        assert out["status"] == "relaunched"

    def test_rapport_livre(self):
        out = json.loads(ss.reported_result({"subsession": "s", "status": "reported", "delivered": True}))
        assert out["status"] == "reported"
        assert out["delivered"] is True

    def test_rapport_non_livre_nest_pas_un_echec(self):
        out = json.loads(ss.reported_result({"subsession": "s", "status": "reported", "delivered": False}))
        assert out["status"] == "reported"
        assert out["delivered"] is False
        assert "PAS un echec" in out["next"]
        assert "Ne le renvoie pas" in out["next"]


def _h3(status, code, message="motif", **data):
    return status, {"statusCode": status, "statusMessage": message, "data": dict(code=code, **data)}


CODES = [
    (409, "agent_session_required"),
    (404, "channel_not_found"),
    (409, "subsession_not_allowed_here"),
    (422, "callee_unreachable"),
    (400, "callees_required"),
    (400, "too_many_callees"),
    (400, "duplicate_callee"),
    (400, "opener_cannot_be_callee"),
    (400, "title_required"),
    (400, "title_too_long"),
    (400, "brief_required"),
    (400, "brief_too_long"),
    (404, "subsession_not_found"),
    (409, "subsession_closed"),
    (400, "subsession_callees_fixed"),
    (409, "not_subsession_opener"),
    (400, "text_required"),
    (400, "text_too_long"),
    (409, "emitter_ambiguous"),
]


class TestRefus:
    @pytest.mark.parametrize("status,code", CODES)
    def test_chaque_refus_est_nomme_avec_son_conseil(self, status, code):
        out = json.loads(ss.http_refusal(*_h3(status, code, f"motif {code}")))
        assert out["status"] == "refused"
        assert out["code"] == code
        assert out["message"] == f"motif {code}"
        # Un conseil PROPRE au code, jamais le repli generique.
        assert out["next"] == ss._ADVICE[code]
        assert out["next"] != ss._ADVICE["not_sent"]

    def test_callee_not_allowed_rend_la_liste(self):
        allowed = [{"profile": "atlas", "displayName": "Atlas"}, {"profile": "nova", "displayName": "Nova"}]
        out = json.loads(
            ss.http_refusal(*_h3(422, "callee_not_allowed", "Agent non autorise : zeus", allowed=allowed, notAllowed=["zeus"]))
        )
        assert out["code"] == "callee_not_allowed"
        assert out["allowed"] == allowed
        assert out["notAllowed"] == ["zeus"]
        assert "`allowed`" in out["next"]
        assert "Peut appeler" in out["next"]

    def test_callee_not_allowed_sans_liste_rend_une_liste_vide(self):
        out = json.loads(ss.http_refusal(*_h3(422, "callee_not_allowed")))
        assert out["allowed"] == []

    def test_callee_unreachable_nomme_lagent(self):
        out = json.loads(ss.http_refusal(*_h3(422, "callee_unreachable", agent="zeus")))
        assert out["agent"] == "zeus"

    @pytest.mark.parametrize("code", ["brief_too_long", "title_too_long", "text_too_long"])
    def test_la_borne_est_relayee(self, code):
        out = json.loads(ss.http_refusal(*_h3(400, code, max=8000)))
        assert out["max"] == 8000

    def test_400_zod_sous_data_issues(self):
        body = {
            "statusMessage": "Requête invalide",
            "data": {"issues": [{"path": ["agents", 0], "message": "Too long"}]},
        }
        out = json.loads(ss.http_refusal(400, body))
        assert out["code"] == "invalid_request"
        assert "agents.0 : Too long" in out["message"]

    def test_404_sans_code_est_une_app_trop_ancienne(self):
        out = json.loads(ss.http_refusal(404, {"statusCode": 404, "statusMessage": "Page not found"}))
        assert out["code"] == "subsessions_unavailable"
        assert "0.51.0" in out["next"]

    @pytest.mark.parametrize("status", [0, 500, 502, 503])
    def test_app_injoignable(self, status):
        out = json.loads(ss.http_refusal(status, {"message": "connexion refusee"}))
        assert out["code"] == "app_unavailable"

    @pytest.mark.parametrize("status", [401, 403])
    def test_identite_refusee(self, status):
        assert json.loads(ss.http_refusal(status, None))["code"] == "not_authorized"

    def test_code_inconnu_garde_son_nom_et_le_conseil_generique(self):
        out = json.loads(ss.http_refusal(*_h3(409, "nouveau_code")))
        assert out["code"] == "nouveau_code"
        assert out["next"] == ss._ADVICE["not_sent"]
