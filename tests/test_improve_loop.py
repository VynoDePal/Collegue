"""Tests G4 (#386) : boucle d'amélioration continue (mesures scriptées, fixture git)."""

import subprocess
from types import SimpleNamespace

import pytest

from collegue.executor import FakeCodeAgent
from collegue.improve import ProjectQualityMetrics, run_improvement
from collegue.pilot import ACTION_CONTINUE, ACTION_PAUSED_BUDGET, ContinueDecision
from collegue.sandbox import SandboxResult  # noqa: F401  (cohérence d'env)
from collegue.state import ProjectStateManager

CONT = ContinueDecision(action=ACTION_CONTINUE, reason="ok")
PAUSE = ContinueDecision(action=ACTION_PAUSED_BUDGET, reason="budget")


def _metrics(composite, *, tests=True, security=0, measured=True, coverage=80.0, review=0.7):
    return ProjectQualityMetrics(
        coverage_pct=coverage,
        review_score=review,
        security_findings=security,
        tests_passed=tests,
        composite=composite,
        coverage_measured=measured,
    )


class _Budget:
    def __init__(self, seq=(CONT,)):
        self._seq = list(seq)
        self._i = 0

    def should_continue(self):
        d = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return d


class _ScriptedMeasure:
    """measure_fn factice : déroule une file de ProjectQualityMetrics (avant/après)."""

    def __init__(self, seq):
        self._seq = list(seq)
        self._i = 0

    async def __call__(self, workspace, ctx, *, sandbox=None, reviewer=None, diff="", weights=None):
        m = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return m


class _Branches:
    def ensure_branch(self, owner, repo, branch, from_branch=None):
        return SimpleNamespace(name=branch)


class _Files:
    def update_file(self, owner, repo, path, message, content, branch=None):
        return {}

    def delete_file(self, owner, repo, path, message, branch=None):
        return {}


class _PRs:
    def __init__(self):
        self.created = []

    def find_pr_by_head(self, owner, repo, head, base=None, state="open"):
        return None

    def create_pr(self, owner, repo, title, head, base, body):
        self.created.append({"head": head, "body": body})
        return SimpleNamespace(number=101, html_url="https://gh/pull/101", head_branch=head)


def _clients():
    from collegue.executor import PrClients

    return PrClients(branches=_Branches(), files=_Files(), prs=_PRs())


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


async def _run(git_repo, manager, *, measure_seq, agent=None, budget=None, dry_run=True, **kw):
    return await run_improvement(
        manager.create_project(name="demo"),
        git_repo,
        ctx=None,
        agent=agent or FakeCodeAgent(),
        owner="o",
        repo="r",
        manager=manager,
        budget=budget or _Budget(),
        clients=_clients(),
        dry_run=dry_run,
        measure_fn=_ScriptedMeasure(measure_seq),
        **kw,
    )


# --- promotion + plateau --------------------------------------------------------


async def test_promotes_gain_then_stops_on_plateau(git_repo, manager):
    # R1: 0.5→0.7 (gain) ; R2: 0.7→0.7 (Δ0) ; R3: 0.7→0.7 (Δ0) → plateau (2 rounds).
    seq = [_metrics(0.5), _metrics(0.7), _metrics(0.7), _metrics(0.7), _metrics(0.7), _metrics(0.7)]
    result = await _run(git_repo, manager, measure_seq=seq, plateau_rounds=2)
    assert result.stop_reason == "plateau"
    assert result.rounds == 3
    assert len(result.promoted) == 1
    assert len(result.rejected) == 2
    assert result.initial_score == 0.5
    assert result.final_score == 0.7
    assert result.promoted[0].delta == pytest.approx(0.2)


async def test_real_run_promotes_and_persists_metric(git_repo, manager):
    pid = manager.create_project(name="real")
    seq = [_metrics(0.5), _metrics(0.7), _metrics(0.7), _metrics(0.7)]
    clients = _clients()
    result = await run_improvement(
        pid,
        git_repo,
        ctx=None,
        agent=FakeCodeAgent(),
        owner="o",
        repo="r",
        manager=manager,
        budget=_Budget(),
        clients=clients,
        dry_run=False,
        plateau_rounds=2,
        measure_fn=_ScriptedMeasure(seq),
    )
    assert result.promoted_prs == [101]  # PR réelle ouverte
    assert any(m.name == "composite" for m in manager.get_metrics(pid))  # métrique persistée
    # La PR d'amélioration ne doit PAS contenir « Closes #N » (le numéro est un
    # compteur de round, pas une vraie issue → ne fermerait pas une issue au hasard).
    body = clients.prs.created[0]["body"]
    assert "Closes #" not in body


async def test_importing_improve_stays_light():
    # Importer collegue.improve ne doit PAS tirer le pilote/exécuteur/openhands
    # (briques importées paresseusement dans run_improvement). Sous-process = fiable.
    import os
    import subprocess
    import sys

    code = (
        "import sys, collegue.improve; "
        "bad=[m for m in sys.modules if m.startswith(('collegue.executor','collegue.pilot','openhands'))]; "
        "assert not bad, bad"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=dict(os.environ))
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- fail-closed (pas de promotion d'une régression) ----------------------------


async def test_regression_is_not_promoted(git_repo, manager):
    # Après : tests rouges → gate rejette quel que soit le score.
    seq = [_metrics(0.7), _metrics(0.99, tests=False)]
    result = await _run(git_repo, manager, measure_seq=seq, plateau_rounds=1)
    assert result.promoted == []
    assert result.stop_reason == "plateau"
    assert "rouges" in result.rejected[0][1]


async def test_no_diff_round_counts_as_no_gain(git_repo, manager):
    # Agent qui n'écrit rien → aucun diff → round à vide (pas de promotion).
    result = await _run(git_repo, manager, measure_seq=[_metrics(0.5)], agent=FakeCodeAgent(files={}), plateau_rounds=1)
    assert result.promoted == []
    assert result.rejected[0][1] == "aucun diff produit"
    assert result.stop_reason == "plateau"


# --- arrêts ---------------------------------------------------------------------


async def test_budget_stops_loop(git_repo, manager):
    seq = [_metrics(0.5), _metrics(0.7), _metrics(0.7), _metrics(0.7)]
    result = await _run(git_repo, manager, measure_seq=seq, budget=_Budget([CONT, PAUSE]), plateau_rounds=5)
    assert result.stop_reason == "paused_budget"
    assert result.rounds == 1  # 1 round avant la pause


async def test_safety_cap(git_repo, manager):
    # Gain à chaque round (jamais de plateau) mais cap à 1 round.
    seq = [_metrics(0.1), _metrics(0.5), _metrics(0.5), _metrics(0.9)]
    result = await _run(git_repo, manager, measure_seq=seq, max_iterations=1, plateau_rounds=9)
    assert result.stop_reason == "safety_cap"
    assert result.rounds == 1
