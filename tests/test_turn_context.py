# -*- coding: utf-8 -*-
"""Jeton de tour (#755 lot 5) : le plugin le TRANSPORTE, tour par tour.

L'app pose ``turnContext`` a la racine de ``message.created`` (et des decisions
remises) ; le plugin le garde dans une ``ContextVar`` pendant le tour et le
renvoie dans l'en-tete ``x-pulse-turn-context`` de ses appels HTTP vers l'app,
et des appels d'inference d'Hermes vers la passerelle IA de Pulse (middleware
``llm_request``) — jamais vers un autre fournisseur.

Doctrine verifiee ici :
  - jamais lu ni juge : renvoye tel quel, ou pas du tout s'il est suspect ;
  - task-local : deux tours simultanes ne se volent pas leur jeton, et le jeton
    d'une trame ne reste pas pose pour la suivante ;
  - il suit le tour la ou asyncio copie le contexte (taches, fils d'outil,
    coroutines renvoyees sur la boucle depuis un fil d'outil) ;
  - un message mis en file derriere un tour n'herite pas du jeton de ce tour ;
  - vers la passerelle IA seulement, sur ses deux entrees (primaire, repli) ;
  - sans jeton (app anterieure), le plugin se comporte exactement comme avant.
"""

import asyncio

from test_adapter_dedup import _load_adapter_module

adapter_module = _load_adapter_module()
tc = adapter_module.turn_context

TOKEN = "ptc1.eyJyIjoiY2FuYXJ5In0.c2lnbmF0dXJl"
OTHER = "ptc1.eyJyIjoic3RhYmxlIn0.YXV0cmU"


class _Config:
    extra = {"url": "http://pulse-chat.test", "token": "token-test"}


def _header(request, name):
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
    requests = []

    def fake_urlopen(request, timeout=None):
        requests.append(request)
        return _FakeResponse()

    monkeypatch.setattr(adapter_module, "_urlopen", fake_urlopen)
    return requests


def _frame(message_id, token=None):
    frame = {
        "type": "message.created",
        "channel": {"slug": "canal-test", "name": "Canal Test"},
        "message": {"id": message_id, "text": "bonjour", "userId": "u1", "userName": "Alice"},
    }
    if token is not None:
        frame["turnContext"] = token
    return frame


# --- 1. Lecture de la trame ---------------------------------------------------

def test_le_jeton_de_la_trame_est_rendu_tel_quel():
    assert tc.from_frame({"turnContext": TOKEN}) == TOKEN


def test_un_jeton_absent_ou_suspect_n_est_jamais_renvoye():
    for value in (None, "", 42, ["x"], "a" * 1025, "ptc1.a\r\nx-injecte: 1", "a b", "a,b"):
        assert tc.from_frame({"turnContext": value}) is None, repr(value)
    assert tc.from_frame({}) is None
    assert tc.from_frame(None) is None


# --- 2. Task-local ------------------------------------------------------------

def test_hors_tour_aucun_en_tete():
    assert tc.current() is None
    assert tc.headers() == {}


def test_enter_leave_pose_puis_retire():
    reset = tc.enter(TOKEN)
    try:
        assert tc.headers() == {"x-pulse-turn-context": TOKEN}
    finally:
        tc.leave(reset)
    assert tc.current() is None


def test_deux_tours_simultanes_gardent_chacun_leur_jeton():
    async def turn(token, seen):
        reset = tc.enter(token)
        try:
            await asyncio.sleep(0)  # l'autre tour avance entre-temps
            seen.append(tc.current())
            await asyncio.sleep(0)
            seen.append(tc.current())
        finally:
            tc.leave(reset)

    async def run():
        a, b = [], []
        await asyncio.gather(turn(TOKEN, a), turn(OTHER, b))
        return a, b

    a, b = asyncio.run(run())
    assert a == [TOKEN, TOKEN]
    assert b == [OTHER, OTHER]


def test_le_jeton_suit_le_tour_dans_ses_taches_et_ses_fils_d_outil():
    async def read():
        return tc.current()

    async def run():
        loop = asyncio.get_running_loop()
        reset = tc.enter(TOKEN)
        try:
            # Tache creee pendant le tour (la passerelle cree ainsi la tache du tour).
            in_task = await asyncio.create_task(read())
            # Fil d'outil (``asyncio.to_thread`` copie le contexte).
            in_thread = await asyncio.to_thread(tc.current)

            # Un outil, dans son fil, renvoie une coroutine sur la boucle de
            # l'adaptateur (approbations, coffre) : le contexte du fil suit.
            def tool():
                return asyncio.run_coroutine_threadsafe(read(), loop).result(timeout=5)

            from_tool = await asyncio.to_thread(tool)
        finally:
            tc.leave(reset)
        # Hors du tour, plus rien.
        after = await asyncio.to_thread(tc.current)
        return in_task, in_thread, from_tool, after

    in_task, in_thread, from_tool, after = asyncio.run(run())
    assert (in_task, in_thread, from_tool) == (TOKEN, TOKEN, TOKEN)
    assert after is None


