"""Tests H2 (#393) : moteur de politique d'auto-merge (opt-in, off par défaut, fail-closed).

Inclut les régressions des contournements trouvés en revue adversariale : code
exécutable sous tests/docs (RCE CI), variantes de casse des fichiers sensibles, débordement
de glob au-delà du segment, plafond LOC à 0 = illimité, liste/CI incomplètes, merge sans SHA.
"""

from types import SimpleNamespace

from collegue.pilot.automerge import (
    DEFAULT_PATH_ALLOWLIST,
    AutoMergeDecision,
    RiskPolicy,
    auto_merge_promotion,
    evaluate_automerge,
    is_sensitive,
    maybe_auto_merge,
)
from collegue.tools.github_commands import CommitChecks, FileChange, PRFilesSnapshot, PRInfo

GREEN = ["success", "success"]


def _policy(**kw):
    base = dict(enabled=True, max_loc=50, path_allowlist=DEFAULT_PATH_ALLOWLIST, method="squash")
    base.update(kw)
    return RiskPolicy(**base)


# --- §6 : off par défaut --------------------------------------------------------


def test_disabled_by_default_never_allows():
    decision = evaluate_automerge(["docs/x.md"], additions=1, deletions=0, checks=["success"], policy=RiskPolicy())
    assert decision.allowed is False and "§6" in decision.reason


def test_rebase_method_is_refused_because_atomic_rollback_is_ambiguous():
    decision = evaluate_automerge(
        ["docs/x.md"],
        additions=1,
        checks=["success"],
        policy=_policy(method="rebase"),
    )
    assert decision.allowed is False and "rollback atomique" in decision.reason


# --- allowlist faible risque (balisage non exécutable) --------------------------


def test_allows_low_risk_markup():
    d = evaluate_automerge(
        ["docs/guide.md", "README.md", "docs/sub/page.rst"], additions=20, deletions=5, checks=GREEN, policy=_policy()
    )
    assert d.allowed is True


def test_rejects_app_code_outside_allowlist():
    d = evaluate_automerge(["collegue/app.py"], additions=3, deletions=0, checks=GREEN, policy=_policy())
    assert d.allowed is False


def test_rejects_when_any_file_outside_allowlist():
    d = evaluate_automerge(["docs/ok.md", "collegue/x.py"], additions=2, deletions=0, checks=GREEN, policy=_policy())
    assert d.allowed is False


# --- CRITICAL-1 : code exécutable jamais auto-mergé (RCE CI) ---------------------


def test_executable_test_infra_blocked():
    # conftest.py est importé par pytest → RCE. Tout .py est du code exécutable.
    for path in ("tests/conftest.py", "tests/__init__.py", "tests/test_x.py", "docs/conf.py", "setup.py"):
        assert is_sensitive(path) is True, path
    # Même si un opérateur ajoute tests/** à l'allowlist, le code reste bloqué (garde dure).
    d = evaluate_automerge(
        ["tests/conftest.py"], additions=1, checks=GREEN, policy=_policy(path_allowlist=("tests/**",))
    )
    assert d.allowed is False and "sensible" in d.reason


# --- CRITICAL-2 : variantes de casse + .github imbriqué -------------------------


def test_sensitive_case_insensitive():
    for path in (".ENV", "config/.Env.local", "Poetry.LOCK", "X.Lock", ".GitHub/workflows/x.yml"):
        assert is_sensitive(path) is True, path


def test_github_blocked_at_any_depth():
    assert is_sensitive("foo/.github/workflows/ci.yml") is True
    # Un .md sous un .GitHub (casse) ne doit pas passer via **/*.md.
    d = evaluate_automerge([".GitHub/workflows/x.md"], additions=1, checks=GREEN, policy=_policy())
    assert d.allowed is False and "sensible" in d.reason


def test_alembic_versions_and_migrations_blocked():
    assert is_sensitive("collegue/state/alembic/Versions/0006_x.py") is True
    assert is_sensitive("app/migrations/0001.py") is True


