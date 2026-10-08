"""Registre budgétaire durable (vague 2) : comportement public du service transactionnel.

SQLite fichier (plusieurs connexions réelles). La même matrice tourne sur un vrai PostgreSQL dans
``test_budget_ledger_postgres.py``. Aucun appel LLM, aucun réseau.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from collegue.state import BudgetLedger, BudgetRefused, ProjectStateManager
from collegue.state.budget_ledger import (
    REFUSED_BLOCKED,
    REFUSED_CAP_TOKENS,
    REFUSED_CAP_USD,
    BudgetLedgerError,
    cap_to_micro,
    usd_to_micro,
)


@pytest.fixture
def url(tmp_path):
    return f"sqlite:///{tmp_path / 'ledger.db'}"


@pytest.fixture
def manager(url):
    return ProjectStateManager.from_url(url, create=True)


def _scope(manager, *, cap_usd=1.0, cap_tokens=None, strict=True):
    pid = manager.create_project(name="p")
    snap = manager.budget_ledger.scope_for_project(pid, max_cost_usd=cap_usd, max_tokens=cap_tokens, strict=strict)
    return pid, snap.scope_key


# --- précision monétaire -------------------------------------------------------------------------


def test_usd_is_rounded_up_never_down_and_caps_down():
    assert usd_to_micro(0.0000001) == 1  # 1e-7 $ → 1 µ$ (jamais 0)
    assert usd_to_micro(0.1) == 100_000
    assert usd_to_micro("0.0000011") == 2
    assert cap_to_micro(1.0000009) == 1_000_000  # plafond arrondi vers le BAS
    assert cap_to_micro(0) is None and cap_to_micro(None) is None and cap_to_micro(-1) is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.01, True, "abc"])
def test_invalid_money_is_rejected(bad):
    with pytest.raises(ValueError):
        usd_to_micro(bad)


# --- réserve / engage / libère -------------------------------------------------------------------


def test_reserve_commit_release_and_balance(manager):
    ledger = manager.budget_ledger
    _pid, key = _scope(manager, cap_usd=1.0, cap_tokens=10_000)

    a = ledger.reserve(key, usd=0.6, tokens=1000, role="coder")
    snap = ledger.snapshot(key)
    assert (snap.reserved_usd, snap.consumed_usd, snap.balance_usd) == (0.6, 0.0, pytest.approx(0.4))

    ledger.commit(a.reservation_id, usd=0.45, tokens=700)
    snap = ledger.snapshot(key)
    assert (snap.reserved_usd, snap.consumed_usd) == (0.0, 0.45)  # le reliquat réservé est libéré
    assert snap.balance_tokens == 10_000 - 700

    b = ledger.reserve(key, usd=0.3, tokens=100)
    ledger.release(b.reservation_id, reason="erreur 400 du fournisseur : rien facturé")
    snap = ledger.snapshot(key)
    assert (snap.reserved_usd, snap.consumed_usd) == (0.0, 0.45)


def test_reservation_over_the_cap_is_refused_before_anything_changes(manager):
    ledger = manager.budget_ledger
    _pid, key = _scope(manager, cap_usd=1.0, cap_tokens=1000)
    ledger.reserve(key, usd=0.7, tokens=100)

    with pytest.raises(BudgetRefused) as usd:
        ledger.reserve(key, usd=0.31, tokens=1)
    assert usd.value.code == REFUSED_CAP_USD
    with pytest.raises(BudgetRefused) as tokens:
        ledger.reserve(key, usd=0.01, tokens=901)
    assert tokens.value.code == REFUSED_CAP_TOKENS

    snap = ledger.snapshot(key)
    assert (snap.reserved_usd, snap.reserved_tokens) == (0.7, 100)  # refus = aucun effet de bord
    ledger.reserve(key, usd=0.3, tokens=900)  # pile au plafond : accepté


def test_actual_consumption_above_the_reservation_is_recorded_in_full(manager):
    ledger = manager.budget_ledger
    _pid, key = _scope(manager, cap_usd=1.0)
    r = ledger.reserve(key, usd=0.2, tokens=10)
    ledger.commit(r.reservation_id, usd=0.9, tokens=500)  # le fournisseur a dépassé l'estimation
    snap = ledger.snapshot(key)
    assert snap.consumed_usd == 0.9
    with pytest.raises(BudgetRefused):
        ledger.reserve(key, usd=0.2, tokens=1)


# --- idempotence ---------------------------------------------------------------------------------


def test_replaying_a_reservation_id_reserves_nothing_more(manager):
    ledger = manager.budget_ledger
    _pid, key = _scope(manager, cap_usd=1.0)
    first = ledger.reserve(key, usd=0.4, tokens=0, reservation_id="call:fixed")
    again = ledger.reserve(key, usd=0.4, tokens=0, reservation_id="call:fixed")
    assert first.replayed is False and again.replayed is True
    assert ledger.snapshot(key).reserved_usd == 0.4  # une seule fois


def test_replaying_a_commit_never_doubles_the_spend(manager):
    ledger = manager.budget_ledger
    _pid, key = _scope(manager, cap_usd=2.0)
    r = ledger.reserve(key, usd=0.5, tokens=0)
    first = ledger.commit(r.reservation_id, usd=0.4, tokens=0, event_key="evt-1")
    replay = ledger.commit(r.reservation_id, usd=0.4, tokens=0, event_key="evt-1")
    assert first.replayed is False and replay.replayed is True
    assert ledger.snapshot(key).consumed_usd == 0.4


def test_a_different_event_key_on_a_settled_reservation_is_refused(manager):
    ledger = manager.budget_ledger
    _pid, key = _scope(manager, cap_usd=2.0)
    r = ledger.reserve(key, usd=0.5, tokens=0)
    ledger.commit(r.reservation_id, usd=0.4, tokens=0, event_key="evt-1")
    with pytest.raises(BudgetLedgerError):
        ledger.commit(r.reservation_id, usd=0.4, tokens=0, event_key="evt-2")  # ne jamais compter deux fois
    with pytest.raises(BudgetLedgerError):
        ledger.release(r.reservation_id, reason="trop tard")
    assert ledger.snapshot(key).consumed_usd == 0.4


def test_an_event_key_cannot_be_reused_for_another_reservation(manager):
    ledger = manager.budget_ledger
    _pid, key = _scope(manager, cap_usd=2.0)
    a = ledger.reserve(key, usd=0.1, tokens=0)
    b = ledger.reserve(key, usd=0.1, tokens=0)
    ledger.commit(a.reservation_id, usd=0.1, tokens=0, event_key="shared")
    with pytest.raises(BudgetLedgerError):
        ledger.commit(b.reservation_id, usd=0.1, tokens=0, event_key="shared")


# --- usage inconnu --------------------------------------------------------------------------------


def test_unknown_usage_keeps_the_reservation_and_blocks_strict_with_a_durable_reason(manager, url):
    ledger = manager.budget_ledger
    _pid, key = _scope(manager, cap_usd=1.0)
    r = ledger.reserve(key, usd=0.3, tokens=50)
    ledger.mark_unknown(r.reservation_id, reason="timeout après émission")

    snap = ledger.snapshot(key)
    assert snap.unknown_usd == 0.3 and snap.reserved_usd == 0.0  # conservée, comptée dans la dépense
    assert snap.spent_usd == 0.3
    assert snap.blocked and "timeout après émission" in snap.blocked_reason
    with pytest.raises(BudgetRefused) as refused:
        ledger.reserve(key, usd=0.01, tokens=1)
    assert refused.value.code == REFUSED_BLOCKED

    # le motif est DURABLE : une autre instance, même base, le voit et refuse aussi
    other = ProjectStateManager.from_url(url, create=False).budget_ledger
    assert other.snapshot(key).blocked_reason == snap.blocked_reason
    with pytest.raises(BudgetRefused):
        other.reserve(key, usd=0.01, tokens=1)


def test_resolving_unknown_usage_with_the_real_bill_unblocks(manager):
    ledger = manager.budget_ledger
    _pid, key = _scope(manager, cap_usd=1.0)
    r = ledger.reserve(key, usd=0.3, tokens=0)
    ledger.mark_unknown(r.reservation_id, reason="inconnu")
    ledger.resolve_unknown(r.reservation_id, usd=0.12, tokens=0, event_key="bill-1")
    snap = ledger.snapshot(key)
    assert (snap.unknown_usd, snap.consumed_usd, snap.blocked_reason) == (0.0, 0.12, None)
    ledger.reserve(key, usd=0.5, tokens=0)  # débloqué


def test_two_unknowns_keep_the_block_until_both_are_resolved(manager):
    ledger = manager.budget_ledger
    _pid, key = _scope(manager, cap_usd=1.0)
    a, b = ledger.reserve(key, usd=0.1, tokens=0), ledger.reserve(key, usd=0.1, tokens=0)
    ledger.mark_unknown(a.reservation_id, reason="a")
    ledger.mark_unknown(b.reservation_id, reason="b")
    ledger.resolve_unknown(a.reservation_id, usd=0.0, tokens=0, event_key="r-a")
    assert ledger.snapshot(key).blocked
    ledger.resolve_unknown(b.reservation_id, usd=0.0, tokens=0, event_key="r-b")
    assert not ledger.snapshot(key).blocked


def test_advisory_mode_records_but_never_blocks_or_refuses(manager):
    ledger = manager.budget_ledger
    _pid, key = _scope(manager, cap_usd=0.5, strict=False)
    r = ledger.reserve(key, usd=0.4, tokens=0)
    ledger.mark_unknown(r.reservation_id, reason="inconnu")
    ledger.reserve(key, usd=9.0, tokens=0)  # au-delà du plafond : enregistré, pas refusé (aucune garantie)
    snap = ledger.snapshot(key)
    assert snap.strict is False and snap.exhausted and snap.spent_usd == pytest.approx(9.4)


def test_an_uncapped_scope_tracks_unknown_but_has_nothing_to_block(manager):
    ledger = manager.budget_ledger
    _pid, key = _scope(manager, cap_usd=None)
    r = ledger.reserve(key, usd=0.4, tokens=0)
    ledger.mark_unknown(r.reservation_id, reason="inconnu")
    assert ledger.snapshot(key).unknown_usd == 0.4 and not ledger.snapshot(key).blocked


# --- crash, reprise, redémarrage -----------------------------------------------------------------


def test_a_reservation_orphaned_by_a_crash_becomes_unknown_on_restart(url):
    """Crash avant comptabilisation : la réservation reste, puis devient inconnue à l'échéance."""
    clock = {"now": datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)}
    first = ProjectStateManager.from_url(url, create=True)
    first_ledger = BudgetLedger(first._session_factory, clock=lambda: clock["now"])
    pid = first.create_project(name="p")
    key = first_ledger.scope_for_project(pid, max_cost_usd=1.0).scope_key
    first_ledger.reserve(key, usd=0.5, tokens=0, ttl_seconds=60)  # puis le process meurt : aucun règlement

    clock["now"] += timedelta(seconds=30)
    restarted = BudgetLedger(ProjectStateManager.from_url(url)._session_factory, clock=lambda: clock["now"])
    assert restarted.snapshot(key).reserved_usd == 0.5  # avant l'échéance : toujours réservée (jamais zéro)
    assert restarted.recover_expired(key) == 0

    clock["now"] += timedelta(seconds=120)
    assert restarted.recover_expired(key) == 1
    snap = restarted.snapshot(key)
    assert (snap.reserved_usd, snap.unknown_usd) == (0.0, 0.5)
    assert snap.blocked and "non réglée à l'échéance" in snap.blocked_reason


