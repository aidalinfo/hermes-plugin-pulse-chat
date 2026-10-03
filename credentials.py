# -*- coding: utf-8 -*-
"""Identifiants d'agent — relais du coffre « Passwords & Logins » d'Hermes
(``agent/vault_store.py``, Hermes >= v2026.9.11) vers Pulse Chat (docs/48 de
l'app).

PUR TRANSPORT : aucune regle ici. Qui a le droit (admin d'instance), la
fraicheur de session, le debit, les doublons : tout vit dans l'app. Ce module
ne fait que traduire une commande ``credentials.command`` en appel du coffre,
et le coffre en instantane de METADONNEES.

Invariants :
  - la verite vit DANS LE BOT : l'app ne garde que l'instantane (metadonnees) ;
  - un SECRET (mot de passe, cle TOTP, code) ne sort de ce module que dans la
    VALEUR rendue par ``execute`` pour ``reveal`` — que l'adaptateur poste tel
    quel dans le corps HTTP de la reponse. Jamais dans un journal, jamais dans
    un message d'erreur (``VaultError`` est sur par contrat d'Hermes, et il est
    nettoye quand meme), jamais dans ``metrics`` ;
  - les fonctions sont SYNCHRONES (le coffre verrouille un fichier) :
    l'adaptateur les appelle par ``asyncio.to_thread``, jamais sur la boucle
    du WebSocket.

Le mot ``vault`` designe deja, dans ce plugin, le coffre de FICHIERS des
canaux (``vault.py``) : d'ou ``credentials`` ici.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Cle de fonction annoncee dans ``capabilities.features`` du ``hello``.
CREDENTIALS_FEATURE = "credentials"

#: Prefixe des poignees du coffre LOCAL (``LocalLoginBackend.prefix``). Seules
#: ces entrees se suppriment et se revelent depuis Pulse.
LOCAL_PREFIX = "vault_"

#: Sources que l'app connait (``CREDENTIAL_SOURCES``) — une autre est ignoree.
_KNOWN_SOURCES = ("local", "onepassword", "bitwarden")
_KINDS = ("login", "payment", "address")
_IDENTIFIER_TYPES = ("email", "phone", "username")
_MAX_ITEMS = 1000


def credentials_available() -> bool:
    """Le coffre d'Hermes est-il importable ? Decide l'annonce du ``hello``.

    Hermes < v2026.9.11 n'a pas ``agent.vault_store`` : la capacite n'est alors
    PAS annoncee, et l'ecran de l'app dit pourquoi au lieu d'un onglet vide.
    """
    try:
        import agent.vault_store  # noqa: F401
    except Exception:
        return False
    return True


def _scope(profile: Optional[str]):
    """Contexte HERMES_HOME du profil (meme regle que la fiche de capacites)."""
    from .capabilities import _profile_scope

    return _profile_scope(profile or "")


def _text(value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text[:limit] if text else None


def _item_from_meta(meta: Any, source: str) -> Optional[Dict[str, Any]]:
    """``VaultItemMeta`` -> ligne d'instantane. LISTE BLANCHE de champs : rien
    d'autre ne franchit la frontiere (le schema de l'app est ``.strict()``)."""
    handle = _text(getattr(meta, "id", None), 200)
    kind = getattr(meta, "kind", None)
    if not handle or kind not in _KINDS:
        return None
    item: Dict[str, Any] = {
        "handle": handle,
        "kind": kind,
        "label": _text(getattr(meta, "label", None), 500) or "",
        "origin": _text(getattr(meta, "origin", None), 500),
        "hasTotp": bool(getattr(meta, "has_otp", False)),
        "source": source,
    }
    identifier = _text(getattr(meta, "identifier", None), 500)
    if identifier:
        item["identifier"] = identifier
        id_type = getattr(meta, "identifier_type", None)
        if id_type in _IDENTIFIER_TYPES:
            item["identifierType"] = id_type
    created = _text(getattr(meta, "created_at", None), 64)
    if created:
        item["createdAt"] = created
    return item


def collect_snapshot(profile: Optional[str] = None) -> List[Dict[str, Any]]:
    """Metadonnees de TOUTES les sources disponibles sans invite. BLOQUANT.

    Un gestionnaire externe (1Password, Bitwarden) verrouille ne contribue
    rien : le deverrouiller demanderait une invite que cette plateforme n'a pas.
    Une source en echec est sautee (journalisee sans detail) — l'instantane des
    autres part quand meme.
    """
    items: List[Dict[str, Any]] = []
    with _scope(profile):
        try:
            from agent.vault_backends import enabled_backends

            backends = list(enabled_backends())
        except Exception:
            # Vieille forme sans `vault_backends` : le coffre local seul.
            from agent.vault_store import get_vault_store

            for meta in get_vault_store().list_items():
                item = _item_from_meta(meta, "local")
                if item:
                    items.append(item)
            return items[:_MAX_ITEMS]
        for backend in backends:
            source = getattr(backend, "name", "")
            if source not in _KNOWN_SOURCES:
                continue
            try:
                if getattr(backend, "needs_unlock", False) and not backend.is_unlocked():
                    continue
                metas = backend.list_items()
            except Exception as exc:
                logger.warning(
                    "Pulse Chat: identifiants — source %s illisible (%s)",
                    source,
                    type(exc).__name__,
                )
                continue
            for meta in metas:
                item = _item_from_meta(meta, source)
                if item:
                    items.append(item)
    return items[:_MAX_ITEMS]


def _failure(code: str, message: str) -> Dict[str, Any]:
    return {"ok": False, "code": code, "message": message[:300]}


def _safe_message(exc: BaseException, secrets: Dict[str, Any]) -> str:
    """Message d'une ``VaultError`` (sure par contrat d'Hermes), nettoyee quand
    meme de toute valeur secrete connue — ceinture et bretelles."""
    text = str(exc)
    try:
        from agent.vault_store import scrub_secret_from_text

        return scrub_secret_from_text(text, secrets)
    except Exception:
        for value in secrets.values():
            if isinstance(value, str) and len(value) >= 3:
                text = text.replace(value, "[REDACTED]")
        return text


def _add(command: Dict[str, Any]) -> Dict[str, Any]:
    from agent.vault_store import VaultError, get_vault_store

    password = command.get("password")
    totp = command.get("totp")
    secret: Dict[str, Any] = {
        "identifier_type": command.get("identifierType"),
        "identifier": command.get("identifier"),
        "password": password,
    }
    # Nom EXACT du champ chez Hermes (v2026.9.11, `VaultStore.add_item`) :
    # `otp_secret` — base32 ou lien otpauth://totp, normalise par Hermes.
    if totp:
        secret["otp_secret"] = totp
    try:
        meta = get_vault_store().add_item(
            "login", str(command.get("label") or ""), secret, origin=command.get("origin")
        )
    except VaultError as exc:
        return _failure("invalid", _safe_message(exc, {"password": password, "totp": totp}))
    return {"ok": True, "value": {"handle": str(meta.id)}}


def _remove(command: Dict[str, Any]) -> Dict[str, Any]:
    from agent.vault_store import get_vault_store

    handle = str(command.get("handle") or "")
    if not handle.startswith(LOCAL_PREFIX):
        return _failure("not_allowed", "only local vault entries can be removed from Pulse Chat")
    if not get_vault_store().remove_item(handle):
        return _failure("not_found", "no such vault item")
    return {"ok": True}


def _reveal(command: Dict[str, Any]) -> Dict[str, Any]:
    from agent.vault_store import get_vault_store, totp_now

    handle = str(command.get("handle") or "")
    what = command.get("what")
    if not handle.startswith(LOCAL_PREFIX):
        return _failure("not_allowed", "only local vault entries can be revealed from Pulse Chat")
    store = get_vault_store()
    meta = store.get_meta(handle)
    if meta is None:
        return _failure("not_found", "no such vault item")
    if getattr(meta, "kind", None) != "login":
        return _failure("not_allowed", "only login entries can be revealed")
    secret = store.resolve_secret(handle)
    if what == "password":
        value = secret.get("password")
    elif what == "totp_code":
        # La CLE n'est jamais rendue : seulement le code courant.
        seed = str(secret.get("otp_secret") or "")
        if not seed:
            return _failure("invalid", "this login has no authenticator key")
        value = totp_now(seed)
    else:
        return _failure("invalid", "unknown reveal target")
    if not isinstance(value, str) or not value:
        return _failure("not_found", "empty secret")
    return {"ok": True, "value": value}


_OPS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "add": _add,
    "remove": _remove,
    "reveal": _reveal,
}


def execute(command: Dict[str, Any]) -> Dict[str, Any]:
    """Execute UNE commande ; rend ``{ok, value?}`` ou ``{ok: false, code,
    message}``. BLOQUANT. Ne leve jamais.

    Une exception imprevue rend son NOM de classe, jamais son texte : rien ne
    garantit qu'une erreur hors ``VaultError`` ne recopie pas une valeur.
    """
    op = command.get("op")
    handler = _OPS.get(op) if isinstance(op, str) else None
    if handler is None:
        return _failure("invalid", "unknown operation")
    if not credentials_available():
        return _failure("unavailable", "Hermes credential vault not available (Hermes >= v2026.9.11)")
    try:
        with _scope(command.get("profile")):
            return handler(command)
    except Exception as exc:  # pragma: no cover - filet
        logger.warning("Pulse Chat: identifiants — %s en echec (%s)", op, type(exc).__name__)
        return _failure("error", type(exc).__name__)


def is_mutation(command: Dict[str, Any]) -> bool:
    """Un ajout ou une suppression change l'instantane : il est repousse AVANT
    la reponse, pour que l'app relise la liste a jour."""
    return command.get("op") in ("add", "remove")
