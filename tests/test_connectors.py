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


#: Catalogue attendu, dans l'ordre de ``shared/connectors.ts``. Ce test n'est
#: pas une redondance du dict : il rend la DERIVE visible. Le miroir a deja
#: decroche une fois — l'app avait livre ``mail.content`` et les capacites
#: GitHub, ce module refusait localement les appels correspondants avec
#: « capacite inconnue », sans qu'aucun test ne rougisse. Ajouter une capacite
#: cote app doit casser ici, et forcer la mise a jour consciente du miroir.
CATALOGUE_ATTENDU = (
    "mail.read",
    "mail.content",
    "mail.draft",
    "mail.send",
    "mail.reply",
    "mail.forward",
    "mail.draft.update",
    "mail.draft.delete",
    "calendar.read",
    "calendar.write",
    "tasks.read",
    "tasks.write",
    "tasks.delete",
    "teams.post",
    "repo.read",
    "issues.read",
    "issues.write",
    "pr.read",
    "pr.write",
    "ci.read",
)


class TestCatalogue:
    def test_miroir_complet_de_shared_connectors_ts(self):
        assert sorted(CONNECTOR_CAPABILITIES) == sorted(CATALOGUE_ATTENDU)


class TestSideEffect:
    def test_le_brouillon_n_a_pas_d_effet_de_bord(self):
        # C'est ce qui permet de le livrer sans approbation asynchrone : rien ne
        # quitte le tenant, l'humain enverra lui-meme.
        assert has_side_effect("mail.draft") is False
        assert has_side_effect("mail.read") is False

    def test_les_ecritures_sortantes_en_ont_un(self):
        for capability in ("mail.send", "calendar.write", "teams.post"):
            assert has_side_effect(capability) is True

    def test_les_taches_suivent_la_regle_du_brouillon_sauf_la_suppression(self):
        # Une tache creee reste dans l'espace du delegant : rien a approuver.
        assert has_side_effect("tasks.read") is False
        assert has_side_effect("tasks.write") is False
        # To Do n'a pas de corbeille : la suppression est irreversible.
        assert has_side_effect("tasks.delete") is True

    def test_le_courriel_lu_en_entier_reste_sans_effet_de_bord(self):
        # Autorite plus large que ``mail.read`` (corps complet + pieces jointes
        # deposees dans le coffre), mais rien ne sort du tenant vers un tiers.
        # La marquer autrement la rendrait inutilisable : le proxy refuse tant
        # qu'aucune approbation n'est cablee.
        assert has_side_effect("mail.content") is False

    def test_la_reponse_et_le_transfert_prennent_le_repli_prudent(self):
        # Leur effet REEL depend de ``params.mode`` et n'est tranche que par le
        # serveur (``capabilitySideEffect``). Ce dict-ci etant statique, il
        # annonce le cas le plus engageant : un envoi peut en decouler.
        assert has_side_effect("mail.reply") is True
        assert has_side_effect("mail.forward") is True

    def test_modifier_ou_jeter_un_brouillon_suit_la_regle_du_brouillon(self):
        # Rien ne quitte le tenant, et un courriel supprime chez Graph part en
        # « Elements supprimes » — recuperable, a la difference de To Do.
        assert has_side_effect("mail.draft.update") is False
        assert has_side_effect("mail.draft.delete") is False

    def test_github_lit_sans_effet_de_bord_mais_ecrit_avec(self):
        for capability in ("repo.read", "issues.read", "pr.read", "ci.read"):
            assert has_side_effect(capability) is False
        # Un commentaire de ticket NOTIFIE des tiers sous le nom du delegant :
        # meme regime que ``mail.send``, a la difference de ``tasks.write``.
        for capability in ("issues.write", "pr.write"):
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
        for code in (
            "connector_no_grant",
            "connector_approval_required",
            "connector_not_activated",
        ):
            assert parse_connector_error(403, {"data": {"code": code}})["retryable"] is False

    def test_un_outil_eteint_dit_ou_l_humain_doit_cliquer(self):
        # « Pas de delegation » et « delegation eteinte dans ce fil » demandent
        # deux gestes DIFFERENTS a l'humain : accorder, ou allumer. Un conseil
        # qui les confond envoie l'utilisateur au mauvais ecran.
        result = parse_connector_error(403, {"data": {"code": "connector_not_activated"}})
        assert "allume" in result["hint"]
        assert "CETTE" in result["hint"]

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
