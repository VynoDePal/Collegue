"""Tests H1 (#392) : merge_pr (PRCommands) + delete_branch (BranchCommands).

Client HTTP **mocké** (pas de réseau en CI) : on remplace les méthodes bas niveau
(``_api_get``/``_api_put``/``_request_json``/``_branch_sha_or_none``) par des fakes
qui enregistrent les appels, et on vérifie idempotence, garde anti-course, refus des
branches protégées et fail-closed.
"""

import pytest

from collegue.tools.base import ToolExecutionError
from collegue.tools.github_commands import (
    BranchCommands,
    CommitChecks,
    MergeResult,
    PRCommands,
    PRFilesSnapshot,
    PRNotMergeableError,
)


class _Recorder:
    def __init__(self):
        self.calls = []


def _pr_with(get_payload, *, put_payload=None):
    pr = PRCommands(token=None)
    rec = _Recorder()

    def fake_get(endpoint, params=None):
        rec.calls.append(("GET", endpoint))
        return get_payload

    def fake_put(endpoint, data):
        rec.calls.append(("PUT", endpoint, data))
        return put_payload or {}

    pr._api_get = fake_get
    pr._api_put = fake_put
    return pr, rec


# --- merge_pr -------------------------------------------------------------------


def test_merge_pr_success():
    pr, rec = _pr_with(
        {"merged": False, "state": "open", "head": {"sha": "abc1234def567"}},
        put_payload={"merged": True, "sha": "mergedsha123", "message": "merged"},
    )
    res = pr.merge_pr("o", "r", 7, method="squash", expected_head_sha="abc1234def567")
    assert isinstance(res, MergeResult)
    assert res.merged and res.sha == "mergedsha123" and not res.already_merged
    assert any(c[0] == "PUT" for c in rec.calls)


def test_merge_pr_idempotent_when_already_merged():
    # PR déjà mergée : état réel renvoyé par GitHub = state closed + merged true.
    pr, rec = _pr_with({"merged": True, "state": "closed", "merge_commit_sha": "x9"})
    res = pr.merge_pr("o", "r", 7)
    assert res.merged and res.already_merged and res.sha == "x9"
    assert not any(c[0] == "PUT" for c in rec.calls)  # pas de second merge (405 évité)


def test_merge_pr_sha_guard_refuses_moved_head():
    pr, _ = _pr_with({"merged": False, "state": "open", "head": {"sha": "aaa1111"}})
    with pytest.raises(ToolExecutionError):
        pr.merge_pr("o", "r", 7, expected_head_sha="bbb2222")


def test_merge_pr_refuses_closed_unmerged():
    pr, _ = _pr_with({"merged": False, "state": "closed", "head": {"sha": "aaa1111"}})
    with pytest.raises(ToolExecutionError):
        pr.merge_pr("o", "r", 7)


def test_merge_pr_invalid_method():
    pr, _ = _pr_with({"merged": False, "state": "open", "head": {"sha": "z"}})
    with pytest.raises(ToolExecutionError):
        pr.merge_pr("o", "r", 7, method="rebandon")


def test_merge_pr_invalid_owner():
    pr, _ = _pr_with({})
    with pytest.raises(ToolExecutionError):
        pr.merge_pr("o/..", "r", 7)  # owner avec traversée → refus avant tout appel


def test_merge_pr_fails_closed_on_unknown_state():
    # GET vide/partiel (pas de ``state``) → état non confirmé → refus de merger.
    pr, rec = _pr_with({})
    with pytest.raises(ToolExecutionError):
        pr.merge_pr("o", "r", 7)
    assert not any(c[0] == "PUT" for c in rec.calls)  # jamais de merge sur un état non vu


def test_merge_pr_empty_put_response_is_not_merged():
    # PUT sans corps de confirmation → on ne suppose PAS le succès (merged défaut False).
    pr, _ = _pr_with({"merged": False, "state": "open", "head": {"sha": "h1"}})  # put_payload None → {}
    res = pr.merge_pr("o", "r", 7)
    assert res.merged is False


