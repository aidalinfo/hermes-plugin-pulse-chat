# -*- coding: utf-8 -*-
"""Identifiants d'agent (``credentials.py``) — relais du coffre « Passwords &
Logins » d'Hermes vers Pulse Chat (docs/48 de l'app).

S'executent SANS hermes installe : ``agent.vault_store`` et
``agent.vault_backends`` sont remplaces par un coffre EN MEMOIRE qui reproduit
le contrat de v2026.9.11 (``add_item(kind, label, secret, origin=)``, champ
``otp_secret``, ``remove_item`` booleen, ``resolve_secret``, ``get_meta``,
``VaultItemMeta`` avec ``has_otp``). Ce qui est verifie :

  - l'instantane ne porte que des METADONNEES (liste blanche) ;
  - ajout / suppression / revelation, et les refus (non local, non login) ;
  - capacite absente quand ``agent.vault_store`` ne s'importe pas ;
  - le secret n'apparait dans AUCUN journal capture, ni dans la trame hello ;
  - l'adaptateur repousse l'instantane AVANT de repondre, hors de la boucle.
"""

import asyncio
import importlib
import json
import logging
import sys
import types
from dataclasses import dataclass
from typing import Optional

import pytest

from test_adapter_dedup import _load_adapter_module

adapter_module = _load_adapter_module()
credentials = importlib.import_module("pulse_chat_plugin_under_test.credentials")

SECRET = "S3ntinelle-ne-sort-jamais"
SEED = "JBSWY3DPEHPK3PXP"


class VaultError(Exception):
    pass


@dataclass(frozen=True)
class VaultItemMeta:
    id: str
    kind: str
    label: str
    origin: Optional[str]
    created_at: str
    identifier_type: Optional[str] = None
    identifier: Optional[str] = None
    has_otp: bool = False


class FakeStore:
    def __init__(self):
        self.records = []

    def add_item(self, kind, label, secret, origin=None):
        if not origin or "://" not in origin:
            raise VaultError(f"origin must include a scheme (got {origin!r})")
        secret = dict(secret)
        id_type = secret.pop("identifier_type")
        identifier = secret.pop("identifier")
        rec = {
            "id": "vault_%012d" % len(self.records),
            "kind": kind,
            "label": label,
            "origin": origin,
            "identifier_type": id_type,
            "identifier": identifier,
            "secret": secret,
        }
        self.records.append(rec)
        return self._meta(rec)

    def list_items(self):
        return [self._meta(r) for r in self.records]

    def remove_item(self, item_id):
        before = len(self.records)
        self.records = [r for r in self.records if r["id"] != item_id]
        return len(self.records) != before

    def get_meta(self, item_id):
        for r in self.records:
            if r["id"] == item_id:
                return self._meta(r)
        return None

    def resolve_secret(self, item_id):
        for r in self.records:
            if r["id"] == item_id:
                return dict(r["secret"])
        raise VaultError("no vault item")

    @staticmethod
    def _meta(r):
        return VaultItemMeta(
            id=r["id"],
            kind=r["kind"],
            label=r["label"],
            origin=r["origin"],
            created_at="2026-10-03T00:00:00+00:00",
            identifier_type=r.get("identifier_type"),
            identifier=r.get("identifier"),
            has_otp=bool(r["secret"].get("otp_secret")),
        )


class FakeBackend:
    def __init__(self, name, items, unlocked=True):
        self.name = name
        self.needs_unlock = name != "local"
        self._items = items
        self._unlocked = unlocked

    def is_unlocked(self):
        return self._unlocked

    def list_items(self):
        return self._items()


