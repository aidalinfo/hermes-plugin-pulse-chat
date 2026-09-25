# -*- coding: utf-8 -*-
"""Plan de taches cote adaptateur : le hook ``post_tool_call`` (sans hermes).

Ce qui est verifie, et chaque point est un mode d'echec MUET :

  - tout autre outil ressort au premier test, sans lire le resultat ;
  - une session qui n'est pas Pulse Chat (Telegram, CLI) ne poste rien ;
  - un resultat illisible ne leve JAMAIS dans Hermes ;
  - le POST part sur la boucle du WebSocket, avec la forme attendue, et le
    thread du hook n'attend pas sa reponse ;
  - deux ecritures du meme tour arrivent DANS L'ORDRE (sinon la carte finit
    sur l'etat ancien) ;
  - un plan vide sans carte ce tour-ci ne cree rien ; vide APRES une carte, il
    la met a jour ;
  - le hook est enregistre, et un Hermes sans ``register_hook`` ne casse pas
    l'enregistrement de la plateforme.
"""

import asyncio
import json
import sys
import threading
import time
import types

import pytest

from test_adapter_dedup import _load_adapter_module

adapter_module = _load_adapter_module()

SESSION = {"HERMES_SESSION_PLATFORM": "pulse_chat", "HERMES_SESSION_CHAT_ID": "compta"}


@pytest.fixture(autouse=True)
def _session(monkeypatch):
    mod = types.ModuleType("gateway.session_context")
    mod.get_session_env = lambda name, default="": SESSION.get(name, default)
    monkeypatch.setitem(sys.modules, "gateway.session_context", mod)
    for a in list(adapter_module._LIVE_ADAPTERS):
        a.is_connected = False
    yield
    for a in list(adapter_module._LIVE_ADAPTERS):
        a.is_connected = False


class _Config:
    extra = {"url": "http://pulse-chat.test", "token": "token-test"}


class _WsLoop:
    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self.loop

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


def _result(*statuses):
    return json.dumps(
        {"todos": [{"id": str(i), "content": f"Etape {i}", "status": s} for i, s in enumerate(statuses)]}
    )


def _connected_adapter(loop, delays=None):
    """Adaptateur connecte dont ``_post_agent_message`` enregistre les charges.

    ``delays`` : attente simulee par envoi (le premier lent, le second rapide
    reproduit deux POST en vol qui se doubleraient)."""
    adapter = adapter_module.PulseChatAdapter(_Config())
    adapter._loop = loop
    adapter.is_connected = True
    posted = []
    delays = list(delays or [])

    async def fake_post(payload, hermes_id):
        if delays:
            await asyncio.sleep(delays.pop(0))
        posted.append(payload)
        return adapter_module.SendResult(success=True, message_id=hermes_id)

    adapter._post_agent_message = fake_post
    return adapter, posted


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _hook(tool_name="todo_list", result=None, turn_id="sess:task:1", **extra):
    return adapter_module._on_post_tool_call(
        tool_name=tool_name,
        args={},
        result=result if result is not None else _result("completed", "in_progress"),
        task_id="task",
        session_id="sess",
        turn_id=turn_id,
        tool_call_id="call-1",
        duration_ms=3,
        status="ok",
        **extra,
    )


class TestFiltrage:
    def test_un_autre_outil_ne_lit_meme_pas_le_resultat(self):
        class Piege:
            def __getattr__(self, name):  # pragma: no cover - ne doit jamais servir
                raise AssertionError("le resultat d'un autre outil a ete lu")

        with _WsLoop() as loop:
            adapter, posted = _connected_adapter(loop)
            assert _hook(tool_name="terminal", result=Piege()) is None
            time.sleep(0.05)
        assert posted == []

    def test_lalias_historique_todo_est_relaye(self):
        with _WsLoop() as loop:
            adapter, posted = _connected_adapter(loop)
            _hook(tool_name="todo")
            assert _wait_for(lambda: len(posted) == 1)
        assert posted[0]["tool"] == "todo_list"

    def test_une_session_telegram_ne_poste_rien(self, monkeypatch):
        monkeypatch.setitem(SESSION, "HERMES_SESSION_PLATFORM", "telegram")
        with _WsLoop() as loop:
            adapter, posted = _connected_adapter(loop)
            _hook()
            time.sleep(0.05)
        assert posted == []

    def test_une_plateforme_vide_ne_poste_rien(self, monkeypatch):
        monkeypatch.setitem(SESSION, "HERMES_SESSION_PLATFORM", "")
        with _WsLoop() as loop:
            adapter, posted = _connected_adapter(loop)
            _hook()
            time.sleep(0.05)
        assert posted == []

    def test_sans_conversation_ne_poste_rien(self, monkeypatch):
        monkeypatch.setitem(SESSION, "HERMES_SESSION_CHAT_ID", "")
        with _WsLoop() as loop:
            adapter, posted = _connected_adapter(loop)
            _hook()
            time.sleep(0.05)
        assert posted == []

    def test_un_sous_agent_ne_poste_rien(self, monkeypatch):
        monkeypatch.setattr(adapter_module, "_is_delegated_child", lambda: True)
        with _WsLoop() as loop:
            adapter, posted = _connected_adapter(loop)
            _hook()
            time.sleep(0.05)
        assert posted == []


