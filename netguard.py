# -*- coding: utf-8 -*-
"""Garde-fous reseau du plugin (stdlib seule, sans hermes).

Trois regles, et chacune ferme une porte precise :

- **Transport** : le jeton de service, le secret d'agent, le jeton de session
  et les liens signes des medias ne voyagent JAMAIS en clair vers un hote
  public. ``http``/``ws`` ne sont acceptes que vers un hote LOCAL (loopback,
  reseau prive, nom de service Docker sans point, suffixes reserves) ou derriere
  ``PULSE_CHAT_ALLOW_INSECURE=1``, un reglage qu'il faut ecrire pour l'avoir.
- **Redirections** : aucune redirection ne quitte l'origine de la requete.
  ``urllib`` recopie tous les en-tetes de la requete d'origine sur la requete
  redirigee — ``Authorization`` compris : suivre un 302 vers un autre hote
  remettrait le Bearer a cet hote.
- **Schemas** : l'ouvreur n'a QUE les gestionnaires HTTP(S). ``urlopen`` par
  defaut sait aussi ouvrir ``file://``, ``ftp://`` et ``data:`` — une URL de
  media ``file:///etc/passwd`` aurait ete lue sur le disque du bot.

``redact_url`` sert aux journaux : la query d'un lien signe (``sig``/``exp``)
EST un droit de lecture tant qu'il n'a pas expire.
"""

from __future__ import annotations

import ipaddress
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable, Mapping, Optional, Set, Tuple

ALLOW_INSECURE_ENV = "PULSE_CHAT_ALLOW_INSECURE"
MEDIA_HOSTS_ENV = "PULSE_CHAT_MEDIA_HOSTS"

#: Suffixes qui ne designent jamais un hote de l'Internet public (RFC 6761,
#: RFC 8375, usages de LAN). ``.test`` couvre aussi les URL des tests.
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".test", ".home.arpa")
_SECURE_SCHEMES = {"https", "wss"}
_CLEAR_SCHEMES = {"http", "ws"}
_DEFAULT_PORTS = {"http": 80, "ws": 80, "https": 443, "wss": 443}
_TRUTHY = {"1", "true", "yes", "on"}


class InsecureTransportError(urllib.error.URLError):
    """Requete refusee : secret en clair vers un hote non local.

    Sous-classe d'``URLError`` pour que chaque appelant, qui rattrape deja les
    erreurs reseau, la traite comme une panne ordinaire — jamais comme une
    exception qui ferait tomber le tour de parole.
    """


def redact_url(url: str) -> str:
    """``scheme://hote[:port]/chemin`` — sans query, fragment ni identifiants."""
    try:
        parts = urllib.parse.urlsplit(str(url))
    except ValueError:
        return "<url illisible>"
    host = parts.hostname or ""
    if ":" in host:  # IPv6
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port else host
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def is_local_host(host: Optional[str]) -> bool:
    """Hote qui ne peut pas etre sur l'Internet public."""
    if not host:
        return False
    name = host.strip().strip("[]").rstrip(".").lower()
    if not name:
        return False
    if name == "localhost" or name.endswith(_LOCAL_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        # Nom SANS point : un nom de service Docker/Compose (`pulse-chat`), qui
        # ne se resout que sur le reseau interne.
        return "." not in name
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local)


#: Reglage lu par l'adaptateur via le magasin de secrets d'Hermes (profils
#: multiples), qui n'est pas forcement recopie dans ``os.environ``. Il ne peut
#: qu'OUVRIR : aucun appel ne le referme, pour qu'un second profil du meme
#: process ne coupe pas le premier en cours de route.
_CONFIG_ALLOWS_INSECURE = False


def is_truthy(value: object) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def allow_insecure_from_config(value: object) -> None:
    global _CONFIG_ALLOWS_INSECURE
    if is_truthy(value):
        _CONFIG_ALLOWS_INSECURE = True


