# -*- coding: utf-8 -*-
"""Fournisseur de navigateur ``pulse`` et outil ``pulse_browser_handoff``.

L'app est simulee par un ``unittest.mock`` sur la fonction HTTP du fournisseur.
Chaque point verifie ici est un mode d'echec MUET dans Hermes :

  - une exception dans ``create_session`` fait naviguer l'agent EN LOCAL sans
    rien dire : elle doit porter le motif de l'app, et ne partir qu'a bon
    escient (pas de canal, autre plateforme, fenetre du 501) ;
  - ``is_available`` est aussi le ``check_fn`` des outils de navigation : un
    appel reseau y bloquerait la construction du schema, un faux y RETIRERAIT
    les outils ;
  - ``close_session`` tourne sur le fil du concierge : une exception y
    arreterait le nettoyage des autres sessions ;
  - l'outil attend une trame qui arrive d'un AUTRE fil.

Style du depot : pas de pytest-asyncio, des fils et des ``Future`` reels.
"""

import asyncio
import importlib.util
import json
import pathlib
import sys
import threading
import time
from unittest import mock

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


browser = _load("browser")

SESSIONS = "/api/agent/browser/sessions"


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _opened(session_id="s1", cdp="wss://chat.test/ws/browser-cdp/jeton"):
    return (
        200,
        {"sessionId": session_id, "cdpUrl": cdp, "profile": {"kind": "personal", "label": "Killian"}},
    )


def _provider(http=None, env=None, configured=True, wait_seconds=5.0, clock=None):
    env = {"HERMES_SESSION_CHAT_ID": "demo", "HERMES_SESSION_PLATFORM": "pulse_chat"} if env is None else env
    http = http if http is not None else mock.Mock(return_value=_opened())
    provider = browser.PulseBrowserProvider(
        http,
        configured=configured if callable(configured) else (lambda: configured),
        session_env=lambda name: env.get(name, ""),
        clock=clock or _Clock(),
        wait_seconds=wait_seconds,
    )
    return provider, http, env


# ── Identite ─────────────────────────────────────────────────────────────


class TestNom:
    def test_le_fournisseur_s_appelle_pulse_jamais_browser_use(self):
        """Browser Use SAUTE le fournisseur nomme exactement ``browser-use`` :
        ``browser_exec`` n'emprunterait alors plus le navigateur Pulse."""
        provider, _, _ = _provider()
        assert provider.name == "pulse"
        assert provider.name != "browser-use"

    def test_l_outil_est_prefixe(self):
        assert browser.HANDOFF_TOOL_NAME == "pulse_browser_handoff"
        assert browser.HANDOFF_TOOL_SCHEMA["name"] == browser.HANDOFF_TOOL_NAME
        assert browser.HANDOFF_TOOL_SCHEMA["parameters"]["required"] == ["reason"]

    def test_l_attente_reste_sous_le_plafond_d_hermes(self):
        assert browser.HANDOFF_WAIT_SECONDS == 270 < 300


# ── create_session ─────────────────────────────────────────────────────────


class TestCreateSession:
    def test_poste_le_canal_courant_et_rend_le_contrat_d_hermes(self):
        provider, http, _ = _provider()

        info = provider.create_session("t1")

        method, path, payload, _timeout = http.call_args.args
        assert (method, path, payload) == ("POST", SESSIONS, {"channel": "demo"})
        assert info["bb_session_id"] == "s1"
        assert info["cdp_url"] == "wss://chat.test/ws/browser-cdp/jeton"
        assert info["session_name"].startswith("pulse_")
        assert isinstance(info["features"], dict)

    def test_aucune_echeance_n_est_annoncee(self):
        """Une ``expires_at`` inventee ferait remplacer par Hermes une session
        encore vivante — l'app ferme a l'inactivite, pas a heure fixe."""
        provider, _, _ = _provider()
        assert "expires_at" not in provider.create_session("t1")

    def test_session_name_unique_par_ouverture(self):
        """La session chaude est reprise par un demon agent-browser NEUF."""
        provider, _, _ = _provider()
        assert provider.create_session("t1")["session_name"] != provider.create_session("t2")["session_name"]

    def test_sans_canal_leve_sans_appeler_l_app(self):
        provider, http, _ = _provider(env={"HERMES_SESSION_PLATFORM": "pulse_chat"})
        with pytest.raises(RuntimeError, match="aucune conversation"):
            provider.create_session("t1")
        http.assert_not_called()

    def test_une_autre_plateforme_ne_poste_rien(self):
        """Un bot Telegram + Pulse : un identifiant de chat Telegram n'est pas
        un slug, et le poster ne ferait qu'un 404 de plus."""
        provider, http, _ = _provider(
            env={"HERMES_SESSION_CHAT_ID": "123456", "HERMES_SESSION_PLATFORM": "telegram"}
        )
        with pytest.raises(RuntimeError, match="telegram"):
            provider.create_session("t1")
        http.assert_not_called()

    def test_plateforme_absente_tolere(self):
        """Hors passerelle (CLI), la plateforme n'est pas posee : le canal decide."""
        provider, http, _ = _provider(env={"HERMES_SESSION_CHAT_ID": "demo"})
        provider.create_session("t1")
        http.assert_called_once()

    @pytest.mark.parametrize(
        "status,code",
        [(503, "browser_capacity"), (403, "agent_credential_required"), (404, "channel_not_found")],
    )
    def test_un_refus_leve_avec_le_motif_de_l_app(self, status, code):
        """Hermes journalise le message puis navigue en local : c'est la SEULE
        trace de « l'agent a navigue sans mon profil »."""
        body = {"statusMessage": "Plus de navigateur libre", "data": {"code": code}}
        provider, _, _ = _provider(http=mock.Mock(return_value=(status, body)))
        with pytest.raises(RuntimeError) as err:
            provider.create_session("t1")
        assert code in str(err.value)
        assert "Plus de navigateur libre" in str(err.value)

    def test_app_injoignable(self):
        provider, _, _ = _provider(http=mock.Mock(return_value=(0, {"message": "connexion refusee"})))
        with pytest.raises(RuntimeError, match="not_connected"):
            provider.create_session("t1")

    def test_reponse_sans_url_cdp_websocket(self):
        provider, _, _ = _provider(http=mock.Mock(return_value=(200, {"sessionId": "s1", "cdpUrl": "http://x"})))
        with pytest.raises(RuntimeError, match="CDP"):
            provider.create_session("t1")


