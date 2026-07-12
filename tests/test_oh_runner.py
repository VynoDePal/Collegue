"""Tests ciblés du runner OpenHands embarqué dans le sandbox."""

from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import pytest

from collegue.executor import oh_runner


def _llm(*, prompt: int, completion: int, cost: float) -> SimpleNamespace:
    usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)
    metrics = SimpleNamespace(accumulated_token_usage=usage, accumulated_cost=cost)
    return SimpleNamespace(metrics=metrics)


def _usage_payloads(output: str) -> list[dict[str, object]]:
    prefix = "[collegue-usage] "
    return [json.loads(line.removeprefix(prefix)) for line in output.splitlines() if line.startswith(prefix)]


def test_usage_emitter_emits_complete_cumulative_usage_as_deltas(capsys):
    llm = _llm(prompt=100, completion=20, cost=0.5)
    emitter = oh_runner._UsageDeltaEmitter(subscription=False)

    emitter.emit(llm)
    llm.metrics.accumulated_token_usage.prompt_tokens = 145
    llm.metrics.accumulated_token_usage.completion_tokens = 32
    llm.metrics.accumulated_cost = 0.8
    emitter.emit(llm)

    payloads = _usage_payloads(capsys.readouterr().out)
    assert [(p["prompt_tokens"], p["completion_tokens"]) for p in payloads] == [(100, 20), (45, 12)]
    assert sum(int(p["prompt_tokens"]) for p in payloads) == 145
    assert sum(int(p["completion_tokens"]) for p in payloads) == 32
    assert sum(float(p["cost_usd"]) for p in payloads) == pytest.approx(0.8)


def test_fallback_model_starts_with_fresh_usage_baseline(monkeypatch, capsys):
    created: list[object] = []

    class FakeLLM:
        def __init__(self, *, model, **_kwargs):
            self.model = model
            self.metrics = _llm(prompt=0, completion=0, cost=0.0).metrics
            created.append(self)

    class FakeConversation:
        def __init__(self, *, agent, **_kwargs):
            self.llm = agent

        def send_message(self, _task):
            return None

        def run(self):
            if self.llm.model == "gemini/gemma-4-31b-it":
                self.llm.metrics = _llm(prompt=100, completion=40, cost=1.25).metrics
                raise RuntimeError("primary unavailable")
            self.llm.metrics = _llm(prompt=20, completion=5, cost=0.2).metrics

    sdk = types.ModuleType("openhands.sdk")
    sdk.LLM = FakeLLM
    sdk.Conversation = FakeConversation
    default = types.ModuleType("openhands.tools.preset.default")
    default.get_default_agent = lambda *, llm, cli_mode: llm
    monkeypatch.setitem(sys.modules, "openhands.sdk", sdk)
    monkeypatch.setitem(sys.modules, "openhands.tools.preset.default", default)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "gemini/gemma-4-31b-it")
    monkeypatch.setenv("OH_FALLBACK_MODELS", "gemini/gemma-4-26b-a4b-it")
    monkeypatch.delenv("LLM_SUBSCRIPTION", raising=False)
    monkeypatch.setattr(sys, "argv", ["oh_runner", "--task", "implement the issue"])

    assert oh_runner.main() == 0

    assert [llm.model for llm in created] == ["gemini/gemma-4-31b-it", "gemini/gemma-4-26b-a4b-it"]
    payloads = _usage_payloads(capsys.readouterr().out)
    assert [(p["prompt_tokens"], p["completion_tokens"]) for p in payloads] == [(100, 40), (20, 5)]
    assert sum(int(p["prompt_tokens"]) for p in payloads) == 120
    assert sum(int(p["completion_tokens"]) for p in payloads) == 45
    assert sum(float(p["cost_usd"]) for p in payloads) == pytest.approx(1.45)
