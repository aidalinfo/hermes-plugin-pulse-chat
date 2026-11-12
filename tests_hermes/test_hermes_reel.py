# -*- coding: utf-8 -*-
"""Le plugin contre un VRAI Hermes (issue #59) — aucun stub ``gateway.*``.

Lance par ``tests_hermes/run.sh <commit Hermes>`` (matrice CI), jamais par
``pytest tests`` : les tests unitaires installent des stubs des modules
d'Hermes dans ``sys.modules``, ceux-ci exigent les vrais.

Ce qui est reel : le chargeur de plugins (``hermes_cli.plugins``), le registre
des plateformes, ``BasePlatformAdapter`` et la file du garde-fou
(``tools.approval`` / ``_await_gateway_decision``), dans un HERMES_HOME
temporaire, sans secret ni reseau. Ce qui est simule : l'app Pulse Chat (le
POST de la carte et la trame ``approval.reply``) et le runner du gateway, dont
on rejoue les deux formes d'appel de ``send_exec_approval`` — sans
``request_id`` (Hermes 0.22.5) et avec (0.23.1, ``run_turn_runner.py:753``).
"""

import asyncio
import importlib.util
import inspect
import os
import pathlib
import shutil
import sys
import threading
import time
import types

import pytest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
PLUGIN_FILES = [p for p in PLUGIN_DIR.iterdir() if p.suffix == ".py" or p.name in ("plugin.yaml", "skills")]

# HERMES_HOME pose AVANT tout import d'Hermes : ses modules le lisent a l'import.
_HOME = pathlib.Path(os.environ.get("HERMES_MATRIX_HOME") or "")
assert _HOME.is_dir(), "HERMES_MATRIX_HOME doit etre un dossier temporaire (cf. tests_hermes/run.sh)"
os.environ["HERMES_HOME"] = str(_HOME)
os.environ["HOME"] = str(_HOME.parent)
# Hermes refuse le gateway sans identifiants : des valeurs factices, aucune
# connexion n'est ouverte par ces tests.
os.environ.setdefault("PULSE_CHAT_URL", "http://pulse-chat.invalid")
os.environ.setdefault("PULSE_CHAT_TOKEN", "jeton-de-test")

APPROVAL_TIMEOUT_S = 3


def _install_plugin():
    target = _HOME / "plugins" / "pulse-chat"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for path in PLUGIN_FILES:
        if path.is_dir():
            shutil.copytree(path, target / path.name)
        else:
            shutil.copy2(path, target / path.name)
    (_HOME / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - pulse-chat\n"
        f"approvals:\n  mode: manual\n  timeout: {APPROVAL_TIMEOUT_S}\n",
        encoding="utf-8",
    )
    return target


_install_plugin()