# ── 501 et is_available ────────────────────────────────────────────────────


_UNAVAILABLE = (501, {"statusMessage": "Navigateur non configure", "data": {"code": "browser_unavailable"}})


class TestIndisponible:
    def test_501_leve_puis_plus_aucun_appel_pendant_5_min(self):
        clock = _Clock()
        provider, http, _ = _provider(http=mock.Mock(return_value=_UNAVAILABLE), clock=clock)

        with pytest.raises(RuntimeError, match="browser_unavailable"):
            provider.create_session("t1")
        assert http.call_count == 1

        clock.now += 299
        with pytest.raises(RuntimeError, match="browser_unavailable"):
            provider.create_session("t2")
        assert http.call_count == 1, "la fenetre du 501 ne doit rien redemander a l'app"

        clock.now += 2
        http.return_value = _opened()
        assert provider.create_session("t3")["bb_session_id"] == "s1"
        assert http.call_count == 2

    def test_is_available_reste_vrai_apres_un_501(self):
        """ECART DELIBERE a la lettre de la spec (§ 10) : en mode cloud,
        ``is_available`` est le ``check_fn`` des outils ``browser_*``
        (``check_browser_requirements``). Faux, Hermes les RETIRE du schema au
        lieu de naviguer en local : l'agent perdrait la navigation entiere. Le
        repli local s'obtient par l'echec de ``create_session``, ci-dessus."""
        provider, _, _ = _provider(http=mock.Mock(return_value=_UNAVAILABLE))
        with pytest.raises(RuntimeError):
            provider.create_session("t1")
        assert provider.is_available() is True

    def test_is_available_n_appelle_jamais_le_reseau(self):
        provider, http, _ = _provider()
        for _ in range(3):
            provider.is_available()
        http.assert_not_called()

    def test_is_available_suit_la_configuration(self):
        assert _provider(configured=False)[0].is_available() is False
        assert _provider(configured=True)[0].is_available() is True

    def test_is_available_ne_leve_pas(self):
        def boom():
            raise RuntimeError("secret illisible")

        assert _provider(configured=boom)[0].is_available() is False


# ── close_session / emergency_cleanup ───────────────────────────────────────


class TestLiberation:
    def test_close_session_libere_par_delete(self):
        provider, http, _ = _provider(http=mock.Mock(return_value=(200, {"ok": True})))
        assert provider.close_session("s1") is True
        method, path, payload, _timeout = http.call_args.args
        assert (method, path, payload) == ("DELETE", f"{SESSIONS}/s1", None)

    def test_deja_fermee_vaut_succes(self):
        provider, _, _ = _provider(http=mock.Mock(return_value=(404, None)))
        assert provider.close_session("s1") is True

    def test_echec_rend_faux(self):
        provider, _, _ = _provider(http=mock.Mock(return_value=(500, None)))
        assert provider.close_session("s1") is False

    def test_close_session_ne_leve_jamais(self):
        provider, _, _ = _provider(http=mock.Mock(side_effect=OSError("reseau coupe")))
        assert provider.close_session("s1") is False

    def test_emergency_cleanup_ne_leve_jamais(self):
        provider, http, _ = _provider(http=mock.Mock(side_effect=OSError("reseau coupe")))
        assert provider.emergency_cleanup("s1") is None
        assert http.call_args.args[0] == "DELETE"

    def test_identifiant_encode_dans_le_chemin(self):
        provider, http, _ = _provider(http=mock.Mock(return_value=(200, None)))
        provider.close_session("a/b")
        assert http.call_args.args[1] == f"{SESSIONS}/a%2Fb"

    def test_fonctionne_sans_contexte_de_session(self):
        """Le concierge n'a AUCUN contexte : tout tient dans l'identifiant."""
        provider, http, _ = _provider(http=mock.Mock(return_value=(200, None)), env={})
        assert provider.close_session("s1") is True