def test_merge_pr_success_without_merge_sha_fails_closed():
    pr, _ = _pr_with(
        {"merged": False, "state": "open", "head": {"sha": "h1"}},
        put_payload={"merged": True, "message": "merged without sha"},
    )
    with pytest.raises(ToolExecutionError, match="sans SHA"):
        pr.merge_pr("o", "r", 7, expected_head_sha="h1")


def test_merge_pr_already_merged_still_checks_expected_head():
    pr, _ = _pr_with({"merged": True, "state": "closed", "head": {"sha": "moved"}, "merge_commit_sha": "merge-sha"})
    with pytest.raises(ToolExecutionError, match="tête"):
        pr.merge_pr("o", "r", 7, expected_head_sha="evaluated")


def test_merge_pr_checks_expected_base_branch_and_sha():
    pr, _ = _pr_with(
        {
            "merged": False,
            "state": "open",
            "head": {"sha": "head"},
            "base": {"ref": "main", "sha": "new-base"},
        }
    )
    with pytest.raises(ToolExecutionError, match="SHA de base"):
        pr.merge_pr(
            "o",
            "r",
            7,
            expected_head_sha="head",
            expected_base_branch="main",
            expected_base_sha="evaluated-base",
        )


# --- détection de conflit avant merge (#434) --------------------------------------


def test_merge_pr_conflict_detected_before_put():
    # Conflit signalé par GitHub (mergeable False / state dirty) → erreur TYPÉE
    # avant tout PUT, pour que l'appelant branche close+redo/update-branch au lieu
    # de parser un 405 générique.
    pr, rec = _pr_with(
        {"merged": False, "state": "open", "head": {"sha": "h"}, "mergeable": False, "mergeable_state": "dirty"}
    )
    with pytest.raises(PRNotMergeableError) as ei:
        pr.merge_pr("o", "r", 64)
    assert ei.value.pr_number == 64
    assert ei.value.mergeable_state == "dirty"
    assert not any(c[0] == "PUT" for c in rec.calls)


def test_merge_pr_mergeable_unknown_proceeds():
    # ``mergeable`` None (calcul GitHub en cours) n'est PAS un conflit confirmé :
    # on laisse le PUT trancher (sinon fausses alertes sur les PRs fraîches).
    pr, _ = _pr_with(
        {"merged": False, "state": "open", "head": {"sha": "h"}, "mergeable": None, "mergeable_state": "unknown"},
        put_payload={"merged": True, "sha": "s", "message": "ok"},
    )
    assert pr.merge_pr("o", "r", 7).merged is True


def test_merge_pr_behind_is_not_a_conflict():
    # ``behind`` (base avancée sans conflit) merge très bien : seul ``dirty``/
    # ``mergeable=False`` déclenche l'erreur typée.
    pr, _ = _pr_with(
        {"merged": False, "state": "open", "head": {"sha": "h"}, "mergeable": True, "mergeable_state": "behind"},
        put_payload={"merged": True, "sha": "s", "message": "ok"},
    )
    assert pr.merge_pr("o", "r", 7).merged is True


def test_merge_pr_put_405_retyped_as_not_mergeable():
    # GET avec ``mergeable`` périmé/absent → GitHub répond 405 au PUT : re-typé
    # pour offrir le même canal de réparation que la détection amont.
    pr, _ = _pr_with({"merged": False, "state": "open", "head": {"sha": "h"}})

    def fail_put(endpoint, data):
        raise ToolExecutionError("Erreur API GitHub: 405 Client Error: Method Not Allowed for url: .../merge")

    pr._api_put = fail_put
    with pytest.raises(PRNotMergeableError) as ei:
        pr.merge_pr("o", "r", 71)
    assert ei.value.pr_number == 71


def test_merge_pr_other_http_errors_propagate_untyped():
    # Une 500 quelconque reste une ToolExecutionError générique (pas un conflit).
    pr, _ = _pr_with({"merged": False, "state": "open", "head": {"sha": "h"}})

    def fail_put(endpoint, data):
        raise ToolExecutionError("Erreur API GitHub: 500 Server Error")

    pr._api_put = fail_put
    with pytest.raises(ToolExecutionError) as ei:
        pr.merge_pr("o", "r", 7)
    assert not isinstance(ei.value, PRNotMergeableError)