def test_traversal_blocked():
    assert is_sensitive("../etc/passwd") is True
    assert evaluate_automerge(["docs/../collegue/app.py"], checks=GREEN, policy=_policy()).allowed is False


# --- CRITICAL-3 : le glob ne déborde pas du segment -----------------------------


def test_glob_star_does_not_cross_slash():
    # "docs/*" ne doit PAS matcher un sous-dossier (le '*' reste dans un segment).
    assert (
        evaluate_automerge(["docs/sub/app.py"], checks=GREEN, policy=_policy(path_allowlist=("docs/*",))).allowed
        is False
    )
    # "**/*.md" matche bien le markdown à toute profondeur (cas légitime).
    assert evaluate_automerge(["a/b/c.md"], additions=1, checks=GREEN, policy=_policy()).allowed is True
    # Pas de faux positif de préfixe : "docs-evil/" ne matche PAS "docs/**".
    assert is_sensitive("docs-evil/notes.md") is False  # pas sensible (markdown)…
    assert (
        evaluate_automerge(["docs-evil/notes.md"], checks=GREEN, policy=_policy(path_allowlist=("docs/**",))).allowed
        is False  # …mais hors de l'allowlist "docs/**" → refusé
    )


# --- HIGH-1 : plafond LOC à 0 = fail-closed, pas illimité ------------------------


def test_max_loc_zero_fails_closed():
    d = evaluate_automerge(["docs/x.md"], additions=100000, checks=GREEN, policy=_policy(max_loc=0))
    assert d.allowed is False and "plafond" in d.reason


def test_from_settings_zero_loc_falls_back_to_default():
    for raw in (0, None, "", "0", False):
        policy = RiskPolicy.from_settings(SimpleNamespace(AUTO_MERGE_ENABLED=True, AUTO_MERGE_MAX_LOC=raw))
        assert policy.max_loc == 50, raw


# --- HIGH-3 / fail-closed sur diff incomplet ------------------------------------


def test_incomplete_file_list_fails_closed():
    d = evaluate_automerge(["docs/x.md"], additions=1, checks=GREEN, policy=_policy(), files_complete=False)
    assert d.allowed is False and "incomplète" in d.reason


# --- plafond LOC ----------------------------------------------------------------


def test_rejects_over_loc_cap():
    d = evaluate_automerge(["docs/big.md"], additions=40, deletions=20, checks=GREEN, policy=_policy(max_loc=50))
    assert d.allowed is False and "LOC" in d.reason


def test_loc_at_cap_allowed():
    d = evaluate_automerge(["docs/x.md"], additions=25, deletions=25, checks=GREEN, policy=_policy(max_loc=50))
    assert d.allowed is True


def test_negative_loc_fails_closed():
    d = evaluate_automerge(["docs/x.md"], additions=-1000, deletions=0, checks=GREEN, policy=_policy())
    assert d.allowed is False and "négatif" in d.reason


# --- vérifs CI (fail-closed) ----------------------------------------------------


def test_rejects_when_checks_pending_or_unknown():
    for checks in (None, [], ["success", "pending"], ["failure"], ["skipped"]):
        d = evaluate_automerge(["docs/x.md"], additions=1, checks=checks, policy=_policy())
        assert d.allowed is False, checks


# --- from_settings --------------------------------------------------------------


def test_from_settings_parses_csv_allowlist_and_flag():
    settings = SimpleNamespace(
        AUTO_MERGE_ENABLED=True,
        AUTO_MERGE_MAX_LOC=30,
        AUTO_MERGE_PATH_ALLOWLIST="docs/**, **/*.md , src/**",
        AUTO_MERGE_METHOD="merge",
    )
    policy = RiskPolicy.from_settings(settings)
    assert policy.enabled is True and policy.max_loc == 30 and policy.method == "merge"
    assert policy.path_allowlist == ("docs/**", "**/*.md", "src/**")


