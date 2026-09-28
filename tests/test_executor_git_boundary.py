"""Frontière Git/sandbox (vague 1, mission A) : le code Git écrit par l'agent ou
par les tests ne s'exécute jamais sur l'hôte.

L'audit a reproduit l'exécution hôte via ``core.fsmonitor`` planté dans
``.git/config`` par l'agent, puis déclenché par le ``git add -A`` de la capture.
Ces tests rejouent le cycle RÉEL (``prepare_workspace`` → agent hostile →
capture) sur des dépôts jetables, avec un témoin d'exécution HORS du workspace
(sous ``tmp_path``) : sa présence prouve une exécution sur l'hôte. Aucun réseau,
aucun secret, aucun chemin fixe.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from collegue.executor import AgentResult, FakeCodeAgent, IssueSpec, PrClients, execute_issue
from collegue.executor.git_boundary import TrustedGit
from collegue.executor.quality_gate import FakeReviewer
from collegue.executor.runner import capture_diff, run_issue
from collegue.executor.workspace import WorkspaceError, apply_seed_diff, prepare_workspace
from collegue.sandbox import SandboxResult

ISSUE = IssueSpec(number=731, title="Audit local de la frontière Git")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}

# Hooks que git peut déclencher pendant capture / seed / commit / revert / checkout.
_HOOK_NAMES = (
    "post-index-change",
    "pre-commit",
    "prepare-commit-msg",
    "commit-msg",
    "post-commit",
    "post-checkout",
    "post-merge",
    "post-rewrite",
    "reference-transaction",
    "pre-auto-gc",
    "fsmonitor-watchman",
)


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )


def _make_repo(path: Path, files=None) -> str:
    files = files or {"existing.txt": "original\n", "requirements.txt": "fastapi\n"}
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    for rel, content in files.items():
        (path / rel).write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return str(path)


@pytest.fixture
def source(tmp_path):
    return _make_repo(tmp_path / "source")


@pytest.fixture
def witness(tmp_path):
    """Témoin d'exécution HORS de tout workspace : créé => code exécuté sur l'hôte."""
    directory = tmp_path / "host-side"
    directory.mkdir()
    return directory / "host-witness.txt"


@pytest.fixture
def evil_repo(tmp_path, witness):
    """Dépôt hostile hors workspace (cible de redirection ``.git`` gitfile/symlink)."""
    evil = Path(_make_repo(tmp_path / "evil-repo", {"x.txt": "x\n"}))
    _plant_fsmonitor_absolute(evil, witness)
    _plant_hooks_dir(evil, witness)
    return evil


# --- charges hostiles ------------------------------------------------------------


def _script(path: Path, witness: Path, *, passthrough: bool = False) -> Path:
    body = f"#!/bin/sh\nprintf executed >> '{witness}'\n"
    body += 'if [ -n "$1" ] && [ -f "$1" ]; then cat "$1"; else cat; fi\n' if passthrough else "printf '\\0'\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _append_config(repo: Path, text: str) -> None:
    with (repo / ".git" / "config").open("a") as handle:
        handle.write("\n" + text + "\n")


def _plant_fsmonitor_relative(ws: Path, witness: Path) -> None:
    """Forme EXACTE de la sonde d'audit (chemin relatif au workspace)."""
    (ws / ".git" / "fsmonitor-audit").write_text(f"#!/bin/sh\nprintf hook-executed > {witness}\nprintf '\\0'\n")
    _append_config(ws, '[core]\n\tfsmonitor = "sh .git/fsmonitor-audit"')


def _plant_fsmonitor_absolute(ws: Path, witness: Path) -> None:
    hook = _script(ws / ".git" / "fsmonitor-abs", witness)
    _append_config(ws, f"[core]\n\tfsmonitor = {hook}")


def _plant_hooks_dir(ws: Path, witness: Path) -> None:
    for name in _HOOK_NAMES:
        _script(ws / ".git" / "hooks" / name, witness)


def _plant_hooks_path(ws: Path, witness: Path) -> None:
    for name in _HOOK_NAMES:
        _script(ws / ".git" / "evil-hooks" / name, witness)
    _append_config(ws, f"[core]\n\thooksPath = {ws / '.git' / 'evil-hooks'}")


def _plant_include(ws: Path, witness: Path) -> None:
    hook = _script(ws / ".git" / "inc-hook", witness)
    (ws / ".git" / "evil.inc").write_text(f"[core]\n\tfsmonitor = {hook}\n")
    _append_config(ws, "[include]\n\tpath = evil.inc")


def _plant_include_if(ws: Path, witness: Path) -> None:
    hook = _script(ws / ".git" / "incif-hook", witness)
    (ws / ".git" / "evil-if.inc").write_text(f"[core]\n\tfsmonitor = {hook}\n")
    _append_config(ws, '[includeIf "gitdir:/"]\n\tpath = evil-if.inc')


def _plant_clean_filter(ws: Path, witness: Path) -> None:
    clean = _script(ws / ".git" / "clean-filter", witness, passthrough=True)
    (ws / ".gitattributes").write_text("* filter=evil\n")
    _append_config(ws, f'[filter "evil"]\n\tclean = {clean}\n\tsmudge = {clean}')


def _plant_external_diff(ws: Path, witness: Path) -> None:
    ext = _script(ws / ".git" / "ext-diff", witness)
    _append_config(ws, f"[diff]\n\texternal = {ext}")


