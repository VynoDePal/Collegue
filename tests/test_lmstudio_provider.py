"""Tests de la configuration du provider local LM Studio (compatible OpenAI)."""

from collegue.config import Settings
from collegue.monitoring.metrics import MetricsCollector


def test_lmstudio_is_local_provider():
    s = Settings(LLM_PROVIDER="lmstudio", LLM_API_KEY=None)
    assert s.is_local_provider is True
    # Provider distant : non local.
    assert Settings(LLM_PROVIDER="gemini").is_local_provider is False


def test_lmstudio_default_base_url():
    s = Settings(LLM_PROVIDER="lmstudio")
    assert s.llm_base_url == "http://localhost:1234/v1"


def test_explicit_base_url_overrides_default():
    s = Settings(LLM_PROVIDER="lmstudio", LLM_BASE_URL="http://host.docker.internal:1234/v1")
    assert s.llm_base_url == "http://host.docker.internal:1234/v1"


def test_remote_provider_has_no_default_base_url():
    assert Settings(LLM_PROVIDER="gemini").llm_base_url is None
    # Sauf si l'utilisateur en fournit une explicitement.
    assert Settings(LLM_PROVIDER="openai", LLM_BASE_URL="https://x/v1").llm_base_url == "https://x/v1"


def test_local_provider_pricing_resolves_zero():
    # Un provider local résout un tarif nul (lu depuis settings).
    s = Settings(LLM_PROVIDER="lmstudio")
    assert s.is_local_provider is True


def test_zero_cost_recorded():
    collector = MetricsCollector(input_cost_per_token=0.0, output_cost_per_token=0.0)
    collector.reset()
    collector.record_execution(
        expert_name="local",
        duration_ms=10.0,
        success=True,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert collector.get_expert_metrics("local")["total_cost_usd"] == 0.0