def insecure_allowed(env: Optional[Mapping[str, str]] = None) -> bool:
    """``env`` explicite (tests) : lui SEUL fait foi."""
    if env is not None:
        return is_truthy(env.get(ALLOW_INSECURE_ENV, ""))
    return _CONFIG_ALLOWS_INSECURE or is_truthy(os.environ.get(ALLOW_INSECURE_ENV, ""))


def transport_allowed(url: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """Le transport de cette URL peut-il porter un secret ?"""
    try:
        parts = urllib.parse.urlsplit(str(url))
    except ValueError:
        return False
    scheme = parts.scheme.lower()
    if scheme in _SECURE_SCHEMES:
        return True
    if scheme in _CLEAR_SCHEMES:
        return is_local_host(parts.hostname) or insecure_allowed(env)
    return False


def origin(url: str) -> Tuple[str, str, Optional[int]]:
    parts = urllib.parse.urlsplit(str(url))
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        port = -1
    return scheme, (parts.hostname or "").lower(), port or _DEFAULT_PORTS.get(scheme)


def parse_hosts(raw: object) -> Set[str]:
    """``"a.fr, https://b.fr:8443/x"`` -> ``{"a.fr", "b.fr"}`` (hotes seuls)."""
    if isinstance(raw, str):
        items: Iterable[object] = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        return set()
    hosts: Set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        if "://" not in text:
            text = "//" + text
        try:
            host = urllib.parse.urlsplit(text).hostname
        except ValueError:
            host = None
        if host:
            hosts.add(host.lower())
    return hosts


def host_of(url: str) -> Optional[str]:
    try:
        host = urllib.parse.urlsplit(str(url)).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def media_url_refusal(
    url: str, allowed_hosts: Iterable[str], env: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    """Motif de refus d'une URL de media, ou ``None`` si elle peut etre suivie."""
    try:
        parts = urllib.parse.urlsplit(str(url))
    except ValueError:
        return "URL illisible"
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return f"schema {scheme or '(vide)'} refuse"
    host = (parts.hostname or "").lower()
    if not host:
        return "URL sans hote"
    if parts.username or parts.password:
        return "identifiants dans l'URL refuses"
    if host not in {h.lower() for h in allowed_hosts}:
        return f"hote {host} hors de la liste autorisee"
    if not transport_allowed(url, env):
        return "http en clair vers un hote non local"
    return None


class GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Ne suit une redirection que vers la MEME origine (schema, hote, port)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        if origin(target) != origin(req.full_url):
            raise urllib.error.HTTPError(
                redact_url(req.full_url),
                code,
                f"redirection refusee vers une autre origine ({redact_url(target)})",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, target)


class TransportGuard(urllib.request.BaseHandler):
    """Pre-traitement de chaque requete ``http`` (redirections comprises)."""

    handler_order = 100

    def http_request(self, req):
        if not transport_allowed(req.full_url):
            raise InsecureTransportError(
                f"http en clair refuse vers {redact_url(req.full_url)} — poser "
                f"{ALLOW_INSECURE_ENV}=1 pour l'autoriser explicitement"
            )
        return req


def build_guarded_opener() -> urllib.request.OpenerDirector:
    """Ouvreur HTTP(S) seul, redirections bornees a l'origine, clair borne au local."""
    opener = urllib.request.OpenerDirector()
    for handler in (
        # Proxies d'environnement HTTP(S) seulement : un ``ftp_proxy`` ferait
        # sinon passer une URL ``ftp://`` par le proxy HTTP.
        urllib.request.ProxyHandler(
            {k: v for k, v in urllib.request.getproxies().items() if k in ("http", "https")}
        ),
        urllib.request.UnknownHandler(),
        urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        GuardedRedirectHandler(),
        urllib.request.HTTPErrorProcessor(),
        TransportGuard(),
    ):
        opener.add_handler(handler)
    return opener
