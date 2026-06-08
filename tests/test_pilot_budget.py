"""Tests F2 (#375) : contrôleur budget-temps (deadline + budget, auto-pause)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from collegue.config import Settings
from collegue.monitoring.metrics import BudgetStatus
from collegue.pilot import BudgetTimeController

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class _Collector:
    """Collecteur factice : renvoie un BudgetStatus fixe (ou None = sous budget)."""

    def __init__(self, status=None):
        self._status = status

    def would_exceed_budget(self, *args, **kwargs):
        return self._status


def _clock(dt):
    return lambda: dt


def _ctrl(*, deadline_seconds=0.0, status=None, now=T0, started_at=T0):
    return BudgetTimeController(
        started_at=started_at,
        deadline_seconds=deadline_seconds,
        collector=_Collector(status),
        clock=_clock(now),
    )


# --- continue -------------------------------------------------------------------


def test_continue_under_budget_before_deadline():
    d = _ctrl(deadline_seconds=100, now=T0 + timedelta(seconds=10)).should_continue()
    assert d.action == "continue"
    assert d.ok is True


def test_no_deadline_and_no_budget_always_continues():
    # deadline 0 + collecteur sous budget → continue même loin dans le futur.
    d = _ctrl(deadline_seconds=0, now=T0 + timedelta(days=365)).should_continue()
    assert d.ok is True
    assert _ctrl(deadline_seconds=0).deadline is None


# --- deadline -------------------------------------------------------------------


def test_deadline_reached():
    d = _ctrl(deadline_seconds=10, now=T0 + timedelta(seconds=11)).should_continue()
    assert d.action == "deadline_reached"
    assert d.ok is False


def test_exactly_at_deadline_stops():
    # comparaison >= : à l'instant pile de la deadline, on s'arrête.
    d = _ctrl(deadline_seconds=10, now=T0 + timedelta(seconds=10)).should_continue()
    assert d.action == "deadline_reached"


def test_time_remaining_seconds():
    ctrl = _ctrl(deadline_seconds=100, now=T0 + timedelta(seconds=30))
    assert abs(ctrl.time_remaining_seconds() - 70) < 1e-6
    assert _ctrl(deadline_seconds=0).time_remaining_seconds() is None


# --- budget ---------------------------------------------------------------------


def test_paused_when_budget_exhausted():
    status = BudgetStatus(exceeded=True, limit_type="cost", current=10.0, limit=5.0)
    d = _ctrl(deadline_seconds=100, status=status, now=T0 + timedelta(seconds=1)).should_continue()
    assert d.action == "paused_budget"
    assert d.ok is False
    assert d.budget_status is status


def test_warn_mode_budget_does_not_pause():
    # BUDGET_EXHAUSTED_ACTION="warn" : budget dépassé mais on continue (comme C4
    # n'y bloque pas les appels LLM). budget_status reste informatif.
    status = BudgetStatus(exceeded=True, limit_type="cost", current=10.0, limit=5.0)
    ctrl = BudgetTimeController(
        started_at=T0,
        deadline_seconds=100,
        collector=_Collector(status),
        settings_obj=SimpleNamespace(MAX_COST_USD=5.0, MAX_TOKENS_BUDGET=0, BUDGET_EXHAUSTED_ACTION="warn"),
        clock=_clock(T0 + timedelta(seconds=1)),
    )
    d = ctrl.should_continue()
    assert d.ok is True
    assert d.budget_status is status


def test_deadline_takes_precedence_over_budget():
    # deadline ET budget dépassés → deadline d'abord (vérifiée en premier).
    status = BudgetStatus(exceeded=True, limit_type="tokens", current=99, limit=10)
    d = _ctrl(deadline_seconds=10, status=status, now=T0 + timedelta(seconds=20)).should_continue()
    assert d.action == "deadline_reached"


# --- robustesse datetime naïf ---------------------------------------------------


def test_naive_started_at_and_clock_do_not_crash():
    # started_at ET horloge naïfs : pas de TypeError (coercition UTC), comportement
    # cohérent (deadline 10s, +11s naïf → atteinte).
    naive_start = datetime(2026, 1, 1, 12, 0, 0)  # sans tzinfo
    ctrl = BudgetTimeController(
        started_at=naive_start,
        deadline_seconds=10,
        collector=_Collector(None),
        clock=lambda: datetime(2026, 1, 1, 12, 0, 11),  # naïf aussi
    )
    assert ctrl.should_continue().action == "deadline_reached"
    assert ctrl.time_remaining_seconds() < 0


def test_caps_forwarded_to_collector():
    # La consistance settings→budget : les plafonds injectés sont bien transmis
    # à would_exceed_budget (pas un repli silencieux sur les globals).
    seen = {}

    class _Capturing:
        def would_exceed_budget(self, max_cost_usd=None, max_tokens=None):
            seen["cost"] = max_cost_usd
            seen["tokens"] = max_tokens
            return None

    BudgetTimeController(
        started_at=T0,
        deadline_seconds=0,
        collector=_Capturing(),
        settings_obj=SimpleNamespace(MAX_COST_USD=7.5, MAX_TOKENS_BUDGET=42, BUDGET_EXHAUSTED_ACTION="pause"),
        clock=_clock(T0),
    ).should_continue()
    assert seen == {"cost": 7.5, "tokens": 42}


# --- deadline depuis les settings ----------------------------------------------


def test_deadline_resolved_from_settings():
    ctrl = BudgetTimeController(
        started_at=T0,
        settings_obj=SimpleNamespace(COLLEGUE_RUN_DEADLINE_SECONDS=10.0),
        collector=_Collector(None),
        clock=_clock(T0 + timedelta(seconds=15)),
    )
    assert ctrl.should_continue().action == "deadline_reached"


# --- validateur de config -------------------------------------------------------


def test_config_deadline_validator_neutralizes_bad_values():
    assert Settings(COLLEGUE_RUN_DEADLINE_SECONDS=-5).COLLEGUE_RUN_DEADLINE_SECONDS == 0.0
    assert Settings(COLLEGUE_RUN_DEADLINE_SECONDS=float("nan")).COLLEGUE_RUN_DEADLINE_SECONDS == 0.0
    assert Settings(COLLEGUE_RUN_DEADLINE_SECONDS=30).COLLEGUE_RUN_DEADLINE_SECONDS == 30.0
