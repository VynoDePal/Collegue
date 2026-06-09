"""Tests C8 (#342) : exécuteur sandbox Docker isolé.

- Unitaires (CI, sans Docker) : commande `docker run` durcie, parsing/troncature
  du résultat, timeout (+ kill du conteneur), refus root, validation du workspace
  — via mock de subprocess.
- Intégration (Docker, marqués `integration`, skippés en CI) : exécution réelle,
  isolation FS hôte (AC#1, via escape `..`), persistance du workspace (AC#2).
"""

import os
import tempfile

import pytest

import collegue.sandbox.executor as ex
from collegue.sandbox import DockerSandbox, SandboxResult, SandboxUnavailable


class _Proc:
    def __init__(self, returncode=0):
        self.returncode = returncode


def _mounts(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]


# --- construction de la commande durcie (pur, sans Docker) ----------------------


def test_build_argv_isolation_flags(tmp_path):
    sb = DockerSandbox(image="img", network="none")
    argv = sb._build_run_argv("echo hi", str(tmp_path))
    joined = " ".join(argv)
    assert "--network none" in joined
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--read-only" in argv
    assert "--tmpfs" in argv and "/tmp" in argv
    assert "--pids-limit" in argv
    assert "--memory" in argv and "--cpus" in argv
    assert "-w" in argv and "/workspace" in argv


def test_build_argv_mounts_only_workspace(tmp_path):
    # AC#1 (config) : SEUL le workspace est monté, aucun autre chemin hôte.
    sb = DockerSandbox(image="img")
    argv = sb._build_run_argv("echo hi", str(tmp_path))
    assert _mounts(argv) == [f"{tmp_path}:/workspace"]


def test_build_argv_env_injection_secret_by_reference(tmp_path):
    # Worker (ex. OpenHands) : la clé API est passée par RÉFÉRENCE (`-e NAME`, sans
    # valeur → docker l'hérite du process appelant) → le secret n'apparaît JAMAIS
    # dans l'argv (ni dans `ps`). Les valeurs non secrètes vont en clair (`-e K=V`).
    sb = DockerSandbox(image="img", env={"LLM_MODEL": "gemini/x"}, env_passthrough=("LLM_API_KEY",))
    argv = sb._build_run_argv("echo hi", str(tmp_path))
    joined = " ".join(argv)
    assert "-e LLM_API_KEY" in joined  # par référence
    assert "LLM_API_KEY=" not in joined  # jamais la valeur du secret
    assert "-e LLM_MODEL=gemini/x" in joined  # valeur non secrète en clair


def test_build_argv_no_env_by_default(tmp_path):
    # Défaut : aucun `-e` hormis HOME=/tmp → argv identique au comportement historique.
    sb = DockerSandbox(image="img")
    argv = sb._build_run_argv("echo hi", str(tmp_path))
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "-e"] == ["HOME=/tmp"]


def test_build_argv_read_only_toggle(tmp_path):
    assert "--read-only" in DockerSandbox(image="img")._build_run_argv("x", str(tmp_path))
    assert "--read-only" not in DockerSandbox(image="img", read_only=False)._build_run_argv("x", str(tmp_path))


def test_build_argv_runs_non_root(tmp_path):
    sb = DockerSandbox(image="img")
    argv = sb._build_run_argv("echo hi", str(tmp_path))
    if hasattr(os, "getuid"):
        assert "--user" in argv
        assert f"{os.getuid()}:{os.getgid()}" in argv


def test_build_argv_name_optional(tmp_path):
    sb = DockerSandbox(image="img")
    assert "--name" not in sb._build_run_argv("echo", str(tmp_path))
    argv = sb._build_run_argv("echo", str(tmp_path), name="c1")
    assert "--name" in argv and "c1" in argv


def test_build_argv_string_is_shell_wrapped(tmp_path):
    sb = DockerSandbox(image="img")
    argv = sb._build_run_argv("echo hi && ls", str(tmp_path))
    assert argv[-3:] == ["sh", "-c", "echo hi && ls"]


def test_build_argv_list_is_passed_directly(tmp_path):
    sb = DockerSandbox(image="img")
    argv = sb._build_run_argv(["pytest", "-q"], str(tmp_path))
    assert argv[-2:] == ["pytest", "-q"]


# --- validation du workspace (anti "monte tout l'hôte") -------------------------


def test_validate_workspace_rejects_fs_root():
    with pytest.raises(ValueError):
        DockerSandbox(image="img")._validate_workspace("/")


def test_validate_workspace_rejects_colon():
    with pytest.raises(ValueError):
        DockerSandbox(image="img")._validate_workspace("/tmp/a:b")


def test_validate_workspace_enforces_root(tmp_path):
    sb = DockerSandbox(image="img", workspace_root=str(tmp_path))
    sub = tmp_path / "proj"
    sub.mkdir()
    assert sb._validate_workspace(str(sub)) == os.path.realpath(str(sub))
    with pytest.raises(ValueError):
        sb._validate_workspace("/etc")


def test_run_command_rejects_dangerous_workspace():
    with pytest.raises(ValueError):
        DockerSandbox(image="img", allow_root=True).run_command("echo hi", "/")


# --- refus de tourner en root ---------------------------------------------------


