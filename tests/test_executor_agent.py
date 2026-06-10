"""Tests E1 (#363) : contrat CodeAgent + adaptateur OpenHands (isolé, fake en CI)."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from collegue.executor import AgentResult, CodeAgent, FakeCodeAgent, IssueSpec, OpenHandsAgent
from collegue.sandbox import SandboxResult

# --- IssueSpec / sanitisation ---------------------------------------------------


def test_issue_to_prompt_sanitizes_markdown_injection():
    issue = IssueSpec(
        number=42,
        title="Ajouter   le  endpoint",
        body="Ligne 1\n## Fausse section\n- [x] faux critère coché",
        acceptance_criteria=("Retourne 200", "  Gère\nles\terreurs  "),
    )
    prompt = issue.to_prompt()
    # Titre/critères inline-isés : pas de saut de ligne forgé par le contenu d'issue.
    assert "Issue #42: Ajouter le endpoint" in prompt
    # Le body multi-ligne est réduit à UNE seule ligne (le contenu reste, mais ne
    # peut plus forger de fausse section ## ni de case - [x]).
    assert "Ligne 1 ## Fausse section - [x] faux critère coché" in prompt
    assert "\n## Fausse section" not in prompt  # ne démarre jamais une ligne
    assert "- Gère les erreurs" in prompt  # critère ré-inliné


def test_issue_to_prompt_minimal():
    prompt = IssueSpec(number=1, title="Titre").to_prompt()
    assert prompt == "Issue #1: Titre"


def test_issue_to_prompt_drops_blank_criteria():
    prompt = IssueSpec(number=1, title="T", acceptance_criteria=("", "   ", "Vrai")).to_prompt()
    assert "- Vrai" in prompt
    assert prompt.count("- ") == 1  # les critères vides sont ignorés


def test_issue_to_prompt_includes_context():
    # #412 : le contexte inter-tâches est rendu (et inline-isé : pas d'injection).
    issue = IssueSpec(number=2, title="T", context="Dépend de « Schéma »\n## faux titre")
    prompt = issue.to_prompt()
    assert "Contexte : Dépend de « Schéma »" in prompt
    assert "\n## faux titre" not in prompt  # multi-ligne aplati (anti-injection)


def test_issue_to_prompt_no_context_by_default():
    assert "Contexte" not in IssueSpec(number=1, title="Titre").to_prompt()


# --- FakeCodeAgent --------------------------------------------------------------


def test_fake_agent_writes_default_marker(tmp_path):
    agent = FakeCodeAgent()
    result = agent.implement_issue(str(tmp_path), IssueSpec(number=7, title="T"))
    assert result.success is True
    assert result.files_changed == ("COLLEGUE_FAKE.txt",)
    assert (tmp_path / "COLLEGUE_FAKE.txt").read_text(encoding="utf-8") == "changement simulé\n"


def test_fake_agent_writes_custom_nested_files(tmp_path):
    agent = FakeCodeAgent({"src/app.py": "print('hi')\n", "README.md": "# Hi\n"})
    result = agent.implement_issue(str(tmp_path), IssueSpec(number=1, title="T"))
    assert result.success is True
    assert result.files_changed == ("README.md", "src/app.py")  # trié
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_fake_agent_failure_writes_nothing(tmp_path):
    result = FakeCodeAgent(succeed=False).implement_issue(str(tmp_path), IssueSpec(number=1, title="T"))
    assert result.success is False
    assert result.files_changed == ()
    assert list(tmp_path.iterdir()) == []  # aucun fichier écrit


def test_fake_agent_rejects_path_traversal(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    agent = FakeCodeAgent({"../evil.txt": "pwn"})
    with pytest.raises(ValueError):
        agent.implement_issue(str(work), IssueSpec(number=1, title="T"))
    assert not (tmp_path / "evil.txt").exists()  # rien écrit hors du workspace


def test_fake_agent_rejects_absolute_path(tmp_path):
    agent = FakeCodeAgent({"/etc/passwd": "pwn"})
    with pytest.raises(ValueError):
        agent.implement_issue(str(tmp_path), IssueSpec(number=1, title="T"))


def test_fake_agent_rejects_empty_or_dir_path(tmp_path):
    # Un nom vide / "." résout vers la racine du workspace : refusé proprement
    # (ValueError), pas d'IsADirectoryError opaque au moment du open().
    for bad in ("", "."):
        with pytest.raises(ValueError):
            FakeCodeAgent({bad: "x"}).implement_issue(str(tmp_path), IssueSpec(number=1, title="T"))


# --- Protocole ------------------------------------------------------------------


def test_codeagent_protocol_runtime_checkable():
    assert isinstance(FakeCodeAgent(), CodeAgent)
    assert isinstance(OpenHandsAgent(sandbox=object()), CodeAgent)
    assert not isinstance(object(), CodeAgent)


# --- OpenHandsAgent (construction pure, sans OpenHands) --------------------------


def _settings(**kw):
    base = {"LLM_PROVIDER": "gemini", "LLM_MODEL": "global-model"}
    base.update(kw)
    return SimpleNamespace(**base)


def test_openhands_resolved_model_uses_coder_role():
    agent = OpenHandsAgent(
        sandbox=object(),
        settings_obj=_settings(LLM_PROVIDER_CODER="openai", LLM_MODEL_CODER="gpt-coder"),
    )
    assert agent.resolved_model() == ("openai", "gpt-coder")


def test_openhands_resolved_model_falls_back_to_global():
    agent = OpenHandsAgent(sandbox=object(), settings_obj=_settings())
    assert agent.resolved_model() == ("gemini", "global-model")


def test_openhands_build_command_carries_task_and_model_not_secret():
    agent = OpenHandsAgent(sandbox=object(), settings_obj=_settings(LLM_MODEL_CODER="coder-x"))
    issue = IssueSpec(number=9, title="Faire la chose")
    argv = agent.build_command(issue)
    assert argv[0] == "env"
    assert "LLM_MODEL=coder-x" in argv
    assert "-t" in argv
    assert argv[argv.index("-t") + 1] == issue.to_prompt()
    assert "openhands.core.main" in argv
    # La clé API ne doit jamais apparaître dans l'argv (fuite via ps).
    assert not any("API_KEY" in part for part in argv)


def test_openhands_build_command_propagates_coder_retry_policy():
    """#422 : le moteur ne peut pas intercepter les appels LiteLLM du sandbox —
    il PROPAGE donc la politique retries/backoff au worker via l'env OpenHands."""
    agent = OpenHandsAgent(
        sandbox=object(),
        settings_obj=_settings(CODER_LLM_NUM_RETRIES=5, CODER_LLM_RETRY_MIN_WAIT=10, CODER_LLM_RETRY_MAX_WAIT=60),
    )
    argv = agent.build_command(IssueSpec(number=9, title="T"))
    assert "LLM_NUM_RETRIES=5" in argv
    assert "LLM_RETRY_MIN_WAIT=10" in argv
    assert "LLM_RETRY_MAX_WAIT=60" in argv
    # L'env précède l'entrypoint python (consommé par `env`).
    assert argv.index("LLM_NUM_RETRIES=5") < argv.index("-m")


