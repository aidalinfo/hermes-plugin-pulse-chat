# -*- coding: utf-8 -*-
"""Barge-in : la trame ``call.interrupt`` arrete VRAIMENT le tour en cours.

Couper le son cote navigateur ne suffit pas. Sans cet arret, Hermes finit de
rediger, ouvre un nouveau flux a la phrase suivante, et la voix repart
par-dessus celle de l'humain — pour un texte que plus personne n'ecoute. C'est
le defaut observe en usage : « quand je parle il ne s'arrete pas, il continue ».

Meme gabarit que test_adapter_dedup.py : stubs ``gateway.*``, chargement de
``adapter.py`` par chemin (le dossier du plugin contient un tiret).
"""

import asyncio
import sys

import pytest

from test_adapter_dedup import _load_adapter_module

adapter_module = _load_adapter_module()


class _Config:
    extra = {"url": "http://pulse-chat.test", "token": "token-test"}


def _adapter():
    a = adapter_module.PulseChatAdapter(config=_Config(), platform="pulse-chat")
    a.interrupted = []

    async def _interrupt(session_key, chat_id, metadata=None):
        a.interrupted.append((session_key, chat_id))

    a.interrupt_session_activity = _interrupt
    return a


@pytest.fixture(autouse=True)
def _session_key_stub():
    """`gateway.session.build_session_key` — la vraie vit dans Hermes."""
    import types

    module = types.ModuleType("gateway.session")
    module.build_session_key = lambda source, **kwargs: "cle:%s" % (
        source.get("chat_id") if isinstance(source, dict) else source
    )
    sys.modules["gateway.session"] = module
    yield
    sys.modules.pop("gateway.session", None)


def test_interrompt_la_session_du_canal():
    a = _adapter()
    # Un message a ete recu sur ce canal : sa source est memorisee, c'est elle
    # qui donne la cle de session a arreter.
    a._last_source["demo"] = {"chat_id": "demo"}

    asyncio.run(a._handle_call_interrupt({"type": "call.interrupt", "chatId": "demo"}))

    assert a.interrupted == [("cle:demo", "demo")]


def test_canal_jamais_vu_ne_leve_pas():
    # Une interruption sur un canal dont aucun message n'est passe depuis la
    # connexion : il n'y a rien a arreter, et surtout rien a faire echouer.
    a = _adapter()
    asyncio.run(a._handle_call_interrupt({"chatId": "inconnu"}))
    assert a.interrupted == []


def test_trame_sans_canal_ignoree():
    a = _adapter()
    asyncio.run(a._handle_call_interrupt({}))
    assert a.interrupted == []


def test_la_source_est_memorisee_par_canal():
    # Deux canaux en parallele : interrompre l'un ne doit pas arreter l'autre.
    a = _adapter()
    a._last_source["demo"] = {"chat_id": "demo"}
    a._last_source["autre"] = {"chat_id": "autre"}

    asyncio.run(a._handle_call_interrupt({"chatId": "autre"}))

    assert a.interrupted == [("cle:autre", "autre")]
