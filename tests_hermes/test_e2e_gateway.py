# -*- coding: utf-8 -*-
"""Bout en bout sur un Hermes EPHEMERE : ``hermes gateway run`` reel, plugin
charge par Hermes lui-meme, fausse app Pulse Chat, faux modele scenarise.

Ce qui est vrai ici et ne l'est pas dans ``test_hermes_reel.py`` : le runner
du gateway, sa boucle d'agent, ses outils (terminal, clarify, delegate_task),
le garde-fou appele par le vrai outil terminal, et le transport WebSocket/HTTP
du plugin. Le modele, lui, est un script (cf. ``e2e_banc.FakeLLM``).
"""

import pathlib
import shutil
import tempfile
import time
import uuid

import pytest

from e2e_banc import FakeApp, FakeLLM, Gateway, wait_until

TURN_S = 90
APPROVAL_TIMEOUT_S = 12


@pytest.fixture(scope="module")
def banc():
    root = pathlib.Path(tempfile.mkdtemp(prefix="hermes-e2e-"))
    llm = FakeLLM()
    app = FakeApp()
    gw = Gateway(root, llm, app, approval_timeout=APPROVAL_TIMEOUT_S)
    try:
        wait_until(lambda: app.frames("hello"), 120, "hello du plugin", gw)
        yield llm, app, gw
    finally:
        gw.close()
        app.close()
        llm.close()
        print(f"\n--- journal de la gateway ({root}) ---\n{gw.tail(200)}")
        shutil.rmtree(root, ignore_errors=True)


def _slug():
    return f"e2e-{uuid.uuid4().hex[:8]}"


def _final_text(app, slug, needle, gw):
    return wait_until(
        lambda: next((m for m in app.messages(slug, "message") if needle in str(m.get("content"))), None),
        TURN_S, f"reponse contenant {needle!r} dans {slug}", gw)


def test_hello_annonce_l_agent(banc):
    _, app, _ = banc
    hello = app.frames("hello")[0]
    assert hello.get("type") == "hello"


def test_aller_retour_texte(banc):
    llm, app, gw = banc
    llm.scripts["texte"] = lambda results: {"text": "PONG-E2E"}
    slug = _slug()
    message_id = app.say(slug, "E2E:texte bonjour")
    _final_text(app, slug, "PONG-E2E", gw)
    wait_until(lambda: any(f.get("messageId") == message_id for f in app.frames("ack")), 10, "ack", gw)


def _final_after_tools(results):
    return {"text": "FIN " + " | ".join(r[:300] for r in results)}


def _cards(app, slug, kind):
    return app.messages(slug, kind)


def _cible(gw, name):
    target = gw.workspace / name
    target.mkdir()
    (target / "f.txt").write_text("x")
    return target


def _rm_script(target, marker):
    def script(results):
        if not results:
            return {"tool": "terminal", "args": {"command": f"rm -rf {target} && echo {marker}"}}
        return _final_after_tools(results)
    return script


# --------------------------------------------------------------------------
# Approbations : le vrai outil terminal declenche le vrai garde-fou
# --------------------------------------------------------------------------


def test_approbation_once_execute_la_commande(banc):
    llm, app, gw = banc
    slug = _slug()
    target = _cible(gw, f"once-{slug}")
    llm.scripts["once"] = _rm_script(target, "SUPPRIME-ONCE")
    app.say(slug, "E2E:once")
    card = wait_until(lambda: (_cards(app, slug, "approval_request") or [None])[0], TURN_S, "carte d'approbation", gw)
    assert str(target) in card["command"]
    assert card["requestId"].startswith("req-")
    assert target.exists(), "commande executee AVANT la decision"
    app.reply_approval(slug, card["requestId"], "once")
    _final_text(app, slug, "SUPPRIME-ONCE", gw)
    assert not target.exists()


def test_approbation_deny_n_execute_rien(banc):
    llm, app, gw = banc
    slug = _slug()
    target = _cible(gw, f"deny-{slug}")
    llm.scripts["deny"] = _rm_script(target, "SUPPRIME-DENY")
    app.say(slug, "E2E:deny")
    card = wait_until(lambda: (_cards(app, slug, "approval_request") or [None])[0], TURN_S, "carte d'approbation", gw)
    app.reply_approval(slug, card["requestId"], "deny")
    final = _final_text(app, slug, "FIN", gw)
    assert "SUPPRIME-DENY" not in str(final["content"])
    assert target.exists()


