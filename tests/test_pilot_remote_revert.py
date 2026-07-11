"""Tests #593 : rollback distant exact, idempotent et sans boucle de revert."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from collegue.executor.revert import RevertResult, prepare_revert
from collegue.executor.workspace import cleanup_workspace
from collegue.pilot.remote_revert import (
    STATUS_BASE_MOVED,
    STATUS_HEALTH_FAILED,
    STATUS_PENDING,
    STATUS_PUBLISH_FAILED,
    STATUS_RECOVERED,
    RemoteRevertError,
    prove_local_revert,
    publish_and_merge_revert,
)


def _git(cwd: str | Path, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=input_text,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _head(cwd: str | Path) -> str:
    return _git(cwd, "rev-parse", "HEAD")


@dataclass
class _LocalCase:
    source: str
    revert: RevertResult
    method: str
    base_sha: str
    bad_sha: str
    base_tree: str
    bad_tree: str
    final_sha: str
    topic_sha: str | None = None


def _build_local_case(tmp_path: Path, method: str) -> _LocalCase:
    source = tmp_path / f"source-{method}"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    (source / "page.txt").write_text("version 1\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "base")
    base_sha = _head(source)
    base_tree = _git(source, "rev-parse", f"{base_sha}^{{tree}}")
    topic_sha = None

    if method == "squash":
        (source / "page.txt").write_text("version fautive\n", encoding="utf-8")
        _git(source, "add", "-A")
        _git(source, "commit", "-q", "-m", "promotion squash")
        bad_sha = _head(source)
        revert = prepare_revert(str(source), bad_sha)
    elif method == "merge":
        main_branch = _git(source, "symbolic-ref", "--short", "HEAD")
        _git(source, "checkout", "-q", "-b", "promotion")
        (source / "page.txt").write_text("version fautive\n", encoding="utf-8")
        _git(source, "add", "-A")
        _git(source, "commit", "-q", "-m", "promotion")
        topic_sha = _head(source)
        _git(source, "checkout", "-q", main_branch)
        _git(source, "merge", "--no-ff", "promotion", "-m", "merge promotion")
        bad_sha = _head(source)
        revert = prepare_revert(str(source), bad_sha, merge_parent=1)
    else:  # pragma: no cover - aide de fixture interne
        raise ValueError(method)

    bad_tree = _git(source, "rev-parse", f"{bad_sha}^{{tree}}")
    # Objet local représentant le résultat attendu d'un squash de la PR de revert.
    # Il n'est pas checkouté avant le faux resync distant.
    final_sha = _git(source, "commit-tree", base_tree, "-p", bad_sha, input_text="remote revert\n")
    return _LocalCase(
        source=str(source),
        revert=revert,
        method=method,
        base_sha=base_sha,
        bad_sha=bad_sha,
        base_tree=base_tree,
        bad_tree=bad_tree,
        final_sha=final_sha,
        topic_sha=topic_sha,
    )


@pytest.fixture
def squash_case(tmp_path, request):
    case = _build_local_case(tmp_path, "squash")
    request.addfinalizer(lambda: cleanup_workspace(case.revert.workspace))
    return case


@pytest.fixture
def merge_case(tmp_path, request):
    case = _build_local_case(tmp_path, "merge")
    request.addfinalizer(lambda: cleanup_workspace(case.revert.workspace))
    return case


def test_prove_local_revert_accepts_exact_squash(squash_case):
    proof = prove_local_revert(
        squash_case.revert,
        squash_case.bad_sha,
        squash_case.base_sha,
        merge_method="squash",
    )

    assert proof.bad_merge_sha == squash_case.bad_sha
    assert proof.expected_base_sha == squash_case.base_sha
    assert proof.restored_tree_sha == squash_case.base_tree
    assert proof.local_revert_sha == squash_case.revert.revert_sha


def test_prove_local_revert_accepts_exact_merge_commit(merge_case):
    proof = prove_local_revert(
        merge_case.revert,
        merge_case.bad_sha,
        merge_case.base_sha,
        merge_method="merge",
    )

    assert proof.restored_tree_sha == merge_case.base_tree
    assert _git(merge_case.source, "rev-list", "--parents", "-n", "1", merge_case.bad_sha).split() == [
        merge_case.bad_sha,
        merge_case.base_sha,
        merge_case.topic_sha,
    ]


def test_prove_local_revert_rejects_dirty_workspace(squash_case):
    Path(squash_case.revert.workspace, "untracked.txt").write_text("hors preuve\n", encoding="utf-8")

    with pytest.raises(RemoteRevertError, match="non propre"):
        prove_local_revert(
            squash_case.revert,
            squash_case.bad_sha,
            squash_case.base_sha,
            merge_method="squash",
        )


def test_prove_local_revert_rejects_wrong_restored_tree(squash_case):
    workspace = squash_case.revert.workspace
    Path(workspace, "extra.txt").write_text("ne vient pas de la base\n", encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "--amend", "--no-edit")
    tampered = replace(squash_case.revert, revert_sha=_head(workspace))

    with pytest.raises(RemoteRevertError, match="tree de base"):
        prove_local_revert(
            tampered,
            squash_case.bad_sha,
            squash_case.base_sha,
            merge_method="squash",
        )


def test_prove_local_revert_rejects_rebase_method(squash_case):
    with pytest.raises(RemoteRevertError, match="rebase"):
        prove_local_revert(
            squash_case.revert,
            squash_case.bad_sha,
            squash_case.base_sha,
            merge_method="rebase",
        )


class _Branches:
    def __init__(
        self,
        case: _LocalCase,
        *,
        branch_exists: bool = False,
        main_moved: bool = False,
        final_tree: str | None = None,
    ):
        self.case = case
        self.revert_branch = case.revert.branch
        self.revert_branch_sha = case.revert.revert_sha
        self.main_sha = "9" * 40 if main_moved else case.bad_sha
        bad_parents = [case.base_sha] if case.method == "squash" else [case.base_sha, case.topic_sha]
        self.commits = {
            case.base_sha: SimpleNamespace(sha=case.base_sha, tree_sha=case.base_tree, parents=[]),
            case.bad_sha: SimpleNamespace(sha=case.bad_sha, tree_sha=case.bad_tree, parents=bad_parents),
            self.revert_branch_sha: SimpleNamespace(
                sha=self.revert_branch_sha,
                tree_sha=case.base_tree,
                parents=[case.bad_sha],
            ),
            case.final_sha: SimpleNamespace(
                sha=case.final_sha,
                tree_sha=final_tree or case.base_tree,
                parents=[case.bad_sha],
            ),
        }
        self.branch_exists = branch_exists
        self.ensure_calls = []
        self.delete_calls = []

    def get_git_commit(self, owner, repo, sha):
        return self.commits[sha]

    def get_branch_sha(self, owner, repo, branch):
        if branch == "main":
            return self.main_sha
        if branch == self.revert_branch and self.branch_exists:
            return self.revert_branch_sha
        raise RuntimeError(f"branche absente: {branch}")

    def ensure_commit_branch(self, owner, repo, branch, *, parent_sha, tree_sha, message):
        self.ensure_calls.append((branch, parent_sha, tree_sha, message))
        assert branch == self.revert_branch
        assert parent_sha == self.case.bad_sha
        assert tree_sha == self.case.base_tree
        self.branch_exists = True
        return SimpleNamespace(name=branch, commit_sha=self.revert_branch_sha)

    def delete_branch(self, owner, repo, branch, *, default_branch=None):
        self.delete_calls.append((branch, default_branch))
        self.branch_exists = False
        return True


class _PRs:
    def __init__(
        self,
        case: _LocalCase,
        branches: _Branches,
        *,
        checks=("success",),
        existing: str | None = None,
        lose_merge_response: bool = False,
    ):
        self.case = case
        self.branches = branches
        self.checks = tuple(checks)
        self.lose_merge_response = lose_merge_response
        self.number = 77
        self.current = None
        self.create_calls = []
        self.merge_calls = []
        self.get_checks_calls = []
        if existing is not None:
            marker = f"<!-- collegue-auto-revert:{case.bad_sha}:{case.base_tree} -->"
            self.current = self._pr(body=f"revert existant\n\n{marker}", merged=existing == "merged")
            branches.branch_exists = existing != "merged"
            if existing == "merged":
                branches.main_sha = case.final_sha

    def _pr(self, *, body: str, merged: bool = False):
        return SimpleNamespace(
            number=self.number,
            state="closed" if merged else "open",
            draft=False,
            merged=merged,
            merge_commit_sha=self.case.final_sha if merged else None,
            head_branch=self.case.revert.branch,
            head_sha=self.case.revert.revert_sha,
            base_branch="main",
            base_sha=self.case.bad_sha,
            body=body,
        )

    def find_pr_by_head(self, owner, repo, head, base=None, state="open"):
        return SimpleNamespace(number=self.number) if self.current is not None else None

    def create_pr(self, owner, repo, title, head, base, body):
        self.create_calls.append((title, head, base, body))
        self.current = self._pr(body=body)
        return SimpleNamespace(number=self.number)

    def get_pr(self, owner, repo, number):
        assert number == self.number and self.current is not None
        return self.current

    def get_commit_checks(self, owner, repo, sha):
        self.get_checks_calls.append(sha)
        return SimpleNamespace(complete=True, states=self.checks)

    def merge_pr(
        self,
        owner,
        repo,
        number,
        *,
        method,
        expected_head_sha,
        expected_base_branch,
        expected_base_sha,
    ):
        self.merge_calls.append(
            {
                "number": number,
                "method": method,
                "head": expected_head_sha,
                "base": expected_base_branch,
                "base_sha": expected_base_sha,
            }
        )
        assert expected_head_sha == self.case.revert.revert_sha
        assert expected_base_sha == self.case.bad_sha
        self.current = self._pr(body=self.current.body, merged=True)
        self.branches.main_sha = self.case.final_sha
        if self.lose_merge_response:
            raise RuntimeError("réponse HTTP perdue après merge")
        return SimpleNamespace(merged=True, sha=self.case.final_sha)


def _sync_to_final(case: _LocalCase):
    def sync(repo_source, base):
        assert repo_source == case.source and base == "main"
        _git(repo_source, "reset", "--hard", case.final_sha)
        return True

    return sync


async def _publish(
    case: _LocalCase,
    branches: _Branches,
    prs: _PRs,
    *,
    sync_base_fn=None,
    health_fn=None,
    ci_timeout_seconds=0,
):
    return await publish_and_merge_revert(
        case.revert,
        case.bad_sha,
        case.base_sha,
        merge_method=case.method,
        enabled=True,
        clients=SimpleNamespace(branches=branches, prs=prs),
        owner="o",
        repo="r",
        base="main",
        repo_source=case.source,
        sandbox=object(),
        health_command="pytest -q",
        reason="garde rouge",
        ci_timeout_seconds=ci_timeout_seconds,
        ci_poll_seconds=0,
        sleep_fn=lambda _seconds: None,
        clock=lambda: 0,
        sync_base_fn=sync_base_fn or _sync_to_final(case),
        health_fn=health_fn or (lambda *a, **k: SimpleNamespace(healthy=True, reason="main verte")),
    )


async def test_remote_revert_success_is_exact_and_guarded(squash_case):
    branches = _Branches(squash_case)
    prs = _PRs(squash_case, branches)

    result = await _publish(squash_case, branches, prs)

    assert result.status == STATUS_RECOVERED and result.restored is True
    assert result.pr_number == 77 and result.merge_sha == squash_case.final_sha
    assert len(prs.create_calls) == len(prs.merge_calls) == 1
    assert prs.merge_calls[0] == {
        "number": 77,
        "method": "squash",
        "head": squash_case.revert.revert_sha,
        "base": "main",
        "base_sha": squash_case.bad_sha,
    }
    assert branches.delete_calls == [(squash_case.revert.branch, "main")]


async def test_remote_revert_refuses_mobile_base_without_publication(squash_case):
    branches = _Branches(squash_case, main_moved=True)
    prs = _PRs(squash_case, branches)

    result = await _publish(squash_case, branches, prs)

    assert result.status == STATUS_BASE_MOVED and result.restored is False
    assert branches.ensure_calls == [] and prs.create_calls == [] and prs.merge_calls == []


@pytest.mark.parametrize(
    ("checks", "expected_status", "reason"),
    [
        (("pending",), STATUS_PENDING, "timeout"),
        (("failure",), STATUS_PUBLISH_FAILED, "rouge"),
    ],
)
async def test_remote_revert_ci_pending_or_red_never_merges(squash_case, checks, expected_status, reason):
    branches = _Branches(squash_case)
    prs = _PRs(squash_case, branches, checks=checks)

    result = await _publish(squash_case, branches, prs)

    assert result.status == expected_status and reason in result.reason
    assert len(prs.create_calls) == 1 and prs.merge_calls == []


@pytest.mark.parametrize("existing", ["branch", "pr"])
async def test_remote_revert_reuses_existing_branch_or_pr(squash_case, existing):
    branches = _Branches(squash_case, branch_exists=True)
    prs = _PRs(squash_case, branches, existing="open" if existing == "pr" else None)

    result = await _publish(squash_case, branches, prs)

    assert result.status == STATUS_RECOVERED
    assert len(prs.merge_calls) == 1
    assert len(prs.create_calls) == (0 if existing == "pr" else 1)
    assert len(branches.ensure_calls) == 1


async def test_remote_revert_resumes_already_merged_pr_without_new_writes(squash_case):
    branches = _Branches(squash_case)
    prs = _PRs(squash_case, branches, existing="merged")

    result = await _publish(squash_case, branches, prs)

    assert result.status == STATUS_RECOVERED and result.merge_sha == squash_case.final_sha
    assert branches.ensure_calls == [] and prs.create_calls == [] and prs.merge_calls == []


async def test_remote_revert_reconstructs_proof_after_local_workspace_is_lost(squash_case):
    branches = _Branches(squash_case)
    prs = _PRs(squash_case, branches)
    cleanup_workspace(squash_case.revert.workspace)

    result = await publish_and_merge_revert(
        None,
        squash_case.bad_sha,
        squash_case.base_sha,
        merge_method="squash",
        enabled=True,
        clients=SimpleNamespace(branches=branches, prs=prs),
        owner="o",
        repo="r",
        base="main",
        repo_source=squash_case.source,
        sandbox=object(),
        health_command="pytest -q",
        ci_timeout_seconds=0,
        sync_base_fn=_sync_to_final(squash_case),
        health_fn=lambda *a, **k: SimpleNamespace(healthy=True, reason="main verte"),
    )

    assert result.status == STATUS_RECOVERED and result.restored is True


async def test_remote_revert_reconciles_lost_merge_response(squash_case):
    branches = _Branches(squash_case)
    prs = _PRs(squash_case, branches, lose_merge_response=True)

    result = await _publish(squash_case, branches, prs)

    assert result.status == STATUS_RECOVERED and len(prs.merge_calls) == 1


async def test_remote_revert_rejects_wrong_final_tree_even_if_health_would_be_green(squash_case):
    branches = _Branches(squash_case, final_tree="8" * 40)
    prs = _PRs(squash_case, branches)
    health_calls = []

    result = await _publish(
        squash_case,
        branches,
        prs,
        health_fn=lambda *a, **k: health_calls.append((a, k)) or SimpleNamespace(healthy=True, reason="vert"),
    )

    assert result.status == STATUS_HEALTH_FAILED and "tree final" in result.reason
    assert health_calls == []


async def test_remote_revert_stops_when_resync_fails(squash_case):
    branches = _Branches(squash_case)
    prs = _PRs(squash_case, branches)
    health_calls = []

    result = await _publish(
        squash_case,
        branches,
        prs,
        sync_base_fn=lambda *_: False,
        health_fn=lambda *a, **k: health_calls.append((a, k)) or SimpleNamespace(healthy=True, reason="vert"),
    )

    assert result.status == STATUS_HEALTH_FAILED and "resynchronisation" in result.reason
    assert health_calls == []


async def test_remote_revert_final_health_red_is_terminal(squash_case):
    branches = _Branches(squash_case)
    prs = _PRs(squash_case, branches)
    health_calls = []

    def red(*args, **kwargs):
        health_calls.append((args, kwargs))
        return SimpleNamespace(healthy=False, reason="toujours rouge")

    result = await _publish(squash_case, branches, prs, health_fn=red)

    assert result.status == STATUS_HEALTH_FAILED and "toujours rouge" in result.reason
    assert len(health_calls) == 1
    assert branches.delete_calls == []  # preuve conservée pour intervention, aucun second revert


async def test_remote_revert_main_move_during_final_health_is_not_reported_recovered(squash_case):
    branches = _Branches(squash_case)
    prs = _PRs(squash_case, branches)

    def move_main(*args, **kwargs):
        branches.main_sha = "7" * 40
        return SimpleNamespace(healthy=True, reason="snapshot vert")

    result = await _publish(squash_case, branches, prs, health_fn=move_main)

    assert result.status == STATUS_HEALTH_FAILED
    assert result.restored is False and branches.delete_calls == []