# ── pulse_browser_handoff ─────────────────────────────────────────────────


def _handoff_http(handoff_response=(200, {"ok": True})):
    posted = threading.Event()

    def http(method, path, payload, timeout):
        if method == "POST" and path == SESSIONS:
            return _opened()
        if path.endswith("/handoff"):
            posted.set()
            return handoff_response
        return (200, None)

    return mock.Mock(side_effect=http), posted


def _run_in_thread(fn, *args, **kwargs):
    box = {}

    def target():
        box["result"] = fn(*args, **kwargs)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


class TestHandoff:
    def test_poste_puis_rend_done_quand_la_main_revient(self):
        http, posted = _handoff_http()
        provider, _, _ = _provider(http=http)
        provider.create_session("t1")

        thread, box = _run_in_thread(provider.handoff, {"reason": "Connecte-toi a LinkedIn"}, task_id="t1")
        assert posted.wait(2)
        call = [c for c in http.call_args_list if c.args[1].endswith("/handoff")][0]
        assert call.args[:3] == ("POST", f"{SESSIONS}/s1/handoff", {"reason": "Connecte-toi a LinkedIn"})

        assert provider.on_control({"type": "browser.control", "sessionId": "s1", "controller": "agent", "by": None})
        thread.join(2)
        assert json.loads(box["result"])["status"] == "done"

    def test_pending_apres_le_delai(self):
        http, _ = _handoff_http()
        provider, _, _ = _provider(http=http, wait_seconds=0.05)
        provider.create_session("t1")

        started = time.monotonic()
        result = json.loads(provider.handoff({"reason": "Saisis le code SMS"}, task_id="t1"))
        assert result["status"] == "pending"
        assert "ARRETE-TOI" in result["next"]
        assert time.monotonic() - started < 2

    def test_sans_navigateur_ouvert_message_explicite(self):
        provider, http, _ = _provider()
        result = json.loads(provider.handoff({"reason": "Connecte-toi"}, task_id="t1"))
        assert result["status"] == "refused"
        assert result["code"] == "no_browser_session"
        assert "ouvre d'abord le navigateur" in result["message"]
        http.assert_not_called()

    def test_apres_liberation_plus_de_navigateur(self):
        http, _ = _handoff_http()
        provider, _, _ = _provider(http=http)
        provider.create_session("t1")
        provider.close_session("s1")
        assert json.loads(provider.handoff({"reason": "x"}, task_id="t1"))["code"] == "no_browser_session"

    def test_reprise_chaude_puis_liberation_tardive_du_tour_precedent(self):
        """Le tour 2 reprend la MEME session ; la liberation du tour 1 arrive
        apres, sur le fil du concierge. Elle ne doit pas effacer le tour 2."""
        http, posted = _handoff_http()
        provider, _, env = _provider(http=http, wait_seconds=0.05)
        provider.create_session("t1")
        provider.create_session("t2")
        provider.close_session("s1")  # liberation tardive du tour 1
        env.pop("HERMES_SESSION_CHAT_ID")  # interdit le repli par canal
        result = json.loads(provider.handoff({"reason": "x"}, task_id="t2"))
        assert result["status"] == "pending"
        assert posted.is_set()

    def test_meme_cle_reouverte_puis_liberation_tardive(self):
        """Hermes recree la session de la MEME tache et l'app rend la session
        chaude : la liberation de la premiere ouverture ne doit pas effacer
        la cle vivante."""
        http, posted = _handoff_http()
        provider, _, env = _provider(http=http, wait_seconds=0.05)
        provider.create_session("t1")
        provider.create_session("t1")
        provider.close_session("s1")
        env.pop("HERMES_SESSION_CHAT_ID")  # interdit le repli par canal
        result = json.loads(provider.handoff({"reason": "x"}, task_id="t1"))
        assert result["status"] == "pending"
        assert posted.is_set()

    def test_meme_cle_nouvelle_session_decompte_l_ancienne(self):
        """Meme tache, session DIFFERENTE : l'ancienne n'est plus tenue par
        cette cle, sa liberation la fait disparaitre."""
        provider, _, _ = _provider(http=mock.Mock(side_effect=[_opened("s-a"), _opened("s-b")]))
        provider.create_session("t1")
        provider.create_session("t1")
        assert "s-a" not in provider._open
        assert provider._open["s-b"] == 1

    def test_cle_suffixee_de_browser_use_retrouvee_par_la_tache(self):
        """``browser_exec`` ouvre sous ``<tache>@<profil servi>``."""
        http, _ = _handoff_http()
        provider, _, env = _provider(http=http, wait_seconds=0.05)
        provider.create_session("t1@support")
        env.pop("HERMES_SESSION_CHAT_ID")
        assert json.loads(provider.handoff({"reason": "x"}, task_id="t1"))["status"] == "pending"

    def test_session_nommee_retrouvee_par_le_canal(self):
        """``bu-named-<nom>`` ne contient pas la tache : le canal departage."""
        http, _ = _handoff_http()
        provider, _, _ = _provider(http=http, wait_seconds=0.05)
        provider.create_session("bu-named-linkedin")
        assert json.loads(provider.handoff({"reason": "x"}, task_id="t9"))["status"] == "pending"

    def test_session_fermee_cote_app(self):
        http, _ = _handoff_http(handoff_response=(404, {"statusMessage": "Session introuvable"}))
        provider, _, _ = _provider(http=http)
        provider.create_session("t1")
        result = json.loads(provider.handoff({"reason": "x"}, task_id="t1"))
        assert result["status"] == "refused"
        assert result["code"] == "browser_session_not_found"

    def test_motif_obligatoire(self):
        provider, http, _ = _provider()
        provider.create_session("t1")
        assert json.loads(provider.handoff({"reason": "  "}, task_id="t1"))["code"] == "no_reason"
        assert http.call_count == 1  # seulement l'ouverture

    def test_motif_borne(self):
        http, posted = _handoff_http()
        provider, _, _ = _provider(http=http, wait_seconds=0.05)
        provider.create_session("t1")
        provider.handoff({"reason": "x" * 2000}, task_id="t1")
        call = [c for c in http.call_args_list if c.args[1].endswith("/handoff")][0]
        assert len(call.args[2]["reason"]) == browser.MAX_REASON_LENGTH

    def test_ne_leve_jamais(self):
        provider, _, _ = _provider(http=mock.Mock(side_effect=[_opened(), OSError("coupe")]))
        provider.create_session("t1")
        assert json.loads(provider.handoff({"reason": "x"}, task_id="t1"))["status"] == "refused"

    def test_la_prise_de_main_seule_ne_reveille_pas(self):
        http, posted = _handoff_http()
        provider, _, _ = _provider(http=http, wait_seconds=0.3)
        provider.create_session("t1")
        thread, box = _run_in_thread(provider.handoff, {"reason": "x"}, task_id="t1")
        assert posted.wait(2)
        assert not provider.on_control(
            {"type": "browser.control", "sessionId": "s1", "controller": "human", "by": "Killian"}
        )
        thread.join(2)
        assert json.loads(box["result"])["status"] == "pending"

    def test_done_nomme_qui_avait_la_main(self):
        http, posted = _handoff_http()
        provider, _, _ = _provider(http=http)
        provider.create_session("t1")
        thread, box = _run_in_thread(provider.handoff, {"reason": "x"}, task_id="t1")
        assert posted.wait(2)
        provider.on_control({"type": "browser.control", "sessionId": "s1", "controller": "human", "by": "Killian"})
        provider.on_control({"type": "browser.control", "sessionId": "s1", "controller": "agent", "by": None})
        thread.join(2)
        assert json.loads(box["result"]) ["by"] == "Killian"

    def test_une_autre_session_ne_reveille_pas(self):
        http, posted = _handoff_http()
        provider, _, _ = _provider(http=http, wait_seconds=0.3)
        provider.create_session("t1")
        thread, box = _run_in_thread(provider.handoff, {"reason": "x"}, task_id="t1")
        assert posted.wait(2)
        assert not provider.on_control({"type": "browser.control", "sessionId": "s2", "controller": "agent"})
        thread.join(2)
        assert json.loads(box["result"])["status"] == "pending"


