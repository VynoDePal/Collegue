"""Tests C3 (#337) : tarifs multi-provider + capture du modèle utilisé."""

from collegue.monitoring.pricing import cost_per_token, has_explicit_pricing, is_explicitly_free
from collegue.monitoring.sampling_usage import record_usage, take_usage

_PER_1M = 1_000_000.0


# --- tarifs multi-provider ------------------------------------------------------


def test_openai_pricing():
    cin, cout = cost_per_token("gpt-5.4")
    assert abs(cin - 2.50 / _PER_1M) < 1e-12
    assert abs(cout - 15.00 / _PER_1M) < 1e-12


def test_openai_mini_pricing():
    cin, cout = cost_per_token("gpt-5.4-mini")
    assert abs(cin - 0.75 / _PER_1M) < 1e-12
    assert abs(cout - 4.50 / _PER_1M) < 1e-12


def test_anthropic_pricing_prefix_match():
    # Préfixe : claude-opus-4-8 → claude-opus-4.
    cin, cout = cost_per_token("claude-opus-4-8")
    assert abs(cin - 5.00 / _PER_1M) < 1e-12
    assert abs(cout - 25.00 / _PER_1M) < 1e-12


def test_mini_with_date_suffix_not_shadowed_by_base():
    # Régression F1 : gpt-5.4-mini-<date> ne doit PAS être facturé au tarif
    # gpt-5.4 (la clé la plus spécifique gagne).
    cin, cout = cost_per_token("gpt-5.4-mini-2026-01")
    assert abs(cin - 0.75 / _PER_1M) < 1e-12
    assert abs(cout - 4.50 / _PER_1M) < 1e-12


def test_base_model_with_date_suffix_matches_base():
    cin, cout = cost_per_token("gpt-5.4-2026-01")
    assert abs(cin - 2.50 / _PER_1M) < 1e-12


def test_prefix_requires_separator_boundary():
    # "gpt-5.4x" ne doit pas matcher "gpt-5.4" (pas de frontière) → défaut.
    cin, cout = cost_per_token("gpt-5.4xyz")
    assert abs(cin - 0.15 / _PER_1M) < 1e-12


def test_gemini_still_priced():
    cin, cout = cost_per_token("gemini-3.5-flash")
    assert abs(cin - 1.50 / _PER_1M) < 1e-12


def test_gemma_4_gemini_api_models_are_explicitly_free():
    for model in ("gemma-4-31b-it", "gemma-4-26b-a4b-it"):
        assert cost_per_token(model, provider="gemini") == (0.0, 0.0)
        assert has_explicit_pricing(model, provider="gemini") is True
        assert is_explicitly_free(model, provider="gemini") is True


def test_unknown_remote_model_is_never_explicitly_free():
    for model in ("gemma-4-inconnu", "gemma-4-31b-it-inconnu"):
        assert has_explicit_pricing(model, provider="gemini") is False
        assert is_explicitly_free(model, provider="gemini") is False
        assert cost_per_token(model, provider="gemini") != (0.0, 0.0)


def test_gemma_4_free_price_is_scoped_to_gemini_provider():
    assert has_explicit_pricing("gemma-4-31b-it", provider="openai") is False
    assert is_explicitly_free("gemma-4-31b-it", provider="openai") is False
    assert cost_per_token("gemma-4-31b-it", provider="openai") != (0.0, 0.0)


def test_local_provider_is_free():
    for provider in ("lmstudio", "ollama", "unsloth"):
        assert cost_per_token("any-model", provider=provider) == (0.0, 0.0)


def test_local_provider_case_insensitive():
    assert cost_per_token("x", provider="LMStudio") == (0.0, 0.0)


def test_unknown_model_falls_back():
    cin, cout = cost_per_token("modele-inexistant-xyz")
    assert abs(cin - 0.15 / _PER_1M) < 1e-12
    assert abs(cout - 0.60 / _PER_1M) < 1e-12


def test_remote_provider_does_not_zero_cost():
    # Un provider non local n'annule pas le coût.
    cin, _ = cost_per_token("gpt-5.4", provider="openai")
    assert cin > 0


def test_explicit_pricing_distinguishes_authoritative_from_fallback():
    assert has_explicit_pricing("gpt-5.4-2026-01", provider="openai") is True
    assert has_explicit_pricing("unknown-model", provider="openai") is False
    assert has_explicit_pricing("anything", provider="ollama") is True


# --- capture du modèle dans l'usage --------------------------------------------


def test_usage_captures_model():
    take_usage()  # purge
    record_usage(100, 50, "gemini-3.5-flash")
    usage = take_usage()
    assert usage == (100, 50, "gemini-3.5-flash")


def test_usage_accumulates_tokens_keeps_model():
    take_usage()  # purge
    record_usage(10, 5, "gpt-5.4")
    record_usage(20, 10, "")  # 2e appel sans modèle → garde le précédent
    assert take_usage() == (30, 15, "gpt-5.4")


def test_take_usage_resets():
    take_usage()
    record_usage(1, 1, "m")
    assert take_usage() is not None
    assert take_usage() is None


def test_usage_rejects_negative_provider_counters():
    import pytest

    with pytest.raises(ValueError, match="négatifs"):
        record_usage(-1, 0, "m")


def test_mixed_model_loop_keeps_last_model_sums_tokens():
    # Comportement documenté : dans une boucle multi-modèles, les tokens sont
    # sommés et le dernier modèle non vide est conservé (l'usage alimente
    # l'activity_log, pas le coût par-modèle).
    take_usage()
    record_usage(10, 5, "model-a")
    record_usage(20, 10, "model-b")
    assert take_usage() == (30, 15, "model-b")
