"""Tests de la résolution de modèle/provider par rôle (C1, epic #334).

Isolation : on construit les Settings avec ``_env_file=None`` et on purge les
variables d'environnement par rôle, pour que chaque test contrôle entièrement
ses entrées (sinon un ``LLM_MODEL_CODER`` ambiant ferait passer/échouer les
tests de fallback à tort).
"""

import pytest

from collegue.config import Settings
from collegue.core.llm import LLMRole, resolve_role

_ROLE_ENV_VARS = [
    "LLM_MODEL_CODER",
    "LLM_PROVIDER_CODER",
    "LLM_MODEL_QA",
    "LLM_PROVIDER_QA",
    "LLM_MODEL_REVIEWER",
    "LLM_PROVIDER_REVIEWER",
    "LLM_MODEL_PLANNER",
    "LLM_PROVIDER_PLANNER",
    "LLM_MODEL",
    "LLM_PROVIDER",
]


@pytest.fixture(autouse=True)
def _clean_role_env(monkeypatch):
    """Purge l'environnement LLM par rôle pour des tests déterministes."""
    for var in _ROLE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _settings(**kwargs) -> Settings:
    # _env_file=None : ne pas charger le .env du repo pendant les tests.
    return Settings(_env_file=None, **kwargs)


def test_default_role_uses_global_config():
    s = _settings(LLM_PROVIDER="gemini", LLM_MODEL="gemini-3-flash-preview")
    assert resolve_role(LLMRole.DEFAULT, s) == ("gemini", "gemini-3-flash-preview")


def test_role_without_dedicated_config_falls_back():
    # Aucun LLM_MODEL_CODER défini → fallback sur le couple global.
    s = _settings(LLM_PROVIDER="gemini", LLM_MODEL="gemini-3-flash-preview")
    assert resolve_role(LLMRole.CODER, s) == ("gemini", "gemini-3-flash-preview")


def test_role_with_dedicated_model():
    s = _settings(
        LLM_PROVIDER="gemini",
        LLM_MODEL="gemini-3-flash-preview",
        LLM_MODEL_CODER="gemini-3-pro-preview",
    )
    # Le modèle du rôle prime, le provider retombe sur le global.
    assert resolve_role(LLMRole.CODER, s) == ("gemini", "gemini-3-pro-preview")


def test_role_with_dedicated_provider_and_model():
    s = _settings(
        LLM_PROVIDER="gemini",
        LLM_MODEL="gemini-3-flash-preview",
        LLM_PROVIDER_QA="lmstudio",
        LLM_MODEL_QA="qwen2.5-coder",
    )
    assert resolve_role(LLMRole.QA, s) == ("lmstudio", "qwen2.5-coder")


def test_independent_fallback_per_dimension():
    # Provider du rôle défini mais pas le modèle → modèle retombe sur le global.
    s = _settings(
        LLM_PROVIDER="gemini",
        LLM_MODEL="gemini-3-flash-preview",
        LLM_PROVIDER_PLANNER="openai",
    )
    assert resolve_role(LLMRole.PLANNER, s) == ("openai", "gemini-3-flash-preview")


def test_role_accepts_string_value():
    s = _settings(LLM_PROVIDER="gemini", LLM_MODEL="m", LLM_MODEL_REVIEWER="rev-model")
    assert resolve_role("reviewer", s) == ("gemini", "rev-model")


def test_unknown_role_treated_as_default():
    s = _settings(LLM_PROVIDER="gemini", LLM_MODEL="m")
    assert resolve_role("inconnu", s) == ("gemini", "m")


def test_empty_string_override_falls_back_to_global():
    # Une valeur vide est traitée comme « non défini » → fallback global.
    s = _settings(LLM_PROVIDER="gemini", LLM_MODEL="m", LLM_MODEL_CODER="")
    assert resolve_role(LLMRole.CODER, s) == ("gemini", "m")
