# -*- coding: utf-8 -*-
"""Plan de taches de l'agent (``todo_list`` d'Hermes) -> carte dans le fil.

Module PUR (aucune dependance Hermes) : il lit le RESULTAT de l'outil et
compose la charge ``POST /api/agent/messages``. Le branchement (hook
``post_tool_call``, contexte de session, envoi) vit dans ``adapter.py``.

Pourquoi le resultat et pas le texte : cote plateforme, Hermes n'envoie qu'une
ligne d'activite cumulative, ``📋 Updating tasks planning 3 task(s)`` — le
contenu des etapes n'arrive JAMAIS. En revanche l'outil RENVOIE toujours la
liste COMPLETE (``tools/todo_tool.py`` : ``{"todos": [...], "revision": n,
"summary": {...}}``, en lecture comme en ecriture), et ``post_tool_call`` recoit
ce resultat.

Le plugin reste MINCE : il transporte la liste brute. La PHASE (en cours /
termine), les bornes d'affichage et le compteur de la carte sont decides par
l'app — le resume ``content`` n'est qu'une ligne lisible pour l'export.
"""

import json
from typing import Any, Dict, List, Optional

#: ``todo_list`` depuis Hermes v2026.9.7 ; ``todo`` avant, alias encore accepte
#: (``model_tools._LEGACY_TOOL_ALIASES``). Un hook peut voir l'un ou l'autre
#: selon la version du coeur.
TODO_TOOL_NAMES = frozenset({"todo_list", "todo"})

#: Nom d'outil sous lequel la carte est postee, quelle que soit la version.
TODO_TOOL = "todo_list"

#: Statuts d'Hermes (``VALID_STATUSES``), mot pour mot. L'app refuse (400) tout
#: autre statut : une etape illisible est ECARTEE ici plutot que de faire
#: tomber toute la carte.
TODO_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})

#: Prefixe de ``hermesMessageId`` : l'app met la carte a jour tant que la cle
#: est la meme (upsert), donc UNE carte par tour.
MESSAGE_ID_PREFIX = "todo:"


def parse_todo_result(result: Any) -> Optional[List[Dict[str, str]]]:
    """La liste d'etapes du resultat de l'outil, ou ``None`` s'il n'en porte pas.

    ``None`` couvre tout ce qui n'est pas un plan : JSON illisible, erreur
    d'outil (``{"error": ...}``), ``todos`` absent ou d'un autre type. Une liste
    VIDE est un plan (l'agent a vide le sien) et se rend telle quelle.
    """
    data = result
    if isinstance(result, (bytes, bytearray)):
        try:
            data = result.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return None
    if not isinstance(data, dict):
        return None
    todos = data.get("todos")
    if not isinstance(todos, list):
        return None
    steps: List[Dict[str, str]] = []
    for item in todos:
        if not isinstance(item, dict):
            continue
        step_id = str(item.get("id") or "").strip()
        status = str(item.get("status") or "").strip().lower()
        if not step_id or status not in TODO_STATUSES:
            continue
        step = {"id": step_id, "content": str(item.get("content") or ""), "status": status}
        parent = str(item.get("parent") or "").strip()
        if parent:
            step["parent"] = parent
        steps.append(step)
    return steps


def todo_message_id(turn_id: str = "", task_id: str = "", session_id: str = "") -> Optional[str]:
    """Cle de la carte : UNE par tour de l'agent.

    ``turn_id`` est pose a chaque tour par Hermes (``agent/turn_context.py`` :
    ``"<session_id>:<task_id>:<hex8>"``). Repli sur ``task_id`` (un par tour
    dans la passerelle), puis sur ``session_id`` : sur un Hermes qui ne fournit
    ni l'un ni l'autre, UNE carte par session vaut mieux qu'aucune carte — elle
    se met a jour au lieu de se multiplier. Rien du tout ⇒ ``None`` : on
    n'invente pas une cle qui creerait une carte par ecriture.
    """
    for key in (turn_id, task_id, session_id):
        key = str(key or "").strip()
        if key:
            return f"{MESSAGE_ID_PREFIX}{key}"
    return None


def summarize(steps: List[Dict[str, str]]) -> str:
    """``Plan : 3/5`` — terminees sur (total - annulees), comme la carte."""
    counted = [s for s in steps if s.get("status") != "cancelled"]
    done = sum(1 for s in counted if s.get("status") == "completed")
    return f"Plan : {done}/{len(counted)}"


def raw_of(result: Any) -> str:
    """Le resultat tel que rendu par l'outil, conserve en ``raw``."""
    if isinstance(result, str):
        return result
    if isinstance(result, (bytes, bytearray)):
        return result.decode("utf-8", "replace")
    try:
        return json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(result)


def build_todo_payload(
    chat_id: str, steps: List[Dict[str, str]], raw: str, message_id: str
) -> Dict[str, Any]:
    """Charge ``POST /api/agent/messages`` d'une carte de plan.

    ``phase`` est envoyee pour la forme (``progress``) : l'app la REDERIVE des
    etapes et ignore celle-ci — un « termine » decide ici mentirait des qu'une
    version du plugin se tromperait.
    """
    return {
        "channelSlug": str(chat_id),
        "kind": "tool_event",
        "tool": TODO_TOOL,
        "phase": "progress",
        "content": summarize(steps),
        "raw": raw,
        "hermesMessageId": message_id,
        "todos": steps,
        "replyToHermesId": None,
    }
