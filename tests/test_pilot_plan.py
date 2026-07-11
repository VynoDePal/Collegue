"""Tests A2 — planification par le produit : runtime.plan_project_from_settings + CLI `plan`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from collegue.pilot import (
    PlanResult,
    approve_project_plan_from_settings,
    format_plan_report,
    plan_project_from_settings,
    sync_project_plan_from_settings,
)

# --- doubles du planner (monkeypatchés là où plan_project_from_settings les importe) ---


def _spec(title="T", objectives=("o1",), criteria=("c1", "c2")):
    return SimpleNamespace(title=title, objectives=list(objectives), acceptance_criteria=list(criteria))


def _patch_planner(
    monkeypatch,
    *,
    tasks=None,
    decompose_fn=None,
    acceptance_fn=None,
    sync_issues=None,
    calls=None,
):
    calls = calls if calls is not None else {}
    order = calls.setdefault("order", [])

    async def _generate_spec(problem, ctx, **kw):
        order.append("generate")
        calls["generate"] = (problem, kw)
        return _spec()

    def _persist_spec(manager, name, spec, **kw):
        order.append("persist")
        calls["persist"] = (name, kw)
        return 42

    async def _decompose(spec, ctx, **kw):
        order.append("decompose")
        calls["decompose"] = kw
        return list(tasks if tasks is not None else [object(), object()])

    async def _acceptance(spec, task_list, ctx, **kw):
        order.append("acceptance")
        calls["acceptance"] = {"spec": spec, "tasks": task_list, **kw}
        return {1: {"source": "def test_x():\n    assert True\n", "provenance": {"role": "qa"}}}

    def _build_preview(manager, project_id):
        order.append("preview")
        return SimpleNamespace(
            to_markdown=lambda: "PREVIEW",
            plan_hash="a" * 64,
            tasks=[],
            task_count=len(tasks if tasks is not None else [object(), object()]),
        )

    def _current_hash(manager, project_id):
        calls["current_hash"] = project_id
        return "a" * 64

    def _approve(manager, project_id, **kw):
        order.append("approve")
        calls["approve"] = (project_id, kw)
        return True

    def _require(manager, project_id):
        order.append("require")
        calls["require"] = project_id
        if "approve" not in calls:
            raise PlanNotApproved("approbation explicite absente")

    def _sync(manager, project_id, owner, repo, **kw):
        order.append("sync")
        calls["sync"] = {"owner": owner, "repo": repo, **kw}
        issues = sync_issues if sync_issues is not None else [{"task_id": 1, "title": "T1", "issue_number": None}]
        return SimpleNamespace(issues=issues)

    monkeypatch.setattr("collegue.planner.spec_generator.generate_spec", _generate_spec)
    monkeypatch.setattr("collegue.planner.spec_generator.persist_spec", _persist_spec)
    monkeypatch.setattr("collegue.planner.decomposer.decompose", decompose_fn or _decompose)
    monkeypatch.setattr(
        "collegue.planner.acceptance_tests.generate_acceptance_tests",
        acceptance_fn or _acceptance,
    )
    monkeypatch.setattr("collegue.planner.plan_review.build_plan_preview", _build_preview)
    monkeypatch.setattr("collegue.planner.plan_review.current_plan_hash", _current_hash)
    monkeypatch.setattr("collegue.planner.plan_review.approve_plan", _approve)
    monkeypatch.setattr("collegue.planner.plan_review.require_approved", _require)
    monkeypatch.setattr("collegue.planner.github_sync.sync_plan", _sync)
    return calls


async def _plan(monkeypatch, **kwargs):
    closed = {"v": False}

    class _Ctx:
        async def aclose(self):
            closed["v"] = True

    defaults = dict(
        name="proj",
        problem="construire X",
        owner="o",
        repo="r",
        ctx=_Ctx(),
        settings_obj=SimpleNamespace(),
        manager=object(),
    )
    defaults.update(kwargs)
    result = await plan_project_from_settings(**defaults)
    return result, closed


# --- orchestration --------------------------------------------------------------


async def test_dry_run_does_not_approve_and_syncs_in_dry_run(monkeypatch):
    calls = _patch_planner(monkeypatch)
    result, _ = await _plan(monkeypatch)
    assert isinstance(result, PlanResult)
    assert result.project_id == 42
    assert result.spec_title == "T"
    assert result.acceptance_criteria == 2
    assert result.task_count == 2
    assert result.dry_run is True
    assert result.preview_markdown == "PREVIEW"
    assert result.action == "draft"
    assert result.plan_hash == "a" * 64
    assert "approve" not in calls
    assert "sync" not in calls  # un draft ne touche même pas la couche GitHub
    assert calls["persist"][1]["plan_sync_config"] == {
        "owner": "o",
        "repo": "r",
        "labels": ["autonome"],
        "milestone_title": "proj MVP",
        "board_title": None,
        "spec_filename": "SPEC.md",
        "base_branch": "main",
    }


@pytest.mark.parametrize("legacy", [{"approve": True}, {"execute_sync": True}, {"approve": True, "execute_sync": True}])
async def test_legacy_one_shot_is_rejected_before_ctx_or_state(monkeypatch, legacy):
    monkeypatch.setattr(
        "collegue.pilot.runtime._build_ctx",
        lambda _settings: pytest.fail("le one-shot ne doit pas construire de ctx"),
    )
    with pytest.raises(ValueError, match="plan approve"):
        await plan_project_from_settings(
            "p",
            "x",
            owner="o",
            repo="r",
            settings_obj=SimpleNamespace(),
            manager=object(),
            **legacy,
        )


async def test_acceptance_artifacts_are_generated_before_preview_and_approval(monkeypatch):
    tasks = [SimpleNamespace(id=1, title="T", acceptance="A", depends_on=[])]
    calls = _patch_planner(monkeypatch, tasks=tasks)

    await _plan(
        monkeypatch,
        settings_obj=SimpleNamespace(GATE_ACCEPTANCE_TESTS=True),
    )

    assert calls["acceptance"]["tasks"] == tasks
    assert calls["acceptance"]["project_id"] == 42
    assert calls["order"].index("decompose") < calls["order"].index("acceptance")
    assert calls["order"].index("acceptance") < calls["order"].index("preview")


async def test_acceptance_generation_failure_prevents_approval_and_sync(monkeypatch):
    async def _boom(*args, **kwargs):
        raise ValueError("oracle invalide")

    calls = _patch_planner(
        monkeypatch,
        tasks=[SimpleNamespace(id=1, title="T", acceptance="A", depends_on=[])],
        acceptance_fn=_boom,
    )
    with pytest.raises(ValueError, match="oracle invalide"):
        await _plan(
            monkeypatch,
            settings_obj=SimpleNamespace(GATE_ACCEPTANCE_TESTS=True),
        )
    assert "approve" not in calls
    assert "sync" not in calls


async def test_decompose_max_tokens_widened(monkeypatch):
    calls = _patch_planner(monkeypatch)
    await _plan(monkeypatch, decompose_max_tokens=16384)
    assert calls["decompose"]["max_tokens"] == 16384


async def test_decompose_retries_on_empty_then_succeeds(monkeypatch):
    attempts = {"n": 0}

    async def _flaky(spec, ctx, **kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ValueError("Décomposition vide")
        return [object()]

    _patch_planner(monkeypatch, decompose_fn=_flaky)
    result, _ = await _plan(monkeypatch, retry_sleep_seconds=0)
    assert attempts["n"] == 2  # 1 échec + 1 succès
    assert result.task_count == 1


async def test_decompose_raises_after_all_attempts(monkeypatch):
    async def _always_empty(spec, ctx, **kw):
        raise ValueError("Décomposition vide")

    _patch_planner(monkeypatch, decompose_fn=_always_empty)
    with pytest.raises(ValueError):
        await _plan(monkeypatch, decompose_attempts=2, retry_sleep_seconds=0)


async def test_owned_ctx_is_closed(monkeypatch):
    _patch_planner(monkeypatch)
    closed = {"v": False}

    class _Ctx:
        async def aclose(self):
            closed["v"] = True

    monkeypatch.setattr("collegue.pilot.runtime._build_ctx", lambda _s: _Ctx())
    # ctx non fourni → assemblé puis fermé
    await plan_project_from_settings(
        name="p", problem="x", owner="o", repo="r", settings_obj=SimpleNamespace(), manager=object()
    )
    assert closed["v"] is True


async def test_injected_ctx_not_closed(monkeypatch):
    _patch_planner(monkeypatch)
    result, closed = await _plan(monkeypatch)  # _plan injecte un ctx
    assert closed["v"] is False


def _stored_manager(*, config=None):
    project = SimpleNamespace(
        id=7,
        name="Durable",
        plan_sync_config=config,
    )
    tasks = [SimpleNamespace(id=11, acceptance="AC", title="T", depends_on=[])]
    return SimpleNamespace(
        get_project=lambda project_id: project if project_id == 7 else None,
        get_tasks=lambda project_id: tasks if project_id == 7 else [],
    )


def _patch_stored_report(monkeypatch):
    monkeypatch.setattr(
        "collegue.planner.plan_review.load_plan_snapshot",
        lambda manager, project_id, **kwargs: SimpleNamespace(
            project_id=project_id,
            plan_sync_config=manager.get_project(project_id).plan_sync_config,
            approved=True,
        ),
    )
    monkeypatch.setattr(
        "collegue.planner.plan_review.build_plan_preview",
        lambda manager, project_id: SimpleNamespace(
            to_markdown=lambda: "STORED PREVIEW",
            plan_hash="b" * 64,
            tasks=[{"acceptance": "AC"}],
            task_count=1,
        ),
    )
    monkeypatch.setattr("collegue.planner.plan_review.current_plan_hash", lambda manager, project_id: "b" * 64)


def test_approve_existing_plan_uses_expected_hash_without_ctx_or_github(monkeypatch):
    manager = _stored_manager(config={})
    _patch_stored_report(monkeypatch)
    calls = {}

    def _approve(manager_arg, project_id, **kwargs):
        calls.update(project_id=project_id, **kwargs)
        return True

    monkeypatch.setattr("collegue.planner.plan_review.approve_plan", _approve)
    monkeypatch.setattr(
        "collegue.pilot.runtime._build_ctx",
        lambda _settings: pytest.fail("approve ne doit pas construire de contexte LLM"),
    )
    monkeypatch.setattr(
        "collegue.pilot.runtime._build_clients",
        lambda _token: pytest.fail("approve ne doit pas construire de client GitHub"),
    )

    result = approve_project_plan_from_settings(
        7,
        "a" * 64,
        settings_obj=SimpleNamespace(),
        manager=manager,
    )

    assert calls == {
        "project_id": 7,
        "actor": "operator:collegue-cli",
        "expected_plan_hash": "a" * 64,
        "require_target": True,
    }
    assert result.action == "approve"
    assert result.plan_hash == "b" * 64


@pytest.mark.parametrize("bad_hash", ["", "abc", "A" * 64, "g" * 64, "0" * 63, "0" * 65])
def test_approve_rejects_malformed_hash_before_opening_state(monkeypatch, bad_hash):
    monkeypatch.setattr(
        "collegue.pilot.runtime._settings",
        lambda: pytest.fail("un hash invalide doit être refusé avant la configuration"),
    )
    with pytest.raises(ValueError, match="SHA-256"):
        approve_project_plan_from_settings(7, bad_hash)


@pytest.mark.parametrize("execute", [False, True])
def test_sync_reloads_persisted_target_without_ctx_or_cli_override(monkeypatch, execute):
    config = {
        "owner": "persisted-owner",
        "repo": "persisted-repo",
        "labels": ["persisted"],
        "milestone_title": "Persisted milestone",
        "board_title": "Persisted board",
        "spec_filename": "docs/CONTRACT.md",
        "base_branch": "develop",
    }
    manager = _stored_manager(config=config)
    _patch_stored_report(monkeypatch)
    calls = {}

    def _sync(manager_arg, project_id, owner, repo, **kwargs):
        calls.update(project_id=project_id, owner=owner, repo=repo, **kwargs)
        return SimpleNamespace(issues=[{"task_id": 11, "title": "T", "issue_number": 101 if execute else None}])

    monkeypatch.setattr("collegue.planner.github_sync.sync_plan", _sync)
    monkeypatch.setattr(
        "collegue.pilot.runtime._build_ctx",
        lambda _settings: pytest.fail("sync ne doit pas construire de contexte LLM"),
    )

    sentinel_clients = object()
    result = sync_project_plan_from_settings(
        7,
        execute=execute,
        settings_obj=SimpleNamespace(),
        manager=manager,
        github_token="token",
        clients=sentinel_clients,
    )

    assert calls["owner"] == "persisted-owner"
    assert calls["repo"] == "persisted-repo"
    assert calls["labels"] == ["persisted"]
    assert calls["milestone_title"] == "Persisted milestone"
    assert calls["board_title"] == "Persisted board"
    assert calls["spec_filename"] == "docs/CONTRACT.md"
    assert calls["base_branch"] == "develop"
    assert calls["dry_run"] is (not execute)
    assert calls["require_spec_commit"] is execute
    assert calls["clients"] is sentinel_clients
    assert result.dry_run is (not execute)
    assert result.action == "sync"


# --- reporting -----------------------------------------------------------------


def test_format_plan_report_dry_run_and_issues():
    result = PlanResult(
        project_id=7,
        spec_title="Facturation",
        objectives=3,
        acceptance_criteria=5,
        task_count=2,
        preview_markdown="# Plan détaillé\n- tâche A",
        dry_run=True,
        issues=[{"task_id": 1, "title": "Schéma", "issue_number": None}],
    )
    report = format_plan_report(result)
    assert "Plan projet #7" in report
    assert "Facturation" in report
    assert "dry-run" in report
    assert "task 1: Schéma" in report
    assert "Aperçu du plan" in report  # l'opérateur voit le plan avant d'approuver
    assert "# Plan détaillé" in report


def test_format_plan_report_execute_shows_issue_numbers():
    result = PlanResult(
        project_id=8,
        spec_title="X",
        objectives=1,
        acceptance_criteria=1,
        task_count=1,
        preview_markdown="",
        dry_run=False,
        issues=[{"task_id": 9, "title": "Auth", "issue_number": 123}],
    )
    report = format_plan_report(result)
    assert "EXECUTE" in report
    assert "[#123] task 9: Auth" in report


def test_format_stored_plan_report_does_not_invent_spec_counts():
    result = PlanResult(
        project_id=9,
        spec_title="Durable",
        objectives=0,
        acceptance_criteria=0,
        task_count=2,
        preview_markdown="# Plan durable",
        dry_run=True,
        action="approve",
        spec_counts_available=False,
    )

    report = format_plan_report(result)

    assert "relu depuis l'état durable" in report
    assert "0 objectif" not in report


# --- CLI -----------------------------------------------------------------------


def test_cli_parses_plan_subcommand():
    from collegue.pilot.__main__ import build_parser

    args = build_parser().parse_args(["plan", "--problem", "P", "--owner", "o", "--repo", "r"])
    assert args.command == "plan"
    assert args.plan_action == "draft"
    assert args.problem == "P" and args.owner == "o" and args.repo == "r"
    assert args.execute is False


def test_cli_parses_approve_and_sync_actions():
    from collegue.pilot.__main__ import build_parser

    parser = build_parser()
    approve = parser.parse_args(["plan", "approve", "--project-id", "7", "--expected-plan-hash", "abc"])
    sync = parser.parse_args(["plan", "sync", "--project-id", "7", "--execute"])
    assert approve.plan_action == "approve" and approve.project_id == 7
    assert approve.expected_plan_hash == "abc"
    assert sync.plan_action == "sync" and sync.execute is True


def test_cli_plan_dispatches_and_parses_args(monkeypatch, capsys):
    import collegue.pilot.__main__ as cli

    captured = {}

    async def _fake_plan(name, problem, **kw):
        captured["name"] = name
        captured["problem"] = problem
        captured.update(kw)
        return PlanResult(
            project_id=1,
            spec_title="X",
            objectives=0,
            acceptance_criteria=0,
            task_count=0,
            preview_markdown="",
            dry_run=False,
            issues=[],
        )

    monkeypatch.setattr("collegue.pilot.runtime.plan_project_from_settings", _fake_plan)
    monkeypatch.setattr("collegue.pilot.runtime.format_plan_report", lambda r: "REPORT")

    rc = cli.main(["plan", "--name", "X", "--problem", "P", "--owner", "o", "--repo", "r", "--labels", "a, b"])
    assert rc == 0
    assert captured["name"] == "X" and captured["problem"] == "P"
    assert captured["owner"] == "o" and captured["repo"] == "r"
    assert captured["labels"] == ["a", "b"]  # CSV nettoyé
    assert captured["deadline"] is None  # pas de --deadline-hours
    assert "REPORT" in capsys.readouterr().out


def test_cli_approve_and_sync_dispatch_without_async_planner(monkeypatch, capsys):
    import collegue.pilot.__main__ as cli

    calls = []
    result = PlanResult(7, "X", 0, 0, 0, "", True)

    def _approve(project_id, expected_hash):
        calls.append(("approve", project_id, expected_hash))
        return result

    def _sync(project_id, *, execute=False):
        calls.append(("sync", project_id, execute))
        return result

    monkeypatch.setattr("collegue.pilot.runtime.approve_project_plan_from_settings", _approve)
    monkeypatch.setattr("collegue.pilot.runtime.sync_project_plan_from_settings", _sync)
    monkeypatch.setattr("collegue.pilot.runtime.format_plan_report", lambda r: "REPORT")

    assert cli.main(["plan", "approve", "--project-id", "7", "--expected-plan-hash", "abc"]) == 0
    assert cli.main(["plan", "sync", "--project-id", "7", "--execute"]) == 0
    assert calls == [("approve", 7, "abc"), ("sync", 7, True)]
    assert capsys.readouterr().out.count("REPORT") == 2


@pytest.mark.parametrize(
    "argv, message",
    [
        (["plan", "approve", "--project-id", "7"], "--expected-plan-hash"),
        (["plan", "sync"], "--project-id"),
        (["plan", "sync", "--project-id", "7", "--owner", "override"], "cible scellée"),
        (["plan", "sync", "--project-id", "7", "--name", "override"], "cible scellée"),
        (["plan", "--problem", "P", "--owner", "o"], "--repo"),
    ],
)
def test_cli_validates_plan_action_specific_arguments(argv, message, capsys):
    from collegue.pilot.__main__ import main

    with pytest.raises(SystemExit, match="2"):
        main(argv)
    assert message in capsys.readouterr().err
