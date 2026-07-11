"""Tests de reprise des transactions Phase 5 persistantes (#593)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from collegue.improve.loop import run_improvement
from collegue.pilot.phase5_resume import resume_phase5_incident
from collegue.pilot.remote_revert import STATUS_PENDING, STATUS_RECOVERED

HEAD = "a" * 40
BASE = "b" * 40
MERGE = "c" * 40


def _incident(state="merge_pending", **overrides):
    payload = {
        "project_id": 7,
        "state": state,
        "revision": 0,
        "owner": "owner",
        "repo": "repo",
        "base_branch": "main",
        "source_pr_number": 42,
        "source_head_sha": HEAD,
        "base_sha_before_merge": BASE,
        "merge_method": "squash",
        "merge_sha": None if state == "merge_pending" else MERGE,
        "health_command": "pytest -q",
        "revert_enabled": True,
        "last_error": None,
        "revert_claim_token": None,
        "revert_claim_expires_at": None,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


class _Manager:
    def __init__(self, incident=None):
        self.incident = incident
        self.transitions = []
        self.clears = []

    def get_phase5_incident(self, project_id):
        assert project_id == 7
        return self.incident

    def transition_phase5_incident(self, project_id, **kwargs):
        assert project_id == 7
        assert self.incident is not None
        assert kwargs["expected_state"] == self.incident.state
        assert kwargs["expected_revision"] == self.incident.revision
        assert kwargs["expected_source_pr_number"] == self.incident.source_pr_number
        assert kwargs["expected_source_head_sha"] == self.incident.source_head_sha
        self.transitions.append(dict(kwargs))
        values = vars(self.incident).copy()
        values["state"] = kwargs["new_state"]
        values["revision"] += 1
        if "merge_sha" in kwargs:
            values["merge_sha"] = kwargs["merge_sha"]
        if "last_error" in kwargs:
            values["last_error"] = kwargs["last_error"]
        if self.incident.state == "revert_in_progress":
            values["revert_claim_token"] = None
            values["revert_claim_expires_at"] = None
        self.incident = SimpleNamespace(**values)
        return self.incident

    def claim_phase5_revert(self, project_id, **kwargs):
        assert self.incident.state in {"revert_pending", "revert_in_progress"}
        values = vars(self.incident).copy()
        values.update(
            state="revert_in_progress",
            revision=self.incident.revision + 1,
            revert_claim_token="claim",
            revert_claim_expires_at=object(),
        )
        self.incident = SimpleNamespace(**values)
        return self.incident

    def clear_phase5_incident(self, project_id, **kwargs):
        assert project_id == 7
        assert self.incident is not None
        assert kwargs == {
            "expected_state": self.incident.state,
            "expected_revision": self.incident.revision,
            "expected_source_pr_number": self.incident.source_pr_number,
            "expected_source_head_sha": self.incident.source_head_sha,
        }
        self.clears.append(dict(kwargs))
        self.incident = None
        return True


def _pr(*, merged=False, head_sha=HEAD, base_sha=BASE, state="open", draft=False):
    return SimpleNamespace(
        number=42,
        state="closed" if merged else state,
        draft=draft,
        merged=merged,
        head_sha=head_sha,
        base_sha=base_sha,
        base_branch="main",
        merge_commit_sha=MERGE if merged else None,
    )


class _PRs:
    def __init__(self, current=None, *, states=("success",), merge_response_lost=False):
        self.current = current or _pr()
        self.states = tuple(states)
        self.merge_response_lost = merge_response_lost
        self.merge_calls = []
        self.reads = 0

    def get_pr(self, owner, repo, number):
        assert (owner, repo, number) == ("owner", "repo", 42)
        self.reads += 1
        return self.current

    def get_commit_checks(self, owner, repo, sha):
        assert (owner, repo, sha) == ("owner", "repo", HEAD)
        return SimpleNamespace(complete=True, states=self.states)

    def merge_pr(self, owner, repo, number, **kwargs):
        self.merge_calls.append((owner, repo, number, kwargs))
        self.current = _pr(merged=True)
        if self.merge_response_lost:
            raise RuntimeError("réponse perdue après PUT")
        return SimpleNamespace(merged=True, sha=MERGE)


class _Branches:
    def __init__(self, main_sha=MERGE):
        self.main_sha = main_sha
        self.reads = []

    def get_branch_sha(self, owner, repo, branch):
        self.reads.append((owner, repo, branch))
        return self.main_sha

    def get_git_commit(self, owner, repo, sha):
        assert (owner, repo, sha) == ("owner", "repo", MERGE)
        return SimpleNamespace(sha=MERGE, tree_sha="f" * 40, parents=[BASE])


def _clients(prs=None, branches=None):
    return SimpleNamespace(prs=prs or _PRs(), branches=branches or _Branches())


def _green_guard(*args, **kwargs):
    return SimpleNamespace(checked=True, healthy=True, reverted=False, reason="main verte")


async def _resume(manager, *, clients=None, **kwargs):
    guard_fn = kwargs.pop("guard_fn", _green_guard)
    kwargs.setdefault("auto_merge_enabled", True)
    return await resume_phase5_incident(
        7,
        manager=manager,
        clients=clients or _clients(),
        owner="owner",
        repo="repo",
        base="main",
        repo_source="/repo",
        sandbox=object(),
        sync_base_fn=lambda source, base: True,
        guard_fn=guard_fn,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_no_incident_allows_improvement_without_github_access():
    manager = _Manager()
    clients = SimpleNamespace()

    outcome = await _resume(manager, clients=clients)

    assert outcome.found is False
    assert outcome.continue_loop is True and outcome.stop_reason is None
    assert manager.transitions == [] and manager.clears == []


@pytest.mark.asyncio
async def test_merge_pending_already_merged_resumes_health_then_clears():
    manager = _Manager(_incident())
    prs = _PRs(_pr(merged=True))

    outcome = await _resume(manager, clients=_clients(prs=prs))

    assert outcome.continue_loop is True
    assert [item["new_state"] for item in manager.transitions] == ["health_pending"]
    assert manager.transitions[0]["merge_sha"] == MERGE
    assert manager.incident is None and len(manager.clears) == 1


@pytest.mark.asyncio
async def test_merge_pending_open_green_ci_merges_with_all_sha_guards():
    manager = _Manager(_incident())
    prs = _PRs(_pr(), states=("success",))

    outcome = await _resume(manager, clients=_clients(prs=prs))

    assert outcome.continue_loop is True and manager.incident is None
    assert prs.merge_calls == [
        (
            "owner",
            "repo",
            42,
            {
                "method": "squash",
                "expected_head_sha": HEAD,
                "expected_base_branch": "main",
                "expected_base_sha": BASE,
            },
        )
    ]


@pytest.mark.asyncio
async def test_merge_pending_mixed_success_and_pending_waits_without_attention():
    manager = _Manager(_incident())
    prs = _PRs(_pr(), states=("success", "pending"))
    outcome = await _resume(manager, clients=_clients(prs=prs))
    assert outcome.stop_reason == "phase5_incident_pending"
    assert manager.incident.state == "merge_pending" and prs.merge_calls == []


@pytest.mark.asyncio
async def test_merge_pending_does_not_merge_when_opt_in_was_removed():
    manager = _Manager(_incident())
    prs = _PRs(_pr(), states=("success",))
    outcome = await _resume(manager, clients=_clients(prs=prs), auto_merge_enabled=False)
    assert outcome.stop_reason == "phase5_incident_pending"
    assert manager.incident.state == "merge_pending" and prs.merge_calls == []


@pytest.mark.asyncio
async def test_lost_merge_response_is_reconciled_as_merged():
    manager = _Manager(_incident())
    prs = _PRs(_pr(), merge_response_lost=True)

    outcome = await _resume(manager, clients=_clients(prs=prs))

    assert outcome.continue_loop is True and manager.incident is None
    assert len(prs.merge_calls) == 1 and prs.reads >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "current",
    [
        _pr(head_sha="d" * 40),
        _pr(base_sha="e" * 40),
    ],
)
async def test_mobile_source_head_or_base_transitions_to_attention(current):
    manager = _Manager(_incident())

    outcome = await _resume(manager, clients=_clients(prs=_PRs(current)))

    assert outcome.continue_loop is False
    assert outcome.stop_reason == "phase5_incident_pending"
    assert manager.incident.state == "attention"
    assert manager.transitions[-1]["new_state"] == "attention"


@pytest.mark.asyncio
async def test_health_pending_green_clears_incident():
    manager = _Manager(_incident("health_pending"))

    outcome = await _resume(manager)

    assert outcome.continue_loop is True
    assert outcome.guard.healthy is True
    assert manager.incident is None and len(manager.clears) == 1


@pytest.mark.asyncio
async def test_health_pending_red_transitions_to_revert_and_recovers_remotely():
    manager = _Manager(_incident("health_pending"))
    local_revert = SimpleNamespace(reverted=True, workspace="/tmp/revert")
    remote_calls = []

    def red_guard(*args, **kwargs):
        return SimpleNamespace(
            checked=True,
            healthy=False,
            reverted=True,
            revert=local_revert,
            reason="main rouge",
        )

    def remote_revert(*args, **kwargs):
        remote_calls.append((args, kwargs))
        return SimpleNamespace(restored=True, status=STATUS_RECOVERED, reason="main restaurée")

    outcome = await _resume(manager, guard_fn=red_guard, remote_revert_fn=remote_revert)

    assert manager.transitions[0]["new_state"] == "revert_pending"
    assert remote_calls[0][0][:3] == (local_revert, MERGE, BASE)
    assert outcome.remote_revert.restored is True
    assert manager.incident.state == "recovered" and manager.clears == []


@pytest.mark.asyncio
async def test_revert_pending_without_workspace_uses_remote_reconstruction():
    manager = _Manager(_incident("revert_pending", last_error="workspace perdu"))
    calls = []

    async def remote_revert(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(restored=True, status=STATUS_RECOVERED, reason="reconstruit")

    outcome = await _resume(manager, remote_revert_fn=remote_revert)

    assert calls[0][0][:3] == (None, MERGE, BASE)
    assert outcome.remote_revert.restored is True
    assert manager.incident.state == "recovered"


@pytest.mark.asyncio
async def test_pending_remote_revert_is_preserved_for_next_resume():
    manager = _Manager(_incident("revert_pending"))

    def remote_revert(*args, **kwargs):
        return SimpleNamespace(restored=False, status=STATUS_PENDING, reason="CI encore pending")

    outcome = await _resume(manager, remote_revert_fn=remote_revert)

    assert outcome.continue_loop is False and outcome.stop_reason == STATUS_PENDING
    assert manager.incident.state == "revert_pending"
    assert manager.incident.last_error == "CI encore pending"
    assert manager.incident.revision == 2 and manager.clears == []


@pytest.mark.asyncio
async def test_recovered_remote_revert_stays_durable_until_acknowledged():
    manager = _Manager(_incident("revert_pending"))

    def remote_revert(*args, **kwargs):
        return SimpleNamespace(restored=True, status=STATUS_RECOVERED, reason="restauré")

    outcome = await _resume(manager, remote_revert_fn=remote_revert)

    assert manager.incident.state == "recovered" and manager.clears == []
    assert outcome.remote_revert.restored is True


@pytest.mark.asyncio
async def test_attention_incident_blocks_without_github_reads_or_mutation():
    manager = _Manager(_incident("attention", last_error="intervention requise"))

    class _NoReads:
        def __getattr__(self, name):
            raise AssertionError(f"appel GitHub inattendu: {name}")

    outcome = await _resume(manager, clients=SimpleNamespace(prs=_NoReads(), branches=_NoReads()))

    assert outcome.continue_loop is False
    assert outcome.stop_reason == "phase5_incident_pending"
    assert "intervention" in outcome.reason
    assert manager.incident.state == "attention"
    assert manager.transitions == [] and manager.clears == []


@pytest.mark.asyncio
async def test_recovered_incident_blocks_until_operator_ack_without_github_reads():
    manager = _Manager(_incident("recovered", last_error="restauré"))
    outcome = await _resume(manager, clients=SimpleNamespace())
    assert outcome.stop_reason == STATUS_RECOVERED and outcome.continue_loop is False
    assert manager.incident.state == "recovered"


@pytest.mark.asyncio
async def test_run_improvement_recovery_hook_blocks_before_first_round():
    calls = []

    async def recovery_hook():
        calls.append("recovery")
        return SimpleNamespace(
            continue_loop=False,
            stop_reason="phase5_incident_pending",
            reason="incident durable non résolu",
        )

    result = await run_improvement(
        7,
        "/repo-inutilisé",
        None,
        agent=object(),
        owner="owner",
        repo="repo",
        manager=object(),
        dry_run=False,
        recovery_hook=recovery_hook,
    )

    assert calls == ["recovery"]
    assert result.rounds == 0 and result.stop_reason == "phase5_incident_pending"
    assert result.rejected == [("phase5_recovery", "incident durable non résolu")]
