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
    BudgetIdentityError,
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
    assert cap_to_micro(0) is None and cap_to_micro(None) is None  # 0/None = pas de plafond
    with pytest.raises(ValueError):
        cap_to_micro(-1)  # négatif : refusé, jamais un plafond désactivé


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


# --- valeurs invalides : jamais un plafond désactivé ni un montant masqué ------------------------


_BAD_AMOUNTS = [float("nan"), float("inf"), float("-inf"), "oops", True, -1, [1]]


@pytest.mark.parametrize("bad", _BAD_AMOUNTS)
def test_an_invalid_usd_cap_is_rejected_not_read_as_no_cap(manager, bad):
    pid = manager.create_project(name="p")
    with pytest.raises(ValueError):
        manager.budget_ledger.scope_for_project(pid, max_cost_usd=bad)
    assert manager.budget_ledger.snapshot_for_project(pid) is None  # aucun scope sans plafond n'a été créé


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "oops", True, -5, 1.5])
def test_an_invalid_token_cap_is_rejected(manager, bad):
    pid = manager.create_project(name="p")
    with pytest.raises(ValueError):
        manager.budget_ledger.scope_for_project(pid, max_tokens=bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "oops", True, -1, 1.5])
def test_invalid_reservation_amounts_are_rejected_before_any_write(manager, bad):
    ledger = manager.budget_ledger
    _pid, key = _scope(manager, cap_usd=1.0)
    with pytest.raises(ValueError):
        ledger.reserve(key, micro_usd=bad, tokens=0)
    with pytest.raises(ValueError):
        ledger.reserve(key, usd=0.0, tokens=bad)
    snap = ledger.snapshot(key)
    assert (snap.reserved_usd, snap.reserved_tokens) == (0.0, 0)


def test_invalid_settlement_amounts_are_rejected_and_keep_the_reservation(manager):
    ledger = manager.budget_ledger
    _pid, key = _scope(manager, cap_usd=1.0)
    r = ledger.reserve(key, usd=0.5, tokens=10)
    for bad in (float("nan"), float("inf"), "oops", True, -0.1):
        with pytest.raises(ValueError):
            ledger.commit(r.reservation_id, usd=bad, tokens=0)
    with pytest.raises(ValueError):
        ledger.commit(r.reservation_id, usd=0.1, tokens=-3)
    snap = ledger.snapshot(key)
    assert (snap.reserved_usd, snap.consumed_usd) == (0.5, 0.0)  # rien n'a été réglé


# --- identité : un rejeu est le MÊME événement, jamais un autre sous la même clé ----------------


def test_a_reservation_id_cannot_be_reused_for_another_scope_or_amount(manager):
    ledger = manager.budget_ledger
    _pid, key = _scope(manager, cap_usd=1.0)
    _pid2, other = _scope(manager, cap_usd=1.0)
    ledger.reserve(key, usd=0.4, tokens=10, reservation_id="call:x", model="m", transport="t", role="r")
    for kwargs in (
        {"usd": 0.9, "tokens": 10},  # autre montant USD
        {"usd": 0.4, "tokens": 11},  # autres tokens
        {"usd": 0.4, "tokens": 10, "kind": "worker"},  # autre type
        {"usd": 0.4, "tokens": 10, "model": "autre"},  # autre modèle
    ):
        args = {"model": "m", "transport": "t", "role": "r", **kwargs}
        with pytest.raises(BudgetLedgerError):
            ledger.reserve(key, reservation_id="call:x", **args)
    with pytest.raises(BudgetLedgerError):  # autre scope
        ledger.reserve(other, usd=0.4, tokens=10, reservation_id="call:x", model="m", transport="t", role="r")
    assert ledger.snapshot(key).reserved_usd == 0.4 and ledger.snapshot(other).reserved_usd == 0.0
    assert ledger.reserve(key, usd=0.4, tokens=10, reservation_id="call:x", model="m", transport="t", role="r").replayed


