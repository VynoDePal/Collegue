"""Rôles LLM et résolution du couple (provider, modèle) par rôle.

Le brief « moteur de dev autonome » (§5) veut des modèles par rôle : un codeur
fort, un QA/triage économique, un planner, etc. Le multi-provider existe déjà
(``collegue/config.py``) ; ce module ajoute uniquement le mapping rôle→modèle,
configurable via ``.env`` (``LLM_MODEL_<ROLE>`` / ``LLM_PROVIDER_<ROLE>``) avec
fallback sur le couple global ``LLM_PROVIDER`` / ``LLM_MODEL``.

Purement additif : sans config par rôle, ``resolve_role`` renvoie le défaut
global, donc aucun comportement existant ne change.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, Tuple


class LLMRole(str, Enum):
    """Rôle fonctionnel d'un appel LLM, qui détermine le modèle utilisé."""

    CODER = "coder"
    QA = "qa"
    REVIEWER = "reviewer"
    PLANNER = "planner"
    DEFAULT = "default"


def resolve_role(role: LLMRole | str = LLMRole.DEFAULT, settings_obj: Optional[object] = None) -> Tuple[str, str]:
    """Retourne ``(provider, model)`` pour un rôle donné.

    Priorité : config dédiée au rôle (``LLM_PROVIDER_<ROLE>`` / ``LLM_MODEL_<ROLE>``)
    si présente, sinon fallback sur le couple global ``LLM_PROVIDER`` / ``LLM_MODEL``.
    Chaque dimension (provider, model) retombe indépendamment sur le défaut.

    Args:
        role: rôle (``LLMRole`` ou sa valeur str). Inconnu → traité comme DEFAULT.
        settings_obj: settings à interroger (défaut : le singleton ``settings``).
    """
    if settings_obj is None:
        from collegue.config import settings as settings_obj

    role_value = role.value if isinstance(role, LLMRole) else str(role).lower()

    default_provider = getattr(settings_obj, "LLM_PROVIDER", "gemini")
    default_model = getattr(settings_obj, "LLM_MODEL", "")

    if role_value == LLMRole.DEFAULT.value:
        return default_provider, default_model

    suffix = role_value.upper()
    provider = getattr(settings_obj, f"LLM_PROVIDER_{suffix}", None) or default_provider
    model = getattr(settings_obj, f"LLM_MODEL_{suffix}", None) or default_model
    return provider, model
