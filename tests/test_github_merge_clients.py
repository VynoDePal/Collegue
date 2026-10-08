"""Clients GitHub de la politique de fusion (W3-B) : pagination, ``app_id``, 404 de protection, erreurs, comparaison.

Transport mocké au niveau ``_api_get`` (aucun réseau, aucun jeton). Le comportement de bout en bout (rulesets,
protections, fusion) est exercé contre le faux serveur REST dans ``test_pilot_merge_*``.
"""

from __future__ import annotations

import pytest

from collegue.tools.base import ToolExecutionError
from collegue.tools.github_commands import BranchCommands, PRCommands

SHA_A, SHA_B = "a" * 40, "b" * 40


def _client(cls, handler):
    client = cls(token=None)
    calls = []

    def fake_get(endpoint, params=None):
        calls.append((endpoint, dict(params or {})))
        return handler(endpoint, dict(params or {}))

    client._api_get = fake_get
    client.calls = calls
    return client


# ── checks : pagination, app_id, statuts legacy ─────────────────────────────────────────────────


def _runs(start, count, *, app_id=15368, conclusion="success"):
    return [
        {"name": f"check-{i}", "status": "completed", "conclusion": conclusion, "app": {"id": app_id}}
        for i in range(start, start + count)
    ]


def test_check_details_read_every_page_and_keep_the_app_id():
    total = 130

    def handler(endpoint, params):
        if endpoint.endswith("/check-runs"):
            start = (params["page"] - 1) * params["per_page"]
            count = max(0, min(params["per_page"], total - start))
            return {"total_count": total, "check_runs": _runs(start, count)}
        return []

    client = _client(PRCommands, handler)
    details = client.get_commit_check_details("o", "r", SHA_A)

    assert details.complete is True and len(details.checks) == total
    assert {c.app_id for c in details.checks} == {15368} and {c.kind for c in details.checks} == {"check_run"}
    assert [p["page"] for e, p in client.calls if e.endswith("/check-runs")] == [1, 2]


def test_check_details_incomplete_when_pages_are_capped_or_malformed():
    def endless(endpoint, params):
        if endpoint.endswith("/check-runs"):
            return {"total_count": 10_000, "check_runs": _runs(0, params["per_page"])}
        return []

    assert _client(PRCommands, endless).get_commit_check_details("o", "r", SHA_A, max_pages=2).complete is False

    def malformed(endpoint, params):
        return {"check_runs": "x"} if endpoint.endswith("/check-runs") else []

    assert _client(PRCommands, malformed).get_commit_check_details("o", "r", SHA_A).complete is False


def test_check_details_pending_runs_and_latest_legacy_status_per_context():
    def handler(endpoint, params):
        if endpoint.endswith("/check-runs"):
            return {
                "total_count": 2,
                "check_runs": [
                    {"name": "ci", "status": "in_progress", "conclusion": None, "app": {"id": 7}},
                    {"name": "lint", "status": "completed", "conclusion": "skipped", "app": None},
                ],
            }
        return [
            {"context": "legacy", "state": "success"},
            {"context": "legacy", "state": "failure"},  # plus ancien : ignoré
        ]

    details = _client(PRCommands, handler).get_commit_check_details("o", "r", SHA_A)

    observed = {(c.name, c.state, c.app_id, c.kind) for c in details.checks}
    assert observed == {
        ("ci", "pending", 7, "check_run"),
        ("lint", "skipped", None, "check_run"),
        ("legacy", "success", None, "status"),
    }
    assert details.complete is True


def test_get_commit_checks_keeps_its_historical_shape():
    def handler(endpoint, params):
        return {"total_count": 1, "check_runs": _runs(0, 1)} if endpoint.endswith("/check-runs") else []

    checks = _client(PRCommands, handler).get_commit_checks("o", "r", SHA_A)
    assert checks.complete is True and list(checks.states) == ["success"]


# ── protections classiques ──────────────────────────────────────────────────────────────────────


def test_classic_protection_with_checks_and_app_ids():
    payload = {
        "required_status_checks": {
            "strict": True,
            "checks": [{"context": "ci", "app_id": 15368}, {"context": "lint", "app_id": -1}],
        },
        "enforce_admins": {"enabled": True},
    }
    protection = _client(BranchCommands, lambda e, p: payload).get_branch_protection("o", "r", "main")
    assert protection.strict is True and protection.enforce_admins is True and protection.has_required_status_checks
    assert [(c.context, c.app_id) for c in protection.required_checks] == [("ci", 15368), ("lint", None)]


def test_classic_protection_legacy_contexts_have_no_expected_app():
    payload = {"required_status_checks": {"strict": False, "contexts": ["ci", "lint"]}}
    protection = _client(BranchCommands, lambda e, p: payload).get_branch_protection("o", "r", "main")
    assert [(c.context, c.app_id) for c in protection.required_checks] == [("ci", None), ("lint", None)]
    assert protection.strict is False and protection.enforce_admins is False


def test_classic_protection_without_status_checks_section():
    protection = _client(BranchCommands, lambda e, p: {"enforce_admins": {"enabled": False}}).get_branch_protection(
        "o", "r", "main"
    )
    assert protection.has_required_status_checks is False and protection.required_checks == []