@pytest.fixture()
def vault(monkeypatch):
    store = FakeStore()
    agent_pkg = types.ModuleType("agent")
    agent_pkg.__path__ = []
    vault_store = types.ModuleType("agent.vault_store")
    vault_store.VaultError = VaultError
    vault_store.get_vault_store = lambda: store
    vault_store.totp_now = lambda seed: "424242" if seed == SEED else "000000"
    vault_store.scrub_secret_from_text = lambda text, secret: text
    backends = types.ModuleType("agent.vault_backends")
    external = [
        VaultItemMeta(id="op:abc", kind="login", label="GitHub", origin="https://github.com", created_at="")
    ]
    state = {"op_unlocked": True}
    backends.enabled_backends = lambda: [
        FakeBackend("local", store.list_items),
        FakeBackend("onepassword", lambda: external, unlocked=state["op_unlocked"]),
    ]
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.vault_store", vault_store)
    monkeypatch.setitem(sys.modules, "agent.vault_backends", backends)
    store.state = state
    return store


def _add(**overrides):
    command = {
        "type": "credentials.command",
        "requestId": "r-add",
        "op": "add",
        "origin": "https://erp.example.fr",
        "label": "ERP",
        "identifierType": "email",
        "identifier": "bot@example.fr",
        "password": SECRET,
        "totp": SEED,
    }
    command.update(overrides)
    return command


# ── Module pur ───────────────────────────────────────────────────────────


