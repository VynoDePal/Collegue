"""Tests F4 (#377) : câblage runtime opt-in (entrypoint + assemblage) + reporting."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from collegue.executor import FakeCodeAgent, FakeReviewer, PrClients
from collegue.pilot import ProjectRunResult, TaskOutcome, format_run_report, run_project_from_settings
from collegue.pilot.__main__ import build_parser
from collegue.pilot.runtime import collegue_home_durability_warning
from collegue.planner import PlanNotApproved, PlanTargetError, approve_plan, normalize_plan_sync_config
from collegue.sandbox import SandboxResult
from collegue.state import ProjectStateManager

# --- fakes (mêmes doubles que F3) -----------------------------------------------


class _Sandbox:
    def run_tests(self, workspace, command="pytest -q"):
        return SandboxResult(exit_code=0, stdout="ok", stderr="")


class _Branches:
    def ensure_branch(self, owner, repo, branch, from_branch=None):
        return SimpleNamespace(name=branch)


class _Files:
    def update_file(self, owner, repo, path, message, content, branch=None):
        return {}

    def delete_file(self, owner, repo, path, message, branch=None):
        return {}


class _PRs:
    def find_pr_by_head(self, owner, repo, head, base=None, state="open"):
        return None

    def create_pr(self, owner, repo, title, head, base, body):
        return SimpleNamespace(number=101, html_url="https://gh/pull/101", head_branch=head)


def _clients():
    return PrClients(branches=_Branches(), files=_Files(), prs=_PRs())


class _Budget:
    """Budget toujours OK (déterministe, sans collecteur global)."""

    def should_continue(self):
        return SimpleNamespace(action="continue", ok=True)

    def time_remaining_seconds(self):
        return None


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    _git(src, "init", "-q")
    _git(src, "config", "user.email", "t@example.com")
    _git(src, "config", "user.name", "Test")
    (src / "existing.txt").write_text("original\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "init")
    return str(src)


@pytest.fixture
def manager(tmp_path):
    return ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)


def _linear(manager, n):
    pid = manager.create_project(name="demo", spec="# SPEC\n")
    prev = None
    for i in range(n):
        prev = manager.add_task(pid, title=f"T{i}", depends_on=[prev] if prev else None)
    return pid


async def _run(manager, git_repo, pid, *, dry_run):
    if not dry_run:
        approve_plan(manager, pid)
    return await run_project_from_settings(
        pid,
        git_repo,
        owner="o",
        repo="r",
        dry_run=dry_run,
        manager=manager,
        sandbox=_Sandbox(),
        agent=FakeCodeAgent(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        budget=_Budget(),
    )


# --- assemblage + run -----------------------------------------------------------


async def test_dry_run_builds_chain_without_writes(git_repo, manager):
    pid = _linear(manager, 2)
    result = await _run(manager, git_repo, pid, dry_run=True)
    assert result.stop_reason == "completed"
    assert result.iterations == 2
    assert all(t.status == "todo" for t in manager.get_tasks(pid))  # aucune écriture
    assert manager.get_decisions(pid) == []  # pas de résumé en dry_run


async def test_real_run_rejects_unapproved_plan_before_execution(git_repo, manager):
    pid = _linear(manager, 1)
    with pytest.raises(PlanNotApproved):
        await run_project_from_settings(
            pid,
            git_repo,
            owner="o",
            repo="r",
            dry_run=False,
            manager=manager,
            sandbox=_Sandbox(),
            agent=FakeCodeAgent(),
            reviewer=FakeReviewer(),
            clients=_clients(),
            budget=_Budget(),
        )
    assert manager.get_tasks(pid)[0].status == "todo"


async def test_real_run_refuses_repository_or_base_outside_sealed_target(git_repo, manager):
    target = normalize_plan_sync_config({"owner": "sealed", "repo": "app", "base_branch": "develop"})
    pid = manager.create_project(name="sealed", spec="# SPEC\n", plan_sync_config=target)
    manager.add_task(pid, title="T")
    approve_plan(manager, pid)

    with pytest.raises(PlanTargetError, match="cible approuvée"):
        await run_project_from_settings(
            pid,
            git_repo,
            owner="sealed",
            repo="other",
            base="main",
            dry_run=False,
            manager=manager,
            sandbox=_Sandbox(),
            agent=FakeCodeAgent(),
            reviewer=FakeReviewer(),
            clients=_clients(),
            budget=_Budget(),
        )


async def test_real_run_records_summary_decision(git_repo, manager):
    pid = _linear(manager, 1)
    result = await _run(manager, git_repo, pid, dry_run=False)
    assert result.stop_reason == "completed"
    assert any("Run pilote" in d.summary for d in manager.get_decisions(pid))
    assert manager.get_tasks(pid)[0].status == "in_review"


async def test_real_run_wires_cost_governance_by_default(git_repo, manager):
    """#441 : en réel, audit persistant + source de coût branchés PAR DÉFAUT — le
    ledger vit (métriques run_cost_usd/run_tokens lues par run_cost_summary) et le
    journal de décisions porte le bilan, au lieu de 0 $ / 0 token sur 7 h de LLM."""
    import dataclasses

    from collegue.pilot.audit import run_cost_summary

    class _UsageAgent:
        def __init__(self):
            self._ok = FakeCodeAgent()

        def implement_issue(self, workspace, issue):
            result = self._ok.implement_issue(workspace, issue)
            return dataclasses.replace(result, prompt_tokens=1000, completion_tokens=200, cost_usd=0.003)

    pid = _linear(manager, 1)
    approve_plan(manager, pid)
    result = await run_project_from_settings(
        pid,
        git_repo,
        owner="o",
        repo="r",
        dry_run=False,
        manager=manager,
        sandbox=_Sandbox(),
        agent=_UsageAgent(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        budget=_Budget(),
    )
    assert result.stop_reason == "completed"
    summary = run_cost_summary(manager, pid)
    assert summary["tokens"] == 1200  # canal coder enfin compté
    assert summary["usd"] == pytest.approx(0.003)
    assert any("coût≈" in d.summary for d in manager.get_decisions(pid))


# --- coder OpenHands SDK + abonnement (env du sandbox) --------------------------


def test_coder_sandbox_env_subscription_mode():
    import collegue.pilot.runtime as runtime

    settings = SimpleNamespace(
        CODER_SUBSCRIPTION=True, CODER_SUBSCRIPTION_MODEL="gpt-5.5", CODER_SUBSCRIPTION_FALLBACK="gpt-5.4"
    )
    env = runtime._coder_sandbox_env(settings)
    assert env["LLM_MODEL"] == "gpt-5.5"  # modèle d'abonnement NU (pas de préfixe gemini/)
    assert env["LLM_SUBSCRIPTION"] == "1"
    assert env["OH_FALLBACK_MODELS"] == "gpt-5.4"
    assert env["HOME"] == "/home/sandbox"  # hors /tmp (requis par le montage des creds)


def test_coder_sandbox_env_api_key_mode():
    import collegue.pilot.runtime as runtime

    settings = SimpleNamespace(LLM_PROVIDER="gemini", LLM_MODEL="gemma-x")  # CODER_SUBSCRIPTION absent → False
    env = runtime._coder_sandbox_env(settings)
    assert env["LLM_MODEL"] == "gemini/gemma-x"  # format LiteLLM
    assert "LLM_SUBSCRIPTION" not in env


def test_gate_sandbox_keeps_runtime_resources_without_coder_credentials(tmp_path):
    """Le code projet du gate garde les ressources/installations, jamais les secrets coder."""
    import collegue.pilot.runtime as runtime

    cache = tmp_path / "pip-cache"
    auth = tmp_path / "openhands-auth"
    auth.mkdir()
    settings = SimpleNamespace(
        SANDBOX_IMAGE="sandbox-project:sha256-test",
        SANDBOX_NETWORK="bridge",
        SANDBOX_DNS="1.1.1.1, 8.8.8.8",
        SANDBOX_PIP_CACHE_DIR=str(cache),
        SANDBOX_SUBSCRIPTION_AUTH_DIR=str(auth),
        SANDBOX_MEMORY="7g",
        SANDBOX_CPUS="3.0",
        SANDBOX_TIMEOUT=987.0,
        CODER_SUBSCRIPTION=True,
        CODER_SUBSCRIPTION_MODEL="gpt-5.5",
    )

    gate = runtime._build_gate_sandbox(settings)

    assert gate.image == "sandbox-project:sha256-test"
    assert gate.network == "bridge"
    assert gate.dns == ("1.1.1.1", "8.8.8.8")
    assert gate.pip_cache_dir == str(cache) and cache.is_dir()
    assert gate.memory == "7g" and gate.cpus == "3.0" and gate.timeout == 987.0
    assert gate.subscription_auth_dir is None
    assert gate.env == {}
    assert gate.env_passthrough == ()

    argv = gate._build_run_argv("python -m pytest -q", str(tmp_path / "workspace"))
    rendered = " ".join(argv)
    assert "LLM_API_KEY" not in rendered
    assert "GEMINI_API_KEY" not in rendered
    assert "LLM_MODEL" not in rendered
    assert ".openhands" not in rendered


def test_acceptance_checker_receives_durable_project_identity(monkeypatch):
    """Le checker d'exécution recharge l'oracle exact depuis l'état durable."""
    import collegue.executor.quality_gate as quality_gate
    import collegue.pilot.runtime as runtime

    captured = {}

    class _Checker:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(quality_gate, "StoredAcceptanceChecker", _Checker)
    manager = object()

    checker = runtime._build_acceptance_checker(manager, 17)

    assert isinstance(checker, _Checker)
    assert captured == {"manager": manager, "project_id": 17}


