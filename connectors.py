# -*- coding: utf-8 -*-
"""Connecteurs tiers (Outlook, Teams, agenda, GitHub) — partie PURE.

Meme principe que ``vault.py``, et pour la meme raison : le plugin n'a AUCUN
identifiant OAuth, aucun scope, aucune URL de fournisseur. Il demande une
CAPACITE a l'app, avec le Bearer de service deja en place ; c'est l'app qui
choisit le compte, resout le jeton, appelle le fournisseur et audite.

    POST /api/agent/connectors/<capability>
        {channelSlug, params, onBehalfOf?, grantId?}

Ce module ne fait pas d'I/O : il construit l'URL, valide localement ce que le
serveur refuserait de toute facon, et traduit les codes d'erreur en messages
exploitables par l'agent. Le controle qui FAIT foi reste celui du serveur —
celui-ci n'est qu'un garde-fou de confort, jamais la frontiere de securite.

La liste blanche ci-dessous DUPLIQUE volontairement
``app/pulse-chat/shared/connectors.ts``. C'est un doublon assume : il evite un
aller-retour reseau pour une capacite manifestement inexistante. En cas de
divergence, le serveur tranche.
"""

from typing import Any, Dict, Optional
from urllib.parse import quote

#: Capacites connues, avec leur effet de bord. Miroir de shared/connectors.ts.
#: ``True`` ⇒ l'action a un effet HORS du produit et demandera l'accord d'un
#: humain (sauf delegation explicitement dispensee).
CONNECTOR_CAPABILITIES: Dict[str, bool] = {
    "mail.read": False,
    # Corps COMPLET + pieces jointes deposees dans le coffre du canal.
    # Capacite distincte de "mail.read" : la projection en apercu est une regle
    # du produit, pas une limite technique, et l'elargir en douce aurait fait de
    # "mail.read" autre chose que ce que le delegant avait accorde.
    "mail.content": False,
    # Redige un brouillon dans la boite du delegant ; c'est LUI qui envoie.
    # Rien ne quitte le tenant, donc aucun effet de bord de notre point de vue —
    # et aucune approbation asynchrone a calibrer. A preferer a ``mail.send``.
    "mail.draft": False,
    "mail.send": True,
    # Repond/transfere un message existant. Porte un parametre ``mode``
    # ("draft"/"send") qui decide de l'effet REEL cote serveur (seul juge qui
    # compte) ; ce dict-ci reste une liste blanche de CONFORT, statique par
    # nature, donc au repli le plus prudent : ``True``, comme un envoi peut en
    # decouler. Un ``mode: "draft"`` refuse ici a tort ne serait qu'un aller-
    # retour reseau en moins ; l'inverse (laisser passer un envoi non audite)
    # serait la vraie faute — et n'arrive de toute facon pas, le serveur
    # tranchant seul (cf. `capabilitySideEffect` dans schemas.ts).
    "mail.reply": True,
    "mail.forward": True,
    # Modifie/supprime un brouillon existant : rien ne quitte le tenant, meme
    # raison que ``mail.draft``. La suppression est distincte de
    # ``tasks.delete`` (effet de bord) car un courriel supprime chez Graph part
    # en "Elements supprimes", recuperable — a la difference de To Do, qui n'a
    # aucune corbeille.
    "mail.draft.update": False,
    "mail.draft.delete": False,
    "calendar.read": False,
    "calendar.write": True,
    # Taches Microsoft To Do. ``tasks.write`` couvre creation ET modification
    # (dont cocher « termine », qui est un changement de statut) : la capacite est
    # l'unite d'AUTORITE, l'action vit dans les parametres. Sans effet de bord,
    # pour la raison de ``mail.draft`` : la tache reste dans l'espace du delegant.
    "tasks.read": False,
    "tasks.write": False,
    # La SUPPRESSION est a part, et a effet de bord : To Do n'a pas de corbeille.
    "tasks.delete": True,
    "teams.post": True,
    # ── GitHub (GitHub App) ────────────────────────────────────────────────
    # Le DEPOT est un parametre, jamais une capacite : le perimetre reel est
    # celui de l'installation, choisie par le delegant. Inventer
    # "repo.readProjetX" serait le "mail.readFromSender" que ce catalogue
    # refuse depuis le premier jour.
    #
    # "repo.read" lit du CODE : sans effet de bord (rien ne sort vers un
    # tiers), mais autorite forte — du code prive entre dans le contexte d'un
    # LLM — d'ou une capacite a part que le delegant coche sciemment, comme
    # "mail.content" face a "mail.read".
    "repo.read": False,
    "issues.read": False,
    # Effet de bord, et contrairement a "tasks.write" ce n'est pas discutable :
    # un commentaire NOTIFIE des tiers sous le nom du delegant, et il est deja
    # parti. Meme regime que "mail.send".
    "issues.write": True,
    # Separee de "issues.read" bien que GitHub serve les deux par la meme API :
    # c'est la PERMISSION qui differe chez le fournisseur, et un diff, c'est du
    # code.
    "pr.read": False,
    "pr.write": True,
    # Ne rend PAS les journaux (variables d'environnement, URL signees,
    # extraits de code) : ce sera "ci.logs", meme raisonnement que
    # "mail.content" face a "mail.read".
    "ci.read": False,
}

