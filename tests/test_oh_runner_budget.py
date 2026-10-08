"""Garde budgétaire DU RUNNER (vague 2) : contrôle de chaque appel AVANT émission, dans le conteneur.

Le runner est un script autonome (stdlib seule) copié dans l'image : ces tests le lancent via ``main()`` avec un
faux SDK OpenHands qui enregistre les appels RÉELLEMENT émis. Aucun réseau, aucune clé réelle.
"""

from __future__ import annotations

import sys
import threading
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
        self.response = object()  # une VRAIE réponse HTTP a été reçue (sinon le statut ne prouve rien)


class ConnectError(Exception):
    """Même nom que ``httpx.ConnectError`` : rien n'est parti."""


class _Harness:
    """Faux SDK : chaque ``LLM`` a un ``completion`` scripté ; la conversation appelle ``completion`` N fois."""

    def __init__(
        self,
        monkeypatch,
        *,
        scripts,
        calls_per_conversation=1,
        with_completion=True,
        payload=None,
        honor_output_cap=True,
    ):
        self.created: list = []
        self.emitted: list = []
        outer = self

        class FakeLLM:
            def __init__(self, *, model, **kwargs):
                self.model = model
                self.kwargs = kwargs
                self.metrics = _metrics()
                self.max_output_tokens = kwargs.get("max_output_tokens") if honor_output_cap else None
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
                    self.llm.completion(*(payload if payload is not None else ("x" * 200,)))

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
        monkeypatch.setenv("OH_MAX_OUTPUT_TOKENS", "1000")
        monkeypatch.setattr(oh_runner.BudgetGuard, "start_watchdog", lambda self, period=0.5: None)
        monkeypatch.delenv("LLM_SUBSCRIPTION", raising=False)
        monkeypatch.setattr(oh_runner.signal, "signal", lambda *a, **k: None)

    def run(self, monkeypatch, *args):
        monkeypatch.setattr(sys, "argv", ["oh_runner", "--task", "t", *args])
        return oh_runner.main()


PRICES = '{"gemini/primary": [0.0000015, 0.000009], "gemini/fallback": [0.0000003, 0.0000025]}'
# Borne du prompt de la conversation factice : 200 octets + 16 (message) + 64 (requête) = 280 tokens ;
# sortie bornée à 1 000 (OH_MAX_OUTPUT_TOKENS) ⇒ une réservation de 1 280 tokens par appel.


def test_without_allocation_the_runner_is_unchanged(monkeypatch, capsys):
    harness = _Harness(monkeypatch, scripts={}, calls_per_conversation=1)
    assert harness.run(monkeypatch) == 0
    out = capsys.readouterr().out
    assert "[collegue-budget]" not in out  # aucune garde, aucun marqueur
    assert harness.created[0].kwargs["num_retries"] == 2  # retries internes historiques conservés
    assert "max_output_tokens" not in harness.created[0].kwargs


def test_with_an_allocation_the_runner_arms_then_finalizes_and_disables_sdk_retries(monkeypatch, capsys):
    harness = _Harness(monkeypatch, scripts={}, calls_per_conversation=1)
    assert harness.run(monkeypatch, "--budget-tokens", "100000", "--deadline-epoch", str(time.time() + 600)) == 0
    out = capsys.readouterr().out
    assert out.index("[collegue-budget] armed") < out.index("OH_RUNNER_DONE") < out.index("[collegue-budget] final")
    assert harness.created[0].kwargs["num_retries"] == 0  # les retries passent par la garde
    assert harness.created[0].kwargs["max_output_tokens"] == 1000  # la sortie est bornée PAR le LLM


def test_a_call_that_does_not_fit_the_token_allocation_is_never_emitted(monkeypatch, capsys):
    harness = _Harness(monkeypatch, scripts={"gemini/primary": [(200, 100, 0.0)]}, calls_per_conversation=3)
    code = harness.run(monkeypatch, "--budget-tokens", "1500")  # 1 280 réservés ; après 300 réels, +1 280 > 1 500
    assert code == 4
    assert len(harness.emitted) == 1  # le 2ᵉ appel n'est PAS parti
    assert "allocation budgétaire atteinte" in capsys.readouterr().err
    assert len(harness.created) == 1  # et le repli de modèle n'agrandit pas l'allocation


