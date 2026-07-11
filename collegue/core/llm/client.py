"""Routage des appels LLM par rôle, au-dessus du sampling FastMCP.

Le trafic LLM passe par ``ctx.sample()`` (sampling côté serveur FastMCP), dont
le modèle est choisi par le handler global. Ce module traduit un rôle
(:class:`~collegue.core.llm.roles.LLMRole`) en ``model_preferences`` à passer à
``ctx.sample()``, en s'appuyant sur la résolution par rôle de C1.

Important : pour que la préférence soit honorée, le handler de sampling doit
accepter un nom de modèle arbitraire (voir ``collegue.core.llm.sampling_handler``).
Le handler ``OpenAISamplingHandler`` de base ne retient que les modèles OpenAI
connus et ignorerait silencieusement un modèle Gemini.

Limite connue (C2) : seul le **modèle** est routé par rôle, pas le **provider**.
Le handler de sampling est construit une fois au démarrage avec un unique
``base_url``/client (provider global). Un ``LLM_PROVIDER_<ROLE>`` différent du
provider global est donc résolu mais non appliqué ici — le routage multi-provider
nécessiterait plusieurs clients et relève d'une évolution ultérieure.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any, List, Optional

from collegue.core.llm.roles import LLMRole, resolve_role


class LLMCallTimeout(Exception):
    """Un appel LLM individuel (``ctx.sample``) a dépassé ``LLM_CALL_TIMEOUT``.

    Exception « normale » (hérite d'``Exception``, contrairement à
    ``BudgetExceeded``) : un appel pendu est un échec *récupérable* — la boucle
    appelante l'enregistre et continue/s'arrête proprement, sans hang.
    """


class UsageAccountingError(RuntimeError):
    """Un plafond dur exige une preuve d'usage que le provider n'a pas fournie."""


def resolved_model_for(role: LLMRole | str = LLMRole.DEFAULT, settings_obj: Optional[object] = None) -> str:
    """Modèle effectif pour un rôle (chaîne vide si aucun configuré)."""
    _provider, model = resolve_role(role, settings_obj)
    return model


def model_preferences_for_role(
    role: LLMRole | str = LLMRole.DEFAULT, settings_obj: Optional[object] = None
) -> Optional[List[str]]:
    """``model_preferences`` à passer à ``ctx.sample()`` pour un rôle.

    Retourne ``[model]`` si un modèle est résolu, sinon ``None`` (le handler
    utilisera alors son modèle par défaut — comportement actuel inchangé).
    """
    model = resolved_model_for(role, settings_obj)
    return [model] if model else None


async def sample_with_timeout(
    ctx: Any,
    *,
    timeout: Optional[float] = None,
    settings_obj: Optional[object] = None,
    **sample_kwargs: Any,
) -> Any:
    """Appelle ``ctx.sample(**sample_kwargs)`` avec un timeout par appel.

    Le ``timeout`` (secondes) est résolu depuis ``settings.LLM_CALL_TIMEOUT`` s'il
    n'est pas fourni. ``<= 0`` / ``None`` → aucun timeout (comportement inchangé).
    En cas de dépassement, la coroutine sous-jacente est **annulée proprement**
    (``asyncio.wait_for`` propage ``CancelledError`` dans ``ctx.sample``) et on lève
    :class:`LLMCallTimeout` — l'appelant gère, pas de hang.
    """
    if timeout is None:
        try:
            from collegue.config import settings as _settings

            timeout = getattr(settings_obj or _settings, "LLM_CALL_TIMEOUT", 0.0)
        except Exception:
            timeout = 0.0

    # `not timeout or timeout <= 0` ne suffit pas : NaN passe les deux tests
    # (not nan == False, nan <= 0 == False) et ferait planter asyncio.wait_for
    # (ValueError dans la loop, non converti). On exige donc une valeur finie > 0.
    if not timeout or not math.isfinite(timeout) or timeout <= 0:
        return await ctx.sample(**sample_kwargs)

    # NB : si la pile de sampling avale CancelledError sans la relancer, wait_for
    # ne lèvera pas TimeoutError (limite connue d'asyncio.wait_for) — le timeout
    # est alors un no-op. ctx.sample (httpx async) relaie l'annulation normalement.
    try:
        return await asyncio.wait_for(ctx.sample(**sample_kwargs), timeout)
    except asyncio.TimeoutError as exc:
        raise LLMCallTimeout(f"Appel LLM interrompu après {timeout:g}s (LLM_CALL_TIMEOUT)") from exc


async def accounted_sample(
    ctx: Any,
    *,
    role: LLMRole | str,
    operation: str,
    settings_obj: Optional[object] = None,
    collector: Any = None,
    **sample_kwargs: Any,
) -> Any:
    """Échantillonne puis débite immédiatement tokens/coût pour planner et QA."""
    if settings_obj is None:
        from collegue.config import settings as settings_obj

    from collegue.monitoring.metrics import enforce_budget, get_metrics_collector
    from collegue.monitoring.pricing import cost_per_token, has_explicit_pricing
    from collegue.monitoring.sampling_usage import capture_usage

    collector = collector or get_metrics_collector()
    enforce_budget(collector=collector, settings_obj=settings_obj)
    provider, requested_model = resolve_role(role, settings_obj)
    subscription_requested = bool(getattr(settings_obj, "CODER_SUBSCRIPTION", False)) and not str(
        requested_model
    ).lower().startswith(("gemma", "gemini"))
    if (
        float(getattr(settings_obj, "MAX_COST_USD", 0) or 0) > 0
        and not subscription_requested
        and not has_explicit_pricing(requested_model, provider=provider)
    ):
        raise UsageAccountingError(
            f"Tarif inconnu pour {provider}/{requested_model} : plafond MAX_COST_USD non garantissable."
        )
    started = time.monotonic()
    succeeded = False
    result = None
    error: Optional[BaseException] = None
    error_traceback = None
    with capture_usage() as captured:
        try:
            result = await sample_with_timeout(ctx, settings_obj=settings_obj, **sample_kwargs)
            succeeded = True
        except BaseException as exc:  # BudgetExceeded doit aussi traverser ce point de débit
            error = exc
            error_traceback = exc.__traceback__

    usage = captured.usage
    if usage is not None:
        prompt_tokens, completion_tokens, actual_model = usage
        subscription = bool(getattr(settings_obj, "CODER_SUBSCRIPTION", False)) and not str(
            actual_model or requested_model
        ).lower().startswith(("gemma", "gemini"))
        input_price, output_price = cost_per_token(actual_model or requested_model, provider=provider)
        cost = 0.0 if subscription else prompt_tokens * input_price + completion_tokens * output_price
        collector.record_execution(
            expert_name=operation,
            duration_ms=(time.monotonic() - started) * 1000,
            success=succeeded,
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            metadata={"role": str(getattr(role, "value", role)), "model": actual_model or requested_model},
            cost_usd=cost,
        )
    if error is not None:
        raise error.with_traceback(error_traceback)

    max_tokens = int(getattr(settings_obj, "MAX_TOKENS_BUDGET", 0) or 0)
    max_cost = float(getattr(settings_obj, "MAX_COST_USD", 0) or 0)
    subscription = bool(getattr(settings_obj, "CODER_SUBSCRIPTION", False)) and not str(
        requested_model
    ).lower().startswith(("gemma", "gemini"))
    usage_proven = usage is not None and int(usage[0]) + int(usage[1]) > 0
    if succeeded and not usage_proven and (max_tokens > 0 or (max_cost > 0 and not subscription)):
        raise UsageAccountingError(
            f"Usage LLM absent pour {operation} : impossible de garantir le plafond dur configuré."
        )
    enforce_budget(collector=collector, settings_obj=settings_obj)
    return result