def _plant_textconv(ws: Path, witness: Path) -> None:
    conv = _script(ws / ".git" / "textconv", witness, passthrough=True)
    (ws / ".gitattributes").write_text("* diff=evil\n")
    _append_config(ws, f'[diff "evil"]\n\ttextconv = {conv}')


def _replace_git_with_gitfile(ws: Path, target: Path) -> None:
    shutil.rmtree(ws / ".git")
    (ws / ".git").write_text(f"gitdir: {target}/.git\n")


def _replace_git_with_symlink(ws: Path, target: Path) -> None:
    shutil.rmtree(ws / ".git")
    os.symlink(target / ".git", ws / ".git")


HOSTILE_PLANTS = {
    "fsmonitor-relatif-sonde-audit": lambda ws, w, evil: _plant_fsmonitor_relative(ws, w),
    "fsmonitor-absolu": lambda ws, w, evil: _plant_fsmonitor_absolute(ws, w),
    "hooks-par-defaut": lambda ws, w, evil: _plant_hooks_dir(ws, w),
    "core-hookspath": lambda ws, w, evil: _plant_hooks_path(ws, w),
    "include-path": lambda ws, w, evil: _plant_include(ws, w),
    "includeif": lambda ws, w, evil: _plant_include_if(ws, w),
    "filtre-clean": lambda ws, w, evil: _plant_clean_filter(ws, w),
    "diff-externe": lambda ws, w, evil: _plant_external_diff(ws, w),
    "textconv": lambda ws, w, evil: _plant_textconv(ws, w),
    "gitfile-redirige": lambda ws, w, evil: _replace_git_with_gitfile(ws, evil),
    "git-symlink": lambda ws, w, evil: _replace_git_with_symlink(ws, evil),
}


_WRITES_GITATTRIBUTES = frozenset({"filtre-clean", "textconv"})


class HostileAgent:
    """Agent non fiable : produit un changement légitime ET plante du code Git."""

    def __init__(self, plant, witness, evil, *, files=None):
        self._plant = plant
        self._witness = witness
        self._evil = evil
        self._files = files if files is not None else {"README.md": "Audit fixture\n"}

    def implement_issue(self, workspace, issue):
        ws = Path(workspace)
        for rel, content in self._files.items():
            target = ws / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self._plant(ws, self._witness, self._evil)
        assert not self._witness.exists(), "le témoin ne doit pas exister avant l'exécution hôte"
        return AgentResult(success=True, files_changed=tuple(self._files))


# --- 1. cycle réel prepare_workspace -> implement_issue hostile -> capture ---------


@pytest.mark.parametrize("kind", sorted(HOSTILE_PLANTS))
def test_real_cycle_hostile_agent_never_executes_on_host(kind, source, tmp_path, witness, evil_repo):
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    agent = HostileAgent(HOSTILE_PLANTS[kind], witness, evil_repo)

    result = run_issue(agent, ws, ISSUE)

    assert not witness.exists(), f"exécution hôte via {kind}"
    # Le changement bénin reste capturé et publiable (les charges à base de
    # ``.gitattributes`` déposent aussi ce fichier, légitimement visible du diff).
    assert result.changed is True and result.success is True
    expected = {"README.md"} | ({".gitattributes"} if kind in _WRITES_GITATTRIBUTES else set())
    assert set(result.files_changed) == expected
    assert "+Audit fixture" in result.diff


def test_audit_probe_shape_fsmonitor_is_not_executed(source, tmp_path, witness):
    """Reproduction de ``git_boundary_probe.py`` sur un NOUVEAU dépôt jetable."""
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "probe"))
    (Path(ws.path) / "README.md").write_text("Audit fixture\n")
    _plant_fsmonitor_relative(Path(ws.path), witness)

    diff, files = capture_diff(ws)

    assert not witness.exists()
    assert files == ("README.md",)
    assert "+Audit fixture" in diff


# --- 2. la référence de base et l'index du workspace ne sont pas une autorité ------


def test_agent_commit_in_workspace_git_does_not_hide_the_change(source, tmp_path):
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    (Path(ws.path) / "existing.txt").write_text("changé par l'agent\n")
    _git(ws.path, "commit", "-q", "-am", "l'agent commit lui-même", check=False)

    diff, files = capture_diff(ws)

    assert files == ("existing.txt",)
    assert "+changé par l'agent" in diff


def test_agent_assume_unchanged_or_skip_worktree_does_not_hide_the_change(source, tmp_path):
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    (Path(ws.path) / "existing.txt").write_text("modification masquée\n")
    _git(ws.path, "update-index", "--assume-unchanged", "existing.txt", check=False)
    _git(ws.path, "update-index", "--skip-worktree", "existing.txt", check=False)

    diff, files = capture_diff(ws)

    assert files == ("existing.txt",)
    assert "+modification masquée" in diff


def test_agent_rewriting_head_or_deleting_git_dir_does_not_change_the_base(source, tmp_path):
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    (Path(ws.path) / "existing.txt").write_text("nouveau contenu\n")
    (Path(ws.path) / ".git" / "HEAD").write_text("ref: refs/heads/inexistante\n")
    shutil.rmtree(Path(ws.path) / ".git" / "objects", ignore_errors=True)

    diff, files = capture_diff(ws)

    assert files == ("existing.txt",)
    assert "-original" in diff and "+nouveau contenu" in diff


