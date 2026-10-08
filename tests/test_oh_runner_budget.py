"""Garde budgétaire DU RUNNER (vague 2) : contrôle de chaque appel AVANT émission, dans le conteneur.

Le runner est un script autonome (stdlib seule) copié dans l'image : ces tests le lancent via ``main()`` avec un
faux SDK OpenHands qui enregistre les appels RÉELLEMENT émis. Aucun réseau, aucune clé réelle.
"""

from __future__ import annotations

import sys
import time
import types
from types import SimpleNamespace

import pytest

from collegue.executor import oh_runner


def _metrics(prompt=0, completion=0, cost=0.0):
    return SimpleNamespace(
        accumulated_token_usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
        accumulated_cost=cost,
    )


class _StatusError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


class _Harness:
    """Faux SDK : chaque ``LLM`` a un ``completion`` scripté ; la conversation appelle ``completion`` N fois."""

    def __init__(self, monkeypatch, *, scripts, calls_per_conversation=1, with_completion=True):
        self.created: list = []
        self.emitted: list = []
        outer = self

        class FakeLLM:
            def __init__(self, *, model, **kwargs):
                self.model = model
                self.kwargs = kwargs
                self.metrics = _metrics()
                self.max_output_tokens = 1000
                outer.created.append(self)
                self._script = list(scripts.get(model, []))
                if with_completion:
                    self.completion = self._completion

            def _completion(self, *args, **kwargs):
                outer.emitted.append((self.model, args, kwargs))
                step = self._script.pop(0) if self._script else (100, 50, 0.01)
                if isinstance(step, BaseException):
                    raise step
                prompt, completion, cost = step
                usage = self.metrics.accumulated_token_usage
                usage.prompt_tokens += prompt
                usage.completion_tokens += completion
                self.metrics.accumulated_cost += cost
                return "ok"

        class FakeConversation:
            def __init__(self, *, agent, **_):
                self.llm = agent

            def send_message(self, _task):
                return None

            def run(self):
                for _ in range(calls_per_conversation):
                    self.llm.completion("x" * 20)

        sdk = types.ModuleType("openhands.sdk")
        sdk.LLM = FakeLLM
        sdk.Conversation = FakeConversation
        default = types.ModuleType("openhands.tools.preset.default")
        default.get_default_agent = lambda *, llm, cli_mode: llm
        monkeypatch.setitem(sys.modules, "openhands.sdk", sdk)
        monkeypatch.setitem(sys.modules, "openhands.tools.preset.default", default)
        monkeypatch.setenv("LLM_API_KEY", "test-key")
        monkeypatch.setenv("LLM_MODEL", "gemini/primary")
        monkeypatch.setenv("OH_FALLBACK_MODELS", "gemini/fallback")
        monkeypatch.setenv("OH_NUM_RETRIES", "2")
        monkeypatch.delenv("LLM_SUBSCRIPTION", raising=False)
        monkeypatch.setattr(oh_runner.signal, "signal", lambda *a, **k: None)

    def run(self, monkeypatch, *args):
        monkeypatch.setattr(sys, "argv", ["oh_runner", "--task", "t", *args])
        return oh_runner.main()


def test_without_allocation_the_runner_is_unchanged(monkeypatch, capsys):
    harness = _Harness(monkeypatch, scripts={}, calls_per_conversation=1)
    assert harness.run(monkeypatch) == 0
    out = capsys.readouterr().out
    assert "[collegue-budget]" not in out  # aucune garde, aucun marqueur
    assert harness.created[0].kwargs["num_retries"] == 2  # retries internes historiques conservés


def test_with_an_allocation_the_runner_arms_then_finalizes_and_disables_sdk_retries(monkeypatch, capsys):
    harness = _Harness(monkeypatch, scripts={}, calls_per_conversation=1)
    assert harness.run(monkeypatch, "--budget-tokens", "100000", "--deadline-epoch", str(time.time() + 600)) == 0
    out = capsys.readouterr().out
    assert out.index("[collegue-budget] armed") < out.index("OH_RUNNER_DONE") < out.index("[collegue-budget] final")
    assert harness.created[0].kwargs["num_retries"] == 0  # les retries passent par la garde


def test_a_call_that_does_not_fit_the_token_allocation_is_never_emitted(monkeypatch, capsys):
    harness = _Harness(monkeypatch, scripts={"gemini/primary": [(500, 400, 0.0)]}, calls_per_conversation=3)
    # 1 appel passe (≈ 72 + 1000 de sortie max réservés) ; le suivant dépasserait 1 600 tokens.
    code = harness.run(monkeypatch, "--budget-tokens", "1600")
    assert code == 4
    assert len(harness.emitted) == 1  # le 2ᵉ appel n'est PAS parti
    assert "allocation budgétaire atteinte" in capsys.readouterr().err
    assert len(harness.created) == 1  # et le repli de modèle n'agrandit pas l'allocation