def test_recovery_is_idempotent_and_late_commit_resolves_the_unknown(url):
    clock = {"now": datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)}
    manager = ProjectStateManager.from_url(url, create=True)
    ledger = BudgetLedger(manager._session_factory, clock=lambda: clock["now"])
    pid = manager.create_project(name="p")
    key = ledger.scope_for_project(pid, max_cost_usd=1.0).scope_key
    r = ledger.reserve(key, usd=0.5, tokens=0, ttl_seconds=10)
    clock["now"] += timedelta(seconds=60)
    assert ledger.recover_expired(key) == 1
    assert ledger.recover_expired(key) == 0  # rejouer ne change rien
    ledger.commit(r.reservation_id, usd=0.2, tokens=0)  # l'usage est enfin établi (reçu tardif)
    snap = ledger.snapshot(key)
    assert (snap.unknown_usd, snap.consumed_usd, snap.blocked) == (0.0, 0.2, False)


def test_a_new_manager_on_the_same_database_sees_the_same_ledger(url):
    one = ProjectStateManager.from_url(url, create=True)
    pid = one.create_project(name="p")
    key = one.budget_ledger.scope_for_project(pid, max_cost_usd=1.0, max_tokens=500).scope_key
    r = one.budget_ledger.reserve(key, usd=0.6, tokens=100)
    one.budget_ledger.commit(r.reservation_id, usd=0.55, tokens=90)

    two = ProjectStateManager.from_url(url)  # « redémarrage » : nouvelle instance, aucun état en mémoire
    snap = two.budget_ledger.scope_for_project(pid, max_cost_usd=1.0, max_tokens=500)
    assert (snap.consumed_usd, snap.consumed_tokens, snap.balance_usd) == (0.55, 90, pytest.approx(0.45))
    with pytest.raises(BudgetRefused):
        two.budget_ledger.reserve(key, usd=0.46, tokens=0)


