# -*- coding: utf-8 -*-
"""Banc d'un Hermes EPHEMERE complet : vraie gateway, faux modele, fausse app.

- ``FakeLLM`` : serveur compatible OpenAI (``/v1/chat/completions``, avec et
  sans flux). Il ne raisonne pas : il lit le dernier message humain, y trouve
  un marqueur ``E2E:<scenario>`` et joue le script de ce scenario (appel d'outil
  puis reponse finale). Tout autre appel (titre, resume…) recoit « ok ».
- ``FakeApp`` : la partie de Pulse Chat que voit le plugin — WebSocket
  ``/ws/hermes`` (hello, message.created, approval.reply…) et toutes les routes
  ``/api/*`` (enregistrees, 200).
- ``Gateway`` : ``hermes gateway run`` au premier plan, HOME et HERMES_HOME
  temporaires, le plugin copie dans ``plugins/pulse-chat``.

Aucun secret, aucun reseau hors 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(predicate: Callable[[], Any], timeout: float, what: str, gateway: "Gateway | None" = None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        if gateway is not None and gateway.proc.poll() is not None:
            raise AssertionError(f"gateway arretee pendant l'attente de {what}\n{gateway.tail()}")
        time.sleep(0.05)
    raise AssertionError(f"delai depasse : {what}" + (f"\n{gateway.tail()}" if gateway else ""))


# --------------------------------------------------------------------------
# Faux modele
# --------------------------------------------------------------------------


def _text(message: Dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


class FakeLLM:
    """Scripts : ``scenario -> fonction(tool_results) -> reponse``.

    Une reponse est ``{"text": ...}``, ``{"tool": nom, "args": {...}}`` ou
    ``{"tools": [{"tool": ..., "args": ...}, ...]}`` (appels paralleles).
    ``tool_results`` : contenus des messages ``tool`` venus APRES le dernier
    message humain, dans l'ordre.
    """

    def __init__(self):
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self.scripts: Dict[str, Callable[[List[str]], Dict[str, Any]]] = {}
        self.requests: List[Dict[str, Any]] = []
        banc = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def _json(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    self._json(200, {"object": "list", "data": [
                        {"id": "e2e-model", "object": "model", "owned_by": "e2e", "context_length": 200000}]})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    self._json(404, {"error": f"route inconnue {self.path}"})
                    return
                banc.requests.append(body)
                reply = banc.decide(body.get("messages") or [])
                if body.get("stream"):
                    self._stream(body, reply)
                else:
                    self._json(200, banc.completion(body, reply))

            def _stream(self, body, reply):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                model = body.get("model") or "e2e-model"

                def chunk(delta, finish=None, usage=None):
                    payload = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                               "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                    if usage is not None:
                        payload["usage"] = usage
                        payload["choices"] = []
                    self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                    self.wfile.flush()

                chunk({"role": "assistant", "content": ""})
                if reply.get("tools"):
                    for index, call in enumerate(reply["tools"]):
                        chunk({"tool_calls": [{"index": index, "id": call["call_id"], "type": "function",
                                               "function": {"name": call["tool"], "arguments": json.dumps(call["args"])}}]})
                    chunk({}, "tool_calls")
                else:
                    chunk({"content": reply["text"]})
                    chunk({}, "stop")
                chunk({}, usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def decide(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=None)
        if last_user is None:
            return {"text": "ok"}
        text = _text(messages[last_user])
        scenario = next((name for name in self.scripts if f"E2E:{name}" in text), None)
        if scenario is None:
            return {"text": "ok"}
        results = [_text(m) for m in messages[last_user + 1:] if m.get("role") == "tool"]
        reply = dict(self.scripts[scenario](results))
        if "tool" in reply:
            reply = {"tools": [{"tool": reply["tool"], "args": reply["args"]}]}
        for call in reply.get("tools") or []:
            call.setdefault("call_id", f"call_{uuid.uuid4().hex[:10]}")
        return reply

    @staticmethod
    def completion(body, reply):
        message: Dict[str, Any] = {"role": "assistant", "content": reply.get("text")}
        finish = "stop"
        if reply.get("tools"):
            message["content"] = None
            message["tool_calls"] = [{"id": call["call_id"], "type": "function",
                                      "function": {"name": call["tool"], "arguments": json.dumps(call["args"])}}
                                     for call in reply["tools"]]
            finish = "tool_calls"
        return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "created": int(time.time()),
                "model": body.get("model") or "e2e-model",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}

    def close(self):
        self.server.shutdown()


# --------------------------------------------------------------------------
# Fausse app Pulse Chat
# --------------------------------------------------------------------------


class FakeApp:
    def __init__(self):
        import uvicorn
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route, WebSocketRoute
        from starlette.websockets import WebSocket, WebSocketDisconnect

        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.token = "jeton-e2e"
        self.frames_in: List[Dict[str, Any]] = []  # recues du plugin par WS
        self.posts: List[Dict[str, Any]] = []      # {path, method, body}
        self.ws: Optional[WebSocket] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()
        app_self = self

        async def ws_endpoint(websocket: WebSocket):
            if websocket.headers.get("authorization") != f"Bearer {app_self.token}":
                await websocket.close(code=4401)
                return
            await websocket.accept()
            app_self.ws = websocket
            app_self.loop = asyncio.get_running_loop()
            try:
                while True:
                    raw = await websocket.receive_text()
                    try:
                        frame = json.loads(raw)
                    except ValueError:
                        continue
                    with app_self._lock:
                        app_self.frames_in.append(frame)
                    if frame.get("type") == "hello":
                        await websocket.send_text(json.dumps({"type": "hello.ack", "sessionToken": "sess-e2e"}))
            except WebSocketDisconnect:
                pass
            finally:
                if app_self.ws is websocket:
                    app_self.ws = None

        async def api(request: Request):
            body: Any = None
            raw = await request.body()
            if raw:
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {"_raw_bytes": len(raw)}
            with app_self._lock:
                app_self.posts.append({"path": request.url.path, "method": request.method, "body": body})
            return JSONResponse({"ok": True, "id": uuid.uuid4().hex})

        routes = [WebSocketRoute("/ws/hermes", ws_endpoint),
                  Route("/api/{rest:path}", api, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])]
        config = uvicorn.Config(Starlette(routes=routes), host="127.0.0.1", port=self.port, log_level="warning",
                                ws="websockets")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        wait_until(lambda: self.server.started, 10, "demarrage de la fausse app")

    # -- envoi vers le plugin
    def send(self, frame: Dict[str, Any]) -> None:
        assert self.ws is not None and self.loop is not None, "plugin non connecte"
        fut = asyncio.run_coroutine_threadsafe(self.ws.send_text(json.dumps(frame)), self.loop)
        fut.result(timeout=5)

    def say(self, slug: str, text: str, **message_extra) -> str:
        message_id = f"m-{uuid.uuid4().hex[:10]}"
        message = {"id": message_id, "text": text, "userId": "u-e2e", "userName": "Alice E2E"}
        message.update(message_extra)
        self.send({"type": "message.created",
                   "channel": {"slug": slug, "name": slug, "hermesProfile": "default"},
                   "message": message})
        return message_id

    def reply_approval(self, slug: str, request_id: str, decision: str) -> None:
        self.send({"type": "approval.reply", "channel": {"slug": slug, "name": slug, "hermesProfile": "default"},
                   "approval": {"requestId": request_id, "decision": decision,
                                "decidedBy": {"userId": "u-e2e", "userName": "Alice E2E"}}})

    def reply_question(self, slug: str, request_id: str, answer: str) -> None:
        self.send({"type": "question.reply", "channel": {"slug": slug, "name": slug, "hermesProfile": "default"},
                   "question": {"requestId": request_id, "answer": answer,
                                "answeredBy": {"userId": "u-e2e", "userName": "Alice E2E"}}})

    # -- lecture
    def messages(self, slug: Optional[str] = None, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            bodies = [p["body"] for p in self.posts if p["path"] == "/api/agent/messages" and isinstance(p["body"], dict)]
        return [b for b in bodies if (slug is None or b.get("channelSlug") == slug)
                and (kind is None or b.get("kind") == kind)]

    def frames(self, ftype: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [f for f in self.frames_in if f.get("type") == ftype]

    def close(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)


# --------------------------------------------------------------------------
# Gateway Hermes ephemere
# --------------------------------------------------------------------------


class Gateway:
    def __init__(self, root: pathlib.Path, llm: FakeLLM, app: FakeApp, approval_timeout: int = 30):
        self.root = root
        self.home = root / "home"
        self.home.mkdir()
        # HERMES_HOME HORS de ``$HOME/.hermes`` : lance depuis pytest, Hermes
        # refuse d'ouvrir un state.db sous ``$HOME/.hermes`` (« live-system
        # guard », il le prend pour celui d'une vraie installation).
        self.hermes_home = root / "hermes-home"
        plugin = self.hermes_home / "plugins" / "pulse-chat"
        plugin.mkdir(parents=True)
        for path in PLUGIN_DIR.iterdir():
            if path.suffix == ".py" or path.name == "plugin.yaml":
                shutil.copy2(path, plugin / path.name)
            elif path.name == "skills":
                shutil.copytree(path, plugin / "skills")
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        (self.hermes_home / "config.yaml").write_text(
            "model:\n"
            "  default: e2e-model\n"
            "  provider: custom\n"
            f"  base_url: {llm.base_url}\n"
            "  api_key: e2e-sans-valeur\n"
            "  context_length: 200000\n"
            "plugins:\n  enabled:\n    - pulse-chat\n"
            f"approvals:\n  mode: manual\n  timeout: {approval_timeout}\n"
            "terminal:\n  backend: local\n"
            f"  cwd: {self.workspace}\n",
            encoding="utf-8",
        )
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HERMES_", "PULSE_", "OPENAI", "ANTHROPIC", "OPENROUTER", "PYTEST"))}
        env.update({
            "HOME": str(self.home),
            "HERMES_HOME": str(self.hermes_home),
            "PULSE_CHAT_URL": app.base_url,
            "PULSE_CHAT_TOKEN": app.token,
            "PULSE_CHAT_ALLOW_ALL_USERS": "true",
            "PULSE_CHAT_VAULT_READ_ROOTS": str(self.workspace),
            "PYTHONUNBUFFERED": "1",
        })
        self.log_path = root / "gateway.log"
        self._log = open(self.log_path, "wb")
        hermes = pathlib.Path(sys.executable).parent / "hermes"
        cmd = [str(hermes)] if hermes.exists() else [sys.executable, "-m", "hermes_cli.main"]
        self.proc = subprocess.Popen(cmd + ["gateway", "run"], env=env, cwd=str(self.workspace),
                                     stdout=self._log, stderr=subprocess.STDOUT, start_new_session=True)

    def tail(self, lines: int = 80) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return ""

    def log(self) -> str:
        return self.log_path.read_text(errors="replace")

    def close(self):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=5)
        self._log.close()
