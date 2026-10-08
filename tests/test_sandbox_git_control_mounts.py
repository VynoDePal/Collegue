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


_ORDINARY_ROOT: list = [None]


@pytest.fixture(autouse=True)
def _ordinary_root(tmp_path_factory):
    _ORDINARY_ROOT[0] = tmp_path_factory.mktemp("ordinary-workspaces")
    yield
    _ORDINARY_ROOT[0] = None


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
    ordinary = _ORDINARY_ROOT[0] / f"ordinary-{kind}"  # indépendant du chemin monté (parent fermé, fichier…)
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
def test_an_unreadable_subdirectory_is_a_refusal(tmp_path, monkeypatch, record_property, kind):
    """Un sous-répertoire non listable rend la vérification impossible ⇒ refus.

    Non-root : vrai ``chmod 0``. Root (``unshare -Urn``, CI) ou injection forcée : ``chmod`` est sans effet,
    donc EACCES est injecté sur ``os.scandir(locked)`` (jamais de skip) ; la preuve est enregistrée.
    """
    tree = tmp_path / "ordinary"
    locked = tree / "locked"
    locked.mkdir(parents=True)
    if _INJECT_ACCESS:
        record_property("proof", PROOF_INJECTED)
        real_scandir = os.scandir

        def scandir(path="."):
            if os.path.abspath(os.fspath(path)) == str(locked):
                raise PermissionError(13, "Permission denied", os.fspath(path))
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", scandir)
        _assert_refused(kind, tree, match="vérification")
        return
    record_property("proof", PROOF_REAL)
    locked.chmod(0)
    try:
        with pytest.raises(PermissionError):
            os.scandir(locked)  # la permission est RÉELLEMENT refusée par le noyau
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
    # seul l'arbre monté (et le workspace ordinaire des autres types) est listé : jamais la cible d'un lien
    allowed = (os.path.realpath(tree), os.path.realpath(_ORDINARY_ROOT[0]))
    assert all(
        any(os.path.realpath(p) == a or os.path.realpath(p).startswith(a + os.sep) for a in allowed) for p in visited
    ), visited
    assert not any(os.path.realpath(p) in (os.path.realpath(parent), "/") for p in visited), visited


