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
