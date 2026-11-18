# -*- coding: utf-8 -*-
"""Acces IA affecte dans Pulse Chat, APPLIQUE par le plugin (trame ``agent.ai``).

Affecter un compte IA a un agent dans Pulse n'autorise que la passerelle de
Pulse (``/api/agent/ai-gateway/<profil>/v1``) : si le ``config.yaml`` du bot
appelle encore un autre fournisseur, l'affectation ne sert a rien. L'app pousse
donc ce que le bot doit appeler, a chaque connexion et a chaque changement :

    {"type": "agent.ai", "profile": "zephyr", "routing": {
       "primary":  {"model": ..., "apiMode": "codex_responses", "path": "/api/agent/ai-gateway/zephyr/v1"},
       "fallback": {"model": ..., "apiMode": "anthropic_messages", "path": ".../fallback/v1"} | null}}

``routing: null`` : Pulse ne gere pas l'acces IA de ce bot, ce que le plugin
avait pose est RETIRE (le bot revient a son propre ``config.yaml``).

Aucun crochet de plugin ne change le modele d'Hermes. Le plugin ecrit donc la
COUCHE GEREE d'Hermes (``hermes_cli/managed_scope.py`` :
``$HERMES_MANAGED_DIR/config.yaml``, fusionnee feuille a feuille PAR-DESSUS le
``config.yaml`` de l'utilisateur, relue par signature de fichier a chaque
message — Hermes >= v2026.9.13 reconstruit alors l'agent sans redemarrage).

Ce qui est ecrit, et pourquoi :

- un fournisseur NOMME ``providers.pulse-ai`` (``base_url``, ``api_mode``,
  ``key_env``) : un ``provider: custom`` simple ignore ``codex_responses``
  vers une adresse non-OpenAI, un fournisseur nomme le garde ;
- ``model`` (``provider``, ``default``, ``base_url``, ``api_mode``) et
  ``model.api_key: ""`` : la fusion est feuille a feuille, une cle
  ``model.api_key`` de l'utilisateur (sa cle Anthropic) gagnerait sinon sur
  ``key_env`` ; la chaine vide la neutralise et laisse ``key_env`` fournir le secret
  (lu par le coffre de profil d'Hermes : jamais de ``${VAR}`` qui, non resolu,
  partirait tel quel comme cle) ;
- ``fallback_providers`` : ``[]`` sans fallback (une liste REMPLACE celle de
  l'utilisateur : aucun repli vers une autre cle), sinon une entree vers le
  fournisseur nomme ``pulse-ai-fallback``.

La cle n'est JAMAIS dans la trame ni dans le fichier : c'est le secret
d'agent ``pca_…`` (``PULSE_CHAT_AGENT_TOKEN``), nomme par ``key_env``.

Le fichier peut porter des cles de l'exploitant : le plugin n'en touche que
quatre (``OWNED_KEYS``), memorise dans un fichier d'etat ce qu'il a ecrit et ce
qu'il a remplace, et le RESTAURE au retrait — sauf si quelqu'un a modifie la
cle entre-temps, auquel cas elle est laissee telle quelle.

Partie PURE (aucune dependance Hermes) : ``parse_frame``, ``render_layer``,
``apply_layer``, ``remove_layer``. Les E/S (YAML, ecriture atomique) sont dans
``ManagedConfigWriter``, dossier injectable.
"""

import copy
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

#: Valeurs ``api_mode`` d'Hermes que la passerelle de Pulse sert.
API_MODES = frozenset({"chat_completions", "codex_responses", "anthropic_messages"})

PRIMARY_PROVIDER = "pulse-ai"
FALLBACK_PROVIDER = "pulse-ai-fallback"

#: Variable qui porte le secret d'agent ``pca_…`` (la cle de la passerelle).
KEY_ENV = "PULSE_CHAT_AGENT_TOKEN"

#: Cles (chemins) que le plugin possede dans la couche geree.
OWNED_KEYS: Tuple[Tuple[str, ...], ...] = (
    ("providers", PRIMARY_PROVIDER),
    ("providers", FALLBACK_PROVIDER),
    ("model",),
    ("fallback_providers",),
)

