# -*- coding: utf-8 -*-
"""Aller-retour d'approbation cote adaptateur (sans hermes installe).

Reutilise les stubs ``gateway.*`` et le chargement par chemin de
``test_adapter_dedup`` : ce qui est verifie ici, c'est l'attente asynchrone —
``request_approval`` bloque jusqu'a la trame ``approval.reply``, et une trame
inexploitable ne debloque JAMAIS une execution.

Style du depot : ``asyncio.run`` plutot que pytest-asyncio (pas de dependance
de test supplementaire pour le plugin).
"""

import asyncio

from test_adapter_dedup import _load_adapter_module

adapter_module = _load_adapter_module()


class _Config:
    extra = {"url": "http://pulse-chat.test", "token": "token-test"}


def _make_adapter(post_ok=True):
    adapter = adapter_module.PulseChatAdapter(_Config())
    posted = []

    async def fake_post(payload, hermes_id):
        posted.append(payload)
        return adapter_module.SendResult(success=post_ok, message_id=hermes_id)

    adapter._post_agent_message = fake_post
    return adapter, posted


def _reply_frame(request_id, decision="once"):
    return {
        "type": "approval.reply",
        "channel": {"slug": "demo", "name": "Demo", "hermesProfile": "default"},
        "approval": {
            "requestId": request_id,
            "decision": decision,
            "decidedBy": {"userId": "u1", "userName": "Alice"},
            "decidedAt": "2026-08-06T08:00:00.000Z",
        },
    }


async def _settle():
    """Laisse la coroutine poster et s'enregistrer avant qu'on reponde."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def test_request_approval_attend_la_reponse():
    async def run():
        adapter, posted = _make_adapter()
        task = asyncio.create_task(
            adapter.request_approval(
                chat_id="demo",
                tool="execute_code",
                command="print(1)",
                reason="execute_code script execution",
                options=["once", "deny"],
            )
        )
        await _settle()

        assert len(posted) == 1
        assert posted[0]["kind"] == "approval_request"
        assert posted[0]["channelSlug"] == "demo"
        assert posted[0]["options"] == ["once", "deny"]
        request_id = posted[0]["requestId"]
        assert not task.done()  # bloque tant que personne n'a tranche

        adapter._handle_approval_reply(_reply_frame(request_id))
        result = await asyncio.wait_for(task, timeout=1)

        assert result["granted"] is True
        assert result["decision"] == "once"
        assert result["decidedBy"]["userName"] == "Alice"
        assert adapter._pending_approvals == {}

    asyncio.run(run())


def test_deny_est_transmis_sans_granted():
    async def run():
        adapter, posted = _make_adapter()
        task = asyncio.create_task(
            adapter.request_approval(chat_id="demo", tool="sh", command="rm -rf /")
        )
        await _settle()

        adapter._handle_approval_reply(_reply_frame(posted[0]["requestId"], "deny"))
        result = await asyncio.wait_for(task, timeout=1)

        assert result["decision"] == "deny"
        assert result["granted"] is False

    asyncio.run(run())


def test_timeout_ne_vaut_pas_autorisation():
    async def run():
        adapter, _ = _make_adapter()
        result = await adapter.request_approval(
            chat_id="demo", tool="sh", command="ls", timeout=0.01
        )
        # None : l'appelant DOIT le traiter comme un refus.
        assert result is None
        assert adapter._pending_approvals == {}

    asyncio.run(run())


def test_post_en_echec_ne_bloque_pas():
    async def run():
        adapter, _ = _make_adapter(post_ok=False)
        result = await adapter.request_approval(chat_id="demo", tool="sh", command="ls")
        assert result is None
        assert adapter._pending_approvals == {}

    asyncio.run(run())


def test_trame_inexploitable_ne_debloque_rien():
    async def run():
        adapter, posted = _make_adapter()
        task = asyncio.create_task(
            adapter.request_approval(chat_id="demo", tool="sh", command="ls")
        )
        await _settle()
        request_id = posted[0]["requestId"]

        # Decision hors liste blanche, puis requestId inconnu : aucun deblocage.
        adapter._handle_approval_reply(_reply_frame(request_id, "vas-y"))
        adapter._handle_approval_reply(_reply_frame("req-autre", "once"))
        await asyncio.sleep(0)
        assert not task.done()

        adapter._handle_approval_reply(_reply_frame(request_id, "deny"))
        result = await asyncio.wait_for(task, timeout=1)
        assert result["granted"] is False

    asyncio.run(run())


def test_decision_sans_attente_active_est_ignoree_sans_erreur():
    async def run():
        adapter, _ = _make_adapter()
        # Rejeu d'une decision apres un timeout / redemarrage du plugin.
        adapter._handle_approval_reply(_reply_frame("req-orphelin"))
        assert adapter._pending_approvals == {}

    asyncio.run(run())