def test_openhands_retry_policy_defaults_and_robustness():
    # Sans settings dédiés → défauts ; valeurs invalides/négatives → défauts.
    assert OpenHandsAgent(sandbox=object(), settings_obj=_settings()).retry_policy() == (8, 8, 90)
    agent = OpenHandsAgent(
        sandbox=object(),
        settings_obj=_settings(CODER_LLM_NUM_RETRIES="n/a", CODER_LLM_RETRY_MIN_WAIT=-3),
    )
    assert agent.retry_policy() == (8, 8, 90)


def test_openhands_min_interval_paces_consecutive_launches():
    """#422 : back-pressure start-to-start — deux lancements coder rapprochés sont
    espacés de CODER_MIN_INTERVAL_SECONDS (lisse le débit sur le quota partagé)."""
    sandbox = _FakeSandbox(SandboxResult(exit_code=0, stdout="ok", stderr=""))
    waits = []
    fake_now = [100.0]
    agent = OpenHandsAgent(
        sandbox=sandbox,
        settings_obj=_settings(CODER_MIN_INTERVAL_SECONDS=30.0),
        clock=lambda: fake_now[0],
        sleep=waits.append,
    )
    issue = IssueSpec(number=1, title="T")
    agent.implement_issue("/work", issue)
    assert waits == []  # premier lancement : aucun délai
    fake_now[0] = 112.0  # 12s plus tard → il manque 18s
    agent.implement_issue("/work", issue)
    assert waits == [18.0]
    fake_now[0] = 200.0  # bien après l'intervalle → pas d'attente
    agent.implement_issue("/work", issue)
    assert waits == [18.0]


