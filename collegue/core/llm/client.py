"""Routage des appels LLM par rôle, au-dessus du sampling FastMCP.

Le trafic LLM passe par ``ctx.sample()`` (sampling côté serveur FastMCP ou ``ctx`` offline). Ce module traduit un rôle
(:class:`~collegue.core.llm.roles.LLMRole`) en ``model_preferences`` à passer à ``ctx.sample()``. Depuis la vague 4 ces
préférences portent le MODÈLE canonique ET un hint de route (``collegue-route:<rôle>``) : le rôle voyage dans les noms de
hints, donc survit à la sérialisation MCP jusqu'au transport, qui résout alors la destination complète (fournisseur,
endpoint, clé) du rôle avec :func:`~collegue.core.llm.roles.resolve_route`. Un modèle seul ne suffisait pas : deux rôles
portant le même nom de modèle mais des clés/endpoints distincts étaient indiscernables.

Portée exacte : le routage complet vaut pour les transports DE COLLÈGUE (``LocalSamplingContext``, handler de sampling
serveur, worker OpenHands). Si le sampling est délégué à un client MCP externe (qui annonce la capacité de sampling), ce
client choisit seul sa destination et ses identifiants : il ne reçoit que le nom canonique du modèle (premier hint) et le
hint de route, qu'il est libre d'ignorer. Collègue ne contrôle ni ne budgétise ce transport.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any, List, Optional

from collegue.core.llm.roles import (
    LLMRole,
    LLMRoutingError,
    parse_route_preferences,
    resolve_role,
    resolve_route,
    route_hint,
)


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
    """``model_preferences`` à passer à ``ctx.sample()`` pour un rôle : ``[modèle canonique, hint de route]``.

    Retourne ``None`` si aucun modèle n'est résolu (le transport utilisera alors le rôle par défaut). Lève
    :class:`~collegue.core.llm.roles.LLMRoutingError` si la configuration du rôle se contredit — l'appelant ne doit
    JAMAIS l'avaler pour retomber sur la destination par défaut.
    """
    model = resolved_model_for(role, settings_obj)
    return [model, route_hint(role)] if model else None


def normalize_preferences(
    role: LLMRole | str, settings_obj: Optional[object], model_preferences: Any = None
) -> Optional[List[str]]:
    """Préférences du rôle, vérifiées contre celles de l'appelant (qui ne peuvent pas changer la destination).

    Les préférences d'un appelant sont acceptées si elles portent le même rôle (ou aucun) et le même modèle que la
    route du rôle ; sinon :class:`LLMRoutingError` (une préférence qui changerait le modèle sans changer client,
    endpoint ni clé est exactement le défaut corrigé).
    """
    expected = model_preferences_for_role(role, settings_obj)
    if model_preferences is None:
        return expected
    carried_role, models = parse_route_preferences(model_preferences)
    role_value = role.value if isinstance(role, LLMRole) else str(role).lower()
    if carried_role is not None and carried_role != role_value:
        raise LLMRoutingError(
            f"préférences de modèle du rôle {carried_role!r} transmises pour le rôle {role_value!r} : refusé"
        )
    if models and expected and models[0] != expected[0]:
        raise LLMRoutingError(
            f"préférence de modèle {models[0]!r} contradictoire avec le modèle du rôle {role_value!r} "
            f"({expected[0]!r}) : une préférence ne peut pas changer le modèle sans changer fournisseur, endpoint et clé"
        )
    return expected


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
    (``asyncio.timeout`` annule la tâche courante : ``CancelledError`` est propagé dans ``ctx.sample``) et on lève
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

    # ``asyncio.timeout`` (et NON ``asyncio.wait_for``) : ``wait_for`` exécute la coroutine dans une NOUVELLE tâche
    # sous Python 3.11 (contexte copié), si bien que l'usage écrit par ``ctx.sample`` dans la ContextVar de
    # ``monitoring.sampling_usage`` y reste enfermé et que l'appelant croit l'usage absent — alors que 3.12 exécute
    # ``wait_for`` dans la tâche appelante. ``asyncio.timeout`` s'exécute dans la tâche de l'appelant sous 3.11 comme
    # sous 3.12 : l'usage reçu est visible de la capture, une erreur ou une annulation externe survenant APRÈS
    # réception de l'usage le laisse lisible, et le délai annule toujours l'appel (``CancelledError`` converti en
    # ``TimeoutError`` à la sortie du bloc).
    # NB : si la pile de sampling avale CancelledError sans la relancer, le délai est un no-op (limite connue d'asyncio).
    # ctx.sample (httpx async) relaie l'annulation normalement.
    try:
        async with asyncio.timeout(timeout):
            return await ctx.sample(**sample_kwargs)
    except TimeoutError as exc:  # == asyncio.TimeoutError depuis Python 3.11
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

    from collegue.core.llm.budget_guard import budget_role, current_binding
    from collegue.monitoring.metrics import enforce_budget, get_metrics_collector
    from collegue.monitoring.pricing import cost_per_token, has_explicit_pricing
    from collegue.monitoring.sampling_usage import capture_usage

    collector = collector or get_metrics_collector()
    # Registre durable lié : c'est LUI qui borne (réservation avant chaque appel, au transport). Le
    # MetricsCollector ne sert plus qu'aux statistiques — jamais à une décision de budget (pas de
    # double comptage ni de garde en mémoire qui repart de zéro).
    ledger_bound = current_binding() is not None
    if not ledger_bound:
        enforce_budget(collector=collector, settings_obj=settings_obj)
    # Destination effective du rôle (cohérence fournisseur/modèle/endpoint) AVANT toute émission : le transport qui
    # émet exige en plus la clé. La tarification et la réservation utilisent CETTE route, jamais le fournisseur global.
    route = resolve_route(role, settings_obj, require_credential=False)
    provider, requested_model = route.provider, route.model
    subscription_requested = route.uses_subscription
    prefs = normalize_preferences(role, settings_obj, sample_kwargs.get("model_preferences"))
    if prefs:
        sample_kwargs["model_preferences"] = prefs
    if (
        not ledger_bound
        and float(getattr(settings_obj, "MAX_COST_USD", 0) or 0) > 0
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
    with capture_usage() as captured, budget_role(role):
        try:
            result = await sample_with_timeout(ctx, settings_obj=settings_obj, **sample_kwargs)
            succeeded = True
        except BaseException as exc:  # BudgetExceeded doit aussi traverser ce point de débit
            error = exc
            error_traceback = exc.__traceback__

    usage = captured.usage
    if usage is not None:
        prompt_tokens, completion_tokens, actual_model = usage
        subscription = subscription_requested
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
    subscription = subscription_requested
    usage_proven = usage is not None and int(usage[0]) + int(usage[1]) > 0
    if succeeded and not usage_proven and (max_tokens > 0 or (max_cost > 0 and not subscription)):
        raise UsageAccountingError(
            f"Usage LLM absent pour {operation} : impossible de garantir le plafond dur configuré."
        )
    if not ledger_bound:
        enforce_budget(collector=collector, settings_obj=settings_obj)
    return result
