# -*- coding: utf-8 -*-
"""Garde-fous reseau : schemas, hotes, redirections, clair, journaux.

Les redirections sont verifiees contre un VRAI serveur HTTP local (thread) :
c'est le gestionnaire de redirection d'urllib qui est en cause, un double
d'``urlopen`` ne prouverait rien.
"""

import asyncio
import http.server
import logging
import threading
import urllib.error
import urllib.request

import pytest

from test_adapter_dedup import _load_adapter_module

adapter_module = _load_adapter_module()
netguard = __import__("pulse_chat_plugin_under_test.netguard", fromlist=["netguard"])

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class _Handler(http.server.BaseHTTPRequestHandler):
    routes = {}
    seen = []

    def do_GET(self):  # noqa: N802
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        route = type(self).routes.get(self.path.split("?")[0])
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        status, headers, body = route
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    _Handler.routes = {}
    _Handler.seen = []
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def _adapter(url, **extra):
    class _Config:
        pass

    _Config.extra = {"url": url, "token": "token-test", **extra}
    return adapter_module.PulseChatAdapter(_Config())


# --- Fonctions pures ----------------------------------------------------------


def test_redact_url_retire_query_fragment_et_identifiants():
    url = "https://user:pw@chat.example.fr:8443/api/attachments/a1/download?exp=1&sig=abc#x"
    assert netguard.redact_url(url) == "https://chat.example.fr:8443/api/attachments/a1/download"


@pytest.mark.parametrize(
    "host",
    ["localhost", "127.0.0.1", "10.0.0.5", "192.168.1.2", "172.18.0.3", "::1",
     "pulse-chat", "app.internal", "pulse-chat.test", "box.local"],
)
def test_hotes_locaux(host):
    assert netguard.is_local_host(host)


@pytest.mark.parametrize("host", ["chat.pulsemyit.fr", "8.8.8.8", "example.com", "", None])
def test_hotes_publics(host):
    assert not netguard.is_local_host(host)


def test_transport_clair_refuse_vers_hote_public_sauf_reglage_explicite():
    assert netguard.transport_allowed("https://chat.example.fr")
    assert netguard.transport_allowed("wss://chat.example.fr/ws")
    assert not netguard.transport_allowed("http://chat.example.fr", env={})
    assert not netguard.transport_allowed("ws://chat.example.fr/ws", env={})
    assert netguard.transport_allowed("http://chat.example.fr", env={"PULSE_CHAT_ALLOW_INSECURE": "1"})
    assert netguard.transport_allowed("http://localhost:4020", env={})
    assert not netguard.transport_allowed("ftp://chat.example.fr", env={"PULSE_CHAT_ALLOW_INSECURE": "1"})


@pytest.mark.parametrize(
    "url, fragment",
    [
        ("file:///etc/passwd", "schema file"),
        ("ftp://chat.example.fr/x.png", "schema ftp"),
        ("data:image/png;base64,AAAA", "schema data"),
        ("https://evil.example.com/x.png", "hote evil.example.com"),
        ("https://u:p@chat.example.fr/x.png", "identifiants"),
        ("http://chat.example.fr/x.png", "clair"),
    ],
)
def test_url_de_media_refusee(url, fragment):
    reason = netguard.media_url_refusal(url, {"chat.example.fr"}, env={})
    assert reason and fragment in reason


def test_url_de_media_acceptee():
    assert netguard.media_url_refusal(
        "https://chat.example.fr/api/attachments/a/download?sig=x", {"chat.example.fr"}, env={}
    ) is None


def test_parse_hosts():
    assert netguard.parse_hosts("a.fr, https://B.fr:8443/x ,") == {"a.fr", "b.fr"}
    assert netguard.parse_hosts(["c.fr"]) == {"c.fr"}
    assert netguard.parse_hosts(None) == set()


# --- WebSocket ----------------------------------------------------------------


def test_ws_url_https_donne_wss():
    assert adapter_module._ws_url("https://chat.example.fr/") == "wss://chat.example.fr/ws/hermes"


def test_ws_url_clair_refuse_vers_hote_public(monkeypatch):
    monkeypatch.delenv("PULSE_CHAT_ALLOW_INSECURE", raising=False)
    with pytest.raises(netguard.InsecureTransportError):
        adapter_module._ws_url("http://chat.example.fr")


def test_ws_url_clair_accepte_en_local_ou_explicitement(monkeypatch):
    monkeypatch.delenv("PULSE_CHAT_ALLOW_INSECURE", raising=False)
    assert adapter_module._ws_url("http://localhost:4020") == "ws://localhost:4020/ws/hermes"
    assert adapter_module._ws_url("http://pulse-chat:3000") == "ws://pulse-chat:3000/ws/hermes"
    monkeypatch.setenv("PULSE_CHAT_ALLOW_INSECURE", "1")
    assert adapter_module._ws_url("http://chat.example.fr") == "ws://chat.example.fr/ws/hermes"


def test_ws_url_autre_schema_refuse():
    with pytest.raises(ValueError):
        adapter_module._ws_url("ftp://chat.example.fr")


