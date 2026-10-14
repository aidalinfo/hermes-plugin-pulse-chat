# -*- coding: utf-8 -*-
"""Battement de vie des cartes de pilotage (CDC §7.5, cf. pilot_heartbeat.py).

Ce qui est verifie, et chaque point est un mode d'echec MUET :

  - le corps est le battement GROUPE, avec les jetons appris au precedent ;
  - un 200 remplace les jetons par ceux des baux RENDUS (une carte refusee est
    oubliee : rien ne se rejoue a l'aveugle) ;
  - une app sans la route (404) arrete la boucle pour de bon, sans bruit ;
  - une panne passagere (0, 409 sans session, 503) garde les jetons ;
  - rien ne part avant le ``hello.ack`` (pas d'identite d'agent) ;
  - une exception ne fait JAMAIS tomber la boucle ;
  - ``disconnect`` annule la boucle.
"""

import asyncio
import json
import logging

import pytest

from test_adapter_dedup import _load_adapter_module

adapter_module = _load_adapter_module()
hb = adapter_module.pilot_heartbeat


class _Config:
    extra = {"url": "http://pulse-chat.test/", "token": "token-test"}


def _adapter():
    adapter = adapter_module.PulseChatAdapter(_Config())
    adapter._session_token = "session-1"
    return adapter


# ── Module pur ──────────────────────────────────────────────────────────


def test_url_et_corps_groupe():
    assert hb.heartbeat_url("http://app.test/") == "http://app.test/api/agent/pilotage/heartbeat"
    assert hb.build_body({}) == {"all": True, "tokens": {}}
    assert hb.build_body({"c1": 3}) == {"all": True, "tokens": {"c1": 3}}


def test_les_jetons_sont_bornes_comme_cote_app():
    tokens = {f"carte-{i}": i for i in range(hb.MAX_TOKENS + 7)}
    assert len(hb.build_body(tokens)["tokens"]) == hb.MAX_TOKENS


def test_200_les_jetons_deviennent_ceux_des_baux_rendus():
    body = {
        "leases": [{"taskId": "a", "leaseToken": 2}, {"taskId": "b", "leaseToken": 5}],
        "refused": [{"taskId": "c", "why": "stale_token"}],
        "heartbeatSeconds": 20,
    }
    tokens, stop, interval = hb.read_response(200, body, {"a": 1, "c": 9})
    assert tokens == {"a": 2, "b": 5}  # c refusee : oubliee
    assert stop is None
    assert interval == 20


def test_200_ignore_les_entrees_mal_formees():
    body = {"leases": [{"taskId": "a", "leaseToken": True}, {"taskId": 3, "leaseToken": 1}, "x", {"taskId": "b", "leaseToken": 1}]}
    tokens, _, interval = hb.read_response(200, body, {})
    assert tokens == {"b": 1}
    assert interval == hb.HEARTBEAT_SECONDS


def test_l_intervalle_annonce_ne_depasse_jamais_20_s():
    # Un serveur qui annoncerait 120 s ferait expirer le bail de 90 s.
    _, _, interval = hb.read_response(200, {"leases": [], "heartbeatSeconds": 120}, {})
    assert interval == hb.HEARTBEAT_SECONDS
    _, _, interval = hb.read_response(200, {"leases": [], "heartbeatSeconds": 10}, {})
    assert interval == 10


def test_404_arrete_la_boucle():
    tokens, stop, _ = hb.read_response(404, {"statusMessage": "x"}, {"a": 1})
    assert stop == "unsupported"
    assert tokens == {"a": 1}


@pytest.mark.parametrize("status", [0, 409, 500, 503])
def test_panne_passagere_garde_les_jetons(status):
    tokens, stop, _ = hb.read_response(status, None, {"a": 1})
    assert tokens == {"a": 1}
    assert stop is None


# ── Adaptateur ──────────────────────────────────────────────────────────


def test_rien_ne_part_avant_le_hello_ack(monkeypatch):
    adapter = _adapter()
    adapter._session_token = None
    calls = []
    monkeypatch.setattr(adapter, "_http_detailed", lambda *a, **k: calls.append(a) or (200, {}))
    tokens, stop, _ = asyncio.run(adapter._pilot_heartbeat_once({"a": 1}))
    assert calls == []
    assert tokens == {"a": 1} and stop is None


def test_un_battement_poste_le_corps_groupe_et_journalise_les_refus(monkeypatch, caplog):
    adapter = _adapter()
    seen = {}

    def fake_http(method, url, *, body=None, headers=None, timeout=None):
        seen.update(method=method, url=url, body=json.loads(body.decode("utf-8")), headers=headers)
        return 200, {"leases": [{"taskId": "a", "leaseToken": 4}], "refused": [{"taskId": "b", "why": "expired"}], "heartbeatSeconds": 20}

    monkeypatch.setattr(adapter, "_http_detailed", fake_http)
    with caplog.at_level(logging.WARNING):
        tokens, stop, interval = asyncio.run(adapter._pilot_heartbeat_once({"a": 3, "b": 1}))
    assert seen["method"] == "POST"
    assert seen["url"] == "http://pulse-chat.test/api/agent/pilotage/heartbeat"
    assert seen["body"] == {"all": True, "tokens": {"a": 3, "b": 1}}
    assert seen["headers"] == {"Content-Type": "application/json"}
    assert tokens == {"a": 4}
    assert stop is None and interval == 20
    assert "carte b (expired)" in caplog.text


def test_la_boucle_survit_a_une_exception_et_s_arrete_sur_404(monkeypatch):
    adapter = _adapter()
    replies = iter([RuntimeError("reseau"), (200, {"leases": [{"taskId": "a", "leaseToken": 1}]}), (404, None)])
    presented = []

    def fake_http(method, url, *, body=None, headers=None, timeout=None):
        presented.append(json.loads(body.decode("utf-8"))["tokens"])
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def no_sleep(_):
        return None

    monkeypatch.setattr(adapter, "_http_detailed", fake_http)
    monkeypatch.setattr(adapter_module.asyncio, "sleep", no_sleep)
    asyncio.run(asyncio.wait_for(adapter._pilot_heartbeat_loop(), timeout=2))
    # 1er appel en echec (jetons inchanges), 2e appris, 3e les presente puis 404.
    assert presented == [{}, {}, {"a": 1}]
    assert adapter._pilot_heartbeat_unsupported is True
    # Plus jamais redemarree : l'app ne connait pas la route.
    async def check():
        adapter._ensure_pilot_heartbeat()
        return adapter._pilot_heartbeat_task

    assert asyncio.run(check()) is None


def test_disconnect_annule_la_boucle(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_http_detailed", lambda *a, **k: (200, {"leases": []}))

    async def scenario():
        adapter._ensure_pilot_heartbeat()
        task = adapter._pilot_heartbeat_task
        await asyncio.sleep(0)
        assert task is not None and not task.done()
        await adapter.disconnect()
        return task

    task = asyncio.run(scenario())
    assert task.cancelled()
    assert adapter._pilot_heartbeat_task is None
