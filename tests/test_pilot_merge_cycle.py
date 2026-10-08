"""Merge-bot BUILD : politique commune + cycle durable, de bout en bout contre un faux serveur GitHub REST.

Les VRAIS clients, la VRAIE politique (``merge_policy``), le VRAI cycle (``merge_cycle``), le VRAI gestionnaire d'état
(SQLite fichier, rouvert après chaque « crash ») et la VRAIE boucle ``runtime._merge_in_review_prs`` /
``run_project_from_settings`` sont exercés. Seules les frontières sont doublées : transport HTTP GitHub (état et
sémantique de fusion simulés, y compris la course sur la base AU MOMENT de l'appel de fusion), preuve de livraison
(contrat du lot A) et resynchronisation git du clone (succès/échec contrôlé). Aucun appel modèle, aucun dépôt réel.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from github_fake_server import (
    FIVE_CHECKS,
    OWNER,
    REPO,
    FakeGitHubServer,
    ProofStore,
    sha_of,
)

from collegue.executor.workspace import branch_for_issue
from collegue.pilot import merge_cycle as cycle
from collegue.pilot import runtime
from collegue.pilot.driver import ProjectRunResult
from collegue.state import ProjectStateManager

pytestmark = pytest.mark.asyncio


class SimulatedCrash(BaseException):
    """Mort du processus : ne doit être interceptée par AUCUN ``except Exception`` du code testé."""


class Sync:
    """Resynchronisation du clone local (frontière git)."""

    def __init__(self):
        self.ok = True
        self.calls = 0

    def __call__(self, repo_source, base, **kw):
        self.calls += 1
        return self.ok


def _accept_local_sync(repo_source, merge_sha, tree_sha):
    return None


async def _no_sleep(_s):
    return None


class World:
    def __init__(self, tmp_path):
        self.db_url = f"sqlite:///{tmp_path / 'state.db'}"
        self.manager = ProjectStateManager.from_url(self.db_url, create=True)
        self.project_id = self.manager.create_project(name="merge-cycle", spec="SPEC de test")
        self.server = FakeGitHubServer()
        self.server.add_ruleset(1)
        self.proofs = ProofStore()
        self.sync = Sync()
        self.clients = self.server.clients()
        self.tasks = {}

    def add_task(self, number, *, with_proof=True, checks="all-green"):
        task_id = self.manager.add_task(self.project_id, f"T{number}", status="in_review")
        self.manager.update_task(task_id, issue_number=number)
        pr = self.server.open_pr(
            10 + number, head_ref=branch_for_issue(number), tree=f"tree-task-{number}", checks=checks
        )
        if with_proof:
            self.proofs.add(self.server, self.project_id, 10 + number)
        self.tasks[number] = task_id
        return task_id, pr

    def restart(self):
        """Nouveau processus : nouvelle instance du gestionnaire sur le même fichier, nouveaux clients."""
        self.manager = ProjectStateManager.from_url(self.db_url)
        self.clients = self.server.clients()
        return self

    async def merge(self, monkeypatch, **overrides):
        monkeypatch.setattr(runtime, "_resync_repo_source", self.sync)
        kwargs = dict(
            project_id=self.project_id,
            owner=OWNER,
            repo=REPO,
            repo_source="/unused",
            base="main",
            sleep_fn=_no_sleep,
            proof_loader=self.proofs.loader,
            ci_timeout_seconds=0.0,
            ci_poll_seconds=0.0,
            verify_fn=_accept_local_sync,
        )
        kwargs.update(overrides)
        return await runtime._merge_in_review_prs(self.manager, self.clients, **kwargs)

    def status(self, number):
        return self.manager.get_task(self.tasks[number]).status

    def cycle_row(self, number):
        return self.manager.get_task_merge(self.tasks[number])


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# ── chemin sain ──────────────────────────────────────────────────────────────────────────────────


async def test_healthy_merge_emits_the_exact_head_sha_and_ends_synced(world, monkeypatch):
    _, pr = world.add_task(1)
    head, base = pr["head"]["sha"], world.server.base_tip

    merged = await world.merge(monkeypatch)

    assert merged == 1
    (call,) = world.server.merge_calls()
    assert call[2] == {"merge_method": "squash", "sha": head}, "le SHA de tête transmis à l'API de fusion est exact"
    new_tip = world.server.base_tip
    commit = world.server.commits[new_tip]
    assert commit["parents"] == [base] and commit["tree"] == "tree-task-1", "main contient exactement le contenu prouvé"
    row = world.cycle_row(1)
    assert (row.state, row.merge_sha, row.head_sha, row.base_sha) == ("synced", new_tip, head, base)
    assert row.tree_sha == sha_of("tree-task-1") and len(row.proof_id) == 64
    assert world.status(1) == "merged"
    assert world.sync.calls == 1


async def test_two_tasks_in_flight_only_the_first_is_merged_the_second_was_built_on_a_stale_base(world, monkeypatch):
    world.add_task(1)
    world.add_task(2)  # construite sur la même base : après la fusion de 1, sa base est périmée

    merged = await world.merge(monkeypatch)

    assert merged == 1
    assert len(world.server.merge_calls()) == 1
    assert world.status(1) == "merged" and world.status(2) == "in_review"
    assert world.cycle_row(2) is None


# ── refus : aucun appel de fusion ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("scenario", ["no-proof", "check-missing", "check-failed", "check-skipped", "no-protection"])
async def test_refusals_never_emit_a_merge_call(world, monkeypatch, scenario):
    task_id, pr = world.add_task(1, with_proof=scenario != "no-proof")
    head = pr["head"]["sha"]
    if scenario == "check-missing":
        world.server.set_checks(head, {n: "success" for n in FIVE_CHECKS[:-1]})
    elif scenario == "check-failed":
        world.server.set_checks(head, {**{n: "success" for n in FIVE_CHECKS}, FIVE_CHECKS[2]: "failure"})
    elif scenario == "check-skipped":
        world.server.set_checks(head, {**{n: "success" for n in FIVE_CHECKS}, FIVE_CHECKS[4]: "skipped"})
    elif scenario == "no-protection":
        world.server.rules.clear()
        world.server.rulesets.clear()

    merged = await world.merge(monkeypatch)

    assert merged == 0
    assert world.server.merge_calls() == []
    assert world.status(1) == "in_review" and world.cycle_row(1) is None
    assert world.sync.calls == 0


async def test_api_error_while_reading_checks_never_merges(world, monkeypatch):
    world.add_task(1)
    world.server.fail("GET", r"/check-runs$", 500)

    assert await world.merge(monkeypatch) == 0
    assert world.server.merge_calls() == [] and world.status(1) == "in_review"


async def test_pr_merged_outside_the_engine_is_never_re_merged_nor_trusted(world, monkeypatch):
    _, pr = world.add_task(1)
    pr.update(state="closed", merged=True, merge_commit_sha=sha_of("human-merge"))

    assert await world.merge(monkeypatch) == 0
    assert world.server.merge_calls() == [] and world.status(1) == "in_review"


async def test_budget_or_deadline_guard_stops_the_merge_before_any_github_write(world, monkeypatch):
    world.add_task(1)

    merged = await world.merge(monkeypatch, continue_fn=lambda: SimpleNamespace(ok=False, reason="deadline atteinte"))

    assert merged == 0 and world.server.merge_calls() == [] and world.status(1) == "in_review"


async def test_pending_checks_are_awaited_within_the_bound_then_merged(world, monkeypatch):
    _, pr = world.add_task(1)
    head = pr["head"]["sha"]
    world.server.set_checks(head, {**{n: "success" for n in FIVE_CHECKS}, FIVE_CHECKS[1]: "in_progress"})
    sleeps = []

    async def sleeper(seconds):
        sleeps.append(seconds)
        world.server.set_checks(head, {n: "success" for n in FIVE_CHECKS})  # la CI se termine pendant l'attente

    merged = await world.merge(monkeypatch, sleep_fn=sleeper, ci_timeout_seconds=60.0, ci_poll_seconds=5.0)

    assert merged == 1 and sleeps == [5.0]
    assert len(world.server.merge_calls()) == 1


# ── courses distantes simulées AU MOMENT de l'appel de fusion ─────────────────────────────────────


async def test_head_moved_exactly_at_the_merge_call_is_rejected_by_the_sha_guard(world, monkeypatch):
    _, pr = world.add_task(1)
    old_head = pr["head"]["sha"]
    world.server.before_merge = lambda s: s.push_to_pr_head(11, tree="tree-late-push")

    merged = await world.merge(monkeypatch)

    assert merged == 0
    first, *rest = world.server.merge_calls()
    assert first[2]["sha"] == old_head, "l'appel émis porte la tête évaluée, que le serveur a refusée (409)"
    assert world.server.base_tip != world.server.prs[11]["merge_commit_sha"]
    assert world.server.prs[11]["merged"] is False
    assert world.status(1) == "in_review"
    assert world.cycle_row(1).state == "abandoned"
    # la nouvelle tête n'a pas de preuve : aucun second appel de fusion ne part
    assert rest == [] or all(c[2]["sha"] != world.server.prs[11]["head"]["sha"] for c in rest)


async def test_base_moved_exactly_at_the_merge_call_is_refused_by_the_strict_server_rule(world, monkeypatch):
    _, pr = world.add_task(1)
    world.server.before_merge = lambda s: s.advance_base(tree="tree-merged-by-someone-else")

    merged = await world.merge(monkeypatch)

    assert merged == 0
    (call, *_) = world.server.merge_calls()
    assert call[2]["sha"] == pr["head"]["sha"]
    assert world.server.prs[11]["merged"] is False, "GitHub a refusé : branche pas à jour (règle stricte applicable)"
    assert world.status(1) == "in_review"
    assert world.cycle_row(1).state == "abandoned"
    # le sommet de main est celui de l'autre contributeur, jamais un contenu non testé
    assert world.server.commits[world.server.base_tip]["tree"] == "tree-merged-by-someone-else"


async def test_untrusted_precondition_is_detected_after_the_fact_and_blocks_everything(world, monkeypatch):
    """Si le serveur ne fait PAS respecter la règle (précondition trompée), la base qui bouge au moment de l'appel
    produit un contenu non testé : détecté par le contrôle du commit de fusion -> attention, tâche suivante bloquée."""
    world.add_task(1)
    world.server.ignore_strict = True
    world.server.before_merge = lambda s: s.advance_base(tree="tree-merged-by-someone-else")

    merged = await world.merge(monkeypatch)

    assert merged == 0
    row = world.cycle_row(1)
    assert row.state == "attention" and "tree" in row.last_error or "parents" in row.last_error
    assert row.merge_sha == world.server.prs[11]["merge_commit_sha"]
    assert world.status(1) == "in_review", "la livraison n'est jamais comptée prête"
    assert world.sync.calls == 0, "aucune resynchronisation d'un contenu non prouvé"
    assert [c.state for c in cycle.blocking_cycles(world.manager, world.project_id)] == ["attention"]
    # relancé : plus aucune fusion, cycle toujours bloquant
    again = await world.merge(monkeypatch)
    assert again == 0 and len(world.server.merge_calls()) == 1


# ── échec de resynchronisation, reprise, crash ─────────────────────────────────────────────────


async def test_failed_resync_after_a_confirmed_merge_is_a_durable_state_not_a_second_merge(world, monkeypatch):
    world.add_task(1)
    world.sync.ok = False

    merged = await world.merge(monkeypatch)

    assert merged == 0
    row = world.cycle_row(1)
    assert row.state == "merged_unsynced" and row.merge_sha == world.server.base_tip
    assert "resynchronisation" in row.last_error
    assert world.status(1) == "in_review", "livraison non prête tant que le clone n'est pas resynchronisé"
    assert len(world.server.merge_calls()) == 1

    # rappelé avec la synchro toujours en panne : aucun nouvel appel de fusion, état inchangé
    assert await world.merge(monkeypatch) == 0
    assert len(world.server.merge_calls()) == 1 and world.cycle_row(1).state == "merged_unsynced"

    # synchro rétablie (nouveau processus) : reprise SANS deuxième fusion
    world.restart()
    world.sync.ok = True
    assert await world.merge(monkeypatch) == 1
    assert len(world.server.merge_calls()) == 1
    assert world.cycle_row(1).state == "synced" and world.status(1) == "merged"


async def test_resync_exception_is_also_durable(world, monkeypatch):
    world.add_task(1)

    def boom(*a, **k):
        raise OSError("git indisponible")

    monkeypatch.setattr(runtime, "_resync_repo_source", boom)
    merged = await runtime._merge_in_review_prs(
        world.manager,
        world.clients,
        project_id=world.project_id,
        owner=OWNER,
        repo=REPO,
        repo_source="/unused",
        base="main",
        sleep_fn=_no_sleep,
        proof_loader=world.proofs.loader,
        ci_timeout_seconds=0.0,
        ci_poll_seconds=0.0,
        verify_fn=_accept_local_sync,
    )

    assert merged == 0 and world.cycle_row(1).state == "merged_unsynced"
    assert "git indisponible" in world.cycle_row(1).last_error


async def test_crash_between_the_remote_success_and_the_local_record_is_reconciled_without_a_second_merge(
    world, monkeypatch
):
    world.add_task(1)

    def die(server, result):
        raise SimulatedCrash()  # le serveur a fusionné ; le processus meurt avant d'avoir lu la réponse

    world.server.after_merge = die
    with pytest.raises(SimulatedCrash):
        await world.merge(monkeypatch)
    assert world.server.prs[11]["merged"] is True
    assert world.cycle_row(1).state == "merge_pending", "seul le write-ahead existe"
    assert world.status(1) == "in_review"

    world.restart()
    merged = await world.merge(monkeypatch)

    assert merged == 1
    assert len(world.server.merge_calls()) == 1, "aucune deuxième fusion"
    row = world.cycle_row(1)
    assert row.state == "synced" and row.merge_sha == world.server.prs[11]["merge_commit_sha"]
    assert world.status(1) == "merged"


async def test_crash_before_the_remote_effect_re_validates_and_merges_exactly_once(world, monkeypatch):
    world.add_task(1)

    def die(server):
        raise SimulatedCrash()

    world.server.before_merge = die
    with pytest.raises(SimulatedCrash):
        await world.merge(monkeypatch)
    assert world.server.prs[11]["merged"] is False and world.cycle_row(1).state == "merge_pending"

    world.restart()
    assert await world.merge(monkeypatch) == 1

    assert world.server.prs[11]["merged"] is True and world.cycle_row(1).state == "synced"
    merge_commits = [c for c in world.server.commits.values() if c["message"] == "merge PR 11"]
    assert len(merge_commits) == 1


async def test_reconciliation_refuses_a_merge_of_another_head(world, monkeypatch):
    world.add_task(1)

    def die(server, result):
        raise SimulatedCrash()

    world.server.after_merge = die
    with pytest.raises(SimulatedCrash):
        await world.merge(monkeypatch)
    # pendant l'indisponibilité, le contenu fusionné ne correspond plus à la tête persistée
    world.server.prs[11]["head"]["sha"] = world.server.commit([world.server.base_tip], tree="tree-other", message="x")

    world.restart()
    assert await world.merge(monkeypatch) == 0
    row = world.cycle_row(1)
    assert row.state == "attention" and "tête" in row.last_error
    assert world.sync.calls == 0 and world.status(1) == "in_review"


async def test_unreadable_github_during_reconciliation_keeps_the_cycle_pending(world, monkeypatch):
    world.add_task(1)

    def die(server, result):
        raise SimulatedCrash()

    world.server.after_merge = die
    with pytest.raises(SimulatedCrash):
        await world.merge(monkeypatch)

    world.restart()
    world.server.fail("GET", r"/pulls/11$", 500)
    assert await world.merge(monkeypatch) == 0
    assert world.cycle_row(1).state == "merge_pending" and len(world.server.merge_calls()) == 1
    assert world.status(1) == "in_review"


async def test_lost_response_is_reconciled_in_process(world, monkeypatch):
    """Réponse perdue (timeout) alors que la fusion a eu lieu : relue, jamais rejouée."""
    world.add_task(1)
    original = world.server.api_put

    def lost(endpoint, data):
        original(endpoint, data)
        from github_fake_server import HttpError

        raise HttpError("timeout", status_code=504)

    for client in (world.clients.prs,):
        client._api_put = lost

    assert await world.merge(monkeypatch) == 1
    assert len(world.server.merge_calls()) == 1 and world.cycle_row(1).state == "synced"


# ── barrière du runtime : pas de tâche suivante tant que le clone n'est pas resynchronisé ──────────


def _settings(**extra):
    return SimpleNamespace(
        BUILD_AUTO_MERGE=True, AUTO_MERGE_CI_TIMEOUT_SECONDS=0, AUTO_MERGE_CI_POLL_SECONDS=0, **extra
    )


class _Budget:
    def should_continue(self):
        return SimpleNamespace(action="continue", ok=True)

    def time_remaining_seconds(self):
        return None


class _Ctx:
    async def aclose(self):
        return None


async def _run(world, monkeypatch, *, settings=None, run_calls=None, passes=None, improve=False, **extra):
    from collegue.executor import FakeCodeAgent, FakeReviewer
    from collegue.pilot import run_project_from_settings
    from collegue.planner import approve_plan

    monkeypatch.setattr(runtime, "_resync_repo_source", world.sync)
    monkeypatch.setattr("collegue.executor.openhands_agent.coder_pricing_resolvable", lambda s=None: True)
    run_calls = run_calls if run_calls is not None else []
    script = list(passes or [ProjectRunResult(stop_reason="completed", iterations=0, processed=[])])

    async def fake_run_project(project_id, repo_source, ctx, **kw):
        run_calls.append(kw)
        step = script.pop(0) if len(script) > 1 else script[0]
        return await step(kw) if callable(step) else step

    monkeypatch.setattr(runtime, "run_project", fake_run_project)
    try:
        approve_plan(world.manager, world.project_id)
    except Exception:  # noqa: BLE001 - plan déjà approuvé ou sans brouillon : sans importance ici
        pass
    return await run_project_from_settings(
        world.project_id,
        "/unused",
        owner=OWNER,
        repo=REPO,
        dry_run=False,
        settings_obj=settings or _settings(),
        manager=world.manager,
        sandbox=SimpleNamespace(),
        agent=FakeCodeAgent(),
        reviewer=FakeReviewer(),
        clients=world.clients,
        budget=_Budget(),
        ctx=_Ctx(),
        improve=improve,
        audit=SimpleNamespace(
            record=lambda *a, **k: None,
            record_cost=lambda *a, **k: None,
            record_once=lambda *a, **k: None,
            cost_summary=lambda: {"usd": 0.0, "tokens": 0},
        ),
        cost_source=lambda: (0.0, 0),
        merge_proof_loader=world.proofs.loader,
        merge_sync_verify_fn=_accept_local_sync,
        **extra,
    )


async def test_resync_failure_stops_the_run_before_the_next_agent_call(world, monkeypatch):
    world.add_task(1)
    world.sync.ok = False
    calls = []
    awaiting = ProjectRunResult(stop_reason="awaiting_merge", iterations=1, processed=[])

    result = await _run(world, monkeypatch, run_calls=calls, passes=[awaiting])

    assert result.stop_reason == cycle.STOP_SYNC_PENDING
    assert len(calls) == 1, "run_project (l'agent de la tâche suivante) n'est PAS relancé depuis l'ancien checkout"
    assert len(world.server.merge_calls()) == 1
    assert world.status(1) == "in_review" and result.project_status is None


async def test_restart_with_a_pending_sync_resynchronises_before_any_task_and_never_merges_again(world, monkeypatch):
    world.add_task(1)
    world.sync.ok = False
    await _run(
        world,
        monkeypatch,
        run_calls=[],
        passes=[ProjectRunResult(stop_reason="awaiting_merge", iterations=1, processed=[])],
    )
    assert world.cycle_row(1).state == "merged_unsynced"

    # redémarrage, synchro toujours cassée : la barrière arrête AVANT toute tâche
    world.restart()
    calls = []
    blocked = await _run(world, monkeypatch, run_calls=calls)
    assert blocked.stop_reason == cycle.STOP_SYNC_PENDING and calls == []

    # synchro rétablie : reprise, puis seulement après, run_project démarre — sans seconde fusion
    world.restart()
    world.sync.ok = True
    done = await _run(world, monkeypatch, run_calls=calls)
    assert len(calls) == 1 and done.stop_reason == "completed"
    assert len(world.server.merge_calls()) == 1
    assert world.cycle_row(1).state == "synced" and world.status(1) == "merged"


async def test_the_barrier_applies_even_when_build_auto_merge_was_turned_off(world, monkeypatch):
    world.add_task(1)
    world.sync.ok = False
    await _run(
        world,
        monkeypatch,
        run_calls=[],
        passes=[ProjectRunResult(stop_reason="awaiting_merge", iterations=1, processed=[])],
    )
    world.restart()
    calls = []

    result = await _run(world, monkeypatch, settings=SimpleNamespace(BUILD_AUTO_MERGE=False), run_calls=calls)

    assert result.stop_reason == cycle.STOP_SYNC_PENDING and calls == []
    assert len(world.server.merge_calls()) == 1


async def test_attention_cycle_blocks_every_later_run(world, monkeypatch):
    world.add_task(1)
    world.server.ignore_strict = True
    world.server.before_merge = lambda s: s.advance_base(tree="tree-other")
    await _run(
        world,
        monkeypatch,
        run_calls=[],
        passes=[ProjectRunResult(stop_reason="awaiting_merge", iterations=1, processed=[])],
    )
    world.restart()
    calls = []

    result = await _run(world, monkeypatch, run_calls=calls)

    assert result.stop_reason == cycle.STOP_ATTENTION and calls == []


async def test_final_drain_uses_the_same_policy_and_phase4_never_starts_after_a_failed_sync(world, monkeypatch):
    world.add_task(1)
    world.sync.ok = False
    calls = []
    improvement = []

    async def fake_improvement(*a, **k):
        improvement.append(1)
        raise AssertionError("Phase 4 interdite")

    completed = ProjectRunResult(stop_reason="completed", iterations=1, processed=[])
    monkeypatch.setattr(runtime, "_resync_repo_source", world.sync)
    result = await _run(world, monkeypatch, run_calls=calls, passes=[completed], improve=True)

    assert result.stop_reason == cycle.STOP_SYNC_PENDING
    assert improvement == [] and len(world.server.merge_calls()) == 1


async def test_opt_out_default_never_touches_github_even_with_in_review_tasks(world, monkeypatch):
    world.add_task(1)
    calls = []
    awaiting = ProjectRunResult(stop_reason="awaiting_merge", iterations=1, processed=[])

    result = await _run(world, monkeypatch, settings=SimpleNamespace(), run_calls=calls, passes=[awaiting])

    assert result.stop_reason == "awaiting_merge"
    assert world.server.merge_calls() == []
    assert not [c for c in world.server.calls if "/rules/" in c[1] or "/protection" in c[1]], (
        "désactivé : aucune lecture de politique"
    )


async def test_build_improve_handoff_orders_merge_resync_then_phase4(world, monkeypatch):
    """Dernière PR BUILD : fusion au SHA prouvé, resync du clone, vérification stricte du handoff, PUIS Phase 4."""
    world.add_task(1)
    events = []
    world.server.before_merge = lambda server: events.append("merged")

    def merge_resync(src, base, **kw):
        events.append("merge_resynced")
        return True

    def handoff_sync(_src, _base):
        events.append("handoff_resynced")
        return True

    async def handoff_pass(kw):
        """Seconde passe du vrai driver : handoff strict (resync vérifié) puis Phase 4."""
        assert kw["sync_base_fn"]("/unused", "main")
        improvement = await kw["run_improvement_fn"]("p", "/unused", None)
        return ProjectRunResult(stop_reason="completed", iterations=0, processed=[], improvement=improvement)

    async def improvement(project_id, repo_source, ctx, **kw):
        events.append("improved")
        return SimpleNamespace(stop_reason="plateau")

    world.sync = merge_resync
    result = await _run(
        world,
        monkeypatch,
        improve=True,
        passes=[ProjectRunResult(stop_reason="completed", iterations=1, processed=[]), handoff_pass],
        run_improvement_fn=improvement,
        sync_base_fn=handoff_sync,
    )

    assert result.stop_reason == "completed" and result.improvement.stop_reason == "plateau"
    assert world.status(1) == "merged" and world.cycle_row(1).state == "synced"
    assert events == ["merged", "merge_resynced", "handoff_resynced", "improved"]


async def test_refused_final_drain_never_reports_completed_or_runs_phase4(world, monkeypatch):
    """Un drain final refusé (check requis absent) reste awaiting_merge : ni faux succès, ni Phase 4."""
    _, pr = world.add_task(1)
    world.server.set_checks(pr["head"]["sha"], {n: "success" for n in FIVE_CHECKS[:-1]})
    improved = []

    async def improvement(*a, **k):
        improved.append(1)
        raise AssertionError("Phase 4 interdite après un merge BUILD refusé")

    result = await _run(
        world,
        monkeypatch,
        improve=True,
        passes=[ProjectRunResult(stop_reason="completed", iterations=1, processed=[])],
        run_improvement_fn=improvement,
        sync_base_fn=lambda _s, _b: True,
    )

    assert result.stop_reason == "awaiting_merge" and result.pending_reviews == [world.tasks[1]]
    assert result.improvement is None and improved == []
    assert world.server.merge_calls() == [] and world.status(1) == "in_review"


async def test_confirmed_merge_runs_the_real_git_resync_commands(world, monkeypatch):
    """Sans double de resync : le vrai ``resync_repository_base`` émet fetch + reset sur la base après la fusion."""
    from collegue.sandbox import SandboxResult

    world.add_task(1)
    commands = []

    class Runner:
        def run_command(self, cmd, ws):
            commands.append(" ".join(cmd) if isinstance(cmd, list) else cmd)
            return SandboxResult(exit_code=0, stdout="", stderr="")

    merged = await runtime._merge_in_review_prs(
        world.manager,
        world.clients,
        project_id=world.project_id,
        owner=OWNER,
        repo=REPO,
        repo_source="/unused",
        base="main",
        git_runner=Runner(),
        sleep_fn=_no_sleep,
        proof_loader=world.proofs.loader,
        ci_timeout_seconds=0.0,
        ci_poll_seconds=0.0,
        verify_fn=_accept_local_sync,
    )

    assert merged == 1 and world.status(1) == "merged"
    assert any("fetch origin main" in c for c in commands) and any("reset --hard origin/main" in c for c in commands)


# ── Phase 5 : même politique, même faux serveur ───────────────────────────────────────────────────


def _phase5_world(world, *, proof_phase="improve", checks="all-green"):
    pr = world.server.open_pr(
        30,
        head_ref="collegue/improve-1",
        tree="tree-improve",
        files=[{"filename": "docs/x.md", "status": "modified", "additions": 3, "deletions": 0}],
        checks=checks,
    )
    world.proofs.add(world.server, world.project_id, 30, phase=proof_phase)
    return pr


async def _promote(world, **overrides):
    from collegue.pilot.automerge import DEFAULT_PATH_ALLOWLIST, RiskPolicy, auto_merge_promotion

    kwargs = dict(
        policy=RiskPolicy(enabled=True, max_loc=50, path_allowlist=DEFAULT_PATH_ALLOWLIST, method="squash"),
        revert_policy=SimpleNamespace(enabled=True),
        clients=world.clients,
        owner=OWNER,
        repo=REPO,
        repo_source="/unused",
        base="main",
        sandbox=object(),
        manager=world.manager,
        project_id=world.project_id,
        ci_timeout_seconds=0,
        sleep_fn=_no_sleep,
        sync_base_fn=lambda src, base: True,
        guard_fn=lambda *a, **k: SimpleNamespace(checked=True, healthy=True, reason="vert"),
        proof_loader=world.proofs.loader,
    )
    kwargs.update(overrides)
    return await auto_merge_promotion(SimpleNamespace(number=30), **kwargs)


async def test_phase5_healthy_promotion_goes_through_the_common_policy(world):
    pr = _phase5_world(world)
    head, base = pr["head"]["sha"], world.server.base_tip

    out = await _promote(world)

    assert out.merged is True and out.continue_loop is True, out.reason
    (call,) = world.server.merge_calls()
    assert call[2] == {"merge_method": "squash", "sha": head}
    commit = world.server.commits[world.server.base_tip]
    assert commit["parents"] == [base] and commit["tree"] == "tree-improve"
    assert world.manager.get_phase5_incident(world.project_id) is None


async def test_phase5_requires_the_improve_delivery_proof(world):
    _phase5_world(world, proof_phase="build")

    out = await _promote(world)

    assert out.merged is False and out.continue_loop is False
    assert "preuve" in out.reason or "phase" in out.reason
    assert world.server.merge_calls() == []
    assert world.manager.get_phase5_incident(world.project_id) is None, "aucun write-ahead avant le refus"


async def test_phase5_without_proof_never_merges(world):
    world.server.open_pr(
        30,
        head_ref="collegue/improve-1",
        tree="tree-improve",
        files=[{"filename": "docs/x.md", "status": "modified", "additions": 3, "deletions": 0}],
    )

    out = await _promote(world)

    assert out.merged is False and world.server.merge_calls() == []


async def test_phase5_missing_required_check_never_merges(world):
    pr = _phase5_world(world)
    world.server.set_checks(pr["head"]["sha"], {n: "success" for n in FIVE_CHECKS[:-1]})

    out = await _promote(world)

    assert out.merged is False and world.server.merge_calls() == []
    assert world.manager.get_phase5_incident(world.project_id) is None


async def test_phase5_base_race_at_merge_time_is_refused_by_the_server_precondition(world):
    _phase5_world(world)
    world.server.before_merge = lambda s: s.advance_base(tree="tree-merged-by-someone-else")

    out = await _promote(world)

    assert out.merged is False and out.continue_loop is False
    assert world.server.prs[30]["merged"] is False
    assert world.server.commits[world.server.base_tip]["tree"] == "tree-merged-by-someone-else"


async def test_phase5_untrusted_precondition_is_caught_by_the_merge_commit_check(world):
    _phase5_world(world)
    world.server.ignore_strict = True
    world.server.before_merge = lambda s: s.advance_base(tree="tree-merged-by-someone-else")

    out = await _promote(world)

    assert out.merged is True and out.continue_loop is False
    assert out.stop_reason == "post_merge_guard_failed" and "preuve" in out.reason
    assert world.manager.get_phase5_incident(world.project_id).state == "attention"


@pytest.mark.parametrize("state", ["merge_pending", "merged_unsynced", "attention"])
async def test_merge_task_never_re_merges_a_task_that_already_has_an_unfinished_cycle(world, state):
    """Même appelé directement (hors reprise), ``merge_task`` n'émet AUCUN PUT pour une tâche à cycle inachevé."""
    task_id, pr = world.add_task(1)
    row = world.manager.begin_task_merge(
        task_id,
        owner=OWNER,
        repo=REPO,
        base_branch="main",
        pr_number=11,
        head_sha=pr["head"]["sha"],
        base_sha=world.server.base_tip,
        tree_sha=sha_of("tree-task-1"),
        proof_id="a" * 64,
        merge_method="squash",
    )
    if state == "merged_unsynced":
        world.manager.transition_task_merge(
            task_id, expected_state=row.state, expected_revision=0, new_state=state, merge_sha="d" * 40
        )
    elif state == "attention":
        world.manager.transition_task_merge(task_id, expected_state=row.state, expected_revision=0, new_state=state)

    result = await cycle.merge_task(
        world.manager,
        world.clients,
        world.manager.get_task(task_id),
        project_id=world.project_id,
        owner=OWNER,
        repo=REPO,
        base="main",
        pr_number=11,
        head_branch=branch_for_issue(1),
        repo_source="/unused",
        resync_fn=world.sync,
        proof_loader=world.proofs.loader,
        ci_timeout_seconds=0.0,
        ci_poll_seconds=0.0,
        sleep_fn=_no_sleep,
        verify_fn=_accept_local_sync,
    )

    assert result.status == cycle.STATUS_PENDING
    assert world.server.merge_calls() == [] and world.sync.calls == 0
    assert world.cycle_row(1).state == state


async def test_repo_sync_failed_from_an_external_merge_skips_the_final_drain_and_phase4(world, monkeypatch):
    """Le driver a refusé de lancer quoi que ce soit (clone non resynchronisé) : ni drain ni handoff n'enchaînent."""
    world.add_task(1)
    improvement = []

    async def fake_improvement(*a, **k):
        improvement.append(1)

    stopped = ProjectRunResult(stop_reason="repo_sync_failed", iterations=0, processed=[])
    result = await _run(world, monkeypatch, passes=[stopped], improve=True, run_improvement_fn=fake_improvement)

    assert result.stop_reason == "repo_sync_failed" and improvement == []
    assert world.server.merge_calls() == [], "aucune fusion n'est émise par le drain"
    assert world.status(1) == "in_review"
