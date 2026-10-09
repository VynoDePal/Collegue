"""Contrat du courtier W5 — mêmes scénarios sur SQLite ET sur un vrai PostgreSQL.

Chaque test prend la fixture ``manager`` (un ``ProjectStateManager`` frais) fournie par le module appelant :
``test_w5_broker.py`` (SQLite, toujours exécuté) et ``test_w5_broker_postgres.py`` (PostgreSQL réel : il ÉCHOUE, il ne se
saute pas, si PostgreSQL manque). Faux fournisseur compteur d'émissions, aucune clé, aucun réseau.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from w5_broker_support import (
    FakeUpstream,
    bad_body,
    chat_request,
    google_response,
    http_error,
    transport_error,
)

from collegue.broker import (
    BrokerAuthError,
    BrokerBlocked,
    BrokerBoundViolation,
    BrokerBudgetRefused,
    BrokerConfig,
    BrokerForbidden,
    BrokerRequestRefused,
    BrokerService,
    BrokerUnsupported,
    BrokerUpstreamAmbiguous,
    BrokerUpstreamRejected,
)
from collegue.state.models import BrokerAttempt


def setup(manager, *, cap_tokens=250_000, worker_tokens=100_000, worker_micro=500_000):
    pid = manager.create_project(name="w5")
    ledger = manager.budget_ledger
    scope = ledger.scope_for_project(pid, max_cost_usd=2.0, max_tokens=cap_tokens)
    reservation = ledger.reserve(
        scope.scope_key,
        micro_usd=worker_micro,
        tokens=worker_tokens,
        kind="worker",
        role="coder",
        model="gemma-4-31b-it",
        transport="worker",
    )
    return ledger, scope.scope_key, reservation.reservation_id


def service_for(manager, upstream=None, *, config=None, clock=None, **setup_kw):
    ledger, scope_key, parent_rid = setup(manager, **setup_kw)
    upstream = upstream or FakeUpstream()
    service = BrokerService(ledger, upstream, config=config or BrokerConfig(), clock=clock)
    return service, upstream, ledger, scope_key, parent_rid


def open_worker(service, scope_key, parent_rid, role="coder", **kw):
    return service.open_session(parent_scope_key=scope_key, parent_reservation_id=parent_rid, role=role, **kw)


async def chat(service, session, body=None, **kw):
    return await service.chat_completion(
        session.session_id, session.token, body if body is not None else chat_request(), **kw
    )


# ── flux nominal et comptabilité ─────────────────────────────────────────────────────────────────────────────


async def test_nominal_flow_counts_once_and_consolidates_into_the_parent(manager):
    service, upstream, ledger, scope_key, parent_rid = service_for(manager)
    session = open_worker(service, scope_key, parent_rid)

    completion = await chat(service, session)

    assert completion["choices"][0]["message"]["content"] == "ok"
    assert completion["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "completion_tokens_details": {"reasoning_tokens": 0},
    }
    # countTokens et generateContent portent le MÊME objet normalisé (instructions, outils et limite de sortie compris).
    assert len(upstream.count_calls) == len(upstream.generate_calls) == 1
    assert upstream.count_calls[0]["body"] == {"generateContentRequest": upstream.generate_calls[0]["body"]}
    assert upstream.generate_calls[0]["body"]["generationConfig"] == {"candidateCount": 1, "maxOutputTokens": 64}
    assert upstream.generate_calls[0]["body"]["model"] == "models/gemma-4-31b-it"
    child = ledger.snapshot(session.scope_key)
    assert (child.consumed_tokens, child.reserved_tokens, child.unknown_tokens) == (15, 0, 0)
    # tant que la session est ouverte, le projet ne voit que la réservation parent (aucun double comptage)
    during = ledger.snapshot(scope_key)
    assert (during.consumed_tokens, during.reserved_tokens) == (0, 100_000)

    summary = await service.close_session(session.session_id)

    assert (summary.consumed_tokens, summary.unknown, summary.parent_settlement) == (15, False, "committed")
    project = ledger.snapshot(scope_key)
    assert (project.consumed_tokens, project.reserved_tokens, project.unknown_tokens) == (15, 0, 0)
    assert project.consumed_micro_usd == 0 and not project.blocked


async def test_reasoning_tokens_are_counted_once(manager):
    upstream = FakeUpstream(response=google_response(prompt=10, candidates=5, thoughts=7))
    service, _, ledger, scope_key, parent_rid = service_for(manager, upstream)
    session = open_worker(service, scope_key, parent_rid)

    completion = await chat(service, session)

    assert completion["usage"]["completion_tokens"] == 12 and completion["usage"]["total_tokens"] == 22
    assert completion["usage"]["completion_tokens_details"]["reasoning_tokens"] == 7
    assert (
        ledger.snapshot(session.scope_key).consumed_tokens == 22
    )  # ni 15 (raisonnement perdu) ni 29 (double addition)


async def test_a_thought_part_is_never_returned_as_content(manager):
    parts = [{"text": "pensée", "thought": True}, {"text": "réponse"}]
    upstream = FakeUpstream(response=google_response(parts=parts, thoughts=3))
    service, _, _, scope_key, parent_rid = service_for(manager, upstream)
    session = open_worker(service, scope_key, parent_rid)
    completion = await chat(service, session)
    assert completion["choices"][0]["message"]["content"] == "réponse"


# ── usage absent / incohérent / borne démentie : bloque et signale ───────────────────────────────────────────


@pytest.mark.parametrize(
    "response, code",
    [
        (google_response(drop_usage=True), "usage_missing"),
        (google_response(prompt=10, candidates=5, total=99), "usage_inconsistent"),
        (google_response(finish="WEIRD"), "response_invalid"),
    ],
    ids=["usage-absent", "usage-incoherent", "finish-inconnu"],
)
async def test_unusable_usage_blocks_the_session_and_the_whole_project(manager, response, code):
    upstream = FakeUpstream(response=response)
    service, _, ledger, scope_key, parent_rid = service_for(manager, upstream)
    session = open_worker(service, scope_key, parent_rid)

    with pytest.raises(BrokerBoundViolation) as caught:
        await chat(service, session)
    assert caught.value.code == code

    child = ledger.snapshot(session.scope_key)
    assert child.blocked and child.unknown_tokens > 0  # réserve CONSERVÉE, jamais un zéro
    project = ledger.snapshot(scope_key)
    assert project.blocked  # l'enfant bloque IMMÉDIATEMENT le projet (avant toute fermeture)
    assert ledger.get_reservation(parent_rid).state == "unknown"
    with pytest.raises(BrokerBlocked):
        await chat(service, session)
    assert len(upstream.generate_calls) == 1  # aucune nouvelle émission
    summary = await service.close_session(session.session_id)
    assert summary.unknown and summary.parent_settlement == "unknown"


@pytest.mark.parametrize(
    "response, fragment",
    [
        (google_response(prompt=10, candidates=100), "sortie"),
        (google_response(prompt=10, candidates=30, thoughts=40), "sortie"),
        (google_response(prompt=50, candidates=5), "entrée"),
    ],
    ids=["sortie-depasse", "raisonnement-depasse", "entree-depasse"],
)
async def test_a_bound_violation_is_committed_as_measured_blocked_and_signalled_never_clamped(
    manager, response, fragment
):
    upstream = FakeUpstream(count=10, response=response)
    service, _, ledger, scope_key, parent_rid = service_for(manager, upstream)
    session = open_worker(service, scope_key, parent_rid)

    with pytest.raises(BrokerBoundViolation, match=fragment):
        await chat(service, session, chat_request(max_tokens=64))

    usage_total = response["usageMetadata"]["totalTokenCount"]
    child = ledger.snapshot(session.scope_key)
    assert child.consumed_tokens == usage_total  # la réalité est enregistrée telle quelle (pas de min/clamp)
    assert child.blocked and ledger.snapshot(scope_key).blocked
    assert any("broker" in str(b["reason"]) for b in ledger.open_blocks(scope_key))


# ── erreurs du fournisseur ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429])
async def test_a_proven_rejection_releases_the_reservation(manager, status):
    upstream = FakeUpstream()
    upstream.generate_error = http_error(status)
    service, _, ledger, scope_key, parent_rid = service_for(manager, upstream)
    session = open_worker(service, scope_key, parent_rid)

    with pytest.raises(BrokerUpstreamRejected) as caught:
        await chat(service, session)

    assert caught.value.retryable is (status == 429) and caught.value.status == (429 if status == 429 else 502)
    child = ledger.snapshot(session.scope_key)
    assert (child.reserved_tokens, child.consumed_tokens, child.unknown_tokens) == (0, 0, 0) and not child.blocked
    upstream.generate_error = None
    assert (await chat(service, session))["choices"]  # la session reste utilisable


@pytest.mark.parametrize(
    "failure", [http_error(500), http_error(503), http_error(408), transport_error(before_send=False), bad_body()]
)
async def test_an_ambiguous_failure_keeps_the_reserve_and_blocks(manager, failure):
    upstream = FakeUpstream()
    upstream.generate_error = failure
    service, _, ledger, scope_key, parent_rid = service_for(manager, upstream)
    session = open_worker(service, scope_key, parent_rid)

    with pytest.raises(BrokerBlocked):
        await chat(service, session)

    child = ledger.snapshot(session.scope_key)
    assert child.unknown_tokens > 0 and child.blocked
    assert ledger.snapshot(scope_key).blocked and ledger.get_reservation(parent_rid).state == "unknown"
    upstream.generate_error = None
    with pytest.raises(BrokerBlocked):
        await chat(service, session)  # aucun retry ne contourne le blocage
    assert len(upstream.generate_calls) == 1


async def test_a_connection_failure_before_sending_releases(manager):
    upstream = FakeUpstream()
    upstream.generate_error = transport_error(before_send=True)
    service, _, ledger, scope_key, parent_rid = service_for(manager, upstream)
    session = open_worker(service, scope_key, parent_rid)
    with pytest.raises(BrokerUpstreamRejected):
        await chat(service, session)
    assert ledger.snapshot(session.scope_key).reserved_tokens == 0 and not ledger.snapshot(scope_key).blocked


async def test_a_count_tokens_failure_never_generates_and_never_estimates(manager):
    upstream = FakeUpstream()
    upstream.count_error = http_error(500)
    service, _, ledger, scope_key, parent_rid = service_for(manager, upstream)
    session = open_worker(service, scope_key, parent_rid)

    with pytest.raises(BrokerUnsupported) as caught:
        await chat(service, session)

    assert caught.value.code == "count_tokens_failed" and upstream.generate_calls == []
    child = ledger.snapshot(session.scope_key)
    assert (child.reserved_tokens, child.consumed_tokens) == (0, 0) and not child.blocked


# ── budget : refus avant émission, concurrence ───────────────────────────────────────────────────────────────


async def test_the_registry_refuses_before_any_emission_when_the_allocation_cannot_hold_the_call(manager):
    upstream = FakeUpstream(count=900)
    service, _, ledger, scope_key, parent_rid = service_for(manager, upstream, worker_tokens=1000)
    session = open_worker(service, scope_key, parent_rid)

    with pytest.raises(BrokerBudgetRefused):
        await chat(service, session, chat_request(max_tokens=200))  # 900 + 200 > 1000

    assert upstream.generate_calls == []
    assert ledger.snapshot(session.scope_key).reserved_tokens == 0


async def test_concurrent_calls_never_exceed_the_allocation(manager):
    upstream = FakeUpstream(count=100, response=google_response(prompt=100, candidates=100))
    gate = asyncio.Event()
    upstream.gate = gate
    service, _, ledger, scope_key, parent_rid = service_for(manager, upstream, worker_tokens=1000)
    session = open_worker(service, scope_key, parent_rid)

    async def one():
        try:
            return await chat(service, session, chat_request(max_tokens=100))
        except BrokerBudgetRefused as exc:
            return exc

    tasks = [asyncio.create_task(one()) for _ in range(8)]  # chaque appel réserve 200 ⇒ 5 tiennent dans 1000
    for _ in range(100):
        await asyncio.sleep(0.02)
        if len(upstream.generate_calls) >= 5:
            break
    gate.set()
    results = await asyncio.gather(*tasks)

    ok = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, BrokerBudgetRefused)]
    assert len(ok) == 5 and len(refused) == 3 and len(upstream.generate_calls) == 5
    child = ledger.snapshot(session.scope_key)
    assert child.consumed_tokens == 5 * 200 and child.reserved_tokens == 0
    assert child.used_tokens <= 1000
    summary = await service.close_session(session.session_id)
    assert summary.consumed_tokens == 1000 and not summary.unknown
    assert ledger.snapshot(scope_key).consumed_tokens == 1000


# ── droits décidés côté serveur ──────────────────────────────────────────────────────────────────────────────


async def test_a_wrong_or_foreign_token_is_rejected_and_a_token_never_crosses_sessions(manager):
    service, upstream, ledger, scope_key, parent_rid = service_for(manager)
    first = open_worker(service, scope_key, parent_rid)
    second_reservation = ledger.reserve(
        scope_key, micro_usd=1, tokens=50_000, kind="worker", role="qa", model="gemma-4-31b-it", transport="worker"
    ).reservation_id
    second = open_worker(service, scope_key, second_reservation, role="qa")

    with pytest.raises(BrokerAuthError):
        await service.chat_completion(first.session_id, "cbk_faux", chat_request())
    with pytest.raises(BrokerAuthError):
        await service.chat_completion(first.session_id, second.token, chat_request())  # jeton d'une AUTRE session
    with pytest.raises(BrokerAuthError):
        await service.chat_completion("bks_inconnue", first.token, chat_request())
    assert upstream.count_calls == []
    assert "cbk_" not in repr(first) and first.token not in repr(first)


async def test_the_fallback_model_is_reserved_to_the_coder_and_everything_else_is_refused(manager):
    service, upstream, ledger, scope_key, parent_rid = service_for(manager)
    coder = open_worker(service, scope_key, parent_rid)
    other = ledger.reserve(
        scope_key,
        micro_usd=1,
        tokens=50_000,
        kind="worker",
        role="reviewer",
        model="gemma-4-31b-it",
        transport="worker",
    ).reservation_id
    reviewer = open_worker(service, scope_key, other, role="reviewer")

    assert (await chat(service, coder, chat_request(model="gemma-4-26b-a4b-it")))["model"] == "gemma-4-26b-a4b-it"
    assert (await chat(service, coder, chat_request(model="openai/gemma-4-31b-it")))["model"] == "gemma-4-31b-it"
    for bad in ("gemma-4-26b-a4b-it", "gemini-2.5-flash", "gpt-5.4", "gemma-3-27b-it", "models/../x"):
        with pytest.raises(BrokerForbidden) as caught:
            await chat(service, reviewer if bad == "gemma-4-26b-a4b-it" else coder, chat_request(model=bad))
        assert caught.value.code == "model_not_allowed"
    assert len(upstream.generate_calls) == 2


@pytest.mark.parametrize(
    "extra",
    [
        {"role": "reviewer"},
        {"scope": "project:1"},
        {"scope_key": "project:1"},
        {"base_url": "https://evil.example/v1"},
        {"api_key": "AIza-not-a-key"},
        {"endpoint": "https://evil.example"},
        {"extra_headers": {"Authorization": "Bearer x"}},
        {"extra_body": {"cachedContent": "x"}},
        {"proxy": "http://127.0.0.1:3128"},
        {"auth": "subscription"},
        {"logprobs": True},
        {"presence_penalty": 1.0},
        {"user": "someone"},
    ],
)
async def test_nothing_the_client_submits_widens_its_rights_or_is_forwarded(manager, extra):
    service, upstream, _, scope_key, parent_rid = service_for(manager)
    session = open_worker(service, scope_key, parent_rid)
    with pytest.raises(BrokerRequestRefused) as caught:
        await chat(service, session, chat_request(**extra))
    assert caught.value.code == "unsupported_field" and list(extra)[0] in str(caught.value)
    assert upstream.count_calls == []


@pytest.mark.parametrize(
    "body, code",
    [
        (
            b'{"model":"gemma-4-31b-it","messages":[{"role":"user","content":"a"}],"max_tokens":10,"max_tokens":10}',
            "duplicate_json_key",
        ),
        (b'{"model":"gemma-4-31b-it","messages":[{"role":"user","content":"a"}],"temperature":NaN}', "invalid_json"),
        (b"{not json", "invalid_json"),
        (b"[]", "invalid_json"),
        (json.dumps(chat_request(max_tokens=10, max_completion_tokens=20)).encode(), "contradictory_output_limit"),
        (json.dumps(chat_request(max_tokens=99999)).encode(), "output_limit_exceeds_ceiling"),
        (json.dumps(chat_request(max_tokens=0)).encode(), "invalid_parameter"),
        (json.dumps(chat_request(stream=True)).encode(), "unsupported"),
        (json.dumps(chat_request(n=2)).encode(), "invalid_parameter"),
        (json.dumps({"model": "gemma-4-31b-it", "messages": []}).encode(), "invalid_message"),
        (
            json.dumps(
                chat_request(
                    messages=[
                        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://x/y.png"}}]}
                    ]
                )
            ).encode(),
            "unsupported",
        ),
        (json.dumps(chat_request(messages=[{"role": "function", "content": "x"}])).encode(), "invalid_message"),
        (b"x" * (300 * 1024), "payload_too_large"),
    ],
)
async def test_malformed_ambiguous_or_oversized_requests_are_refused_before_any_provider_call(manager, body, code):
    service, upstream, ledger, scope_key, parent_rid = service_for(manager)
    session = open_worker(service, scope_key, parent_rid)
    with pytest.raises(BrokerRequestRefused) as caught:
        await chat(service, session, body)
    assert caught.value.code == code
    assert upstream.count_calls == [] and upstream.generate_calls == []
    assert ledger.snapshot(session.scope_key).reserved_tokens == 0


async def test_an_output_limit_above_the_session_ceiling_is_refused_not_clamped(manager):
    service, upstream, _, scope_key, parent_rid = service_for(manager)
    session = open_worker(service, scope_key, parent_rid, max_output_tokens=128)
    with pytest.raises(BrokerRequestRefused) as caught:
        await chat(service, session, chat_request(max_tokens=129))
    assert caught.value.code == "output_limit_exceeds_ceiling" and upstream.generate_calls == []
    await chat(service, session, chat_request(max_tokens=128))
    assert upstream.generate_calls[0]["body"]["generationConfig"]["maxOutputTokens"] == 128


async def test_the_default_output_limit_is_the_servers_when_the_client_sends_none(manager):
    service, upstream, _, scope_key, parent_rid = service_for(manager)
    session = open_worker(service, scope_key, parent_rid, max_output_tokens=256)
    body = chat_request()
    body.pop("max_tokens")
    await chat(service, session, body)
    assert upstream.generate_calls[0]["body"]["generationConfig"]["maxOutputTokens"] == 256


# ── activation : la réservation parent d'abord ───────────────────────────────────────────────────────────────


async def test_a_session_requires_an_active_parent_worker_reservation(manager):
    service, _, ledger, scope_key, parent_rid = service_for(manager)
    call_reservation = ledger.reserve(scope_key, micro_usd=0, tokens=10, kind="call", transport="x").reservation_id

    with pytest.raises(BrokerForbidden) as caught:
        open_worker(service, scope_key, "worker:inexistante")
    assert caught.value.code == "no_parent_reservation"
    with pytest.raises(BrokerForbidden):
        open_worker(service, scope_key, call_reservation)  # pas une réservation de worker
    session = open_worker(service, scope_key, parent_rid)
    with pytest.raises(BrokerForbidden) as again:
        open_worker(service, scope_key, parent_rid)
    assert again.value.code == "session_exists"
    # le scope enfant a pour plafonds EXACTEMENT les montants réservés au parent
    child = ledger.snapshot(session.scope_key)
    assert (child.cap_tokens, child.cap_micro_usd, child.kind) == (100_000, 500_000, "child")
    await service.close_session(session.session_id)


async def test_a_parent_scope_blocked_by_an_unknown_refuses_the_activation(manager):
    service, _, ledger, scope_key, parent_rid = service_for(manager)
    other = ledger.reserve(scope_key, micro_usd=0, tokens=10, kind="worker", transport="x").reservation_id
    ledger.mark_unknown(other, reason="test")
    with pytest.raises(BrokerBlocked):
        open_worker(service, scope_key, parent_rid)


# ── rejeu / idempotence ─────────────────────────────────────────────────────────────────────────────────────


async def test_the_same_request_id_returns_the_stored_result_without_a_second_generation(manager):
    service, upstream, ledger, scope_key, parent_rid = service_for(manager)
    session = open_worker(service, scope_key, parent_rid)

    first = await chat(service, session, request_id="req-1")
    second = await chat(service, session, request_id="req-1")

    assert first == second and len(upstream.generate_calls) == 1
    assert ledger.snapshot(session.scope_key).consumed_tokens == 15  # compté UNE fois
    with pytest.raises(BrokerRequestRefused) as caught:
        await chat(service, session, chat_request("autre contenu"), request_id="req-1")
    assert caught.value.code == "request_id_conflict" and caught.value.status == 409


async def test_a_released_request_id_can_be_sent_again_as_a_new_execution(manager):
    """429 (rejet démontré) puis renvoi du MÊME request_id : nouvelle exécution, une seule consommation au final."""
    upstream = FakeUpstream()
    upstream.generate_error = http_error(429)
    service, _, ledger, scope_key, parent_rid = service_for(manager, upstream)
    session = open_worker(service, scope_key, parent_rid)
    with pytest.raises(BrokerUpstreamRejected):
        await chat(service, session, request_id="req-429")
    upstream.generate_error = None

    completion = await chat(service, session, request_id="req-429")

    assert completion["choices"] and len(upstream.generate_calls) == 2
    child = ledger.snapshot(session.scope_key)
    assert (child.consumed_tokens, child.reserved_tokens, child.unknown_tokens) == (15, 0, 0)
    assert await chat(service, session, request_id="req-429") == completion  # puis rejeu pur
    assert len(upstream.generate_calls) == 2


async def test_a_replay_of_an_uncertain_emission_is_refused_not_reissued(manager):
    upstream = FakeUpstream()
    upstream.generate_error = http_error(503)
    service, _, ledger, scope_key, parent_rid = service_for(manager, upstream)
    session = open_worker(service, scope_key, parent_rid)
    with pytest.raises(BrokerBlocked):
        await chat(service, session, request_id="req-2")
    upstream.generate_error = None
    with pytest.raises(BrokerBlocked) as caught:
        await chat(service, session, request_id="req-2")
    assert caught.value.code == "replay_refused" and len(upstream.generate_calls) == 1


# ── arrêt / crash / réparation ──────────────────────────────────────────────────────────────────────────────


async def _interrupt_at(manager, state):
    """Reconstruit l'état durable laissé par un crash : tentative ``prepared`` (réservée) ou ``emitting``."""
    service, upstream, ledger, scope_key, parent_rid = service_for(manager)
    session = open_worker(service, scope_key, parent_rid)
    attempt, _ = service.store.create_attempt(
        attempt_id=f"{session.session_id}:crash",
        request_id=None,
        session_id=session.session_id,
        scope_key=session.scope_key,
        role="coder",
        model="gemma-4-31b-it",
        request_sha256="0" * 64,
        output_cap=64,
    )
    rid = f"broker:{attempt.attempt_id}"  # identifiant DÉTERMINISTE de la réservation de la tentative
    ledger.reserve(session.scope_key, micro_usd=0, tokens=500, kind="call", transport="broker", reservation_id=rid)
    service.store.attach_reservation(attempt.attempt_id, reservation_id=rid, counted_tokens=436, reserved_tokens=500)
    if state == "emitting":
        assert service.store.mark_emitting(attempt.attempt_id, datetime.now(timezone.utc))
    return service, upstream, ledger, scope_key, parent_rid, session


