"""Séquencement des modèles d'une session (A27) — mêmes scénarios sur SQLite ET sur un vrai PostgreSQL.

Règle SERVEUR : le repli 26B n'est admis qu'après une tentative principale (31B) TERMINÉE avec un refus établi avant traitement
(``released``) ou une consommation connue (``settled``) ; jamais tant qu'une génération d'un autre modèle de la session est en vol
ou d'usage inconnu, quel que soit le client (timeout, retry, connexions concurrentes, commande hostile). Les autres sessions
(autres rôles / allocations) restent concurrentes.
"""

from __future__ import annotations

import asyncio

import pytest
from w5_broker_contract import chat, open_worker, service_for
from w5_broker_support import FakeUpstream, chat_request, http_error

from collegue.broker import BrokerBlocked, BrokerForbidden, BrokerRequestRefused, BrokerUpstreamRejected

PRIMARY = "gemma-4-31b-it"
FALLBACK = "gemma-4-26b-a4b-it"


class ModelGatedUpstream(FakeUpstream):
    """Émissions du modèle principal retenues tant que ``release`` n'est pas posé ; ``sent`` signale la première émission."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.sent = asyncio.Event()
        self.release = asyncio.Event()
        self.models = []
        self.primary_error = None

    async def generate(self, request):
        self.models.append(request.model)
        if request.model == PRIMARY:
            self.sent.set()
            await self.release.wait()
            if self.primary_error is not None:
                raise self.primary_error
        return await super().generate(request)


async def _wait_sent(upstream):
    await asyncio.wait_for(upstream.sent.wait(), 5)


async def test_a_fallback_is_refused_while_the_primary_emission_is_in_flight_even_after_the_client_gave_up(manager):
    upstream = ModelGatedUpstream()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    primary = asyncio.create_task(chat(service, worker, chat_request(model=PRIMARY), request_id="p1"))
    await _wait_sent(upstream)
    with pytest.raises(asyncio.TimeoutError):  # le client abandonne : le serveur, lui, continue de traiter
        await asyncio.wait_for(asyncio.shield(primary), timeout=0.01)

    with pytest.raises(BrokerForbidden) as caught:
        await chat(service, worker, chat_request(model=FALLBACK), request_id="f1")
    assert caught.value.code == "fallback_not_authorized"
    assert upstream.models == [PRIMARY]  # AUCUNE émission de repli
    upstream.release.set()
    assert (await primary)["model"] == PRIMARY
    snapshot = ledger.snapshot(worker.scope_key)
    assert (snapshot.reserved_tokens, snapshot.unknown_tokens) == (0, 0)  # la tentative refusée n'a rien gardé
    assert not ledger.snapshot(scope).blocked


async def test_the_fallback_is_allowed_after_the_lost_primary_response_is_finally_settled(manager):
    upstream = ModelGatedUpstream()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    primary = asyncio.create_task(chat(service, worker, chat_request(model=PRIMARY), request_id="p1"))
    await _wait_sent(upstream)
    upstream.release.set()
    await primary  # consommation CONNUE (15 jetons) : le repli est désormais autorisé
    assert (await chat(service, worker, chat_request(model=FALLBACK), request_id="f1"))["model"] == FALLBACK
    assert upstream.models == [PRIMARY, FALLBACK]
    assert ledger.snapshot(worker.scope_key).consumed_tokens == 30


async def test_a_fallback_without_any_authorizing_antecedent_is_refused_and_nothing_stays_reserved(manager):
    upstream = ModelGatedUpstream()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    for request_id in ("f1", "f2", None):  # même un renvoi répété : aucun antécédent n'apparaît par répétition
        with pytest.raises(BrokerForbidden) as caught:
            await chat(service, worker, chat_request(model=FALLBACK), request_id=request_id)
        assert caught.value.code == "fallback_not_authorized"
    assert upstream.models == [] and upstream.generate_calls == []
    snapshot = ledger.snapshot(worker.scope_key)
    assert (snapshot.reserved_tokens, snapshot.consumed_tokens, snapshot.unknown_tokens) == (0, 0, 0)


async def test_the_fallback_follows_a_primary_refusal_established_before_processing(manager):
    upstream = ModelGatedUpstream()
    upstream.release.set()
    upstream.primary_error = http_error(429)  # refus démontré : le fournisseur n'a rien traité
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    with pytest.raises(BrokerUpstreamRejected):
        await chat(service, worker, chat_request(model=PRIMARY), request_id="p1")
    assert (await chat(service, worker, chat_request(model=FALLBACK), request_id="f1"))["model"] == FALLBACK
    assert upstream.models == [PRIMARY, FALLBACK]
    assert not ledger.snapshot(scope).blocked


async def test_an_ambiguous_primary_failure_blocks_the_project_and_forbids_the_fallback(manager):
    upstream = ModelGatedUpstream()
    upstream.release.set()
    upstream.primary_error = http_error(500)  # échec APRÈS émission : usage inconnu
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    with pytest.raises(Exception):
        await chat(service, worker, chat_request(model=PRIMARY), request_id="p1")
    with pytest.raises(BrokerBlocked):
        await chat(service, worker, chat_request(model=FALLBACK), request_id="f1")
    assert upstream.models == [PRIMARY] and ledger.snapshot(scope).blocked


async def test_two_connections_of_the_same_session_cannot_race_the_primary_and_the_fallback(manager):
    upstream = ModelGatedUpstream()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    primary = asyncio.create_task(chat(service, worker, chat_request(model=PRIMARY), request_id="p1"))
    fallback = asyncio.create_task(chat(service, worker, chat_request(model=FALLBACK), request_id="f1"))
    await asyncio.sleep(0.3)
    upstream.release.set()
    results = await asyncio.gather(primary, fallback, return_exceptions=True)
    assert isinstance(results[1], BrokerForbidden) and results[1].code == "fallback_not_authorized"
    assert results[0]["model"] == PRIMARY and upstream.models == [PRIMARY]


async def test_a_same_model_retry_with_a_new_id_after_a_lost_response_cannot_emit_again(manager):
    """Serialisation par session : tant que l'issue de la première génération n'est pas connue, AUCUNE autre, même modèle."""
    upstream = ModelGatedUpstream()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    first = asyncio.create_task(chat(service, worker, chat_request(model=PRIMARY), request_id="p1"))
    await _wait_sent(upstream)
    reserved_by_the_first = ledger.snapshot(worker.scope_key).reserved_tokens
    assert reserved_by_the_first > 0
    with pytest.raises(asyncio.TimeoutError):  # le client perd la réponse ; le serveur continue
        await asyncio.wait_for(asyncio.shield(first), timeout=0.01)
    for request_id in ("p2", "p3", None):
        with pytest.raises(BrokerRequestRefused) as caught:
            await chat(service, worker, chat_request(model=PRIMARY), request_id=request_id)
        assert (caught.value.code, caught.value.status) == ("generation_in_flight", 429)
    assert upstream.models == [PRIMARY]  # UNE seule émission
    assert (
        ledger.snapshot(worker.scope_key).reserved_tokens == reserved_by_the_first
    )  # la réserve de la première est intacte, rien d'autre
    upstream.release.set()
    assert (await first)["model"] == PRIMARY
    snapshot = ledger.snapshot(worker.scope_key)
    assert (snapshot.consumed_tokens, snapshot.reserved_tokens, snapshot.unknown_tokens) == (15, 0, 0)
    assert not ledger.snapshot(scope).blocked  # le refus n'a créé aucune inconnue artificielle
    # une fois l'issue connue, le renvoi est une génération légitime ; le MÊME identifiant rejoue sans régénérer
    assert (await chat(service, worker, chat_request(model=PRIMARY), request_id="p2"))["model"] == PRIMARY
    again = await chat(service, worker, chat_request(model=PRIMARY), request_id="p1")
    assert again["model"] == PRIMARY and upstream.models == [PRIMARY, PRIMARY]


async def test_other_sessions_stay_concurrent_with_a_primary_in_flight(manager):
    """Concurrence légitime entre allocations : la règle ne vaut QUE dans une session."""
    upstream = ModelGatedUpstream()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    busy = open_worker(service, scope, rid)
    other_parent = ledger.reserve(
        scope, micro_usd=1, tokens=50_000, kind="worker", role="coder", model=PRIMARY, transport="worker"
    ).reservation_id
    other = open_worker(service, scope, other_parent)
    primary = asyncio.create_task(chat(service, busy, chat_request(model=PRIMARY), request_id="a1"))
    await _wait_sent(upstream)
    # une AUTRE session obtient son propre primaire (retenu comme l'autre) puis, une fois réglé, son repli
    other_primary = asyncio.create_task(chat(service, other, chat_request(model=PRIMARY), request_id="b1"))
    await asyncio.sleep(0.1)
    upstream.release.set()
    await asyncio.gather(primary, other_primary)
    assert (await chat(service, other, chat_request(model=FALLBACK), request_id="b2"))["model"] == FALLBACK
    assert upstream.models == [PRIMARY, PRIMARY, FALLBACK]


async def test_host_privileged_fallback_canaries_do_not_depend_on_a_session(manager):
    """Canaris de qualification (producteur de confiance, scope propre, aucune session) : le 26B reste exerçable avant le codeur."""
    upstream = FakeUpstream()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    completion = await service.sampling_completion(
        scope, "coder", chat_request(model=FALLBACK), request_id="canary-26b"
    )
    assert completion["model"] == FALLBACK and upstream.generate_calls


# ── précontrôle local AVANT countTokens (A29) : observable, sans réserve, sans changement d'usage connu ──────────────────────


def _attempts(service, session):
    from sqlalchemy import select

    from collegue.state.models import BrokerAttempt

    def read(db):
        return [
            (a.model, a.state, a.error_code, a.reservation_id)
            for a in db.scalars(select(BrokerAttempt).order_by(BrokerAttempt.id))
        ]

    return service.ledger._run(read)


async def test_an_unauthorized_fallback_never_reaches_the_provider_not_even_to_count(manager):
    upstream = ModelGatedUpstream()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    with pytest.raises(BrokerForbidden) as caught:
        await chat(service, worker, chat_request(model=FALLBACK), request_id="forbidden-fallback")
    assert caught.value.code == "fallback_not_authorized"
    assert (
        upstream.count_calls == [] and upstream.generate_calls == []
    )  # AUCUNE requête fournisseur, countTokens compris
    # observable : la tentative est journalisée et libérée ; rien n'a été réservé ni imputé, aucune inconnue
    assert _attempts(service, worker) == [(FALLBACK, "released", "fallback_not_authorized", None)]
    snapshot = ledger.snapshot(worker.scope_key)
    assert (snapshot.reserved_tokens, snapshot.consumed_tokens, snapshot.unknown_tokens) == (0, 0, 0)
    assert not snapshot.blocked and not ledger.snapshot(scope).blocked


async def test_a_busy_session_refuses_a_new_generation_before_counting_it(manager):
    upstream = ModelGatedUpstream()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    first = asyncio.create_task(chat(service, worker, chat_request(model=PRIMARY), request_id="p1"))
    await _wait_sent(upstream)
    counted_before = len(upstream.count_calls)
    reserved_before = ledger.snapshot(worker.scope_key).reserved_tokens
    with pytest.raises(BrokerRequestRefused) as caught:
        await chat(service, worker, chat_request(model=PRIMARY), request_id="p2")
    assert (caught.value.code, caught.value.status) == ("generation_in_flight", 429)
    assert len(upstream.count_calls) == counted_before  # le refus précoce n'a pas atteint le fournisseur
    assert (
        ledger.snapshot(worker.scope_key).reserved_tokens == reserved_before
    )  # réserve de la première intacte, rien d'abandonné
    upstream.release.set()
    await first
    snapshot = ledger.snapshot(worker.scope_key)
    assert (snapshot.consumed_tokens, snapshot.reserved_tokens, snapshot.unknown_tokens) == (15, 0, 0)
    assert [a[:3] for a in _attempts(service, worker)] == [
        (PRIMARY, "settled", None),
        (PRIMARY, "released", "generation_in_flight"),
    ]


async def test_the_final_admission_stays_decisive_when_the_session_gets_busy_during_count_tokens(manager):
    """Le précontrôle ne remplace pas l'admission transactionnelle : une génération admise PENDANT countTokens interdit l'émission de la première."""

    class FirstCountPaused(ModelGatedUpstream):
        def __init__(self):
            super().__init__()
            self.count_started = asyncio.Event()
            self.count_release = asyncio.Event()

        async def count_tokens(self, request):
            first = not self.count_calls
            counted = await super().count_tokens(request)
            if first:
                self.count_started.set()
                await self.count_release.wait()
            return counted

    upstream = FirstCountPaused()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    slow = asyncio.create_task(chat(service, worker, chat_request(model=PRIMARY), request_id="slow"))
    await asyncio.wait_for(upstream.count_started.wait(), 5)  # le précontrôle de « slow » est passé (session libre)
    fast = asyncio.create_task(chat(service, worker, chat_request(model=PRIMARY), request_id="fast"))
    await _wait_sent(upstream)  # « fast » est admise et en vol
    upstream.count_release.set()  # « slow » reprend : réservation, puis admission finale
    with pytest.raises(BrokerRequestRefused) as caught:
        await slow
    assert caught.value.code == "generation_in_flight"
    assert upstream.models == [PRIMARY]  # une seule émission
    upstream.release.set()
    await fast
    snapshot = ledger.snapshot(worker.scope_key)
    assert (snapshot.consumed_tokens, snapshot.reserved_tokens, snapshot.unknown_tokens) == (
        15,
        0,
        0,
    )  # « slow » : réserve libérée
    assert not ledger.snapshot(scope).blocked
