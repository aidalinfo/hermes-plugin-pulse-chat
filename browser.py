# -*- coding: utf-8 -*-
"""Fournisseur de navigateur ``pulse`` et outil ``pulse_browser_handoff``.

Le navigateur des agents est HEBERGE par l'app Pulse Chat (service
``browser-runner``) : un Chromium par session, avec les cookies de la personne
qui a prete son profil a l'agent, visible en direct dans le canal, et dont un
humain peut prendre la main. Ce module branche ce Chromium sur les outils de
navigation d'Hermes — ``browser_exec`` (Browser Use) comme ``browser_*``
(agent-browser) empruntent tous deux le fournisseur actif — SANS rien modifier
d'Hermes : il rend une URL CDP, Hermes pilote.

Le plugin reste MINCE : il ne choisit ni le profil, ni le pret, ni qui peut
prendre la main — l'app decide de tout (spec § 5). Il traduit
``create_session`` / ``close_session`` en appels HTTP, et relaie la trame
``browser.control`` a l'outil qui l'attend.

Contrat app (spec § 5.2, § 5.3, § 5.7) :

    POST   /api/agent/browser/sessions              {channel}
           -> {sessionId, cdpUrl, profile: {kind, label}}
           501 browser_unavailable · 503 browser_capacity ·
           403 agent_credential_required
    DELETE /api/agent/browser/sessions/:id          (LIBERE, idempotent)
    POST   /api/agent/browser/sessions/:id/handoff  {reason}

    app -> plugin   trame WS
        {type: "browser.control", sessionId, controller: "agent"|"human", by}

Contrat Hermes (v2026.9.24, ``agent/browser_provider.py``) — verifie dans le
source, et chaque point a une consequence ici :

  - ``is_available()`` ne fait AUCUN appel reseau. C'est aussi le ``check_fn``
    des outils ``browser_*`` en mode cloud (``check_browser_requirements``) :
    FAUX retire les outils du schema — il n'y a pas de repli local a ce
    niveau-la (cf. ``_on_unavailable``).
  - ``create_session(task_id)`` tourne dans le fil de l'OUTIL, ou les
    ``ContextVar`` de la passerelle sont propagees : le canal s'y lit. S'il
    leve, Hermes retombe SANS BRUIT sur son Chromium local
    (``_create_cloud_session_or_fallback``).
  - ``close_session`` / ``emergency_cleanup`` tournent sur le fil du concierge
    (ou a ``atexit``), SANS contexte de session : tout ce qu'il faut tient dans
    ``bb_session_id`` (nom historique, il porte l'identifiant Pulse). Ils ne
    levent JAMAIS.
  - Nom ``pulse``, jamais ``browser-use`` : Browser Use saute le fournisseur
    qui porte exactement ce nom (``tools/browser_use_cli.py``).
"""

import concurrent.futures
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

try:  # pragma: no cover - chemin runtime Hermes
    from agent.browser_provider import BrowserProvider
except Exception:  # hors Hermes (pytest), ou Hermes anterieur aux fournisseurs de navigateur
    # Base de repli SANS abstraction : elle ne sert qu'a rendre le module
    # importable. ``register_browser_provider`` verifie le type contre la VRAIE
    # classe d'Hermes — sur un Hermes qui ne l'a pas, la methode n'existe pas
    # non plus, et l'enregistrement est simplement saute.
    class BrowserProvider:  # type: ignore[no-redef]
        pass


logger = logging.getLogger(__name__)

#: Valeur de ``browser.cloud_provider`` qui selectionne ce fournisseur.
PROVIDER_NAME = "pulse"

#: Nom de l'outil. PREFIXE, comme ``pulse_request_approval`` : ``register_tool``
#: sans ``override`` rend None sur un nom pris, sans lever.
HANDOFF_TOOL_NAME = "pulse_browser_handoff"

#: Attente dans l'outil. Un outil de plugin est coupe par Hermes (300 s pour un
#: outil asynchrone, 420 s pour un lot synchrone) : 270 s reste sous les deux,
#: et c'est la valeur de l'app (``BROWSER_HANDOFF_WAIT_MS``).
HANDOFF_WAIT_SECONDS = 270

#: Apres un 501, duree pendant laquelle on ne redemande plus rien a l'app.
UNAVAILABLE_BACKOFF_SECONDS = 300