def test_build_agent_uses_sdk_coder():
    import collegue.pilot.runtime as runtime
    from collegue.executor import OHSdkAgent

    agent = runtime._build_agent(sandbox=object(), settings_obj=SimpleNamespace(LLM_PROVIDER="gemini", LLM_MODEL="m"))
    assert isinstance(agent, OHSdkAgent)


async def test_runtime_routes_coder_and_gate_to_distinct_sandboxes(monkeypatch, manager, git_repo):
    """Chemin produit : l'agent reçoit le sandbox avec creds, le driver celui sans creds."""
    import collegue.pilot.runtime as runtime

    coder_sandbox = object()
    gate_sandbox = object()
    built_agent = object()
    captured = {}

    monkeypatch.setattr(runtime, "_build_sandbox", lambda _settings: coder_sandbox)
    monkeypatch.setattr(runtime, "_build_gate_sandbox", lambda _settings: gate_sandbox)

    def _fake_build_agent(sandbox, _settings):
        captured["agent_sandbox"] = sandbox
        return built_agent

    async def _fake_run_project(_project_id, _repo_source, _ctx, **kwargs):
        captured["run_kwargs"] = kwargs
        return ProjectRunResult(stop_reason="completed", iterations=0, processed=[])

    monkeypatch.setattr(runtime, "_build_agent", _fake_build_agent)
    monkeypatch.setattr(runtime, "run_project", _fake_run_project)

    pid = _linear(manager, 1)
    await run_project_from_settings(
        pid,
        git_repo,
        owner="o",
        repo="r",
        ctx=object(),
        dry_run=True,
        settings_obj=SimpleNamespace(BUILD_AUTO_MERGE=False),
        manager=manager,
        reviewer=FakeReviewer(),
        clients=_clients(),
        budget=_Budget(),
    )

    assert captured["agent_sandbox"] is coder_sandbox
    assert captured["run_kwargs"]["agent"] is built_agent
    assert captured["run_kwargs"]["sandbox"] is gate_sandbox