def test_caps_follow_the_current_configuration_on_reopen(manager):
    pid = manager.create_project(name="p")
    ledger = manager.budget_ledger
    key = ledger.scope_for_project(pid, max_cost_usd=1.0).scope_key
    ledger.commit(ledger.reserve(key, usd=0.9, tokens=0).reservation_id, usd=0.9, tokens=0)
    with pytest.raises(BudgetRefused):
        ledger.reserve(key, usd=0.2, tokens=0)
    ledger.scope_for_project(pid, max_cost_usd=5.0)  # l'opérateur relève le plafond
    ledger.reserve(key, usd=0.2, tokens=0)


# --- import historique UNIQUE ----------------------------------------------------------------------


def test_legacy_cumulative_snapshots_are_imported_once_not_summed(manager, url):
    pid = manager.create_project(name="legacy")
    for usd, tokens in ((0.2, 100), (0.5, 400), (0.9, 900)):  # snapshots CUMULATIFS ordonnés
        manager.add_metric(pid, "run_cost_usd", usd)
        manager.add_metric(pid, "run_tokens", float(tokens))
    ledger = manager.budget_ledger

    first = ledger.scope_for_project(pid, max_cost_usd=2.0)
    assert (first.consumed_usd, first.consumed_tokens) == (0.9, 900)  # la DERNIÈRE valeur, pas 1.6
    again = ledger.scope_for_project(pid, max_cost_usd=2.0)
    reopened = ProjectStateManager.from_url(url).budget_ledger
    assert again.consumed_usd == 0.9
    assert reopened.scope_for_project(pid, max_cost_usd=2.0).consumed_usd == 0.9  # pas de double import

    manager.add_metric(pid, "run_cost_usd", 5.0)  # une métrique écrite APRÈS l'import ne ré-importe rien
    assert ledger.scope_for_project(pid, max_cost_usd=2.0).consumed_usd == 0.9