#: Plafonds reseau. L'ouverture lance un Chromium et y injecte l'etat de
#: session : elle peut prendre plusieurs secondes. La liberation, elle, tourne
#: sur le fil du concierge, qui ne doit pas rester bloque ; ``atexit`` encore
#: moins.
CREATE_TIMEOUT_SECONDS = 60.0
RELEASE_TIMEOUT_SECONDS = 10.0
EMERGENCY_TIMEOUT_SECONDS = 5.0
HANDOFF_POST_TIMEOUT_SECONDS = 15.0

#: Borne du motif. Il est affiche tel quel dans le panneau et dans une
#: notification push : une phrase suffit.
MAX_REASON_LENGTH = 500

#: Sessions retenues par tache (borne : un oubli de ``close_session`` ne doit
#: pas faire grossir la table indefiniment).
MAX_TRACKED_SESSIONS = 64

#: Nom de plateforme de ce plugin, tel que la passerelle le pose dans
#: ``HERMES_SESSION_PLATFORM`` (``Platform("pulse_chat").value``).
PLATFORM_NAME = "pulse_chat"

CONTROLLERS = ("agent", "human")

SESSIONS_PATH = "/api/agent/browser/sessions"

#: ``(methode, chemin, charge JSON ou None, timeout) -> (statut, corps)``.
#: Statut ``0`` = l'app n'a pas repondu. Ne leve pas (l'appelant se protege
#: quand meme : c'est lui qui promet de ne jamais lever).
HttpFn = Callable[[str, str, Optional[Dict[str, Any]], float], Tuple[int, Any]]


HANDOFF_TOOL_DESCRIPTION = (
    "Demande a un HUMAIN de prendre la main sur ton navigateur Pulse Chat, et "
    "ATTEND qu'il te la rende. A appeler quand la page exige quelqu'un que tu ne "
    "peux pas etre : se connecter (identifiant, mot de passe), passer un captcha, "
    "saisir un code recu par SMS ou par e-mail, valider une double "
    "authentification, accepter une condition au nom de la personne. Le "
    "navigateur est visible en direct dans la conversation ; la personne qui "
    "peut debloquer est prevenue par une notification. Ne tente JAMAIS de "
    "deviner un mot de passe ni de contourner un captcha. Le navigateur doit "
    "deja etre ouvert (ouvre d'abord une page avec tes outils de navigation). "
    "Issue : `done` (la main t'est rendue : reprends ou tu en etais, en "
    "commencant par regarder la page — elle a change), `pending` (personne n'a "
    "rendu la main a temps : ARRETE-TOI, ne rappelle pas l'outil en boucle, "
    "ecris a l'humain ce que tu attends de lui ; il te relancera), `refused` "
    "(lis `code` et `message`). Tant qu'un humain a la main, tes commandes de "
    "navigation sont refusees (`pulse:human_in_control`) : c'est normal."
)

HANDOFF_TOOL_SCHEMA: Dict[str, Any] = {
    "name": HANDOFF_TOOL_NAME,
    "description": HANDOFF_TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "maxLength": MAX_REASON_LENGTH,
                "description": (
                    "Ce que l'humain doit faire, en une phrase, a la deuxieme "
                    "personne (ex. « Connecte-toi a LinkedIn, je reprendrai sur "
                    "ta messagerie », « Saisis le code recu par SMS »). "
                    "C'est le texte de la notification et du panneau."
                ),
            },
        },
        "required": ["reason"],
    },
}


# ── Resultats rendus au modele ───────────────────────────────────────────

_ADVICE = {
    "no_browser_session": (
        "Ouvre d'abord le navigateur : charge la page concernee avec tes outils de "
        "navigation (browser_navigate, ou browser_exec), puis rappelle "
        "pulse_browser_handoff. Si tu navigues deja, le navigateur n'est pas celui "
        "de Pulse Chat (reglage du bot) : dis a l'humain ce qu'il doit faire, par ecrit."
    ),
    "no_reason": "Donne un `reason` : une phrase qui dit a l'humain quoi faire.",
    "browser_session_not_found": (
        "Le navigateur a ete ferme entre-temps. Rouvre la page avec tes outils de "
        "navigation, puis rappelle pulse_browser_handoff."
    ),
    "not_connected": (
        "Pulse Chat est injoignable pour l'instant. Dis a l'humain, par ecrit, ce "
        "que tu attends de lui dans le navigateur."
    ),
    "not_sent": (
        "La demande n'a pas pu etre transmise. Dis a l'humain, par ecrit, ce que tu "
        "attends de lui dans le navigateur."
    ),
}


