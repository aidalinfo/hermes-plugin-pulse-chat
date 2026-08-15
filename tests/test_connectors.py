# -*- coding: utf-8 -*-
"""Connecteurs — module PUR, testable sans hermes ni reseau.

Ce que ces tests protegent : la liste blanche locale ne doit jamais « corriger »
une capacite en silence, et un echec ne doit jamais pouvoir passer pour une
autorisation.
"""

import pytest

from connectors import (
    CONNECTOR_CAPABILITIES,
    ConnectorCapabilityError,
    build_connector_payload,
    connector_url,
    has_side_effect,
    normalize_capability,
    parse_connector_error,
)


class TestNormalizeCapability:
    def test_accepte_les_capacites_du_catalogue(self):
        for capability in CONNECTOR_CAPABILITIES:
            assert normalize_capability(capability) == capability

    def test_tolere_les_espaces_de_bordure(self):
        assert normalize_capability("  mail.draft  ") == "mail.draft"

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", None, 42, "mail.exfiltrate", "mail.readFromSender", "../vault"],
    )
    def test_refuse_tout_le_reste(self, raw):
        # Refuser plutot que nettoyer : une capacite reecrite ferait agir l'agent
        # autrement qu'il ne le croit.
        with pytest.raises(ConnectorCapabilityError):
            normalize_capability(raw)


class TestSideEffect:
    def test_le_brouillon_n_a_pas_d_effet_de_bord(self):
        # C'est ce qui permet de le livrer sans approbation asynchrone : rien ne
        # quitte le tenant, l'humain enverra lui-meme.
        assert has_side_effect("mail.draft") is False
        assert has_side_effect("mail.read") is False

    def test_les_ecritures_sortantes_en_ont_un(self):
        for capability in ("mail.send", "calendar.write", "teams.post"):
            assert has_side_effect(capability) is True

    def test_une_capacite_inconnue_est_traitee_comme_dangereuse(self):
        # Repli le plus PRUDENT : la traiter comme sans effet de bord la ferait
        # passer sous le radar des approbations.
        assert has_side_effect("capacite.inventee") is True


class TestConnectorUrl:
    def test_construit_l_url_et_valide_la_capacite(self):
        assert (
            connector_url("https://chat.example.test", "mail.draft")
            == "https://chat.example.test/api/agent/connectors/mail.draft"
        )

    def test_tolere_un_slash_final(self):
        assert connector_url("https://x.test/", "mail.read").endswith(
            "/api/agent/connectors/mail.read"
        )

    def test_refuse_une_capacite_inconnue_avant_le_reseau(self):
        with pytest.raises(ConnectorCapabilityError):
            connector_url("https://x.test", "mail.delete")


class TestBuildPayload:
    def test_forme_minimale(self):
        assert build_connector_payload("canal-demo") == {
            "channelSlug": "canal-demo",
            "params": {},
        }

    def test_omet_les_champs_vides_plutot_que_de_les_poser_a_none(self):
        # Le serveur distingue « non precise » (il resout, et refuse si ambigu)
        # de « precise » : une cle nulle sur le fil brouillerait la distinction.
        payload = build_connector_payload("c", {"search": "x"}, on_behalf_of=None, grant_id="")
        assert "onBehalfOf" not in payload
        assert "grantId" not in payload

    def test_transmet_les_champs_fournis(self):
        payload = build_connector_payload(
            "c", {"search": "x"}, on_behalf_of="u-1", grant_id="g-1"
        )
        assert payload["onBehalfOf"] == "u-1"
        assert payload["grantId"] == "g-1"
        assert payload["params"] == {"search": "x"}

    def test_des_params_non_dict_sont_remplaces_par_un_dict_vide(self):
        assert build_connector_payload("c", "pas-un-dict")["params"] == {}


class TestParseConnectorError:
    def test_un_echec_ne_ressemble_jamais_a_une_autorisation(self):
        result = parse_connector_error(403, {"data": {"code": "connector_no_grant"}})
        assert result["ok"] is False
        assert "granted" not in result

    def test_rend_un_conseil_actionnable_par_code(self):
        result = parse_connector_error(
            409, {"data": {"code": "connector_grant_ambiguous", "options": [{"grantId": "g"}]}}
        )
        assert result["code"] == "connector_grant_ambiguous"
        assert "DEMANDER" in result["hint"]
        assert result["ambiguous_options"] == [{"grantId": "g"}]

    def test_un_refus_de_delegation_n_est_pas_retentable(self):
        # Insister ne changerait rien et remplirait le journal de refus identiques.
        for code in ("connector_no_grant", "connector_approval_required"):
            assert parse_connector_error(403, {"data": {"code": code}})["retryable"] is False

    def test_un_quota_ou_une_panne_est_retentable(self):
        assert parse_connector_error(429, {})["retryable"] is True
        assert parse_connector_error(502, {})["retryable"] is True

    def test_un_corps_illisible_donne_une_forme_exploitable(self):
        # Source reseau : on ne suppose jamais la forme recue.
        for body in (None, "texte brut", 42, [], {}):
            result = parse_connector_error(500, body)
            assert result["ok"] is False
            assert result["code"] == "connector_error"
            assert isinstance(result["message"], str)
            assert result["ambiguous_options"] == []
