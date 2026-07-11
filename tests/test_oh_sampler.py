"""Extraction des métriques du sampler abonnement (#590)."""

from types import SimpleNamespace

from collegue.executor.oh_sampler import _usage_payload


def test_usage_payload_supports_sdk_object_and_marks_subscription_non_billable():
    llm = SimpleNamespace(
        metrics=SimpleNamespace(accumulated_token_usage=SimpleNamespace(input_tokens=21, output_tokens=8))
    )

    assert _usage_payload(llm, "gpt-5.4") == {
        "prompt_tokens": 21,
        "completion_tokens": 8,
        "model": "gpt-5.4",
        "billable": False,
    }


def test_usage_payload_missing_metrics_is_explicit_zero_not_fabricated():
    assert _usage_payload(SimpleNamespace(), "gpt-5.4")["prompt_tokens"] == 0