#: Codes d'erreur du serveur, et ce que l'agent doit en faire. Traduire ici
#: plutot que de laisser remonter un HTTP nu : un agent qui recoit « 403 » ne
#: sait pas s'il doit renoncer, reformuler, ou demander quelque chose a l'humain.
CONNECTOR_ERROR_HINTS: Dict[str, str] = {
    "agent_credential_required": (
        "Cette action exige un secret d'agent (AgentCredential). Le deploiement "
        "doit definir PULSE_CHAT_AGENT_TOKEN — ne pas reessayer."
    ),
    "connector_not_activated": (
        "La delegation existe mais l'outil n'est pas allume dans CETTE "
        "conversation. Demander a l'humain de l'activer (icone prise du champ "
        "de saisie) ; inutile de reessayer avant."
    ),
    "connector_no_grant": (
        "Aucune delegation active ne couvre cette action. Demander a l'utilisateur "
        "d'en accorder une depuis ses reglages, puis reessayer."
    ),
    "connector_grant_ambiguous": (
        "Plusieurs comptes conviennent. DEMANDER a l'utilisateur lequel utiliser, "
        "puis rappeler avec `grantId`. Ne jamais choisir soi-meme."
    ),
    "connector_ambiguous_target": (
        "Plusieurs objets du compte repondent a la designation donnee (deux listes "
        "de taches du meme nom, par exemple). DEMANDER a l'humain lequel, ou "
        "reprendre l'identifiant rendu par la lecture. Ne pas choisir soi-meme."
    ),
    "connector_approval_required": (
        "Le proprietaire du compte doit donner son accord. Le demander dans la "
        "conversation et attendre sa reponse."
    ),
    "connector_cross_user_denied": (
        "Ce connecteur ne sert que son proprietaire : l'action demandee par une "
        "autre personne est refusee. Ne pas insister."
    ),
    "connector_reauth_required": (
        "Le compte doit etre reconnecte par son proprietaire. L'en informer ; "
        "reessayer est inutile jusque-la."
    ),
    "connector_rate_limited": (
        "Plafond d'actions atteint. Attendre avant de reessayer."
    ),
    "connector_invalid_params": (
        "Parametres refuses par le serveur. Corriger l'appel d'apres le detail "
        "renvoye plutot que de reessayer a l'identique."
    ),
    "connector_scope_missing": (
        "Le compte n'a pas consenti aux droits necessaires. L'utilisateur doit "
        "les accorder en reconnectant son compte."
    ),
}


class ConnectorCapabilityError(ValueError):
    """Capacite refusee avant meme l'appel reseau."""


def normalize_capability(raw: Any) -> str:
    """Valide une capacite contre la liste blanche et la renvoie.

    Refuse (plutot que de « corriger » en silence) : type invalide, capacite
    absente du catalogue. Une capacite devinee ou reecrite ferait agir l'agent
    autrement qu'il ne le croit.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ConnectorCapabilityError("capacite vide")
    capability = raw.strip()
    # `in` sur un dict Python ne remonte pas de chaine de prototypes, mais on
    # reste explicite : seule une cle du catalogue passe.
    if capability not in CONNECTOR_CAPABILITIES:
        known = ", ".join(sorted(CONNECTOR_CAPABILITIES))
        raise ConnectorCapabilityError(
            "capacite inconnue: %s (connues: %s)" % (capability, known)
        )
    return capability


def has_side_effect(capability: str) -> bool:
    """La capacite a-t-elle un effet hors du produit ? Inconnue ⇒ ``True``.

    Le repli est VOLONTAIREMENT le plus prudent : traiter une capacite inconnue
    comme sans effet de bord la ferait passer sous le radar des approbations.
    """
    return CONNECTOR_CAPABILITIES.get(capability, True)


def connector_url(base_url: str, capability: str) -> str:
    """URL d'appel d'une capacite. Valide la capacite au passage."""
    normalized = normalize_capability(capability)
    return "%s/api/agent/connectors/%s" % (
        base_url.rstrip("/"),
        quote(normalized, safe=""),
    )


def build_connector_payload(
    channel_slug: str,
    params: Optional[Dict[str, Any]] = None,
    on_behalf_of: Optional[str] = None,
    grant_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Construit le POST sortant. PURE — aucune I/O.

    ``on_behalf_of`` et ``grant_id`` sont OMIS quand ils sont vides, plutot que
    posés à ``None`` : le serveur distingue « non precise » (il resout, et refuse
    si c'est ambigu) de « precise », et une cle nulle sur le fil brouillerait
    cette distinction.
    """
    payload: Dict[str, Any] = {
        "channelSlug": channel_slug,
        "params": params if isinstance(params, dict) else {},
    }
    if on_behalf_of:
        payload["onBehalfOf"] = on_behalf_of
    if grant_id:
        payload["grantId"] = grant_id
    return payload


def parse_connector_error(status: int, body: Any) -> Dict[str, Any]:
    """Traduit une reponse d'erreur en dict exploitable par l'agent.

    Renvoie toujours la MEME forme, avec ``granted`` absent et ``retryable``
    explicite : un appelant qui teste ``if result:`` ne doit jamais pouvoir
    confondre un echec avec une autorisation.

    ``ambiguous_options`` est renseigne pour le seul cas ou l'agent a quelque
    chose a faire de la liste : demander a l'humain lequel de ses comptes
    utiliser.
    """
    code = None
    message = None
    options = None
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, dict):
            code = data.get("code")
            raw_options = data.get("options")
            if isinstance(raw_options, list):
                options = [o for o in raw_options if isinstance(o, dict)]
        message = body.get("statusMessage") or body.get("message")

    return {
        "ok": False,
        "status": status,
        "code": code if isinstance(code, str) else "connector_error",
        "message": message if isinstance(message, str) else "appel de connecteur refuse",
        "hint": CONNECTOR_ERROR_HINTS.get(code or "", ""),
        # 429 et 5xx valent une nouvelle tentative plus tard ; un refus de
        # delegation ou d'approbation, jamais — insister ne changerait rien et
        # remplirait le journal d'audit de refus identiques.
        "retryable": status == 429 or status >= 500,
        "ambiguous_options": options or [],
    }
