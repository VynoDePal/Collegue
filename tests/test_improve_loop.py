"""Tests G4 (#386) : boucle d'amélioration continue (mesures scriptées, fixture git)."""

import math
import os
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


def _metrics(
    composite,
    *,
    tests=True,
    security=0,
    security_weighted=0.0,
    measured=True,
    coverage=80.0,
    review=0.7,
    review_measured=True,
    review_blocking=False,
):
    # Une mesure RÉELLE avec reviewer renseigne le verdict de revue (vague 3) : le script le reproduit fidèlement.
    return ProjectQualityMetrics(
        coverage_pct=coverage,
        security_findings=security,
        security_weighted=security_weighted,
        tests_passed=tests,
        composite=composite,
        coverage_measured=measured,
        review_score=review,
        review_measured=review_measured,
        review_blocking=review_blocking,
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


_REMOTE_STATE = {}


@pytest.fixture(autouse=True)
def _reset_remote_state():
    _REMOTE_STATE.clear()
    yield
    _REMOTE_STATE.clear()


def _clients():
    """Clients GitHub FIDÈLES : vrai dépôt Git distant cloné depuis la source du test (arbres calculés par git)."""
    from github_fakes import FakeRemote

    if "source" not in _REMOTE_STATE:  # test sans dépôt source : rien n'atteindra jamais GitHub
        from collegue.executor import PrClients

        return PrClients(branches=None, files=None, prs=None)
    _REMOTE_STATE["count"] = _REMOTE_STATE.get("count", 0) + 1
    root = _REMOTE_STATE["root"] / f"remote-{_REMOTE_STATE['count']}"
    root.mkdir()
    remote = FakeRemote(root, _REMOTE_STATE["source"])
    clients = remote.clients()
    clients.remote = remote
    return clients


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
    _REMOTE_STATE.update(source=str(src), root=tmp_path, count=0)
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


async def test_phase5_hook_runs_immediately_and_auto_merged_round_returns_to_main(git_repo, manager):
    pid = manager.create_project(name="phase5")
    clients = _clients()
    calls = []

    async def hook(pr):
        calls.append(pr.number)
        return SimpleNamespace(merged=True, continue_loop=True, stop_reason=None, reason="main verte")

    result = await run_improvement(
        pid,
        git_repo,
        ctx=None,
        agent=FakeCodeAgent(),
        owner="o",
        repo="r",
        manager=manager,
        budget=_Budget((CONT, PAUSE)),
        clients=clients,
        dry_run=False,
        measure_fn=_ScriptedMeasure([_metrics(0.5), _metrics(0.7)]),
        promotion_hook=hook,
    )
    assert calls == [101]
    assert result.promoted[0].auto_merged is True
    assert result.stop_reason == "paused_budget"
    assert clients.prs.created[0]["base"] == "main"


async def test_phase5_refusal_stops_before_any_child_pr(git_repo, manager):
    clients = _clients()

    async def hook(pr):
        return SimpleNamespace(
            merged=False,
            continue_loop=False,
            stop_reason="auto_merge_blocked",
            reason="CI absente",
        )

    result = await run_improvement(
        manager.create_project(name="blocked"),
        git_repo,
        ctx=None,
        agent=FakeCodeAgent(),
        owner="o",
        repo="r",
        manager=manager,
        budget=_Budget(),
        clients=clients,
        dry_run=False,
        measure_fn=_ScriptedMeasure([_metrics(0.5), _metrics(0.7), _metrics(0.7), _metrics(0.9)]),
        promotion_hook=hook,
    )
    assert result.stop_reason == "auto_merge_blocked"
    assert len(clients.prs.created) == 1 and len(result.promoted) == 1
    assert "CI absente" in result.rejected[-1][1]


async def test_phase5_guard_failure_reason_propagates(git_repo, manager):
    async def hook(pr):
        return SimpleNamespace(
            merged=True,
            continue_loop=False,
            stop_reason="post_merge_guard_failed",
            reason="main rouge",
        )

    result = await run_improvement(
        manager.create_project(name="red"),
        git_repo,
        ctx=None,
        agent=FakeCodeAgent(),
        owner="o",
        repo="r",
        manager=manager,
        budget=_Budget(),
        clients=_clients(),
        dry_run=False,
        measure_fn=_ScriptedMeasure([_metrics(0.5), _metrics(0.7)]),
        promotion_hook=hook,
    )
    assert result.stop_reason == "post_merge_guard_failed"
    assert result.promoted[0].auto_merged is True


async def test_phase5_recovered_revert_is_not_reported_or_persisted_as_current_quality(git_repo, manager):
    project_id = manager.create_project(name="reverted")

    async def hook(pr):
        return SimpleNamespace(
            merged=True,
            continue_loop=False,
            stop_reason="auto_revert_recovered",
            reason="main restaurée",
            remote_revert=SimpleNamespace(restored=True),
        )

    result = await run_improvement(
        project_id,
        git_repo,
        ctx=None,
        agent=FakeCodeAgent(),
        owner="o",
        repo="r",
        manager=manager,
        budget=_Budget(),
        clients=_clients(),
        dry_run=False,
        measure_fn=_ScriptedMeasure([_metrics(0.5), _metrics(0.7)]),
        promotion_hook=hook,
    )
    assert result.stop_reason == "auto_revert_recovered"
    assert result.final_score == result.initial_score == 0.5
    assert result.promoted[0].reverted is True
    assert result.promoted[0].auto_merged is False
    assert manager.get_metrics(project_id) == []


async def test_phase5_hook_is_never_called_in_dry_run(git_repo, manager):
    def forbidden(_pr):
        raise AssertionError("hook interdit en dry-run")

    result = await _run(
        git_repo,
        manager,
        measure_seq=[_metrics(0.5), _metrics(0.7), _metrics(0.7), _metrics(0.7)],
        plateau_rounds=1,
        promotion_hook=forbidden,
    )
    assert result.promoted and result.promoted[0].auto_merged is False


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


async def test_measure_that_mutates_snapshot_is_never_promoted(git_repo, manager):
    """#582 : Phase 4 livre exactement les octets mesurés, sinon elle rejette."""

    class _MutatingMeasure:
        def __init__(self):
            self.calls = 0

        async def __call__(self, workspace, ctx, *, sandbox=None, reviewer=None, diff="", weights=None):
            self.calls += 1
            if self.calls == 2:
                from pathlib import Path

                Path(workspace, "COLLEGUE_FAKE.txt").write_text("mutation pendant la mesure\n")
                return _metrics(0.9)
            return _metrics(0.5)

    result = await run_improvement(
        manager.create_project(name="drift"),
        git_repo,
        ctx=None,
        agent=FakeCodeAgent(),
        owner="o",
        repo="r",
        manager=manager,
        budget=_Budget(),
        clients=_clients(),
        dry_run=False,
        plateau_rounds=1,
        measure_fn=_MutatingMeasure(),
    )
    assert result.promoted == []
    assert result.rejected and "intégrité du livrable refusée" in result.rejected[0][1]


async def test_no_diff_round_counts_as_no_gain(git_repo, manager):
    # Agent qui n'écrit rien → aucun diff → round à vide (pas de promotion).
    result = await _run(git_repo, manager, measure_seq=[_metrics(0.5)], agent=FakeCodeAgent(files={}), plateau_rounds=1)
    assert result.promoted == []
    assert result.rejected[0][1] == "aucun diff produit"
    assert result.stop_reason == "plateau"


# --- compounding (#545) ---------------------------------------------------------


def test_seed_promoted_diffs_reapplies_and_commits(git_repo):
    # Un diff promu réappliqué sur un clone neuf → fichier présent ET committé.
    from collegue.executor.agent import IssueSpec
    from collegue.executor.runner import capture_diff
    from collegue.executor.workspace import prepare_workspace
    from collegue.improve.loop import _seed_promoted_diffs

    ws = prepare_workspace(git_repo, IssueSpec(number=1, title="t"))
    with open(os.path.join(ws.path, "feature.py"), "w") as fh:
        fh.write("VALUE = 1\n")
    diff, _ = capture_diff(ws)
    # remet le clone à l'état vierge (simule le clone neuf d'un round)
    _git(ws.path, "reset", "--hard")
    _git(ws.path, "clean", "-fdq")
    assert not os.path.exists(os.path.join(ws.path, "feature.py"))

    applied = _seed_promoted_diffs(ws, [diff])
    assert applied == 1
    assert os.path.exists(os.path.join(ws.path, "feature.py"))
    status = subprocess.run(["git", "status", "--porcelain"], cwd=ws.path, capture_output=True, text=True)
    assert status.stdout.strip() == ""  # tout est committé (HEAD = état cumulé)


def test_seed_promoted_diffs_applies_two_in_cascade(git_repo):
    # Deux diffs promus successifs (diff2 capturé SUR base+diff1) réappliqués en
    # cascade sur un clone neuf → les deux fichiers présents (3-way en série).
    #
    # Frontière Git (vague 1) : la base avance par ``advance_base`` (commit dans le
    # contrôle de confiance), plus par un commit dans le ``.git`` du workspace que
    # l'agent/les tests peuvent écrire. Le « clone neuf » est un vrai second workspace.
    from collegue.executor.agent import IssueSpec
    from collegue.executor.runner import capture_diff
    from collegue.executor.workspace import advance_base, prepare_workspace
    from collegue.improve.loop import _seed_promoted_diffs

    ws = prepare_workspace(git_repo, IssueSpec(number=3, title="t"))
    with open(os.path.join(ws.path, "a.py"), "w") as fh:
        fh.write("A = 1\n")
    diff1, _ = capture_diff(ws)
    assert advance_base(ws, "a")
    with open(os.path.join(ws.path, "b.py"), "w") as fh:
        fh.write("B = 2\n")
    diff2, files2 = capture_diff(ws)  # capturé contre base+diff1 (b.py seul)
    assert files2 == ("b.py",)
    assert "a.py" not in diff2

    fresh = prepare_workspace(git_repo, IssueSpec(number=4, title="t"))  # clone neuf : ni a.py ni b.py
    assert not os.path.exists(os.path.join(fresh.path, "a.py"))
    assert not os.path.exists(os.path.join(fresh.path, "b.py"))

    applied = _seed_promoted_diffs(fresh, [diff1, diff2])
    assert applied == 2
    assert os.path.exists(os.path.join(fresh.path, "a.py"))
    assert os.path.exists(os.path.join(fresh.path, "b.py"))


def test_seed_promoted_diffs_skips_inapplicable(git_repo):
    # Un diff corrompu/inapplicable est sauté (best-effort), sans casser le run.
    from collegue.executor.agent import IssueSpec
    from collegue.executor.workspace import prepare_workspace
    from collegue.improve.loop import _seed_promoted_diffs

    ws = prepare_workspace(git_repo, IssueSpec(number=2, title="t"))
    assert _seed_promoted_diffs(ws, ["pas un diff valide\n"]) == 0


async def test_compounding_reapplies_promoted_diff_on_next_round(git_repo, manager):
    # Round 1 promeut (feature.py) ; round 2 doit RÉAPPLIQUER ce diff sur le clone neuf
    # → feature.py présent à la mesure baseline du round 2 (score cumulatif).
    baseline_has_feature = []

    class _ProbeMeasure:
        def __init__(self):
            self.i = 0

        async def __call__(self, workspace, ctx, *, sandbox=None, reviewer=None, diff="", weights=None):
            if not diff:  # mesure baseline (« avant »)
                baseline_has_feature.append(os.path.exists(os.path.join(workspace, "feature.py")))
            scores = [0.5, 0.7, 0.7, 0.7]  # r1.before, r1.after (promu), r2.before, …
            m = _metrics(scores[min(self.i, len(scores) - 1)])
            self.i += 1
            return m

    result = await run_improvement(
        manager.create_project(name="compound"),
        git_repo,
        ctx=None,
        agent=FakeCodeAgent(files={"feature.py": "VALUE = 1\n"}),
        owner="o",
        repo="r",
        manager=manager,
        budget=_Budget(),
        clients=_clients(),
        dry_run=True,
        plateau_rounds=1,
        measure_fn=_ProbeMeasure(),
    )
    assert len(result.promoted) == 1
    assert baseline_has_feature[0] is False  # round 1 : clone neuf, pas de feature.py
    assert baseline_has_feature[1] is True  # round 2 : diff promu réappliqué (cumulatif)


async def test_autofix_lint_cleans_promoted_diff_end_to_end(git_repo, manager):
    # Le coder écrit un .py avec un import inutilisé ; l'auto-fix (#549) le retire AVANT
    # la mesure → le diff promu (passé à la mesure « after ») est propre. Couvre aussi
    # le chemin « diff vidé » (round 2 : l'auto-fix ramène le diff au HEAD cumulé →
    # gain nul → rejet « gain insuffisant », sans promotion ni crash).
    from collegue.improve.metrics import _find_ruff

    if _find_ruff() is None:
        pytest.skip("ruff indisponible dans cet environnement")

    after_diffs = []

    class _Probe:
        def __init__(self):
            self.i = 0

        async def __call__(self, workspace, ctx, *, sandbox=None, reviewer=None, diff="", weights=None):
            if diff:  # mesure « after »
                after_diffs.append(diff)
            m = _metrics([0.5, 0.7][min(self.i, 1)])
            self.i += 1
            return m

    result = await run_improvement(
        manager.create_project(name="autofix"),
        git_repo,
        ctx=None,
        agent=FakeCodeAgent(files={"feat.py": "import os\nVALUE = 1\n"}),
        owner="o",
        repo="r",
        manager=manager,
        budget=_Budget(),
        clients=_clients(),
        dry_run=True,
        plateau_rounds=1,
        measure_fn=_Probe(),
    )
    assert len(result.promoted) == 1
    assert after_diffs  # une mesure « after » a bien eu lieu
    assert "import os" not in after_diffs[0]  # F401 retiré par l'auto-fix avant mesure
    assert "feat.py" in after_diffs[0]  # le diff promu reste celui du round


async def test_execute_mode_stacks_prs_on_previous_branch(git_repo, manager):
    # En --execute (#554), la PR du round N se base sur la branche de la promotion N-1
    # (la 1ʳᵉ sur `main`) → diffs incrémentaux, mergeables dans l'ordre, pas de conflit.
    from collegue.executor.agent import AgentResult

    class _CounterAgent:
        def __init__(self):
            self.n = 0

        def implement_issue(self, workspace, issue):
            self.n += 1
            rel = f"feat_{self.n}.py"
            with open(os.path.join(workspace, rel), "w") as fh:
                fh.write(f"VALUE_{self.n} = {self.n}\n")  # fichier unique → diff à chaque round
            return AgentResult(success=True, files_changed=(rel,), summary="feat", logs="ok")

    clients = _clients()
    created = clients.prs.created
    seq = [_metrics(0.5), _metrics(0.7), _metrics(0.7), _metrics(0.9), _metrics(0.9), _metrics(0.9)]
    result = await run_improvement(
        manager.create_project(name="stack"),
        git_repo,
        ctx=None,
        agent=_CounterAgent(),
        owner="o",
        repo="r",
        manager=manager,
        budget=_Budget(),
        clients=clients,
        dry_run=False,
        plateau_rounds=1,
        measure_fn=_ScriptedMeasure(seq),
    )
    assert len(result.promoted) >= 2
    assert len(created) >= 2
    assert created[0]["base"] == "main"  # 1ʳᵉ PR : base = main
    assert created[1]["base"] == created[0]["head"]  # 2ᵉ PR : stackée sur la 1ʳᵉ
    # Vague 3 : chaque PR est livrée avec sa preuve, liée à la tête DISTANTE vérifiée (arbre publié == arbre testé),
    # et la 2ᵉ preuve a pour base la tête de la 1ʳᵉ (empilement vérifié, pas supposé).
    remote = clients.remote
    first, second = result.promoted[0], result.promoted[1]
    assert first.proof is not None and second.proof is not None
    assert remote.tree_of(first.head_sha) == first.proof.tree_sha
    assert remote.tree_of(second.head_sha) == second.proof.tree_sha
    assert second.proof.base_sha == first.head_sha
    assert second.proof.base_tree_sha == first.proof.tree_sha


async def test_unreliable_baseline_skips_agent_round(git_repo, manager):
    # Baseline non fiable (composite non fini, ex. scan sécu KO → inf) → round à vide
    # SANS appel agent ; fail-closed : aucune promotion, pas de score fantôme inf (#541).
    result = await _run(git_repo, manager, measure_seq=[_metrics(math.inf)], plateau_rounds=1)
    assert result.promoted == []
    assert result.stop_reason == "plateau"
    assert result.rejected[0] == ("baseline", "mesure baseline non fiable (composite non fini)")
    assert result.initial_score is None  # pas de score fantôme inf enregistré


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


# --- propagation de la commande de test (#573) ----------------------------------


async def test_forwards_configured_test_command_to_measure(git_repo, manager):
    # #573 : la commande de test du projet (GATE_TEST_COMMAND) doit atteindre measure()
    # à CHAQUE mesure (avant ET après le diff). Sans ça, measure() retombe sur
    # DEFAULT_COVERAGE_COMMAND (pytest --cov en dur) → tests rouges sur un projet à setup
    # non trivial (make, monorepo, service DB) → garde dure G2 rejette TOUTE amélioration.
    seen = []

    async def spy_measure(workspace, ctx, *, sandbox=None, reviewer=None, diff="", weights=None, coverage_command=None):
        seen.append(coverage_command)
        return _metrics(1.0)  # baseline finie → la boucle déroule un round complet

    await run_improvement(
        manager.create_project(name="cmd"),
        git_repo,
        ctx=None,
        agent=FakeCodeAgent(),
        owner="o",
        repo="r",
        manager=manager,
        budget=_Budget([CONT, PAUSE]),
        clients=_clients(),
        dry_run=True,
        coverage_command="make check",
        measure_fn=spy_measure,
    )
    assert seen, "measure_fn jamais appelé"
    assert set(seen) == {"make check"}, f"measure() doit recevoir la commande de test configurée, reçu {seen}"


# --- frontière hôte : noms de fichiers d'un agent et audit de dépendances ---------------------


def _install_fake_ruff(tmp_path, monkeypatch):
    """Faux ruff : ajoute une ligne à chaque ``*.py`` reçu (réécrit donc à travers un lien)."""
    import collegue.improve.metrics as metrics_mod

    script = tmp_path / "fake-ruff"
    script.write_text(
        '#!/bin/sh\nfor a in "$@"; do case "$a" in *.py) printf "# ruff-touched\\n" >> "$a"; '
        f'printf "%s\\n" "$a" >> "{tmp_path}/ruff-calls.log";; esac; done\n'
    )
    script.chmod(0o755)
    monkeypatch.setattr(metrics_mod, "_find_ruff", lambda: str(script))


async def test_loop_agent_symlink_never_makes_the_host_rewrite_an_outside_file(
    git_repo, manager, tmp_path, monkeypatch
):
    """Comportement public de la boucle : l'agent crée ``escape.py`` → fichier hôte ; l'auto-fix (#549)
    n'a le droit ni de le suivre ni de le réécrire, mais traite toujours le fichier légitime."""
    from pathlib import Path

    from collegue.executor import AgentResult

    _install_fake_ruff(tmp_path, monkeypatch)
    outside = tmp_path / "host-secret.py"
    outside.write_text("import os\nSECRET=1\n")

    class _SymlinkAgent:
        def implement_issue(self, workspace, issue):
            # appelé à chaque round (et le compounding réapplique un diff) : idempotent
            link = os.path.join(workspace, "escape.py")
            if not os.path.lexists(link):
                os.symlink(outside, link)
            Path(workspace, "feature.py").write_text("VALUE=1\n")
            return AgentResult(success=True)

    after_diffs = []

    class _Probe:
        def __init__(self):
            self.i = 0

        async def __call__(self, workspace, ctx, *, sandbox=None, reviewer=None, diff="", weights=None):
            if diff:
                after_diffs.append(diff)
            self.i += 1
            return _metrics([0.5, 0.7][min(self.i - 1, 1)])

    result = await run_improvement(
        manager.create_project(name="symlink"),
        git_repo,
        ctx=None,
        agent=_SymlinkAgent(),
        owner="o",
        repo="r",
        manager=manager,
        budget=_Budget(),
        clients=_clients(),
        dry_run=True,
        plateau_rounds=1,
        measure_fn=_Probe(),
    )

    assert outside.read_text() == "import os\nSECRET=1\n"  # fichier hôte intact
    # L'auto-fix n'a traité que le fichier légitime (jamais le lien ni sa cible hors workspace)...
    touched = (tmp_path / "ruff-calls.log").read_text().splitlines()
    assert touched and all(path.endswith("feature.py") for path in touched)
    # ... et le lien symbolique rend la livraison non représentable : refus explicite AVANT toute mesure, rien de
    # l'hôte n'entre dans un diff mesuré ou livré (avant la vague 3, le lien était omis en silence).
    assert result.promoted == []
    assert after_diffs == []
    assert any("escape.py" in reason for _dim, reason in result.rejected)


def _commit_requirements(git_repo, text="click==8.1.7\n"):
    with open(os.path.join(git_repo, "requirements.txt"), "w") as fh:
        fh.write(text)
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "requirements")


