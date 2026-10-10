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

from collegue.broker import BrokerError, BrokerRequestRefused

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


# ── le diagnostic de refus ne recopie JAMAIS ce que le client a fourni (revue A31) ──────────────────────────────────────────

MARKER = "FAKE_PRIVATE_CONTENT_739a"


def _payload_with_marker(where):
    body = chat_request()
    if where == "unknown_field_name":
        body[MARKER] = "x"
    elif where == "role_value":
        body["messages"][0]["role"] = MARKER
    elif where == "model_value":
        body["model"] = MARKER
    elif where == "tool_name":
        body["tools"] = [{"type": "function", "function": {"name": MARKER, "parameters": 7}}]
    elif where == "response_format":
        body["response_format"] = {"type": MARKER}
    elif where == "message_key":
        body["messages"][0][MARKER] = "x"
    elif where == "content_part":
        body["messages"][0]["content"] = [{"type": MARKER, "text": "x"}]
    elif where == "stop":
        body["stop"] = [MARKER, 7]
    return body


@pytest.mark.parametrize(
    "where",
    [
        "unknown_field_name",
        "role_value",
        "model_value",
        "tool_name",
        "response_format",
        "message_key",
        "content_part",
        "stop",
    ],
)
async def test_the_pre_attempt_diagnostic_never_copies_what_the_client_supplied(manager, caplog, where):
    upstream = FakeUpstream()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    with caplog.at_level(logging.DEBUG):  # TOUS les niveaux, TOUS les journaux
        with pytest.raises(BrokerError) as caught:
            await chat(service, worker, _payload_with_marker(where))
    assert upstream.count_calls == [] and upstream.generate_calls == [] and _attempts(service, worker) == []
    assert MARKER not in caplog.text, f"contenu client recopié dans le journal ({where})"
    assert "requête refusée avant tentative" in caplog.text  # le diagnostic existe, sans le contenu
    # le refus explicite reste complet CÔTÉ CLIENT : la raison (champ nommé) n'est pas perdue pour lui
    if where == "unknown_field_name":
        assert MARKER in str(caught.value) and caught.value.code == "unsupported_field"


async def test_the_diagnostic_identifies_known_sdk_parameters_by_name_and_counts_the_others(manager, caplog):
    service, upstream, ledger, scope, rid = service_for(manager, FakeUpstream())
    worker = open_worker(service, scope, rid)
    body = chat_request(reasoning_effort="high", prompt_cache_retention="24h")
    body[MARKER] = 1
    body["autre_inconnu"] = 2
    with caplog.at_level(logging.WARNING, logger="collegue.broker.service"):
        with pytest.raises(BrokerRequestRefused):
            await chat(service, worker, body)
    line = next(r.getMessage() for r in caplog.records if "refusée avant tentative" in r.getMessage())
    assert "code=unsupported_field" in line and "statut=400" in line
    assert "parametres_connus=prompt_cache_retention,reasoning_effort" in line and "autres_parametres=2" in line
    assert MARKER not in line and "autre_inconnu" not in line


async def test_a_duplicate_key_in_the_raw_body_does_not_copy_the_key_either(manager, caplog):
    service, upstream, ledger, scope, rid = service_for(manager, FakeUpstream())
    worker = open_worker(service, scope, rid)
    raw = (
        '{"model":"gemma-4-31b-it","messages":[{"role":"user","content":"x"}],"%s":1,"%s":2}' % (MARKER, MARKER)
    ).encode()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(BrokerRequestRefused):
            await chat(service, worker, raw)
    assert MARKER not in caplog.text and "code=duplicate_json_key" in caplog.text