def test_core_worktree_redirection_cannot_pull_in_outside_content(source, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SECRET.txt").write_text("secret hors workspace\n")
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    (Path(ws.path) / "README.md").write_text("ok\n")
    _append_config(Path(ws.path), f"[core]\n\tworktree = {outside}")

    diff, files = capture_diff(ws)

    assert files == ("README.md",)
    assert "SECRET" not in diff


def test_symlinks_out_of_workspace_are_recorded_as_links_never_followed(source, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SECRET.txt").write_text("secret hors workspace\n")
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    os.symlink(outside, Path(ws.path) / "leak-dir")
    os.symlink(outside / "SECRET.txt", Path(ws.path) / "leak-file")
    (Path(ws.path) / "README.md").write_text("ok\n")

    diff, files = capture_diff(ws)

    assert "secret hors workspace" not in diff
    assert set(files) == {"README.md", "leak-dir", "leak-file"}


def test_nested_git_repository_fails_closed(source, tmp_path, witness):
    """Un dépôt imbriqué (avec sa propre config hostile) n'est jamais interrogé."""
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    nested = Path(ws.path) / "vendor" / "lib"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q")
    (nested / "code.py").write_text("x = 1\n")
    _git(nested, "add", "-A")
    _git(nested, "commit", "-q", "-m", "n")
    # la charge hostile est posée APRÈS les commandes git du test lui-même
    _plant_fsmonitor_absolute(nested, witness)
    _plant_hooks_dir(nested, witness)
    _plant_clean_filter(nested, witness)

    with pytest.raises(WorkspaceError):
        capture_diff(ws)
    assert not witness.exists()


# --- 3. les changements bénins restent capturés / publiables ------------------------


def test_benign_changes_binary_add_delete_are_preserved(source, tmp_path):
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    root = Path(ws.path)
    (root / "assets").mkdir()
    (root / "assets" / "hero.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(64)))
    (root / "src").mkdir()
    (root / "src" / "new.py").write_text("x = 1\n")
    (root / "src" / "café ünï.py").write_text("y = 2\n")
    (root / "existing.txt").unlink()
    (root / "empty.txt").write_text("")
    (root / "run.sh").write_text("#!/bin/sh\n")
    (root / "run.sh").chmod(0o755)

    diff, files = capture_diff(ws)

    assert set(files) == {
        "assets/hero.png",
        "src/new.py",
        "src/café ünï.py",
        "existing.txt",
        "empty.txt",
        "run.sh",
    }
    assert "GIT binary patch" in diff
    assert "deleted file mode" in diff
    assert "new file mode 100755" in diff


def test_rename_reports_both_old_and_new_paths(source, tmp_path):
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    root = Path(ws.path)
    (root / "existing.txt").rename(root / "renamed.txt")

    _diff, files = capture_diff(ws)

    # Sans les deux chemins, la PR ne supprimerait jamais l'ancien fichier.
    assert set(files) == {"existing.txt", "renamed.txt"}


def test_recapture_after_tests_only_stages_requested_paths(source, tmp_path):
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    root = Path(ws.path)
    (root / "app.py").write_text("import httpx\n")
    first_diff, first_files = capture_diff(ws)
    assert first_files == ("app.py",)

    # « tests » : artefacts + remédiation déterministe de requirements.txt
    (root / "node_modules").mkdir()
    (root / "node_modules" / "artefact.js").write_text("// gate\n")
    (root / "requirements.txt").write_text("fastapi\nhttpx\n")

    diff, files = capture_diff(ws, paths=("requirements.txt",))

    assert set(files) == {"app.py", "requirements.txt"}
    assert "+httpx" in diff and "artefact.js" not in diff


def test_recapture_after_tests_mutated_git_metadata_is_still_safe(source, tmp_path, witness):
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    root = Path(ws.path)
    (root / "app.py").write_text("x = 1\n")
    capture_diff(ws)

    # les tests (sandbox RW) plantent APRÈS la première capture
    _plant_fsmonitor_absolute(root, witness)
    _plant_hooks_dir(root, witness)
    _plant_clean_filter(root, witness)
    (root / "requirements.txt").write_text("fastapi\nhttpx\n")

    diff, files = capture_diff(ws, paths=("requirements.txt",))

    assert not witness.exists()
    assert set(files) == {"app.py", "requirements.txt"}


# --- seed / retry ------------------------------------------------------------------


def test_seed_diff_reapplies_on_fresh_workspace_without_running_planted_code(source, tmp_path, witness):
    first = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "first"))
    (Path(first.path) / "existing.txt").write_text("état de la meilleure tentative\n")
    (Path(first.path) / "assets").mkdir()
    (Path(first.path) / "assets" / "logo.bin").write_bytes(bytes(range(200)))
    diff, _files = capture_diff(first)

    fresh = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "fresh"))
    _plant_fsmonitor_absolute(Path(fresh.path), witness)
    _plant_hooks_dir(Path(fresh.path), witness)
    _plant_clean_filter(Path(fresh.path), witness)

    assert apply_seed_diff(fresh, diff) is True
    assert not witness.exists()
    assert (Path(fresh.path) / "existing.txt").read_text() == "état de la meilleure tentative\n"
    assert (Path(fresh.path) / "assets" / "logo.bin").read_bytes() == bytes(range(200))
    # le diff autoritatif de la tentative reprise contient le seed
    diff2, files2 = capture_diff(fresh)
    assert set(files2) == {"existing.txt", "assets/logo.bin", ".gitattributes"}