def test_connect_refuse_le_clair_sans_toucher_au_reseau(monkeypatch):
    monkeypatch.delenv("PULSE_CHAT_ALLOW_INSECURE", raising=False)
    adapter = _adapter("http://chat.example.fr")
    assert asyncio.run(adapter.connect()) is False
    kind, _message, retryable = adapter.last_error
    assert kind == "insecure_transport" and retryable is False


# --- Appels HTTP porteurs du jeton --------------------------------------------


def test_post_http_clair_vers_hote_public_refuse_avant_envoi(monkeypatch):
    """Le jeton ne part pas : refus AVANT la connexion (aucun DNS necessaire)."""
    monkeypatch.delenv("PULSE_CHAT_ALLOW_INSECURE", raising=False)
    adapter = _adapter("http://chat.example.invalid-public.fr")
    status, body = adapter._http_detailed("GET", adapter.base_url + "/api/agent/x")
    assert status == 0 and "en clair refuse" in body["message"]


def test_file_scheme_refuse_par_l_ouvreur():
    with pytest.raises(urllib.error.URLError):
        adapter_module._urlopen(urllib.request.Request("file:///etc/passwd"), timeout=2)


def test_redirection_vers_autre_origine_refusee_et_bearer_non_transmis(server):
    port = server
    # 127.0.0.1 -> localhost : meme machine, AUTRE origine au sens d'urllib.
    _Handler.routes = {
        "/api/agent/x": (302, {"Location": f"http://localhost:{port}/steal"}, b""),
        "/steal": (200, {"Content-Type": "application/json"}, b"{}"),
    }
    adapter = _adapter(f"http://127.0.0.1:{port}")
    status, _body = adapter._http_detailed("GET", f"{adapter.base_url}/api/agent/x")
    assert status == 302
    assert [path for path, _ in _Handler.seen] == ["/api/agent/x"]


def test_redirection_meme_origine_suivie(server):
    port = server
    _Handler.routes = {
        "/api/agent/x": (302, {"Location": "/api/agent/y"}, b""),
        "/api/agent/y": (200, {"Content-Type": "application/json"}, b'{"ok": true}'),
    }
    adapter = _adapter(f"http://127.0.0.1:{port}")
    status, body = adapter._http_detailed("GET", f"{adapter.base_url}/api/agent/x")
    assert status == 200 and body == {"ok": True}
    assert _Handler.seen[1] == ("/api/agent/y", "Bearer token-test")


# --- Medias -------------------------------------------------------------------


def test_media_d_un_autre_hote_refuse_sans_reseau_et_sans_signature_au_journal(caplog, server):
    adapter = _adapter(f"http://127.0.0.1:{server}")
    url = "https://evil.example.com/x.png?exp=123&sig=SECRETSIG"
    with caplog.at_level(logging.WARNING):
        assert adapter._download_one(url) == (None, None)
    assert "SECRETSIG" not in caplog.text and "exp=" not in caplog.text
    assert "PULSE_CHAT_MEDIA_HOSTS" in caplog.text
    assert _Handler.seen == []


def test_media_file_scheme_refuse(server):
    adapter = _adapter(f"http://127.0.0.1:{server}")
    assert adapter._download_one("file:///etc/passwd") == (None, None)


def test_media_de_l_hote_de_l_app_telecharge(server):
    _Handler.routes = {"/api/attachments/a/download": (200, {"Content-Type": "image/png"}, PNG)}
    adapter = _adapter(f"http://127.0.0.1:{server}")
    path, mime = adapter._download_one(
        f"http://127.0.0.1:{server}/api/attachments/a/download?exp=1&sig=s"
    )
    assert mime == "image/png"
    with open(path, "rb") as handle:
        assert handle.read() == PNG


def test_media_hote_supplementaire_autorise_par_reglage(server):
    _Handler.routes = {"/x.png": (200, {"Content-Type": "image/png"}, PNG)}
    adapter = _adapter("https://chat.example.fr", media_hosts="127.0.0.1")
    path, mime = adapter._download_one(f"http://127.0.0.1:{server}/x.png")
    assert path and mime == "image/png"


def test_media_redirection_vers_autre_hote_refusee(server):
    port = server
    _Handler.routes = {
        "/api/attachments/a/download": (302, {"Location": f"http://localhost:{port}/x.png"}, b""),
        "/x.png": (200, {"Content-Type": "image/png"}, PNG),
    }
    adapter = _adapter(f"http://127.0.0.1:{port}")
    with pytest.raises(urllib.error.HTTPError):
        adapter._download_one(f"http://127.0.0.1:{port}/api/attachments/a/download?sig=s")
    assert [path for path, _ in _Handler.seen] == ["/api/attachments/a/download?sig=s"]


def test_media_trop_gros_journalise_sans_la_query(caplog, monkeypatch, server):
    _Handler.routes = {"/big.png": (200, {"Content-Type": "image/png"}, PNG)}
    monkeypatch.setattr(adapter_module, "_MEDIA_MAX_BYTES", 8)
    adapter = _adapter(f"http://127.0.0.1:{server}")
    with caplog.at_level(logging.WARNING):
        assert adapter._download_one(f"http://127.0.0.1:{server}/big.png?exp=9&sig=SECRETSIG") == (
            None,
            None,
        )
    assert "trop volumineux" in caplog.text
    assert "SECRETSIG" not in caplog.text and "/big.png" in caplog.text