async def test_a_crash_before_the_emission_mark_provably_emitted_nothing_and_is_released(manager):
    service, upstream, ledger, scope_key, parent_rid, session = await _interrupt_at(manager, "prepared")

    assert service.repair(session_id=session.session_id) == 1

    assert upstream.generate_calls == [] and upstream.count_calls == []  # la réparation n'émet JAMAIS
    child = ledger.snapshot(session.scope_key)
    assert (child.reserved_tokens, child.unknown_tokens) == (0, 0) and not child.blocked
    assert service.store.get_attempt(f"{session.session_id}:crash").state == "released"


async def test_a_crash_after_the_emission_mark_is_unknown_blocks_and_is_never_replayed(manager):
    service, upstream, ledger, scope_key, parent_rid, session = await _interrupt_at(manager, "emitting")

    service.repair(session_id=session.session_id)

    assert upstream.generate_calls == []
    child = ledger.snapshot(session.scope_key)
    assert child.unknown_tokens == 500 and child.blocked
    assert ledger.snapshot(scope_key).blocked and ledger.get_reservation(parent_rid).state == "unknown"
    assert service.repair(session_id=session.session_id) == 0  # idempotent
    summary = await service.close_session(session.session_id)
    assert summary.unknown and summary.parent_settlement == "unknown"