def _git_source(tmp_path: Path) -> str:
    import subprocess

    source = tmp_path / "source"
    source.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@e.x"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
    (source / "a.txt").write_text("a\n")
    subprocess.run(["git", "add", "-A"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=source, check=True, capture_output=True)
    return str(source)


@pytest.mark.parametrize("kind", KINDS)
def test_a_managed_workspace_holding_another_managed_workspace_is_never_mounted(tmp_path, kind):
    """Une preuve d'appariement à SON contrôle frère ne prouve pas l'absence d'AUTRES contrôles dans
    l'arbre : ``prepare_workspace(dest_root=outer/nested)`` place ``inner.control`` sous le montage outer."""
    from collegue.executor import IssueSpec

    source = _git_source(tmp_path)
    outer = prepare_workspace(source, IssueSpec(number=1, title="t"), dest_root=str(tmp_path / "outer"))
    inner = prepare_workspace(source, IssueSpec(number=2, title="t"), dest_root=str(Path(outer.path) / "nested"))
    assert os.path.isdir(inner.path + ".control")  # le contrôle imbriqué est bien SOUS outer

    _assert_refused(kind, Path(outer.path))
    # …alors que chaque workspace, seul, reste montable (le contrôle est un frère, hors montage)
    sandbox = DockerSandbox()
    assert sandbox._validate_workspace(inner.path) == os.path.realpath(inner.path)


def test_a_restored_or_resumed_nested_control_is_still_refused(tmp_path):
    """Le refus ne dépend d'aucun état en mémoire : il vaut pour un contrôle posé par une exécution antérieure."""
    outer, _control = _make_control(_nested(tmp_path / "top", 1))
    inner_parent = outer / "nested" / "deeper"
    _make_control(inner_parent)  # contrôle « restauré » dans l'arbre du workspace outer
    with pytest.raises(ValueError, match="contrôle"):
        DockerSandbox()._validate_workspace(str(outer))


def test_the_agent_cannot_make_a_managed_workspace_mountable_by_forging_state(tmp_path):
    """Pas de dispense : l'arbre d'un workspace géré est parcouru comme tout autre, donc un piège de l'agent
    (marqueur forgé, sous-répertoire illisible) fait REFUSER le montage — jamais l'autoriser."""
    workspace, _control = _make_control(_nested(tmp_path / "top", 2))
    (workspace / "y").mkdir()
    (workspace / "y" / GIT_CONTROL_MARKER).write_text("x\n")
    with pytest.raises(ValueError, match="contrôle"):
        DockerSandbox()._validate_workspace(str(workspace))


def test_a_forged_marker_in_an_unmanaged_tree_is_refused_not_trusted(tmp_path):
    tree = tmp_path / "ordinary"
    (tree / "x").mkdir(parents=True)
    (tree / "x" / GIT_CONTROL_MARKER).write_text("/forged\n")
    _assert_refused("pip_cache", tree)


# --- bornes : entrées parcourues et file, pas seulement les répertoires dépilés --------------------


class _CountingScandir:
    """Espion de ``os.scandir`` qui compte les entrées RÉELLEMENT itérées."""

    def __init__(self, monkeypatch):
        self.entries = 0
        self.max_pending = 0
        real = os.scandir

        def scandir(path="."):
            it = real(path)
            outer = self

            class _Ctx:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    it.close()

                def __iter__(self_inner):
                    for entry in it:
                        outer.entries += 1
                        yield entry

            return _Ctx()

        monkeypatch.setattr(ex.os, "scandir", scandir)


@pytest.mark.parametrize("kind", KINDS)
def test_a_directory_with_too_many_files_is_refused_after_the_entry_bound(tmp_path, monkeypatch, kind):
    tree = tmp_path / "ordinary"
    tree.mkdir()
    for i in range(60):
        (tree / f"f{i}.txt").write_text("x")
    monkeypatch.setattr(ex, "GIT_CONTROL_SCAN_MAX_ENTRIES", 10)
    spy = _CountingScandir(monkeypatch)

    _assert_refused(kind, tree, match="vérification")
    assert spy.entries <= 11  # la borne coupe l'itération, le répertoire n'est pas parcouru entièrement


@pytest.mark.parametrize("kind", KINDS)
def test_a_directory_with_too_many_subdirectories_is_refused_while_the_queue_grows(tmp_path, monkeypatch, kind):
    tree = tmp_path / "ordinary"
    for i in range(60):
        (tree / f"d{i}").mkdir(parents=True)
    monkeypatch.setattr(ex, "GIT_CONTROL_SCAN_MAX_DIRS", 5)
    spy = _CountingScandir(monkeypatch)

    _assert_refused(kind, tree, match="vérification")
    assert spy.entries <= 7  # refus dès que la file dépasse la borne, avant d'empiler les 60 sous-répertoires


def test_scan_bounds_are_finite_module_constants():
    assert 0 < ex.GIT_CONTROL_SCAN_MAX_ENTRIES < 100_000_000
    assert 0 < ex.GIT_CONTROL_SCAN_MAX_DIRS < ex.GIT_CONTROL_SCAN_MAX_ENTRIES
    assert 0 < ex.GIT_CONTROL_SCAN_MAX_DEPTH <= 256


# --- quatrième constructeur de montage : sampling_ctx (sampler d'abonnement) -----------------------


def _sampling_ctx(auth_dir, script, calls):
    from collegue.core.llm.sampling_ctx import LocalSamplingContext

    def runner(argv, payload):
        calls.append(list(argv))
        return 0, "<<<SAMPLE_BEGIN>>>ok<<<SAMPLE_END>>>", ""

    return LocalSamplingContext(
        default_model="d",
        subscription_enabled=True,
        subscription_auth_dir=str(auth_dir),
        sampler_script=str(script),
        runner=runner,
    )


def _sample(ctx):
    import asyncio

    return asyncio.run(ctx._sample_subscription("gpt-5.4", [{"role": "user", "content": "hi"}]))


@pytest.fixture
def sampler_script(tmp_path):
    script = tmp_path / "sampler" / "oh_sampler.py"
    script.parent.mkdir()
    script.write_text("print('x')\n")
    return script


@pytest.mark.parametrize("depth", [0, 2, 4])
def test_sampling_auth_dir_exposing_the_control_is_refused_and_emits_no_docker_call(tmp_path, sampler_script, depth):
    top = tmp_path / "top"
    parent = _nested(top, depth) if depth else top
    parent.mkdir(parents=True, exist_ok=True)
    _workspace, control = _make_control(parent)
    calls = []

    for mounted in (control, control / "objects", top, parent):
        with pytest.raises(RuntimeError, match="contrôle"):
            _sample(_sampling_ctx(mounted, sampler_script, calls))
    assert calls == []


def test_sampling_auth_dir_alias_to_the_control_is_resolved(tmp_path, sampler_script):
    _workspace, control = _make_control(_nested(tmp_path / "top", 2))
    alias = tmp_path / "alias"
    os.symlink(control.parent.parent, alias)
    calls = []
    with pytest.raises(RuntimeError, match="contrôle"):
        _sample(_sampling_ctx(alias, sampler_script, calls))
    assert calls == []


def test_sampling_script_inside_or_aliasing_the_control_is_refused(tmp_path):
    _workspace, control = _make_control(_nested(tmp_path / "top", 2))
    inside = control / "oh_sampler.py"
    inside.write_text("print('x')\n")
    alias = tmp_path / "script-alias.py"
    os.symlink(inside, alias)
    auth = tmp_path / "auth"
    auth.mkdir()
    calls = []
    for script in (inside, alias, control, control.parent):
        with pytest.raises(RuntimeError, match="contrôle|fichier"):
            _sample(_sampling_ctx(auth, script, calls))
    assert calls == []


def test_sampling_script_that_is_a_directory_or_auth_with_colon_is_refused(tmp_path, sampler_script):
    auth = tmp_path / "auth"
    auth.mkdir()
    calls = []
    with pytest.raises(RuntimeError, match="fichier"):
        _sample(_sampling_ctx(auth, tmp_path / "sampler", calls))  # un répertoire n'est pas un script
    colon = tmp_path / "a:b"
    colon.mkdir()
    with pytest.raises(RuntimeError, match="':'"):
        _sample(_sampling_ctx(colon, sampler_script, calls))
    assert calls == []


def test_sampling_scanner_error_on_auth_dir_is_a_refusal(tmp_path, sampler_script, monkeypatch):
    auth = tmp_path / "auth"
    (auth / "sub").mkdir(parents=True)
    real = os.scandir

    def failing(path="."):
        if os.path.realpath(path).startswith(os.path.realpath(auth)):
            raise PermissionError(13, "interdit", str(path))
        return real(path)

    monkeypatch.setattr(ex.os, "scandir", failing)
    calls = []
    with pytest.raises(RuntimeError, match="vérification"):
        _sample(_sampling_ctx(auth, sampler_script, calls))
    assert calls == []


def test_sampling_ordinary_auth_dir_and_script_are_accepted_and_mounted(tmp_path, sampler_script):
    auth = tmp_path / "auth" / ".openhands"
    (auth / "sessions").mkdir(parents=True)
    (auth / "auth.json").write_text("{}")
    calls = []

    assert _sample(_sampling_ctx(auth, sampler_script, calls)) == "ok"

    (argv,) = calls
    mounts = [argv[i + 1] for i, v in enumerate(argv) if v == "-v"]
    assert mounts == [
        f"{os.path.realpath(auth)}:/home/sandbox/.openhands",
        f"{os.path.realpath(sampler_script)}:/oh_sampler.py:ro",
    ]


def test_sampling_absent_auth_dir_stays_compatible(tmp_path, sampler_script):
    calls = []
    assert _sample(_sampling_ctx(tmp_path / "not" / "yet", sampler_script, calls)) == "ok"
    assert len(calls) == 1


# --- erreurs de stat/résolution : jamais lues comme une absence ---------------------------------------
#
# ``os.path.lexists`` / ``isdir`` avalent ``PermissionError`` : un contrôle sous un parent non
# traversable paraissait absent, et le démon Docker (qui résout le bind avec SES privilèges) pouvait le
# monter. Seule une absence ÉTABLIE (ENOENT / ENOTDIR) autorise le chemin « à créer ».
#
# Preuve : en non-root, vraies permissions (``chmod 000``, uid courant) ; en root (CI) ``chmod`` est
# sans effet, donc la même situation est reproduite par injection d'EACCES sur ``lstat/stat/scandir``.
# Aucun cas n'est ignoré (pas de skip) ; ``record_property('proof', …)`` rend la nature de la preuve visible.

PROOF_REAL = "real-permissions-uid-nonroot"
PROOF_INJECTED = "injected-eacces-root"
# Root (CI) : ``chmod`` est sans effet → injection. ``COLLEGUE_FORCE_INJECTED_EACCES=1`` force aussi la
# branche injection en non-root, pour l'exercer sans être root (la preuve réelle reste le défaut).
_INJECT_ACCESS = os.getuid() == 0 or os.environ.get("COLLEGUE_FORCE_INJECTED_EACCES") == "1"


@pytest.fixture
def closed_tree(tmp_path, monkeypatch, record_property):
    """``root/closed`` est NON traversable ; ``closed/workspace.control`` (marqué) est dedans."""
    root = tmp_path / "root"
    closed = root / "closed"
    control = closed / "workspace.control"
    control.mkdir(parents=True)
    (control / GIT_CONTROL_MARKER).write_text("managed\n")
    (control / "objects").mkdir()
    prefix = str(closed) + os.sep
    if _INJECT_ACCESS:
        record_property("proof", PROOF_INJECTED)
        real_lstat, real_stat, real_scandir = os.lstat, os.stat, os.scandir

        def denied(path) -> bool:
            text = os.fspath(path) if not isinstance(path, int) else ""
            text = os.path.abspath(text)
            return text.startswith(prefix) or text == str(closed) + "/."

        def lstat(path, *a, **k):
            if denied(path):
                raise PermissionError(13, "Permission denied", os.fspath(path))
            return real_lstat(path, *a, **k)

        def stat(path, *a, **k):
            if denied(path):
                raise PermissionError(13, "Permission denied", os.fspath(path))
            return real_stat(path, *a, **k)

        def scandir(path="."):
            if os.path.abspath(os.fspath(path)) == str(closed) or denied(path):
                raise PermissionError(13, "Permission denied", os.fspath(path))
            return real_scandir(path)

        monkeypatch.setattr(os, "lstat", lstat)
        monkeypatch.setattr(os, "stat", stat)
        monkeypatch.setattr(os, "scandir", scandir)
        yield closed, control
        return
    record_property("proof", PROOF_REAL)
    closed.chmod(0)
    try:
        yield closed, control
    finally:
        closed.chmod(0o700)  # pour que tmp_path puisse être nettoyé


def test_the_proof_kind_is_identifiable(closed_tree, record_property):
    """Le journal dit explicitement si la preuve est réelle (uid non-root) ou injectée (root)."""
    expected = PROOF_INJECTED if _INJECT_ACCESS else PROOF_REAL
    record_property("proof_expected", expected)
    if not _INJECT_ACCESS:
        with pytest.raises(PermissionError):
            os.lstat(closed_tree[1])  # la permission est RÉELLEMENT refusée par le noyau


@pytest.mark.parametrize("kind", KINDS)
def test_a_control_behind_an_inaccessible_parent_is_never_mounted(closed_tree, kind):
    closed, control = closed_tree
    _assert_refused(kind, control, match="vérification")
    _assert_refused(kind, control / "objects", match="vérification")  # sous-chemin du contrôle
    _assert_refused(kind, closed / "workspace.control" / "absent", match="vérification")
    _assert_refused(kind, closed / "other-project", match="vérification")  # « à créer » sous un parent fermé
    _assert_refused(kind, closed, match="vérification")  # le parent fermé lui-même (scan impossible)


@pytest.mark.parametrize("kind", KINDS)
def test_an_alias_through_an_inaccessible_parent_is_refused(closed_tree, tmp_path, kind):
    _closed, control = closed_tree
    alias = tmp_path / "alias-to-control"
    os.symlink(control, alias)  # le lien est lisible, sa cible ne l'est pas
    _assert_refused(kind, alias, match="vérification")


def test_run_command_never_emits_docker_for_a_control_behind_an_inaccessible_parent(closed_tree, monkeypatch):
    _closed, control = closed_tree
    launched = []
    monkeypatch.setattr(ex.subprocess, "run", lambda *a, **k: launched.append(a) or pytest.fail("docker lancé"))
    monkeypatch.setattr(ex.os, "getuid", lambda: 1000)
    with pytest.raises(ValueError, match="vérification"):
        DockerSandbox().run_command("echo hi", str(control))
    assert launched == []


def test_sampling_refuses_a_control_behind_an_inaccessible_parent(closed_tree, sampler_script):
    closed, control = closed_tree
    calls = []
    for auth in (control, control / "objects", closed / "auth-to-create"):
        with pytest.raises(RuntimeError, match="vérification"):
            _sample(_sampling_ctx(auth, sampler_script, calls))
    assert calls == []


def test_sampling_refuses_a_script_behind_an_inaccessible_parent(closed_tree, tmp_path):
    closed, _control = closed_tree
    auth = tmp_path / "auth"
    auth.mkdir()
    calls = []
    with pytest.raises(RuntimeError, match="vérification"):
        _sample(_sampling_ctx(auth, closed / "oh_sampler.py", calls))
    assert calls == []


# --- injection indépendante du uid : TOUJOURS exécutée, y compris en non-root -------------------------


@pytest.fixture
def stat_errors(monkeypatch):
    """Injecte une erreur ``errno`` sur ``lstat``/``stat`` pour les chemins sous ``<marker>``."""

    def install(trigger: str, err: int):
        import errno as errno_mod

        real_lstat, real_stat = os.lstat, os.stat

        def fail(real):
            def wrapper(path, *a, **k):
                if trigger in os.fspath(path):
                    raise OSError(err, errno_mod.errorcode.get(err, "ERR"), os.fspath(path))
                return real(path, *a, **k)

            return wrapper

        monkeypatch.setattr(os, "lstat", fail(real_lstat))
        monkeypatch.setattr(os, "stat", fail(real_stat))

    return install


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("err_name", ["EACCES", "EPERM", "EIO", "ELOOP", "ETIMEDOUT"])
def test_any_stat_error_other_than_absence_refuses_the_mount(tmp_path, stat_errors, kind, err_name):
    import errno

    target = tmp_path / "sentinel-dir" / "project"
    target.mkdir(parents=True)
    stat_errors("sentinel-dir", getattr(errno, err_name))
    _assert_refused(kind, target, match="vérification")


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("err_name", ["ENOENT", "ENOTDIR"])
def test_an_established_absence_keeps_the_path_to_create_compatible(tmp_path, kind, err_name):
    import errno

    if err_name == "ENOENT":
        missing = tmp_path / "a" / "b" / "to-create"
    else:  # un composant est un fichier : absence établie par ENOTDIR
        (tmp_path / "plainfile").write_text("x")
        missing = tmp_path / "plainfile" / "to-create"
    assert not os.path.exists(missing)
    assert getattr(errno, err_name)
    argv = _argv(kind, missing)
    assert any("to-create" in a for a in argv)


@pytest.mark.parametrize("kind", KINDS)
def test_a_dangling_symlink_cannot_establish_absence(tmp_path, kind):
    target = tmp_path / "later-created-control"
    link = tmp_path / "dangling"
    os.symlink(target, link)
    _assert_refused(kind, link, match="vérification")


@pytest.mark.parametrize("kind", KINDS)
def test_a_symlink_loop_is_a_refusal(tmp_path, kind):
    os.symlink("loop-b", tmp_path / "loop-a")
    os.symlink("loop-a", tmp_path / "loop-b")
    _assert_refused(kind, tmp_path / "loop-a", match="vérification")


@pytest.fixture
def execonly_tree(tmp_path, monkeypatch, record_property):
    """``parent`` est TRAVERSABLE mais NON LISTABLE ; ``parent/workspace.control`` (marqué) est dedans.

    Contrat distinct de ``closed_tree`` (parent totalement inaccessible) : ici ``lstat`` sur les enfants
    FONCTIONNE, seul le listage (``scandir``) du parent est impossible.

    - non-root : vrai ``chmod 0o111`` (le noyau refuse le listage, autorise la traversée) ;
    - root (CI, ``unshare -Urn``) ou ``COLLEGUE_FORCE_INJECTED_EACCES=1`` : ``chmod`` est sans effet ⇒
      injection d'EACCES sur ``os.scandir(parent)`` UNIQUEMENT ; ``lstat``/``stat`` restent réels.
    La nature de la preuve est enregistrée (``record_property('proof', …)``).
    """
    parent = tmp_path / "execonly"
    control = parent / "workspace.control"
    control.mkdir(parents=True)
    (control / GIT_CONTROL_MARKER).write_text("managed\n")
    if _INJECT_ACCESS:
        record_property("proof", PROOF_INJECTED)
        real_scandir = os.scandir

        def scandir(path="."):
            if os.path.abspath(os.fspath(path)) == str(parent):
                raise PermissionError(13, "Permission denied", os.fspath(path))
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", scandir)
        yield parent, control
        return
    record_property("proof", PROOF_REAL)
    parent.chmod(0o111)
    try:
        yield parent, control
    finally:
        parent.chmod(0o700)  # pour que tmp_path puisse être nettoyé


@pytest.mark.parametrize("kind", KINDS)
def test_a_traversable_but_unlistable_parent_still_finds_a_control_by_direct_stat(execonly_tree, kind):
    """Un parent ``--x`` (traversable, non listable) laisse ``lstat`` fonctionner : le contrôle est vu
    DIRECTEMENT (marqueur), pas par un échec de listage ; le montage du parent, lui, est refusé."""
    parent, control = execonly_tree
    # Le scénario est bien celui du contrat : listage impossible, stat des enfants possible.
    with pytest.raises(PermissionError):
        os.scandir(parent)
    assert os.lstat(control / GIT_CONTROL_MARKER).st_size > 0

    reason = ex.git_control_exposure(str(control))
    assert reason is not None and "répertoire de contrôle Git" in reason
    assert "vérification impossible" not in reason  # détecté par lstat, pas par une erreur
    assert ex.git_control_exposure(str(control / "objects-to-create")) is not None  # sous-chemin : ancêtre = contrôle

    _assert_refused(kind, control)
    _assert_refused(kind, parent, match="vérification")  # monter le parent : listage impossible ⇒ refus
