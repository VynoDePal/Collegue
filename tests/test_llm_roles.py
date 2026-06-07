"""Tests de la résolution de modèle/provider par rôle (C1, epic #334)."""

from collegue.config import Settings
from collegue.core.llm import LLMRole, resolve_role


def test_default_role_uses_global_config():
    s = Settings(LLM_PROVIDER="gemini", LLM_MODEL="gemini-3-flash-preview")
    assert resolve_role(LLMRole.DEFAULT, s) == ("gemini", "gemini-3-flash-preview")


def test_role_without_dedicated_config_falls_back():
    # Aucun LLM_MODEL_CODER défini → fallback sur le couple global.
    s = Settings(LLM_PROVIDER="gemini", LLM_MODEL="gemini-3-flash-preview")
    assert resolve_role(LLMRole.CODER, s) == ("gemini", "gemini-3-flash-preview")


def test_role_with_dedicated_model():
    s = Settings(
        LLM_PROVIDER="gemini",
        LLM_MODEL="gemini-3-flash-preview",
        LLM_MODEL_CODER="gemini-3-pro-preview",
    )
    # Le modèle du rôle prime, le provider retombe sur le global.
    assert resolve_role(LLMRole.CODER, s) == ("gemini", "gemini-3-pro-preview")


def test_role_with_dedicated_provider_and_model():
    s = Settings(
        LLM_PROVIDER="gemini",
        LLM_MODEL="gemini-3-flash-preview",
        LLM_PROVIDER_QA="lmstudio",
        LLM_MODEL_QA="qwen2.5-coder",
    )
    assert resolve_role(LLMRole.QA, s) == ("lmstudio", "qwen2.5-coder")


def test_independent_fallback_per_dimension():
    # Provider du rôle défini mais pas le modèle → modèle retombe sur le global.
    s = Settings(
        LLM_PROVIDER="gemini",
        LLM_MODEL="gemini-3-flash-preview",
        LLM_PROVIDER_PLANNER="openai",
    )
    assert resolve_role(LLMRole.PLANNER, s) == ("openai", "gemini-3-flash-preview")


def test_role_accepts_string_value():
    s = Settings(LLM_PROVIDER="gemini", LLM_MODEL="m", LLM_MODEL_REVIEWER="rev-model")
    assert resolve_role("reviewer", s) == ("gemini", "rev-model")


def test_unknown_role_treated_as_default():
    s = Settings(LLM_PROVIDER="gemini", LLM_MODEL="m")
    assert resolve_role("inconnu", s) == ("gemini", "m")