def test_from_settings_defaults_when_absent():
    policy = RiskPolicy.from_settings(SimpleNamespace())
    assert policy.enabled is False and policy.path_allowlist == DEFAULT_PATH_ALLOWLIST


# --- maybe_auto_merge (wiring vers H1) ------------------------------------------


class _PRs:
    def __init__(self):
        self.merged_calls = []

    def merge_pr(
        self,
        owner,
        repo,
        number,
        *,
        method="squash",
        commit_title=None,
        commit_message=None,
        expected_head_sha=None,
        expected_base_branch=None,
        expected_base_sha=None,
    ):
        self.merged_calls.append(
            {
                "number": number,
                "method": method,
                "sha": expected_head_sha,
                "base": expected_base_branch,
                "base_sha": expected_base_sha,
            }
        )
        return SimpleNamespace(merged=True, sha="merged-sha", already_merged=False)


class _Branches:
    def __init__(self, heads=None):
        self.heads = list(heads or ["merged-sha"])

    def get_branch_sha(self, owner, repo, branch):
        return self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]


def _clients(prs, branches=None):
    return SimpleNamespace(prs=prs, branches=branches or _Branches())


def test_maybe_auto_merge_disabled_does_not_merge():
    prs = _PRs()
    out = maybe_auto_merge(
        SimpleNamespace(number=7, head_sha="abc"),
        ["docs/x.md"],
        additions=1,
        checks=["success"],
        policy=RiskPolicy(),  # off
        clients=_clients(prs),
        owner="o",
        repo="r",
        dry_run=False,
    )
    assert out.merged is False and prs.merged_calls == []


def test_maybe_auto_merge_dry_run_does_not_merge():
    prs = _PRs()
    out = maybe_auto_merge(
        SimpleNamespace(number=7, head_sha="abc"),
        ["docs/x.md"],
        additions=1,
        checks=["success"],
        policy=_policy(),
        clients=_clients(prs),
        owner="o",
        repo="r",
        dry_run=True,
    )
    assert out.decision.allowed is True and out.merged is False and out.dry_run is True
    assert prs.merged_calls == []


def test_maybe_auto_merge_requires_head_sha():
    # MEDIUM-1 : sans SHA de tête, pas de garde anti-course → on refuse de merger.
    prs = _PRs()
    out = maybe_auto_merge(
        SimpleNamespace(number=7),  # ni head_sha ni sha
        ["docs/x.md"],
        additions=1,
        checks=["success"],
        policy=_policy(),
        clients=_clients(prs),
        owner="o",
        repo="r",
        dry_run=False,
    )
    assert out.merged is False and prs.merged_calls == []
    assert "SHA" in out.decision.reason


def test_maybe_auto_merge_real_calls_merge_pr():
    prs = _PRs()
    out = maybe_auto_merge(
        SimpleNamespace(number=42, head_sha="abc123"),
        ["docs/x.md", "docs/sub/page.md"],
        additions=10,
        deletions=2,
        checks=["success"],
        policy=_policy(method="squash"),
        clients=_clients(prs),
        owner="o",
        repo="r",
        dry_run=False,
    )
    assert out.merged is True
    assert prs.merged_calls == [{"number": 42, "method": "squash", "sha": "abc123", "base": None, "base_sha": None}]


def test_evaluate_returns_decision_type():
    assert isinstance(evaluate_automerge(["docs/x.md"], checks=["success"], policy=_policy()), AutoMergeDecision)


