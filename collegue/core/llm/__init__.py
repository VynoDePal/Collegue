"""Couche LLM unifiée de Collègue (rôles, résolution de modèle par rôle)."""

from collegue.core.llm.roles import LLMRole, resolve_role

__all__ = ["LLMRole", "resolve_role"]