# --- mapping merged (réconciliation #442) -----------------------------------------


def _pr_payload(**overrides):
    payload = {
        "number": 72,
        "title": "t",
        "state": "closed",
        "html_url": "https://gh/pull/72",
        "user": {"login": "x"},
        "base": {"ref": "main"},
        "head": {"ref": "collegue/issue-11"},
        "created_at": "2026-06-10T00:00:00Z",
        "updated_at": "2026-06-10T01:00:00Z",
    }
    payload.update(overrides)
    return payload


def test_find_pr_by_head_maps_merged_from_merged_at():
    # L'endpoint de LISTE n'expose pas `merged` (bool) mais `merged_at` : une PR
    # closed+merged doit être distinguable d'une closed-abandonnée (#442).
    pr, _ = _pr_with([_pr_payload(merged_at="2026-06-10T02:00:00Z")])
    info = pr.find_pr_by_head("o", "r", "collegue/issue-11", state="all")
    assert info.merged is True and info.state == "closed"

    pr2, _ = _pr_with([_pr_payload(merged_at=None)])
    info2 = pr2.find_pr_by_head("o", "r", "collegue/issue-11", state="all")
    assert info2.merged is False


def test_get_pr_maps_merged_bool():
    pr, _ = _pr_with(
        _pr_payload(
            merged=True,
            head={"ref": "collegue/issue-11", "sha": "head-sha"},
            base={"ref": "main", "sha": "base-sha"},
            additions=7,
            deletions=2,
            changed_files=3,
            merge_commit_sha="merge-sha",
        )
    )
    info = pr.get_pr("o", "r", 72)
    assert info.merged is True
    assert (info.head_sha, info.base_sha, info.additions, info.deletions, info.changed_files) == (
        "head-sha",
        "base-sha",
        7,
        2,
        3,
    )


# --- contexte Phase 5 : fichiers/checks exhaustifs ------------------------------


def _file(number):
    return {
        "filename": f"docs/{number}.md",
        "status": "modified",
        "additions": 1,
        "deletions": 0,
        "changes": 1,
    }


def test_pr_files_snapshot_paginates_and_proves_completeness():
    pr = PRCommands(token=None)
    calls = []

    def fake_get(endpoint, params=None):
        calls.append((endpoint, params))
        return [_file(1), _file(2)] if params["page"] == 1 else [_file(3)]

    pr._api_get = fake_get
    snapshot = pr.get_pr_files_snapshot("o", "r", 7, expected_count=3, page_size=2)
    assert isinstance(snapshot, PRFilesSnapshot)
    assert snapshot.complete is True and [item.filename for item in snapshot.files] == [
        "docs/1.md",
        "docs/2.md",
        "docs/3.md",
    ]
    assert [params["page"] for _, params in calls] == [1, 2]


def test_pr_files_snapshot_fails_closed_on_truncation_or_duplicate():
    pr = PRCommands(token=None)
    pr._api_get = lambda endpoint, params=None: [_file(1), _file(1)]
    duplicate = pr.get_pr_files_snapshot("o", "r", 7, expected_count=2, page_size=2, max_pages=1)
    assert duplicate.complete is False

    pr._api_get = lambda endpoint, params=None: [_file(1)]
    truncated = pr.get_pr_files_snapshot("o", "r", 7, expected_count=2, page_size=2, max_pages=1)
    assert truncated.complete is False