def test_the_prompt_bound_counts_the_whole_payload_in_utf8_bytes(monkeypatch):
    # 400 caractères CJK = 1 200 octets ⇒ borne ≥ 1 200 tokens (l'ancien « chars/2 + 32 » en comptait 232).
    harness = _Harness(monkeypatch, scripts={}, payload=("漢" * 400,), calls_per_conversation=1)
    assert harness.run(monkeypatch, "--budget-tokens", "2000") == 4  # 1 200 + 80 + 1 000 > 2 000
    assert harness.emitted == []


def test_system_tools_and_schemas_are_part_of_the_bound(monkeypatch):
    messages = [{"role": "system", "content": "s" * 300}, {"role": "user", "content": "u"}]
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"d": "p" * 600}}}]
    harness = _Harness(monkeypatch, scripts={}, calls_per_conversation=1)
    harness.payload_kwargs = None
    # appel direct de la garde : les outils comptent même s'ils ne sont pas dans « messages »
    guard = oh_runner.BudgetGuard(max_tokens=10**6)
    llm = SimpleNamespace(max_output_tokens=100, metrics=_metrics())
    bound_plain, _ = guard.precheck(llm, ((), {"messages": messages}))
    bound_tools, _ = guard.precheck(llm, ((), {"messages": messages, "tools": tools}))
    assert bound_tools >= bound_plain + 600


def test_a_non_text_modality_is_refused_before_emission(monkeypatch, capsys):
    image = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}]
    harness = _Harness(monkeypatch, scripts={}, payload=(image,), calls_per_conversation=1)
    assert harness.run(monkeypatch, "--budget-tokens", "100000") == 4
    assert harness.emitted == [] and "non bornable" in capsys.readouterr().err


def test_a_usd_allocation_is_enforced_with_the_price_of_the_model_in_use(monkeypatch):
    harness = _Harness(monkeypatch, scripts={"gemini/primary": [(280, 900, 0.0)]}, calls_per_conversation=3)
    code = harness.run(monkeypatch, "--budget-usd", "0.012", "--prices", PRICES, "--budget-tokens", "10000000")
    # 1ʳᵉ réservation ≈ 0,0094 $ ≤ 0,012 ; après 1 appel réel (0,0085 $) le suivant ferait ≈ 0,0179 $ > 0,012 $.
    assert code == 4 and len(harness.emitted) == 1


def test_each_fallback_model_is_priced_with_its_own_price(monkeypatch):
    """Le repli n'hérite PAS du tarif du modèle principal : un modèle sans tarif propre est écarté."""
    harness = _Harness(
        monkeypatch,
        scripts={"gemini/primary": [RuntimeError("503 persistants")], "gemini/fallback": [(100, 50, 0.0)]},
        calls_per_conversation=1,
    )
    only_primary = '{"gemini/primary": [0.0000015, 0.000009]}'
    code = harness.run(monkeypatch, "--budget-usd", "5", "--prices", only_primary)
    assert code == 1  # le repli a été écarté faute de tarif : il n'a jamais été construit ni appelé
    assert [llm.model for llm in harness.created] == ["gemini/primary"]


def test_a_known_fallback_price_lets_the_fallback_run_under_the_same_allocation(monkeypatch):
    harness = _Harness(
        monkeypatch,
        scripts={"gemini/primary": [RuntimeError("503 persistants")], "gemini/fallback": [(100, 50, 0.0)]},
        calls_per_conversation=1,
    )
    assert harness.run(monkeypatch, "--budget-usd", "5", "--prices", PRICES) == 0
    assert [e[0] for e in harness.emitted] == ["gemini/primary", "gemini/fallback"]


def test_a_429_is_retried_but_each_retry_is_checked_and_a_5xx_is_not_retried_in_strict(monkeypatch):
    harness = _Harness(
        monkeypatch,
        scripts={"gemini/primary": [_StatusError(429), _StatusError(429), (10, 5, 0.0)]},
        calls_per_conversation=1,
    )
    sleeps = []
    monkeypatch.setattr(oh_runner.time, "sleep", lambda s: sleeps.append(s))
    assert harness.run(monkeypatch, "--budget-tokens", "100000", "--strict") == 0
    assert len(harness.emitted) == 3 and len(sleeps) == 2  # 2 rejets 429 prouvés, chacun précédé d'un contrôle