def test_inapplicable_seed_restores_a_clean_tree(source, tmp_path, witness):
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    _plant_hooks_dir(Path(ws.path), witness)

    assert apply_seed_diff(ws, "pas un diff git valide\n") is False
    assert not witness.exists()
    _diff, files = capture_diff(ws)
    assert files == ()


# --- pipeline de bout en bout : agent ET tests hostiles ------------------------------


class _Branches:
    def __init__(self):
        self.created = []

    def ensure_branch(self, owner, repo, branch, from_branch=None):
        self.created.append(branch)
        return None


class _Files:
    def __init__(self):
        self.updated = []

    def update_file(self, owner, repo, path, message, content, branch=None):
        self.updated.append((path, content))
        return {}

    def delete_file(self, owner, repo, path, message, branch=None):
        return {}


class _PRs:
    def __init__(self):
        self.created = []

    def find_pr_by_head(self, owner, repo, head, base=None, state="open"):
        return None

    def create_pr(self, owner, repo, title, head, base, body):
        from types import SimpleNamespace

        self.created.append(head)
        return SimpleNamespace(number=7, html_url="https://gh/pull/7", head_branch=head)


class _HostileGateSandbox:
    """Le gate (tests) tourne en RW sur le workspace : il plante APRÈS la capture."""

    def __init__(self, witness, evil, results):
        self._witness = witness
        self._evil = evil
        self._results = list(results)

    def run_tests(self, workspace, command="pytest -q"):
        ws = Path(workspace)
        if (ws / ".git").is_dir() and not (ws / ".git").is_symlink():
            _plant_fsmonitor_absolute(ws, self._witness)
            _plant_hooks_dir(ws, self._witness)
            _plant_clean_filter(ws, self._witness)
        (ws / "node_modules").mkdir(exist_ok=True)
        (ws / "node_modules" / "artefact.js").write_text("// écrit par le conteneur du gate\n")
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]


async def test_pipeline_with_hostile_agent_and_hostile_tests_recaptures_safely(source, witness, evil_repo):
    red = SandboxResult(exit_code=2, stdout="E   ModuleNotFoundError: No module named 'httpx'\n", stderr="")
    green = SandboxResult(exit_code=0, stdout="2 passed", stderr="")
    clients = PrClients(branches=_Branches(), files=_Files(), prs=_PRs())
    agent = HostileAgent(
        HOSTILE_PLANTS["fsmonitor-relatif-sonde-audit"],
        witness,
        evil_repo,
        files={"requirements.txt": "fastapi\n", "app.py": "import httpx\n"},
    )

    outcome = await execute_issue(
        ISSUE,
        source,
        ctx=None,
        dry_run=False,
        agent=agent,
        owner="o",
        repo="r",
        sandbox=_HostileGateSandbox(witness, evil_repo, [red, green]),
        reviewer=FakeReviewer(),
        clients=clients,
    )

    assert not witness.exists()
    assert outcome.success is True, outcome.error
    assert outcome.quality_report.requirements_added == ("httpx",)
    assert set(outcome.execution.files_changed) == {"requirements.txt", "app.py"}
    assert "artefact.js" not in outcome.execution.diff
    assert clients.prs.created  # la PR part, avec le correctif


async def test_pipeline_retry_seed_with_hostile_workspace_keeps_the_seed(source, witness, evil_repo):
    scratch = prepare_workspace(source, ISSUE)
    (Path(scratch.path) / "existing.txt").write_text("état de la meilleure tentative\n")
    seed, _ = capture_diff(scratch)

    agent = HostileAgent(HOSTILE_PLANTS["core-hookspath"], witness, evil_repo, files={"new.txt": "x\n"})
    outcome = await execute_issue(
        ISSUE,
        source,
        ctx=None,
        dry_run=True,
        seed_diff=seed,
        agent=agent,
        owner="o",
        repo="r",
        sandbox=_HostileGateSandbox(witness, evil_repo, [SandboxResult(exit_code=0, stdout="ok", stderr="")]),
        reviewer=FakeReviewer(),
        clients=PrClients(branches=_Branches(), files=_Files(), prs=_PRs()),
    )

    assert not witness.exists()
    assert outcome.success is True, outcome.error
    assert set(outcome.execution.files_changed) == {"existing.txt", "new.txt"}


# --- sans métadonnées de contrôle : fail closed, jamais de repli silencieux ----------


def test_unmanaged_workspace_without_runner_is_refused(tmp_path):
    """Un dossier sans métadonnées de contrôle n'est pas une source fiable."""
    from collegue.executor.workspace import Workspace

    plain = tmp_path / "plain"
    plain.mkdir()
    _git(plain, "init", "-q")
    ws = Workspace(path=str(plain), branch="x", base_commit="0" * 40)
    with pytest.raises(WorkspaceError):
        run_issue(FakeCodeAgent(), ws, ISSUE)


# --- snapshot de livraison : ni exécution, ni chemin sortant ---------------------------


def test_snapshot_verification_and_recapture_survive_hostile_git_redirection(source, tmp_path, witness, evil_repo):
    from collegue.executor.pr import capture_delivery_snapshot, verify_delivery_snapshot

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SECRET.txt").write_text("secret hors workspace\n")
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    root = Path(ws.path)
    (root / "README.md").write_text("livrable\n")
    diff, files = capture_diff(ws)
    snapshot = capture_delivery_snapshot(ws, files, diff=diff)

    # le gate (sandbox RW) redirige ``.git`` vers un dépôt hostile et pose un lien sortant
    _replace_git_with_symlink(root, evil_repo)
    os.symlink(outside / "SECRET.txt", root / "leak")
    verify_delivery_snapshot(ws, snapshot)  # ne lit que les fichiers figés
    diff2, files2 = capture_diff(ws, paths=("README.md",))

    assert not witness.exists()
    assert "secret hors workspace" not in diff2
    assert files2 == ("README.md",)


