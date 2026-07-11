"""Tests Git Data de ``BranchCommands`` pour la publication du revert distant.

Le client HTTP est integralement simule. Ces tests verrouillent la preuve
``parent + tree + message`` utilisee par Phase 5-B, ainsi que l'idempotence sans
force-push.
"""

from __future__ import annotations

import pytest

from collegue.tools.base import ToolExecutionError
from collegue.tools.github_commands.branches import BranchCommands, GitCommitInfo

PARENT = "a" * 40
TREE = "b" * 40
COMMIT = "c" * 40
RACED_COMMIT = "d" * 40
OTHER = "e" * 40
MESSAGE = "revert exact"


def _commit_payload(sha=COMMIT, *, tree=TREE, parents=(PARENT,), message=MESSAGE):
    return {
        "sha": sha,
        "tree": {"sha": tree},
        "parents": [{"sha": parent} for parent in parents],
        "message": message,
    }


def test_get_git_commit_returns_exact_tree_parents_and_message():
    commands = BranchCommands(token=None)
    calls = []

    def fake_get(endpoint, params=None):
        calls.append((endpoint, params))
        return _commit_payload()

    commands._api_get = fake_get
    result = commands.get_git_commit("owner", "repo", COMMIT)

    assert result == GitCommitInfo(sha=COMMIT, tree_sha=TREE, parents=[PARENT], message=MESSAGE)
    assert calls == [(f"/repos/owner/repo/git/commits/{COMMIT}", None)]


@pytest.mark.parametrize(
    ("payload", "expected_fragment"),
    [
        (_commit_payload(sha=OTHER), "commit Git inattendu"),
        (_commit_payload(tree="tree-invalide"), "SHA du tree"),
        ({"sha": COMMIT, "tree": {"sha": TREE}, "parents": "non-liste", "message": MESSAGE}, "parents"),
        (_commit_payload(parents=("parent-invalide",)), "SHA parent"),
        (_commit_payload(message=None), "message"),
        (_commit_payload(message="   "), "message"),
    ],
)
def test_get_git_commit_rejects_malformed_or_mismatched_payload(payload, expected_fragment):
    commands = BranchCommands(token=None)
    commands._api_get = lambda endpoint, params=None: payload

    with pytest.raises(ToolExecutionError, match=expected_fragment):
        commands.get_git_commit("owner", "repo", COMMIT)


def test_get_git_commit_rejects_malformed_requested_sha_before_network():
    commands = BranchCommands(token=None)
    commands._api_get = lambda *args, **kwargs: pytest.fail("aucun appel reseau attendu")

    with pytest.raises(ToolExecutionError, match="SHA du commit"):
        commands.get_git_commit("owner", "repo", "court")


def test_ensure_commit_branch_creates_exact_commit_then_ref():
    commands = BranchCommands(token=None)
    branch_reads = iter((None, COMMIT))
    commands._branch_sha_or_none = lambda *args: next(branch_reads)
    calls = []

    def fake_get(endpoint, params=None):
        assert endpoint == f"/repos/owner/repo/git/commits/{COMMIT}"
        return _commit_payload()

    def fake_post(endpoint, data):
        calls.append((endpoint, data))
        if endpoint.endswith("/git/commits"):
            return {"sha": COMMIT}
        if endpoint.endswith("/git/refs"):
            return {"ref": "refs/heads/collegue/revert-test", "object": {"sha": COMMIT}}
        pytest.fail(f"endpoint inattendu: {endpoint}")

    commands._api_get = fake_get
    commands._api_post = fake_post

    result = commands.ensure_commit_branch(
        "owner",
        "repo",
        "collegue/revert-test",
        parent_sha=PARENT,
        tree_sha=TREE,
        message=MESSAGE,
    )

    assert result.name == "collegue/revert-test" and result.commit_sha == COMMIT
    assert calls == [
        (
            "/repos/owner/repo/git/commits",
            {"message": MESSAGE, "tree": TREE, "parents": [PARENT]},
        ),
        (
            "/repos/owner/repo/git/refs",
            {"ref": "refs/heads/collegue/revert-test", "sha": COMMIT},
        ),
    ]


def test_ensure_commit_branch_reuses_exact_existing_branch_without_write():
    commands = BranchCommands(token=None)
    commands._branch_sha_or_none = lambda *args: COMMIT
    commands._api_get = lambda endpoint, params=None: _commit_payload()
    commands._api_post = lambda *args, **kwargs: pytest.fail("aucune ecriture attendue")

    result = commands.ensure_commit_branch(
        "owner",
        "repo",
        "collegue/revert-test",
        parent_sha=PARENT,
        tree_sha=TREE,
        message=MESSAGE,
    )

    assert result.commit_sha == COMMIT


def test_ensure_commit_branch_rejects_existing_branch_with_different_message():
    commands = BranchCommands(token=None)
    commands._branch_sha_or_none = lambda *args: COMMIT
    commands._api_get = lambda endpoint, params=None: _commit_payload(message="autre propriétaire")
    commands._api_post = lambda *args, **kwargs: pytest.fail("aucune ecriture attendue")

    with pytest.raises(ToolExecutionError, match="parent/tree/message"):
        commands.ensure_commit_branch(
            "owner",
            "repo",
            "collegue/revert-test",
            parent_sha=PARENT,
            tree_sha=TREE,
            message=MESSAGE,
        )


def test_ensure_commit_branch_rejects_existing_multi_parent_commit():
    commands = BranchCommands(token=None)
    commands._branch_sha_or_none = lambda *args: COMMIT
    commands._api_get = lambda endpoint, params=None: _commit_payload(parents=(PARENT, OTHER))
    commands._api_post = lambda *args, **kwargs: pytest.fail("aucune ecriture attendue")

    with pytest.raises(ToolExecutionError, match="autre parent/tree"):
        commands.ensure_commit_branch(
            "owner",
            "repo",
            "collegue/revert-test",
            parent_sha=PARENT,
            tree_sha=TREE,
            message=MESSAGE,
        )


