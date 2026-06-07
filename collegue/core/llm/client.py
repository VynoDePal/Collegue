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
from typing import Any, List, Optional

from collegue.core.llm.roles import LLMRole, resolve_role


class LLMCallTimeout(Exception):
    """Un appel LLM individuel (``ctx.sample``) a dépassé ``LLM_CALL_TIMEOUT``.

    Exception « normale » (hérite d'``Exception``, contrairement à
    ``BudgetExceeded``) : un appel pendu est un échec *récupérable* — la boucle
    appelante l'enregistre et continue/s'arrête proprement, sans hang.
    """


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