def _load_plugin_package():
    """Le plugin importe comme un paquet, avec les VRAIS modules d'Hermes."""
    name = "pulse_chat_matrix"
    if name in sys.modules:
        return sys.modules[f"{name}.adapter"]
    package = types.ModuleType(name)
    package.__path__ = [str(PLUGIN_DIR)]
    sys.modules[name] = package
    spec = importlib.util.spec_from_file_location(f"{name}.adapter", PLUGIN_DIR / "adapter.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


adapter_module = _load_plugin_package()


# --------------------------------------------------------------------------
# Chargement et enregistrement
# --------------------------------------------------------------------------


def test_les_imports_gardes_trouvent_les_vraies_API():
    import tools.approval as approval

    # Toujours present depuis les Hermes supportes.
    assert adapter_module.resolve_gateway_approval is approval.resolve_gateway_approval
    # API facultatives : presentes si et seulement si l'Hermes charge les a.
    for name in ("resolve_gateway_approval_card", "restrict_gateway_approval_card_to_deny", "list_gateway_approvals"):
        assert getattr(adapter_module, name) is getattr(approval, name, None), name


def test_le_vrai_chargeur_enregistre_la_plateforme():
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load(force=True)
    loaded = {key: plugin for key, plugin in manager._plugins.items() if "pulse-chat" in str(key)}
    assert loaded, f"plugin non decouvert : {sorted(manager._plugins)}"
    for plugin in loaded.values():
        assert not plugin.error, plugin.error

    from gateway.platform_registry import platform_registry

    # ``get`` charge aussi une plateforme differee, comme le gateway au demarrage.
    entry = platform_registry.get("pulse_chat")
    assert entry is not None, sorted(platform_registry.registered_names())
    assert entry.source == "plugin"
    assert callable(entry.adapter_factory)
    assert entry.check_fn() is True


def test_l_adaptateur_herite_du_vrai_BasePlatformAdapter():
    from gateway.platforms.base import BasePlatformAdapter

    assert issubclass(adapter_module.PulseChatAdapter, BasePlatformAdapter)


def test_le_runner_rend_une_carte_et_l_appel_se_lie():
    """Ce que fait le vrai runner avant d'appeler la carte, puis la liaison de
    SA forme d'appel a la signature du plugin (le ``TypeError`` de l'issue)."""
    import gateway.run_turn_runner as runner

    cls = adapter_module.PulseChatAdapter
    assert runner._renders_exec_approval_buttons(cls) is True

    source = inspect.getsource(runner)
    passes_request_id = 'request_id=approval_data.get("request_id")' in source
    kwargs = {
        "chat_id": "demo", "command": "ls", "session_key": "s", "description": "d", "metadata": None,
        "allow_permanent": True, "allow_session": True, "smart_denied": False,
    }
    if passes_request_id:
        kwargs["request_id"] = "h-1"
    inspect.signature(cls.send_exec_approval).bind(object(), **kwargs)


# --------------------------------------------------------------------------
# Approbations sur la vraie file du garde-fou
# --------------------------------------------------------------------------


class _Config:
    extra = {"url": "http://pulse-chat.invalid", "token": "jeton-de-test"}


class _Banc:
    """Une boucle asyncio (le gateway) et l'adaptateur, la carte captee."""

    def __init__(self, with_request_id):
        self.with_request_id = with_request_id
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.adapter = adapter_module.PulseChatAdapter(_Config())
        self.posted = []
        self.posted_event = threading.Event()

        async def fake_post(payload, request_id):
            self.posted.append(payload)
            self.posted_event.set()
            return adapter_module.SendResult(success=True, message_id=request_id)

        self.adapter._post_agent_message = fake_post

    def notify_cb(self, session_key):
        """Le role du runner : appeler la carte depuis le thread agent."""

        def notify(data):
            kwargs = dict(
                chat_id="demo", command=data.get("command", ""), session_key=session_key,
                description=data.get("description") or "dangerous", metadata=None,
                allow_permanent=data.get("allow_permanent", True),
                allow_session=data.get("allow_session", True),
                smart_denied=data.get("smart_denied", False),
            )
            if self.with_request_id:
                kwargs["request_id"] = data.get("request_id")
            fut = asyncio.run_coroutine_threadsafe(self.adapter.send_exec_approval(**kwargs), self.loop)
            result = fut.result(timeout=5)
            if not result.success:
                raise RuntimeError(result.error)

        return notify

    def ask(self, session_key, command):
        """Une demande du garde-fou dans un thread agent ; rend (thread, resultat)."""
        from tools.approval_gateway_wait import _await_gateway_decision

        out = {}
        data = {"command": command, "pattern_key": command, "pattern_keys": [command], "description": "test"}

        def run():
            out.update(_await_gateway_decision(session_key, self.notify_cb(session_key), data))

        before = len(self.posted)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while len(self.posted) == before and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(self.posted) > before, "carte non postee"
        return thread, out, self.posted[-1]["requestId"]

    def reply(self, app_request_id, decision):
        frame = {
            "type": "approval.reply",
            "channel": {"slug": "demo", "name": "Demo", "hermesProfile": "default"},
            "approval": {"requestId": app_request_id, "decision": decision,
                         "decidedBy": {"userId": "u1", "userName": "Alice"}},
        }
        self.loop.call_soon_threadsafe(self.adapter._handle_approval_reply, frame)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


@pytest.fixture(params=[False, True], ids=["appel-0.22", "appel-0.23"])
def banc(request):
    b = _Banc(with_request_id=request.param)
    yield b
    b.close()


def _session(name):
    return f"agent:main:pulse-chat:dm:{name}:{time.monotonic_ns()}"


def test_once_libere_la_bonne_demande(banc):
    thread, out, app_id = banc.ask(_session("once"), "rm -rf /tmp/x")
    banc.reply(app_id, "once")
    thread.join(timeout=5)
    assert out.get("resolved") is True and out.get("choice") == "once", out


def test_deny_est_un_refus(banc):
    thread, out, app_id = banc.ask(_session("deny"), "rm -rf /tmp/y")
    banc.reply(app_id, "deny")
    thread.join(timeout=5)
    assert out.get("choice") == "deny", out


def test_sans_reponse_rien_n_est_accorde(banc):
    # Jamais automatique : l'expiration est un refus, pas un accord.
    thread, out, _ = banc.ask(_session("expire"), "rm -rf /tmp/z")
    thread.join(timeout=APPROVAL_TIMEOUT_S + 10)
    assert out.get("resolved") is False and out.get("choice") is None, out


def test_une_decision_rejouee_ou_tardive_ne_tranche_rien(banc):
    session = _session("rejeu")
    thread, out, app_id = banc.ask(session, "rm -rf /tmp/a")
    banc.reply(app_id, "deny")
    thread.join(timeout=5)
    assert out.get("choice") == "deny"
    # Une seconde demande de la meme session ne doit pas recevoir le rejeu.
    thread2, out2, app_id2 = banc.ask(session, "rm -rf /tmp/b")
    banc.reply(app_id, "once")
    time.sleep(0.3)
    assert thread2.is_alive(), f"le rejeu a tranche une autre demande : {out2}"
    banc.reply(app_id2, "deny")
    thread2.join(timeout=5)
    assert out2.get("choice") == "deny", out2


def test_deux_demandes_concurrentes_chacune_sa_decision(banc):
    session = _session("concurrentes")
    t1, out1, app1 = banc.ask(session, "rm -rf /tmp/c1")
    t2, out2, app2 = banc.ask(session, "rm -rf /tmp/c2")
    # La SECONDE carte est tranchee d'abord : en FIFO, elle trancherait la premiere.
    banc.reply(app2, "deny")
    time.sleep(0.3)
    assert t1.is_alive(), f"la decision de la carte 2 a tranche la demande 1 : {out1}"
    banc.reply(app1, "once")
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert out1.get("choice") == "once", out1
    assert out2.get("choice") == "deny", out2


def test_chaque_surcharge_accepte_les_arguments_de_la_base():
    """Le piege de l'issue #59, generalise : une methode de ``BasePlatformAdapter``
    surchargee ici doit accepter TOUT argument nomme que la base de cet Hermes
    declare — le runner appelle la base, pas notre signature."""
    from gateway.platforms.base import BasePlatformAdapter

    cls = adapter_module.PulseChatAdapter
    problems = []
    for name, ours in vars(cls).items():
        base = getattr(BasePlatformAdapter, name, None)
        if not callable(ours) or not callable(base) or name.startswith("__"):
            continue
        try:
            base_params = inspect.signature(base).parameters
            our_sig = inspect.signature(ours)
        except (TypeError, ValueError):
            continue
        our_params = our_sig.parameters
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in our_params.values()):
            continue
        for pname, p in base_params.items():
            if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            if pname not in our_params:
                problems.append(f"{name}({pname})")
    assert not problems, f"surcharges qui leveraient TypeError : {problems}"