def test_a_project_without_history_imports_nothing(manager):
    pid = manager.create_project(name="fresh")
    snap = manager.budget_ledger.scope_for_project(pid, max_cost_usd=1.0)
    assert (snap.consumed_usd, snap.consumed_tokens) == (0.0, 0)
    assert manager.budget_ledger.reservations(snap.scope_key) == []


def test_non_finite_or_negative_legacy_values_are_ignored(manager):
    pid = manager.create_project(name="dirty")
    manager.add_metric(pid, "run_cost_usd", 0.3)
    manager.add_metric(pid, "run_cost_usd", float("inf"))
    manager.add_metric(pid, "run_tokens", -5.0)
    snap = manager.budget_ledger.scope_for_project(pid, max_cost_usd=1.0)
    assert (snap.consumed_usd, snap.consumed_tokens) == (0.3, 0)


# --- planification : le scope existe avant le projet --------------------------------------------------


def test_planning_scope_exists_before_the_project_and_follows_it(manager):
    ledger = manager.budget_ledger
    planning = ledger.create_planning_scope(max_cost_usd=1.0)
    r = ledger.reserve(planning.scope_key, usd=0.2, tokens=0, role="planner")  # dépense AVANT l'ID projet
    ledger.commit(r.reservation_id, usd=0.15, tokens=0)

    pid = manager.create_project(name="planned")
    bound = ledger.bind_project(planning.scope_key, pid)
    assert bound.project_id == pid
    assert ledger.bind_project(planning.scope_key, pid).project_id == pid  # idempotent

    later = ledger.scope_for_project(pid, max_cost_usd=1.0)  # BUILD retrouve le MÊME scope
    assert later.scope_key == planning.scope_key and later.consumed_usd == 0.15


def test_a_failed_planning_keeps_its_spend_and_its_error(manager):
    ledger = manager.budget_ledger
    planning = ledger.create_planning_scope(max_cost_usd=1.0)
    r = ledger.reserve(planning.scope_key, usd=0.3, tokens=0)
    ledger.commit(r.reservation_id, usd=0.25, tokens=0)
    ledger.note_failure(planning.scope_key, "ValueError: SPEC invalide")
    snap = ledger.snapshot(planning.scope_key)
    assert snap.consumed_usd == 0.25 and "SPEC invalide" in snap.last_error and snap.project_id is None


