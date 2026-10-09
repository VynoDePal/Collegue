"""Couche LLM unifiée de Collègue (rôles, destination effective par rôle : fournisseur, modèle, endpoint, clé)."""

from collegue.core.llm.client import (
    UsageAccountingError,
    accounted_sample,
    model_preferences_for_role,
    normalize_preferences,
    resolved_model_for,
)
from collegue.core.llm.roles import (
    LLMRole,
    LLMRoute,
    LLMRoutingError,
    parse_route_preferences,
    resolve_role,
    resolve_route,
    validate_role_routes,
)

__all__ = [
    "LLMRole",
    "LLMRoute",
    "LLMRoutingError",
    "UsageAccountingError",
    "accounted_sample",
    "normalize_preferences",
    "parse_route_preferences",
    "resolve_role",
    "resolve_route",
    "validate_role_routes",
    "model_preferences_for_role",
    "resolved_model_for",
]