def test_maybe_auto_merge_emits_audit_events():
    # L'audit (H4) reçoit un événement `automerge_decision` à chaque décision → visible
    # au dashboard (#405). Refus puis merge réel.
    audit = SimpleNamespace(events=[])
    audit.record = lambda kind, **d: audit.events.append((kind, d))
    prs = _PRs()

    maybe_auto_merge(  # refusé (politique off)
        SimpleNamespace(number=7, head_sha="abc"),
        ["docs/x.md"],
        additions=1,
        checks=["success"],
        policy=RiskPolicy(),
        clients=_clients(prs),
        owner="o",
        repo="r",
        dry_run=False,
        audit=audit,
    )
    assert audit.events[-1][0] == "automerge_decision" and audit.events[-1][1]["allowed"] is False

    maybe_auto_merge(  # autorisé + merge réel
        SimpleNamespace(number=42, head_sha="abc123"),
        ["docs/x.md"],
        additions=5,
        checks=["success"],
        policy=_policy(),
        clients=_clients(prs),
        owner="o",
        repo="r",
        dry_run=False,
        audit=audit,
    )
    ev = audit.events[-1]
    assert ev[0] == "automerge_decision" and ev[1]["allowed"] is True and ev[1]["merged"] is True


# --- câblage produit Phase 5-A --------------------------------------------------


