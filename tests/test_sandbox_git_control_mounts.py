"""Suivi W1 : AUCUN bind mount du sandbox n'expose le répertoire de contrôle Git.

Les trois sources de ``-v`` (workspace, cache pip, creds d'abonnement) sont des chemins
d'entrée : le workspace vient du moteur, les deux autres des réglages de l'opérateur. Aucun ne
doit pouvoir monter le contrôle autoritatif (``<workspace>.control``) — directement, depuis un
ancêtre quelconque (profondeur 2, 4…), ou via un alias —, et une vérification impossible
(erreur, arbre trop grand ou trop profond) vaut un refus, jamais une autorisation.
Sans Docker ni réseau : l'argv n'est jamais exécuté, et ``subprocess.run`` est piégé.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import collegue.sandbox.executor as ex
from collegue.executor.workspace import prepare_workspace
from collegue.sandbox import DockerSandbox

GIT_CONTROL_MARKER = ex.GIT_CONTROL_MARKER
HOME = {"HOME": "/home/worker"}  # hors /tmp : requis par subscription_auth_dir


def _make_control(parent: Path, name: str = "workspace") -> tuple[Path, Path]:
    """Layout d'un workspace géré sous ``parent`` : (workspace, contrôle frère marqué)."""
    workspace = parent / name
    workspace.mkdir(parents=True)
    control = parent / f"{name}.control"
    control.mkdir()
    (control / GIT_CONTROL_MARKER).write_text(f"{os.path.realpath(workspace)}\n")
    (control / "config").write_text("[core]\n\tbare = false\n")
    (control / "objects").mkdir()
    return workspace, control


def _nested(root: Path, depth: int) -> Path:
    """``root/d1/d2/…/d<depth>`` : un ancêtre ``depth`` niveaux au-dessus du parent du workspace."""
    path = root
    for level in range(depth):
        path = path / f"d{level}"
    path.mkdir(parents=True)
    return path


def _sandbox(kind: str, mounted: Path):
    """Sandbox dont le montage ``kind`` pointe ``mounted`` ; les deux autres restent ordinaires."""
    if kind == "workspace":
        return DockerSandbox(env=HOME), mounted
    ordinary = mounted.parent / f"ordinary-{kind}"
    ordinary.mkdir(parents=True, exist_ok=True)
    if kind == "pip_cache":
        return DockerSandbox(pip_cache_dir=str(mounted), env=HOME), ordinary
    if kind == "subscription_auth":
        return DockerSandbox(subscription_auth_dir=str(mounted), env=HOME), ordinary
    raise AssertionError(kind)


def _argv(kind: str, mounted: Path):
    sandbox, workspace = _sandbox(kind, mounted)
    ws = sandbox._validate_workspace(str(workspace))
    return sandbox._build_run_argv(["true"], ws)


KINDS = ["workspace", "pip_cache", "subscription_auth"]


def _assert_refused(kind: str, mounted: Path, match: str = "contrôle"):
    with pytest.raises(ValueError, match=match):
        _argv(kind, mounted)


# --- ancêtres lointains et contrôle direct -------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("depth", [1, 2, 4])
def test_a_distant_ancestor_of_the_control_is_never_mounted(tmp_path, kind, depth):
    top = tmp_path / "top"
    parent = _nested(top, depth)
    _make_control(parent)

    _assert_refused(kind, top)  # l'ancêtre le plus éloigné
    _assert_refused(kind, parent)  # le parent direct
    if depth >= 2:
        _assert_refused(kind, top / "d0")  # un ancêtre intermédiaire


@pytest.mark.parametrize("kind", KINDS)
def test_the_control_dir_itself_and_its_insides_are_never_mounted(tmp_path, kind):
    _workspace, control = _make_control(_nested(tmp_path / "top", 2))
    _assert_refused(kind, control)
    _assert_refused(kind, control / "objects")  # un sous-répertoire du contrôle en expose aussi une partie


@pytest.mark.parametrize("kind", KINDS)
def test_aliases_to_the_control_or_an_ancestor_are_resolved_before_checking(tmp_path, kind):
    top = tmp_path / "top"
    parent = _nested(top, 3)
    _workspace, control = _make_control(parent)
    aliases = tmp_path / "aliases"
    aliases.mkdir()
    os.symlink(top, aliases / "to-ancestor")
    os.symlink(control, aliases / "to-control")
    os.symlink(aliases / "to-ancestor", aliases / "chained")  # alias d'alias

    _assert_refused(kind, aliases / "to-ancestor")
    _assert_refused(kind, aliases / "to-control")
    _assert_refused(kind, aliases / "chained")
    _assert_refused(kind, top / "d0" / ".." / "d0")  # chemin non normalisé
    relative = os.path.relpath(top, os.getcwd())
    _assert_refused(kind, Path(relative))  # chemin relatif au cwd


