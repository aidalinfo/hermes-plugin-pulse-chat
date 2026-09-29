# -*- coding: utf-8 -*-
"""Une réponse RÉEXPÉDIÉE par Hermes (« ♻️ Recovered reply … : ») part en MESSAGE.

Le registre de livraison d'Hermes préfixe la réponse entière d'un marqueur ♻️
quand il la réexpédie après un échec de livraison. Elle arrivait dans le fil
en activité d'outil (« Interim ») : la réponse de l'agent était repliée sous
« 1 activité d'outil », illisible sans la déplier. La bulle ne porte que le
CORPS ; le contenu original reste dans ``raw``.
"""

import asyncio

from test_adapter_dedup import _load_adapter_module

adapter_module = _load_adapter_module()

MARKER = (
    "♻️ Recovered reply — the messaging platform reconnected after the original "
    "delivery failed, so this may be a duplicate:"
)
BODY = "@Maxime — **authentification retirée**.\n\nLa maquette est accessible."


class _Config:
    extra = {"url": "http://pulse-chat.test", "token": "token-test"}


def _send(content):
    adapter = adapter_module.PulseChatAdapter(_Config())
    posted = []

    async def fake_post(payload, hermes_id):
        posted.append(payload)
        return None

    adapter._post_agent_message = fake_post
    asyncio.run(adapter.send("canal-test", content))
    return posted[0]


def test_recovered_reply_posts_its_body_as_a_message():
    content = f"{MARKER}\n\n{BODY}"
    payload = _send(content)
    assert payload["kind"] == "message"
    assert payload["content"] == BODY
    assert payload["raw"] == content
    assert payload["tool"] is None
    assert payload["phase"] is None


def test_marker_without_body_stays_an_interim():
    payload = _send("♻️ Gateway online")
    assert payload["kind"] == "tool_event"
    assert payload["phase"] == "interim"
    assert payload["content"] == "♻️ Gateway online"


def test_ordinary_reply_is_untouched():
    payload = _send(BODY)
    assert payload["kind"] == "message"
    assert payload["content"] == BODY
    assert payload["raw"] == BODY