@pytest.mark.parametrize(
    "payload",
    [
        _commit_payload(tree=OTHER),
        _commit_payload(parents=(OTHER,)),
    ],
)
def test_ensure_commit_branch_rejects_divergent_homonymous_branch(payload):
    commands = BranchCommands(token=None)
    commands._branch_sha_or_none = lambda *args: COMMIT
    commands._api_get = lambda endpoint, params=None: payload
    commands._api_post = lambda *args, **kwargs: pytest.fail("aucune ecriture attendue")

    with pytest.raises(ToolExecutionError, match="autre parent/tree"):
        commands.ensure_commit_branch(
            "owner",
            "repo",
            "collegue/revert-test",
            parent_sha=PARENT,
            tree_sha=TREE,
            message=MESSAGE,
        )


def test_ensure_commit_branch_rejects_new_commit_with_different_message_before_ref():
    commands = BranchCommands(token=None)
    commands._branch_sha_or_none = lambda *args: None
    posts = []
    commands._api_get = lambda endpoint, params=None: _commit_payload(message="message modifié")

    def fake_post(endpoint, data):
        posts.append((endpoint, data))
        if endpoint.endswith("/git/commits"):
            return {"sha": COMMIT}
        pytest.fail("la ref ne doit pas être créée sans preuve exacte du message")

    commands._api_post = fake_post

    with pytest.raises(ToolExecutionError, match="parent/tree/message"):
        commands.ensure_commit_branch(
            "owner",
            "repo",
            "collegue/revert-test",
            parent_sha=PARENT,
            tree_sha=TREE,
            message=MESSAGE,
        )

    assert [endpoint for endpoint, _ in posts] == ["/repos/owner/repo/git/commits"]


def test_ensure_commit_branch_accepts_equivalent_creation_race():
    commands = BranchCommands(token=None)
    branch_reads = iter((None, RACED_COMMIT, RACED_COMMIT))
    commands._branch_sha_or_none = lambda *args: next(branch_reads)
    posts = []

    def fake_get(endpoint, params=None):
        sha = endpoint.rsplit("/", 1)[-1]
        assert sha in {COMMIT, RACED_COMMIT}
        return _commit_payload(sha=sha)

    def fake_post(endpoint, data):
        posts.append((endpoint, data))
        if endpoint.endswith("/git/commits"):
            return {"sha": COMMIT}
        if endpoint.endswith("/git/refs"):
            raise ToolExecutionError("Reference already exists")
        pytest.fail(f"endpoint inattendu: {endpoint}")

    commands._api_get = fake_get
    commands._api_post = fake_post

    result = commands.ensure_commit_branch(
        "owner",
        "repo",
        "collegue/revert-test",
        parent_sha=PARENT,
        tree_sha=TREE,
        message=MESSAGE,
    )

    assert result.commit_sha == RACED_COMMIT
    assert [endpoint for endpoint, _ in posts] == [
        "/repos/owner/repo/git/commits",
        "/repos/owner/repo/git/refs",
    ]


def test_ensure_commit_branch_rejects_creation_race_with_different_message():
    commands = BranchCommands(token=None)
    branch_reads = iter((None, RACED_COMMIT))
    commands._branch_sha_or_none = lambda *args: next(branch_reads)

    def fake_get(endpoint, params=None):
        sha = endpoint.rsplit("/", 1)[-1]
        if sha == COMMIT:
            return _commit_payload(sha=COMMIT)
        assert sha == RACED_COMMIT
        return _commit_payload(sha=RACED_COMMIT, message="message concurrent")

    def fake_post(endpoint, data):
        if endpoint.endswith("/git/commits"):
            return {"sha": COMMIT}
        raise ToolExecutionError("Reference already exists")

    commands._api_get = fake_get
    commands._api_post = fake_post

    with pytest.raises(ToolExecutionError, match="Reference already exists"):
        commands.ensure_commit_branch(
            "owner",
            "repo",
            "collegue/revert-test",
            parent_sha=PARENT,
            tree_sha=TREE,
            message=MESSAGE,
        )


@pytest.mark.parametrize(
    ("parent_sha", "tree_sha", "message"),
    [
        ("parent-court", TREE, "revert"),
        (PARENT, "tree-court", "revert"),
        (PARENT, TREE, "   "),
        (PARENT, TREE, None),
    ],
)
def test_ensure_commit_branch_rejects_malformed_sha_tree_or_message_before_write(parent_sha, tree_sha, message):
    commands = BranchCommands(token=None)
    commands._branch_sha_or_none = lambda *args: pytest.fail("aucune lecture de branche attendue")
    commands._api_post = lambda *args, **kwargs: pytest.fail("aucune ecriture attendue")

    with pytest.raises(ToolExecutionError):
        commands.ensure_commit_branch(
            "owner",
            "repo",
            "collegue/revert-test",
            parent_sha=parent_sha,
            tree_sha=tree_sha,
            message=message,
        )


def test_ensure_commit_branch_rejects_malformed_created_commit_sha():
    commands = BranchCommands(token=None)
    commands._branch_sha_or_none = lambda *args: None
    commands._api_post = lambda endpoint, data: {"sha": "malforme"}

    with pytest.raises(ToolExecutionError, match="commit cr"):
        commands.ensure_commit_branch(
            "owner",
            "repo",
            "collegue/revert-test",
            parent_sha=PARENT,
            tree_sha=TREE,
            message=MESSAGE,
        )
