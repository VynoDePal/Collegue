"""Reprise après une fusion survenue HORS moteur (opérateur, autre outil), par le runtime PUBLIC.

Vrais dépôts Git (source opérateur, distant nu, cloneur tiers), vrai SQLite, vrai ``run_project_from_settings`` et vrai
workspace géré ; seuls le client GitHub (la PR est « déjà fusionnée »), l'agent (observe puis s'arrête) et, dans les cas
d'échec, la resynchronisation sont doublés. ``BUILD_AUTO_MERGE`` est désactivé : le moteur n'émet AUCUNE fusion.
Défaut démontré (W3) : T1 passait ``merged`` et T2 partait d'un clone SANS le code livré par T1.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from collegue.executor.agent import AgentResult
from collegue.pilot import runtime
from collegue.planner import approve_plan
from collegue.state import ProjectStateManager

pytestmark = pytest.mark.asyncio


def git(path, *args):
    return subprocess.check_output(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=path,
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


class _Budget:
    def should_continue(self):
        return SimpleNamespace(action="continue", ok=True, reason="test")

    def time_remaining_seconds(self):
        return None


class _NoGate:
    def run_tests(self, *a, **k):
        raise AssertionError("l'agent d'observation s'arrête avant le gate")


class _Branches:
    def ensure_branch(self, *a, **k):
        raise AssertionError("aucune publication attendue")


class World:
    """Source opérateur + distant nu où la livraison de T1 a été fusionnée hors moteur."""

    def __init__(self, root: Path, *, synced: bool, independent: bool = False):
        self.source = root / "source"
        self.source.mkdir()
        git(self.source, "init", "-q", "-b", "main")
        (self.source / "base.txt").write_text("base\n")
        git(self.source, "add", ".")
        git(self.source, "commit", "-q", "-m", "base")
        self.remote = root / "remote.git"
        git(root, "clone", "-q", "--bare", str(self.source), str(self.remote))
        git(self.source, "remote", "add", "origin", str(self.remote))
        git(self.source, "fetch", "-q", "origin")
        third = root / "third-party"
        git(root, "clone", "-q", str(self.remote), str(third))
        (third / "dependency.txt").write_text("code livré par T1\n")
        git(third, "add", ".")
        git(third, "commit", "-q", "-m", "T1 livré (fusion manuelle)")
        git(third, "push", "-q", "origin", "main")
        self.merge_sha = git(third, "rev-parse", "HEAD")
        if synced:
            git(self.source, "fetch", "-q", "origin", "main")
            git(self.source, "reset", "-q", "--hard", "origin/main")
        self.db_url = f"sqlite:///{root / 'state.db'}"
        self.state = ProjectStateManager.from_url(self.db_url, create=True)
        self.project_id = self.state.create_project(name="resume", spec="# Deux tâches dépendantes\n")
        self.first = self.state.add_task(self.project_id, title="Dependency")
        self.second = self.state.add_task(
            self.project_id, title="Consumer", depends_on=None if independent else [self.first]
        )
        approve_plan(self.state, self.project_id)
        self.state.update_task_status(self.first, "in_review")
        self.visits = []
        self.merge_requests = []
        self.discovery = "ok"  # "ok" | "unavailable" (panne GitHub) | "none" (PR introuvable)

    def head(self):
        return git(self.source, "rev-parse", "HEAD")

    def clients(self, merge_sha=None):
        world = self
        sha = merge_sha or self.merge_sha

        class Prs:
            def find_pr_by_head(self, owner, repo, head, base=None, state="open"):
                if world.discovery == "unavailable":
                    raise OSError("panne temporaire de l'API GitHub")
                if world.discovery == "none":
                    return None
                if head == f"collegue/issue-{world.first}":
                    return SimpleNamespace(
                        number=11,
                        state="closed",
                        merged=True,
                        merge_commit_sha=sha,
                        head_branch=head,
                        base_branch="main",
                    )
                return None

            def merge_pr(self, *a, **k):
                world.merge_requests.append((a, k))
                raise AssertionError("le moteur ne doit émettre aucune fusion")

        return SimpleNamespace(prs=Prs(), branches=_Branches(), files=_Branches())

    def agent(self):
        world = self

        class Agent:
            budget_enforcement = "test-double"

            def implement_issue(self, workspace, issue):
                world.visits.append(
                    {
                        "task": issue.source_task_id,
                        "dependency_present": (Path(workspace) / "dependency.txt").exists(),
                        "managed": Path(str(workspace) + ".control").exists(),
                    }
                )
                return AgentResult(success=False, logs="observation terminée", cost_authoritative=True)

        return Agent()

    async def run(self, **overrides):
        settings = SimpleNamespace(
            BUILD_AUTO_MERGE=False,
            DEPS_REQUIRE_MERGED=True,
            TASK_MAX_ATTEMPTS=1,
            TASK_RETRY_BACKOFF_SECONDS=0,
            AUTO_MERGE_ENABLED=False,
            GATE_ACCEPTANCE_TESTS=False,
        )
        kwargs = dict(
            owner="fixture",
            repo="fixture",
            ctx=object(),
            base="main",
            dry_run=False,
            settings_obj=settings,
            manager=self.state,
            sandbox=_NoGate(),
            agent=self.agent(),
            reviewer=object(),
            clients=self.clients(),
            budget=_Budget(),
            cost_source=lambda: (0.0, 0),
            max_iterations=1,
        )
        kwargs.update(overrides)
        return await runtime.run_project_from_settings(self.state_project(), str(self.source), **kwargs)

    def state_project(self):
        return self.project_id


@pytest.fixture
def stale(tmp_path):
    return World(tmp_path, synced=False)


@pytest.fixture
def synced(tmp_path):
    return World(tmp_path, synced=True)


async def test_witness_already_synchronised_source_starts_the_dependent_with_the_delivered_code(synced):
    await synced.run()
    assert synced.visits == [{"task": synced.second, "dependency_present": True, "managed": True}]
    assert synced.state.get_task(synced.first).status == "merged" and synced.merge_requests == []


async def test_stale_source_is_resynchronised_and_proved_before_the_dependent_starts(stale):
    assert stale.head() != stale.merge_sha
    await stale.run()
    assert stale.visits == [{"task": stale.second, "dependency_present": True, "managed": True}]
    assert stale.head() == stale.merge_sha and stale.state.get_task(stale.first).status == "merged"
    assert stale.merge_requests == []


@pytest.mark.parametrize("failure", ["returns_false", "raises"])
async def test_failed_resync_keeps_the_task_in_review_and_launches_nothing(stale, failure):
    stale_head = stale.head()

    def broken(src, base):
        if failure == "raises":
            raise RuntimeError("git indisponible")
        return False

    result = await stale.run(sync_base_fn=broken)

    assert result.stop_reason == "repo_sync_failed" and result.pending_reviews == [stale.first]
    assert stale.visits == [], "aucun agent n'a été lancé depuis un clone périmé"
    assert stale.state.get_task(stale.first).status == "in_review" and stale.head() == stale_head
    assert stale.state.get_task(stale.second).status == "todo" and stale.merge_requests == []


async def test_resync_that_succeeds_but_does_not_contain_the_merge_is_not_accepted(stale):
    """Resync « réussie » mais la fusion annoncée (SHA inconnu du clone) n'y figure pas : rien n'est lancé."""
    result = await stale.run(clients=stale.clients(merge_sha="e" * 40))
    assert result.stop_reason == "repo_sync_failed" and stale.visits == []
    assert stale.state.get_task(stale.first).status == "in_review"


async def test_interrupted_resume_continues_on_the_next_run_without_any_merge(stale):
    first = await stale.run(sync_base_fn=lambda *_: False)
    assert first.stop_reason == "repo_sync_failed" and stale.visits == []

    stale.state = ProjectStateManager.from_url(stale.db_url)  # nouveau processus, même base
    await stale.run()

    assert stale.state.get_task(stale.first).status == "merged"
    assert stale.visits == [{"task": stale.second, "dependency_present": True, "managed": True}]
    assert stale.merge_requests == []


async def test_a_managed_workspace_is_never_accepted_as_the_operator_checkout(stale, tmp_path):
    """Frontière de confiance : un workspace géré passé comme ``repo_source`` fait échouer la resynchronisation."""
    from collegue.executor.workspace import IssueSpec, prepare_workspace

    managed = prepare_workspace(str(stale.source), IssueSpec(number=99, title="x"), dest_root=str(tmp_path / "ws"))
    result = await runtime.run_project_from_settings(
        stale.project_id,
        str(managed.path),
        owner="fixture",
        repo="fixture",
        ctx=object(),
        base="main",
        dry_run=False,
        settings_obj=SimpleNamespace(
            BUILD_AUTO_MERGE=False,
            DEPS_REQUIRE_MERGED=True,
            TASK_MAX_ATTEMPTS=1,
            TASK_RETRY_BACKOFF_SECONDS=0,
            AUTO_MERGE_ENABLED=False,
            GATE_ACCEPTANCE_TESTS=False,
        ),
        manager=stale.state,
        sandbox=_NoGate(),
        agent=stale.agent(),
        reviewer=object(),
        clients=stale.clients(),
        budget=_Budget(),
        cost_source=lambda: (0.0, 0),
        max_iterations=1,
    )
    assert result.stop_reason == "repo_sync_failed" and stale.visits == []


# ── le blocage « fusion connue, synchronisation non prouvée » survit au redémarrage ───────────────


@pytest.fixture
def twin(tmp_path):
    """Deux tâches INDÉPENDANTES (aucune dépendance stricte ne protège T2) ; clone périmé."""
    return World(tmp_path, synced=False, independent=True)


def reopen(world):
    """Nouveau processus : nouveau gestionnaire sur la même base."""
    world.state = ProjectStateManager.from_url(world.db_url)
    return world


@pytest.mark.parametrize("discovery", ["unavailable", "none"])
async def test_sync_barrier_survives_a_restart_even_when_github_discovery_fails_afterwards(twin, discovery):
    first = await twin.run(sync_base_fn=lambda *_: False)
    assert first.stop_reason == "repo_sync_failed" and twin.visits == []
    rows = twin.state.list_task_merges(twin.project_id)
    assert [(r.task_id, r.state, r.merge_sha, r.pr_number) for r in rows] == [
        (twin.first, "merged_unsynced", twin.merge_sha, 11)
    ], "le fait « fusion connue, synchronisation non prouvée » est persisté"

    reopen(twin)
    twin.discovery = discovery
    still_broken = await twin.run(sync_base_fn=lambda *_: False)
    assert twin.visits == [], "T2 (indépendante) ne part JAMAIS d'un clone sans le contenu livré"
    assert still_broken.stop_reason in {"repo_sync_failed", "merge_sync_pending"}
    assert twin.state.get_task(twin.first).status == "in_review"

    reopen(twin)
    await twin.run()  # sync réelle + verify_local_sync réel, découverte toujours en panne
    assert twin.state.get_task(twin.first).status == "merged"
    assert twin.state.get_task(twin.first).status == "merged" and twin.head() == twin.merge_sha
    assert twin.visits == [{"task": twin.second, "dependency_present": True, "managed": True}]
    assert twin.merge_requests == [], "aucune seconde fusion"
    assert [r.state for r in twin.state.list_task_merges(twin.project_id)] == ["synced"]


class SimulatedCrash(BaseException):
    """Mort du processus entre la persistance de la barrière et la synchronisation."""


async def test_crash_between_observation_and_sync_is_resumed_from_the_known_merge_sha(twin):
    def die(src, base):
        raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        await twin.run(sync_base_fn=die)
    assert twin.visits == []
    assert [r.state for r in reopen(twin).state.list_task_merges(twin.project_id)] == ["merged_unsynced"]

    twin.discovery = "unavailable"
    await twin.run()
    assert twin.state.get_task(twin.first).status == "merged" and twin.merge_requests == []
    assert [v["dependency_present"] for v in twin.visits] == [True]


@pytest.mark.parametrize("failure", ["returns_false", "raises"])
async def test_failed_sync_is_recorded_and_retried_without_discovery(twin, failure):
    def broken(src, base):
        if failure == "raises":
            raise RuntimeError("git indisponible")
        return False

    await twin.run(sync_base_fn=broken)
    (row,) = twin.state.list_task_merges(twin.project_id)
    assert row.state == "merged_unsynced" and row.last_error and "resynchronisation" in row.last_error

    reopen(twin).discovery = "unavailable"
    await twin.run()
    assert twin.state.get_task(twin.first).status == "merged" and len(twin.visits) == 1


async def test_unproved_sync_is_not_accepted_on_resume_and_keeps_the_barrier(twin):
    """La resync « réussit » mais la fusion enregistrée n'est pas dans le clone : barrière conservée."""

    await twin.run(sync_base_fn=lambda *_: False)
    reopen(twin).discovery = "unavailable"
    (row,) = twin.state.list_task_merges(twin.project_id)
    result = await twin.run(sync_base_fn=lambda *_: True, merge_sync_verify_fn=_refuse_verify)
    assert twin.visits == [] and result.stop_reason in {"repo_sync_failed", "merge_sync_pending"}
    assert twin.state.get_task_merge(twin.first).state == "merged_unsynced" and row.merge_sha == twin.merge_sha


def _refuse_verify(src, sha, tree):
    raise RuntimeError("le clone local ne contient pas la fusion")


async def test_unreadable_or_unwritable_state_stays_blocking(twin, monkeypatch):
    """Erreur de persistance de la barrière : jamais de tâche lancée (ni marquage merged)."""

    def refuse(*a, **k):
        raise RuntimeError("base d'état indisponible")

    monkeypatch.setattr(twin.state, "begin_external_task_merge", refuse)
    result = await twin.run(sync_base_fn=lambda *_: True)
    assert twin.visits == [] and result.stop_reason == "repo_sync_failed"
    assert twin.state.get_task(twin.first).status == "in_review"

    monkeypatch.undo()
    monkeypatch.setattr(twin.state, "list_task_merges", refuse)
    with pytest.raises(RuntimeError, match="indisponible"):
        await twin.run(sync_base_fn=lambda *_: True)
    assert twin.visits == []