# --- 3. Adaptateur : du message.created aux appels HTTP -----------------------

def test_les_appels_du_tour_portent_le_jeton_de_sa_trame(monkeypatch):
    adapter = adapter_module.PulseChatAdapter(_Config())
    requests = _capture_http(monkeypatch)
    turns = []

    async def handle_message(event):
        # Comme la passerelle : le tour tourne dans une TACHE creee ici.
        turns.append(asyncio.create_task(adapter.send("canal-test", "reponse")))

    async def no_ack(message_id):
        return None

    adapter.handle_message = handle_message
    adapter._send_ack = no_ack

    async def run():
        await adapter._handle_message_created(_frame("msg-1", TOKEN))
        # Le jeton ne reste pas pose pour la trame suivante.
        assert tc.current() is None
        await adapter._handle_message_created(_frame("msg-2"))
        await asyncio.gather(*turns)

    asyncio.run(run())

    assert len(requests) == 2
    # Les deux tours tournent en parallele : l'ordre des appels n'est pas garanti.
    # Un appel porte le jeton de SA trame ; l'autre (app anterieure, trame sans
    # jeton) part exactement comme avant, sans en-tete.
    seen = sorted(_header(request, "x-pulse-turn-context") or "" for request in requests)
    assert seen == ["", TOKEN]
    for request in requests:
        assert _header(request, "Authorization") == "Bearer token-test"


def test_une_decision_injectee_sans_attente_porte_son_propre_jeton():
    """Reprise apres redemarrage : le tour neuf porte le jeton (stable) de la decision."""
    adapter = adapter_module.PulseChatAdapter(_Config())
    seen = []

    async def handle_message(event):
        seen.append(tc.current())

    adapter.handle_message = handle_message
    frame = {
        "type": "gate.reply",
        "turnContext": OTHER,
        "channel": {"slug": "compta", "name": "Compta"},
        "gate": {
            "requestId": "gate-reprise",
            "title": "Plan",
            "decision": "approved",
            "comment": None,
            "decidedBy": {"userId": "u1", "userName": "Killian"},
            "decidedAt": "2026-11-25T10:00:00.000Z",
        },
    }

    reset = tc.enter(TOKEN)  # un tour precedent du meme fil
    try:
        asyncio.run(adapter._handle_gate_reply(frame))
    finally:
        tc.leave(reset)

    assert seen == [OTHER]


def test_un_message_mis_en_file_n_herite_pas_du_jeton_du_tour_en_cours():
    """Hermes traite un message mis en file dans une tache creee DEPUIS le tour
    en cours (``_spawn_drain_task``) : ``on_processing_start`` repose le jeton
    de CET evenement, ou l'efface s'il n'en a pas."""
    adapter = adapter_module.PulseChatAdapter(_Config())
    events = {}

    async def handle_message(event):
        events[event.message_id] = event

    async def no_ack(message_id):
        return None

    adapter.handle_message = handle_message
    adapter._send_ack = no_ack

    async def process(event):
        # ``_process_message_background`` : le crochet, puis l'agent, dans la tache.
        await adapter.on_processing_start(event)
        return tc.current()

    async def run():
        await adapter._handle_message_created(_frame("msg-a", TOKEN))
        await adapter._handle_message_created(_frame("msg-b", OTHER))
        await adapter._handle_message_created(_frame("msg-c"))
        reset = tc.enter(TOKEN)  # la tache du tour de msg-a
        try:
            queued_b = await asyncio.create_task(process(events["msg-b"]))
            queued_c = await asyncio.create_task(process(events["msg-c"]))
        finally:
            tc.leave(reset)
        return queued_b, queued_c

    assert asyncio.run(run()) == (OTHER, None)


# --- 4. Middleware ``llm_request`` : vers la passerelle IA seulement ----------

PULSE = "https://pulse.example.com"
PRIMARY = f"{PULSE}/api/agent/ai-gateway/zephyr/v1"
FALLBACK = f"{PULSE}/api/agent/ai-gateway/zephyr/fallback/v1"


def _request(**extra):
    return {"model": "m", "messages": [{"role": "user", "content": "x"}], **extra}