async def test_recover_all_closes_every_interrupted_session_and_consolidates(manager):
    service, _, ledger, scope_key, parent_rid, session = await _interrupt_at(manager, "prepared")
    assert await service.recover_all() == 1
    record = service.store.get_session(session.session_id)
    assert record.state == "closed" and record.consolidated
    assert ledger.get_reservation(parent_rid).state == "committed"
    assert ledger.snapshot(scope_key).reserved_tokens == 0


async def test_the_ledger_follows_the_durable_state_after_a_crash_between_the_two_writes(manager):
    """settled écrit, commit du registre perdu : la réparation rejoue le commit (idempotent), sans double comptage."""
    service, upstream, ledger, scope_key, parent_rid, session = await _interrupt_at(manager, "emitting")
    assert service.store.settle(
        f"{session.session_id}:crash",
        now=datetime.now(timezone.utc),
        prompt=300,
        candidates=20,
        thoughts=0,
        total=320,
        response_json="{}",
    )
    service.repair(session_id=session.session_id)
    service.repair(session_id=session.session_id)
    child = ledger.snapshot(session.scope_key)
    assert (child.consumed_tokens, child.reserved_tokens, child.unknown_tokens) == (320, 0, 0)


# ── fermeture concurrente ───────────────────────────────────────────────────────────────────────────────────


