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
    evaluate_automerge,
    is_sensitive,
    maybe_auto_merge,
)

GREEN = ["success", "success"]


def _policy(**kw):
    base = dict(enabled=True, max_loc=50, path_allowlist=DEFAULT_PATH_ALLOWLIST, method="squash")
    base.update(kw)
    return RiskPolicy(**base)


# --- §6 : off par défaut --------------------------------------------------------


def test_disabled_by_default_never_allows():
    decision = evaluate_automerge(["docs/x.md"], additions=1, deletions=0, checks=["success"], policy=RiskPolicy())
    assert decision.allowed is False and "§6" in decision.reason


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
        self, owner, repo, number, *, method="squash", commit_title=None, commit_message=None, expected_head_sha=None
    ):
        self.merged_calls.append({"number": number, "method": method, "sha": expected_head_sha})
        return SimpleNamespace(merged=True, sha="merged-sha", already_merged=False)


def _clients(prs):
    return SimpleNamespace(prs=prs)


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
    assert prs.merged_calls == [{"number": 42, "method": "squash", "sha": "abc123"}]


def test_evaluate_returns_decision_type():
    assert isinstance(evaluate_automerge(["docs/x.md"], checks=["success"], policy=_policy()), AutoMergeDecision)