def _json(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def refused_result(code: str, message: str) -> str:
    return _json(
        {
            "status": "refused",
            "code": code,
            "message": message,
            "next": _ADVICE.get(code, _ADVICE["not_sent"]),
        }
    )


def done_result(by: Optional[str]) -> str:
    return _json(
        {
            "status": "done",
            "controller": "agent",
            "by": by,
            "next": (
                "La main t'est rendue. La page a pu changer (connexion faite, "
                "onglet ouvert) : regarde-la d'abord (capture ou instantane), "
                "puis reprends ta tache."
            ),
        }
    )


def pending_result() -> str:
    return _json(
        {
            "status": "pending",
            "next": (
                "Personne n'a rendu la main dans le delai. ARRETE-TOI : ne rappelle "
                "pas l'outil, n'insiste pas dans le navigateur. Ecris a l'humain ce "
                "que tu attends de lui (se connecter, saisir le code...) et qu'il "
                "te previenne quand c'est fait ; tu reprendras a ce moment-la."
            ),
        }
    )


def parse_error(status: int, body: Any) -> Dict[str, str]:
    """``{code, message}`` d'une reponse d'erreur de l'app (``createError``
    de Nuxt : ``statusMessage`` + ``data.code``), ou d'un echec de transport."""
    if status == 0:
        text = body.get("message") if isinstance(body, dict) else None
        return {"code": "not_connected", "message": str(text or "Pulse Chat injoignable")}
    code = "http_%d" % status
    message = "HTTP %d" % status
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, dict) and isinstance(data.get("code"), str) and data["code"]:
            code = data["code"]
        text = body.get("statusMessage") or body.get("message")
        if isinstance(text, str) and text.strip():
            message = text.strip()
    return {"code": code, "message": message}


def parse_control_frame(frame: Any) -> Optional[Dict[str, Any]]:
    """Trame ``browser.control`` exploitable, ou ``None``.

    Source reseau : on ne suppose jamais la forme recue. Un ``controller`` hors
    des deux valeurs connues est REJETE plutot que devine — le lire comme
    « agent » reveillerait un outil pendant qu'un humain tape son mot de passe.
    """
    if not isinstance(frame, dict) or frame.get("type") != "browser.control":
        return None
    session_id = frame.get("sessionId")
    controller = frame.get("controller")
    if not isinstance(session_id, str) or not session_id or controller not in CONTROLLERS:
        return None
    by = frame.get("by")
    return {
        "sessionId": session_id,
        "controller": controller,
        "by": by if isinstance(by, str) and by.strip() else None,
    }


def _task_prefix(key: str) -> str:
    """Tache d'une cle de session d'Hermes. Browser Use suffixe la sienne d'un
    ``@<profil servi>`` (``_backend_cache_key``) : sans cette reduction,
    l'outil ne retrouverait pas le navigateur ouvert par ``browser_exec``."""
    return key.split("@", 1)[0]


# ── Le fournisseur ───────────────────────────────────────────────────────


