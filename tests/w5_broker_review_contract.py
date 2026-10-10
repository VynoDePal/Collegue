"""Contrat de la revue indépendante (A26) — mêmes scénarios sur SQLite ET sur un vrai PostgreSQL.

Reprend les quatre sondes rouges du manager sur e6bb292 (admission après blocage parent, allocation nulle, reprise des producteurs en
processus, réserve orpheline entre réservation et attachement), puis les cas voisins : course d'admission SOUS VERROU de ligne,
contrat de propriété (un propriétaire vivant n'est jamais réparé), réserve sans état durable, erreurs réelles du registre,
fermeture pendant countTokens, qualification des deux modèles, transfert de l'échéance planning → projet.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update
from w5_broker_contract import Clock, chat, open_worker, service_for
from w5_broker_support import CanaryUpstream, FakeUpstream, chat_request, google_response, http_error

from collegue.broker import (
    BrokerBlocked,
    BrokerConfig,
    BrokerForbidden,
    BrokerService,
    BrokerUpstreamAmbiguous,
)
from collegue.state import BudgetLedgerError
from collegue.state.models import BrokerOwner, BudgetScope


async def _wait_until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


class PausedCount(FakeUpstream):
    """countTokens répond puis ATTEND : on peut agir sur l'état pendant que l'appel est entre comptage et émission."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def count_tokens(self, request):
        counted = await super().count_tokens(request)
        self.started.set()
        await self.release.wait()
        return counted


# ── 1. admission à l'émission ────────────────────────────────────────────────────────────────────────────────


async def test_a_project_blocked_during_counting_prevents_the_emission(manager):
    upstream = PausedCount()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    task = asyncio.create_task(chat(service, worker, request_id="race"))
    await upstream.started.wait()
    other = ledger.reserve(scope, tokens=1, kind="call")
    ledger.mark_unknown(other.reservation_id, reason="un autre rôle a perdu sa réponse")
    assert ledger.snapshot(scope).blocked
    upstream.release.set()

    with pytest.raises(BrokerBlocked):
        await task

    assert upstream.generate_calls == []  # JAMAIS émise
    child = ledger.snapshot(worker.scope_key)
    assert child.reserved_tokens == 0 and not child.blocked  # libérée : l'absence d'émission est établie
    assert service.store.find_attempt(worker.scope_key, "race").state == "released"


async def test_a_parent_reservation_settled_during_counting_prevents_the_emission(manager):
    upstream = PausedCount()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    task = asyncio.create_task(chat(service, worker))
    await upstream.started.wait()
    ledger.mark_unknown(rid, reason="réservation parent réglée par ailleurs")
    upstream.release.set()
    with pytest.raises(BrokerBlocked):
        await task
    assert upstream.generate_calls == []


async def test_a_session_closed_during_counting_prevents_the_emission_and_the_close_does_not_wait_for_it(manager):
    upstream = PausedCount()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    task = asyncio.create_task(chat(service, worker))
    await upstream.started.wait()
    closing = asyncio.create_task(service.close_session(worker.session_id, "fin"))
    await _wait_until(lambda: service.store.get_session(worker.session_id).state == "closing")
    upstream.release.set()

    with pytest.raises(BrokerForbidden) as caught:
        await task
    summary = await closing

    assert caught.value.code == "session_closed" and upstream.generate_calls == []
    assert (summary.consumed_tokens, summary.unknown, summary.parent_settlement) == (0, False, "committed")
    assert ledger.snapshot(worker.scope_key).reserved_tokens == 0 and ledger.snapshot(scope).reserved_tokens == 0


def _prepared_attempt(service, ledger, worker, scope_key=None):
    attempt, _ = service.store.create_attempt(
        attempt_id=f"{worker.session_id}:locked",
        request_id=None,
        session_id=worker.session_id,
        scope_key=worker.scope_key,
        role="coder",
        model="gemma-4-31b-it",
        request_sha256="0" * 64,
        output_cap=64,
        owner_id=service.owner_id,
    )
    rid = f"broker:{attempt.attempt_id}"
    service.store.attach_reservation(attempt.attempt_id, reservation_id=rid, counted_tokens=10, reserved_tokens=74)
    ledger.reserve(worker.scope_key, micro_usd=0, tokens=74, kind="call", transport="broker", reservation_id=rid)
    return attempt