def test_approbation_sans_reponse_expire_en_refus(banc):
    llm, app, gw = banc
    slug = _slug()
    target = _cible(gw, f"expire-{slug}")
    llm.scripts["expire"] = _rm_script(target, "SUPPRIME-EXPIRE")
    app.say(slug, "E2E:expire")
    wait_until(lambda: _cards(app, slug, "approval_request"), TURN_S, "carte d'approbation", gw)
    final = _final_text(app, slug, "FIN", gw)
    assert "SUPPRIME-EXPIRE" not in str(final["content"])
    assert target.exists()


def test_approbation_rejouee_apres_coup_ne_fait_rien(banc):
    llm, app, gw = banc
    slug = _slug()
    target = _cible(gw, f"rejeu-{slug}")
    llm.scripts["rejeu"] = _rm_script(target, "SUPPRIME-REJEU")
    app.say(slug, "E2E:rejeu")
    card = wait_until(lambda: (_cards(app, slug, "approval_request") or [None])[0], TURN_S, "carte", gw)
    app.reply_approval(slug, card["requestId"], "deny")
    _final_text(app, slug, "FIN", gw)
    # La meme decision, rejouee en « once » (reconnexion de l'app) : sans effet.
    app.reply_approval(slug, card["requestId"], "once")
    target2 = _cible(gw, f"rejeu2-{slug}")
    llm.scripts["rejeu2"] = _rm_script(target2, "SUPPRIME-REJEU2")
    app.say(slug, "E2E:rejeu2")
    wait_until(lambda: len(_cards(app, slug, "approval_request")) >= 2, TURN_S, "seconde carte", gw)
    app.reply_approval(slug, card["requestId"], "once")
    time.sleep(2)
    assert target2.exists(), "le rejeu d'une ancienne carte a approuve une nouvelle demande"
    app.reply_approval(slug, _cards(app, slug, "approval_request")[1]["requestId"], "deny")
    _final_text(app, slug, "FIN", gw)
    assert target.exists() and target2.exists()


def test_deux_demandes_chacune_sa_decision(banc):
    """Deux commandes dangereuses dans le meme tour : la decision de chaque
    carte ne s'applique qu'a SA commande, quel que soit l'ordre des reponses."""
    llm, app, gw = banc
    slug = _slug()
    a = _cible(gw, f"a-{slug}")
    b = _cible(gw, f"b-{slug}")

    def script(results):
        if not results:
            return {"tools": [
                {"tool": "terminal", "args": {"command": f"rm -rf {a} && echo SUPPRIME-A"}},
                {"tool": "terminal", "args": {"command": f"rm -rf {b} && echo SUPPRIME-B"}},
            ]}
        return _final_after_tools(results)

    llm.scripts["deux"] = script
    app.say(slug, "E2E:deux")
    answered = set()
    deadline = time.monotonic() + TURN_S
    while len(answered) < 2 and time.monotonic() < deadline:
        cards = [c for c in _cards(app, slug, "approval_request") if c["requestId"] not in answered]
        # Toujours la carte la plus RECENTE d'abord : en FIFO, sa decision
        # tomberait sur l'autre demande.
        for card in reversed(cards):
            decision = "once" if str(a) in card["command"] else "deny"
            app.reply_approval(slug, card["requestId"], decision)
            answered.add(card["requestId"])
        time.sleep(0.1)
    assert len(answered) == 2, gw.tail()
    _final_text(app, slug, "FIN", gw)
    assert not a.exists(), "la commande approuvee n'a pas tourne"
    assert b.exists(), "la commande refusee a tourne"


# --------------------------------------------------------------------------
# Question (clarify), sous-agent, coffre, voix
# --------------------------------------------------------------------------


def test_clarify_en_carte_et_reponse(banc):
    llm, app, gw = banc
    slug = _slug()

    def script(results):
        if not results:
            return {"tool": "clarify", "args": {"questions": [
                {"question": "Quelle couleur E2E ?", "choices": ["Rouge", "Bleu"]}]}}
        return _final_after_tools(results)

    llm.scripts["clarify"] = script
    app.say(slug, "E2E:clarify")
    card = wait_until(lambda: (_cards(app, slug, "question_request") or [None])[0], TURN_S, "carte question", gw)
    assert "Quelle couleur E2E" in card["question"]
    app.reply_question(slug, card["requestId"], "Bleu")
    final = _final_text(app, slug, "FIN", gw)
    assert "Bleu" in str(final["content"])