def test_snapshot_of_a_symlink_never_reads_its_target(source, tmp_path):
    from collegue.executor.pr import DELIVERY_SKIP_SYMLINK, capture_delivery_snapshot

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SECRET.txt").write_text("secret hors workspace\n")
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    os.symlink(outside / "SECRET.txt", Path(ws.path) / "leak")
    diff, files = capture_diff(ws)

    snapshot = capture_delivery_snapshot(ws, files, diff=diff)

    assert [item.operation for item in snapshot.files] == [DELIVERY_SKIP_SYMLINK]
    assert all(item.content is None for item in snapshot.files)


# --- compounding (amélioration cumulative) avec agent ET mesures hostiles ---------------


def _metrics(composite):
    from collegue.improve import ProjectQualityMetrics

    return ProjectQualityMetrics(
        coverage_pct=80.0,
        security_findings=0,
        security_weighted=0.0,
        tests_passed=True,
        composite=composite,
        coverage_measured=True,
        review_score=0.7,
    )


class _RoundAgent:
    """Un agent hostile par round : fichier distinct + charge Git plantée."""

    def __init__(self, witness, evil):
        self.round = 0
        self._witness = witness
        self._evil = evil

    def implement_issue(self, workspace, issue):
        self.round += 1
        ws = Path(workspace)
        (ws / f"feature_{self.round}.py").write_text(f"VALUE = {self.round}\n")
        _plant_fsmonitor_absolute(ws, self._witness)
        _plant_hooks_dir(ws, self._witness)
        _plant_hooks_path(ws, self._witness)
        _plant_clean_filter(ws, self._witness)
        return AgentResult(success=True)


class _HostileMeasure:
    """``measure_fn`` (donc du code du projet en RW) qui replante après chaque mesure."""

    def __init__(self, scores, witness):
        self._scores = list(scores)
        self._witness = witness
        self.calls = 0
        self.baseline_has = []
        self.after_diffs = []

    async def __call__(self, workspace, ctx, *, sandbox=None, reviewer=None, diff="", weights=None, **_):
        ws = Path(workspace)
        if not diff:
            self.baseline_has.append(sorted(p.name for p in ws.glob("feature_*.py")))
        else:
            self.after_diffs.append(diff)
        score = self._scores[min(self.calls, len(self._scores) - 1)]
        self.calls += 1
        if (ws / ".git").is_dir() and not (ws / ".git").is_symlink():
            _plant_fsmonitor_absolute(ws, self._witness)
            _plant_hooks_dir(ws, self._witness)
        return _metrics(score)


class _Budget:
    def should_continue(self):
        from collegue.pilot import ACTION_CONTINUE, ContinueDecision

        return ContinueDecision(action=ACTION_CONTINUE, reason="ok")


async def test_improve_loop_compounding_with_hostile_agent_and_measures_never_executes_on_host(
    source, tmp_path, witness, evil_repo
):
    from collegue.improve import run_improvement
    from collegue.state import ProjectStateManager

    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    measure = _HostileMeasure([0.5, 0.7, 0.7, 0.9, 0.9, 0.9], witness)

    result = await run_improvement(
        manager.create_project(name="hostile"),
        source,
        ctx=None,
        agent=_RoundAgent(witness, evil_repo),
        owner="o",
        repo="r",
        manager=manager,
        budget=_Budget(),
        clients=PrClients(branches=_Branches(), files=_Files(), prs=_PRs()),
        dry_run=True,
        plateau_rounds=1,
        measure_fn=measure,
    )

    assert not witness.exists()
    assert len(result.promoted) == 2
    # compounding : la baseline du round 2 contient déjà le round 1, et la baseline du
    # round 3 les deux ; le diff « après » du round 2 ne contient QUE son propre fichier.
    assert measure.baseline_has[0] == []
    assert measure.baseline_has[1] == ["feature_1.py"]
    assert measure.baseline_has[2] == ["feature_1.py", "feature_2.py"]
    assert "feature_2.py" in measure.after_diffs[1] and "feature_1.py" not in measure.after_diffs[1]


def test_compounding_seed_and_commit_never_run_planted_hooks(source, tmp_path, witness):
    from collegue.improve.loop import _seed_promoted_diffs

    first = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "first"))
    (Path(first.path) / "feature.py").write_text("VALUE = 1\n")
    diff, _ = capture_diff(first)

    fresh = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "fresh"))
    for plant in (_plant_hooks_dir, _plant_hooks_path, _plant_fsmonitor_absolute, _plant_clean_filter):
        plant(Path(fresh.path), witness)

    assert _seed_promoted_diffs(fresh, [diff]) == 1
    assert not witness.exists()
    # la base fiable a avancé (commit de contrôle) : plus rien à capturer que le bruit planté
    from collegue.executor.workspace import trusted_base

    assert trusted_base(fresh) != fresh.base_commit
    _diff, files = capture_diff(fresh)
    assert "feature.py" not in files


# --- revert -----------------------------------------------------------------------------