async def test_closing_waits_for_the_call_in_flight_and_refuses_new_ones(manager):
    upstream = FakeUpstream()
    gate = asyncio.Event()
    upstream.gate = gate
    service, _, ledger, scope_key, parent_rid = service_for(manager, upstream)
    session = open_worker(service, scope_key, parent_rid)

    call = asyncio.create_task(chat(service, session))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if upstream.generate_calls:
            break
    closing = asyncio.create_task(service.close_session(session.session_id, "fin"))
    await asyncio.sleep(0.1)
    assert not closing.done()  # l'appel en vol retient la fermeture
    with pytest.raises(BrokerForbidden) as caught:
        await chat(service, session)
    assert caught.value.code == "session_closed"
    gate.set()
    completion = await call
    summary = await closing

    assert completion["choices"] and (summary.consumed_tokens, summary.unknown, summary.parent_settlement) == (
        15,
        False,
        "committed",
    )
    again = await service.close_session(session.session_id)  # idempotent
    assert again.consumed_tokens == 15 and ledger.snapshot(scope_key).consumed_tokens == 15


async def test_two_concurrent_closes_consolidate_once(manager):
    service, _, ledger, scope_key, parent_rid = service_for(manager)
    session = open_worker(service, scope_key, parent_rid)
    await chat(service, session)
    first, second = await asyncio.gather(
        service.close_session(session.session_id), service.close_session(session.session_id)
    )
    assert first.consumed_tokens == second.consumed_tokens == 15
    assert ledger.snapshot(scope_key).consumed_tokens == 15  # jamais 30


