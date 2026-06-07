"""Tests C5 (#339) : timeout par appel LLM (sample_with_timeout)."""

import asyncio

import pytest

from collegue.core.llm.client import LLMCallTimeout, sample_with_timeout


class _SlowCtx:
    """ctx factice dont sample() dort `delay` secondes."""

    def __init__(self, delay):
        self.delay = delay
        self.calls = []

    async def sample(self, **kwargs):
        self.calls.append(kwargs)
        await asyncio.sleep(self.delay)
        return "done"


@pytest.mark.asyncio
async def test_timeout_raises_clean_exception():
    ctx = _SlowCtx(delay=1.0)
    with pytest.raises(LLMCallTimeout):
        await sample_with_timeout(ctx, timeout=0.05, messages="x")


@pytest.mark.asyncio
async def test_no_timeout_returns_result():
    ctx = _SlowCtx(delay=0.0)
    assert await sample_with_timeout(ctx, timeout=0, messages="x") == "done"


@pytest.mark.asyncio
async def test_negative_timeout_disabled():
    ctx = _SlowCtx(delay=0.0)
    # <= 0 → désactivé, l'appel passe normalement.
    assert await sample_with_timeout(ctx, timeout=-1, messages="x") == "done"


@pytest.mark.asyncio
async def test_timeout_resolved_from_settings():
    class _S:
        LLM_CALL_TIMEOUT = 0.05

    ctx = _SlowCtx(delay=1.0)
    with pytest.raises(LLMCallTimeout):
        await sample_with_timeout(ctx, settings_obj=_S(), messages="x")


@pytest.mark.asyncio
async def test_settings_disabled_lets_call_through():
    class _S:
        LLM_CALL_TIMEOUT = 0.0

    ctx = _SlowCtx(delay=0.0)
    assert await sample_with_timeout(ctx, settings_obj=_S(), messages="x") == "done"


@pytest.mark.asyncio
async def test_kwargs_forwarded_to_sample():
    ctx = _SlowCtx(delay=0.0)
    await sample_with_timeout(ctx, timeout=0, messages="hello", temperature=0.3)
    assert ctx.calls[0] == {"messages": "hello", "temperature": 0.3}


@pytest.mark.asyncio
async def test_underlying_coro_cancelled_on_timeout():
    """Annulation propre : la coroutine sous-jacente reçoit CancelledError.

    On synchronise via un Event posé dans le handler CancelledError plutôt qu'un
    sleep fixe (évite la flakiness sur runner chargé).
    """
    cancelled = asyncio.Event()

    class _Ctx:
        async def sample(self, **kwargs):
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "done"

    with pytest.raises(LLMCallTimeout):
        await sample_with_timeout(_Ctx(), timeout=0.05, messages="x")
    await asyncio.wait_for(cancelled.wait(), timeout=2.0)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_swallowed_cancellation_is_noop():
    """Limite documentée (F6) : si ctx.sample avale CancelledError, wait_for ne
    lève pas TimeoutError → le timeout est un no-op (pas de LLMCallTimeout)."""

    class _SwallowCtx:
        async def sample(self, **kwargs):
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                return "swallowed"  # ne relance pas → défait wait_for
            return "done"

    result = await sample_with_timeout(_SwallowCtx(), timeout=0.05, messages="x")
    assert result == "swallowed"


def test_llm_call_timeout_is_recoverable_exception():
    # Contrairement à BudgetExceeded (BaseException), un timeout est récupérable :
    # un `except Exception` (boucle agentique) doit pouvoir le gérer.
    assert issubclass(LLMCallTimeout, Exception)


@pytest.mark.asyncio
async def test_nan_timeout_does_not_crash():
    """F1 : un timeout NaN passe le garde naïf (not nan / nan<=0 == False) et
    planterait wait_for — le garde isfinite doit le traiter comme désactivé."""
    ctx = _SlowCtx(delay=0.0)
    assert await sample_with_timeout(ctx, timeout=float("nan"), messages="x") == "done"


@pytest.mark.asyncio
async def test_inf_timeout_does_not_crash():
    ctx = _SlowCtx(delay=0.0)
    assert await sample_with_timeout(ctx, timeout=float("inf"), messages="x") == "done"


# --- normalisation config LLM_CALL_TIMEOUT (F1) --------------------------------


def test_config_rejects_non_finite_and_negative_timeout():
    from collegue.config import Settings

    assert Settings(_env_file=None, LLM_CALL_TIMEOUT="nan").LLM_CALL_TIMEOUT == 0.0
    assert Settings(_env_file=None, LLM_CALL_TIMEOUT="inf").LLM_CALL_TIMEOUT == 0.0
    assert Settings(_env_file=None, LLM_CALL_TIMEOUT=-5).LLM_CALL_TIMEOUT == 0.0
    assert Settings(_env_file=None, LLM_CALL_TIMEOUT="garbage").LLM_CALL_TIMEOUT == 0.0
    assert Settings(_env_file=None, LLM_CALL_TIMEOUT="30").LLM_CALL_TIMEOUT == 30.0


# --- intégration agent_loop : timeout → itération échouée, pas de hang (F8) -----


@pytest.mark.asyncio
async def test_agent_loop_records_failed_iteration_on_timeout(monkeypatch):
    """Un ctx.sample pendu → LLMCallTimeout → itération échouée + sortie de boucle
    (pas de hang). Teste le vrai chemin via settings.LLM_CALL_TIMEOUT."""
    import collegue.config as config_mod
    from collegue.tools.agent_loop import AgentLoopConfig, AgentLoopMixin

    monkeypatch.setattr(config_mod.settings, "LLM_CALL_TIMEOUT", 0.05)

    class _SlowSampleCtx:
        async def sample(self, **kwargs):
            await asyncio.sleep(5.0)
            return type("R", (), {"text": "tardif"})()

        async def info(self, *a, **k):
            pass

        async def report_progress(self, *a, **k):
            pass

    class _Agent(AgentLoopMixin):
        agent_config = AgentLoopConfig(max_iterations=1)

        async def validate_agent_output(self, output, context):
            return []

        async def assess_agent_quality(self, output, context):
            return 1.0

        async def build_agent_feedback(self, output, errors, quality, context):
            return ""

    # Garde-fou : si la boucle hang malgré le timeout, le test échoue au lieu de bloquer.
    result = await asyncio.wait_for(
        _Agent().agent_execute(initial_prompt="p", system_prompt="s", ctx=_SlowSampleCtx()),
        timeout=3.0,
    )

    assert result.total_iterations == 1
    assert result.iterations[0].validation_passed is False
    assert any("LLM" in e for e in result.iterations[0].validation_errors)