def test_openhands_min_interval_disabled_by_default():
    sandbox = _FakeSandbox(SandboxResult(exit_code=0, stdout="ok", stderr=""))
    waits = []
    agent = OpenHandsAgent(sandbox=sandbox, settings_obj=_settings(), clock=lambda: 0.0, sleep=waits.append)
    issue = IssueSpec(number=1, title="T")
    agent.implement_issue("/work", issue)
    agent.implement_issue("/work", issue)
    assert waits == []  # 0 (défaut) = pas d'espacement


class _FakeSandbox:
    """Sandbox factice : enregistre l'argv et renvoie un SandboxResult canné."""

    def __init__(self, result: SandboxResult):
        self._result = result
        self.calls = []

    def run_command(self, argv, workspace):
        self.calls.append((list(argv), workspace))
        return self._result


def test_openhands_implement_issue_success_maps_sandbox_result():
    sandbox = _FakeSandbox(SandboxResult(exit_code=0, stdout="ok", stderr=""))
    agent = OpenHandsAgent(sandbox=sandbox, settings_obj=_settings())
    issue = IssueSpec(number=3, title="T")
    result = agent.implement_issue("/work", issue)
    assert isinstance(result, AgentResult)
    assert result.success is True
    assert "ok" in result.logs
    # Le sandbox a bien reçu l'argv construit, sur le bon workspace.
    assert sandbox.calls == [(agent.build_command(issue), "/work")]


def test_openhands_implement_issue_failure_is_not_success():
    sandbox = _FakeSandbox(SandboxResult(exit_code=1, stdout="", stderr="boom"))
    agent = OpenHandsAgent(sandbox=sandbox, settings_obj=_settings())
    result = agent.implement_issue("/work", IssueSpec(number=1, title="T"))
    assert result.success is False
    assert "boom" in result.logs


def test_openhands_implement_issue_timeout_is_not_success():
    sandbox = _FakeSandbox(SandboxResult(exit_code=124, stdout="", stderr="", timed_out=True))
    agent = OpenHandsAgent(sandbox=sandbox, settings_obj=_settings())
    assert agent.implement_issue("/work", IssueSpec(number=1, title="T")).success is False


# --- Isolation ------------------------------------------------------------------


def test_importing_executor_does_not_pull_openhands():
    # Importer l'exécuteur ne doit tirer aucune dépendance OpenHands (elle vit
    # dans l'image sandbox, pas dans le package Python).
    import collegue.executor  # noqa: F401

    assert not any(name == "openhands" or name.startswith("openhands.") for name in sys.modules)


def test_app_does_not_wire_executor():
    # Garde d'isolation : app.py ne câble pas l'exécuteur (le pilote Phase 3 le fera).
    app_src = (Path(__file__).resolve().parent.parent / "collegue" / "app.py").read_text(encoding="utf-8")
    assert "collegue.executor" not in app_src
    assert "from collegue.executor" not in app_src