def test_run_command_refuses_root_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(ex.os, "getuid", lambda: 0)
    with pytest.raises(SandboxUnavailable):
        DockerSandbox(image="img").run_command("echo hi", str(tmp_path))


# --- run_command (subprocess mocké : sortie vers fichiers) ----------------------


def test_run_command_parses_result(monkeypatch, tmp_path):
    captured = {}

    def _fake_run(argv, **kw):
        captured["argv"] = argv
        kw["stdout"].write(b"hello\n")
        return _Proc(returncode=0)

    monkeypatch.setattr(ex.subprocess, "run", _fake_run)
    res = DockerSandbox(image="img", allow_root=True).run_command("echo hello", str(tmp_path))
    assert res.ok
    assert res.stdout == "hello\n"
    assert captured["argv"][:3] == ["docker", "run", "--rm"]
    assert "--name" in captured["argv"]


def test_run_command_creates_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(ex.subprocess, "run", lambda argv, **kw: _Proc())
    ws = tmp_path / "new_ws"
    DockerSandbox(image="img", allow_root=True).run_command("echo hi", str(ws))
    assert ws.is_dir()


def test_run_command_output_truncated(monkeypatch, tmp_path):
    def _fake_run(argv, **kw):
        kw["stdout"].write(b"abcdefghij")  # 10 octets
        return _Proc(returncode=0)

    monkeypatch.setattr(ex.subprocess, "run", _fake_run)
    res = DockerSandbox(image="img", allow_root=True, max_output_bytes=5).run_command("x", str(tmp_path))
    assert res.stdout.startswith("abcde")
    assert "tronquée" in res.stdout


def test_run_command_timeout_kills_container(monkeypatch, tmp_path):
    killed = {}

    def _fake_run(argv, **kw):
        if argv[1:2] == ["kill"]:
            killed["name"] = argv[-1]
            return _Proc()
        raise ex.subprocess.TimeoutExpired(cmd=argv, timeout=1)

    monkeypatch.setattr(ex.subprocess, "run", _fake_run)
    res = DockerSandbox(image="img", allow_root=True, timeout=1).run_command("sleep 100", str(tmp_path))
    assert res.timed_out
    assert res.exit_code == ex.TIMEOUT_EXIT_CODE
    assert killed.get("name", "").startswith("collegue-sbx-")  # conteneur tué (pas d'orphelin)


def test_run_command_docker_missing_raises(monkeypatch, tmp_path):
    def _fake_run(argv, **kw):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(ex.subprocess, "run", _fake_run)
    with pytest.raises(SandboxUnavailable):
        DockerSandbox(image="img", allow_root=True).run_command("echo hi", str(tmp_path))


def test_run_tests_default_command(monkeypatch, tmp_path):
    captured = {}

    def _fake_run(argv, **kw):
        captured["argv"] = argv
        return _Proc()

    monkeypatch.setattr(ex.subprocess, "run", _fake_run)
    DockerSandbox(image="img", allow_root=True).run_tests(str(tmp_path))
    assert captured["argv"][-3:] == ["sh", "-c", "pytest -q"]


def test_is_available(monkeypatch):
    monkeypatch.setattr(ex.subprocess, "run", lambda *a, **k: _Proc(returncode=0))
    assert DockerSandbox().is_available() is True

    def _missing(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(ex.subprocess, "run", _missing)
    assert DockerSandbox().is_available() is False


def test_result_ok_property():
    assert SandboxResult(0, "", "").ok
    assert not SandboxResult(1, "", "").ok
    assert not SandboxResult(0, "", "", timed_out=True).ok


# --- intégration Docker réelle (skippée en CI) ----------------------------------

_SANDBOX_TEST_IMAGE = os.getenv("SANDBOX_TEST_IMAGE", "python:3.12-slim")


def _sandbox_or_skip():
    sb = DockerSandbox(image=_SANDBOX_TEST_IMAGE, allow_root=True)
    if not sb.is_available():
        pytest.skip("Docker indisponible")
    return sb


@pytest.mark.integration
def test_sandbox_runs_command_real(tmp_path):
    res = _sandbox_or_skip().run_command("echo sandbox-ok", str(tmp_path))
    assert res.ok
    assert "sandbox-ok" in res.stdout


@pytest.mark.integration
def test_sandbox_cannot_escape_to_host(tmp_path):
    # AC#1 : depuis le sandbox, impossible d'atteindre un fichier hôte hors workspace
    # (le parent de /workspace dans le conteneur est sa propre racine, pas l'hôte).
    sb = _sandbox_or_skip()
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "secret.txt").write_text("SECRET_SIBLING")
    res = sb.run_command("cat /workspace/../secret.txt", str(ws))
    assert "SECRET_SIBLING" not in res.stdout
    assert res.exit_code != 0


@pytest.mark.integration
def test_sandbox_root_fs_read_only(tmp_path):
    res = _sandbox_or_skip().run_command("echo x > /etc/passwd", str(tmp_path))
    assert res.exit_code != 0  # root FS en lecture seule


@pytest.mark.integration
def test_sandbox_workspace_persists(tmp_path):
    sb = _sandbox_or_skip()
    sb.run_command("echo persisted > marker.txt", str(tmp_path))
    res = sb.run_command("cat marker.txt", str(tmp_path))
    assert "persisted" in res.stdout