class TestTrame:
    @pytest.mark.parametrize(
        "frame",
        [
            None,
            {"type": "gate.reply"},
            {"type": "browser.control", "controller": "agent"},
            {"type": "browser.control", "sessionId": "s1", "controller": "robot"},
            {"type": "browser.control", "sessionId": "", "controller": "agent"},
        ],
    )
    def test_trame_inexploitable_ignoree(self, frame):
        assert browser.parse_control_frame(frame) is None
        provider, _, _ = _provider()
        assert provider.on_control(frame) is False

    def test_point_d_entree_module_sans_fournisseur(self):
        browser.deactivate()
        assert browser.on_control({"type": "browser.control", "sessionId": "s1", "controller": "agent"}) is False

    def test_la_trame_atteint_chaque_fournisseur_inscrit(self):
        """Deux instances du plugin (portee par profil) : la trame d'une
        session ouverte par la SECONDE doit la reveiller, pas se perdre dans
        la premiere."""
        browser.deactivate()
        first, _, _ = _provider(http=mock.Mock(return_value=_opened("s-a")))
        http, posted = _handoff_http()
        second, _, _ = _provider(http=http)
        first.create_session("t0")
        second.create_session("t1")
        browser.activate(first)
        browser.activate(second)
        try:
            thread, box = _run_in_thread(second.handoff, {"reason": "x"}, task_id="t1")
            assert posted.wait(2)
            assert browser.on_control(
                {"type": "browser.control", "sessionId": "s1", "controller": "agent", "by": None}
            )
            thread.join(2)
            assert json.loads(box["result"])["status"] == "done"
        finally:
            browser.deactivate()

    def test_les_prises_de_main_retenues_sont_bornees(self):
        provider, _, _ = _provider()
        for index in range(browser.MAX_TRACKED_SESSIONS + 10):
            provider.on_control(
                {"type": "browser.control", "sessionId": f"s{index}", "controller": "human", "by": "K"}
            )
        assert len(provider._last_human) == browser.MAX_TRACKED_SESSIONS


