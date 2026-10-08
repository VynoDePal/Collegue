"""Propagation de l'usage LLM à travers le délai par appel (Python 3.11 ET 3.12).

``asyncio.wait_for`` exécute la coroutine dans une NOUVELLE tâche (contexte copié) sous Python 3.11 : l'usage écrit
par ``ctx.sample`` dans la ContextVar de ``monitoring.sampling_usage`` y restait enfermé, et ``accounted_sample``
levait ``UsageAccountingError`` sur un cycle sain dès qu'un ``LLM_CALL_TIMEOUT`` était actif. Ces tests utilisent le
VRAI ``sample_with_timeout`` et le VRAI ``capture_usage`` ; l'un d'eux vérifie directement que l'appel s'exécute dans
la tâche de l'appelant. Ils sont ROUGES sur 3.11 avec l'ancien ``wait_for`` et verts sur 3.12 (où ``wait_for`` ne crée
pas de tâche) : seule une exécution sous 3.11 détecte la régression.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from collegue.core.llm.client import LLMCallTimeout, UsageAccountingError, accounted_sample, sample_with_timeout
from collegue.monitoring.metrics import MetricsCollector
from collegue.monitoring.sampling_usage import capture_usage, record_usage, take_usage


class _Ctx:
    """ctx factice : écrit l'usage comme le handler réel (ContextVar), après un délai optionnel."""

    def __init__(self, prompt=11, completion=7, *, model="gpt-4o-mini", delay=0.0, emissions=1, then=None):
        self.prompt, self.completion, self.model = prompt, completion, model
        self.delay, self.emissions, self.then = delay, emissions, then
        self.cancelled = False
        self.task = None

    async def sample(self, **kwargs):
        self.task = asyncio.current_task()
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            for _ in range(self.emissions):
                record_usage(self.prompt, self.completion, self.model)
            if self.then is not None:
                await self.then()
            return "ok"
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def _captured(ctx, timeout):
    with capture_usage() as captured:
        result = await sample_with_timeout(ctx, timeout=timeout)
    return result, captured.usage


@pytest.mark.parametrize("timeout", [0, None, 0.5])
async def test_the_usage_reaches_the_caller_with_or_without_a_deadline(timeout):
    result, usage = await _captured(_Ctx(), timeout)
    assert result == "ok" and usage == (11, 7, "gpt-4o-mini")


async def test_the_sampled_call_runs_in_the_callers_task_even_with_a_deadline():
    """Pas de tâche fille au contexte copié (cause de la perte d'usage sous Python 3.11)."""
    ctx = _Ctx()
    with capture_usage():
        await sample_with_timeout(ctx, timeout=0.5)
        assert ctx.task is asyncio.current_task()


async def test_several_emissions_in_one_call_are_aggregated_not_lost():
    result, usage = await _captured(_Ctx(emissions=3), 0.5)
    assert usage == (33, 21, "gpt-4o-mini")  # trois émissions cumulées, une seule fois


async def test_parallel_calls_keep_their_own_usage():
    first, second = await asyncio.gather(_captured(_Ctx(13), 0.5), _captured(_Ctx(17), 0.5))
    assert first == ("ok", (13, 7, "gpt-4o-mini")) and second == ("ok", (17, 7, "gpt-4o-mini"))


async def test_a_nested_capture_neither_steals_from_nor_leaks_into_its_parent():
    with capture_usage() as parent:
        record_usage(23, 3, "parent")
        inner = await _captured(_Ctx(19), 0.5)
        record_usage(1, 1, "parent")
    assert inner == ("ok", (19, 7, "gpt-4o-mini"))
    assert parent.usage == (24, 4, "parent")  # le parent garde SON usage, sans celui de l'appel interne
    assert take_usage() is None  # et rien ne fuit vers l'appel suivant de la tâche


async def test_consecutive_calls_in_one_task_do_not_share_usage():
    with capture_usage() as outer:
        _, first = await _captured(_Ctx(5), 0.5)
        _, second = await _captured(_Ctx(9), 0.5)
    assert first == (5, 7, "gpt-4o-mini") and second == (9, 7, "gpt-4o-mini") and outer.usage is None