CONFIG_NAME = "config.yaml"
#: Etat du plugin, a cote du fichier gere : ce qu'il a ecrit et ce qu'il a remplace.
STATE_NAME = ".pulse-chat-ai.json"

_ABSENT = object()


class RoutingError(ValueError):
    """Trame ``agent.ai`` invalide : ignoree, rien n'est ecrit."""


@dataclass(frozen=True)
class Route:
    model: str
    api_mode: str
    path: str


@dataclass(frozen=True)
class Routing:
    primary: Route
    fallback: Optional[Route]


def _parse_route(raw: Any, label: str) -> Route:
    if not isinstance(raw, dict):
        raise RoutingError(f"{label} absent ou invalide")
    model = raw.get("model")
    api_mode = raw.get("apiMode")
    path = raw.get("path")
    if not isinstance(model, str) or not model.strip():
        raise RoutingError(f"{label}.model absent")
    if api_mode not in API_MODES:
        raise RoutingError(f"{label}.apiMode inconnu : {api_mode!r}")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or not path.rstrip("/").endswith("/v1")
        or any(c.isspace() for c in path)
    ):
        raise RoutingError(f"{label}.path invalide : {path!r}")
    return Route(model=model.strip(), api_mode=api_mode, path=path.rstrip("/"))


def parse_frame(data: Any) -> Optional[Routing]:
    """``Routing`` a appliquer, ``None`` pour ``routing: null`` (retrait).

    Leve ``RoutingError`` sur une trame qu'on ne sait pas appliquer (``apiMode``
    inconnu, chemin invalide, cle ``routing`` absente) : elle est ignoree.
    """
    if not isinstance(data, dict) or "routing" not in data:
        raise RoutingError("cle routing absente")
    raw = data["routing"]
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RoutingError("routing invalide")
    primary = _parse_route(raw.get("primary"), "primary")
    fallback_raw = raw.get("fallback")
    fallback = None if fallback_raw is None else _parse_route(fallback_raw, "fallback")
    return Routing(primary=primary, fallback=fallback)


def gateway_url(base_url: str, path: str) -> str:
    """Adresse de la passerelle : base de ``PULSE_CHAT_URL`` + chemin de la trame."""
    return base_url.rstrip("/") + path


def render_layer(routing: Routing, base_url: str) -> Dict[str, Any]:
    """Valeurs des cles possedees (``OWNED_KEYS``) ; absente = a retirer."""
    primary_url = gateway_url(base_url, routing.primary.path)
    layer: Dict[str, Any] = {
        ("providers", PRIMARY_PROVIDER): {
            "base_url": primary_url,
            "api_mode": routing.primary.api_mode,
            "key_env": KEY_ENV,
        },
        ("model",): {
            "provider": PRIMARY_PROVIDER,
            "default": routing.primary.model,
            "base_url": primary_url,
            "api_mode": routing.primary.api_mode,
            "api_key": "",
        },
        ("fallback_providers",): [],
    }
    if routing.fallback is not None:
        layer[("providers", FALLBACK_PROVIDER)] = {
            "base_url": gateway_url(base_url, routing.fallback.path),
            "api_mode": routing.fallback.api_mode,
            "key_env": KEY_ENV,
        }
        layer[("fallback_providers",)] = [
            {"provider": FALLBACK_PROVIDER, "model": routing.fallback.model}
        ]
    return layer


def _get(doc: Dict[str, Any], path: Tuple[str, ...]) -> Any:
    node: Any = doc
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return _ABSENT
        node = node[part]
    return node


