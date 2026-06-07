"""Tests C4 (#338) : budget DUR global ($/tokens) + auto-pause.

Couvre :
- `MetricsCollector.would_exceed_budget()` (sous plafond, dépassement coût/tokens, désactivé) ;
- `enforce_budget()` (pause = lève + trace, warn = ne bloque pas, désactivé = no-op) ;
- l'exception `BudgetExceeded` et la normalisation de `BUDGET_EXHAUSTED_ACTION`.
"""

import pytest

from collegue.monitoring.metrics import MetricsCollector, enforce_budget
from collegue.tools.quotas import BudgetExceeded


@pytest.fixture
def collector(monkeypatch):
    """MetricsCollector isolé du disque, 1 USD/token in & out (coût == tokens)."""
    monkeypatch.setattr(MetricsCollector, "_load_from_disk", lambda self: None)
    monkeypatch.setattr(MetricsCollector, "_save_to_disk", lambda self: None)
    return MetricsCollector(input_cost_per_token=1.0, output_cost_per_token=1.0)


def _spend(collector, input_tokens=0, output_tokens=0):
    collector.record_execution(
        expert_name="e",
        duration_ms=1.0,
        success=True,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


class _FakeSettings:
    def __init__(self, cost=0.0, tokens=0, action="pause"):
        self.MAX_COST_USD = cost
        self.MAX_TOKENS_BUDGET = tokens
        self.BUDGET_EXHAUSTED_ACTION = action


@pytest.fixture
def fake_activity_log(monkeypatch):
    """Capture les appels log_budget_event pour vérifier le traçage."""
    events = []

    class _Log:
        def log_budget_event(self, **kwargs):
            events.append(kwargs)

    monkeypatch.setattr("collegue.monitoring.activity_log.get_activity_log", lambda: _Log())
    return events


# --- would_exceed_budget --------------------------------------------------------


def test_under_cost_budget_returns_none(collector):
    _spend(collector, input_tokens=5)  # coût = 5.0
    assert collector.would_exceed_budget(max_cost_usd=10.0, max_tokens=0) is None


def test_cost_budget_exceeded(collector):
    _spend(collector, input_tokens=12)  # coût = 12.0
    status = collector.would_exceed_budget(max_cost_usd=10.0, max_tokens=0)
    assert status is not None and status.exceeded
    assert status.limit_type == "cost"
    assert status.current == pytest.approx(12.0)
    assert status.limit == pytest.approx(10.0)


def test_cost_budget_exact_boundary_blocks(collector):
    # Comparaison >= : atteindre la limite bloque l'appel suivant.
    _spend(collector, input_tokens=10)  # coût = 10.0
    assert collector.would_exceed_budget(max_cost_usd=10.0, max_tokens=0) is not None


def test_under_token_budget_returns_none(collector):
    _spend(collector, input_tokens=40, output_tokens=40)  # 80 tokens
    assert collector.would_exceed_budget(max_cost_usd=0, max_tokens=100) is None


def test_token_budget_exceeded(collector):
    _spend(collector, input_tokens=100, output_tokens=50)  # 150 tokens
    status = collector.would_exceed_budget(max_cost_usd=0, max_tokens=100)
    assert status is not None and status.limit_type == "tokens"
    assert status.current == pytest.approx(150.0)
    assert status.limit == pytest.approx(100.0)


def test_disabled_never_blocks(collector):
    _spend(collector, input_tokens=10_000, output_tokens=10_000)
    assert collector.would_exceed_budget(max_cost_usd=0, max_tokens=0) is None


def test_cost_checked_before_tokens(collector):
    # Les deux plafonds dépassés → c'est le coût qui est signalé en premier.
    _spend(collector, input_tokens=200)  # coût 200, tokens 200
    status = collector.would_exceed_budget(max_cost_usd=10.0, max_tokens=10)
    assert status.limit_type == "cost"


# --- enforce_budget -------------------------------------------------------------


def test_enforce_under_budget_does_not_raise(collector, fake_activity_log):
    _spend(collector, input_tokens=5)
    enforce_budget(collector=collector, settings_obj=_FakeSettings(cost=100.0, action="pause"))
    assert fake_activity_log == []


def test_enforce_pause_raises_and_traces_cost(collector, fake_activity_log):
    _spend(collector, input_tokens=20)
    with pytest.raises(BudgetExceeded) as exc:
        enforce_budget(collector=collector, settings_obj=_FakeSettings(cost=10.0, action="pause"))
    assert exc.value.budget_type == "cost"
    # L'événement est tracé (AC : "l'événement est tracé").
    assert len(fake_activity_log) == 1
    assert fake_activity_log[0]["limit_type"] == "cost"


def test_enforce_pause_raises_on_tokens(collector, fake_activity_log):
    _spend(collector, input_tokens=100, output_tokens=100)  # 200 tokens
    with pytest.raises(BudgetExceeded) as exc:
        enforce_budget(collector=collector, settings_obj=_FakeSettings(tokens=150, action="pause"))
    assert exc.value.budget_type == "tokens"


def test_enforce_warn_does_not_raise_but_traces(collector, fake_activity_log):
    _spend(collector, input_tokens=20)
    # action=warn : journalise (et trace) mais ne bloque pas.
    enforce_budget(collector=collector, settings_obj=_FakeSettings(cost=10.0, action="warn"))
    assert len(fake_activity_log) == 1
    assert fake_activity_log[0]["action"] == "warn"


def test_enforce_disabled_is_noop(collector, fake_activity_log):
    _spend(collector, input_tokens=10_000)
    enforce_budget(collector=collector, settings_obj=_FakeSettings(cost=0.0, tokens=0, action="pause"))
    assert fake_activity_log == []


# --- BudgetExceeded -------------------------------------------------------------


def test_budget_exceeded_carries_fields():
    exc = BudgetExceeded("cost", 12.5, 10.0)
    assert exc.budget_type == "cost"
    assert exc.current == 12.5
    assert exc.limit == 10.0
    assert "pause" in str(exc).lower()


def test_budget_exceeded_is_baseexception_not_exception():
    # L'auto-pause est un signal d'arrêt dur : hériter de BaseException garantit
    # qu'aucun `except Exception` générique (agent_loop, orchestrateur, tools) ne
    # l'avale en simple « erreur LLM » — sinon la pause serait illusoire.
    assert issubclass(BudgetExceeded, BaseException)
    assert not issubclass(BudgetExceeded, Exception)


def test_budget_exceeded_traverses_generic_except():
    # Régression du bug "pause illusoire" : un `except Exception` ne doit PAS
    # attraper BudgetExceeded (il doit remonter jusqu'au sommet).
    caught_by_generic = False
    propagated = False
    try:
        try:
            raise BudgetExceeded("cost", 1.0, 0.5)
        except Exception:
            caught_by_generic = True
    except BudgetExceeded:
        propagated = True
    assert caught_by_generic is False
    assert propagated is True


# --- fail-safe coût non fini (NaN/inf) ------------------------------------------


def test_nan_cost_does_not_blind_cap(collector):
    # NaN >= cap est False → sans fail-safe, un coût corrompu aveuglerait le cap.
    _spend(collector, input_tokens=1)
    collector._experts["e"].total_cost = float("nan")
    status = collector.would_exceed_budget(max_cost_usd=10.0, max_tokens=0)
    assert status is not None and status.limit_type == "cost"


def test_inf_cost_trips_cap(collector):
    _spend(collector, input_tokens=1)
    collector._experts["e"].total_cost = float("inf")
    status = collector.would_exceed_budget(max_cost_usd=10.0, max_tokens=0)
    assert status is not None and status.limit_type == "cost"


# --- traçage disque réel (log_budget_event) -------------------------------------


def test_log_budget_event_writes_valid_jsonl(tmp_path):
    from collegue.monitoring.activity_log import ActivityLog

    log = ActivityLog(base_dir=tmp_path)
    log.log_budget_event(limit_type="cost", current=12.3456789, limit=10.0, action="pause")
    events = log.read_events(event_type="budget_exhausted")
    assert len(events) == 1
    assert events[0]["limit_type"] == "cost"
    assert events[0]["action"] == "pause"
    assert events[0]["limit"] == 10.0


# --- câblage handler : enforce_budget AVANT l'appel LLM -------------------------


@pytest.mark.asyncio
async def test_handler_enforces_budget_before_llm_call(monkeypatch):
    """Le chokepoint universel (_create) appelle enforce_budget AVANT inner().

    Sans ce test, la ligne qui rend la feature réelle (sampling_handler._create)
    serait du code mort : un refactor pourrait la supprimer en gardant la CI verte.
    """
    pytest.importorskip("fastmcp")
    pytest.importorskip("openai")
    import collegue.monitoring.metrics as metrics_mod
    from collegue.core.llm.sampling_handler import build_sampling_handler

    calls = []

    def _fake_enforce(*a, **k):
        calls.append("enforce")
        raise BudgetExceeded("cost", 1.0, 0.5)

    # _create importe enforce_budget en lazy depuis ce module → patch pris en compte.
    monkeypatch.setattr(metrics_mod, "enforce_budget", _fake_enforce)

    handler = build_sampling_handler(default_model="m", api_key="x", base_url=None)
    assert handler is not None

    # Appeler le create enveloppé : enforce lève AVANT tout appel réseau (inner).
    # Si BudgetExceeded remonte, c'est qu'enforce a court-circuité avant inner.
    with pytest.raises(BudgetExceeded):
        await handler.client.chat.completions.create(model="m", messages=[])
    assert calls == ["enforce"]


# --- normalisation BUDGET_EXHAUSTED_ACTION --------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("pause", "pause"), ("warn", "warn"), ("PAUSE", "pause"), ("WARN", "warn"), ("garbage", "pause"), ("", "pause")],
)
def test_budget_action_normalized(raw, expected):
    from collegue.config import Settings

    s = Settings(_env_file=None, BUDGET_EXHAUSTED_ACTION=raw)
    assert s.BUDGET_EXHAUSTED_ACTION == expected
