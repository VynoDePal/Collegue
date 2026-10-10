"""Raccord avec le SDK OpenHands 1.19.1 (A31) — mêmes scénarios sur SQLite ET sur un vrai PostgreSQL.

Le SDK épingle ``prompt_cache_key`` (identifiant de conversation) sur chaque requête Chat Completions d'une conversation, sans option
publique pour l'ôter. Le courtier le tient pour une métadonnée de transport INERTE : validée puis retirée avant toute traduction.
Il n'atteint ni Google, ni le journal, ni la comptabilité ; tout autre champ inconnu reste refusé.
"""

from __future__ import annotations

import json
import logging

import pytest
from w5_broker_contract import chat, open_worker, service_for
from w5_broker_fallback_contract import _attempts
from w5_broker_support import FakeUpstream, chat_request

from collegue.broker import BrokerRequestRefused

CONVERSATION_ID = "6f1c3a52-8c1e-4d5e-9a57-0d6c1f3b2a10"


async def test_the_sdk_prompt_cache_key_reaches_neither_google_nor_the_journal_and_changes_no_accounting(manager):
    upstream = FakeUpstream()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    before = service.store.get_session(worker.session_id)

    with_key = await chat(service, worker, chat_request(prompt_cache_key=CONVERSATION_ID), request_id="with-key")
    without = await chat(service, worker, chat_request(), request_id="without-key")

    # corps COMPTÉ et corps ÉMIS : identiques, sans trace de la métadonnée
    assert upstream.count_calls[0]["body"] == upstream.count_calls[1]["body"]
    assert upstream.generate_calls[0]["body"] == upstream.generate_calls[1]["body"]
    assert CONVERSATION_ID not in json.dumps(upstream.count_calls + upstream.generate_calls)
    assert "cachedContent" not in json.dumps(upstream.generate_calls)  # aucun cache hébergé activé
    # comptabilité identique : même réserve, même règlement, même usage
    assert with_key["usage"] == without["usage"]
    rows = _attempts(service, worker)
    assert [row[:2] for row in rows] == [("gemma-4-31b-it", "settled")] * 2
    snapshot = ledger.snapshot(worker.scope_key)
    assert (snapshot.consumed_tokens, snapshot.reserved_tokens, snapshot.unknown_tokens) == (30, 0, 0)
    # session / rôle / allocation / échéance inchangés ; rien de la métadonnée n'est stocké
    after = service.store.get_session(worker.session_id)
    assert (after.role, after.scope_key, after.allowed_models, after.deadline_at, after.max_output_tokens) == (
        before.role,
        before.scope_key,
        before.allowed_models,
        before.deadline_at,
        before.max_output_tokens,
    )
    assert CONVERSATION_ID not in json.dumps([list(row) for row in rows], default=str)


async def test_the_same_request_id_with_another_cache_key_is_the_same_request_and_replays_without_regenerating(manager):
    upstream = FakeUpstream()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    first = await chat(service, worker, chat_request(prompt_cache_key="conv-a"), request_id="same")
    again = await chat(service, worker, chat_request(prompt_cache_key="conv-b"), request_id="same")
    assert (
        again == first and len(upstream.generate_calls) == 1
    )  # métadonnée inerte : aucune "autre" requête, aucun conflit
    assert ledger.snapshot(worker.scope_key).consumed_tokens == 15


@pytest.mark.parametrize(
    "bad", [None, "", 7, ["a"], "x" * 129, "a\nb"], ids=["null", "empty", "int", "list", "long", "ctrl"]
)
async def test_a_malformed_cache_key_is_refused_before_any_attempt_provider_traffic_or_reservation(manager, bad):
    upstream = FakeUpstream()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    with pytest.raises(BrokerRequestRefused) as caught:
        await chat(service, worker, chat_request(prompt_cache_key=bad), request_id="bad-key")
    assert caught.value.code == "invalid_parameter"
    assert upstream.count_calls == [] and upstream.generate_calls == [] and _attempts(service, worker) == []
    snapshot = ledger.snapshot(worker.scope_key)
    assert (snapshot.reserved_tokens, snapshot.consumed_tokens, snapshot.unknown_tokens) == (0, 0, 0)


@pytest.mark.parametrize(
    "field, value", [("prompt_cache_retention", "24h"), ("reasoning_effort", "high"), ("user", "x")]
)
async def test_every_other_unknown_field_stays_refused_and_the_refusal_names_it_in_the_service_log(
    manager, caplog, field, value
):
    upstream = FakeUpstream()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    with caplog.at_level(logging.WARNING, logger="collegue.broker.service"):
        with pytest.raises(BrokerRequestRefused) as caught:
            await chat(service, worker, chat_request(prompt_cache_key=CONVERSATION_ID, **{field: value}))
    assert caught.value.code == "unsupported_field"
    assert upstream.count_calls == [] and upstream.generate_calls == [] and _attempts(service, worker) == []
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "unsupported_field" in logged and field in logged  # cause lisible même si le client n'affiche que la classe
    assert CONVERSATION_ID not in logged
