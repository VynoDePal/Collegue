"""Tests de l'agent codeur OpenHands SDK 1.7 (``OHSdkAgent``) — construction pure, sans OpenHands.

OpenHands 1.7 est SDK-first : ``openhands.core.main`` n'existe plus → le coder réel
lance ``oh_runner.py`` (baké dans l'image). Modèle/clé/abonnement passent par l'env du
sandbox (jamais l'argv). L'exécution réelle est derrière le marqueur ``integration``.
"""

from __future__ import annotations

from types import SimpleNamespace

from collegue.executor import CodeAgent, IssueSpec, OHSdkAgent
from collegue.sandbox import SandboxResult


def _settings(**kw):
    base = {"LLM_PROVIDER": "gemini", "LLM_MODEL": "global-model"}
    base.update(kw)
    return SimpleNamespace(**base)


# --- résolution du modèle (format LiteLLM) -------------------------------------


def test_litellm_model_prefixes_gemini_when_unprefixed():
    agent = OHSdkAgent(object(), settings_obj=_settings(LLM_MODEL_CODER="gemma-x"))
    assert agent.litellm_model() == "gemini/gemma-x"


def test_litellm_model_keeps_already_prefixed():
    agent = OHSdkAgent(object(), settings_obj=_settings(LLM_MODEL_CODER="openai/gpt-5.5"))
    assert agent.litellm_model() == "openai/gpt-5.5"


def test_litellm_model_falls_back_to_global():
    assert OHSdkAgent(object(), settings_obj=_settings()).litellm_model() == "gemini/global-model"


# --- build_command : lance le runner, rien de secret dans l'argv ----------------


def test_build_command_runs_runner_with_task():
    agent = OHSdkAgent(object(), settings_obj=_settings(), max_iterations=12)
    issue = IssueSpec(number=9, title="Faire la chose")
    argv = agent.build_command(issue)
    assert argv == ["python", "/opt/oh_runner.py", "--max-iterations", "12", "-t", issue.to_prompt()]
    # ni modèle ni clé dans l'argv (injectés par l'env du sandbox) → pas de fuite via ps.
    assert not any("API_KEY" in part or "LLM_MODEL" in part for part in argv)


# --- implement_issue : statut + usage ------------------------------------------


class _Sandbox:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def run_command(self, argv, workspace):
        self.calls.append((argv, workspace))
        return self._result


def test_implement_issue_parses_usage_and_subscription_authoritative():
    out = (
        "oh_runner: modèle gpt-5.5\n"
        '[collegue-usage] {"prompt_tokens": 100, "completion_tokens": 20, "cost_usd": 0.0, "billable": false}\n'
        "OH_RUNNER_DONE"
    )
    sandbox = _Sandbox(SandboxResult(exit_code=0, stdout=out, stderr=""))
    res = OHSdkAgent(sandbox, settings_obj=_settings()).implement_issue("/ws", IssueSpec(number=3, title="T"))
    assert res.success is True
    assert res.prompt_tokens == 100 and res.completion_tokens == 20
    assert res.cost_usd == 0.0
    assert res.cost_authoritative is True  # billable:false (abonnement) → coût 0 autoritaire (#504)
    assert sandbox.calls[0][1] == "/ws"


def test_implement_issue_failure_is_fail_closed():
    res = OHSdkAgent(_Sandbox(SandboxResult(exit_code=1, stdout="", stderr="boom"))).implement_issue(
        "/ws", IssueSpec(number=1, title="T")
    )
    assert res.success is False


def test_sdk_agent_satisfies_codeagent_protocol():
    assert isinstance(OHSdkAgent(object()), CodeAgent)