class _CoverageAuditSandbox:
    """Couverture 50 % → 90 % dès que ``feature.py`` existe ; réponse configurable pour pip-audit."""

    def __init__(self, audit):
        self.audit = audit
        self.commands = []

    def run_tests(self, workspace, command="pytest -q"):
        self.commands.append(command)
        if "pip-audit" in command:
            return self.audit
        cover = 90 if os.path.exists(os.path.join(workspace, "feature.py")) else 50
        return SandboxResult(exit_code=0, stdout=f"TOTAL          10      1    {cover}%\n", stderr="")


class _CountingAgent(FakeCodeAgent):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0

    def implement_issue(self, workspace, issue):
        self.calls += 1
        return super().implement_issue(workspace, issue)


def _audit_measure():
    import functools

    from collegue.improve.metrics import measure

    return functools.partial(
        measure,
        dep_vulns_enabled=True,
        security_scan_fn=lambda ws: (0, 0.0),
        quality_scan_fn=lambda ws: (0, 0, True),
        doc_coverage_fn=lambda ws: 1.0,
    )


@pytest.mark.parametrize(
    "audit",
    [
        SandboxResult(exit_code=127, stdout="", stderr="sh: 1: pip-audit: not found"),
        SandboxResult(exit_code=124, stdout="", stderr="[sandbox] délai dépassé", timed_out=True),
        SandboxResult(exit_code=1, stdout="", stderr="ERROR: unsupported requirement"),
    ],
    ids=["outil-absent", "timeout", "echec"],
)
async def test_loop_refuses_to_improve_when_the_dep_audit_is_unavailable(git_repo, manager, audit):
    """Audit ACTIVÉ mais indisponible : la baseline est non fiable, l'agent (coûteux) ne tourne pas et
    rien n'est promu — l'échec de l'outil n'est jamais lu comme « 0 vulnérabilité »."""
    _commit_requirements(git_repo)
    agent = _CountingAgent(files={"feature.py": "VALUE = 1\n"})
    sandbox = _CoverageAuditSandbox(audit)

    result = await run_improvement(
        manager.create_project(name="audit-off"),
        git_repo,
        ctx=None,
        agent=agent,
        owner="o",
        repo="r",
        manager=manager,
        budget=_Budget(),
        clients=_clients(),
        sandbox=sandbox,
        dry_run=True,
        plateau_rounds=1,
        measure_fn=_audit_measure(),
    )

    assert agent.calls == 0
    assert result.promoted == [] and result.initial_score is None
    assert result.rejected and result.rejected[0][0] == "baseline"
    assert any("pip-audit" in c for c in sandbox.commands)  # tenté DANS le sandbox, jamais sur l'hôte


async def test_loop_promotes_normally_when_the_dep_audit_is_valid(git_repo, manager):
    from collegue.executor import FakeReviewer

    _commit_requirements(git_repo)
    agent = _CountingAgent(files={"feature.py": "VALUE = 1\n"})
    sandbox = _CoverageAuditSandbox(
        SandboxResult(
            exit_code=0,
            stdout='{"dependencies": [{"name": "click", "version": "8.1.7", "vulns": []}], "fixes": []}',
            stderr="",
        )
    )

    result = await run_improvement(
        manager.create_project(name="audit-on"),
        git_repo,
        ctx=None,
        agent=agent,
        owner="o",
        repo="r",
        manager=manager,
        budget=_Budget(),
        clients=_clients(),
        sandbox=sandbox,
        dry_run=True,
        plateau_rounds=1,
        reviewer=FakeReviewer(),
        measure_fn=_audit_measure(),
    )

    assert agent.calls >= 1
    assert len(result.promoted) == 1