def _admit(service, worker, scope, parent_rid, attempt):
    return service.store.admit_emission(
        attempt.attempt_id,
        datetime.now(timezone.utc),
        session_id=worker.session_id,
        scope_keys=(worker.scope_key, scope),
        parent_reservation_id=parent_rid,
    )


async def test_the_admission_waits_for_a_block_being_committed_by_another_transaction_then_refuses(manager):
    """Course ENTRE connexions : le blocage est écrit (non validé) par une autre transaction pendant l'admission."""
    service, _, ledger, scope, rid = service_for(manager)
    worker = open_worker(service, scope, rid)
    attempt = _prepared_attempt(service, ledger, worker)

    other = ledger._session_factory()
    other.execute(
        update(BudgetScope).where(BudgetScope.scope_key == scope).values(blocked_reason="écrit par un autre processus")
    )
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.update(result=_admit(service, worker, scope, rid, attempt)))
    thread.start()
    await asyncio.sleep(0.5)
    other.commit()  # le blocage devient visible
    other.close()
    thread.join(timeout=60)

    admitted, why = outcome["result"]
    assert admitted is False and "bloqué" in why  # jamais « emitting » après un blocage validé avant l'admission
    assert service.store.get_attempt(attempt.attempt_id).state == "prepared"


async def test_a_block_committed_after_the_admission_leaves_a_legitimate_in_flight_emission(manager):
    service, _, ledger, scope, rid = service_for(manager)
    worker = open_worker(service, scope, rid)
    attempt = _prepared_attempt(service, ledger, worker)

    assert _admit(service, worker, scope, rid, attempt) == (True, "")
    other = ledger.reserve(scope, tokens=1, kind="call")
    ledger.mark_unknown(other.reservation_id, reason="après l'admission")

    assert (
        service.store.get_attempt(attempt.attempt_id).state == "emitting"
    )  # déjà en vol : son sort sera réglé, pas annulé