def test_sous_agent_rend_son_resultat(banc):
    """``delegate_task`` : synchrone sur un Hermes ancien (le resultat revient
    dans l'outil), en arriere-plan depuis 0.23 (il revient en message). Dans
    les deux cas, le parent doit le recevoir et repondre dans le canal."""
    llm, app, gw = banc
    slug = _slug()
    llm.scripts["enfant"] = lambda results: {"text": "RESULTAT-SOUS-AGENT"}
    llm.triggers["RESULTAT-SOUS-AGENT"] = lambda results: {"text": "RELAIS-PARENT"}

    def parent(results):
        if not results:
            return {"tool": "delegate_task", "args": {"tasks": [{"goal": "E2E:enfant reponds simplement"}]}}
        if any("RESULTAT-SOUS-AGENT" in r for r in results):
            return {"text": "RELAIS-PARENT"}
        return {"text": "ATTENTE-SOUS-AGENT"}

    llm.scripts["parent"] = parent
    app.say(slug, "E2E:parent")
    _final_text(app, slug, "RELAIS-PARENT", gw)


def test_coffre_ecriture_part_vers_l_app(banc):
    llm, app, gw = banc
    slug = _slug()

    def script(results):
        if not results:
            return {"tool": "pulse_vault_write", "args": {"path": "rapports/e2e.md", "content": "# E2E\n"}}
        return _final_after_tools(results)

    llm.scripts["coffre"] = script
    app.say(slug, "E2E:coffre")
    _final_text(app, slug, "FIN", gw)
    writes = [p for p in app.posts if p["path"].startswith(f"/api/agent/vault/{slug}/")]
    assert writes and writes[0]["method"] == "PUT", [m.get("content") for m in app.messages(slug)]
    assert writes[0]["path"].endswith("rapports/e2e.md")


def test_message_vocal_sans_media_reste_un_tour_normal(banc):
    llm, app, gw = banc
    llm.scripts["vocal"] = lambda results: {"text": "PONG-VOCAL"}
    slug = _slug()
    app.say(slug, "E2E:vocal", messageType="voice")
    _final_text(app, slug, "PONG-VOCAL", gw)


def test_voix_la_synthese_part_vers_l_app(banc):
    """Le vrai outil ``text_to_speech`` (synthese factice) : le fichier produit
    doit partir vers l'app par ``POST /api/agent/voice/<canal>``."""
    llm, app, gw = banc
    slug = _slug()

    def script(results):
        if not results:
            return {"tool": "text_to_speech", "args": {"text": "Bonjour depuis le banc"}}
        return _final_after_tools(results)

    llm.scripts["voix"] = script
    app.say(slug, "E2E:voix", messageType="voice")
    wait_until(lambda: [p for p in app.posts if p["path"] == f"/api/agent/voice/{slug}"], TURN_S,
               "POST de la voix", gw)
    assert llm.speech and llm.speech[0].get("input") == "Bonjour depuis le banc"


def test_navigateur_passe_par_le_fournisseur_pulse(banc):
    """``browser.cloud_provider: pulse`` : le vrai outil navigateur doit
    demander sa session a l'app, pour le canal de la conversation. L'app
    repond 501 (pas de navigateur) : on verifie le branchement, pas Chromium."""
    llm, app, gw = banc
    slug = _slug()
    app.responses[("POST", "/api/agent/browser/sessions")] = (501, {"error": "browser_unavailable"})

    def script(results):
        if not results:
            return {"tool": "browser_navigate", "args": {"url": "https://example.com"}}
        return _final_after_tools(results)

    llm.scripts["navigateur"] = script
    app.say(slug, "E2E:navigateur")
    _final_text(app, slug, "FIN", gw)
    sessions = [p for p in app.posts if p["path"] == "/api/agent/browser/sessions"]
    assert sessions, [m.get("content") for m in app.messages(slug)]
    assert sessions[0]["body"].get("channel") == slug
