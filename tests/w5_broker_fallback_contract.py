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

from collegue.broker import BrokerBlocked, BrokerForbidden, BrokerUpstreamRejected

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


async def test_a_same_model_retry_after_a_lost_response_is_accounted_on_its_own_and_never_free(manager):
    """Le renvoi du MÊME modèle (nouvelle identité) est une génération distincte : réservée, réglée, imputée — jamais gratuite."""
    upstream = ModelGatedUpstream()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    first = asyncio.create_task(chat(service, worker, chat_request(model=PRIMARY), request_id="p1"))
    await _wait_sent(upstream)
    retry = asyncio.create_task(chat(service, worker, chat_request(model=PRIMARY), request_id="p2"))
    await asyncio.sleep(0.2)
    upstream.release.set()
    done = await asyncio.gather(first, retry)
    assert [d["model"] for d in done] == [PRIMARY, PRIMARY]
    snapshot = ledger.snapshot(worker.scope_key)
    assert (snapshot.consumed_tokens, snapshot.reserved_tokens, snapshot.unknown_tokens) == (30, 0, 0)
    # le même identifiant rejoué ne régénère PAS : résultat déjà obtenu rendu tel quel
    again = await chat(service, worker, chat_request(model=PRIMARY), request_id="p1")
    assert again["model"] == PRIMARY and len(upstream.models) == 2


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