# --- Docker n'est jamais lancé quand un montage est refusé ---------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_run_command_never_emits_a_docker_command_when_a_mount_is_refused(tmp_path, monkeypatch, kind):
    top = tmp_path / "top"
    _make_control(_nested(top, 2))
    sandbox, workspace = _sandbox(kind, top)
    launched = []
    monkeypatch.setattr(ex.subprocess, "run", lambda *a, **k: launched.append(a) or pytest.fail("docker lancé"))
    monkeypatch.setattr(ex.subprocess, "Popen", lambda *a, **k: launched.append(a) or pytest.fail("docker lancé"))
    monkeypatch.setattr(ex.os, "getuid", lambda: 1000)

    with pytest.raises(ValueError, match="contrôle"):
        sandbox.run_command("echo hi", str(workspace))
    assert launched == []


# --- cas ordinaires : toujours acceptés ----------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_ordinary_directories_are_accepted_and_mounted(tmp_path, kind):
    tree = tmp_path / "ordinary"
    (tree / "a" / "b" / "c").mkdir(parents=True)
    (tree / "a" / "b" / "c" / "file.txt").write_text("x\n")
    (tree / "node_modules" / "pkg").mkdir(parents=True)

    argv = _argv(kind, tree)

    assert f"{os.path.realpath(tree)}:" in " ".join(argv)


@pytest.mark.parametrize("kind", ["pip_cache", "subscription_auth"])
def test_an_absent_cache_or_auth_dir_stays_compatible(tmp_path, kind):
    absent = tmp_path / "does" / "not" / "exist-yet"
    argv = _argv(kind, absent)
    assert any(str(absent) in a for a in argv)


def test_an_absent_workspace_stays_compatible(tmp_path):
    sandbox = DockerSandbox()
    ws = sandbox._validate_workspace(str(tmp_path / "fresh" / "workspace"))
    assert ws.endswith("fresh/workspace")


@pytest.mark.parametrize("kind", KINDS)
def test_a_sibling_of_a_control_is_not_confused_with_it(tmp_path, kind):
    parent = _nested(tmp_path / "top", 2)
    _make_control(parent)
    other = parent / "other-project"
    other.mkdir()
    (other / "file.txt").write_text("x\n")
    assert f"{os.path.realpath(other)}:" in " ".join(_argv(kind, other))


def test_the_managed_workspace_itself_is_mounted_but_never_with_its_control(tmp_path):
    workspace, control = _make_control(_nested(tmp_path / "top", 3))
    sandbox = DockerSandbox()
    argv = sandbox._build_run_argv(["true"], sandbox._validate_workspace(str(workspace)))
    assert [a for a in argv if a.startswith(str(workspace))] == [f"{os.path.realpath(workspace)}:/workspace"]
    assert str(control) not in " ".join(argv)


