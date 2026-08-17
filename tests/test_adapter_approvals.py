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


def _capture_resolutions(monkeypatch):
    """Remplace ``resolve_gateway_approval`` (absent hors d'Hermes) par un espion.

    Il rend le nombre d'attentes debloquees, comme le vrai : ``0`` signifie que
    le garde-fou d'Hermes a deja expire.
    """
    resolutions = []

    def fake_resolve(session_key, choice, resolve_all=False, reason=None):
        resolutions.append((session_key, choice))
        return 1

    monkeypatch.setattr(adapter_module, "resolve_gateway_approval", fake_resolve)
    return resolutions


class TestSendExecApproval:
    """Le garde-fou d'Hermes doit produire une CARTE, pas une invite texte.

    Hermes teste ``getattr(type(adapter), "send_exec_approval")`` : sans cette
    methode il poste « tapez /approve » et aucune ligne `approvalRequest` n'est
    jamais creee — c'est exactement ce qu'on a constate en production.
    """

    def test_la_methode_existe_sur_la_CLASSE(self):
        # Hermes interroge la classe, pas l'instance (il se protege des mocks).
        assert getattr(adapter_module.PulseChatAdapter, "send_exec_approval", None)

    def test_poste_une_carte_et_rend_la_main_sans_attendre(self, monkeypatch):
        async def run():
            _capture_resolutions(monkeypatch)
            adapter, posted = _make_adapter()

            # Ne DOIT pas bloquer : Hermes borne cet envoi a 15 s puis tient le
            # thread agent de son cote.
            result = await asyncio.wait_for(
                adapter.send_exec_approval(
                    chat_id="demo",
                    command="rm -rf /var/tmp/cache",
                    session_key="sess-1",
                    description="Security scan — dotfile overwrite",
                ),
                timeout=1,
            )

            assert result.success is True
            assert len(posted) == 1
            assert posted[0]["kind"] == "approval_request"
            assert posted[0]["channelSlug"] == "demo"
            assert posted[0]["command"] == "rm -rf /var/tmp/cache"
            assert posted[0]["reason"] == "Security scan — dotfile overwrite"
            assert posted[0]["options"] == ["once", "session", "always", "deny"]

        asyncio.run(run())

    def test_la_decision_debloque_le_thread_agent_dHermes(self, monkeypatch):
        async def run():
            resolutions = _capture_resolutions(monkeypatch)
            adapter, posted = _make_adapter()
            await adapter.send_exec_approval(
                chat_id="demo", command="ls", session_key="sess-42"
            )
            request_id = posted[0]["requestId"]

            adapter._handle_approval_reply(_reply_frame(request_id, "session"))

            assert resolutions == [("sess-42", "session")]
            # Correlation consommee : un rejeu ne debloque pas deux fois.
            assert adapter._gateway_approvals == {}

        asyncio.run(run())

    def test_deny_est_transmis_tel_quel(self, monkeypatch):
        async def run():
            resolutions = _capture_resolutions(monkeypatch)
            adapter, posted = _make_adapter()
            await adapter.send_exec_approval(
                chat_id="demo", command="ls", session_key="sess-9"
            )
            adapter._handle_approval_reply(
                _reply_frame(posted[0]["requestId"], "deny")
            )
            assert resolutions == [("sess-9", "deny")]

        asyncio.run(run())

    def test_smart_deny_ne_propose_que_once(self, monkeypatch):
        async def run():
            _capture_resolutions(monkeypatch)
            adapter, posted = _make_adapter()
            await adapter.send_exec_approval(
                chat_id="demo",
                command="ls",
                session_key="sess-2",
                smart_denied=True,
            )
            assert posted[0]["options"] == ["once", "deny"]

        asyncio.run(run())

    def test_post_en_echec_rend_la_main_a_linvite_texte(self, monkeypatch):
        """``success=False`` fait reprendre a Hermes son message texte.

        C'est la seule degradation acceptable : lever une exception ou se taire
        laisserait l'humain devant un agent muet jusqu'a l'expiration du garde.
        """

        async def run():
            _capture_resolutions(monkeypatch)
            adapter, _ = _make_adapter(post_ok=False)
            result = await adapter.send_exec_approval(
                chat_id="demo", command="ls", session_key="sess-3"
            )
            assert result.success is False
            # Correlation retiree : la carte n'existe pas, la decision non plus.
            assert adapter._gateway_approvals == {}

        asyncio.run(run())

    def test_sans_tools_approval_on_refuse_de_poster_la_carte(self, monkeypatch):
        """Des boutons sans deblocage seraient pires que l'invite texte."""

        async def run():
            monkeypatch.setattr(adapter_module, "resolve_gateway_approval", None)
            adapter, posted = _make_adapter()
            result = await adapter.send_exec_approval(
                chat_id="demo", command="ls", session_key="sess-4"
            )
            assert result.success is False
            assert posted == []

        asyncio.run(run())

    def test_garde_fou_expire_ne_leve_pas(self, monkeypatch):
        """Un clic arrive apres le delai d'Hermes : trace, jamais d'exception."""

        async def run():
            def fake_resolve(session_key, choice, resolve_all=False, reason=None):
                return 0  # plus rien en attente cote Hermes

            monkeypatch.setattr(
                adapter_module, "resolve_gateway_approval", fake_resolve
            )
            adapter, posted = _make_adapter()
            await adapter.send_exec_approval(
                chat_id="demo", command="ls", session_key="sess-5"
            )
            adapter._handle_approval_reply(_reply_frame(posted[0]["requestId"]))
            assert adapter._gateway_approvals == {}

        asyncio.run(run())

    def test_les_deux_chemins_ne_se_marchent_pas_dessus(self, monkeypatch):
        """``request_approval`` attend ici ; le garde-fou attend chez Hermes."""

        async def run():
            resolutions = _capture_resolutions(monkeypatch)
            adapter, posted = _make_adapter()

            task = asyncio.create_task(
                adapter.request_approval(chat_id="demo", tool="sh", command="ls")
            )
            await _settle()
            explicite = posted[0]["requestId"]

            await adapter.send_exec_approval(
                chat_id="demo", command="whoami", session_key="sess-6"
            )
            garde = posted[1]["requestId"]

            adapter._handle_approval_reply(_reply_frame(garde, "once"))
            assert resolutions == [("sess-6", "once")]
            assert not task.done()  # l'attente explicite est intacte

            adapter._handle_approval_reply(_reply_frame(explicite, "deny"))
            result = await asyncio.wait_for(task, timeout=1)
            assert result["granted"] is False
            assert resolutions == [("sess-6", "once")]

        asyncio.run(run())


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