def _set(doc: Dict[str, Any], path: Tuple[str, ...], value: Any) -> None:
    node = doc
    for part in path[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[path[-1]] = copy.deepcopy(value)


def _delete(doc: Dict[str, Any], path: Tuple[str, ...]) -> None:
    parents = [doc]
    node: Any = doc
    for part in path[:-1]:
        if not isinstance(node, dict) or not isinstance(node.get(part), dict):
            return
        node = node[part]
        parents.append(node)
    node.pop(path[-1], None)
    # Un parent vide (``providers: {}``) est retire avec la cle.
    for depth in range(len(path) - 1, 0, -1):
        if parents[depth]:
            break
        parents[depth - 1].pop(path[depth - 1], None)


def _key(path: Tuple[str, ...]) -> str:
    return ".".join(path)


def apply_layer(
    doc: Dict[str, Any], state: Dict[str, Any], layer: Dict[Tuple[str, ...], Any]
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """``(document, etat)`` apres application de ``layer`` sur les cles possedees.

    L'etat retient pour chaque cle ce que le plugin a ECRIT (``written``) et ce
    qu'il a REMPLACE (``previous``, absent = rien). Une cle deja a nous garde
    son ``previous`` d'origine ; une cle modifiee par un tiers depuis notre
    ecriture redevient la valeur a restaurer.
    """
    doc = copy.deepcopy(doc) if isinstance(doc, dict) else {}
    written = dict((state or {}).get("written") or {})
    previous = dict((state or {}).get("previous") or {})
    for path in OWNED_KEYS:
        key = _key(path)
        current = _get(doc, path)
        ours = key in written and current is not _ABSENT and current == written[key]
        if not ours:
            # La valeur actuelle (tiers, ou rien) est celle a rendre au retrait.
            if current is _ABSENT:
                previous.pop(key, None)
            else:
                previous[key] = copy.deepcopy(current)
        if path in layer:
            _set(doc, path, layer[path])
            written[key] = copy.deepcopy(layer[path])
        elif ours:
            _restore(doc, path, previous)
            written.pop(key, None)
        else:
            written.pop(key, None)
            previous.pop(key, None)
    return doc, _state(written, previous)


def _restore(doc: Dict[str, Any], path: Tuple[str, ...], previous: Dict[str, Any]) -> None:
    key = _key(path)
    if key in previous:
        _set(doc, path, previous.pop(key))
    else:
        _delete(doc, path)


def remove_layer(doc: Dict[str, Any], state: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Retire ce que le plugin avait pose et restaure ce qu'il avait remplace.

    Une cle modifiee par un tiers depuis l'ecriture du plugin n'est pas touchee.
    """
    doc = copy.deepcopy(doc) if isinstance(doc, dict) else {}
    written = dict((state or {}).get("written") or {})
    previous = dict((state or {}).get("previous") or {})
    for path in OWNED_KEYS:
        key = _key(path)
        if key not in written:
            continue
        if _get(doc, path) == written[key]:
            _restore(doc, path, previous)
        written.pop(key, None)
        previous.pop(key, None)
    return doc, _state({}, {})


def _state(written: Dict[str, Any], previous: Dict[str, Any]) -> Dict[str, Any]:
    if not written and not previous:
        return {}
    return {"written": written, "previous": previous}


# --------------------------------------------------------------------------- E/S


class YamlUnavailable(RuntimeError):
    """Aucune bibliotheque YAML importable : rien n'est lu ni ecrit."""


def yaml_codec() -> Any:
    """Module offrant ``safe_load`` / ``safe_dump`` (memes arguments que PyYAML).

    D'abord ``hermes_yaml`` : l'interface YAML d'Hermes lui-meme (ruamel, YAML
    1.1, celle qui relit la couche geree). Les Hermes supportes (v0.21.6 a
    v0.23.x) n'embarquent PAS PyYAML : un ``import yaml`` echouait dans leur
    environnement et l'acces IA n'etait jamais applique (1.27.0). PyYAML ne
    sert qu'hors Hermes (tests unitaires, Hermes plus ancien).
    """
    try:
        import hermes_yaml

        return hermes_yaml
    except ImportError:
        pass
    try:
        import yaml

        return yaml
    except ImportError as exc:
        raise YamlUnavailable("ni hermes_yaml (Hermes) ni PyYAML importables") from exc


def _atomic_write(path: Path, text: str) -> None:
    """Fichier temporaire dans le MEME dossier + ``os.replace`` : le cache par
    signature d'Hermes ne lit jamais un demi-fichier."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ManagedConfigWriter:
    """Lit-modifie-ecrit ``<dossier>/config.yaml`` et l'etat du plugin."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.config_path = self.directory / CONFIG_NAME
        self.state_path = self.directory / STATE_NAME

    def _read_doc(self) -> Dict[str, Any]:
        yaml = yaml_codec()
        try:
            text = self.config_path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return {}
        loaded = yaml.safe_load(text)
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{self.config_path} : la racine n'est pas un dictionnaire")
        return loaded

    def _read_state(self) -> Dict[str, Any]:
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except ValueError:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _write(self, doc: Dict[str, Any], state: Dict[str, Any], before: Dict[str, Any]) -> bool:
        yaml = yaml_codec()
        changed = doc != before
        if changed:
            if doc:
                _atomic_write(
                    self.config_path,
                    yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False),
                )
            else:
                # Fichier vide => Hermes le lit comme absent ; on le retire.
                try:
                    self.config_path.unlink()
                except FileNotFoundError:
                    pass
        if state:
            if state != self._read_state():
                _atomic_write(self.state_path, json.dumps(state, ensure_ascii=False, indent=2))
        else:
            try:
                self.state_path.unlink()
            except FileNotFoundError:
                pass
        return changed

    def apply(self, routing: Routing, base_url: str) -> bool:
        """Ecrit la couche ; ``True`` si ``config.yaml`` a change."""
        before = self._read_doc()
        doc, state = apply_layer(before, self._read_state(), render_layer(routing, base_url))
        return self._write(doc, state, before)

    def remove(self) -> bool:
        """Retire la couche ; ``True`` si ``config.yaml`` a change."""
        state = self._read_state()
        if not state and not self.config_path.exists():
            return False
        before = self._read_doc()
        doc, new_state = remove_layer(before, state)
        return self._write(doc, new_state, before)


# --------------------------------------------------------------- dossier gere

MANAGED_DIR_ENV = "HERMES_MANAGED_DIR"
SYSTEM_MANAGED_DIR = Path("/etc/hermes")
PLUGIN_MANAGED_SUBDIR = "pulse-managed"


def resolve_managed_dir(
    hermes_home: Optional[Path], *, create: bool, environ: Optional[Dict[str, str]] = None,
    system_dir: Path = SYSTEM_MANAGED_DIR,
) -> Tuple[Optional[Path], str]:
    """``(dossier, raison)`` de la couche geree a ecrire ; dossier ``None`` = ne rien ecrire.

    - ``HERMES_MANAGED_DIR`` pose : il est utilise (cree s'il le faut, Hermes
      exige qu'il existe) ;
    - sinon ``/etc/hermes`` existe : c'est la couche de l'EXPLOITANT, Hermes la
      lit et un autre dossier la masquerait — rien n'est ecrit ;
    - sinon ``<HERMES_HOME>/pulse-managed``, et ``HERMES_MANAGED_DIR`` est pose
      dans l'environnement du processus (``get_managed_dir()`` le relit a chaque
      appel). Sans ``create``, seulement s'il existe deja (chargement du plugin).
    """
    env = os.environ if environ is None else environ
    override = (env.get(MANAGED_DIR_ENV) or "").strip()
    if override:
        path = Path(override)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return (path if path.is_dir() else None), "HERMES_MANAGED_DIR"
    if system_dir.is_dir():
        return None, f"{system_dir} existe (couche geree de l'exploitant)"
    if hermes_home is None:
        return None, "HERMES_HOME introuvable"
    path = Path(hermes_home) / PLUGIN_MANAGED_SUBDIR
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        return None, "pas encore de couche posee par le plugin"
    env[MANAGED_DIR_ENV] = str(path)
    return path, f"{path} (HERMES_MANAGED_DIR pose par le plugin)"