async def test_an_error_after_the_usage_was_received_keeps_the_usage_visible():
    async def boom():
        raise RuntimeError("post-traitement en échec après réception de l'usage")

    with capture_usage() as captured:
        with pytest.raises(RuntimeError):
            await sample_with_timeout(_Ctx(then=boom), timeout=0.5)
    assert captured.usage == (11, 7, "gpt-4o-mini")  # la dépense est connue même si l'appel échoue ensuite


async def test_the_deadline_still_cancels_and_keeps_the_usage_received_before_it():
    async def hang():
        await asyncio.sleep(3600)

    ctx = _Ctx(then=hang)
    with capture_usage() as captured:
        with pytest.raises(LLMCallTimeout):
            await sample_with_timeout(ctx, timeout=0.05)
    assert ctx.cancelled and captured.usage == (11, 7, "gpt-4o-mini")


async def test_an_external_cancellation_propagates_and_keeps_the_usage_received():
    async def hang():
        await asyncio.sleep(3600)

    ctx = _Ctx(then=hang)

    async def run():
        with capture_usage() as captured:
            try:
                await sample_with_timeout(ctx, timeout=30)
            finally:
                seen.append(captured)

    seen = []
    task = asyncio.create_task(run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ctx.cancelled and seen[0].usage == (11, 7, "gpt-4o-mini")  # annulation EXTERNE : propagée, usage conservé


async def test_a_deadline_without_any_usage_leaves_the_usage_absent_not_zero():
    with capture_usage() as captured:
        with pytest.raises(LLMCallTimeout):
            await sample_with_timeout(_Ctx(delay=1.0), timeout=0.05)
    assert captured.usage is None  # absence ≠ zéro : l'appelant ne peut pas conclure à une dépense nulle


# --- entrée publique : accounted_sample (planner / QA) ---------------------------------------------------------


def _settings(**extra):
    base = dict(
        LLM_PROVIDER="openai",
        LLM_MODEL="gpt-5.4",  # tarif de la grille : le chemin historique n'est pas refusé pour « tarif inconnu »
        LLM_CALL_TIMEOUT=2.0,
        MAX_COST_USD=1.0,
        MAX_TOKENS_BUDGET=100_000,
        BUDGET_EXHAUSTED_ACTION="pause",
        LLM_PRICE_PROMPT_PER_1M=1.0,
        LLM_PRICE_COMPLETION_PER_1M=2.0,
    )
    base.update(extra)
    return SimpleNamespace(**base)


async def _accounted(ctx, settings):
    collector = MetricsCollector(input_cost_per_token=0.0, output_cost_per_token=0.0)
    result = await accounted_sample(
        ctx, role="planner", operation="planner.spec", settings_obj=settings, collector=collector, messages="x"
    )
    return result, collector


@pytest.mark.parametrize("timeout", [0.0, 2.0])
async def test_accounted_sample_sees_the_usage_whatever_the_deadline_setting(timeout, tmp_path, monkeypatch):
    monkeypatch.setattr(MetricsCollector, "_PERSIST_DIR", tmp_path / "monitoring")
    result, collector = await _accounted(_Ctx(), _settings(LLM_CALL_TIMEOUT=timeout))
    assert result == "ok"  # pas d'UsageAccountingError sur un cycle sain
    assert collector._cumulative_totals()[1] == 18  # l'usage reçu (11 + 7) est débité une fois


async def test_accounted_sample_still_refuses_a_real_absence_of_usage_under_a_cap(tmp_path, monkeypatch):
    """Témoin : la vérification d'usage reste active ; une absence n'est jamais lue comme zéro."""
    monkeypatch.setattr(MetricsCollector, "_PERSIST_DIR", tmp_path / "monitoring")

    class _Silent:
        async def sample(self, **kwargs):
            return "ok"  # aucune ContextVar écrite : usage réellement absent

    for timeout in (0.0, 2.0):
        with pytest.raises(UsageAccountingError, match="Usage LLM absent"):
            await _accounted(_Silent(), _settings(LLM_CALL_TIMEOUT=timeout))
