"""Tests A2 — planification par le produit : runtime.plan_project_from_settings + CLI `plan`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from collegue.pilot import PlanResult, format_plan_report, plan_project_from_settings

# --- doubles du planner (monkeypatchés là où plan_project_from_settings les importe) ---


def _spec(title="T", objectives=("o1",), criteria=("c1", "c2")):
    return SimpleNamespace(title=title, objectives=list(objectives), acceptance_criteria=list(criteria))


def _patch_planner(monkeypatch, *, tasks=None, decompose_fn=None, sync_issues=None, calls=None):
    calls = calls if calls is not None else {}

    async def _generate_spec(problem, ctx, **kw):
        calls["generate"] = (problem, kw)
        return _spec()

    def _persist_spec(manager, name, spec, **kw):
        calls["persist"] = (name, kw)
        return 42

    async def _decompose(spec, ctx, **kw):
        calls["decompose"] = kw
        return list(tasks if tasks is not None else [object(), object()])

    def _build_preview(manager, project_id):
        return SimpleNamespace(to_markdown=lambda: "PREVIEW")

    def _approve(manager, project_id, **kw):
        calls["approve"] = (project_id, kw)
        return True

    def _sync(manager, project_id, owner, repo, **kw):
        calls["sync"] = {"owner": owner, "repo": repo, **kw}
        issues = sync_issues if sync_issues is not None else [{"task_id": 1, "title": "T1", "issue_number": None}]
        return SimpleNamespace(issues=issues)

    monkeypatch.setattr("collegue.planner.spec_generator.generate_spec", _generate_spec)
    monkeypatch.setattr("collegue.planner.spec_generator.persist_spec", _persist_spec)
    monkeypatch.setattr("collegue.planner.decomposer.decompose", decompose_fn or _decompose)
    monkeypatch.setattr("collegue.planner.plan_review.build_plan_preview", _build_preview)
    monkeypatch.setattr("collegue.planner.plan_review.approve_plan", _approve)
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
    assert "approve" not in calls  # ni approve ni execute → pas d'approbation
    assert calls["sync"]["dry_run"] is True
    assert calls["sync"]["labels"] == ["autonome"]  # défaut
    assert calls["sync"]["milestone_title"] == "proj MVP"  # défaut


async def test_execute_sync_approves_and_creates(monkeypatch):
    calls = _patch_planner(monkeypatch, sync_issues=[{"task_id": 1, "title": "T1", "issue_number": 101}])
    result, _ = await _plan(monkeypatch, execute_sync=True, labels=["x"], milestone_title="M")
    assert result.dry_run is False
    assert calls["approve"][0] == 42  # gate P5 satisfait
    assert calls["sync"]["dry_run"] is False
    assert calls["sync"]["labels"] == ["x"]
    assert calls["sync"]["milestone_title"] == "M"
    assert result.issues[0]["issue_number"] == 101


async def test_approve_flag_alone_approves_without_execute(monkeypatch):
    calls = _patch_planner(monkeypatch)
    result, _ = await _plan(monkeypatch, approve=True)
    assert "approve" in calls
    assert calls["sync"]["dry_run"] is True  # approuvé mais pas exécuté → dry-run


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


# --- CLI -----------------------------------------------------------------------


def test_cli_parses_plan_subcommand():
    from collegue.pilot.__main__ import build_parser

    args = build_parser().parse_args(["plan", "--problem", "P", "--owner", "o", "--repo", "r"])
    assert args.command == "plan"
    assert args.problem == "P" and args.owner == "o" and args.repo == "r"
    assert args.execute_sync is False  # dry-run par défaut


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

    rc = cli.main(
        ["plan", "--name", "X", "--problem", "P", "--owner", "o", "--repo", "r", "--execute-sync", "--labels", "a, b"]
    )
    assert rc == 0
    assert captured["name"] == "X" and captured["problem"] == "P"
    assert captured["owner"] == "o" and captured["repo"] == "r"
    assert captured["execute_sync"] is True
    assert captured["labels"] == ["a", "b"]  # CSV nettoyé
    assert captured["deadline"] is None  # pas de --deadline-hours
    assert "REPORT" in capsys.readouterr().out