@pytest.fixture
def two_commit_source(tmp_path):
    src = tmp_path / "rsource"
    src.mkdir()
    _git(src, "init", "-q")
    _git(src, "config", "user.email", "t@example.com")
    _git(src, "config", "user.name", "Test")
    (src / "file.txt").write_text("v1\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "v1")
    (src / "file.txt").write_text("v2\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "v2")
    return str(src), _git(src, "rev-parse", "HEAD").stdout.strip()


def test_revert_on_managed_workspace_uses_control_and_ignores_workspace_git(two_commit_source, tmp_path, witness):
    from collegue.executor.revert import revert_commit
    from collegue.executor.workspace import trusted_base

    src, sha2 = two_commit_source
    ws = prepare_workspace(src, ISSUE, dest_root=str(tmp_path / "out"))
    for plant in (_plant_fsmonitor_absolute, _plant_hooks_dir, _plant_hooks_path, _plant_clean_filter):
        plant(Path(ws.path), witness)

    res = revert_commit(ws.path, sha2)

    assert not witness.exists()
    assert res.reverted is True
    assert (Path(ws.path) / "file.txt").read_text() == "v1\n"
    assert res.revert_sha == trusted_base(ws)


@pytest.mark.parametrize(
    "plant",
    [_plant_fsmonitor_absolute, _plant_hooks_path, _plant_include, _plant_clean_filter, _plant_external_diff],
    ids=["fsmonitor", "hookspath", "include", "filtre", "diff-externe"],
)
def test_revert_refuses_a_plain_clone_whose_config_was_tampered(two_commit_source, tmp_path, witness, plant):
    from collegue.executor.revert import revert_commit

    src, sha2 = two_commit_source
    dest = tmp_path / "clone"
    _git(tmp_path, "clone", "--quiet", src, str(dest))
    plant(dest, witness)

    res = revert_commit(str(dest), sha2)

    assert not witness.exists()
    assert res.reverted is False
    assert "refusé" in res.message


def test_revert_refuses_a_plain_clone_with_git_redirection(two_commit_source, tmp_path, witness, evil_repo):
    from collegue.executor.revert import revert_commit

    src, sha2 = two_commit_source
    for name, redirect in (("gitfile", _replace_git_with_gitfile), ("symlink", _replace_git_with_symlink)):
        dest = tmp_path / f"clone-{name}"
        _git(tmp_path, "clone", "--quiet", src, str(dest))
        redirect(dest, evil_repo)
        res = revert_commit(str(dest), sha2)
        assert res.reverted is False and "refusé" in res.message, name
    assert not witness.exists()


def test_prepare_revert_still_produces_a_pushable_plain_clone(two_commit_source):
    from collegue.executor.revert import prepare_revert

    src, sha2 = two_commit_source
    res = prepare_revert(src, sha2)
    assert res.reverted
    assert (Path(res.workspace) / ".git").is_dir()
    assert _git(res.workspace, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == res.branch


# --- garde-fous du runner durci ---------------------------------------------------------


def test_hardened_runner_only_runs_git_argv_without_global_options(tmp_path):
    from collegue.executor.git_boundary import HardenedGitRunner

    runner = HardenedGitRunner()
    with pytest.raises(TypeError):
        runner.run_command("git status", str(tmp_path))
    for argv in (["git"], ["git", "-C", "/", "status"], ["git", "--git-dir=/x", "status"], []):
        res = runner.run_command(argv, str(tmp_path))
        assert res.exit_code == 126 and "refusé" in res.stderr
    missing = runner.run_command(["git", "status"], str(tmp_path / "absent"))
    assert missing.exit_code == 127


def test_hardened_runner_env_scrub_ignores_host_git_environment(source, tmp_path, witness, monkeypatch):
    """Même un ``GIT_CONFIG_COUNT``/``GIT_DIR``/``GIT_EXTERNAL_DIFF`` hérité de l'hôte est ignoré."""
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    hook = _script(tmp_path / "env-hook", witness)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hook))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(hook))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "nowhere-index"))
    (Path(ws.path) / "README.md").write_text("ok\n")

    diff, files = capture_diff(ws)

    assert not witness.exists()
    assert files == ("README.md",)


def test_hardened_runner_caller_config_cannot_reenable_hooks(two_commit_source, tmp_path, witness):
    from collegue.executor.git_boundary import HardenedGitRunner

    src, _sha2 = two_commit_source
    dest = tmp_path / "clone"
    _git(tmp_path, "clone", "--quiet", src, str(dest))
    for name in _HOOK_NAMES:
        _script(tmp_path / "hooks" / name, witness)
    (dest / "new.txt").write_text("x\n")
    runner = HardenedGitRunner()

    add = runner.run_command(["git", "add", "-A"], str(dest))
    commit = runner.run_command(
        [
            "git",
            "-c",
            f"core.hooksPath={tmp_path / 'hooks'}",
            "-c",
            "user.email=b@e.x",
            "-c",
            "user.name=b",
            "commit",
            "-q",
            "-m",
            "m",
        ],
        str(dest),
    )

    assert add.ok and commit.ok, (add.stderr, commit.stderr)
    assert not witness.exists()


def test_hardened_runner_accepts_a_benign_host_made_clone(two_commit_source, tmp_path):
    from collegue.executor.git_boundary import HardenedGitRunner, plain_git_dir_problem

    src, sha2 = two_commit_source
    dest = tmp_path / "clone"
    _git(tmp_path, "clone", "--quiet", src, str(dest))
    _git(dest, "config", "user.email", "b@e.x")
    _git(dest, "config", "user.name", "b")
    assert plain_git_dir_problem(str(dest)) is None
    res = HardenedGitRunner().run_command(["git", "rev-parse", "HEAD"], str(dest))
    assert res.ok and res.stdout.strip() == sha2


