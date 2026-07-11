"""Comptabilisation par appel du sampling planner/QA (#590)."""

from types import SimpleNamespace

import pytest

from collegue.core.llm import LLMRole
from collegue.core.llm.client import UsageAccountingError, accounted_sample
from collegue.monitoring.metrics import BudgetStatus
from collegue.monitoring.sampling_usage import record_usage, take_usage
from collegue.tools.quotas import BudgetExceeded


class _Collector:
    def __init__(self):
        self.records = []
        self.tokens = 0
        self.cost = 0.0

    def record_execution(self, **kwargs):
        self.records.append(kwargs)
        self.tokens += kwargs["input_tokens"] + kwargs["output_tokens"]
        self.cost += kwargs["cost_usd"]

    def would_exceed_budget(self, max_cost_usd=None, max_tokens=None, **_kwargs):
        if max_cost_usd and self.cost >= max_cost_usd:
            return BudgetStatus(True, "cost", self.cost, max_cost_usd)
        if max_tokens and self.tokens >= max_tokens:
            return BudgetStatus(True, "tokens", self.tokens, max_tokens)
        return None


def _settings(**overrides):
    values = {
        "LLM_PROVIDER": "openai",
        "LLM_MODEL": "gpt-5.4",
        "LLM_MODEL_PLANNER": "gpt-5.4",
        "MAX_COST_USD": 0,
        "MAX_TOKENS_BUDGET": 0,
        "BUDGET_EXHAUSTED_ACTION": "pause",
        "LLM_CALL_TIMEOUT": 0,
        "CODER_SUBSCRIPTION": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Ctx:
    def __init__(self, *, usage=(100, 20, "gpt-5.4"), error=None):
        self.usage = usage
        self.error = error

    async def sample(self, **_kwargs):
        if self.usage is not None:
            record_usage(*self.usage)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text="ok")


async def test_accounted_sample_records_exact_role_model_cost_and_tokens():
    collector = _Collector()
    result = await accounted_sample(
        _Ctx(),
        role=LLMRole.PLANNER,
        operation="planner.spec",
        settings_obj=_settings(),
        collector=collector,
        messages="x",
    )
    assert result.text == "ok"
    assert collector.tokens == 120
    assert collector.cost == pytest.approx(100 * 2.5e-6 + 20 * 15e-6)
    assert collector.records[0]["metadata"]["model"] == "gpt-5.4"


async def test_accounted_sample_restores_parent_usage_without_leak():
    take_usage()
    record_usage(7, 3, "parent")
    await accounted_sample(
        _Ctx(usage=(10, 5, "child")),
        role=LLMRole.PLANNER,
        operation="planner.decompose",
        settings_obj=_settings(),
        collector=_Collector(),
        messages="x",
    )
    assert take_usage() == (7, 3, "parent")


async def test_missing_usage_fails_closed_when_token_cap_is_active():
    with pytest.raises(UsageAccountingError, match="Usage LLM absent"):
        await accounted_sample(
            _Ctx(usage=None),
            role=LLMRole.PLANNER,
            operation="planner.spec",
            settings_obj=_settings(MAX_TOKENS_BUDGET=100),
            collector=_Collector(),
            messages="x",
        )


async def test_unknown_remote_price_fails_before_call_when_cost_cap_is_active():
    ctx = _Ctx()
    ctx.called = False

    async def sample(**_kwargs):
        ctx.called = True
        return SimpleNamespace(text="should not run")

    ctx.sample = sample
    with pytest.raises(UsageAccountingError, match="Tarif inconnu"):
        await accounted_sample(
            ctx,
            role=LLMRole.PLANNER,
            operation="planner.spec",
            settings_obj=_settings(LLM_MODEL_PLANNER="unknown-model", MAX_COST_USD=1),
            collector=_Collector(),
            messages="x",
        )
    assert ctx.called is False


async def test_post_debit_budget_check_blocks_returned_result():
    with pytest.raises(BudgetExceeded):
        await accounted_sample(
            _Ctx(usage=(60, 40, "gpt-5.4")),
            role=LLMRole.PLANNER,
            operation="planner.spec",
            settings_obj=_settings(MAX_TOKENS_BUDGET=100),
            collector=_Collector(),
            messages="x",
        )


async def test_usage_is_debited_even_when_sampling_then_raises():
    collector = _Collector()
    with pytest.raises(ValueError, match="réponse invalide"):
        await accounted_sample(
            _Ctx(usage=(9, 4, "gpt-5.4"), error=ValueError("réponse invalide")),
            role=LLMRole.PLANNER,
            operation="planner.decompose",
            settings_obj=_settings(),
            collector=collector,
            messages="x",
        )
    assert collector.tokens == 13
    assert collector.records[0]["success"] is False