def test_commit_checks_aggregate_check_runs_and_latest_legacy_statuses():
    pr = PRCommands(token=None)

    def fake_get(endpoint, params=None):
        if endpoint.endswith("/check-runs"):
            return {
                "total_count": 2,
                "check_runs": [
                    {"name": "tests", "status": "completed", "conclusion": "success"},
                    {"name": "docker", "status": "in_progress", "conclusion": None},
                ],
            }
        assert endpoint.endswith("/statuses")
        return [
            {"context": "legacy", "state": "success"},
            {"context": "legacy", "state": "failure"},  # ancien verdict ignoré
        ]

    pr._api_get = fake_get
    checks = pr.get_commit_checks("o", "r", "sha")
    assert isinstance(checks, CommitChecks) and checks.complete is True
    assert checks.states == ["success", "pending", "success"]
    assert checks.names == ["tests", "docker", "legacy"]


def test_commit_checks_malformed_endpoint_is_incomplete():
    pr = PRCommands(token=None)
    pr._api_get = lambda endpoint, params=None: {} if endpoint.endswith("/check-runs") else []
    assert pr.get_commit_checks("o", "r", "sha").complete is False


# --- delete_branch --------------------------------------------------------------


def _branches(sha_seq, *, default_branch="main", repo_get_error=False):
    """``sha_seq`` : valeurs successives renvoyées par ``_branch_sha_or_none``.

    ``default_branch`` : branche par défaut renvoyée par le GET ``/repos/{o}/{r}``.
    ``repo_get_error`` : si vrai, ce GET lève (simule une base non résolvable).
    """
    br = BranchCommands(token=None)
    rec = _Recorder()
    seq = list(sha_seq)

    def fake_sha(owner, repo, branch):
        return seq.pop(0) if seq else None

    def fake_get(endpoint, params=None):
        rec.calls.append(("GET", endpoint))
        if repo_get_error:
            raise ToolExecutionError("repo introuvable")
        return {"default_branch": default_branch}

    def fake_request(method, endpoint, **kw):
        rec.calls.append((method, endpoint))
        return None

    br._branch_sha_or_none = fake_sha
    br._api_get = fake_get
    br._request_json = fake_request
    return br, rec


def test_delete_branch_deletes_existing():
    br, rec = _branches(["sha-exists"])
    assert br.delete_branch("o", "r", "feat/x") is True
    assert any(c[0] == "DELETE" for c in rec.calls)


def test_delete_branch_idempotent_when_absent():
    br, rec = _branches([None])
    assert br.delete_branch("o", "r", "feat/x") is True
    assert not any(c[0] == "DELETE" for c in rec.calls)  # rien à supprimer


def test_delete_branch_refuses_protected():
    br, rec = _branches(["sha", "sha"])
    for protected in ("main", "master"):
        with pytest.raises(ToolExecutionError):
            br.delete_branch("o", "r", protected)
    assert rec.calls == []  # aucun appel réseau pour une branche protégée


def test_delete_branch_refuses_traversal():
    br, _ = _branches(["sha"])
    with pytest.raises(ToolExecutionError):
        br.delete_branch("o", "r", "../evil")


def test_delete_branch_allows_slash_in_name():
    # Un nom de branche légitime avec '/' ne doit PAS être rejeté.
    br, rec = _branches(["sha"])
    assert br.delete_branch("o", "r", "feat/h1-merge") is True


def test_delete_branch_protects_real_default_branch():
    # La VRAIE branche par défaut (ici ``develop``) est protégée, pas seulement main/master.
    br, rec = _branches(["sha"], default_branch="develop")
    with pytest.raises(ToolExecutionError):
        br.delete_branch("o", "r", "develop")
    assert not any(c[0] == "DELETE" for c in rec.calls)


def test_delete_branch_default_param_skips_repo_get():
    # Quand le caller fournit ``default_branch``, pas de round-trip GET /repos.
    br, rec = _branches(["sha"], default_branch="main")
    with pytest.raises(ToolExecutionError):
        br.delete_branch("o", "r", "trunk", default_branch="trunk")
    assert not any(c[0] == "GET" for c in rec.calls)


def test_delete_branch_fail_closed_when_default_unresolvable():
    # GET /repos échoue → on ne peut pas garantir qu'on ne supprime pas la base → refus.
    br, _ = _branches(["sha"], repo_get_error=True)
    with pytest.raises(ToolExecutionError):
        br.delete_branch("o", "r", "feat/x")