def test_protection_404_is_none_but_other_errors_propagate():
    def not_found(endpoint, params):
        raise ToolExecutionError("404", status_code=404)

    assert _client(BranchCommands, not_found).get_branch_protection("o", "r", "main") is None

    for code in (401, 403, 500):

        def failing(endpoint, params, _c=code):
            raise ToolExecutionError("boom", status_code=_c)

        with pytest.raises(ToolExecutionError):
            _client(BranchCommands, failing).get_branch_protection("o", "r", "main")


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"required_status_checks": "oops"},
        {"required_status_checks": {"checks": [{"app_id": 1}]}},
        {"required_status_checks": {"checks": ["x"]}},
    ],
)
def test_classic_protection_malformed_is_an_error_not_an_absence(payload):
    with pytest.raises(ToolExecutionError):
        _client(BranchCommands, lambda e, p: payload).get_branch_protection("o", "r", "main")


# ── rulesets ────────────────────────────────────────────────────────────────────────────────────


def test_branch_rules_are_read_across_pages():
    pages = {
        1: [{"type": "required_status_checks", "ruleset_id": 5, "parameters": {}}] * 100,
        2: [{"type": "deletion"}],
    }
    client = _client(BranchCommands, lambda e, p: pages[p["page"]])

    rules = client.get_branch_rules("o", "r", "main")

    assert len(rules) == 101 and rules[0].ruleset_id == 5 and rules[-1].type == "deletion"
    assert [p["page"] for _, p in client.calls] == [1, 2]


def test_branch_rules_truncated_or_malformed_fail_closed():
    full = [{"type": "deletion"}] * 100
    with pytest.raises(ToolExecutionError, match="tronquée"):
        _client(BranchCommands, lambda e, p: full).get_branch_rules("o", "r", "main", max_pages=3)
    with pytest.raises(ToolExecutionError):
        _client(BranchCommands, lambda e, p: {"not": "a list"}).get_branch_rules("o", "r", "main")
    with pytest.raises(ToolExecutionError):
        _client(BranchCommands, lambda e, p: [{"no_type": 1}]).get_branch_rules("o", "r", "main")


def test_ruleset_enforcement_and_bypass_are_normalised():
    payload = {"id": 9, "name": "main", "target": "branch", "enforcement": "Active", "current_user_can_bypass": "Never"}
    info = _client(BranchCommands, lambda e, p: payload).get_ruleset("o", "r", 9)
    assert (info.enforcement, info.current_user_can_bypass, info.target) == ("active", "never", "branch")

    unknown = _client(BranchCommands, lambda e, p: {"id": 9, "enforcement": "active"}).get_ruleset("o", "r", 9)
    assert unknown.current_user_can_bypass is None, "bypass inconnu n'est jamais présumé « jamais »"


def test_ruleset_wrong_id_or_invalid_id_is_refused():
    with pytest.raises(ToolExecutionError):
        _client(BranchCommands, lambda e, p: {"id": 10}).get_ruleset("o", "r", 9)
    for bad in (0, -1, True, "9"):
        with pytest.raises(ToolExecutionError):
            _client(BranchCommands, lambda e, p: {}).get_ruleset("o", "r", bad)


# ── acteur, rôle, comparaison, branche ──────────────────────────────────────────────────────────


def test_actor_and_role():
    assert _client(BranchCommands, lambda e, p: {"login": " bot "}).get_authenticated_login() == "bot"
    for payload in ({}, {"login": ""}, [], None):
        with pytest.raises(ToolExecutionError):
            _client(BranchCommands, lambda e, p, _x=payload: _x).get_authenticated_login()

    assert (
        _client(BranchCommands, lambda e, p: {"role_name": "Admin"}).get_collaborator_role("o", "r", "bot") == "admin"
    )
    assert (
        _client(BranchCommands, lambda e, p: {"permission": "write"}).get_collaborator_role("o", "r", "bot") == "write"
    )
    with pytest.raises(ToolExecutionError):
        _client(BranchCommands, lambda e, p: {}).get_collaborator_role("o", "r", "bot")


def test_compare_commits_reports_ancestry_and_validates_shas():
    payload = {"status": "Ahead", "ahead_by": 2, "behind_by": 0, "merge_base_commit": {"sha": SHA_A}}
    client = _client(BranchCommands, lambda e, p: payload)

    info = client.compare_commits("o", "r", SHA_A, SHA_B)

    assert (info.status, info.ahead_by, info.behind_by, info.merge_base_sha) == ("ahead", 2, 0, SHA_A)
    assert client.calls[0][0].endswith(f"/compare/{SHA_A}...{SHA_B}")
    with pytest.raises(ToolExecutionError):
        client.compare_commits("o", "r", "main", SHA_B)
    with pytest.raises(ToolExecutionError):
        _client(BranchCommands, lambda e, p: {"nope": 1}).compare_commits("o", "r", SHA_A, SHA_B)
    odd = _client(BranchCommands, lambda e, p: {"status": "diverged", "merge_base_commit": {"sha": "zzz"}})
    assert odd.compare_commits("o", "r", SHA_A, SHA_B).merge_base_sha is None


def test_get_branch_requires_a_full_commit_sha():
    ok = _client(BranchCommands, lambda e, p: {"name": "main", "commit": {"sha": SHA_A}, "protected": True})
    details = ok.get_branch("o", "r", "main")
    assert (details.name, details.commit_sha, details.protected) == ("main", SHA_A, True)
    with pytest.raises(ToolExecutionError):
        _client(BranchCommands, lambda e, p: {"commit": {"sha": "abc"}}).get_branch("o", "r", "main")
    with pytest.raises(ToolExecutionError):
        _client(BranchCommands, lambda e, p: {}).get_branch("o", "r", "bad..name")
