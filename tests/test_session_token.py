# -*- coding: utf-8 -*-
"""Jeton de session : le plugin porte son identite (tache 3).

Le serveur repond au ``hello`` par une trame
``{"type": "hello.ack", "sessionToken": "<64 hex>", "profiles": [...]}``. Le
plugin MEMORISE ce jeton et le REJOINT a ses appels sortants (en-tete HTTP
``x-hermes-session``, en plus du Bearer inchange, et champ ``sessionToken`` de
l'ack WebSocket).

Doctrine verifiee ici : le plugin transporte et n'interprete pas — il ne lit
jamais le contenu du jeton, n'en derive aucune decision, et fonctionne
EXACTEMENT comme avant quand la trame n'arrive pas (serveur anterieur).

Chargement identique aux autres tests d'adaptateur : stubs ``gateway.*`` puis
chargement d'``adapter.py`` par chemin.
"""

import asyncio
import json
import urllib.error
import urllib.request

from test_adapter_dedup import _load_adapter_module

adapter_module = _load_adapter_module()

TOKEN_A = "a" * 64
TOKEN_B = "b" * 64


class _Config:
    extra = {"url": "http://pulse-chat.test", "token": "token-test"}


def _make_adapter():
    return adapter_module.PulseChatAdapter(_Config())


def _header(request, name):
    """Lecture insensible a la casse (urllib normalise les noms d'en-tete)."""
    for key, value in request.header_items():
        if key.lower() == name.lower():
            return value
    return None


class _FakeResponse:
    status = 200

    def read(self, *args, **kwargs):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture_http(monkeypatch):
    """Intercepte urlopen et rend la liste des ``Request`` emises."""
    requests = []

    def fake_urlopen(request, timeout=None):
        requests.append(request)
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return requests


class _FakeWs:
    """WS double : collecte les trames emises, rejoue celles qu'on lui donne."""

    def __init__(self, incoming=()):
        self.sent = []
        self._incoming = list(incoming)

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    def __aiter__(self):
        async def gen():
            for frame in self._incoming:
                yield json.dumps(frame)

        return gen()

    async def close(self):
        return None


# --- 1. Memorisation ---------------------------------------------------------

def test_le_jeton_recu_dans_hello_ack_est_memorise():
    """La trame arrive par la boucle de reception, comme en production."""
    adapter = _make_adapter()
    assert adapter.session_token is None
    adapter._ws = _FakeWs(
        [{"type": "hello.ack", "sessionToken": TOKEN_A, "profiles": ["support"]}]
    )

    asyncio.run(adapter._receive_loop())

    assert adapter.session_token == TOKEN_A


# --- 2. Appels HTTP sortants -------------------------------------------------

def test_le_jeton_est_joint_aux_appels_http_sortants(monkeypatch):
    """Tous les points d'appel HTTP vers l'app portent l'en-tete de session."""
    adapter = _make_adapter()
    adapter._handle_hello_ack({"type": "hello.ack", "sessionToken": TOKEN_A})
    requests = _capture_http(monkeypatch)

    async def run():
        # POST /api/agent/messages (send, edit_message, artifacts, approbations)
        await adapter.send("demo", "bonjour")
        # Coffre-fort (GET / PUT / DELETE)
        await adapter.vault_list("demo")
        await adapter.vault_write("demo", "a.md", b"x", "text/markdown")
        await adapter.vault_delete("demo", "a.md")

    asyncio.run(run())

    assert len(requests) == 4
    for request in requests:
        assert _header(request, "x-hermes-session") == TOKEN_A
        # Le Bearer reste inchange : la session s'AJOUTE, elle ne remplace pas.
        assert _header(request, "Authorization") == "Bearer token-test"


# --- 3. Ack WebSocket --------------------------------------------------------

def test_le_jeton_est_joint_a_l_ack_websocket():
    adapter = _make_adapter()
    adapter._ws = _FakeWs()
    adapter._handle_hello_ack({"sessionToken": TOKEN_A})

    asyncio.run(adapter._send_ack("msg-1"))

    assert adapter._ws.sent == [
        {"type": "ack", "messageId": "msg-1", "sessionToken": TOKEN_A}
    ]


# --- 4. Repli silencieux (serveur anterieur, trame perdue) -------------------

def test_sans_jeton_le_plugin_se_comporte_exactement_comme_avant(monkeypatch):
    """Aucun hello.ack, ou un hello.ack sans jeton : ni en-tete, ni erreur."""
    adapter = _make_adapter()
    adapter._ws = _FakeWs()

    # Trames degradees : rien ne doit lever, rien ne doit etre memorise.
    for frame in ({"type": "hello.ack"},
                  {"type": "hello.ack", "sessionToken": None},
                  {"type": "hello.ack", "sessionToken": ""},
                  {"type": "hello.ack", "sessionToken": "   "},
                  {"type": "hello.ack", "sessionToken": 42}):
        adapter._handle_hello_ack(frame)
        assert adapter.session_token is None

    requests = _capture_http(monkeypatch)

    async def run():
        await adapter.send("demo", "bonjour")
        await adapter.vault_list("demo")
        await adapter._send_ack("msg-1")

    asyncio.run(run())

    for request in requests:
        assert _header(request, "x-hermes-session") is None
        assert _header(request, "Authorization") == "Bearer token-test"
    assert adapter._ws.sent == [{"type": "ack", "messageId": "msg-1"}]


# --- 5. Reconnexion ----------------------------------------------------------

def test_une_reconnexion_remplace_le_jeton_precedent():
    """Un nouveau hello invalide le precedent cote serveur : ne pas le garder."""
    adapter = _make_adapter()
    adapter._handle_hello_ack({"sessionToken": TOKEN_A})

    # Nouveau hello (reconnexion) : le jeton precedent est immediatement oublie,
    # sans attendre l'ack — sinon on continuerait a presenter un jeton revoque.
    adapter._forget_session_token()
    assert adapter.session_token is None

    adapter._handle_hello_ack({"sessionToken": TOKEN_B})
    assert adapter.session_token == TOKEN_B


# --- 6. Perte de session en vol ----------------------------------------------

def _fail_http(monkeypatch, status, reason="Conflict"):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, status, reason, {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def test_un_409_est_transitoire_et_rejouable(monkeypatch):
    """Le 409 « emetteur indeterminable » ne doit JAMAIS jeter la production.

    La fermeture du WebSocket revoque la session cote serveur, et le plugin ne
    retrouve un jeton qu'au prochain hello.ack, apres tout le backoff de
    reconnexion. Pendant cette fenetre, chaque envoi sur un canal multi-agents
    recoit ce 409 : classe en definitif, il faisait disparaitre la reponse de
    l'agent (un redeploiement de l'app suffisait) avec un simple avertissement.
    """
    adapter = _make_adapter()
    _fail_http(monkeypatch, 409)

    result = asyncio.run(adapter.send("demo", "bonjour"))

    assert result.success is False
    assert result.retryable is True, "un 409 doit etre rejoue apres reconnexion"
    assert result.error_kind == "transient"


def test_un_refus_definitif_reste_definitif(monkeypatch):
    """Contre-epreuve : la garde ne rend pas tout rejouable."""
    adapter = _make_adapter()
    _fail_http(monkeypatch, 403, "Forbidden")

    result = asyncio.run(adapter.send("demo", "bonjour"))

    assert result.success is False
    assert result.retryable is False
    assert result.error_kind == "forbidden"
