# -*- coding: utf-8 -*-
"""Modules purs artifacts.py et vault.py (sans hermes installe).

Le controle de chemin qui FAIT foi est celui du serveur
(app/pulse-chat/shared/vaultPath.ts) ; celui teste ici est un garde-fou de
confort cote plugin. Les deux doivent refuser les memes formes — d'ou des cas
volontairement identiques des deux cotes.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load(module_name):
    pkg_name = "pulse_chat_pure_under_test"
    if pkg_name not in sys.modules:
        package = types.ModuleType(pkg_name)
        package.__path__ = [str(_PLUGIN_DIR)]
        sys.modules[pkg_name] = package
    full = "%s.%s" % (pkg_name, module_name)
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _PLUGIN_DIR / ("%s.py" % module_name))
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


artifacts = _load("artifacts")
vault = _load("vault")
approvals_mod = _load("approvals")


class TestArtifactPayload:
    def test_forme_du_payload(self):
        payload = artifacts.build_artifact_payload(
            channel_slug="demo",
            artifact_id="art-1",
            kind="mermaid",
            path="artifacts/archi.mmd",
            title="  Archi   réseau  ",
        )
        assert payload == {
            "channelSlug": "demo",
            "kind": "artifact",
            "artifactId": "art-1",
            "artifactKind": "mermaid",
            "title": "Archi réseau",
            "path": "artifacts/archi.mmd",
        }

    def test_le_contenu_ne_transite_jamais(self):
        # Source unique de vérité : le contenu vit dans le coffre, le payload
        # ne porte qu'un pointeur.
        payload = artifacts.build_artifact_payload("demo", "a", "html", "artifacts/x.html")
        assert "content" not in payload

    def test_titre_vide_devient_none(self):
        payload = artifacts.build_artifact_payload("demo", "a", "markdown", "# Titre", "   ")
        assert payload["title"] is None

    def test_titre_borne(self):
        payload = artifacts.build_artifact_payload("demo", "a", "markdown", "x", "T" * 300)
        assert len(payload["title"]) == artifacts.MAX_ARTIFACT_TITLE_LENGTH

    def test_refuse_un_type_inconnu(self):
        with pytest.raises(ValueError):
            artifacts.build_artifact_payload("demo", "a", "powerpoint", "x")

    def test_refuse_un_chemin_vide(self):
        for bad in ("", "   ", None):
            with pytest.raises(ValueError):
                artifacts.build_artifact_payload("demo", "a", "html", bad)

    def test_chemin_par_defaut_derive_du_titre(self):
        # Un humain qui parcourt le coffre doit reconnaître ce qu'il voit.
        # Les accents sont CONSERVÉS (isalnum les accepte) : S3 gère l'UTF-8 et
        # un nom lisible vaut mieux qu'un nom translittéré.
        assert (
            artifacts.default_artifact_path("mermaid", "Archi réseau", "art-1")
            == 'artifacts/archi-réseau.mmd'
        )
        assert artifacts.default_artifact_path("markdown", None, "art-9") == 'artifacts/art-9.md'
        # Republier le même titre vise le MÊME fichier.
        assert artifacts.default_artifact_path("html", "Bilan", "x") == artifacts.default_artifact_path(
            "html", "  bilan  ", "y"
        )

    def test_exige_un_artifact_id(self):
        with pytest.raises(ValueError):
            artifacts.build_artifact_payload("demo", "", "html", "<p>x</p>")

    def test_tous_les_types_annonces_passent(self):
        for kind in artifacts.ARTIFACT_KINDS:
            payload = artifacts.build_artifact_payload("demo", "a", kind, "contenu")
            assert payload["artifactKind"] == kind


class TestDefaultArtifactId:
    def test_stable_pour_un_meme_titre(self):
        a = artifacts.default_artifact_id("mermaid", "Architecture reseau")
        b = artifacts.default_artifact_id("mermaid", "Architecture reseau")
        assert a == b and a.startswith("art-mermaid-")

    def test_insensible_a_la_casse_et_aux_espaces(self):
        a = artifacts.default_artifact_id("markdown", "  Mon  Doc ")
        b = artifacts.default_artifact_id("markdown", "mon doc")
        assert a == b

    def test_distingue_titres_et_types(self):
        assert artifacts.default_artifact_id("mermaid", "A") != artifacts.default_artifact_id(
            "mermaid", "B"
        )
        assert artifacts.default_artifact_id("mermaid", "A") != artifacts.default_artifact_id(
            "markdown", "A"
        )

    def test_none_sans_titre(self):
        # Pas de titre = pas d'identite stable : l'appelant genere un id unique.
        for empty in (None, "", "   ", 42):
            assert artifacts.default_artifact_id("html", empty) is None


class TestRefusal:
    def test_toujours_refusant(self):
        r = approvals_mod.refusal("req-1", "timeout")
        assert r["granted"] is False
        assert r["decision"] is None
        assert r["status"] == "timeout"

    def test_statut_inconnu_retombe_sur_not_sent(self):
        assert approvals_mod.refusal("req-1", "n'importe quoi")["status"] == "not_sent"

    def test_meme_forme_qu_une_decision_reelle(self):
        decided = approvals_mod.parse_approval_reply(
            {
                "type": "approval.reply",
                "channel": {"slug": "demo"},
                "approval": {
                    "requestId": "req-1",
                    "decision": "once",
                    "decidedBy": {"userId": "u", "userName": "n"},
                    "decidedAt": "2026-08-06T08:00:00.000Z",
                },
            }
        )
        assert set(decided) == set(approvals_mod.refusal("req-1", "timeout"))
        assert decided["status"] == "decided"


class TestNormalizeVaultPath:
    def test_chemin_simple(self):
        assert vault.normalize_vault_path("rapport.md") == "rapport.md"
        assert vault.normalize_vault_path("out/rapport.md") == "out/rapport.md"

    def test_compacte_les_separateurs(self):
        assert vault.normalize_vault_path("a//b///c.txt") == "a/b/c.txt"

    def test_antislash_traite_comme_separateur(self):
        # Un agent sous Windows ne doit pas contourner la detection de « .. ».
        assert vault.normalize_vault_path("a\\b.txt") == "a/b.txt"
        with pytest.raises(vault.VaultPathError):
            vault.normalize_vault_path("..\\secret.txt")

    def test_refuse_la_remontee(self):
        for bad in ("../secret", "a/../../b", "..", "a/.."):
            with pytest.raises(vault.VaultPathError):
                vault.normalize_vault_path(bad)

    def test_refuse_les_chemins_absolus(self):
        for bad in ("/etc/passwd", "C:/Windows", "//serveur/partage"):
            with pytest.raises(vault.VaultPathError):
                vault.normalize_vault_path(bad)

    def test_refuse_vide_et_types_invalides(self):
        for bad in ("", "   ", None, 42, "/"):
            with pytest.raises(vault.VaultPathError):
                vault.normalize_vault_path(bad)

    def test_refuse_les_caracteres_de_controle(self):
        with pytest.raises(vault.VaultPathError):
            vault.normalize_vault_path("a\x00b")
        with pytest.raises(vault.VaultPathError):
            vault.normalize_vault_path("a\nb")

    def test_refuse_les_longueurs_excessives(self):
        with pytest.raises(vault.VaultPathError):
            vault.normalize_vault_path("x" * (vault.MAX_VAULT_PATH_LENGTH + 1))
        with pytest.raises(vault.VaultPathError):
            vault.normalize_vault_path("x" * (vault.MAX_VAULT_SEGMENT_LENGTH + 1))

    def test_refuse_les_espaces_en_bordure(self):
        with pytest.raises(vault.VaultPathError):
            vault.normalize_vault_path(" fichier.txt")


class TestVaultUrl:
    def test_url_de_listing(self):
        assert (
            vault.vault_url("http://app.test", "demo")
            == "http://app.test/api/agent/vault/demo"
        )

    def test_url_de_fichier(self):
        assert (
            vault.vault_url("http://app.test/", "demo", "out/rapport.md")
            == "http://app.test/api/agent/vault/demo/out/rapport.md"
        )

    def test_encode_le_slug_et_les_espaces(self):
        url = vault.vault_url("http://app.test", "client a", "mon rapport.md")
        assert "client%20a" in url
        assert "mon%20rapport.md" in url

    def test_les_separateurs_restent_des_separateurs(self):
        url = vault.vault_url("http://app.test", "demo", "a/b/c.txt")
        assert url.endswith("/a/b/c.txt")

    def test_propage_le_refus_de_chemin(self):
        with pytest.raises(vault.VaultPathError):
            vault.vault_url("http://app.test", "demo", "../secret")