# ── Rendu de main : relancer l'agent qui n'attend plus ───────────────────


def _rendu(event="released", turn_ended=False, slug="compta", name="Compta", by="Killian", session="s1"):
    frame = {"type": "browser.control", "sessionId": session, "controller": "agent", "by": by}
    if event is not None:
        frame.update(event=event, turnEnded=turn_ended, channelSlug=slug, channelName=name)
    return frame


class TestTrameDeRendu:
    def test_les_nouveaux_champs_sont_gardes(self):
        control = browser.parse_control_frame(_rendu("auto_released", True, "compta", "Compta", None))
        assert control == {
            "sessionId": "s1",
            "controller": "agent",
            "by": None,
            "event": "auto_released",
            "turnEnded": True,
            "channelSlug": "compta",
            "channelName": "Compta",
        }

    def test_une_app_ancienne_ne_les_envoie_pas(self):
        control = browser.parse_control_frame(_rendu(event=None))
        assert control["event"] is None
        assert control["turnEnded"] is False
        assert control["channelSlug"] is None
        assert control["channelName"] is None

    @pytest.mark.parametrize(
        "field, value, expected",
        [
            ("event", "robot", None),
            ("event", "RELEASED", None),
            ("event", 1, None),
            ("turnEnded", "true", False),
            ("turnEnded", 1, False),
            ("turnEnded", None, False),
            ("channelSlug", "", None),
            ("channelSlug", "   ", None),
            ("channelSlug", 42, None),
            ("channelName", "", None),
            ("channelName", ["Compta"], None),
        ],
    )
    def test_une_valeur_invalide_est_ignoree_jamais_devinee(self, field, value, expected):
        frame = _rendu("released", True)
        frame[field] = value
        control = browser.parse_control_frame(frame)
        assert control is not None, "un champ optionnel invalide ne rejette pas la trame"
        assert control[field] == expected


class TestDecisionDeRelance:
    def _control(self, **overrides):
        control = browser.parse_control_frame(_rendu("released", True))
        control.update(overrides)
        return control

    def test_une_prise_de_main_ne_relance_pas(self):
        assert not browser.handback_notice_needed(self._control(controller="human"), False, True)

    def test_un_outil_reveille_suffit(self):
        assert not browser.handback_notice_needed(self._control(), True, True)

    def test_app_ancienne_sans_event(self):
        assert not browser.handback_notice_needed(self._control(event=None), False, True)

    def test_session_fermee(self):
        assert not browser.handback_notice_needed(self._control(event="closed"), False, True)

    def test_tour_fini(self):
        assert browser.handback_notice_needed(self._control(turnEnded=True), False, False)

    def test_tour_en_cours_sans_attente_n_est_pas_interrompu(self):
        assert not browser.handback_notice_needed(self._control(turnEnded=False), False, False)

    def test_tour_en_cours_mais_handoff_en_pending(self):
        """La fenetre entre le ``pending`` et la fin du tour : l'agent s'est
        arrete, mais Hermes n'a pas encore libere la session."""
        assert browser.handback_notice_needed(self._control(turnEnded=False), False, True)

    def test_rendu_d_office_tour_fini(self):
        assert browser.handback_notice_needed(self._control(event="auto_released"), False, False)

    def test_sans_canal(self):
        assert not browser.handback_notice_needed(self._control(channelSlug=None), False, True)


class TestTexteDeRelance:
    def test_rendu_par_un_humain(self):
        text = browser.handback_message_text("released", "Killian")
        assert text.startswith("[Navigateur] Killian t'a rendu la main sur ton navigateur.")
        assert "capture ou instantane" in text

    def test_rendu_par_quelqu_un_d_inconnu(self):
        assert browser.handback_message_text("released", None).startswith(
            "[Navigateur] Un humain t'a rendu la main"
        )

    def test_rendu_d_office(self):
        """« D'office » couvre DEUX causes cote app — 5 min sans entree, ou la
        personne a perdu l'acces au canal : le texte ne doit pas en affirmer une."""
        text = browser.handback_message_text("auto_released", None)
        assert text.startswith("[Navigateur] La main t'a ete rendue d'office")
        assert "n'a plus acces" in text
        assert "sans redemander la main en boucle" in text

    def test_rien_en_cours_rien_a_faire(self):
        """Une prise de main « pour regarder » ne doit pas faire reprendre une
        tache deja close."""
        for event in ("released", "auto_released"):
            assert "Si tu n'avais rien en cours, ne fais rien" in browser.handback_message_text(event, "Killian")