def test_a_usd_allocation_is_enforced_with_authoritative_prices(monkeypatch):
    harness = _Harness(monkeypatch, scripts={"gemini/primary": [(1000, 500, 0.0)]}, calls_per_conversation=3)
    code = harness.run(
        monkeypatch,
        "--budget-usd",
        "0.012",
        "--price-in",
        "0.0000015",
        "--price-out",
        "0.000009",
    )
    # 1ʳᵉ réservation ≈ 0,0091 $ ≤ 0,012 → émis ; après 1 appel réel (0,0060 $) le suivant ferait ≈ 0,0151 $
    # > 0,012 $ → refusé AVANT émission.
    assert code == 4 and len(harness.emitted) == 1


def test_retries_are_controlled_before_each_emission_and_use_the_remaining_allocation(monkeypatch):
    harness = _Harness(
        monkeypatch,
        scripts={"gemini/primary": [_StatusError(503), _StatusError(429), (10, 5, 0.0)]},
        calls_per_conversation=1,
    )
    sleeps = []
    monkeypatch.setattr(oh_runner.time, "sleep", lambda s: sleeps.append(s))
    assert harness.run(monkeypatch, "--budget-tokens", "100000") == 0
    assert len(harness.emitted) == 3 and len(sleeps) == 2  # 2 retries retentables, chacun précédé d'un contrôle


def test_a_non_retryable_error_is_not_retried(monkeypatch):
    harness = _Harness(
        monkeypatch,
        scripts={"gemini/primary": [_StatusError(400)], "gemini/fallback": [(10, 5, 0.0)]},
        calls_per_conversation=1,
    )
    assert harness.run(monkeypatch, "--budget-tokens", "100000") == 0
    assert [e[0] for e in harness.emitted] == ["gemini/primary", "gemini/fallback"]  # 1 appel puis repli de modèle


def test_the_fallback_model_shares_the_same_allocation(monkeypatch):
    """Repli de modèle : l'usage du 1ᵉʳ modèle compte contre l'allocation du second."""
    harness = _Harness(
        monkeypatch,
        scripts={"gemini/primary": [(900, 100, 0.0), RuntimeError("503 persistants")], "gemini/fallback": []},
        calls_per_conversation=2,
    )
    code = harness.run(monkeypatch, "--budget-tokens", "2100")
    # primaire : 1 appel (1 000 tokens) puis erreur → repli ; le repli doit tenir dans 2 100 − 1 000 :
    # son 1ᵉʳ appel (≈ 1 047 réservés) passe, le suivant (1 150 + 1 047 > 2 100) est refusé.
    assert code == 4
    assert [e[0] for e in harness.emitted] == ["gemini/primary", "gemini/primary", "gemini/fallback"]


def test_an_expired_deadline_stops_before_any_emission(monkeypatch, capsys):
    harness = _Harness(monkeypatch, scripts={}, calls_per_conversation=2)
    code = harness.run(monkeypatch, "--budget-tokens", "100000", "--deadline-epoch", str(time.time() - 1))
    assert code == 4 and harness.emitted == []
    assert "échéance" in capsys.readouterr().err


def test_the_runner_refuses_to_start_when_no_emission_point_can_be_guarded(monkeypatch, capsys):
    harness = _Harness(monkeypatch, scripts={}, with_completion=False)
    code = harness.run(monkeypatch, "--budget-tokens", "100000")
    # fail-closed : sans point d'émission contrôlable la garantie ne peut pas être prétendue
    assert code != 0 and harness.emitted == []
    assert "garde budgétaire indisponible" in capsys.readouterr().err


def test_a_usd_cap_without_prices_on_a_billed_model_is_refused_up_front(monkeypatch, capsys):
    harness = _Harness(monkeypatch, scripts={})
    assert harness.run(monkeypatch, "--budget-usd", "0.5") == 3
    assert harness.created == [] and "non bornable" in capsys.readouterr().err


def test_a_subscription_model_needs_no_price_under_a_usd_cap(monkeypatch):
    harness = _Harness(monkeypatch, scripts={}, calls_per_conversation=2)
    assert harness.run(monkeypatch, "--budget-usd", "0.5", "--no-billing", "--budget-tokens", "100000") == 0
    assert len(harness.emitted) == 2  # non facturé : le plafond USD n'a rien à borner


def test_the_final_marker_is_printed_even_when_the_run_fails(monkeypatch, capsys):
    harness = _Harness(
        monkeypatch,
        scripts={"gemini/primary": [RuntimeError("boom")], "gemini/fallback": [RuntimeError("boom 2")]},
        calls_per_conversation=1,
    )
    assert harness.run(monkeypatch, "--budget-tokens", "100000") == 1
    out = capsys.readouterr().out
    assert "[collegue-budget] armed" in out and out.rstrip().endswith("[collegue-budget] final")  # tout émis avant


def test_sigterm_is_turned_into_a_clean_exit_so_usage_is_flushed(monkeypatch):
    harness = _Harness(monkeypatch, scripts={})
    installed = {}
    monkeypatch.setattr(oh_runner.signal, "signal", lambda num, handler: installed.update({num: handler}))
    harness.run(monkeypatch, "--budget-tokens", "100000")
    handler = installed[oh_runner.signal.SIGTERM]
    with pytest.raises(SystemExit) as stop:
        handler(15, None)
    assert stop.value.code == 143
