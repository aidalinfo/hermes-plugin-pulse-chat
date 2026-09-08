# -*- coding: utf-8 -*-
"""Routage d'un envoi vers ``pulse_chat:<slug>`` (parseur de cible declare).

Hermes resout la cible d'un envoi en trois temps : parseur declare par le
plugin, regles generiques, annuaire de canaux. Un slug Pulse Chat n'est ni
numerique ni une syntaxe native connue d'Hermes, et l'annuaire ne contient
aucune entree Pulse Chat : sans parseur declare, tout envoi vers
``pulse_chat:<slug>`` echoue sur un « Could not resolve » alors que le canal
existe. C'est ce que corrigeait, avant, un patch du coeur d'Hermes
(``tools/send_message_tool.py``) reapplique a chaque version.

Le second axe teste ici est la TOLERANCE DE VERSION : ``parse_target_ref_fn``
est absent de ``PlatformEntry`` avant Hermes v2026.8.13, et un kwarg inconnu
remonte en ``TypeError``. Sans repli, le plugin ne perdrait pas le routage — il
perdrait l'enregistrement de la plateforme entiere.

Chargement identique a test_adapter_dedup.py.
"""

import sys

from test_adapter_dedup import _load_adapter_module

adapter_module = _load_adapter_module()
parse_target_ref = adapter_module.parse_target_ref


class _Ctx:
    """Hermes >= v2026.8.13 : ``register_platform`` accepte tous les kwargs."""

    def __init__(self):
        self.calls = []

    def register_platform(self, **kwargs):
        self.calls.append(kwargs)
        return object()


class _LegacyCtx(_Ctx):
    """Hermes ancien : tout kwarg inconnu de ``PlatformEntry`` leve TypeError."""

    _KNOWN = {
        "name", "label", "adapter_factory", "check_fn", "validate_config",
        "required_env", "install_hint", "allow_all_env", "max_message_length",
        "emoji", "pii_safe", "platform_hint",
    }

    def register_platform(self, **kwargs):
        unknown = set(kwargs) - self._KNOWN
        if unknown:
            raise TypeError(
                "__init__() got an unexpected keyword argument "
                f"{sorted(unknown)[0]!r}"
            )
        return super().register_platform(**kwargs)


# -- le parseur ------------------------------------------------------------


def test_slug_rendu_tel_quel_sans_fil():
    """Un slug est la cible native : rendu verbatim, jamais de thread_id."""
    assert parse_target_ref("general") == ("general", None)
    assert parse_target_ref("rt-1a2b3c4d") == ("rt-1a2b3c4d", None)


def test_slug_entoure_d_espaces_est_normalise():
    assert parse_target_ref("  general\n") == ("general", None)


def test_slug_non_numerique_est_bien_le_cas_qui_echouait():
    """Les regles generiques d'Hermes ne retiennent qu'un id numerique.

    Ce test borne la RAISON du parseur : si un jour un slug devenait numerique
    il serait resolu sans nous, mais ce n'est pas la forme que l'app produit.
    """
    assert not "general".lstrip("-").isdigit()
    assert not "rt-1a2b3c4d".lstrip("-").isdigit()


def test_cible_vide_rend_none_jamais_une_cible_vide():
    """``("", None)`` ferait poster dans un canal que personne n'a nomme."""
    for empty in ("", "   ", "\n", None):
        assert parse_target_ref(empty) is None


def test_aucun_motif_de_slug_n_est_impose():
    """L'app est la seule autorite sur l'existence d'un canal (404).

    Un motif code ici serait une seconde regle a tenir d'accord avec l'app, et
    une forme de slug ajoutee cote app echouerait ici sans cause designee.
    """
    assert parse_target_ref("forme-inconnue-2027") == ("forme-inconnue-2027", None)


def test_la_forme_rendue_satisfait_la_validation_d_hermes():
    """Hermes VALIDE ce que rend un parseur de plugin, et refuse sans detail.

    Recopie du predicat de ``resolve_send_target`` (tools/send_message_targets):
    tuple de 2, chat_id chaine non vide, thread chaine ou None. Une forme
    invalide ne rend pas une erreur qui designe le plugin — seulement
    « returned an invalid result ».
    """
    for ref in ("general", "rt-1a2b3c4d", "  general  "):
        parsed = parse_target_ref(ref)
        assert isinstance(parsed, tuple) and len(parsed) == 2
        assert isinstance(parsed[0], str) and parsed[0]
        assert parsed[1] is None or isinstance(parsed[1], str)


# -- l'enregistrement ------------------------------------------------------


def test_le_parseur_est_declare_a_hermes():
    ctx = _Ctx()
    adapter_module.register(ctx)
    assert len(ctx.calls) == 1
    assert ctx.calls[0]["parse_target_ref_fn"] is parse_target_ref
    assert ctx.calls[0]["name"] == "pulse_chat"


def test_hermes_ancien_enregistre_quand_meme_la_plateforme():
    """Le repli garde Pulse Chat : sans lui, le bot perdrait la plateforme."""
    adapter_module._WARNED_ONCE.discard("parse_target_ref_kwarg")
    ctx = _LegacyCtx()
    adapter_module.register(ctx)
    assert len(ctx.calls) == 1, "l'enregistrement de repli n'a pas eu lieu"
    assert "parse_target_ref_fn" not in ctx.calls[0]
    assert ctx.calls[0]["name"] == "pulse_chat"


def test_le_repli_est_journalise(caplog):
    """Une degradation muette de routage se chercherait cote Hermes pendant des heures."""
    adapter_module._WARNED_ONCE.discard("parse_target_ref_kwarg")
    with caplog.at_level("WARNING"):
        adapter_module.register(_LegacyCtx())
    assert any("parse_target_ref_fn" in r.getMessage() for r in caplog.records)


def test_le_repli_ne_perd_aucun_autre_reglage():
    """Le repli retire le parseur, et RIEN d'autre."""
    modern, legacy = _Ctx(), _LegacyCtx()
    adapter_module.register(modern)
    adapter_module.register(legacy)
    expected = {k: v for k, v in modern.calls[0].items()
                if k != "parse_target_ref_fn"}
    assert set(legacy.calls[0]) == set(expected)
    for key in ("label", "allow_all_env", "max_message_length", "platform_hint",
                "required_env", "install_hint", "emoji", "pii_safe"):
        assert legacy.calls[0][key] == expected[key]


def test_le_module_est_bien_celui_charge_par_les_autres_tests():
    """Garde-fou du harnais : deux modules charges = un test vert pour rien."""
    assert adapter_module is sys.modules[adapter_module.__name__]