def test_binding_one_scope_twice_to_different_projects_is_refused(manager):
    ledger = manager.budget_ledger
    planning = ledger.create_planning_scope()
    a, b = manager.create_project(name="a"), manager.create_project(name="b")
    ledger.bind_project(planning.scope_key, a)
    with pytest.raises(BudgetLedgerError):
        ledger.bind_project(planning.scope_key, b)


# --- concurrence (deux connexions réelles) ------------------------------------------------------------


def _hammer(url, key, amount_usd, workers):
    """``workers`` managers (donc connexions) distincts réservent en même temps."""
    barrier = threading.Barrier(workers)
    outcomes, errors = [], []

    def worker():
        ledger = ProjectStateManager.from_url(url).budget_ledger
        barrier.wait()
        try:
            ledger.reserve(key, usd=amount_usd, tokens=0)
            outcomes.append("ok")
        except BudgetRefused as exc:
            outcomes.append(exc.code)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return outcomes, errors


def test_concurrent_reservations_on_two_connections_never_exceed_the_cap(url):
    manager = ProjectStateManager.from_url(url, create=True)
    pid = manager.create_project(name="p")
    key = manager.budget_ledger.scope_for_project(pid, max_cost_usd=1.0).scope_key

    outcomes, errors = _hammer(url, key, 0.3, workers=8)

    assert not errors, errors
    assert outcomes.count("ok") == 3  # 3 × 0,3 = 0,9 ≤ 1,0 < 1,2 : exactement trois gagnent
    assert outcomes.count(REFUSED_CAP_USD) == 5
    assert manager.budget_ledger.snapshot(key).reserved_usd == pytest.approx(0.9)


def test_concurrent_identical_reservation_ids_reserve_once(url):
    manager = ProjectStateManager.from_url(url, create=True)
    pid = manager.create_project(name="p")
    key = manager.budget_ledger.scope_for_project(pid, max_cost_usd=1.0).scope_key
    barrier = threading.Barrier(6)
    replays = []

    def worker():
        ledger = ProjectStateManager.from_url(url).budget_ledger
        barrier.wait()
        replays.append(ledger.reserve(key, usd=0.4, tokens=0, reservation_id="call:same").replayed)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(replays) == 6 and replays.count(False) == 1
    assert manager.budget_ledger.snapshot(key).reserved_usd == 0.4  # jamais 2,4


def test_concurrent_replayed_commits_count_once(url):
    manager = ProjectStateManager.from_url(url, create=True)
    pid = manager.create_project(name="p")
    ledger = manager.budget_ledger
    key = ledger.scope_for_project(pid, max_cost_usd=5.0).scope_key
    rid = ledger.reserve(key, usd=1.0, tokens=0).reservation_id
    barrier = threading.Barrier(6)
    results = []

    def worker():
        other = ProjectStateManager.from_url(url).budget_ledger
        barrier.wait()
        results.append(other.commit(rid, usd=0.7, tokens=0, event_key="evt").replayed)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(results) == 6 and results.count(False) == 1
    snap = ledger.snapshot(key)
    assert (snap.consumed_usd, snap.reserved_usd) == (0.7, 0.0)


# --- erreurs de persistance : jamais un zéro --------------------------------------------------------


def test_a_persistence_error_on_reserve_refuses_instead_of_proceeding(manager, monkeypatch):
    ledger = manager.budget_ledger
    _pid, key = _scope(manager, cap_usd=1.0)
    from sqlalchemy.exc import OperationalError

    real_factory = ledger._session_factory

    def broken():
        session = real_factory()

        def boom(*args, **kwargs):
            raise OperationalError("UPDATE", {}, Exception("disk I/O error"))

        monkeypatch.setattr(session, "execute", boom)
        return session

    monkeypatch.setattr(ledger, "_session_factory", broken)
    with pytest.raises(BudgetRefused) as refused:
        ledger.reserve(key, usd=0.1, tokens=0)
    assert refused.value.code == "ledger_unavailable"
    monkeypatch.setattr(ledger, "_session_factory", real_factory)
    assert ledger.snapshot(key).reserved_usd == 0.0  # rien n'a été réservé ni "oublié"


def test_unknown_scope_is_refused(manager):
    with pytest.raises(BudgetRefused):
        manager.budget_ledger.reserve("project:999", usd=0.1, tokens=0)
