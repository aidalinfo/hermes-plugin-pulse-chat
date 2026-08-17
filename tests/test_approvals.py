# -*- coding: utf-8 -*-
"""Transport des approbations — partie pure (aucun hermes requis).

Miroir de app/pulse-chat/tests/unit/approvals.spec.ts : les deux cotes doivent
filtrer les memes options et refuser les memes decisions.
"""

import importlib.util
import sys
import types
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load_approvals():
    """Charge ``approvals.py`` par chemin (le paquet contient un tiret)."""
    pkg_name = "pulse_chat_approvals_under_test"
    if pkg_name not in sys.modules:
        package = types.ModuleType(pkg_name)
        package.__path__ = [str(_PLUGIN_DIR)]
        sys.modules[pkg_name] = package
    module_name = f"{pkg_name}.approvals"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name, _PLUGIN_DIR / "approvals.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


approvals = _load_approvals()


class TestNormalizeOptions:
    def test_ordre_canonique(self):
        assert approvals.normalize_options(["deny", "always", "once"]) == [
            "once",
            "always",
            "deny",
        ]

    def test_dedoublonne(self):
        assert approvals.normalize_options(["once", "once", "deny"]) == ["once", "deny"]

    def test_ignore_les_inconnues(self):
        assert approvals.normalize_options(["once", "rm-rf", None, 42]) == [
            "once",
            "deny",
        ]

    def test_repli_sur_once_deny(self):
        assert approvals.normalize_options(None) == ["once", "deny"]
        assert approvals.normalize_options([]) == ["once", "deny"]
        assert approvals.normalize_options(["inconnu"]) == ["once", "deny"]

    def test_ajoute_toujours_une_issue_negative(self):
        assert "deny" in approvals.normalize_options(["always"])


class TestBuildApprovalPayload:
    def test_forme_du_payload(self):
        payload = approvals.build_approval_payload(
            channel_slug="demo",
            request_id="req-1",
            tool="execute_code",
            command="print(1)",
            reason="execute_code script execution",
            options=["once", "deny"],
        )
        assert payload == {
            "channelSlug": "demo",
            "kind": "approval_request",
            "requestId": "req-1",
            "tool": "execute_code",
            "command": "print(1)",
            "reason": "execute_code script execution",
            "options": ["once", "deny"],
        }

    def test_reason_optionnelle(self):
        payload = approvals.build_approval_payload("demo", "req-2", "sh", "ls")
        assert payload["reason"] is None
        assert payload["options"] == ["once", "deny"]

    def test_champs_vides_OMIS_et_pas_envoyes_a_null(self):
        """Compatibilite avec une app anterieure a `summary`/`risks`.

        Le schema d'entree de l'app est `strict()` : envoyer la CLE, meme a
        `None`, fait repondre `400 Unrecognized keys` a une app ancienne et la
        demande n'est pas creee. La mise a jour des bots etant manuelle et
        etalee, ce plugin ne doit pas dependre de l'ordre de deploiement.
        """
        payload = approvals.build_approval_payload("demo", "req-4", "sh", "ls")
        assert "summary" not in payload
        assert "risks" not in payload

        # Vides « en apparence renseignes » : meme traitement.
        vide = approvals.build_approval_payload(
            "demo", "req-5", "sh", "ls", summary="   ", risks=["", "  ", 42]
        )
        assert "summary" not in vide
        assert "risks" not in vide

    def test_champs_renseignes_presents(self):
        payload = approvals.build_approval_payload(
            "demo", "req-6", "sh", "ls", summary="Résumé", risks=["Impact"]
        )
        assert payload["summary"] == "Résumé"
        assert payload["risks"] == ["Impact"]

    def test_resume_et_impacts_transportes(self):
        payload = approvals.build_approval_payload(
            channel_slug="demo",
            request_id="req-3",
            tool="execute_code",
            command="print(1)",
            summary="  Installer la cle publique sur atelier-02.  ",
            risks=["  Acces SSH  ", "", "Ecriture dans ~/.ssh"],
        )
        assert payload["summary"] == "Installer la cle publique sur atelier-02."
        assert payload["risks"] == ["Acces SSH", "Ecriture dans ~/.ssh"]

    def test_resume_vide_vaut_absence(self):
        # `""`, `"   "` et un non-`str` valent absence : l'app affiche « pas de
        # resume », elle ne reserve pas une ligne vide dans la carte.
        assert "summary" not in approvals.build_approval_payload(
            "d", "r", "sh", "ls", summary="   "
        )
        assert "summary" not in approvals.build_approval_payload("d", "r", "sh", "ls", summary=42)


class TestNormalizeRisks:
    def test_ignore_ce_qui_n_est_pas_une_chaine(self):
        assert approvals.normalize_risks(["ok", 42, None, {"a": 1}, "  "]) == ["ok"]

    def test_borne_le_nombre(self):
        many = [f"impact {i}" for i in range(approvals.MAX_RISKS + 5)]
        assert len(approvals.normalize_risks(many)) == approvals.MAX_RISKS

    def test_borne_la_longueur(self):
        [only] = approvals.normalize_risks(["x" * (approvals.MAX_RISK_LENGTH + 50)])
        assert len(only) == approvals.MAX_RISK_LENGTH

    def test_absence_donne_liste_vide(self):
        assert approvals.normalize_risks(None) == []
        assert approvals.normalize_risks([]) == []


def _reply(**approval):
    base = {
        "requestId": "req-1",
        "decision": "once",
        "decidedBy": {"userId": "u1", "userName": "Alice"},
        "decidedAt": "2026-08-06T08:00:00.000Z",
    }
    base.update(approval)
    return {
        "type": "approval.reply",
        "channel": {"slug": "demo", "name": "Demo", "hermesProfile": "default"},
        "approval": base,
    }


class TestParseApprovalReply:
    def test_trame_complete(self):
        parsed = approvals.parse_approval_reply(_reply())
        assert parsed["requestId"] == "req-1"
        assert parsed["decision"] == "once"
        assert parsed["granted"] is True
        assert parsed["decidedBy"] == {"userId": "u1", "userName": "Alice"}
        assert parsed["channelSlug"] == "demo"

    def test_deny_ne_donne_pas_granted(self):
        assert approvals.parse_approval_reply(_reply(decision="deny"))["granted"] is False

    def test_ignore_les_autres_trames(self):
        assert approvals.parse_approval_reply({"type": "message.created"}) is None
        assert approvals.parse_approval_reply("pas un dict") is None
        assert approvals.parse_approval_reply(None) is None

    def test_refuse_une_decision_hors_liste_blanche(self):
        # On ne debloque pas une execution sur un mot qu'on ne sait pas lire.
        assert approvals.parse_approval_reply(_reply(decision="vas-y")) is None
        assert approvals.parse_approval_reply(_reply(decision=None)) is None

    def test_refuse_une_trame_sans_request_id(self):
        assert approvals.parse_approval_reply(_reply(requestId="")) is None
        assert approvals.parse_approval_reply(_reply(requestId=None)) is None

    def test_tolere_un_decidedBy_absent(self):
        parsed = approvals.parse_approval_reply(_reply(decidedBy=None))
        assert parsed["decidedBy"] == {"userId": "", "userName": ""}


class TestIsGranting:
    def test_seul_deny_refuse(self):
        assert approvals.is_granting("once") is True
        assert approvals.is_granting("session") is True
        assert approvals.is_granting("always") is True
        assert approvals.is_granting("deny") is False

    def test_absence_de_decision_n_accorde_rien(self):
        assert approvals.is_granting(None) is False
        assert approvals.is_granting("") is False