async def test_runtime_keeps_injected_sandbox_compatibility_and_allows_gate_override(monkeypatch, manager, git_repo):
    """Un ancien double injecté reste partagé ; gate_sandbox permet une séparation explicite."""
    import collegue.pilot.runtime as runtime

    injected = object()
    explicit_gate = object()
    seen = []

    def _must_not_build(_settings):
        raise AssertionError("aucun sandbox réel ne doit être construit quand un double est injecté")

    async def _fake_run_project(_project_id, _repo_source, _ctx, **kwargs):
        seen.append(kwargs["sandbox"])
        return ProjectRunResult(stop_reason="completed", iterations=0, processed=[])

    monkeypatch.setattr(runtime, "_build_sandbox", _must_not_build)
    monkeypatch.setattr(runtime, "_build_gate_sandbox", _must_not_build)
    monkeypatch.setattr(runtime, "run_project", _fake_run_project)

    common = dict(
        owner="o",
        repo="r",
        ctx=object(),
        dry_run=True,
        settings_obj=SimpleNamespace(BUILD_AUTO_MERGE=False),
        manager=manager,
        sandbox=injected,
        agent=FakeCodeAgent(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        budget=_Budget(),
    )
    pid = _linear(manager, 1)
    await run_project_from_settings(pid, git_repo, **common)
    await run_project_from_settings(pid, git_repo, gate_sandbox=explicit_gate, **common)

    assert seen == [injected, explicit_gate]


# --- ctx de sampling offline (A1) ----------------------------------------------


async def test_builds_offline_ctx_when_none_and_closes(monkeypatch, manager, git_repo):
    """Hors serveur MCP, ``ctx=None`` → un ctx offline est assemblé puis fermé."""
    import collegue.pilot.runtime as runtime

    closed = {"v": False}
    captured = {}

    class _Ctx:
        async def aclose(self):
            closed["v"] = True

    async def _fake_run_project(pid, src, ctx, **kw):
        captured["ctx"] = ctx
        return ProjectRunResult(stop_reason="completed", iterations=0, processed=[])

    monkeypatch.setattr(runtime, "_build_ctx", lambda _s: _Ctx())
    monkeypatch.setattr(runtime, "run_project", _fake_run_project)

    pid = _linear(manager, 1)
    await run_project_from_settings(
        pid,
        git_repo,
        owner="o",
        repo="r",
        dry_run=True,
        manager=manager,
        sandbox=_Sandbox(),
        agent=FakeCodeAgent(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        budget=_Budget(),
    )
    assert isinstance(captured["ctx"], _Ctx)  # ctx assemblé et transmis
    assert closed["v"] is True  # fermé en fin de run (on l'a créé)


async def test_injected_ctx_is_not_closed(monkeypatch, manager, git_repo):
    """Un ctx fourni par l'appelant lui appartient : ``run_project_from_settings`` ne le ferme pas."""
    import collegue.pilot.runtime as runtime

    closed = {"v": False}

    class _Ctx:
        async def aclose(self):
            closed["v"] = True

    async def _fake_run_project(pid, src, ctx, **kw):
        return ProjectRunResult(stop_reason="completed", iterations=0, processed=[])

    monkeypatch.setattr(runtime, "run_project", _fake_run_project)

    pid = _linear(manager, 1)
    mine = _Ctx()
    await run_project_from_settings(
        pid,
        git_repo,
        owner="o",
        repo="r",
        ctx=mine,
        dry_run=True,
        manager=manager,
        sandbox=_Sandbox(),
        agent=FakeCodeAgent(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        budget=_Budget(),
    )
    assert closed["v"] is False  # ctx injecté → non fermé par le runtime


# --- merge-bot de la phase BUILD (#411/#434) ------------------------------------


async def _noop_sleep(_s):
    return None


async def test_merge_in_review_prs_merges_sets_status_and_resyncs(manager, git_repo):
    """Le merge-bot du build merge la PR d'une tâche in_review, la passe `merged`, resync le clone."""
    import collegue.pilot.runtime as runtime

    pid = manager.create_project(name="demo")
    tid = manager.add_task(pid, title="T1")
    manager.update_task_status(tid, "in_review")

    calls = {"merge": [], "git": []}

    class _PRs2:
        def find_pr_by_head(self, owner, repo, head, base=None, state="open"):
            return SimpleNamespace(number=77)

        def merge_pr(self, owner, repo, number, method="squash", expected_head_sha=None):
            calls["merge"].append((number, method))
            return SimpleNamespace(merged=True, already_merged=False)

    class _Runner:
        def run_command(self, cmd, ws):
            calls["git"].append(" ".join(cmd) if isinstance(cmd, list) else cmd)
            return SandboxResult(exit_code=0, stdout="", stderr="")

    clients = PrClients(branches=_Branches(), files=_Files(), prs=_PRs2())
    merged = await runtime._merge_in_review_prs(
        manager,
        clients,
        project_id=pid,
        owner="o",
        repo="r",
        repo_source=git_repo,
        base="main",
        git_runner=_Runner(),
        sleep_fn=_noop_sleep,
    )
    assert merged == 1
    assert calls["merge"] == [(77, "squash")]  # squash
    assert manager.get_task(tid).status == "merged"  # statut avancé
    assert any("fetch origin main" in g for g in calls["git"])  # resync
    assert any("reset --hard origin/main" in g for g in calls["git"])


async def test_build_auto_merge_loops_until_complete(monkeypatch, manager, git_repo):
    """auto-merge ON : driver ↔ merge-bot bouclent jusqu'à `completed` (1 PR mergée par passe)."""
    import collegue.pilot.runtime as runtime

    pid = _linear(manager, 2)
    approve_plan(manager, pid)
    calls = {"run": 0, "merge": 0, "require_merged": []}

    async def _fake_run_project(p, src, ctx, **kw):
        calls["run"] += 1
        calls["require_merged"].append(kw.get("require_merged_deps"))
        if calls["run"] < 3:
            return ProjectRunResult(stop_reason="awaiting_merge", iterations=1, processed=[])
        return ProjectRunResult(stop_reason="completed", iterations=0, processed=[])

    async def _fake_merge(mgr, cli, **kw):
        calls["merge"] += 1
        return 1

    class _Ctx:
        async def aclose(self):
            return None

    monkeypatch.setattr(runtime, "run_project", _fake_run_project)
    monkeypatch.setattr(runtime, "_merge_in_review_prs", _fake_merge)
    monkeypatch.setattr("collegue.executor.openhands_agent.coder_pricing_resolvable", lambda s: True)

    await run_project_from_settings(
        pid,
        git_repo,
        owner="o",
        repo="r",
        dry_run=False,  # active l'auto-merge
        settings_obj=SimpleNamespace(BUILD_AUTO_MERGE=True),
        manager=manager,
        sandbox=_Sandbox(),
        agent=FakeCodeAgent(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        budget=_Budget(),
        ctx=_Ctx(),
        audit=SimpleNamespace(cost_summary=lambda: {"usd": 0.0, "tokens": 0}),
        cost_source=lambda: (0.0, 0),
    )
    assert calls["run"] == 3  # 2 passes awaiting_merge + 1 completed
    assert calls["merge"] == 3  # 2 merges entre passes + 1 drain final (dernière tâche)
    assert all(calls["require_merged"])  # deps strictes forcées en auto-merge


async def test_build_auto_merge_drains_last_pr_on_complete(monkeypatch, manager, git_repo):
    """Drain final : build qui COMPLÈTE direct (sans awaiting_merge) → la dernière PR in_review est mergée."""
    import collegue.pilot.runtime as runtime

    pid = _linear(manager, 1)
    approve_plan(manager, pid)
    calls = {"run": 0, "merge": 0}

    async def _fake_run_project(p, src, ctx, **kw):
        calls["run"] += 1
        return ProjectRunResult(stop_reason="completed", iterations=1, processed=[])

    async def _fake_merge(mgr, cli, **kw):
        calls["merge"] += 1
        return 1

    class _Ctx:
        async def aclose(self):
            return None

    monkeypatch.setattr(runtime, "run_project", _fake_run_project)
    monkeypatch.setattr(runtime, "_merge_in_review_prs", _fake_merge)
    monkeypatch.setattr("collegue.executor.openhands_agent.coder_pricing_resolvable", lambda s: True)

    await run_project_from_settings(
        pid,
        git_repo,
        owner="o",
        repo="r",
        dry_run=False,
        settings_obj=SimpleNamespace(BUILD_AUTO_MERGE=True),
        manager=manager,
        sandbox=_Sandbox(),
        agent=FakeCodeAgent(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        budget=_Budget(),
        ctx=_Ctx(),
        audit=SimpleNamespace(cost_summary=lambda: {"usd": 0.0, "tokens": 0}),
        cost_source=lambda: (0.0, 0),
    )
    assert calls["run"] == 1  # pas d'awaiting_merge → 1 passe
    assert calls["merge"] == 1  # drain final exécuté quand même (dernière PR)


async def test_build_auto_merge_off_single_pass(monkeypatch, manager, git_repo):
    """auto-merge OFF (dry_run) : 1 seule passe, pas de merge-bot (merge humain au prochain run)."""
    import collegue.pilot.runtime as runtime

    pid = _linear(manager, 2)
    calls = {"run": 0, "merge": 0}

    async def _fake_run_project(p, src, ctx, **kw):
        calls["run"] += 1
        return ProjectRunResult(stop_reason="awaiting_merge", iterations=1, processed=[])

    async def _fake_merge(mgr, cli, **kw):
        calls["merge"] += 1
        return 1

    monkeypatch.setattr(runtime, "run_project", _fake_run_project)
    monkeypatch.setattr(runtime, "_merge_in_review_prs", _fake_merge)

    await _run(manager, git_repo, pid, dry_run=True)
    assert calls["run"] == 1  # pas de boucle en dry_run
    assert calls["merge"] == 0  # aucun auto-merge


# --- reporting ------------------------------------------------------------------


def test_format_run_report_contents():
    result = ProjectRunResult(
        stop_reason="completed",
        iterations=1,
        processed=[TaskOutcome(task_id=5, title="Faire X", success=True, stage="pr", pr_number=42)],
        project_status="improving",
    )
    report = format_run_report(result, project_id=7, budget=_Budget())
    assert "Arrêt : completed" in report
    assert "#5 Faire X (pr) → PR #42" in report
    assert "improving" in report
    assert "illimité" in report  # budget sans deadline


def test_format_run_report_no_prs_and_no_budget():
    result = ProjectRunResult(stop_reason="blocked", iterations=0, processed=[], project_status=None)
    report = format_run_report(result)
    assert "PRs ouvertes : (aucune)" in report
    assert "Statut projet : (inchangé)" in report
    assert "Budget-temps restant : n/a" in report


# --- durabilité du plafond budget (#406) ------------------------------------------


def test_home_durability_warns_on_relative_home_with_hard_budget(monkeypatch):
    monkeypatch.delenv("COLLEGUE_HOME", raising=False)
    s = SimpleNamespace(MAX_COST_USD=5.0, MAX_TOKENS_BUDGET=0)
    msg = collegue_home_durability_warning(s)
    assert msg is not None and "COLLEGUE_HOME" in msg
    monkeypatch.setenv("COLLEGUE_HOME", "rel/.collegue")  # relatif explicite → idem
    assert collegue_home_durability_warning(s) is not None


def test_home_durability_silent_when_absolute_home(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLEGUE_HOME", str(tmp_path))
    s = SimpleNamespace(MAX_COST_USD=0.0, MAX_TOKENS_BUDGET=100_000)
    assert collegue_home_durability_warning(s) is None


def test_home_durability_silent_without_hard_budget(monkeypatch):
    # Pas de plafond dur configuré → rien à perdre, pas de bruit.
    monkeypatch.delenv("COLLEGUE_HOME", raising=False)
    s = SimpleNamespace(MAX_COST_USD=0.0, MAX_TOKENS_BUDGET=0)
    assert collegue_home_durability_warning(s) is None


# --- CLI parser -----------------------------------------------------------------


def test_parser_defaults_dry_run():
    ns = build_parser().parse_args(["--project-id", "3", "--repo-source", "/r", "--owner", "o", "--repo", "app"])
    assert ns.project_id == 3
    assert ns.repo_source == "/r"
    assert ns.owner == "o" and ns.repo == "app"
    assert ns.base == "main"
    assert ns.execute is False  # dry_run par défaut
    assert ns.max_iterations is None


def test_parser_execute_and_overrides():
    ns = build_parser().parse_args(
        [
            "--project-id",
            "1",
            "--repo-source",
            "/r",
            "--owner",
            "o",
            "--repo",
            "a",
            "--execute",
            "--base",
            "dev",
            "--max-iterations",
            "5",
        ]
    )
    assert ns.execute is True
    assert ns.base == "dev"
    assert ns.max_iterations == 5


def test_parser_requires_mandatory_args():
    # #506 : la validation des args du run a migré d'argparse vers main() pour
    # cohabiter avec les sous-commandes opérateur (argparse ne peut pas conditionner
    # `required` sur l'absence de sous-commande). parse_args réussit désormais ;
    # le SystemExit est levé par main() (parser.error) quand un args requis manque.
    import collegue.pilot.__main__ as cli

    ns = build_parser().parse_args(["--owner", "o"])  # plus de SystemExit ici
    assert ns.command is None and ns.project_id is None
    with pytest.raises(SystemExit):
        cli.main(["--owner", "o"])  # manque project-id/repo-source/repo


# --- CLI sous-commandes opérateur (#506) ----------------------------------------


def test_parser_task_requeue_subcommand():
    ns = build_parser().parse_args(["task", "requeue", "42", "--message", "boom"])
    assert ns.command == "task"
    assert ns.task_command == "requeue"
    assert ns.task_id == 42
    assert ns.message == "boom"


def test_parser_task_reset_subcommand_defaults_todo():
    ns = build_parser().parse_args(["task", "reset", "7", "--message", "m"])
    assert ns.task_command == "reset"
    assert ns.task_id == 7
    assert ns.status == "todo"
    ns2 = build_parser().parse_args(["task", "reset", "7", "--message", "m", "--status", "blocked"])
    assert ns2.status == "blocked"


def test_parser_task_requeue_requires_message():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["task", "requeue", "1"])  # --message obligatoire


def test_parser_default_run_has_no_command():
    # rétro-compat : sans sous-commande, command reste None (chemin run inchangé).
    ns = build_parser().parse_args(["--project-id", "3", "--repo-source", "/r", "--owner", "o", "--repo", "app"])
    assert ns.command is None


def test_main_task_command_traces_decision(monkeypatch, tmp_path):
    # Glue _run_task_command end-to-end SANS Docker/réseau : manager SQLite en mémoire,
    # injecté via monkeypatch de runtime._build_manager + runtime._settings.
    import collegue.pilot.__main__ as cli
    import collegue.pilot.runtime as runtime
    from collegue.state import ProjectStateManager

    url = f"sqlite:///{tmp_path / 'state.db'}"
    manager = ProjectStateManager.from_url(url, create=True)
    pid = manager.create_project(name="p")
    tid = manager.add_task(pid, title="T")
    manager.update_task(tid, status="failed")

    monkeypatch.setattr(runtime, "_settings", lambda: SimpleNamespace(STATE_DATABASE_URL=url))
    monkeypatch.setattr(runtime, "_build_manager", lambda _s: manager)

    rc = cli.main(["task", "reset", str(tid), "--message", "reset post-incident"])
    assert rc == 0
    assert manager.get_task(tid).status == "todo"
    assert any(getattr(d, "summary", "") == "[run] operator_reset" for d in manager.get_decisions(pid))


def test_main_task_command_unknown_task_returns_1(monkeypatch, tmp_path):
    import collegue.pilot.__main__ as cli
    import collegue.pilot.runtime as runtime
    from collegue.state import ProjectStateManager

    url = f"sqlite:///{tmp_path / 'state.db'}"
    manager = ProjectStateManager.from_url(url, create=True)
    monkeypatch.setattr(runtime, "_settings", lambda: SimpleNamespace(STATE_DATABASE_URL=url))
    monkeypatch.setattr(runtime, "_build_manager", lambda _s: manager)
    assert cli.main(["task", "reset", "9999", "--message", "x"]) == 1


# --- CLI main (glue + codes de sortie) ------------------------------------------

_CLI_ARGS = ["--project-id", "1", "--repo-source", "/r", "--owner", "o", "--repo", "a"]


def _patch_run(monkeypatch, result):
    import collegue.pilot.__main__ as cli

    async def _fake(*args, **kwargs):
        return result

    monkeypatch.setattr(cli, "run_project_from_settings", _fake)
    return cli


def test_main_returns_0_on_completed(monkeypatch, capsys):
    cli = _patch_run(
        monkeypatch,
        ProjectRunResult(stop_reason="completed", iterations=1, processed=[], project_status="improving"),
    )
    assert cli.main(_CLI_ARGS) == 0
    assert "Rapport du pilote" in capsys.readouterr().out


def test_main_returns_1_on_blocked(monkeypatch):
    cli = _patch_run(monkeypatch, ProjectRunResult(stop_reason="blocked", iterations=0, processed=[]))
    assert cli.main(_CLI_ARGS) == 1


def test_gate_options_built_from_settings():
    # #437/#438/#439 : la config GATE_* devient les kwargs du gate (vide → défauts).
    from collegue.pilot.runtime import _gate_options

    custom = SimpleNamespace(
        GATE_FRONTEND=False,
        GATE_TEST_COMMAND="npm run check",
        GATE_REQUIRE_DEPS_INSTALL=True,
        GATE_CHECK_INSTALLABILITY=True,
        GATE_REQUIRE_TEST_CHANGES=True,
    )
    assert _gate_options(custom) == {
        "frontend_gate": False,
        "test_command": "npm run check",
        "require_deps_install": True,
        "check_installability": True,
        "require_test_changes": True,
    }
    defaults = SimpleNamespace(GATE_TEST_COMMAND="")
    assert _gate_options(defaults) == {
        "frontend_gate": True,
        "require_deps_install": False,
        "check_installability": False,
        "require_test_changes": False,
    }
    # GATE_ADEQUACY (opt-in) câble un checker LLM dans les options.
    with_adequacy = SimpleNamespace(GATE_ADEQUACY=True)
    options = _gate_options(with_adequacy)
    assert options["adequacy_checker"] is not None

    # GATE_SMOKE_RUN (#458, opt-in) : commande + chemins sondés câblés.
    with_smoke = SimpleNamespace(
        GATE_SMOKE_RUN=True,
        GATE_SMOKE_COMMAND="python serve.py",
        GATE_SMOKE_PATHS="/health, /factures/",
    )
    options = _gate_options(with_smoke)
    assert options["smoke_run"] is True
    assert options["smoke_command"] == "python serve.py"
    assert options["smoke_paths"] == ("/health", "/factures/")
    assert options["smoke_timeout"] == 30.0

    # #483 : le préfixe « MÉTHODE: » traverse le mapping config sans altération
    # (le parsing vit dans la sonde, pas ici).
    with_post = SimpleNamespace(GATE_SMOKE_RUN=True, GATE_SMOKE_PATHS="/, POST:/auth/register")
    assert _gate_options(with_post)["smoke_paths"] == ("/", "POST:/auth/register")
    assert "smoke_run" not in _gate_options(SimpleNamespace())  # défaut : off


# --- isolation ------------------------------------------------------------------


def test_app_wires_only_the_gated_pilot_tool_no_autostart():
    # Depuis H6 (Phase 5), app.py expose le pilote en outil MCP — MAIS uniquement via
    # ``register_pilot_tool`` (gaté, off par défaut). L'invariant de sûreté n'est plus
    # « aucune référence au pilote » mais « aucun AUTO-RUN » : app.py ne doit jamais
    # appeler ``run_project``/``run_project_from_settings`` directement au démarrage.
    app_src = (Path(__file__).resolve().parent.parent / "collegue" / "app.py").read_text(encoding="utf-8")
    assert "register_pilot_tool" in app_src  # l'outil gaté est bien câblé
    assert "run_project_from_settings(" not in app_src  # mais aucun run lancé au boot
    assert "run_project(" not in app_src


def test_importing_pilot_does_not_pull_openhands_runtime():
    import collegue.pilot  # noqa: F401

    assert not any(name == "openhands" or name.startswith("openhands.") for name in sys.modules)


def test_gate_fix_requirements_opt_out_emitted_only_on_deviation():
    """#481 : la remédiation requirements est ON par défaut côté gate — la clé
    n'apparaît dans les options qu'en opt-out (les défauts restent inchangés,
    cf. les assertions d'égalité stricte ci-dessus)."""
    from types import SimpleNamespace

    from collegue.pilot.runtime import _gate_options

    assert "fix_missing_requirements" not in _gate_options(SimpleNamespace(GATE_TEST_COMMAND=""))
    options = _gate_options(SimpleNamespace(GATE_FIX_REQUIREMENTS=False))
    assert options["fix_missing_requirements"] is False


def test_gate_requirements_append_only_opt_out_emitted_only_on_deviation():
    """#482 : garde append-only ON par défaut côté gate — clé émise seulement
    en opt-out (défauts inchangés)."""
    from types import SimpleNamespace

    from collegue.pilot.runtime import _gate_options

    assert "requirements_guard" not in _gate_options(SimpleNamespace(GATE_TEST_COMMAND=""))
    assert _gate_options(SimpleNamespace(GATE_REQUIREMENTS_APPEND_ONLY=False))["requirements_guard"] is False


def test_sandbox_dns_parsed_from_settings():
    """#485 : SANDBOX_DNS (IPs séparées par des virgules) → tuple pour DockerSandbox."""
    from types import SimpleNamespace

    from collegue.pilot.runtime import _sandbox_dns

    assert _sandbox_dns(SimpleNamespace(SANDBOX_DNS="1.1.1.1, 8.8.8.8")) == ("1.1.1.1", "8.8.8.8")
    assert _sandbox_dns(SimpleNamespace(SANDBOX_DNS="")) == ()
    assert _sandbox_dns(SimpleNamespace()) == ()  # setting absent → défaut Docker


def test_gate_pin_guard_opt_out_emitted_only_on_deviation():
    """#497 : signal deps non épinglées ON par défaut — clé émise seulement en opt-out."""
    from types import SimpleNamespace

    from collegue.pilot.runtime import _gate_options

    assert "pin_guard" not in _gate_options(SimpleNamespace(GATE_TEST_COMMAND=""))
    assert _gate_options(SimpleNamespace(GATE_PIN_GUARD=False))["pin_guard"] is False


def test_gate_forbidden_files_opt_out_and_block_opt_in():
    """#508 : garde fichiers parasites ON par défaut (clé émise en opt-out) ;
    le mode bloquant est opt-in (clé émise seulement quand activé)."""
    from types import SimpleNamespace

    from collegue.pilot.runtime import _gate_options

    # défaut : aucune clé #508 (préserve l'égalité stricte du chemin défaut)
    assert "forbidden_files_guard" not in _gate_options(SimpleNamespace(GATE_TEST_COMMAND=""))
    assert "forbidden_files_block" not in _gate_options(SimpleNamespace(GATE_TEST_COMMAND=""))
    # opt-out de la garde
    assert _gate_options(SimpleNamespace(GATE_FORBIDDEN_FILES=False))["forbidden_files_guard"] is False
    # opt-in du mode bloquant
    assert _gate_options(SimpleNamespace(GATE_FORBIDDEN_FILES_BLOCK=True))["forbidden_files_block"] is True


def test_gate_smoke_cors_origin_emitted_only_on_override():
    """#503 : le défaut CORS vit dans la signature du gate — la clé runtime n'est
    émise qu'en override explicite (préserve l'égalité stricte du chemin défaut)."""
    from types import SimpleNamespace

    from collegue.pilot.runtime import _gate_options

    assert "smoke_cors_origin" not in _gate_options(SimpleNamespace(GATE_SMOKE_RUN=True))
    over = SimpleNamespace(GATE_SMOKE_RUN=True, GATE_SMOKE_CORS_ORIGIN="https://app.example")
    assert _gate_options(over)["smoke_cors_origin"] == "https://app.example"
    off = SimpleNamespace(GATE_SMOKE_RUN=True, GATE_SMOKE_CORS_ORIGIN="")
    assert _gate_options(off)["smoke_cors_origin"] == ""


def test_sandbox_pip_cache_parsed_from_settings(tmp_path):
    """#496 : SANDBOX_PIP_CACHE_DIR → chemin créé ; vide/absent → None."""
    from types import SimpleNamespace

    from collegue.pilot.runtime import _sandbox_pip_cache

    target = tmp_path / "pipcache"
    assert _sandbox_pip_cache(SimpleNamespace(SANDBOX_PIP_CACHE_DIR=str(target))) == str(target)
    assert target.is_dir()  # créé (writable par l'uid hôte)
    assert _sandbox_pip_cache(SimpleNamespace(SANDBOX_PIP_CACHE_DIR="")) is None
    assert _sandbox_pip_cache(SimpleNamespace()) is None


def test_sandbox_subscription_auth_parsed_from_settings(tmp_path):
    """Creds d'abonnement : SANDBOX_SUBSCRIPTION_AUTH_DIR → chemin (NON créé) ; vide → None."""
    from types import SimpleNamespace

    from collegue.pilot.runtime import _sandbox_subscription_auth

    target = tmp_path / "openhands"
    # ne crée PAS le dossier (creds doivent préexister, login fait en amont)
    assert _sandbox_subscription_auth(SimpleNamespace(SANDBOX_SUBSCRIPTION_AUTH_DIR=str(target))) == str(target)
    assert not target.exists()
    assert _sandbox_subscription_auth(SimpleNamespace(SANDBOX_SUBSCRIPTION_AUTH_DIR="")) is None
    assert _sandbox_subscription_auth(SimpleNamespace()) is None