def _llm(request, base_url):
    """Comme ``invoke_middleware`` d'Hermes : kwargs nommes, champs additifs."""
    return adapter_module._on_llm_request(
        request=request,
        original_request=request,
        base_url=base_url,
        api_mode="codex_responses",
        task_id="t",
        telemetry_schema_version="hermes.observer.v1",
    )


def test_dans_un_tour_l_appel_vers_la_passerelle_porte_le_jeton(monkeypatch):
    monkeypatch.setenv("PULSE_CHAT_URL", PULSE)
    request = _request()
    reset = tc.enter(TOKEN)
    try:
        primary = _llm(request, PRIMARY)
        fallback = _llm(request, FALLBACK + "/")
    finally:
        tc.leave(reset)
    for result in (primary, fallback):
        assert result["request"]["extra_headers"] == {"x-pulse-turn-context": TOKEN}
        assert result["request"]["messages"] == request["messages"]
        assert result["source"] == "pulse-chat"
    assert "extra_headers" not in request  # la requete recue n'est pas modifiee


def test_hors_tour_aucun_en_tete_vers_la_passerelle(monkeypatch):
    monkeypatch.setenv("PULSE_CHAT_URL", PULSE)
    assert _llm(_request(), PRIMARY) is None


def test_jamais_vers_un_autre_hote_ni_une_autre_route(monkeypatch):
    monkeypatch.setenv("PULSE_CHAT_URL", PULSE)
    reset = tc.enter(TOKEN)
    try:
        for url in (
            "https://api.anthropic.com/v1",
            "https://openrouter.ai/api/v1",
            "https://pulse.example.com.autre.tld/api/agent/ai-gateway/zephyr/v1",
            f"{PULSE}/api/agent/messages",
            f"{PULSE}/api/agent/ai-gateway",
            None,
        ):
            assert _llm(_request(), url) is None, url
    finally:
        tc.leave(reset)


def test_sans_pulse_chat_url_rien_n_est_ajoute(monkeypatch):
    monkeypatch.delenv("PULSE_CHAT_URL", raising=False)
    monkeypatch.setattr(adapter_module, "_PULSE_BASE_URL", "")
    reset = tc.enter(TOKEN)
    try:
        assert _llm(_request(), PRIMARY) is None
    finally:
        tc.leave(reset)


def test_pulse_chat_url_de_la_config_suffit(monkeypatch):
    monkeypatch.delenv("PULSE_CHAT_URL", raising=False)
    monkeypatch.setattr(adapter_module, "_PULSE_BASE_URL", "")

    class _Cfg:
        extra = {"url": PULSE + "/", "token": "token-test"}

    adapter_module.PulseChatAdapter(_Cfg())
    reset = tc.enter(TOKEN)
    try:
        result = _llm(_request(), PRIMARY)
    finally:
        tc.leave(reset)
    assert result["request"]["extra_headers"] == {"x-pulse-turn-context": TOKEN}


def test_les_en_tetes_existants_sont_conserves(monkeypatch):
    monkeypatch.setenv("PULSE_CHAT_URL", PULSE)
    request = _request(extra_headers={"anthropic-beta": "fast-mode"})
    reset = tc.enter(TOKEN)
    try:
        result = _llm(request, PRIMARY)
    finally:
        tc.leave(reset)
    assert result["request"]["extra_headers"] == {
        "anthropic-beta": "fast-mode",
        "x-pulse-turn-context": TOKEN,
    }
    assert request["extra_headers"] == {"anthropic-beta": "fast-mode"}


def test_deux_tours_concurrents_envoient_chacun_leur_jeton(monkeypatch):
    """Deux canaux en meme temps : chaque boucle de conversation tourne dans un
    fil a contexte copie (``copy_context().run``), le middleware y est appele
    de facon synchrone."""
    monkeypatch.setenv("PULSE_CHAT_URL", PULSE)

    async def turn(token):
        reset = tc.enter(token)
        try:
            seen = []
            for _ in range(3):
                result = await asyncio.to_thread(_llm, _request(), PRIMARY)
                seen.append(result["request"]["extra_headers"]["x-pulse-turn-context"])
                await asyncio.sleep(0)
            return seen
        finally:
            tc.leave(reset)

    async def run():
        return await asyncio.gather(turn(TOKEN), turn(OTHER))

    a, b = asyncio.run(run())
    assert a == [TOKEN] * 3
    assert b == [OTHER] * 3


def test_le_middleware_est_enregistre_aupres_d_hermes():
    registered = []

    class _Ctx:
        def register_middleware(self, kind, callback):
            registered.append((kind, callback))

    adapter_module._register_turn_context_middleware(_Ctx())
    assert registered == [("llm_request", adapter_module._on_llm_request)]


def test_un_hermes_sans_middleware_charge_quand_meme():
    adapter_module._register_turn_context_middleware(object())
