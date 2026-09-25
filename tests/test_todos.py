# -*- coding: utf-8 -*-
"""Plan de taches : lecture du resultat de ``todo_list`` et charge envoyee.

Module pur (``todos.py``) — aucun Hermes requis. Ce qui est verrouille :

  - un resultat illisible, une erreur d'outil ou un ``todos`` absent ne
    produisent AUCUNE carte (``None``), jamais une exception ;
  - une etape au statut inconnu est ecartee, pas « completee » d'un statut
    invente — l'app refuserait toute la carte (400) ;
  - la cle est UNE par tour (``turn_id``), avec un repli documente ;
  - la charge porte la liste brute, le resume et le JSON d'origine.
"""

import json

from test_adapter_dedup import _load_adapter_module

_load_adapter_module()  # installe le paquet de test (imports relatifs)

import importlib  # noqa: E402

todos = importlib.import_module("pulse_chat_plugin_under_test.todos")

RESULT = json.dumps(
    {
        "todos": [
            {"id": "1", "content": "Lire le ticket", "status": "completed"},
            {"id": "2", "content": "Corriger", "status": "in_progress"},
            {"id": "2a", "content": "Ecrire le test", "status": "pending", "parent": "2"},
            {"id": "3", "content": "Abandonne", "status": "cancelled"},
        ],
        "revision": 4,
        "summary": {"total": 4},
    },
    ensure_ascii=False,
)


class TestLecture:
    def test_lit_la_liste_complete(self):
        steps = todos.parse_todo_result(RESULT)
        assert [s["id"] for s in steps] == ["1", "2", "2a", "3"]
        assert steps[2] == {"id": "2a", "content": "Ecrire le test", "status": "pending", "parent": "2"}

    def test_accepte_un_dict_deja_decode(self):
        assert todos.parse_todo_result(json.loads(RESULT))[0]["status"] == "completed"

    def test_json_illisible_ne_donne_aucune_carte(self):
        assert todos.parse_todo_result("pas du json") is None
        assert todos.parse_todo_result(b"\xff\xfe") is None
        assert todos.parse_todo_result(None) is None

    def test_une_erreur_doutil_ne_donne_aucune_carte(self):
        assert todos.parse_todo_result(json.dumps({"error": "todos must be a list"})) is None

    def test_todos_dun_autre_type_ne_donne_aucune_carte(self):
        assert todos.parse_todo_result(json.dumps({"todos": "x"})) is None

    def test_une_liste_vide_reste_un_plan(self):
        assert todos.parse_todo_result(json.dumps({"todos": []})) == []

    def test_ecarte_une_etape_au_statut_inconnu(self):
        steps = todos.parse_todo_result(
            json.dumps({"todos": [{"id": "1", "content": "a", "status": "blocked"}, {"id": "2", "status": "pending"}]})
        )
        assert steps == [{"id": "2", "content": "", "status": "pending"}]

    def test_un_parent_vide_nest_pas_un_parent(self):
        steps = todos.parse_todo_result(json.dumps({"todos": [{"id": "1", "content": "a", "status": "pending", "parent": ""}]}))
        assert "parent" not in steps[0]


class TestCle:
    def test_une_carte_par_tour(self):
        assert todos.todo_message_id(turn_id="s:t:ab12", task_id="t", session_id="s") == "todo:s:t:ab12"

    def test_repli_sur_la_tache_puis_la_session(self):
        assert todos.todo_message_id(turn_id="", task_id="t", session_id="s") == "todo:t"
        assert todos.todo_message_id(turn_id="", task_id="", session_id="s") == "todo:s"

    def test_sans_aucune_cle_on_ninvente_rien(self):
        assert todos.todo_message_id() is None


class TestCharge:
    def test_forme_du_post(self):
        steps = todos.parse_todo_result(RESULT)
        payload = todos.build_todo_payload("compta", steps, RESULT, "todo:turn-1")
        assert payload == {
            "channelSlug": "compta",
            "kind": "tool_event",
            "tool": "todo_list",
            "phase": "progress",
            # Annulee hors compteur : 1 terminee sur 3 comptees.
            "content": "Plan : 1/3",
            "raw": RESULT,
            "hermesMessageId": "todo:turn-1",
            "todos": steps,
            "replyToHermesId": None,
        }

    def test_raw_dun_dict_est_son_json(self):
        assert json.loads(todos.raw_of({"todos": []})) == {"todos": []}

    def test_les_deux_noms_doutil(self):
        assert todos.TODO_TOOL_NAMES == frozenset({"todo_list", "todo"})