def test_capacite_absente_sans_coffre_hermes(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.vault_store", None)
    assert credentials.credentials_available() is False
    assert credentials.execute(_add())["code"] == "unavailable"


def test_ajout_ecrit_le_champ_otp_secret_et_rend_la_poignee(vault):
    result = credentials.execute(_add())
    assert result == {"ok": True, "value": {"handle": "vault_000000000000"}}
    assert vault.records[0]["secret"] == {"password": SECRET, "otp_secret": SEED}


def test_ajout_refuse_relaie_le_message_du_coffre(vault):
    result = credentials.execute(_add(origin="erp.example.fr"))
    assert result["ok"] is False and result["code"] == "invalid"
    assert SECRET not in json.dumps(result)


def test_instantane_ne_porte_que_des_metadonnees(vault):
    credentials.execute(_add())
    items = credentials.collect_snapshot("default")
    assert {i["source"] for i in items} == {"local", "onepassword"}
    local = next(i for i in items if i["source"] == "local")
    assert set(local) <= {
        "handle", "kind", "label", "origin", "identifier", "identifierType", "hasTotp", "source", "createdAt",
    }
    assert local["hasTotp"] is True
    assert SECRET not in json.dumps(items) and SEED not in json.dumps(items)


def test_gestionnaire_verrouille_ne_contribue_rien(vault):
    vault.state["op_unlocked"] = False
    assert {i["source"] for i in credentials.collect_snapshot()} == set()


def test_revele_mot_de_passe_et_code_jamais_la_cle(vault):
    handle = credentials.execute(_add())["value"]["handle"]
    assert credentials.execute({"op": "reveal", "handle": handle, "what": "password"}) == {
        "ok": True,
        "value": SECRET,
    }
    code = credentials.execute({"op": "reveal", "handle": handle, "what": "totp_code"})
    assert code == {"ok": True, "value": "424242"}


def test_refuse_de_reveler_ou_supprimer_une_entree_non_locale(vault):
    for op in ("reveal", "remove"):
        res = credentials.execute({"op": op, "handle": "op:abc", "what": "password"})
        assert res["code"] == "not_allowed"


def test_refuse_de_reveler_une_carte(vault):
    vault.records.append(
        {"id": "vault_card", "kind": "payment", "label": "Carte", "origin": None, "secret": {"cvc": "123"}}
    )
    assert credentials.execute({"op": "reveal", "handle": "vault_card", "what": "password"})["code"] == "not_allowed"


def test_suppression(vault):
    handle = credentials.execute(_add())["value"]["handle"]
    assert credentials.execute({"op": "remove", "handle": handle}) == {"ok": True}
    assert credentials.execute({"op": "remove", "handle": handle})["code"] == "not_found"


def test_operation_inconnue(vault):
    assert credentials.execute({"op": "export"})["code"] == "invalid"


# ── Adaptateur ───────────────────────────────────────────────────────────


class _Config:
    extra = {"url": "http://pulse-chat.test", "token": "token-test", "profile": "bot"}


def _adapter(posts):
    adapter = adapter_module.PulseChatAdapter(_Config())
    adapter._credentials_enabled = True

    def fake_http(method, url, *, body=None, headers=None, timeout=None):
        posts.append((url, json.loads(body.decode("utf-8"))))
        return 200, {}

    adapter._http_detailed = fake_http
    return adapter


def test_commande_ajout_repousse_l_instantane_avant_de_repondre(vault, caplog):
    posts = []
    adapter = _adapter(posts)
    caplog.set_level(logging.DEBUG)

    async def run():
        await adapter._handle_credentials_command(_add(profile="bot"))

    asyncio.run(run())
    urls = [u for u, _ in posts]
    assert urls == [
        "http://pulse-chat.test/api/agent/credentials/snapshot",
        "http://pulse-chat.test/api/agent/credentials/result/r-add",
    ]
    assert posts[0][1]["profile"] == "bot"
    assert SECRET not in json.dumps(posts[0][1])
    assert posts[1][1]["ok"] is True
    assert SECRET not in caplog.text and SEED not in caplog.text


def test_revelation_poste_la_valeur_et_ne_la_journalise_jamais(vault, caplog):
    posts = []
    adapter = _adapter(posts)
    handle = credentials.execute(_add())["value"]["handle"]
    caplog.set_level(logging.DEBUG)

    async def run():
        await adapter._handle_credentials_command(
            {"type": "credentials.command", "requestId": "r-rev", "op": "reveal", "handle": handle, "what": "password"}
        )

    asyncio.run(run())
    # Pas d'instantane pour une lecture ; la valeur part dans le corps, une fois.
    assert [u for u, _ in posts] == ["http://pulse-chat.test/api/agent/credentials/result/r-rev"]
    assert posts[0][1] == {"ok": True, "value": SECRET}
    assert SECRET not in caplog.text


def test_dispatch_de_la_trame_hors_boucle_de_reception(vault):
    """La trame est confiee a une tache : la boucle de reception n'attend pas
    le coffre (synchrone, parfois lent pour un gestionnaire externe)."""
    posts = []
    adapter = _adapter(posts)

    class _WS:
        def __init__(self, frames):
            self._frames = list(frames)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._frames:
                raise StopAsyncIteration
            return self._frames.pop(0)

        async def close(self):
            return None

    async def run():
        adapter._ws = _WS([json.dumps(_add(profile="bot"))])
        await adapter._receive_loop()
        await asyncio.gather(*list(adapter._credential_tasks))

    asyncio.run(run())
    assert posts[-1][0].endswith("/result/r-add")


def test_hello_annonce_la_capacite_seulement_si_le_coffre_s_importe(vault, monkeypatch):
    fake_ws_sent = []

    class _FakeWS:
        async def send(self, data):
            fake_ws_sent.append(data)

        async def close(self):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    websockets = types.ModuleType("websockets")

    def _connect(url, **kwargs):
        async def _open():
            return _FakeWS()

        return _open()

    websockets.connect = _connect
    monkeypatch.setitem(sys.modules, "websockets", websockets)
    adapter = adapter_module.PulseChatAdapter(_Config())

    async def run():
        assert await adapter.connect() is True
        await adapter.disconnect()

    asyncio.run(run())
    frame = json.loads(fake_ws_sent[0])
    assert frame["capabilities"]["features"] == ["credentials"]

    # Sans le coffre : aucune annonce.
    fake_ws_sent.clear()
    monkeypatch.setitem(sys.modules, "agent.vault_store", None)
    adapter = adapter_module.PulseChatAdapter(_Config())
    asyncio.run(run_again(adapter))
    assert "capabilities" not in json.loads(fake_ws_sent[0])


async def run_again(adapter):
    assert await adapter.connect() is True
    await adapter.disconnect()