def _phase5_info(*, head_sha="head", base_sha="base", filename="docs/x.md"):
    return PRInfo(
        number=42,
        title="docs",
        state="open",
        html_url="https://gh/pr/42",
        user="bot",
        base_branch="main",
        head_branch="improve/docs",
        head_sha=head_sha,
        base_sha=base_sha,
        additions=1,
        deletions=0,
        changed_files=1,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


class _Phase5PRs(_PRs):
    def __init__(self, *, infos=None, check_states=("success",), filename="docs/x.md"):
        super().__init__()
        self.infos = list(infos or [_phase5_info(filename=filename)])
        self.check_states = list(check_states)
        self.reads = []
        self.filename = filename

    def get_pr(self, owner, repo, number):
        self.reads.append("pr")
        return self.infos.pop(0) if len(self.infos) > 1 else self.infos[0]

    def get_pr_files_snapshot(self, owner, repo, number, expected_count=None):
        self.reads.append("files")
        return PRFilesSnapshot(
            files=[FileChange(filename=self.filename, status="modified", additions=1, deletions=0, changes=1)],
            complete=True,
            expected_count=1,
        )

    def get_commit_checks(self, owner, repo, sha):
        self.reads.append(("checks", sha))
        states = (
            self.check_states.pop(0)
            if self.check_states and isinstance(self.check_states[0], (list, tuple))
            else self.check_states
        )
        return CommitChecks(states=list(states), names=["ci"] * len(states), complete=True)


class _Phase5State:
    def __init__(self):
        self.incident = None
        self.cleared = False
        self.transitions = []

    def begin_phase5_incident(self, project_id, **payload):
        self.incident = SimpleNamespace(
            project_id=project_id,
            state="merge_pending",
            revision=0,
            revert_claim_token=None,
            revert_claim_expires_at=None,
            **payload,
        )
        return self.incident

    def claim_phase5_revert(self, project_id, **payload):
        assert self.incident is not None and self.incident.state == "revert_pending"
        values = vars(self.incident).copy()
        values.update(
            state="revert_in_progress",
            revision=self.incident.revision + 1,
            revert_claim_token="claim",
            revert_claim_expires_at=object(),
        )
        self.incident = SimpleNamespace(**values)
        return self.incident

    def transition_phase5_incident(self, project_id, **payload):
        assert self.incident is not None
        assert payload["expected_state"] == self.incident.state
        assert payload["expected_revision"] == self.incident.revision
        self.transitions.append(payload.copy())
        values = vars(self.incident).copy()
        values.update(
            state=payload["new_state"],
            revision=self.incident.revision + 1,
            last_error=payload.get("last_error"),
        )
        if "merge_sha" in payload:
            values["merge_sha"] = payload["merge_sha"]
        if self.incident.state == "revert_in_progress":
            values["revert_claim_token"] = None
            values["revert_claim_expires_at"] = None
        self.incident = SimpleNamespace(**values)
        return self.incident

    def clear_phase5_incident(self, project_id, **payload):
        assert self.incident is not None
        assert payload["expected_state"] == self.incident.state
        assert payload["expected_revision"] == self.incident.revision
        self.cleared = True
        self.incident = None
        return True


async def test_phase5_success_merges_resyncs_then_guards():
    prs = _Phase5PRs()
    state = _Phase5State()
    events = []
    out = await auto_merge_promotion(
        SimpleNamespace(number=42),
        policy=_policy(),
        revert_policy=SimpleNamespace(enabled=True),
        clients=_clients(prs),
        owner="o",
        repo="r",
        repo_source="/repo",
        base="main",
        sandbox=object(),
        manager=state,
        project_id=1,
        ci_timeout_seconds=0,
        sync_base_fn=lambda src, base: events.append(("sync", src, base)) or True,
        guard_fn=lambda src, sha, **kw: (
            events.append(("guard", src, sha)) or SimpleNamespace(checked=True, healthy=True, reason="vert")
        ),
    )
    assert out.merged is True and out.continue_loop is True
    assert prs.merged_calls == [{"number": 42, "method": "squash", "sha": "head", "base": "main", "base_sha": "base"}]
    assert events == [("sync", "/repo", "main"), ("guard", "/repo", "merged-sha")]
    assert state.cleared is True


async def test_phase5_pending_timeout_leaves_pr_open_and_stops():
    prs = _Phase5PRs(check_states=("pending",))
    out = await auto_merge_promotion(
        SimpleNamespace(number=42),
        policy=_policy(),
        revert_policy=SimpleNamespace(enabled=True),
        clients=_clients(prs),
        owner="o",
        repo="r",
        repo_source="/repo",
        base="main",
        sandbox=object(),
        ci_timeout_seconds=0,
    )
    assert out.merged is False and out.continue_loop is False and "délai" in out.reason
    assert prs.merged_calls == []


async def test_phase5_head_move_is_rejected_before_merge():
    prs = _Phase5PRs(infos=[_phase5_info(), _phase5_info(head_sha="moved")])
    out = await auto_merge_promotion(
        SimpleNamespace(number=42),
        policy=_policy(),
        revert_policy=SimpleNamespace(enabled=True),
        clients=_clients(prs),
        owner="o",
        repo="r",
        repo_source="/repo",
        base="main",
        sandbox=object(),
    )
    assert out.merged is False and "bougé" in out.reason and prs.merged_calls == []


async def test_phase5_guard_red_stops_after_merge():
    prs = _Phase5PRs()
    state = _Phase5State()
    out = await auto_merge_promotion(
        SimpleNamespace(number=42),
        policy=_policy(),
        revert_policy=SimpleNamespace(enabled=True),
        clients=_clients(prs),
        owner="o",
        repo="r",
        repo_source="/repo",
        base="main",
        sandbox=object(),
        manager=state,
        project_id=1,
        sync_base_fn=lambda src, base: True,
        guard_fn=lambda *a, **kw: SimpleNamespace(checked=True, healthy=False, reverted=True, reason="rouge"),
    )
    assert out.merged is True and out.continue_loop is False
    assert out.stop_reason == "post_merge_guard_failed"
    assert state.incident.state == "attention"


async def test_phase5_main_move_during_health_guard_never_clears_incident():
    prs = _Phase5PRs()
    state = _Phase5State()
    branches = _Branches(["merged-sha", "merged-sha", "other-sha"])
    out = await auto_merge_promotion(
        SimpleNamespace(number=42),
        policy=_policy(),
        revert_policy=SimpleNamespace(enabled=True),
        clients=_clients(prs, branches),
        owner="o",
        repo="r",
        repo_source="/repo",
        base="main",
        sandbox=object(),
        manager=state,
        project_id=1,
        sync_base_fn=lambda src, base: True,
        guard_fn=lambda *a, **kw: SimpleNamespace(checked=True, healthy=True, reason="vert"),
    )
    assert out.continue_loop is False and out.stop_reason == "post_merge_guard_failed"
    assert state.incident.state == "attention" and state.cleared is False


async def test_phase5_guard_red_publishes_remote_revert_and_stops_recovered():
    prs = _Phase5PRs()
    state = _Phase5State()
    local_revert = SimpleNamespace(reverted=True, branch="collegue/revert-merged-sha")
    seen = {}

    async def remote(revert, bad_sha, base_sha, **kwargs):
        seen.update(revert=revert, bad_sha=bad_sha, base_sha=base_sha, kwargs=kwargs)
        return SimpleNamespace(
            restored=True,
            status="auto_revert_recovered",
            reason="main restaurée",
        )

    out = await auto_merge_promotion(
        SimpleNamespace(number=42),
        policy=_policy(),
        revert_policy=SimpleNamespace(enabled=True, revert_enabled=True, health_command="pytest -q"),
        clients=_clients(prs),
        owner="o",
        repo="r",
        repo_source="/repo",
        base="main",
        sandbox=object(),
        manager=state,
        project_id=1,
        sync_base_fn=lambda src, base: True,
        guard_fn=lambda *a, **kw: SimpleNamespace(
            checked=True,
            healthy=False,
            reverted=True,
            revert=local_revert,
            reason="rouge",
        ),
        remote_revert_fn=remote,
    )
    assert out.merged is True and out.continue_loop is False
    assert out.stop_reason == "auto_revert_recovered"
    assert out.remote_revert.restored is True
    assert seen["revert"] is local_revert
    assert seen["bad_sha"] == "merged-sha" and seen["base_sha"] == "base"
    assert seen["kwargs"]["merge_method"] == "squash"
    assert state.incident.state == "recovered"


async def test_phase5_remote_revert_failure_status_is_propagated():
    prs = _Phase5PRs()
    state = _Phase5State()

    async def remote(*args, **kwargs):
        return SimpleNamespace(
            restored=False,
            status="auto_revert_base_moved",
            reason="main a bougé",
        )

    out = await auto_merge_promotion(
        SimpleNamespace(number=42),
        policy=_policy(),
        revert_policy=SimpleNamespace(enabled=True, revert_enabled=True),
        clients=_clients(prs),
        owner="o",
        repo="r",
        repo_source="/repo",
        base="main",
        sandbox=object(),
        manager=state,
        project_id=1,
        sync_base_fn=lambda src, base: True,
        guard_fn=lambda *a, **kw: SimpleNamespace(
            checked=True,
            healthy=False,
            reverted=True,
            revert=SimpleNamespace(reverted=True),
            reason="rouge",
        ),
        remote_revert_fn=remote,
    )
    assert out.stop_reason == "auto_revert_base_moved"
    assert out.remote_revert.restored is False and out.continue_loop is False
    assert state.incident.state == "attention"


async def test_phase5_refuses_merge_when_durable_write_ahead_is_unavailable():
    prs = _Phase5PRs()
    out = await auto_merge_promotion(
        SimpleNamespace(number=42),
        policy=_policy(),
        revert_policy=SimpleNamespace(enabled=True),
        clients=_clients(prs),
        owner="o",
        repo="r",
        repo_source="/repo",
        base="main",
        sandbox=object(),
    )
    assert out.stop_reason == "phase5_incident_pending"
    assert prs.merged_calls == []


async def test_phase5_off_and_dry_run_perform_no_github_reads():
    class _NoReads:
        def get_pr(self, *args, **kwargs):
            raise AssertionError("aucun GET attendu")

    clients = _clients(_NoReads())
    for policy, dry_run in ((RiskPolicy(), False), (_policy(), True)):
        out = await auto_merge_promotion(
            SimpleNamespace(number=42),
            policy=policy,
            revert_policy=SimpleNamespace(enabled=True),
            clients=clients,
            owner="o",
            repo="r",
            repo_source="/repo",
            base="main",
            sandbox=object(),
            dry_run=dry_run,
        )
        assert out.continue_loop is True and out.merged is False
