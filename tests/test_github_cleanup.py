"""Primitives GitHub de cleanup distant, avec identité gardée et sans réseau."""

from __future__ import annotations

import pytest

from collegue.tools.base import ToolExecutionError
from collegue.tools.github_commands import IssueCommands, LabelCommands, PRCommands, RepoCommands


def _repo_payload(repo_id: int = 123) -> dict:
    return {
        "id": repo_id,
        "name": "fixture",
        "full_name": "owner/fixture",
        "description": "nightly fixture",
        "html_url": "https://github.test/owner/fixture",
        "default_branch": "main",
        "language": "Python",
        "stargazers_count": 0,
        "forks_count": 0,
        "open_issues_count": 0,
        "private": True,
        "updated_at": "2026-07-10T00:00:00Z",
    }


def _pr_payload(*, state: str = "open", merged: bool = False) -> dict:
    return {
        "number": 17,
        "title": "Nightly",
        "state": state,
        "html_url": "https://github.test/owner/fixture/pull/17",
        "user": {"login": "collegue-bot"},
        "base": {"ref": "nightly/run-1", "sha": "b" * 40},
        "head": {"ref": "collegue/issue-42", "sha": "a" * 40},
        "created_at": "2026-07-10T00:00:00Z",
        "updated_at": "2026-07-10T00:01:00Z",
        "labels": [],
        "draft": False,
        "merged": merged,
        "merged_at": "2026-07-10T00:02:00Z" if merged else None,
        "body": "rapport\n<!-- nightly-run:1 -->",
    }


def _issue_payload(*, state: str = "open", pull_request: bool = False) -> dict:
    payload = {
        "number": 42,
        "title": "Nightly task",
        "state": state,
        "html_url": "https://github.test/owner/fixture/issues/42",
        "user": {"login": "collegue-bot"},
        "labels": [{"name": "collegue-nightly"}, {"name": "Run-1"}, {"name": "human-extra"}],
        "created_at": "2026-07-10T00:00:00Z",
        "updated_at": "2026-07-10T00:01:00Z",
        "body": "critère\n<!-- nightly-run:1 -->",
    }
    if pull_request:
        payload["pull_request"] = {"url": "https://api.github.test/pulls/42"}
    return payload


def test_repo_commands_expose_immutable_id_for_list_and_get():
    repos = RepoCommands(token=None)
    repos._api_get = lambda endpoint, params=None: (
        [_repo_payload(123)] if endpoint == "/user/repos" else _repo_payload(456)
    )

    listed = repos.list_repos(None)
    fetched = repos.get_repo("owner", "fixture")

    assert listed[0].id == 123
    assert fetched.id == 456


def test_list_prs_passes_base_filter_to_github():
    prs = PRCommands(token=None)
    calls = []

    def fake_get(endpoint, params=None):
        calls.append((endpoint, params))
        return [_pr_payload()]

    prs._api_get = fake_get
    result = prs.list_prs("owner", "fixture", state="all", limit=7, base="nightly/run-1")

    assert result[0].base_branch == "nightly/run-1"
    assert calls == [
        (
            "/repos/owner/fixture/pulls",
            {
                "state": "all",
                "per_page": 7,
                "sort": "updated",
                "direction": "desc",
                "base": "nightly/run-1",
            },
        )
    ]