def test_a_commit_key_cannot_be_replayed_with_another_amount_or_as_a_release(manager):
    ledger = manager.budget_ledger
    _pid, key = _scope(manager, cap_usd=2.0)
    r = ledger.reserve(key, usd=0.5, tokens=10)
    ledger.commit(r.reservation_id, usd=0.4, tokens=5, event_key="evt")
    with pytest.raises(BudgetLedgerError):
        ledger.commit(r.reservation_id, usd=0.1, tokens=5, event_key="evt")  # autre montant, même clé
    with pytest.raises(BudgetLedgerError):
        ledger.commit(r.reservation_id, usd=0.4, tokens=6, event_key="evt")  # autres tokens, même clé
    with pytest.raises(BudgetLedgerError):
        ledger.release(r.reservation_id, reason="x", event_key="evt")  # la clé d'un commit ne libère pas
    with pytest.raises(BudgetLedgerError):
        ledger.mark_unknown(r.reservation_id, reason="x", event_key="evt")
    snap = ledger.snapshot(key)
    assert (snap.consumed_usd, snap.consumed_tokens, snap.unknown_usd) == (0.4, 5, 0.0)
    assert ledger.commit(r.reservation_id, usd=0.4, tokens=5, event_key="evt").replayed


def _race(url, key, calls):
    """Lance ``calls`` (une par connexion indépendante) derrière une barrière ; renvoie les issues."""
    barrier = threading.Barrier(len(calls))
    outcomes = []

    def worker(call):
        ledger = ProjectStateManager.from_url(url).budget_ledger
        barrier.wait()
        try:
            outcomes.append(("ok", call(ledger)))
        except BudgetLedgerError as exc:
            outcomes.append(("contradiction", str(exc)))
        except BaseException as exc:  # noqa: BLE001 - toute autre issue est un échec du test
            outcomes.append(("autre", repr(exc)))

    threads = [threading.Thread(target=worker, args=(call,)) for call in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return outcomes


def test_concurrent_reservations_reusing_an_id_with_other_amounts_reserve_once_and_refuse_the_rest(url):
    manager = ProjectStateManager.from_url(url, create=True)
    pid = manager.create_project(name="p")
    key = manager.budget_ledger.scope_for_project(pid, max_cost_usd=10.0).scope_key
    amounts = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
    calls = [lambda ledger, a=a: ledger.reserve(key, usd=a, tokens=0, reservation_id="call:same") for a in amounts]

    outcomes = _race(url, key, calls)

    assert [kind for kind, _ in outcomes].count("ok") == 1, outcomes
    assert [kind for kind, _ in outcomes].count("contradiction") == len(amounts) - 1, outcomes
    reserved = manager.budget_ledger.snapshot(key).reserved_usd
    assert reserved in amounts  # exactement UNE des demandes a gagné, sans somme ni mélange


def test_concurrent_commits_reusing_a_key_with_other_amounts_count_once_and_refuse_the_rest(url):
    manager = ProjectStateManager.from_url(url, create=True)
    pid = manager.create_project(name="p")
    ledger = manager.budget_ledger
    key = ledger.scope_for_project(pid, max_cost_usd=10.0).scope_key
    rid = ledger.reserve(key, usd=5.0, tokens=0).reservation_id
    amounts = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
    calls = [lambda other, a=a: other.commit(rid, usd=a, tokens=0, event_key="evt") for a in amounts]

    outcomes = _race(url, key, calls)

    assert [kind for kind, _ in outcomes].count("ok") == 1, outcomes
    assert [kind for kind, _ in outcomes].count("contradiction") == len(amounts) - 1, outcomes
    snap = ledger.snapshot(key)
    assert snap.consumed_usd in amounts and snap.reserved_usd == 0.0


def test_a_concurrent_release_under_a_commit_key_is_a_contradiction(url):
    manager = ProjectStateManager.from_url(url, create=True)
    pid = manager.create_project(name="p")
    ledger = manager.budget_ledger
    key = ledger.scope_for_project(pid, max_cost_usd=10.0).scope_key
    rid = ledger.reserve(key, usd=1.0, tokens=0).reservation_id
    calls = [lambda other: other.commit(rid, usd=0.5, tokens=0, event_key="evt")] + [
        lambda other: other.release(rid, reason="x", event_key="evt")
    ] * 3

    outcomes = _race(url, key, calls)

    assert [kind for kind, _ in outcomes].count("autre") == 0, outcomes
    snap = ledger.snapshot(key)
    # soit le commit gagne (0,5 consommé), soit la libération gagne (rien) : jamais les deux, jamais de mélange
    assert (snap.consumed_usd, snap.reserved_usd) in {(0.5, 0.0), (0.0, 0.0)}
    assert len(ledger.reservations(key)) == 1


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


# --- causes de blocage INDÉPENDANTES ---------------------------------------------------------------------------


def _capped(manager, **kw):
    ledger = manager.budget_ledger
    key = ledger.create_planning_scope(max_cost_usd=1, max_tokens=1000, **kw).scope_key
    return ledger, key


def _unknown_call(ledger, key, reason="appel incomplet"):
    reservation = ledger.reserve(key, tokens=20, usd=0.02)
    ledger.mark_unknown(reservation.reservation_id, reason=reason)
    return reservation.reservation_id


def test_settling_an_unknown_call_never_lifts_an_independent_bound_violation(manager):
    ledger, key = _capped(manager)
    rid = _unknown_call(ledger, key)
    ledger.block(key, reason="borne du fournisseur démentie", event_key="bound-1")
    assert ledger.snapshot(key).blocked_reason == "appel incomplet"  # la plus ancienne cause s'affiche

    ledger.resolve_unknown(rid, usd=0.01, tokens=10, event_key="resolve-1", reason="relevé fournisseur")

    snap = ledger.snapshot(key)
    assert snap.blocked and snap.blocked_reason == "borne du fournisseur démentie"  # la cause restante, pas l'ancienne
    assert [b["block_key"] for b in ledger.open_blocks(key)] == ["bound-1"]
    with pytest.raises(BudgetRefused) as refused:
        ledger.reserve(key, tokens=1, usd=0.001)
    assert refused.value.code == REFUSED_BLOCKED


def test_each_cause_is_resolved_explicitly_and_the_scope_unblocks_only_when_none_remains(manager):
    ledger, key = _capped(manager)
    rid = _unknown_call(ledger, key)
    ledger.block(key, reason="borne démentie", event_key="bound-1")
    ledger.block(key, reason="blocage manuel", event_key="manual-1", kind="manual")

    ledger.resolve_block(key, "bound-1", event_key="r-bound", reason="hypothèse corrigée")
    assert ledger.snapshot(key).blocked  # usage inconnu + cause manuelle restent
    ledger.resolve_unknown(rid, usd=0.01, tokens=10, event_key="r-unknown")
    assert ledger.snapshot(key).blocked_reason == "blocage manuel"
    assert ledger.resolve_block(key, "manual-1", event_key="r-manual", reason="vérifié") is True
    snap = ledger.snapshot(key)
    assert not snap.blocked and snap.blocked_reason is None and ledger.open_blocks(key) == []


def test_blocks_survive_a_restart_and_a_resolution_is_idempotent(url):
    first = ProjectStateManager.from_url(url, create=True)
    ledger, key = _capped(first)
    ledger.block(key, reason="borne démentie", event_key="bound-1")
    restarted = ProjectStateManager.from_url(url).budget_ledger
    assert restarted.snapshot(key).blocked and len(restarted.open_blocks(key)) == 1
    assert restarted.resolve_block(key, "bound-1", event_key="r1", reason="ok") is True
    assert restarted.resolve_block(key, "bound-1", event_key="r1", reason="ok") is False  # rejeu identique
    assert not ProjectStateManager.from_url(url).budget_ledger.snapshot(key).blocked


def test_a_block_on_an_uncapped_scope_becomes_blocking_once_a_cap_is_configured(manager):
    ledger = manager.budget_ledger
    key = ledger.create_planning_scope().scope_key  # aucun plafond : rien à protéger encore
    ledger.block(key, reason="borne démentie", event_key="bound-1")
    assert not ledger.snapshot(key).blocked
    ledger.create_planning_scope(scope_key=key, max_cost_usd=1)  # un plafond apparaît
    assert ledger.snapshot(key).blocked_reason == "borne démentie"


def test_a_block_key_replayed_with_another_scope_or_meaning_is_refused(manager):
    ledger = manager.budget_ledger
    one = ledger.create_planning_scope(scope_key="planning:one", max_cost_usd=1).scope_key
    two = ledger.create_planning_scope(scope_key="planning:two", max_cost_usd=1).scope_key
    ledger.block(one, reason="borne démentie", event_key="bound-failure")
    ledger.block(one, reason="borne démentie", event_key="bound-failure")  # rejeu identique : idempotent
    assert len(ledger.open_blocks(one)) == 1
    with pytest.raises(BudgetIdentityError):
        ledger.block(two, reason="borne démentie", event_key="bound-failure")  # autre scope
    with pytest.raises(BudgetIdentityError):
        ledger.block(one, reason="autre motif", event_key="bound-failure")  # autre sens
    with pytest.raises(BudgetIdentityError):
        ledger.block(one, reason="borne démentie", event_key="bound-failure", kind="manual")  # autre type
    assert not ledger.snapshot(two).blocked  # la seconde cause n'a pas été perdue en silence : elle a été refusée


def test_a_resolution_key_replayed_with_another_scope_cause_or_amount_is_refused(manager):
    ledger = manager.budget_ledger
    one = ledger.create_planning_scope(scope_key="planning:one", max_cost_usd=1).scope_key
    two = ledger.create_planning_scope(scope_key="planning:two", max_cost_usd=1).scope_key
    ledger.block(one, reason="a", event_key="b1")
    ledger.block(one, reason="b", event_key="b2")
    ledger.block(two, reason="c", event_key="b3")
    ledger.resolve_block(one, "b1", event_key="resolution", reason="corrigé")
    assert ledger.resolve_block(one, "b1", event_key="resolution", reason="corrigé") is False
    with pytest.raises(BudgetIdentityError):
        ledger.resolve_block(two, "b3", event_key="resolution", reason="corrigé")  # autre scope
    with pytest.raises(BudgetIdentityError):
        ledger.resolve_block(one, "b2", event_key="resolution", reason="corrigé")  # autre cause
    with pytest.raises(BudgetIdentityError):
        ledger.resolve_block(one, "b1", event_key="resolution", reason="corrigé", usd=0.5)  # autre montant
    with pytest.raises(BudgetIdentityError):
        ledger.resolve_block(one, "b1", event_key="resolution", reason="autre justification")
    assert ledger.snapshot(two).blocked and [b["block_key"] for b in ledger.open_blocks(one)] == ["b2"]


def test_concurrent_resolutions_of_an_unknown_call_never_erase_a_concurrent_block(url):
    """Règlement sans rapport + blocage en parallèle, sur des connexions réelles : le blocage reste, toujours."""
    manager = ProjectStateManager.from_url(url, create=True)
    ledger, key = _capped(manager)
    rids = [ledger.reserve(key, tokens=20, usd=0.02).reservation_id for i in range(6)]
    for i, rid in enumerate(rids):  # réserver d'abord TOUT, puis passer en inconnu (un inconnu bloque les réservations)
        ledger.mark_unknown(rid, reason=f"appel {i}")
    barrier = threading.Barrier(len(rids) + 1)
    errors = []

    def resolver(rid):
        other = ProjectStateManager.from_url(url).budget_ledger
        barrier.wait()
        try:
            other.resolve_unknown(rid, usd=0.001, tokens=1, event_key=f"r:{rid}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(repr(exc))

    def blocker():
        other = ProjectStateManager.from_url(url).budget_ledger
        barrier.wait()
        try:
            other.block(key, reason="borne démentie", event_key="bound-1")
        except BaseException as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=resolver, args=(rid,)) for rid in rids] + [threading.Thread(target=blocker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    snap = ledger.snapshot(key)
    assert snap.unknown_usd == 0 and snap.blocked and snap.blocked_reason == "borne démentie"


def test_concurrent_block_keys_with_different_scopes_keep_exactly_one_winner(url):
    manager = ProjectStateManager.from_url(url, create=True)
    ledger = manager.budget_ledger
    keys = [ledger.create_planning_scope(scope_key=f"planning:s{i}", max_cost_usd=1).scope_key for i in range(6)]
    barrier = threading.Barrier(len(keys))
    outcomes = []

    def worker(scope):
        other = ProjectStateManager.from_url(url).budget_ledger
        barrier.wait()
        try:
            other.block(scope, reason="borne démentie", event_key="shared-key")
            outcomes.append("ok")
        except BudgetIdentityError:
            outcomes.append("contradiction")
        except BaseException as exc:  # noqa: BLE001
            outcomes.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert outcomes.count("ok") == 1 and outcomes.count("contradiction") == len(keys) - 1, outcomes
    assert sum(1 for k in keys if ledger.snapshot(k).blocked) == 1


# --- historique legacy : dernier cumul, ambiguïté bloquante ---------------------------------------------------

INF = float("inf")
# nom -> (série coût, série tokens, (consommé µ$, tokens), clés de blocs ambigus attendues)
LEGACY_SCENARIOS = {
    "increasing": ([0.2, 0.9], [400.0, 900.0], (900_000, 900), []),
    "decreasing": ([0.9, 0.2], [900.0, 400.0], (900_000, 900), ["run_cost_usd", "run_tokens"]),
    "decreasing-cost-only": ([0.9, 0.2], [400.0, 900.0], (900_000, 900), ["run_cost_usd"]),
    "invalid-value": ([0.5, INF, 0.7], [100.0], (700_000, 100), ["run_cost_usd"]),
    "negative-value": ([0.5, -1.0], [100.0], (500_000, 100), ["run_cost_usd"]),
    "only-invalid": ([INF], [], (0, 0), ["run_cost_usd"]),
    "zero": ([0.0], [0.0], (0, 0), []),
    "no-history": ([], [], (0, 0), []),
}


def _seed_legacy(manager, pid, cost, tokens):
    for value in cost:
        manager.add_metric(pid, name="run_cost_usd", value=value)
    for value in tokens:
        manager.add_metric(pid, name="run_tokens", value=value)


def _legacy_view(ledger, pid, cap=5.0):
    snap = ledger.scope_for_project(pid, max_cost_usd=cap, max_tokens=10_000_000)
    blocks = sorted(b["block_key"].rsplit(":", 1)[-1] for b in ledger.open_blocks(snap.scope_key))
    return (snap.consumed_micro_usd, snap.consumed_tokens), blocks, snap.blocked


@pytest.mark.parametrize("scenario", list(LEGACY_SCENARIOS))
def test_legacy_history_imports_the_last_cumulative_and_blocks_when_ambiguous(manager, scenario):
    cost, tokens, expected, anomalies = LEGACY_SCENARIOS[scenario]
    pid = manager.create_project(name="legacy")
    _seed_legacy(manager, pid, cost, tokens)

    consumed, blocks, blocked = _legacy_view(manager.budget_ledger, pid)

    assert consumed == expected  # jamais la somme des cumuls ; en cas de décroissance, la borne au maximum observé
    assert blocks == sorted(anomalies) and blocked is bool(anomalies)
    assert _legacy_view(manager.budget_ledger, pid) == (consumed, blocks, blocked)  # rejeu : import unique


def test_an_ambiguous_history_stays_blocked_until_the_operator_resolves_it_with_the_established_spend(manager):
    pid = manager.create_project(name="legacy")
    _seed_legacy(manager, pid, [0.9, 0.2], [])
    ledger = manager.budget_ledger
    snap = ledger.scope_for_project(pid, max_cost_usd=5.0)
    assert snap.blocked and "PAS la dépense totale établie" in snap.blocked_reason
    assert "ambigu" in snap.blocked_reason
    (block,) = ledger.open_blocks(snap.scope_key)
    assert block["kind"] == "ambiguous_history"

    ledger.resolve_block(
        snap.scope_key, block["block_key"], event_key="resolve-history", reason="relevé fournisseur", usd=1.5, tokens=0
    )

    after = ledger.snapshot(snap.scope_key)
    assert not after.blocked and after.consumed_usd == pytest.approx(0.9 + 1.5)  # la dépense établie s'ajoute


def test_an_ambiguous_history_recorded_without_a_cap_blocks_once_a_cap_appears(manager):
    pid = manager.create_project(name="legacy")
    _seed_legacy(manager, pid, [0.9, 0.2], [])
    ledger = manager.budget_ledger
    assert not ledger.scope_for_project(pid).blocked  # aucun plafond : rien à protéger encore, mais la cause est notée
    assert ledger.scope_for_project(pid, max_cost_usd=5.0).blocked


def test_analyze_legacy_series_flags_nan_and_decreases_and_keeps_the_maximum():
    from collegue.state.budget_ledger import analyze_legacy_series

    assert analyze_legacy_series([0.2, 0.9]) == (0.9, [])
    bound, issues = analyze_legacy_series([0.9, float("nan"), 0.2, None, -3])
    assert bound == 0.9 and len(issues) == 4 and any("décroissante" in i for i in issues)


def _migrate_sqlite_between(tmp_path, monkeypatch, seed):
    """Alembic jusqu'à 0010, ``seed(url)`` insère l'historique, puis 0011 : la voie MIGRATION."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("STATE_DATABASE_URL", url)
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "collegue" / "migrations"))
    command.upgrade(cfg, "0010")
    seed(url)
    command.upgrade(cfg, "0011")
    return url, cfg, command


@pytest.mark.parametrize("scenario", list(LEGACY_SCENARIOS))
def test_the_migration_import_has_the_same_semantics_as_the_lazy_create_all_import(tmp_path, monkeypatch, scenario):
    from sqlalchemy import create_engine, text

    cost, tokens, _expected, _anomalies = LEGACY_SCENARIOS[scenario]
    lazy_mgr = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'lazy.db'}", create=True)
    lazy_pid = lazy_mgr.create_project(name="legacy")
    _seed_legacy(lazy_mgr, lazy_pid, cost, tokens)
    lazy = _legacy_view(lazy_mgr.budget_ledger, lazy_pid)

    holder = {}

    def seed(url):
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO projects (name, phase, status) VALUES ('legacy', '1', 'active')"))
            holder["pid"] = conn.execute(text("SELECT id FROM projects")).scalar_one()
            for name, series in (("run_cost_usd", cost), ("run_tokens", tokens)):
                for value in series:
                    conn.execute(
                        text(
                            "INSERT INTO metrics (project_id, ts, name, value) VALUES (:p, CURRENT_TIMESTAMP, :n, :v)"
                        ),
                        {"p": holder["pid"], "n": name, "v": value},
                    )
        engine.dispose()

    url, cfg, command = _migrate_sqlite_between(tmp_path, monkeypatch, seed)
    migrated = _legacy_view(ProjectStateManager.from_url(url, create=False).budget_ledger, holder["pid"])

    assert migrated == lazy  # même consommé, mêmes causes d'ambiguïté, même blocage

    command.downgrade(cfg, "0010")
    command.upgrade(cfg, "0011")  # rejeu après downgrade : une seule importation
    assert _legacy_view(ProjectStateManager.from_url(url, create=False).budget_ledger, holder["pid"]) == lazy
