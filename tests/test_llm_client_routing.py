"""Tests du routage LLM par rôle (C2, epic #334).

Couvre : model_preferences_for_role (résolution → préférence ctx.sample),
le câblage rétrocompatible de BaseTool.sample_llm (role optionnel), et le fait
que le handler honore un modèle arbitraire (sinon le routage serait un no-op).
"""

import pytest

from collegue.config import Settings
from collegue.core.llm import LLMRole, model_preferences_for_role, resolved_model_for

_ROLE_ENV_VARS = [
    "LLM_MODEL_CODER",
    "LLM_PROVIDER_CODER",
    "LLM_MODEL_QA",
    "LLM_PROVIDER_QA",
    "LLM_MODEL",
    "LLM_PROVIDER",
]


@pytest.fixture(autouse=True)
def _clean_role_env(monkeypatch):
    for var in _ROLE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


# --- model_preferences_for_role -------------------------------------------------


def test_preferences_for_role_with_dedicated_model():
    s = _settings(LLM_PROVIDER="gemini", LLM_MODEL="flash", LLM_MODEL_CODER="pro")
    assert model_preferences_for_role(LLMRole.CODER, s) == ["pro"]


def test_preferences_for_role_falls_back_to_global():
    s = _settings(LLM_PROVIDER="gemini", LLM_MODEL="flash")
    # Pas de modèle CODER dédié → préférence = modèle global.
    assert model_preferences_for_role(LLMRole.CODER, s) == ["flash"]


def test_preferences_none_when_no_model():
    s = _settings(LLM_PROVIDER="gemini", LLM_MODEL="")
    # Aucun modèle résolu → None (le handler garde son défaut).
    assert model_preferences_for_role(LLMRole.QA, s) is None


def test_resolved_model_for_role():
    s = _settings(LLM_PROVIDER="gemini", LLM_MODEL="flash", LLM_MODEL_QA="qa-model")
    assert resolved_model_for(LLMRole.QA, s) == "qa-model"


# --- handler honore un modèle arbitraire ---------------------------------------


def test_handler_honors_arbitrary_model():
    """Le VRAI handler doit retenir un modèle non-OpenAI (Gemini), sinon no-op.

    Teste la classe réelle (collegue.core.llm.sampling_handler), pas une copie,
    pour qu'une régression dans app.py/le module soit attrapée.
    """
    pytest.importorskip("fastmcp")
    pytest.importorskip("openai")
    from collegue.core.llm.sampling_handler import build_sampling_handler

    h = build_sampling_handler(default_model="gemini-3-flash-preview", api_key="x", base_url=None)
    assert h is not None
    # Un modèle Gemini arbitraire est honoré (le handler de base l'ignorerait).
    assert h._select_model_from_preferences(["gemini-3-pro-preview"]) == "gemini-3-pro-preview"
    # Sans préférence → modèle par défaut.
    assert h._select_model_from_preferences(None) == "gemini-3-flash-preview"
    # Première préférence non vide retenue.
    assert h._select_model_from_preferences(["", "real-model"]) == "real-model"


# --- rétrocompatibilité sample_llm ---------------------------------------------


@pytest.mark.asyncio
async def test_sample_llm_passes_model_preferences_for_role(monkeypatch):
    """Avec role, sample_llm passe model_preferences à ctx.sample ; sans role, non."""
    from collegue.tools.base import BaseTool

    captured = {}

    class _Result:
        text = "ok"
        result = None

    class _Ctx:
        async def sample(self, **kwargs):
            captured.clear()
            captured.update(kwargs)
            return _Result()

    # Sous-classe concrète minimale (BaseTool est abstraite). On neutralise la
    # comptabilité de tokens (hors sujet ici) pour tester le routage seul.
    class _Tool(BaseTool):
        def _execute_core_logic(self, request, **kwargs):
            return None

        def _record_llm_tokens(self, tokens, **kwargs):
            pass

    tool = _Tool.__new__(_Tool)
    tool.tool_name = "test_tool"
    tool._last_input_tokens = 0
    tool._last_output_tokens = 0

    import collegue.core.llm.client as client_mod

    # Résolution déterministe indépendante de l'env (restaurée par monkeypatch).
    monkeypatch.setattr(client_mod, "model_preferences_for_role", lambda role, settings_obj=None: ["coder-model"])

    ctx = _Ctx()
    await tool.sample_llm("prompt", ctx=ctx, role=LLMRole.CODER)
    assert captured.get("model_preferences") == ["coder-model"]

    await tool.sample_llm("prompt", ctx=ctx)  # sans role
    assert "model_preferences" not in captured


@pytest.mark.asyncio
async def test_agent_loop_routes_by_llm_role(monkeypatch):
    """agent_execute (vrai chemin chaud) passe model_preferences selon llm_role."""
    from collegue.tools.agent_loop import AgentLoopConfig, AgentLoopMixin

    captured = {}

    class _Result:
        text = "réponse"

    class _Ctx:
        async def sample(self, **kwargs):
            captured.clear()
            captured.update(kwargs)
            return _Result()

        async def info(self, *a, **k):
            pass

        async def report_progress(self, *a, **k):
            pass

    class _Agent(AgentLoopMixin):
        agent_config = AgentLoopConfig(max_iterations=1)
        llm_role = LLMRole.CODER

        async def validate_agent_output(self, output, context):
            return []

        async def assess_agent_quality(self, output, context):
            return 1.0

        async def build_agent_feedback(self, output, errors, quality, context):
            return ""

    import collegue.core.llm.client as client_mod

    monkeypatch.setattr(client_mod, "model_preferences_for_role", lambda role, settings_obj=None: ["coder-model"])

    agent = _Agent()
    await agent.agent_execute(initial_prompt="p", system_prompt="s", ctx=_Ctx())
    assert captured.get("model_preferences") == ["coder-model"]