async def test_a_call_still_running_after_the_close_wait_is_declared_unknown(manager):
    upstream = FakeUpstream()
    upstream.gate = asyncio.Event()
    service, _, ledger, scope_key, parent_rid = service_for(
        manager, upstream, config=BrokerConfig(close_wait_seconds=0.1)
    )
    session = open_worker(service, scope_key, parent_rid)
    call = asyncio.create_task(chat(service, session))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if upstream.generate_calls:
            break

    summary = await service.close_session(session.session_id)

    assert summary.unknown and summary.parent_settlement == "unknown" and ledger.snapshot(scope_key).blocked
    upstream.gate.set()
    with pytest.raises(BrokerBlocked) as caught:
        await call  # son règlement tardif ne s'impute pas
    assert caught.value.code == "closed_during_call"
    assert ledger.snapshot(session.scope_key).consumed_tokens == 0


# ── échéances ────────────────────────────────────────────────────────────────────────────────────────────────


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


async def test_the_global_deadline_starts_at_the_first_real_opening_and_never_resets(manager):
    clock = Clock()
    service, upstream, ledger, scope_key, parent_rid = service_for(
        manager, config=BrokerConfig(global_deadline_seconds=900), clock=clock
    )
    assert service.store.clock_deadline(scope_key) is None  # rien n'est ouvert tant que Google n'a pas été touché
    session = open_worker(service, scope_key, parent_rid)
    await chat(service, session)
    opened = service.store.clock_deadline(scope_key)
    assert opened == clock.now + timedelta(seconds=900)

    clock.advance(600)
    await chat(service, session)
    assert service.store.clock_deadline(scope_key) == opened  # aucune remise à zéro

    clock.advance(301)
    with pytest.raises(BrokerForbidden) as caught:
        await chat(service, session)
    assert caught.value.code == "global_deadline" and len(upstream.generate_calls) == 2
    summary = await service.close_session(
        session.session_id
    )  # la fermeture / consolidation reste possible après l'échéance
    assert summary.consumed_tokens == 30