async def test_concurrent_admissions_and_blocks_are_serialized_and_a_blocked_project_admits_nothing_more(manager):
    """Huit tours de vraie concurrence (threads, transactions parallèles) : admission et blocage simultanés, projet neuf à chaque tour."""
    outcomes = []
    for round_number in range(8):
        service, _, ledger, scope, rid = service_for(manager, worker_tokens=100_000)
        worker = open_worker(service, scope, rid)
        attempt = _prepared_attempt(service, ledger, worker)
        blocker = ledger.reserve(scope, tokens=1, kind="call")
        results = {}
        barrier = threading.Barrier(2)

        def admit():
            barrier.wait()
            results["admit"] = _admit(service, worker, scope, rid, attempt)

        def block():
            barrier.wait()
            ledger.mark_unknown(blocker.reservation_id, reason=f"tour {round_number}")

        threads = [threading.Thread(target=admit), threading.Thread(target=block)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        admitted, _ = results["admit"]
        outcomes.append(admitted)
        assert ledger.snapshot(scope).blocked  # le blocage est toujours établi à la fin
        assert service.store.get_attempt(attempt.attempt_id).state == ("emitting" if admitted else "prepared")
        # Une fois le blocage validé, aucune NOUVELLE tentative n'est admise : ni retard ni exception pour le contourner.
        later, _ = service.store.create_attempt(
            attempt_id=f"{worker.session_id}:later",
            request_id=None,
            session_id=worker.session_id,
            scope_key=worker.scope_key,
            role="coder",
            model="gemma-4-31b-it",
            request_sha256="0" * 64,
            output_cap=64,
            owner_id=service.owner_id,
        )
        service.store.attach_reservation(
            later.attempt_id, reservation_id=f"broker:{later.attempt_id}", counted_tokens=1, reserved_tokens=2
        )
        ledger._run(lambda session: None)
        assert _admit(service, worker, scope, rid, later)[0] is False
    assert (
        len(outcomes) == 8
    )  # (selon l'ordonnancement : admise-puis-bloquée ou refusée — les deux issues sont cohérentes)


# ── 2. allocation nulle ──────────────────────────────────────────────────────────────────────────────────────


async def test_a_zero_token_allocation_authorizes_no_generation(manager):
    service, upstream, ledger, scope, rid = service_for(manager, worker_tokens=0)
    with pytest.raises(BrokerForbidden) as caught:
        open_worker(service, scope, rid)
    assert caught.value.code == "zero_allocation" and upstream.count_calls == [] and upstream.generate_calls == []
    assert service.store.open_sessions() == []  # aucune session ni scope enfant « illimité »


async def test_the_usd_dimension_is_independent_of_the_token_allocation(manager):
    service, upstream, ledger, scope, rid = service_for(manager, worker_micro=0, worker_tokens=1000)
    worker = open_worker(service, scope, rid)
    child = ledger.snapshot(worker.scope_key)
    assert (child.cap_micro_usd, child.cap_tokens) == (0, 1000)  # 0 reste 0 : jamais « sans plafond »
    assert (await chat(service, worker))[
        "choices"
    ]  # Gemma coûte 0 $ : une réserve USD nulle n'empêche pas une génération gratuite
    assert ledger.snapshot(worker.scope_key).consumed_tokens == 15


# ── 3. reprise de TOUS les producteurs, contrat de propriété ─────────────────────────────────────────────────


def _crashed_in_process_emission(service, ledger, scope, *, owner_id=None, name="in-process"):
    attempt, _ = service.store.create_attempt(
        attempt_id=f"crash-{name}",
        request_id=f"crash-{name}",
        session_id=None,
        scope_key=scope,
        role="planner",
        model="gemma-4-31b-it",
        request_sha256="0" * 64,
        output_cap=64,
        owner_id=owner_id,
    )
    rid = f"broker:{attempt.attempt_id}"
    ledger.reserve(scope, tokens=74, kind="call", reservation_id=rid)
    service.store.attach_reservation(attempt.attempt_id, reservation_id=rid, counted_tokens=10, reserved_tokens=74)
    service.store.mark_emitting(attempt.attempt_id, datetime.now(timezone.utc))
    return attempt, rid


async def test_recover_all_repairs_the_in_process_producers_and_blocks_before_any_new_call(manager):
    service, upstream, ledger, scope, _ = service_for(manager)
    attempt, rid = _crashed_in_process_emission(service, ledger, scope)

    await service.recover_all()

    assert service.store.get_attempt(attempt.attempt_id).state == "unknown"
    assert (
        ledger.snapshot(scope).blocked and ledger.get_reservation(rid).state == "unknown"
    )  # conservée, jamais libérée
    with pytest.raises(BrokerBlocked):
        await service.sampling_completion(scope, "planner", chat_request())
    assert upstream.count_calls == [] and upstream.generate_calls == []


def _owner_row(ledger, owner_id, *, host, pid, ticks, heartbeat):
    def add(session):
        session.add(
            BrokerOwner(
                owner_id=owner_id, host=host, pid=pid, start_ticks=ticks, created_at=heartbeat, heartbeat_at=heartbeat
            )
        )

    ledger._run(add)


async def test_a_live_owner_in_another_process_is_never_repaired(manager):
    """Un autre service VIVANT (ici : même hôte, processus existant, même date de démarrage) garde ses tentatives en vol."""
    first, upstream, ledger, scope, _ = service_for(manager)
    second = BrokerService(ledger, FakeUpstream(), config=BrokerConfig())
    second._ensure_owner()
    attempt, rid = _crashed_in_process_emission(second, ledger, scope, owner_id=second.owner_id, name="live")

    await first.recover_all()

    assert first.store.get_attempt(attempt.attempt_id).state == "emitting"  # intacte
    assert not ledger.snapshot(scope).blocked and ledger.get_reservation(rid).state == "reserved"
    # propriétaire proprement terminé : ses restes deviennent réparables
    second.shutdown()
    await first.recover_all()
    assert first.store.get_attempt(attempt.attempt_id).state == "unknown" and ledger.snapshot(scope).blocked


async def test_a_vanished_owner_is_repaired_and_a_recent_remote_owner_is_not(manager):
    service, upstream, ledger, scope, _ = service_for(manager)
    now = datetime.now(timezone.utc)
    _owner_row(ledger, "own_dead", host=socket.gethostname(), pid=2**22 + 12345, ticks=1, heartbeat=now)
    _owner_row(ledger, "own_remote_fresh", host="autre-hote", pid=1, ticks=None, heartbeat=now)
    _owner_row(ledger, "own_remote_stale", host="autre-hote", pid=1, ticks=None, heartbeat=now - timedelta(hours=3))
    dead, _ = _crashed_in_process_emission(service, ledger, scope, owner_id="own_dead", name="dead")
    fresh, _ = _crashed_in_process_emission(service, ledger, scope, owner_id="own_remote_fresh", name="fresh")
    stale, _ = _crashed_in_process_emission(service, ledger, scope, owner_id="own_remote_stale", name="stale")

    await service.recover_all()

    states = {
        n: service.store.get_attempt(a.attempt_id).state
        for n, a in (("dead", dead), ("fresh", fresh), ("stale", stale))
    }
    assert states == {"dead": "unknown", "fresh": "emitting", "stale": "unknown"}


async def test_recover_all_refuses_to_claim_its_own_work_while_calls_are_in_flight(manager):
    upstream = FakeUpstream()
    upstream.gate = asyncio.Event()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    task = asyncio.create_task(chat(service, worker))
    assert await _wait_until(lambda: bool(upstream.generate_calls))

    with pytest.raises(BrokerForbidden) as caught:
        await service.recover_all()
    assert caught.value.code == "recovery_while_busy"
    assert await service.recover_all(include_own=False) == 0  # sans revendiquer ses propres tentatives : rien à toucher
    upstream.gate.set()
    assert (await task)["choices"]


async def test_a_dead_owners_emission_blocks_the_next_call_before_any_provider_traffic(manager):
    service, upstream, ledger, scope, _ = service_for(manager)
    _crashed_in_process_emission(service, ledger, scope, owner_id=None, name="orphan-owner")
    with pytest.raises(BrokerBlocked):
        await service.sampling_completion(scope, "qa", chat_request())
    assert upstream.count_calls == [] and upstream.generate_calls == []


# ── 4. fenêtre réservation / attachement, réserves orphelines ────────────────────────────────────────────────


async def test_a_crash_between_the_reservation_and_its_attachment_is_reconciled(manager):
    service, upstream, ledger, scope, rid = service_for(manager)
    worker = open_worker(service, scope, rid)
    attempt, _ = service.store.create_attempt(
        attempt_id="reserve-attach-crash",
        request_id=None,
        session_id=worker.session_id,
        scope_key=worker.scope_key,
        role="coder",
        model="gemma-4-31b-it",
        request_sha256="0" * 64,
        output_cap=64,
    )
    child_rid = f"broker:{attempt.attempt_id}"
    ledger.reserve(worker.scope_key, tokens=74, kind="call", reservation_id=child_rid)

    await service.recover_all()

    assert ledger.snapshot(worker.scope_key).reserved_tokens == 0
    assert ledger.get_reservation(child_rid).state == "released"  # jamais émise : libérable
    assert service.store.get_attempt(attempt.attempt_id).state == "released"
    assert upstream.generate_calls == []


async def test_the_reservation_id_is_written_in_the_attempt_before_the_ledger_reserves(manager):
    class Spy(FakeUpstream):
        pass

    service, upstream, ledger, scope, rid = service_for(manager, Spy())
    worker = open_worker(service, scope, rid)
    seen = []
    original = ledger.reserve

    def spying(scope_key, **kw):
        attempt = service.store.attempts(scope_key=scope_key)[-1]
        seen.append(
            (kw.get("reservation_id"), attempt.reservation_id)
        )  # ce que la tentative DÉSIGNE au moment de réserver
        return original(scope_key, **kw)

    ledger.reserve = spying
    try:
        await chat(service, worker)
    finally:
        ledger.reserve = original
    assert seen and all(rid_asked == rid_attached for rid_asked, rid_attached in seen)


async def test_a_reservation_id_is_deterministic_and_distinct_for_each_run_of_a_reopened_attempt(manager):
    upstream = FakeUpstream()
    upstream.generate_error = http_error(429)
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    with pytest.raises(Exception):
        await chat(service, worker, request_id="again")
    upstream.generate_error = None
    await chat(service, worker, request_id="again")
    attempt = service.store.find_attempt(worker.scope_key, "again")
    assert attempt.runs == 1 and attempt.reservation_id == f"broker:{attempt.attempt_id}#1"
    assert ledger.get_reservation(f"broker:{attempt.attempt_id}").state == "released"  # la 1re exécution, libérée
    assert ledger.get_reservation(attempt.reservation_id).state == "committed"


async def test_a_reservation_without_any_durable_state_is_unknown_never_released(manager):
    service, upstream, ledger, scope, rid = service_for(manager)
    worker = open_worker(service, scope, rid)
    ghost = ledger.reserve(worker.scope_key, tokens=100, kind="call", reservation_id="broker:ghost:no-attempt-row")

    summary = await service.close_session(worker.session_id)

    assert ledger.get_reservation(ghost.reservation_id).state == "unknown"  # on ne sait pas si elle a servi : conservée
    assert summary.unknown and summary.parent_settlement == "unknown" and ledger.snapshot(scope).blocked
    assert ledger.snapshot(worker.scope_key).reserved_tokens == 0


async def test_a_closed_session_leaves_no_unconsolidated_reservation_whatever_the_crash_window(manager):
    service, upstream, ledger, scope, rid = service_for(manager)
    worker = open_worker(service, scope, rid)
    # fenêtre A : réservée, jamais attachée ; fenêtre B : attachée, jamais réservée ; fenêtre C : émission marquée
    for name, reserve, attach in (("a", True, False), ("b", False, True)):
        attempt, _ = service.store.create_attempt(
            attempt_id=f"window-{name}",
            request_id=None,
            session_id=worker.session_id,
            scope_key=worker.scope_key,
            role="coder",
            model="gemma-4-31b-it",
            request_sha256="0" * 64,
            output_cap=64,
        )
        arid = f"broker:{attempt.attempt_id}"
        if attach:
            service.store.attach_reservation(
                attempt.attempt_id, reservation_id=arid, counted_tokens=1, reserved_tokens=74
            )
        if reserve:
            ledger.reserve(worker.scope_key, tokens=74, kind="call", reservation_id=arid)
    emitting, rid_c = _crashed_in_process_emission(service, ledger, worker.scope_key, name="c")

    summary = await service.close_session(worker.session_id)

    child = ledger.snapshot(worker.scope_key)
    assert child.reserved_tokens == 0 and child.unknown_tokens == 74  # A, B libérées ; C conservée (inconnue)
    assert ledger.get_reservation(rid_c).state == "unknown" and summary.unknown
    assert service.store.get_session(worker.session_id).state == "closed"


# ── 5. erreurs réelles du registre ───────────────────────────────────────────────────────────────────────────


async def test_a_real_ledger_error_while_blocking_the_parent_is_not_masked(manager):
    upstream = FakeUpstream()
    upstream.generate_error = http_error(503)
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    original = ledger.mark_unknown

    def failing(reservation_id, **kw):
        if reservation_id == rid:
            raise BudgetLedgerError("disque plein")
        return original(reservation_id, **kw)

    ledger.mark_unknown = failing
    try:
        with pytest.raises(BudgetLedgerError):
            await chat(
                service, worker
            )  # PAS un « parent forcément inconnu » : l'erreur se propage, la poursuite est interdite
    finally:
        ledger.mark_unknown = original
    assert service.store.find_attempt(worker.scope_key, "x") is None
    assert [a.state for a in service.store.attempts(scope_key=worker.scope_key)] == [
        "unknown"
    ]  # l'inconnue enfant reste établie


async def test_a_child_unknown_after_the_parent_was_settled_blocks_the_parent_scope(manager):
    service, upstream, ledger, scope, rid = service_for(manager)
    worker = open_worker(service, scope, rid)
    record = service.store.get_session(worker.session_id)
    await chat(service, worker)
    await service.close_session(worker.session_id)
    assert ledger.get_reservation(rid).state == "committed"

    service._block_parent(record, "inconnue tardive")

    assert ledger.snapshot(scope).blocked  # la réserve est réglée : le scope parent porte le blocage


async def test_the_result_of_attach_reservation_is_checked(manager):
    """Si la tentative a changé d'état entre countTokens et la réservation, RIEN n'est réservé ni émis."""

    class SteppedCount(FakeUpstream):
        async def count_tokens(self, request):
            counted = await super().count_tokens(request)
            for attempt in service.store.attempts(scope_key=worker.scope_key):
                service.store.release(
                    attempt.attempt_id, now=datetime.now(timezone.utc), code="repris", detail="autre exécution"
                )
            return counted

    upstream = SteppedCount()
    service, _, ledger, scope, rid = service_for(manager, upstream)
    worker = open_worker(service, scope, rid)
    with pytest.raises(BrokerBlocked) as caught:
        await chat(service, worker)
    assert caught.value.code == "attempt_taken" and upstream.generate_calls == []
    assert ledger.snapshot(worker.scope_key).reserved_tokens == 0


# ── 6. qualification des DEUX modèles ────────────────────────────────────────────────────────────────────────


async def test_the_qualification_exercises_both_models_and_the_three_capabilities_through_the_real_pipeline(manager):
    clock = Clock()
    upstream = CanaryUpstream()
    ledger = manager.budget_ledger
    scope = ledger.create_planning_scope(
        max_cost_usd=2.0, max_tokens=250_000, scope_key="planning:cycle:camp-1"
    ).scope_key
    service = BrokerService(ledger, upstream, config=BrokerConfig(global_deadline_seconds=900), clock=clock)

    report = await service.qualify_models(scope)

    assert report.ok and report.reason == "" and not report.blocked
    assert [(m.model, m.role, m.ok) for m in report.models] == [
        ("gemma-4-31b-it", "default", True),
        ("gemma-4-26b-a4b-it", "coder", True),
    ]
    assert [c.capability for c in report.models[0].capabilities] == ["text", "json", "tools"]
    assert [(c["url_model"]) for c in upstream.generate_calls] == ["gemma-4-31b-it"] * 3 + ["gemma-4-26b-a4b-it"] * 3
    assert (
        len(upstream.count_calls) == 6
        and upstream.count_calls[0]["body"]["generateContentRequest"] == upstream.generate_calls[0]["body"]
    )
    assert (
        report.consumed_tokens == 6 * 15 == ledger.snapshot(scope).consumed_tokens
    )  # usage et réservations dans le scope
    assert (
        report.deadline_at == clock.now + timedelta(seconds=900) == service.persisted_deadline(scope)
    )  # horloge ouverte ICI
    assert report.destination.startswith("generativelanguage.googleapis.com")
    # chaque canari a une identité durable : relancer la qualification ne réémet RIEN
    again = await service.qualify_models(scope)
    assert again.ok and len(upstream.generate_calls) == 6 and ledger.snapshot(scope).consumed_tokens == 90
    assert report.to_dict()["models"][1]["capabilities"][2]["request_id"] == f"qualify:{scope}:gemma-4-26b-a4b-it:tools"


@pytest.mark.parametrize(
    "override, fragment",
    [
        ({("gemma-4-26b-a4b-it", "json"): google_response(text="pas du json")}, "gemma-4-26b-a4b-it/json"),
        ({("gemma-4-31b-it", "tools"): google_response(text="je ne sais pas appeler")}, "gemma-4-31b-it/tools"),
        ({("gemma-4-31b-it", "text"): google_response(text="")}, "gemma-4-31b-it/text"),
    ],
)
async def test_the_first_incompatibility_stops_the_qualification_without_estimate_or_retry(manager, override, fragment):
    upstream = CanaryUpstream(override=override)
    ledger = manager.budget_ledger
    scope = ledger.create_planning_scope(max_cost_usd=2.0, max_tokens=250_000).scope_key
    service = BrokerService(ledger, upstream, config=BrokerConfig(global_deadline_seconds=900))

    report = await service.qualify_models(scope)

    assert not report.ok and fragment in report.reason
    skipped = [c for m in report.models for c in m.capabilities if "non exécuté" in c.detail]
    assert skipped or fragment.endswith("tools") and report.models[1].capabilities[0].detail.startswith("non exécuté")
    before = len(upstream.generate_calls)
    assert before < 6
    # relancer ne réémet pas les canaris déjà réglés, mais ne « répare » rien non plus : la défaillance est durable
    again = await service.qualify_models(scope)
    assert not again.ok


async def test_an_ambiguous_canary_blocks_the_scope_and_ends_the_qualification(manager):
    upstream = CanaryUpstream(override={("gemma-4-31b-it", "json"): http_error(503)})
    ledger = manager.budget_ledger
    scope = ledger.create_planning_scope(max_cost_usd=2.0, max_tokens=250_000).scope_key
    service = BrokerService(ledger, upstream, config=BrokerConfig(global_deadline_seconds=900))

    report = await service.qualify_models(scope)

    assert not report.ok and report.blocked and "upstream_ambiguous" in report.reason
    assert len(upstream.generate_calls) == 2  # texte puis JSON ; rien après
    assert ledger.snapshot(scope).blocked


async def test_a_refused_count_tokens_makes_the_qualification_incomplete_with_no_estimate(manager):
    upstream = CanaryUpstream()
    upstream.count_error = http_error(404)
    ledger = manager.budget_ledger
    scope = ledger.create_planning_scope(max_cost_usd=2.0, max_tokens=250_000).scope_key
    service = BrokerService(ledger, upstream, config=BrokerConfig(global_deadline_seconds=900))
    report = await service.qualify_models(scope)
    assert not report.ok and "count_tokens_failed" in report.reason and upstream.generate_calls == []
    assert ledger.snapshot(scope).consumed_tokens == 0


# ── 7. échéance : du planning au projet ──────────────────────────────────────────────────────────────────────


async def test_the_deadline_opened_by_the_qualification_survives_the_binding_to_the_project(manager):
    clock = Clock()
    ledger = manager.budget_ledger
    scope = ledger.create_planning_scope(
        max_cost_usd=2.0, max_tokens=250_000, scope_key="planning:cycle:camp-2"
    ).scope_key
    service = BrokerService(ledger, CanaryUpstream(), config=BrokerConfig(global_deadline_seconds=900), clock=clock)
    await service.qualify_models(scope)
    deadline = service.persisted_deadline(scope)
    assert deadline == clock.now + timedelta(seconds=900)

    clock.advance(400)
    project_id = manager.create_project(name="liaison")
    ledger.bind_project(scope, project_id)
    bound = ledger.scope_for_project(project_id, max_cost_usd=2.0, max_tokens=250_000)

    assert bound.scope_key == scope  # même ligne de scope : la liaison conserve le scope ET l'horloge
    assert service.persisted_deadline(bound.scope_key) == deadline  # pas de nouvelle fenêtre
    assert service.remaining_seconds(bound.scope_key) == pytest.approx(500, abs=1)
    assert service.open_clock(bound.scope_key) == deadline  # ouvrir à nouveau ne déplace jamais l'échéance
    clock.advance(600)
    assert service.remaining_seconds(bound.scope_key) < 0
    with pytest.raises(BrokerForbidden) as caught:
        await service.sampling_completion(bound.scope_key, "planner", chat_request())
    assert caught.value.code == "global_deadline"