def test_plain_git_dir_problem_rejects_transports_and_worktree_redirects(two_commit_source, tmp_path):
    from collegue.executor.git_boundary import plain_git_dir_problem

    src, _sha2 = two_commit_source
    dest = tmp_path / "clone"
    _git(tmp_path, "clone", "--quiet", src, str(dest))
    _git(dest, "config", "remote.origin.url", "ext::sh -c 'echo pwned'")
    assert "transport" in (plain_git_dir_problem(str(dest)) or "")
    _git(dest, "config", "remote.origin.url", src)
    assert plain_git_dir_problem(str(dest)) is None
    _git(dest, "config", "core.worktree", str(tmp_path))
    assert "non autorisée" in (plain_git_dir_problem(str(dest)) or "")
    _git(dest, "config", "--unset", "core.worktree")
    (dest / ".git" / "commondir").write_text("../elsewhere\n")
    assert "commondir" in (plain_git_dir_problem(str(dest)) or "")


# --- invariants du workspace géré -------------------------------------------------------


def test_prepare_workspace_keeps_control_outside_the_mount_and_shares_no_inode(source, tmp_path):
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    workspace, control = Path(ws.path), Path(ws.path + ".control")

    assert control.is_dir() and not control.is_relative_to(workspace)
    assert (control / ".collegue-git-control").read_text().strip() == os.path.realpath(ws.path)
    assert (workspace / ".git").is_dir() and not (workspace / ".git" / ".collegue-git-control").exists()
    assert oct(control.stat().st_mode & 0o777) == "0o700"
    # la copie jetable de l'agent ne partage AUCUN inode avec le contrôle (pas de hardlink)
    control_inodes = {p.stat().st_ino for p in control.rglob("*") if p.is_file()}
    for path in (workspace / ".git").rglob("*"):
        if path.is_file():
            assert path.stat().st_ino not in control_inodes, path
            assert path.stat().st_nlink == 1, path


def test_control_tampering_is_detected_fail_closed(source, tmp_path):
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    marker = Path(ws.path + ".control") / ".collegue-git-control"
    marker.write_text("/un/autre/workspace\n")
    with pytest.raises(WorkspaceError):
        capture_diff(ws)
    marker.unlink()
    with pytest.raises(WorkspaceError):
        capture_diff(ws)


def test_injected_runner_is_refused_on_a_managed_workspace_before_the_agent_runs(source, tmp_path):
    from collegue.executor import LocalCommandRunner

    calls = []

    class _Spy:
        def implement_issue(self, workspace, issue):
            calls.append(workspace)
            return AgentResult(success=True)

    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    with pytest.raises(WorkspaceError):
        capture_diff(ws, runner=LocalCommandRunner())
    with pytest.raises(WorkspaceError):
        run_issue(_Spy(), ws, ISSUE, runner=LocalCommandRunner())
    assert calls == []  # fail-closed AVANT l'agent


def test_injected_runner_remains_available_for_unmanaged_trusted_fixtures(tmp_path):
    from collegue.executor import LocalCommandRunner
    from collegue.executor.workspace import Workspace

    plain = Path(_make_repo(tmp_path / "fixture"))
    (plain / "new.txt").write_text("x\n")
    ws = Workspace(path=str(plain), branch="b", base_commit="0" * 40)

    diff, files = capture_diff(ws, runner=LocalCommandRunner())

    assert files == ("new.txt",) and "+x" in diff


def test_seed_on_an_unmanaged_workspace_is_refused_not_silently_local(tmp_path):
    from collegue.executor.workspace import Workspace

    plain = Path(_make_repo(tmp_path / "fixture"))
    ws = Workspace(path=str(plain), branch="b", base_commit="0" * 40)
    with pytest.raises(WorkspaceError):
        apply_seed_diff(ws, "diff --git a/x b/x\n")


def test_trusted_base_is_the_control_head_and_advances_only_through_the_boundary(source, tmp_path):
    from collegue.executor.workspace import advance_base, trusted_base

    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    assert trusted_base(ws) == ws.base_commit
    (Path(ws.path) / "a.txt").write_text("a\n")
    _git(ws.path, "-c", "user.email=x@y.z", "-c", "user.name=x", "commit", "-q", "--allow-empty", "-m", "forgé")
    assert trusted_base(ws) == ws.base_commit  # un commit dans le .git du workspace n'avance rien
    assert advance_base(ws, "base fiable") is True
    assert trusted_base(ws) != ws.base_commit
    _diff, files = capture_diff(ws)
    assert files == ()


# --- exécution bornée --------------------------------------------------------------------


def test_timeout_kills_the_process_group_and_clears_a_stale_index_lock(source, tmp_path):
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    slow = tmp_path / "slow-git"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)
    repo = TrustedGit(ws.path + ".control", os.path.realpath(ws.path), git_bin=str(slow), timeout=0.5)
    lock = Path(ws.path + ".control") / "index.lock"
    lock.write_text("")

    result = repo.run("status")

    assert result.timed_out and result.exit_code == 124
    assert not lock.exists()  # sinon toute opération suivante échouerait


def test_capture_refuses_a_truncated_diff_instead_of_delivering_a_partial_one(source, tmp_path):
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    (Path(ws.path) / "big.txt").write_text("ligne de texte\n" * 5000)
    repo = TrustedGit(ws.path + ".control", os.path.realpath(ws.path), max_output_bytes=2048)
    with pytest.raises(WorkspaceError, match="supérieur"):
        repo.capture()