def test_timeout_refuse_explicitement():
    async def run():
        adapter, _ = _make_adapter()
        result = await adapter.request_approval(
            chat_id="demo", tool="sh", command="ls", timeout=0.01
        )
        # Jamais None : un echec ne doit pas pouvoir ressembler a une autorisation.
        assert result["granted"] is False
        assert result["status"] == "timeout"
        assert result["decision"] is None
        assert adapter._pending_approvals == {}

    asyncio.run(run())


def test_post_en_echec_refuse_explicitement():
    async def run():
        adapter, _ = _make_adapter(post_ok=False)
        result = await adapter.request_approval(chat_id="demo", tool="sh", command="ls")
        assert result["granted"] is False
        assert result["status"] == "not_sent"
        assert adapter._pending_approvals == {}

    asyncio.run(run())


def test_toutes_les_issues_ont_la_meme_forme():
    """L'appelant ne doit jamais avoir a distinguer les chemins."""

    async def run():
        adapter, posted = _make_adapter()
        task = asyncio.create_task(
            adapter.request_approval(chat_id="demo", tool="sh", command="ls")
        )
        await _settle()
        adapter._handle_approval_reply(_reply_frame(posted[0]["requestId"]))
        accorde = await asyncio.wait_for(task, timeout=1)

        refuse = await adapter.request_approval(
            chat_id="demo", tool="sh", command="ls", timeout=0.01
        )

        assert set(accorde) == set(refuse)
        for issue in (accorde, refuse):
            assert isinstance(issue["granted"], bool)
            assert issue["status"] in ("decided", "timeout", "not_sent")

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


def test_resume_et_impacts_partent_dans_le_payload():
    """Ce que l'humain LIT avant de cliquer doit arriver jusqu'a la carte."""

    async def run():
        adapter, posted = _make_adapter()
        task = asyncio.create_task(
            adapter.request_approval(
                chat_id="demo",
                tool="execute_code",
                command="ssh-copy-id ops@atelier-02",
                summary="Installer la cle publique sur atelier-02.",
                risks=["Acces SSH au poste atelier-02", "Ecriture dans ~/.ssh"],
            )
        )
        await _settle()

        assert posted[0]["summary"] == "Installer la cle publique sur atelier-02."
        assert posted[0]["risks"] == [
            "Acces SSH au poste atelier-02",
            "Ecriture dans ~/.ssh",
        ]

        adapter._handle_approval_reply(_reply_frame(posted[0]["requestId"]))
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())


def test_reemission_du_meme_request_id_est_debloquee_par_la_reponse():
    """Une demande deja tranchee cote app repond a la reemission.

    Le futur est arme AVANT le POST et ``timeout`` vaut None par defaut : si
    l'app se contentait d'un ``{id}`` muet, cet appel n'aurait aucune issue.
    Ici on verifie le pendant cote plugin — la reponse a une reemission
    debloque bien l'attente, avec le meme ``request_id`` impose par l'appelant.
    """

    async def run():
        adapter, posted = _make_adapter()
        task = asyncio.create_task(
            adapter.request_approval(
                chat_id="demo",
                tool="execute_code",
                command="print(1)",
                request_id="req-fixe",
            )
        )
        await _settle()

        assert posted[0]["requestId"] == "req-fixe"
        assert not task.done()

        adapter._handle_approval_reply(_reply_frame("req-fixe", "once"))
        result = await asyncio.wait_for(task, timeout=1)
        assert result["granted"] is True
        assert result["status"] == "decided"
        assert adapter._pending_approvals == {}

    asyncio.run(run())