@pytest.fixture
def _inscrit():
    """Inscrit des fournisseurs au point d'entree du module, desinscrits apres."""
    browser.deactivate()

    def inscrire(provider):
        browser.activate(provider)
        return provider

    yield inscrire
    browser.deactivate()


class TestHandleControl:
    def _pending(self, provider, task_id="t1"):
        provider.create_session(task_id)
        result = json.loads(provider.handoff({"reason": "Connecte-toi"}, task_id=task_id))
        assert result["status"] == "pending"

    def test_la_consigne_pending_annonce_la_relance(self):
        advice = json.loads(browser.pending_result())["next"]
        assert "ARRETE-TOI" in advice
        assert "[navigateur]" in advice.lower()
        # Ne JAMAIS interdire de demander a l'humain de prevenir : face a une
        # app qui n'envoie pas encore la relance, ce serait le seul reveil.
        assert "ne lui demande pas" not in advice.lower()

    def test_tour_fini_rend_un_avis(self, _inscrit):
        _inscrit(_provider()[0])
        notice = browser.handle_control(_rendu("released", True, by="Killian"))
        assert notice["channelSlug"] == "compta"
        assert notice["channelName"] == "Compta"
        assert notice["by"] == "Killian"
        assert notice["sessionId"] == "s1"
        assert notice["text"].startswith("[Navigateur] Killian t'a rendu la main")

    def test_sans_fournisseur_un_tour_fini_rend_quand_meme_un_avis(self, _inscrit):
        """Bot redemarre : plus rien ne connait la session, l'agent doit
        pourtant reprendre."""
        assert browser.handle_control(_rendu("released", True)) is not None

    def test_nomme_l_humain_qui_avait_pris_la_main(self, _inscrit):
        _inscrit(_provider()[0])
        browser.handle_control({"type": "browser.control", "sessionId": "s1", "controller": "human", "by": "Anais"})
        notice = browser.handle_control(_rendu("released", True, by="Killian"))
        assert notice["by"] == "Anais"
        assert "Anais t'a rendu la main" in notice["text"]

    def test_une_prise_de_main_ne_rend_rien(self, _inscrit):
        _inscrit(_provider()[0])
        frame = {"type": "browser.control", "sessionId": "s1", "controller": "human", "by": "Anais"}
        assert browser.handle_control(frame) is None

    def test_un_outil_en_attente_est_reveille_sans_avis(self, _inscrit):
        http, posted = _handoff_http()
        provider = _inscrit(_provider(http=http)[0])
        provider.create_session("t1")
        thread, box = _run_in_thread(provider.handoff, {"reason": "x"}, task_id="t1")
        assert posted.wait(2)
        assert browser.handle_control(_rendu("released", True)) is None
        thread.join(2)
        assert json.loads(box["result"])["status"] == "done"

    def test_pending_memorise_puis_consomme(self, _inscrit):
        http, _ = _handoff_http()
        provider = _inscrit(_provider(http=http, wait_seconds=0.05)[0])
        self._pending(provider)
        assert "s1" in provider._handoff_pending
        # Le tour tourne encore (turnEnded faux) : le pending suffit a relancer.
        assert browser.handle_control(_rendu("released", False)) is not None
        assert "s1" not in provider._handoff_pending
        # Consomme : un second rendu, tour en cours, ne relance plus.
        assert browser.handle_control(_rendu("released", False)) is None

    def test_un_pending_du_tour_precedent_ne_relance_pas_en_plein_tour(self, _inscrit):
        """Tour 1 : handoff -> pending, fin du tour. La personne ECRIT au lieu
        de rendre la main, ce qui ouvre le tour 2 sur la meme session chaude.
        Rendre la main pendant le tour 2 ne doit pas l'interrompre : le pending
        n'attendait plus rien (et un tour fini est couvert par turnEnded)."""
        http, _ = _handoff_http()
        provider = _inscrit(_provider(http=http, wait_seconds=0.05)[0])
        self._pending(provider)
        provider.close_session("s1")
        provider.create_session("t1")
        assert "s1" not in provider._handoff_pending
        assert browser.handle_control(_rendu("released", False)) is None

    def test_un_nouveau_handoff_oublie_le_pending(self, _inscrit):
        http, posted = _handoff_http()
        provider = _inscrit(_provider(http=http, wait_seconds=0.05)[0])
        self._pending(provider)
        provider._wait_seconds = 2.0
        posted.clear()
        thread, box = _run_in_thread(provider.handoff, {"reason": "encore"}, task_id="t1")
        # L'attente est armee AVANT le POST : une fois poste, le pending est oublie.
        assert posted.wait(2)
        assert "s1" not in provider._handoff_pending
        # L'outil attend : le rendu le reveille, et aucun avis ne part.
        assert browser.handle_control(_rendu("released", False)) is None
        thread.join(3)
        assert json.loads(box["result"])["status"] == "done"

    def test_pending_ne_laisse_aucune_attente_armee(self, _inscrit):
        """Apres un ``pending``, un rendu ne doit pas « reveiller » une attente
        morte — et croire l'agent prevenu."""
        http, _ = _handoff_http()
        provider = _inscrit(_provider(http=http, wait_seconds=0.05)[0])
        self._pending(provider)
        assert "s1" not in provider._waiters

    def test_session_fermee_aucun_avis_et_pending_oublie(self, _inscrit):
        http, _ = _handoff_http()
        provider = _inscrit(_provider(http=http, wait_seconds=0.05)[0])
        self._pending(provider)
        assert browser.handle_control(_rendu("closed", True)) is None
        assert "s1" not in provider._handoff_pending

    def test_app_ancienne_aucun_avis(self, _inscrit):
        http, _ = _handoff_http()
        provider = _inscrit(_provider(http=http, wait_seconds=0.05)[0])
        self._pending(provider)
        assert browser.handle_control(_rendu(event=None)) is None

    def test_sans_canal_aucun_avis_et_un_avertissement(self, _inscrit, caplog):
        _inscrit(_provider()[0])
        with caplog.at_level("WARNING"):
            assert browser.handle_control(_rendu("released", True, slug=None)) is None
        assert any("sans canal" in r.getMessage() for r in caplog.records)

    def test_trame_inexploitable(self, _inscrit):
        assert browser.handle_control({"type": "browser.control", "controller": "agent"}) is None

    def test_deux_fournisseurs_un_seul_avis(self, _inscrit):
        """Deux instances du plugin : un rendu = UN avis, et le pending de
        l'une compte meme si l'autre ne connait pas la session."""
        http, _ = _handoff_http()
        _inscrit(_provider(http=mock.Mock(return_value=_opened("s-a")))[0])
        second = _inscrit(_provider(http=http, wait_seconds=0.05)[0])
        self._pending(second)
        notice = browser.handle_control(_rendu("released", False))
        assert isinstance(notice, dict) and notice["channelSlug"] == "compta"

    def test_les_pending_retenus_sont_bornes(self):
        provider, _, _ = _provider()
        for index in range(browser.MAX_TRACKED_SESSIONS + 10):
            provider._mark_handoff_pending(f"s{index}")
        assert len(provider._handoff_pending) == browser.MAX_TRACKED_SESSIONS
        assert "s0" not in provider._handoff_pending