async def test_the_deadline_passed_during_count_tokens_releases_without_emitting(manager):
    clock = Clock()
    upstream = FakeUpstream()

    async def slow_count(request):
        upstream.count_calls.append({})
        clock.advance(1000)
        return {"totalTokens": 10}

    upstream.count_tokens = slow_count
    service, _, ledger, scope_key, parent_rid = service_for(
        manager, upstream, config=BrokerConfig(global_deadline_seconds=900), clock=clock
    )
    session = open_worker(service, scope_key, parent_rid)
    with pytest.raises(BrokerForbidden) as caught:
        await chat(service, session)
    assert caught.value.code == "global_deadline" and upstream.generate_calls == []
    assert ledger.snapshot(session.scope_key).reserved_tokens == 0


async def test_a_session_deadline_refuses_new_generations(manager):
    clock = Clock()
    service, upstream, _, scope_key, parent_rid = service_for(manager, clock=clock)
    session = open_worker(service, scope_key, parent_rid, deadline=clock.now + timedelta(seconds=30))
    await chat(service, session)
    clock.advance(31)
    with pytest.raises(BrokerForbidden) as caught:
        await chat(service, session)
    assert caught.value.code == "session_expired" and len(upstream.generate_calls) == 1


# ── producteurs hors worker : même scope global ─────────────────────────────────────────────────────────────


