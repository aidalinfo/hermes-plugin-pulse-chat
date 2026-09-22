# -*- coding: utf-8 -*-
"""Aller-retour d'une QUESTION cote adaptateur (sans hermes installe).

Reutilise les stubs ``gateway.*`` et le chargement par chemin de
``test_adapter_dedup``. Ce qui est verifie ici :

  - la CLASSE definit bien ``send_clarify`` (le runner d'Hermes teste
    ``type(adapter).send_clarify is BasePlatformAdapter.send_clarify`` pour
    savoir s'il existe un repli texte a replanifier — sans override sur la
    classe, la carte n'existe simplement pas) ;
  - la carte poste et rend la main sans attendre ;
  - la reponse debloque le thread agent via ``resolve_gateway_clarify``, avec
    le ``clarify_id`` TEL QUEL ;
  - un echec de POST rend la main a l'invite texte d'Hermes, sans la doubler ;
  - le retrait ne poste que pour une carte reellement posee, et ne leve jamais.

Style du depot : ``asyncio.run`` plutot que pytest-asyncio.
"""

import asyncio

from test_adapter_dedup import _load_adapter_module

adapter_module = _load_adapter_module()

CLARIFY_ID = "clr-abc123"


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


def _reply_frame(request_id, answer="Lecture seule et alertes"):
    return {
        "type": "question.reply",
        "channel": {"slug": "demo", "name": "Demo", "hermesProfile": "default"},
        "question": {
            "requestId": request_id,
            "answer": answer,
            "answeredBy": {"userId": "u1", "userName": "Alice"},
            "answeredAt": "2026-09-22T08:00:00.000Z",
        },
    }


def _capture_resolutions(monkeypatch):
    """Remplace ``resolve_gateway_clarify`` (absent hors d'Hermes) par un espion.

    Il rend un booleen comme le vrai : ``False`` signifie que l'attente d'Hermes
    n'existe plus (delai ecoule, ``/new``, ou bot redemarre).
    """
    resolutions = []

    def fake_resolve(clarify_id, response):
        resolutions.append((clarify_id, response))
        return True

    monkeypatch.setattr(adapter_module, "resolve_gateway_clarify", fake_resolve)
    return resolutions


CHOICES = [
    "Lecture + commentaires de revue/CI + creation d'issues (Recommended)",
    "Lecture seule et alertes, sans ecrire sur GitHub",
]


class TestSendClarify:
    def test_la_methode_existe_sur_la_CLASSE(self):
        """Le runner teste la CLASSE, pas l'instance.

        ``run_turn_runner_clarify_delivery.text_fallback_coro`` compare
        ``type(adapter).send_clarify`` a celui de la base : une methode posee
        sur l'instance passerait inapercue, et la carte ne serait jamais
        tentee.
        """
        assert "send_clarify" in vars(adapter_module.PulseChatAdapter)

    def test_poste_une_carte_et_rend_la_main_sans_attendre(self, monkeypatch):
        async def run():
            _capture_resolutions(monkeypatch)
            adapter, posted = _make_adapter()

            result = await asyncio.wait_for(
                adapter.send_clarify(
                    chat_id="demo",
                    question="Quel niveau d'action GitHub ?",
                    choices=CHOICES,
                    clarify_id=CLARIFY_ID,
                    session_key="sess-1",
                ),
                timeout=1,
            )

            assert result.success
            assert len(posted) == 1
            assert posted[0]["kind"] == "question_request"
            assert posted[0]["channelSlug"] == "demo"
            # Le clarify_id part TEL QUEL : c'est lui qui debloque le thread.
            assert posted[0]["requestId"] == CLARIFY_ID
            assert posted[0]["choices"] == CHOICES

        asyncio.run(run())

    def test_une_question_ouverte_part_sans_choix(self, monkeypatch):
        """``choices`` OMIS, jamais envoye a ``[]`` — cf. build_question_payload."""

        async def run():
            _capture_resolutions(monkeypatch)
            adapter, posted = _make_adapter()

            await adapter.send_clarify(
                chat_id="demo",
                question="Que veux-tu que je fasse ensuite ?",
                choices=None,
                clarify_id=CLARIFY_ID,
                session_key="sess-1",
            )

            assert "choices" not in posted[0]

        asyncio.run(run())

    def test_la_reponse_debloque_le_thread_agent_dHermes(self, monkeypatch):
        async def run():
            resolutions = _capture_resolutions(monkeypatch)
            adapter, _ = _make_adapter()

            await adapter.send_clarify(
                chat_id="demo",
                question="Quel niveau ?",
                choices=CHOICES,
                clarify_id=CLARIFY_ID,
                session_key="sess-1",
            )
            adapter._handle_question_reply(_reply_frame(CLARIFY_ID))

            # Le libelle BRUT, tel que l'app l'a resolu depuis sa ligne.
            assert resolutions == [(CLARIFY_ID, "Lecture seule et alertes")]

        asyncio.run(run())

    def test_post_en_echec_rend_la_main_a_linvite_texte(self, monkeypatch):
        """Un seul POST, et `success=False` : c'est le RUNNER qui replanifie.

        Rappeler ``super().send_clarify()`` ici enverrait DEUX invites pour une
        question — la carte ratee, puis la liste numerotee, puis encore celle du
        runner.
        """

        async def run():
            _capture_resolutions(monkeypatch)
            adapter, posted = _make_adapter(post_ok=False)

            result = await adapter.send_clarify(
                chat_id="demo",
                question="Quel niveau ?",
                choices=CHOICES,
                clarify_id=CLARIFY_ID,
                session_key="sess-1",
            )

            assert result.success is False
            assert len(posted) == 1
            # Correlation nettoyee : un retrait ne doit pas poster sur une
            # carte qui n'existe pas.
            assert CLARIFY_ID not in adapter._clarify_cards

        asyncio.run(run())

    def test_sans_clarify_gateway_on_refuse_de_poster_la_carte(self, monkeypatch):
        """Des boutons qu'on ne saurait pas denouer figeraient l'agent."""

        async def run():
            monkeypatch.setattr(adapter_module, "resolve_gateway_clarify", None)
            adapter, posted = _make_adapter()

            result = await adapter.send_clarify(
                chat_id="demo",
                question="Quel niveau ?",
                choices=CHOICES,
                clarify_id=CLARIFY_ID,
                session_key="sess-1",
            )

            assert result.success is False
            assert posted == []

        asyncio.run(run())

    def test_attente_deja_relachee_ne_leve_pas(self, monkeypatch):
        """Un humain qui repond apres la fin ne doit pas faire tomber la boucle WS."""

        async def run():
            def fake_resolve(clarify_id, response):
                return False

            monkeypatch.setattr(adapter_module, "resolve_gateway_clarify", fake_resolve)
            adapter, _ = _make_adapter()

            await adapter.send_clarify(
                chat_id="demo",
                question="Quel niveau ?",
                choices=CHOICES,
                clarify_id=CLARIFY_ID,
                session_key="sess-1",
            )
            adapter._handle_question_reply(_reply_frame(CLARIFY_ID))

        asyncio.run(run())

    def test_trame_inexploitable_ne_debloque_rien(self, monkeypatch):
        """Une reponse VIDE debloquerait l'agent sur une chaine vide."""

        async def run():
            resolutions = _capture_resolutions(monkeypatch)
            adapter, _ = _make_adapter()

            adapter._handle_question_reply({"type": "question.reply"})
            adapter._handle_question_reply(_reply_frame(CLARIFY_ID, answer="   "))
            adapter._handle_question_reply({"type": "autre.chose"})

            assert resolutions == []

        asyncio.run(run())