def _pr_cleanup_client(payloads: list[dict]):
    prs = PRCommands(token=None)
    queue = list(payloads)
    calls = []

    def fake_get(endpoint, params=None):
        calls.append(("GET", endpoint, params))
        return queue.pop(0)

    def fake_request(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        return {"state": "closed"}

    prs._api_get = fake_get
    prs._request_json = fake_request
    return prs, calls


_PR_GUARDS = {
    "expected_head_sha": "a" * 40,
    "expected_head_branch": "collegue/issue-42",
    "expected_base_branch": "nightly/run-1",
    "body_marker": "<!-- nightly-run:1 -->",
}


def test_close_pr_validates_then_confirms_remote_close():
    prs, calls = _pr_cleanup_client([_pr_payload(), _pr_payload(state="closed")])

    closed = prs.close_pr("owner", "fixture", 17, **_PR_GUARDS)

    assert closed.state == "closed" and closed.merged is False
    assert [call[0] for call in calls] == ["GET", "PATCH", "GET"]
    assert calls[1][2] == {"json_data": {"state": "closed"}}


def test_close_pr_is_idempotent_when_already_closed_and_matching():
    prs, calls = _pr_cleanup_client([_pr_payload(state="closed")])

    assert prs.close_pr("owner", "fixture", 17, **_PR_GUARDS).state == "closed"
    assert [call[0] for call in calls] == ["GET"]


@pytest.mark.parametrize(
    ("guard", "value"),
    [
        ("expected_head_sha", "f" * 40),
        ("expected_head_branch", "collegue/issue-999"),
        ("expected_base_branch", "main"),
        ("body_marker", "<!-- another-run -->"),
    ],
)
def test_close_pr_refuses_identity_mismatch_before_patch(guard, value):
    prs, calls = _pr_cleanup_client([_pr_payload()])
    guards = {**_PR_GUARDS, guard: value}

    with pytest.raises(ToolExecutionError):
        prs.close_pr("owner", "fixture", 17, **guards)

    assert [call[0] for call in calls] == ["GET"]


def test_close_pr_refuses_merged_pr_even_when_other_guards_match():
    prs, calls = _pr_cleanup_client([_pr_payload(state="closed", merged=True)])

    with pytest.raises(ToolExecutionError, match="mergée"):
        prs.close_pr("owner", "fixture", 17, **_PR_GUARDS)

    assert [call[0] for call in calls] == ["GET"]


def test_close_pr_fails_closed_when_confirmation_stays_open():
    prs, calls = _pr_cleanup_client([_pr_payload(), _pr_payload()])

    with pytest.raises(ToolExecutionError, match="non confirmée"):
        prs.close_pr("owner", "fixture", 17, **_PR_GUARDS)

    assert [call[0] for call in calls] == ["GET", "PATCH", "GET"]


def test_list_issues_passes_labels_filter_and_excludes_pull_requests():
    issues = IssueCommands(token=None)
    calls = []

    def fake_get(endpoint, params=None):
        calls.append((endpoint, params))
        return [_issue_payload(), _issue_payload(pull_request=True)]

    issues._api_get = fake_get
    result = issues.list_issues("owner", "fixture", state="all", limit=9, labels="run-1,collegue-nightly")

    assert [issue.number for issue in result] == [42]
    assert calls[0][1]["labels"] == "run-1,collegue-nightly"


def test_create_issue_with_metadata_is_one_atomic_post():
    issues = IssueCommands(token=None)
    calls = []

    def fake_post(endpoint, data):
        calls.append((endpoint, data))
        return _issue_payload()

    issues._api_post = fake_post
    created = issues.create_issue_with_metadata(
        "owner",
        "fixture",
        "Nightly task",
        body="critère\n<!-- nightly-run:1 -->",
        labels=["collegue-nightly", "run-1"],
        milestone_number=7,
    )

    assert created.number == 42
    assert calls == [
        (
            "/repos/owner/fixture/issues",
            {
                "title": "Nightly task",
                "body": "critère\n<!-- nightly-run:1 -->",
                "labels": ["collegue-nightly", "run-1"],
                "milestone": 7,
            },
        )
    ]


def _issue_cleanup_client(payloads: list[dict]):
    issues = IssueCommands(token=None)
    queue = list(payloads)
    calls = []

    def fake_get(endpoint, params=None):
        calls.append(("GET", endpoint, params))
        return queue.pop(0)

    def fake_request(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        return {"state": "closed"}

    issues._api_get = fake_get
    issues._request_json = fake_request
    return issues, calls


_ISSUE_GUARDS = {
    "expected_labels": ["COLLEGUE-NIGHTLY", "run-1"],
    "body_marker": "<!-- nightly-run:1 -->",
}


def test_close_issue_validates_then_confirms_remote_close():
    issues, calls = _issue_cleanup_client([_issue_payload(), _issue_payload(state="closed")])

    closed = issues.close_issue("owner", "fixture", 42, **_ISSUE_GUARDS)

    assert closed.state == "closed"
    assert [call[0] for call in calls] == ["GET", "PATCH", "GET"]
    assert calls[1][2] == {"json_data": {"state": "closed"}}


def test_close_issue_is_idempotent_when_already_closed_and_matching():
    issues, calls = _issue_cleanup_client([_issue_payload(state="closed")])

    assert issues.close_issue("owner", "fixture", 42, **_ISSUE_GUARDS).state == "closed"
    assert [call[0] for call in calls] == ["GET"]


def test_close_issue_refuses_pull_request_before_patch():
    issues, calls = _issue_cleanup_client([_issue_payload(pull_request=True)])

    with pytest.raises(ToolExecutionError, match="pull request"):
        issues.close_issue("owner", "fixture", 42, **_ISSUE_GUARDS)

    assert [call[0] for call in calls] == ["GET"]


@pytest.mark.parametrize(
    "guards",
    [
        {"expected_labels": ["missing"], "body_marker": "<!-- nightly-run:1 -->"},
        {"expected_labels": ["run-1"], "body_marker": "<!-- another-run -->"},
    ],
)
def test_close_issue_refuses_identity_mismatch_before_patch(guards):
    issues, calls = _issue_cleanup_client([_issue_payload()])

    with pytest.raises(ToolExecutionError):
        issues.close_issue("owner", "fixture", 42, **guards)

    assert [call[0] for call in calls] == ["GET"]


def test_close_issue_fails_closed_when_confirmation_stays_open():
    issues, calls = _issue_cleanup_client([_issue_payload(), _issue_payload()])

    with pytest.raises(ToolExecutionError, match="non confirmée"):
        issues.close_issue("owner", "fixture", 42, **_ISSUE_GUARDS)

    assert [call[0] for call in calls] == ["GET", "PATCH", "GET"]


def test_get_issue_preserves_a_marker_after_two_thousand_characters():
    issues = IssueCommands(token=None)
    payload = _issue_payload()
    payload["body"] = "x" * 3000 + "\n<!-- collegue-task:42 -->"
    issues._api_get = lambda endpoint, params=None: payload

    fetched = issues.get_issue("owner", "fixture", 42)

    assert fetched.body.endswith("<!-- collegue-task:42 -->")
    assert len(fetched.body) > 3000


def test_delete_label_is_exact_idempotent_and_confirmed():
    labels = LabelCommands(token=None)
    responses = [
        [{"name": "collegue-nightly-1", "color": "ededed"}],
        [],
        [],
    ]
    calls = []

    def fake_get(endpoint, params=None):
        calls.append(("GET", endpoint))
        return responses.pop(0)

    def fake_request(method, endpoint, **kwargs):
        calls.append((method, endpoint))
        return None

    labels._api_get = fake_get
    labels._request_json = fake_request

    assert labels.delete_label(
        "owner",
        "fixture",
        "collegue-nightly-1",
        expected_name="collegue-nightly-1",
    )
    assert ("DELETE", "/repos/owner/fixture/labels/collegue-nightly-1") in calls
    assert labels.delete_label(
        "owner",
        "fixture",
        "collegue-nightly-1",
        expected_name="collegue-nightly-1",
    )


@pytest.mark.parametrize(
    ("expected_color", "expected_description", "message"),
    [("1d76db", "wrong", "description"), ("ffffff", "owner-proof", "couleur")],
)
def test_delete_label_refuses_wrong_ownership_metadata(expected_color, expected_description, message):
    labels = LabelCommands(token=None)
    labels._api_get = lambda endpoint, params=None: [
        {
            "name": "collegue-nightly-1",
            "color": "1d76db",
            "description": "owner-proof",
        }
    ]
    labels._request_json = lambda *args, **kwargs: pytest.fail("DELETE ne doit pas partir")

    with pytest.raises(ToolExecutionError, match=message):
        labels.delete_label(
            "owner",
            "fixture",
            "collegue-nightly-1",
            expected_name="collegue-nightly-1",
            expected_color=expected_color,
            expected_description=expected_description,
        )