def test_a_5xx_in_strict_is_unknown_usage_without_retry_or_fallback(monkeypatch, capsys):
    harness = _Harness(
        monkeypatch,
        scripts={"gemini/primary": [_StatusError(503), (10, 5, 0.0)], "gemini/fallback": [(10, 5, 0.0)]},
        calls_per_conversation=1,
    )
    assert harness.run(monkeypatch, "--budget-tokens", "100000", "--strict") == 4
    captured = capsys.readouterr()
    assert [e[0] for e in harness.emitted] == ["gemini/primary"]  # ni retry ni repli
    assert "[collegue-budget] unknown" in captured.out
    assert "[collegue-budget] final" not in captured.out  # `final` ne prouve rien : l'usage est inconnu


def test_a_5xx_in_advisory_mode_keeps_the_historical_retry(monkeypatch):
    harness = _Harness(monkeypatch, scripts={"gemini/primary": [_StatusError(503), (10, 5, 0.0)]})
    monkeypatch.setattr(oh_runner.time, "sleep", lambda s: None)
    assert harness.run(monkeypatch, "--budget-tokens", "100000") == 0
    assert len(harness.emitted) == 2


def test_a_connection_failure_before_send_is_released_and_retried_even_in_strict(monkeypatch):
    refused = RuntimeError("connexion refusée")
    refused.__cause__ = ConnectError("refused")
    harness = _Harness(monkeypatch, scripts={"gemini/primary": [refused, (10, 5, 0.0)]})
    monkeypatch.setattr(oh_runner.time, "sleep", lambda s: None)
    assert harness.run(monkeypatch, "--budget-tokens", "100000", "--strict") == 0
    assert len(harness.emitted) == 2


def test_a_proven_rejection_falls_back_to_the_next_model(monkeypatch):
    harness = _Harness(
        monkeypatch,
        scripts={"gemini/primary": [_StatusError(400)], "gemini/fallback": [(10, 5, 0.0)]},
        calls_per_conversation=1,
    )
    assert harness.run(monkeypatch, "--budget-tokens", "100000", "--strict") == 0
    assert [e[0] for e in harness.emitted] == ["gemini/primary", "gemini/fallback"]


def test_the_fallback_model_shares_the_same_allocation(monkeypatch):
    """Repli de modèle (mode advisory, échec non classé) : l'usage du 1ᵉʳ modèle compte contre l'allocation du second."""
    harness = _Harness(
        monkeypatch,
        scripts={"gemini/primary": [(200, 100, 0.0), RuntimeError("503 persistants")], "gemini/fallback": []},
        calls_per_conversation=2,
    )
    code = harness.run(monkeypatch, "--budget-tokens", "1600")
    # primaire : 1 appel (300 tokens) puis erreur → repli ; son 1ᵉʳ appel (300 + 1 280 ≤ 1 600) passe, le suivant
    # (450 + 1 280 > 1 600) est refusé : l'allocation est COMMUNE aux modèles.
    assert code == 4
    assert [e[0] for e in harness.emitted] == ["gemini/primary", "gemini/primary", "gemini/fallback"]


def test_a_response_without_counted_usage_makes_the_usage_unknown(monkeypatch, capsys):
    harness = _Harness(monkeypatch, scripts={"gemini/primary": [(0, 0, 0.0)]}, calls_per_conversation=1)
    assert harness.run(monkeypatch, "--budget-tokens", "100000", "--strict") == 4
    out = capsys.readouterr().out
    assert "[collegue-budget] unknown" in out and "[collegue-budget] final" not in out


def test_a_provider_exceeding_the_bound_makes_the_usage_unknown(monkeypatch, capsys):
    harness = _Harness(monkeypatch, scripts={"gemini/primary": [(5000, 50, 0.0)]}, calls_per_conversation=1)
    assert harness.run(monkeypatch, "--budget-tokens", "100000", "--strict") == 4
    captured = capsys.readouterr()
    assert "borne de tokens démentie" in captured.out + captured.err
    assert "[collegue-budget] final" not in captured.out


def test_a_sdk_that_swallows_the_stop_still_ends_with_unknown_usage(monkeypatch, capsys):
    """Le SDK peut attraper l'exception de la garde et finir « normalement » : le run n'est PAS un succès."""
    harness = _Harness(monkeypatch, scripts={"gemini/primary": [_StatusError(503)]}, calls_per_conversation=1)
    monkeypatch.setattr(
        harness_conversation := sys.modules["openhands.sdk"].Conversation,
        "run",
        lambda self: _swallow(lambda: self.llm.completion("x" * 200)),
    )
    assert harness_conversation is not None
    assert harness.run(monkeypatch, "--budget-tokens", "100000", "--strict") == 4
    out = capsys.readouterr().out
    assert "OH_RUNNER_DONE" not in out and "[collegue-budget] final" not in out


