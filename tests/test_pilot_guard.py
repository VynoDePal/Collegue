"""Tests H3 (#394) : garde post-merge (santé de main → auto-revert si rouge)."""

import subprocess
from types import SimpleNamespace

import pytest

from collegue.pilot.guard import (
    RevertPolicy,
    check_main_health,
    guard_post_merge,
)
from collegue.sandbox import SandboxResult


class _Sandbox:
    def __init__(self, exit_code=0, raises=False, stdout="2 passed in 0.1s"):
        self._exit = exit_code
        self._raises = raises
        self._stdout = stdout

    def run_tests(self, workspace, command="pytest -q"):
        if self._raises:
            raise RuntimeError("docker indisponible")
        return SandboxResult(exit_code=self._exit, stdout=self._stdout, stderr="")


class _Manager:
    def __init__(self):
        self.decisions = []

    def record_decision(self, project_id, summary, rationale=None):
        self.decisions.append((summary, rationale))


class _Audit:
    def __init__(self):
        self.events = []

    def record(self, kind, **detail):
        self.events.append((kind, detail))


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """Dépôt avec 2 commits ; renvoie (chemin, sha_du_dernier_commit_à_réverter)."""
    src = tmp_path / "main"
    src.mkdir()
    _git(src, "init", "-q")
    _git(src, "config", "user.email", "t@example.com")
    _git(src, "config", "user.name", "Test")
    (src / "f.txt").write_text("v1\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "v1")
    (src / "f.txt").write_text("v2\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "merge bad change")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True).stdout.strip()
    return str(src), sha


def _policy(enabled=True):
    return RevertPolicy(enabled=enabled, health_command="pytest -q")


# --- check_main_health ----------------------------------------------------------


def test_health_green(repo):
    src, _ = repo
    res = check_main_health(src, sandbox=_Sandbox(exit_code=0))
    assert res.healthy is True and res.exit_code == 0


def test_health_red(repo):
    src, _ = repo
    res = check_main_health(src, sandbox=_Sandbox(exit_code=1))
    assert res.healthy is False


def test_health_sandbox_unavailable_is_failclosed(repo):
    src, _ = repo
    res = check_main_health(src, sandbox=_Sandbox(raises=True))
    assert res.healthy is False and "sandbox" in res.reason


def test_health_bad_repo_is_failclosed(tmp_path):
    res = check_main_health(str(tmp_path / "nope"), sandbox=_Sandbox(exit_code=0))
    assert res.healthy is False and "clone" in res.reason


def test_health_no_tests_marker_is_failclosed(repo):
    # Exit 0 mais aucun test exécuté (« collected 0 items ») → non concluant.
    src, _ = repo
    res = check_main_health(src, sandbox=_Sandbox(exit_code=0, stdout="collected 0 items"))
    assert res.healthy is False and "aucun test" in res.reason


def test_health_unsafe_command_is_failclosed(repo):
    # Une commande qui pourrait masquer un échec (|| true) est refusée (fail-closed).
    src, _ = repo
    res = check_main_health(src, sandbox=_Sandbox(exit_code=0), command="pytest -q || true")
    assert res.healthy is False and "non sûre" in res.reason


def test_health_clone_missing_commit_is_failclosed(repo):
    # Le clone (source périmée) ne contient pas le commit mergé → non concluant.
    src, _ = repo
    absent = "0123456789abcdef0123456789abcdef01234567"
    res = check_main_health(src, sandbox=_Sandbox(exit_code=0), merge_sha=absent)
    assert res.healthy is False and "ne contient pas" in res.reason


# --- guard_post_merge -----------------------------------------------------------


def test_guard_disabled_does_nothing(repo):
    src, sha = repo
    out = guard_post_merge(src, sha, sandbox=_Sandbox(exit_code=1), policy=_policy(enabled=False))
    assert out.checked is False and out.reverted is False


def test_guard_healthy_no_revert(repo):
    src, sha = repo
    out = guard_post_merge(src, sha, sandbox=_Sandbox(exit_code=0), policy=_policy())
    assert out.checked is True and out.healthy is True and out.reverted is False


def test_guard_red_prepares_revert_and_logs(repo):
    src, sha = repo
    manager, audit = _Manager(), _Audit()
    out = guard_post_merge(
        src, sha, sandbox=_Sandbox(exit_code=1), policy=_policy(), manager=manager, project_id=1, audit=audit
    )
    assert out.checked is True and out.healthy is False and out.reverted is True
    assert out.revert.branch and out.revert.branch.startswith("collegue/revert-")
    assert manager.decisions and "Auto-revert" in manager.decisions[0][0]
    assert audit.events and audit.events[0][0] == "auto_revert"


def test_guard_inconclusive_health_reverts_failclosed(repo):
    # Santé non concluante (sandbox indispo) → traité comme rouge → revert.
    src, sha = repo
    out = guard_post_merge(src, sha, sandbox=_Sandbox(raises=True), policy=_policy())
    assert out.healthy is False and out.reverted is True


def test_guard_revert_failure_escalates(repo):
    # Main rouge ET revert impossible (commit absent) → escalade, pas un faux succès.
    src, _ = repo
    absent = "0123456789abcdef0123456789abcdef01234567"
    manager, audit = _Manager(), _Audit()
    out = guard_post_merge(
        src, absent, sandbox=_Sandbox(exit_code=1), policy=_policy(), manager=manager, project_id=1, audit=audit
    )
    assert out.healthy is False and out.reverted is False and out.revert_failed is True
    assert audit.events and audit.events[0][0] == "auto_revert_failed"
    assert manager.decisions and "ÉCHEC" in manager.decisions[0][0]


def test_guard_invalid_sha_escalates_without_raising(repo):
    # Un SHA malformé fait lever prepare_revert (RevertError) : la garde l'attrape et
    # escalade (ne propage pas l'exception au-delà du filet de sécurité).
    src, _ = repo
    out = guard_post_merge(src, "not-a-sha", sandbox=_Sandbox(exit_code=1), policy=_policy())
    assert out.revert_failed is True and out.reverted is False


# --- RevertPolicy.from_settings -------------------------------------------------


def test_policy_off_when_automerge_off():
    p = RevertPolicy.from_settings(SimpleNamespace(AUTO_MERGE_ENABLED=False, AUTO_REVERT_ENABLED=True))
    assert p.enabled is False  # rien n'est auto-mergé → rien à réverter


def test_policy_on_follows_automerge_by_default():
    p = RevertPolicy.from_settings(SimpleNamespace(AUTO_MERGE_ENABLED=True))  # AUTO_REVERT_ENABLED défaut True
    assert p.enabled is True


def test_policy_can_be_disabled_explicitly():
    p = RevertPolicy.from_settings(SimpleNamespace(AUTO_MERGE_ENABLED=True, AUTO_REVERT_ENABLED=False))
    assert p.enabled is False  # filet désactivé explicitement (risqué)