# ── Cote adaptateur : transport, trame, enregistrement ─────────────────────


from test_adapter_dedup import _load_adapter_module  # noqa: E402

adapter_module = _load_adapter_module()


class _Config:
    extra = {"url": "http://pulse-chat.test", "token": "token-test"}


class _FakeResponse:
    status = 200

    def __init__(self, payload):
        self._payload = payload

    def read(self, *args, **kwargs):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_le_transport_porte_bearer_et_session(monkeypatch):
    """Meme en-tetes que le coffre : sans ``x-hermes-session``, l'app ne sait
    pas quel agent ouvre le navigateur et refuse (session ``verified``)."""
    import urllib.request

    adapter = adapter_module.PulseChatAdapter(_Config())
    adapter._handle_hello_ack({"type": "hello.ack", "sessionToken": "hs_jeton"})
    monkeypatch.setattr(adapter_module, "_live_adapter", lambda: adapter)
    requests = []

    def fake_urlopen(request, timeout=None):
        requests.append((request, timeout))
        return _FakeResponse({"sessionId": "s1", "cdpUrl": "wss://x/ws/browser-cdp/j"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    status, body = adapter_module._browser_http("POST", SESSIONS, {"channel": "demo"}, 60.0)

    assert status == 200 and body["sessionId"] == "s1"
    request, timeout = requests[0]
    assert request.full_url == "http://pulse-chat.test/api/agent/browser/sessions"
    headers = {k.lower(): v for k, v in request.header_items()}
    assert headers["authorization"] == "Bearer token-test"
    assert headers["x-hermes-session"] == "hs_jeton"
    assert json.loads(request.data) == {"channel": "demo"}
    assert timeout == 60.0


def test_la_trame_browser_control_est_relayee_par_la_boucle_de_reception(monkeypatch):
    seen = []
    monkeypatch.setattr(adapter_module.browser_provider, "handle_control", lambda frame: seen.append(frame))
    frame = {"type": "browser.control", "sessionId": "s1", "controller": "agent", "by": None}

    class _Ws:
        def __aiter__(self):
            async def gen():
                yield json.dumps(frame)

            return gen()

        async def close(self):
            return None

    adapter = adapter_module.PulseChatAdapter(_Config())
    adapter._ws = _Ws()
    asyncio.run(adapter._receive_loop())
    assert seen == [frame]


class _Ctx:
    def __init__(self, with_browser=True):
        self.providers = []
        self.tools = []
        self.platforms = []
        if with_browser:
            self.register_browser_provider = self._register_provider

    def _register_provider(self, provider):
        self.providers.append(provider)
        return object()

    def register_platform(self, **kwargs):
        self.platforms.append(kwargs)
        return object()

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)
        return object()

    def register_skill(self, *args, **kwargs):
        return object()


def test_register_enregistre_le_fournisseur_et_l_outil():
    ctx = _Ctx()
    adapter_module.register(ctx)

    assert [p.name for p in ctx.providers] == ["pulse"]
    tool = [t for t in ctx.tools if t["name"] == "pulse_browser_handoff"][0]
    assert tool["is_async"] is False
    assert tool["handler"] == ctx.providers[0].handoff
    # La trame atteint bien CE fournisseur.
    assert ctx.providers[0] in adapter_module.browser_provider._providers
    adapter_module.browser_provider.deactivate()


def _platform_hint(ctx):
    return ctx.platforms[-1]["platform_hint"]


def _fake_config(monkeypatch, cfg, readonly=True):
    import types

    config = types.ModuleType("hermes_cli.config")
    calls = []

    def reader():
        calls.append("read")
        if isinstance(cfg, Exception):
            raise cfg
        return cfg

    if readonly:
        config.read_raw_config_readonly = reader
    config.read_raw_config = lambda: (_ for _ in ()).throw(AssertionError("copie profonde inutile")) if readonly else reader()
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    sys.modules["hermes_cli"].config = config
    return calls


def test_hint_navigateur_absent_sans_cloud_provider_pulse(monkeypatch):
    """Tout bot existant, le jour ou il recoit cette version : aucun n'a le
    reglage. Lui parler d'un navigateur « vu en direct » et d'un outil cache
    par son check_fn serait lui mentir (« Unknown tool »)."""
    _fake_config(monkeypatch, {"browser": {"cloud_provider": "browserbase"}})
    ctx = _Ctx()
    adapter_module.register(ctx)
    assert "pulse_browser_handoff" not in _platform_hint(ctx)
    adapter_module.browser_provider.deactivate()


def test_hint_navigateur_absent_si_configuration_illisible(monkeypatch):
    _fake_config(monkeypatch, OSError("config.yaml illisible"))
    ctx = _Ctx()
    adapter_module.register(ctx)
    assert "pulse_browser_handoff" not in _platform_hint(ctx)
    adapter_module.browser_provider.deactivate()


def test_hint_navigateur_present_avec_cloud_provider_pulse(monkeypatch):
    _fake_config(monkeypatch, {"browser": {"cloud_provider": "pulse"}})
    ctx = _Ctx()
    adapter_module.register(ctx)
    hint = _platform_hint(ctx)
    assert "pulse_browser_handoff" in hint
    assert hint.endswith(adapter_module.BROWSER_HINT)
    adapter_module.browser_provider.deactivate()


def test_hint_navigateur_absent_sur_un_hermes_sans_fournisseurs(monkeypatch):
    _fake_config(monkeypatch, {"browser": {"cloud_provider": "pulse"}})
    ctx = _Ctx(with_browser=False)
    adapter_module.register(ctx)
    assert "pulse_browser_handoff" not in _platform_hint(ctx)


def test_hermes_sans_fournisseurs_de_navigateur_ne_casse_rien():
    ctx = _Ctx(with_browser=False)
    adapter_module.register(ctx)
    assert not any(t["name"] == "pulse_browser_handoff" for t in ctx.tools)
    # Les autres outils du plugin sont toujours la.
    assert any(t["name"] == "pulse_request_approval" for t in ctx.tools)


def test_outil_masque_sans_cloud_provider_pulse(monkeypatch):
    _fake_config(monkeypatch, {"browser": {"cloud_provider": "browserbase"}})
    assert adapter_module._handoff_tool_available() is False
    _fake_config(monkeypatch, {"browser": {"cloud_provider": "pulse"}})
    assert adapter_module._handoff_tool_available() is True
    _fake_config(monkeypatch, {})
    assert adapter_module._handoff_tool_available() is False
    # Illisible : l'outil reste propose (une prise de main impossible coute plus).
    _fake_config(monkeypatch, OSError("illisible"))
    assert adapter_module._handoff_tool_available() is True


def test_lecture_sans_copie_puis_repli_sur_un_hermes_ancien(monkeypatch):
    calls = _fake_config(monkeypatch, {"browser": {"cloud_provider": "pulse"}}, readonly=True)
    assert adapter_module._handoff_tool_available() is True
    assert calls == ["read"]
    calls = _fake_config(monkeypatch, {"browser": {"cloud_provider": "pulse"}}, readonly=False)
    assert adapter_module._handoff_tool_available() is True
    assert calls == ["read"]