class TestRetireClarifyCard:
    def test_retire_une_carte_posee(self, monkeypatch):
        async def run():
            _capture_resolutions(monkeypatch)
            adapter, posted = _make_adapter()

            await adapter.send_clarify(
                chat_id="demo",
                question="Quel niveau ?",
                choices=CHOICES,
                clarify_id=CLARIFY_ID,
                session_key="sess-1",
            )
            await adapter.retire_clarify_card(CLARIFY_ID, "This prompt expired")

            assert posted[-1] == {
                "channelSlug": "demo",
                "kind": "question_retire",
                "requestId": CLARIFY_ID,
            }

        asyncio.run(run())

    def test_ne_poste_rien_sans_carte_connue(self, monkeypatch):
        """Multi-select ou repli texte : aucune carte a refermer."""

        async def run():
            _capture_resolutions(monkeypatch)
            adapter, posted = _make_adapter()

            await adapter.retire_clarify_card("inconnu", "expired")

            assert posted == []

        asyncio.run(run())

    def test_une_reponse_arrivee_avant_annule_le_retrait(self, monkeypatch):
        """La carte est terminale des qu'une reponse arrive.

        Sans ce depilage, un retrait planifie par le gateway posterait un
        « expiree » par-dessus une question repondue.
        """

        async def run():
            _capture_resolutions(monkeypatch)
            adapter, posted = _make_adapter()

            await adapter.send_clarify(
                chat_id="demo",
                question="Quel niveau ?",
                choices=CHOICES,
                clarify_id=CLARIFY_ID,
                session_key="sess-1",
            )
            adapter._handle_question_reply(_reply_frame(CLARIFY_ID))
            await adapter.retire_clarify_card(CLARIFY_ID, "expired")

            assert [p["kind"] for p in posted] == ["question_request"]

        asyncio.run(run())

    def test_un_echec_de_post_ne_leve_pas(self, monkeypatch):
        """Le gateway planifie ce retrait sans l'attendre : une exception ici
        remonterait dans une tache detachee."""

        async def run():
            _capture_resolutions(monkeypatch)
            adapter, _ = _make_adapter()

            await adapter.send_clarify(
                chat_id="demo",
                question="Quel niveau ?",
                choices=CHOICES,
                clarify_id=CLARIFY_ID,
                session_key="sess-1",
            )

            async def boom(payload, hermes_id):
                raise RuntimeError("bucket down")

            adapter._post_agent_message = boom
            await adapter.retire_clarify_card(CLARIFY_ID, "expired")

        asyncio.run(run())