async def test_in_process_producers_spend_in_the_global_scope_through_the_same_pipeline(manager):
    service, upstream, ledger, scope_key, parent_rid = service_for(manager)

    completion = await service.sampling_completion(scope_key, "planner", chat_request())

    assert completion["choices"][0]["message"]["content"] == "ok"
    project = ledger.snapshot(scope_key)
    assert project.consumed_tokens == 15 and project.reserved_tokens == 100_000  # + la réservation parent du worker
    assert upstream.count_calls[0]["body"] == {"generateContentRequest": upstream.generate_calls[0]["body"]}
    with pytest.raises(BrokerForbidden):
        await service.sampling_completion(
            scope_key, "planner", chat_request(model="gemma-4-26b-a4b-it")
        )  # repli = coder seul
    with pytest.raises(BrokerForbidden):
        await service.sampling_completion(scope_key, "root", chat_request())


async def test_an_in_process_unknown_blocks_the_project_and_the_next_producer(manager):
    upstream = FakeUpstream()
    upstream.generate_error = http_error(503)
    service, _, ledger, scope_key, _ = service_for(manager, upstream)
    with pytest.raises(BrokerUpstreamAmbiguous):
        await service.sampling_completion(scope_key, "qa", chat_request())
    upstream.generate_error = None
    assert ledger.snapshot(scope_key).blocked
    with pytest.raises(BrokerBlocked):
        await service.sampling_completion(scope_key, "reviewer", chat_request())
    assert len(upstream.generate_calls) == 1 and len(upstream.count_calls) == 1  # pas même un countTokens