def test_a_real_prepared_workspace_mounts_alone_under_every_ancestor_refusal(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@e.x"], ["config", "user.name", "t"]):
        import subprocess

        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
    (source / "a.txt").write_text("a\n")
    subprocess.run(["git", "add", "-A"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=source, check=True, capture_output=True)
    from collegue.executor import IssueSpec

    top = tmp_path / "top"
    dest = _nested(top, 3)
    ws = prepare_workspace(str(source), IssueSpec(number=1, title="t"), dest_root=str(dest))

    sandbox = DockerSandbox()
    assert sandbox._validate_workspace(ws.path) == os.path.realpath(ws.path)
    for ancestor in (dest, top, top / "d0"):
        with pytest.raises(ValueError, match="contrôle"):
            sandbox._validate_workspace(str(ancestor))
    with pytest.raises(ValueError, match="contrôle"):
        DockerSandbox(pip_cache_dir=ws.path + ".control")._build_run_argv(["true"], ws.path)


# --- vérification bornée, fail-closed, sans suivre les liens -------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_a_scanner_error_is_a_refusal_not_an_authorization(tmp_path, monkeypatch, kind):
    tree = tmp_path / "ordinary"
    (tree / "sub").mkdir(parents=True)
    real_scandir = os.scandir

    def failing(path="."):
        if os.path.realpath(path).startswith(os.path.realpath(tree)):
            raise PermissionError(13, "scandir interdit", str(path))
        return real_scandir(path)

    monkeypatch.setattr(ex.os, "scandir", failing)
    _assert_refused(kind, tree, match="vérification")


@pytest.mark.parametrize("kind", KINDS)
def test_an_unreadable_subdirectory_is_a_refusal(tmp_path, kind):
    if os.getuid() == 0:
        pytest.skip("root lit tout")
    tree = tmp_path / "ordinary"
    locked = tree / "locked"
    locked.mkdir(parents=True)
    locked.chmod(0)
    try:
        _assert_refused(kind, tree, match="vérification")
    finally:
        locked.chmod(0o755)


@pytest.mark.parametrize("kind", KINDS)
def test_a_too_large_tree_is_refused_instead_of_scanned_without_limit(tmp_path, monkeypatch, kind):
    tree = tmp_path / "ordinary"
    for i in range(8):
        (tree / f"d{i}").mkdir(parents=True)
    monkeypatch.setattr(ex, "GIT_CONTROL_SCAN_MAX_DIRS", 5)
    _assert_refused(kind, tree, match="vérification")


@pytest.mark.parametrize("kind", KINDS)
def test_a_too_deep_tree_is_refused(tmp_path, monkeypatch, kind):
    tree = _nested(tmp_path / "ordinary", 10)
    monkeypatch.setattr(ex, "GIT_CONTROL_SCAN_MAX_DEPTH", 4)
    _assert_refused(kind, tree.parents[9], match="vérification")


@pytest.mark.parametrize("kind", KINDS)
def test_symlink_loops_and_links_to_the_control_are_not_followed_and_do_not_hang(tmp_path, monkeypatch, kind):
    parent = _nested(tmp_path / "top", 2)
    _workspace, control = _make_control(parent)
    tree = tmp_path / "ordinary"
    tree.mkdir()
    os.symlink(tree, tree / "loop")  # boucle sur soi-même
    os.symlink("a", tree / "a")  # boucle de lien
    os.symlink(parent, tree / "to-parent")  # lien vers un ancêtre du contrôle
    os.symlink(control, tree / "to-control")
    os.symlink("/", tree / "to-root")
    visited = []
    real_scandir = os.scandir

    def spy(path="."):
        visited.append(os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(ex.os, "scandir", spy)

    argv = _argv(kind, tree)

    assert f"{os.path.realpath(tree)}:" in " ".join(argv)
    # seul l'arbre monté est listé : jamais la cible d'un lien
    assert all(os.path.realpath(p).startswith(os.path.realpath(tree)) for p in visited), visited


def test_a_managed_workspace_skips_the_full_scan_even_when_the_agent_made_it_unreadable(tmp_path, monkeypatch):
    """Le workspace géré est apparié à son contrôle (marqueur hors de portée de l'agent) : l'arbre
    écrit par l'agent n'a pas à être parcouru, et l'agent ne peut pas empêcher le montage en le piégeant."""
    workspace, _control = _make_control(_nested(tmp_path / "top", 2))
    (workspace / "deep" / "er").mkdir(parents=True)
    visited = []
    real_scandir = os.scandir
    monkeypatch.setattr(ex.os, "scandir", lambda p=".": visited.append(os.fspath(p)) or real_scandir(p))

    DockerSandbox()._validate_workspace(str(workspace))

    assert [p for p in visited if os.path.realpath(p).startswith(os.path.realpath(workspace))] == []


def test_a_forged_marker_in_an_unmanaged_tree_is_refused_not_trusted(tmp_path):
    """Faux positif assumé (fail-closed) : un marqueur planté par l'agent dans un arbre non géré refuse
    le montage ; il ne peut en aucun cas AUTORISER quoi que ce soit."""
    tree = tmp_path / "ordinary"
    (tree / "x").mkdir(parents=True)
    (tree / "x" / GIT_CONTROL_MARKER).write_text("/forged\n")
    _assert_refused("pip_cache", tree)
    # et un workspace « géré » dont le marqueur désigne un autre chemin n'est pas dispensé du scan
    workspace, control = _make_control(_nested(tmp_path / "top", 2))
    (control / GIT_CONTROL_MARKER).write_text("/un/autre/workspace\n")
    (workspace / "y").mkdir()
    (workspace / "y" / GIT_CONTROL_MARKER).write_text("x\n")
    with pytest.raises(ValueError, match="contrôle"):
        DockerSandbox()._validate_workspace(str(workspace))
