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

from typing import List, Optional

from collegue.core.llm.roles import LLMRole, resolve_role


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
