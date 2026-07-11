"""Couche LLM unifiée de Collègue (rôles, résolution de modèle par rôle)."""

from collegue.core.llm.client import (
    UsageAccountingError,
    accounted_sample,
    model_preferences_for_role,
    resolved_model_for,
)
from collegue.core.llm.roles import LLMRole, resolve_role

__all__ = [
    "LLMRole",
    "UsageAccountingError",
    "accounted_sample",
    "resolve_role",
    "model_preferences_for_role",
    "resolved_model_for",
]