def test_capture_refuses_non_utf8_file_names(source, tmp_path):
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))
    with open(os.fsencode(ws.path) + b"/\xff-non-utf8.txt", "wb") as handle:
        handle.write(b"x\n")
    with pytest.raises(WorkspaceError, match="UTF-8"):
        capture_diff(ws)


# --- cycle de vie ------------------------------------------------------------------------


def test_cleanup_workspace_removes_the_control_dir_too(source, tmp_path):
    from collegue.executor.workspace import cleanup_workspace

    # défaut : tmpdir collegue-exec-* → le parent (donc workspace + contrôle) disparaît
    ws = prepare_workspace(source, ISSUE)
    control = ws.path + ".control"
    assert os.path.isdir(control)
    cleanup_workspace(ws)
    assert not os.path.exists(ws.path) and not os.path.exists(control)

    # dest_root fourni par l'appelant : le contrôle frère est supprimé s'il porte NOTRE marqueur…
    ws2 = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "custom"))
    foreign = tmp_path / "custom" / "autre.control"
    foreign.mkdir()
    cleanup_workspace(ws2.path)
    assert not os.path.exists(ws2.path) and not os.path.exists(ws2.path + ".control")
    assert foreign.exists()  # … jamais un dossier étranger


def test_failed_prepare_leaves_no_orphan(tmp_path, monkeypatch):
    import tempfile

    empty_repo = tmp_path / "empty"
    empty_repo.mkdir()
    _git(empty_repo, "init", "-q")  # aucun commit : HEAD illisible
    tmproot = tmp_path / "tmproot"
    tmproot.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmproot))

    with pytest.raises(WorkspaceError):
        prepare_workspace(str(empty_repo), ISSUE)
    assert list(tmproot.iterdir()) == []  # ni collegue-exec-*, ni workspace, ni contrôle

    with pytest.raises(WorkspaceError):
        prepare_workspace(str(empty_repo), ISSUE, dest_root=str(tmp_path / "out"))
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == []


def test_prepare_workspace_refuses_a_non_empty_destination(source, tmp_path):
    out = tmp_path / "out"
    (out / "workspace").mkdir(parents=True)
    (out / "workspace" / "precieux.txt").write_text("ne pas écraser\n")
    with pytest.raises(WorkspaceError):
        prepare_workspace(source, ISSUE, dest_root=str(out))
    assert (out / "workspace" / "precieux.txt").read_text() == "ne pas écraser\n"


# --- patch de seed dérivé du travail d'un agent : jamais d'écriture hors workspace ----------


def _crafted_symlink_escape_patch(outside: Path) -> str:
    """Crée un lien ``link -> outside`` puis écrit ``link/pwned`` : git doit refuser."""
    return (
        "diff --git a/link b/link\n"
        "new file mode 120000\n"
        "index 0000000000000000000000000000000000000000..e1f5cf5\n"
        "--- /dev/null\n"
        "+++ b/link\n"
        "@@ -0,0 +1 @@\n"
        f"+{outside}\n"
        "\\ No newline at end of file\n"
        "diff --git a/link/pwned b/link/pwned\n"
        "new file mode 100644\n"
        "index 0000000000000000000000000000000000000000..d95f3ad\n"
        "--- /dev/null\n"
        "+++ b/link/pwned\n"
        "@@ -0,0 +1 @@\n"
        "+content\n"
    )


def test_seed_patch_cannot_write_through_a_symlink_or_into_git_metadata(source, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    ws = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "out"))

    assert apply_seed_diff(ws, _crafted_symlink_escape_patch(outside)) is False
    assert list(outside.iterdir()) == []

    to_git_dir = (
        "diff --git a/.git/hooks/pre-commit b/.git/hooks/pre-commit\n"
        "new file mode 100755\n"
        "index 0000000..d95f3ad\n"
        "--- /dev/null\n"
        "+++ b/.git/hooks/pre-commit\n"
        "@@ -0,0 +1 @@\n"
        "+content\n"
    )
    assert apply_seed_diff(ws, to_git_dir) is False
    escape = to_git_dir.replace(".git/hooks/pre-commit", "../escaped.txt")
    assert apply_seed_diff(ws, escape) is False
    assert not (tmp_path / "out" / "escaped.txt").exists()
    assert capture_diff(ws)[1] == ()  # l'arbre est resté propre après chaque refus


# --- sandbox : un workspace géré réel se monte seul, jamais avec son contrôle --------------


def test_real_managed_workspace_mounts_alone_and_never_with_its_control(source, tmp_path):
    from collegue.sandbox import DockerSandbox

    ws = prepare_workspace(source, ISSUE)  # tmpdir collegue-exec-* : parent = workspace + contrôle
    try:
        sandbox = DockerSandbox(image="img")
        assert sandbox._validate_workspace(ws.path) == os.path.realpath(ws.path)
        argv = sandbox._build_run_argv("true", ws.path)
        assert [a for a in argv if ".control" in a] == []
        with pytest.raises(ValueError, match="contrôle"):
            sandbox._validate_workspace(os.path.dirname(ws.path))
        with pytest.raises(ValueError, match="contrôle"):
            sandbox._validate_workspace(ws.path + ".control")
    finally:
        from collegue.executor.workspace import cleanup_workspace

        cleanup_workspace(ws)