def _swallow(call):
    try:
        call()
    except Exception:  # noqa: BLE001 - ce que ferait un SDK qui avale l'erreur
        pass


def test_an_unbounded_output_is_refused_before_emission(monkeypatch, capsys):
    harness = _Harness(monkeypatch, scripts={}, honor_output_cap=False, calls_per_conversation=1)
    assert harness.run(monkeypatch, "--budget-tokens", "100000") == 4
    assert harness.emitted == [] and "sortie non bornée" in capsys.readouterr().err


def test_an_unknown_tokenizer_family_is_refused_in_strict_unless_attested(monkeypatch, capsys):
    monkeypatch.setenv("LLM_MODEL", "mystery/primary")
    monkeypatch.setenv("OH_FALLBACK_MODELS", "")
    harness = _Harness(monkeypatch, scripts={})
    monkeypatch.setenv("LLM_MODEL", "mystery/primary")
    monkeypatch.setenv("OH_FALLBACK_MODELS", "")
    assert harness.run(monkeypatch, "--budget-tokens", "100000", "--strict") == 3
    assert harness.created == [] and "tokenizer" in capsys.readouterr().err
    assert harness.run(monkeypatch, "--budget-tokens", "100000", "--strict", "--byte-bounded-models", "mystery") == 0


def test_an_expired_deadline_stops_before_any_emission(monkeypatch, capsys):
    harness = _Harness(monkeypatch, scripts={}, calls_per_conversation=2)
    code = harness.run(monkeypatch, "--budget-tokens", "100000", "--deadline-epoch", str(time.time() - 1))
    assert code == 4 and harness.emitted == []
    assert "échéance" in capsys.readouterr().err


def test_the_deadline_cancels_an_in_flight_call_and_leaves_the_usage_unknown(capsys):
    """Chien de garde RÉEL (thread) : l'appel en vol ne rend jamais la main ; à l'échéance le process s'arrête."""
    exited = threading.Event()
    codes = []
    guard = oh_runner.BudgetGuard(
        max_tokens=100000, deadline_epoch=time.time() + 0.15, exit_fn=lambda code: (codes.append(code), exited.set())
    )
    release = threading.Event()
    llm = SimpleNamespace(
        max_output_tokens=100, metrics=_metrics(), completion=lambda *a, **k: release.wait(10) and "late"
    )
    guard.install(llm, "gemini/primary")
    guard.start_watchdog(period=0.01)
    call = threading.Thread(target=lambda: _swallow(lambda: llm.completion("x")), daemon=True)
    call.start()
    assert exited.wait(5), "le chien de garde n'a pas arrêté le worker à l'échéance"
    release.set()
    assert codes[0] == 5 and guard.tainted and "en vol" in guard.tainted
    out = capsys.readouterr().out
    assert "[collegue-budget] unknown" in out and "[collegue-budget] final" not in out


def test_a_deadline_without_a_call_in_flight_is_a_known_stop():
    codes = []
    guard = oh_runner.BudgetGuard(max_tokens=10, deadline_epoch=time.time() - 1, exit_fn=codes.append)
    assert guard.enforce_deadline() is True and codes == [5] and guard.tainted is None


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


def test_the_final_marker_is_printed_even_when_the_run_fails_without_unknown_usage(monkeypatch, capsys):
    harness = _Harness(
        monkeypatch,
        scripts={"gemini/primary": [RuntimeError("boom")], "gemini/fallback": [RuntimeError("boom 2")]},
        calls_per_conversation=1,
    )
    assert harness.run(monkeypatch, "--budget-tokens", "100000") == 1  # advisory : échecs non classés « sans conso »
    out = capsys.readouterr().out
    assert "[collegue-budget] armed" in out and out.rstrip().endswith("[collegue-budget] final")


def test_sigterm_is_turned_into_a_clean_exit_so_usage_is_flushed(monkeypatch):
    harness = _Harness(monkeypatch, scripts={})
    installed = {}
    monkeypatch.setattr(oh_runner.signal, "signal", lambda num, handler: installed.update({num: handler}))
    harness.run(monkeypatch, "--budget-tokens", "100000")
    handler = installed[oh_runner.signal.SIGTERM]
    with pytest.raises(SystemExit) as stop:
        handler(15, None)
    assert stop.value.code == 143