class TestRobustesse:
    def test_json_illisible_ne_leve_pas_et_ne_poste_rien(self):
        with _WsLoop() as loop:
            adapter, posted = _connected_adapter(loop)
            assert _hook(result="{pas du json") is None
            assert _hook(result=json.dumps({"error": "boom"})) is None
            time.sleep(0.05)
        assert posted == []

    def test_une_panne_interne_ne_remonte_jamais(self, monkeypatch):
        def boom(*_a, **_kw):
            raise RuntimeError("panne")

        monkeypatch.setattr(adapter_module, "_forward_todo_plan", boom)
        assert _hook() is None

    def test_sans_adaptateur_connecte_rien_ne_part(self):
        assert _hook() is None


class TestEnvoi:
    def test_forme_du_post_et_hook_non_bloquant(self):
        with _WsLoop() as loop:
            adapter, posted = _connected_adapter(loop, delays=[0.3])
            started = time.monotonic()
            _hook(result=_result("completed", "in_progress", "pending"))
            # Le thread du hook n'attend pas la reponse HTTP.
            assert time.monotonic() - started < 0.2
            assert _wait_for(lambda: len(posted) == 1)
        payload = posted[0]
        assert payload["channelSlug"] == "compta"
        assert payload["kind"] == "tool_event"
        assert payload["tool"] == "todo_list"
        assert payload["hermesMessageId"] == "todo:sess:task:1"
        assert payload["content"] == "Plan : 1/3"
        assert [s["status"] for s in payload["todos"]] == ["completed", "in_progress", "pending"]
        assert json.loads(payload["raw"])["todos"][0]["content"] == "Etape 0"

    def test_deux_ecritures_du_meme_tour_arrivent_dans_lordre(self):
        with _WsLoop() as loop:
            # Le premier envoi est LENT : sans verrou, le second le doublerait.
            adapter, posted = _connected_adapter(loop, delays=[0.2, 0.0])
            _hook(result=_result("in_progress", "pending"))
            _hook(result=_result("completed", "in_progress"))
            assert _wait_for(lambda: len(posted) == 2)
        assert [p["todos"][0]["status"] for p in posted] == ["in_progress", "completed"]

    def test_un_nouveau_tour_ouvre_une_nouvelle_cle(self):
        with _WsLoop() as loop:
            adapter, posted = _connected_adapter(loop)
            _hook(turn_id="t1")
            _hook(turn_id="t2")
            assert _wait_for(lambda: len(posted) == 2)
        assert [p["hermesMessageId"] for p in posted] == ["todo:t1", "todo:t2"]

    def test_un_plan_vide_sans_carte_ne_cree_rien(self):
        with _WsLoop() as loop:
            adapter, posted = _connected_adapter(loop)
            _hook(result=json.dumps({"todos": []}))
            time.sleep(0.05)
        assert posted == []

    def test_un_plan_vide_apres_une_carte_la_met_a_jour(self):
        with _WsLoop() as loop:
            adapter, posted = _connected_adapter(loop)
            _hook(result=_result("pending"))
            _hook(result=json.dumps({"todos": []}))
            assert _wait_for(lambda: len(posted) == 2)
        assert posted[1]["todos"] == []
        assert posted[1]["hermesMessageId"] == posted[0]["hermesMessageId"]


class TestEnregistrement:
    def test_le_hook_est_enregistre_sur_post_tool_call(self):
        hooks = []

        class Ctx:
            def register_hook(self, name, cb):
                hooks.append((name, cb))

        adapter_module._register_todo_hook(Ctx())
        assert hooks == [("post_tool_call", adapter_module._on_post_tool_call)]

    def test_un_hermes_sans_register_hook_ne_casse_rien(self):
        adapter_module._register_todo_hook(object())