class PulseBrowserProvider(BrowserProvider):
    """``browser.cloud_provider: pulse`` — le Chromium heberge par Pulse Chat.

    Tout l'etat vit ici, sous UN verrou : ``create_session`` et l'outil tournent
    dans le fil d'outil, ``close_session`` dans celui du concierge, et
    ``on_control`` dans la boucle du WebSocket.
    """

    def __init__(
        self,
        http: HttpFn,
        *,
        configured: Callable[[], bool],
        session_env: Callable[[str], str],
        clock: Callable[[], float] = time.monotonic,
        wait_seconds: float = HANDOFF_WAIT_SECONDS,
    ) -> None:
        self._http = http
        self._configured = configured
        self._session_env = session_env
        self._clock = clock
        self._wait_seconds = wait_seconds
        self._lock = threading.Lock()
        # cle de session Hermes -> {"sessionId", "channel"} (ordre = anciennete)
        self._sessions: "OrderedDict[str, Dict[str, str]]" = OrderedDict()
        # sessionId -> nombre de ``create_session`` non encore liberes. Le tour
        # suivant reprend la MEME session chaude (spec § 5.3) : la liberation du
        # tour precedent, sur le fil du concierge, peut arriver APRES la reprise,
        # et ne doit pas effacer la cle toute fraiche du nouveau tour.
        self._open: Dict[str, int] = {}
        # sessionId -> Futures des outils en attente de la main.
        self._waiters: Dict[str, List["concurrent.futures.Future[Dict[str, Any]]"]] = {}
        # sessionId -> dernier humain annonce a la main (pour le rendre au modele).
        self._last_human: Dict[str, Optional[str]] = {}
        # Instant (horloge monotone) jusqu'auquel l'app a dit 501.
        self._unavailable_until: float = 0.0

    # -- Contrat BrowserProvider --------------------------------------------

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def display_name(self) -> str:
        return "Pulse Chat"

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "",
            "tag": "Navigateur heberge par Pulse Chat (profils par personne, vue en direct)",
            "env_vars": [],
        }

    def is_available(self) -> bool:
        """Configuration presente — jamais un appel reseau (contrat Hermes).

        Delibere : un 501 recent ne rend PAS faux. En mode cloud, cette methode
        est le ``check_fn`` des outils ``browser_*`` : faux, Hermes les RETIRE
        du schema (« no such tool »), il ne retombe pas sur son Chromium local.
        L'agent perdrait la navigation entiere, par intermittence, au lieu de
        naviguer comme avant. Le repli local, lui, s'obtient en faisant echouer
        ``create_session`` — c'est ce que fait la fenetre du 501.
        """
        try:
            return bool(self._configured())
        except Exception:
            return False

    def create_session(self, task_id: str) -> Dict[str, object]:
        """Ouvre (ou reprend) la session Pulse du canal courant.

        Toute erreur est un ``RuntimeError`` qui porte le motif de l'app : Hermes
        le journalise puis retombe sur son Chromium LOCAL, sans rien dire a
        personne — le message est la seule trace de « l'agent a navigue sans
        mon profil » (spec § 9).
        """
        platform = str(self._session_env("HERMES_SESSION_PLATFORM") or "").strip().lower()
        if platform and platform != PLATFORM_NAME:
            # Un bot peut servir plusieurs plateformes : une conversation
            # Telegram n'a pas de canal Pulse, poster son identifiant de chat
            # ne ferait qu'un 404 de plus.
            raise RuntimeError(
                "Navigateur Pulse Chat : la conversation vient de %r, pas de Pulse Chat" % platform
            )
        channel = str(self._session_env("HERMES_SESSION_CHAT_ID") or "").strip()
        if not channel:
            raise RuntimeError(
                "Navigateur Pulse Chat : aucune conversation Pulse Chat en cours "
                "(HERMES_SESSION_CHAT_ID vide) — le navigateur Pulse n'existe que dans un canal"
            )
        remaining = self._unavailable_remaining()
        if remaining > 0:
            # AUCUN appel reseau : l'app a dit « fonctionnalite absente » il y a
            # moins de 5 min, la redemander a chaque tour ne changerait rien.
            raise RuntimeError(
                "Navigateur Pulse Chat indisponible (browser_unavailable, encore %d s "
                "avant de redemander) — navigation locale" % int(remaining)
            )

        status, body = self._http("POST", SESSIONS_PATH, {"channel": channel}, CREATE_TIMEOUT_SECONDS)
        if not 200 <= status < 300:
            err = parse_error(status, body)
            if status == 501:
                self._on_unavailable()
            logger.warning(
                "Pulse Chat: navigateur refuse pour %s (HTTP %s %s) — Hermes navigue en LOCAL, sans profil",
                channel,
                status,
                err["code"],
            )
            raise RuntimeError(
                "Navigateur Pulse Chat refuse (%s, HTTP %s) : %s" % (err["code"], status, err["message"])
            )

        session_id = body.get("sessionId") if isinstance(body, dict) else None
        cdp_url = body.get("cdpUrl") if isinstance(body, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("Navigateur Pulse Chat : reponse sans sessionId")
        if not isinstance(cdp_url, str) or not cdp_url.startswith(("ws://", "wss://")):
            raise RuntimeError("Navigateur Pulse Chat : reponse sans URL CDP websocket")

        self._remember(str(task_id or "default"), session_id, channel)
        profile = body.get("profile") if isinstance(body.get("profile"), dict) else {}
        logger.info(
            "Pulse Chat: navigateur %s pour %s (profil %s)",
            session_id,
            channel,
            profile.get("kind") or "?",
        )
        # PAS d'``expires_at`` : la session n'a pas d'echeance fixe cote app
        # (elle se ferme a l'inactivite, § 5.8). En annoncer une ferait
        # remplacer par Hermes une session encore vivante.
        return {
            # Unique par ouverture, comme les fournisseurs d'Hermes : c'est le
            # nom du demon agent-browser, et la session chaude est reprise d'un
            # tour a l'autre par un demon NEUF.
            "session_name": "pulse_%s" % uuid.uuid4().hex[:10],
            "bb_session_id": session_id,
            "cdp_url": cdp_url,
            "features": {"pulse": True},
        }

    def close_session(self, session_id: str) -> bool:
        """LIBERE la session (spec § 5.3) : le Chromium reste ouvert jusqu'a
        l'inactivite, pour que la personne qui a pris la main pour se connecter
        ne soit pas coupee quand l'agent a fini de parler. Ne leve jamais."""
        return self._release(session_id, RELEASE_TIMEOUT_SECONDS)

    def emergency_cleanup(self, session_id: str) -> None:
        """Meme liberation, depuis ``atexit`` : delai court, aucune exception."""
        self._release(session_id, EMERGENCY_TIMEOUT_SECONDS)

    # -- Outil pulse_browser_handoff ----------------------------------------

    def handoff(self, args: Dict[str, Any], task_id: Optional[str] = None) -> str:
        """Handler SYNCHRONE de ``pulse_browser_handoff`` — rend TOUJOURS une
        chaine JSON, jamais une exception.

        Synchrone parce qu'il ne fait que du HTTP bloquant puis une attente :
        il tourne dans le fil d'outil, ou les ``ContextVar`` de la passerelle
        sont propagees, et attend sur un ``concurrent.futures.Future`` que le
        fil du WebSocket resout (``on_control``) — un Future asyncio ne
        s'attendrait pas d'une autre boucle que la sienne.
        """
        try:
            return self._handoff(args or {}, task_id)
        except Exception as exc:  # filet : un outil qui leve n'apprend rien au modele
            logger.warning("Pulse Chat: %s en echec — %s", HANDOFF_TOOL_NAME, exc)
            return refused_result("not_sent", str(exc))

    def _handoff(self, args: Dict[str, Any], task_id: Optional[str]) -> str:
        reason = str(args.get("reason") or "").strip()[:MAX_REASON_LENGTH]
        if not reason:
            return refused_result("no_reason", "Le motif (`reason`) est requis")
        session_id = self._session_for(task_id)
        if session_id is None:
            return refused_result(
                "no_browser_session",
                "Aucun navigateur Pulse Chat ouvert pour cette tache : ouvre d'abord le navigateur",
            )

        waiter: "concurrent.futures.Future[Dict[str, Any]]" = concurrent.futures.Future()
        # Armee AVANT le POST, comme une approbation : l'app ne doit jamais
        # pouvoir rendre la main plus vite que l'outil ne se met a l'ecoute.
        with self._lock:
            self._waiters.setdefault(session_id, []).append(waiter)
        try:
            path = "%s/%s/handoff" % (SESSIONS_PATH, quote(session_id, safe=""))
            status, body = self._http("POST", path, {"reason": reason}, HANDOFF_POST_TIMEOUT_SECONDS)
            if not 200 <= status < 300:
                err = parse_error(status, body)
                code = "browser_session_not_found" if status == 404 else err["code"]
                return refused_result(code, err["message"])
            try:
                control = waiter.result(timeout=self._wait_seconds)
            except concurrent.futures.TimeoutError:
                return pending_result()
            return done_result(control.get("by"))
        finally:
            with self._lock:
                pending = self._waiters.get(session_id)
                if pending is not None:
                    if waiter in pending:
                        pending.remove(waiter)
                    if not pending:
                        self._waiters.pop(session_id, None)

    # -- Trame browser.control ------------------------------------------------

    def on_control(self, frame: Any) -> bool:
        """Relaie un changement de main. ``True`` si au moins un outil attendait.

        Seul le retour a l'AGENT reveille un outil : l'app n'emet
        ``controller: agent`` qu'a la fin d'une prise de main (rendue, ou
        reprise d'office apres 5 min sans entree). Thread-safe : appele depuis
        la boucle du WebSocket, resout des Futures attendues ailleurs.
        """
        control = parse_control_frame(frame)
        if control is None:
            logger.warning("Pulse Chat: trame browser.control inexploitable ignoree")
            return False
        session_id = control["sessionId"]
        with self._lock:
            if control["controller"] == "human":
                self._last_human[session_id] = control["by"]
                return False
            helped_by = self._last_human.pop(session_id, None)
            waiters = self._waiters.pop(session_id, [])
        result = {"controller": "agent", "by": helped_by or control["by"]}
        woke = False
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(result)
                woke = True
        return woke

    # -- Interne ----------------------------------------------------------------

    def _unavailable_remaining(self) -> float:
        with self._lock:
            return max(0.0, self._unavailable_until - self._clock())

    def _on_unavailable(self) -> None:
        with self._lock:
            self._unavailable_until = self._clock() + UNAVAILABLE_BACKOFF_SECONDS

    def _remember(self, key: str, session_id: str, channel: str) -> None:
        with self._lock:
            previous = self._sessions.pop(key, None)
            if previous is not None:
                self._forget_one(previous["sessionId"])
            self._sessions[key] = {"sessionId": session_id, "channel": channel}
            self._open[session_id] = self._open.get(session_id, 0) + 1
            while len(self._sessions) > MAX_TRACKED_SESSIONS:
                _, oldest = self._sessions.popitem(last=False)
                self._forget_one(oldest["sessionId"])

    def _forget_one(self, session_id: str) -> None:
        """Decompte une ouverture (verrou tenu par l'appelant)."""
        count = self._open.get(session_id, 0) - 1
        if count > 0:
            self._open[session_id] = count
        else:
            self._open.pop(session_id, None)

    def _session_for(self, task_id: Optional[str]) -> Optional[str]:
        """Session Pulse ouverte pour cette tache, sinon pour ce canal.

        Le repli par canal couvre les sessions NOMMEES de Browser Use
        (``bu-named-<nom>``), dont la cle ne contient pas la tache. Il ne
        melange pas deux agents : l'app tient une session par (agent, canal),
        et un bot n'ouvre ici que celles de ses propres tours.
        """
        channel = str(self._session_env("HERMES_SESSION_CHAT_ID") or "").strip()
        with self._lock:
            if task_id:
                exact = self._sessions.get(task_id)
                if exact is not None:
                    return exact["sessionId"]
                for key in reversed(self._sessions):
                    if _task_prefix(key) == task_id:
                        return self._sessions[key]["sessionId"]
            if channel:
                for key in reversed(self._sessions):
                    if self._sessions[key]["channel"] == channel:
                        return self._sessions[key]["sessionId"]
        return None

    def _release(self, session_id: str, timeout: float) -> bool:
        try:
            if not session_id:
                return False
            with self._lock:
                self._forget_one(session_id)
                if session_id not in self._open:
                    for key in [k for k, v in self._sessions.items() if v["sessionId"] == session_id]:
                        self._sessions.pop(key, None)
            path = "%s/%s" % (SESSIONS_PATH, quote(str(session_id), safe=""))
            status, _body = self._http("DELETE", path, None, timeout)
            # 404 = deja fermee (balayage d'inactivite, fermeture a la main) :
            # le resultat voulu est atteint.
            if 200 <= status < 300 or status == 404:
                return True
            logger.warning("Pulse Chat: liberation du navigateur %s -> HTTP %s", session_id, status)
            return False
        except Exception as exc:
            logger.warning("Pulse Chat: liberation du navigateur %s en echec — %s", session_id, exc)
            return False


# ── Point d'entree des trames (adaptateur) ─────────────────────────────────

_active: Optional[PulseBrowserProvider] = None


def activate(provider: Optional[PulseBrowserProvider]) -> None:
    """Designe le fournisseur qui recoit les trames ``browser.control``."""
    global _active
    _active = provider


def on_control(frame: Any) -> bool:
    """Trame ``browser.control`` recue par l'adaptateur. Sans fournisseur actif
    (Hermes sans fournisseurs de navigateur), il n'y a personne a reveiller."""
    provider = _active
    if provider is None:
        return False
    return provider.on_control(frame)