async def test_the_attempt_journal_never_stores_a_credential(manager):
    service, upstream, ledger, scope_key, parent_rid = service_for(manager)
    session = open_worker(service, scope_key, parent_rid)
    await chat(service, session)
    dump = json.dumps([a.__dict__ for a in service.store.attempts(scope_key=session.scope_key)], default=str)
    assert session.token not in dump
    assert BrokerAttempt.__tablename__ == "broker_attempts"


# ── vraie concurrence (threads, transactions parallèles) ────────────────────────────────────────────────────


def test_threads_with_their_own_event_loops_never_exceed_the_allocation(manager):
    """Huit threads, huit boucles, un seul registre : la réservation CAS tranche, jamais plus que l'allocation."""
    import threading

    upstream = FakeUpstream(count=100, response=google_response(prompt=100, candidates=100))
    service, _, ledger, scope_key, parent_rid = service_for(manager, upstream, worker_tokens=1000)
    session = open_worker(service, scope_key, parent_rid)
    outcomes = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            outcomes.append(asyncio.run(chat(service, session, chat_request(max_tokens=100))))
        except BrokerBudgetRefused as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    ok = [o for o in outcomes if isinstance(o, dict)]
    assert len(outcomes) == 8 and len(ok) == 5 and len(upstream.generate_calls) == 5
    child = ledger.snapshot(session.scope_key)
    assert child.consumed_tokens == 1000 and child.reserved_tokens == 0 and child.used_tokens <= 1000
    summary = asyncio.run(service.close_session(session.session_id))
    assert summary.consumed_tokens == 1000 and ledger.snapshot(scope_key).consumed_tokens == 1000


def test_threaded_closes_and_calls_keep_the_invariants(manager):
    import threading

    service, upstream, ledger, scope_key, parent_rid = service_for(manager)
    session = open_worker(service, scope_key, parent_rid)
    results = []

    def caller():
        try:
            results.append(asyncio.run(chat(service, session)))
        except Exception as exc:  # noqa: BLE001
            results.append(exc)

    threads = [threading.Thread(target=caller) for _ in range(4)] + [
        threading.Thread(target=lambda: results.append(asyncio.run(service.close_session(session.session_id))))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    summary = asyncio.run(service.close_session(session.session_id))
    settled = len(upstream.generate_calls)
    assert (
        summary.consumed_tokens == 15 * settled and not summary.unknown
    )  # chaque génération émise est comptée UNE fois
    assert (
        ledger.snapshot(scope_key).consumed_tokens == 15 * settled and ledger.snapshot(scope_key).reserved_tokens == 0
    )
