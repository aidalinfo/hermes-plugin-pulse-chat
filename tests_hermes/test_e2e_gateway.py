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
import uuid

import pytest

from e2e_banc import FakeApp, FakeLLM, Gateway, wait_until

TURN_S = 90


@pytest.fixture(scope="module")
def banc():
    root = pathlib.Path(tempfile.mkdtemp(prefix="hermes-e2e-"))
    llm = FakeLLM()
    app = FakeApp()
    gw = Gateway(root, llm, app)
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
