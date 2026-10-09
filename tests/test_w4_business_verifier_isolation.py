"""Isolement du vérificateur métier : le livrable généré (code NON FIABLE) ne s'exécute jamais sur l'hôte par défaut.

Aucun Docker, aucun réseau, aucun vrai secret : la frontière ``subprocess.run`` est remplacée par un double qui refuse Docker
(code 125) ; les témoins exécutables sont des modules inoffensifs écrits par le test dans un dossier du test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from collegue.pilot import w4_business as business
from collegue.sandbox.executor import GIT_CONTROL_MARKER, SandboxRefused

FAKE_SECRETS = {
    "OPENAI_API_KEY": "sk-fake-for-test",
    "GITHUB_TOKEN": "ghp_fake_for_test",
    "LLM_API_KEY": "fake-llm",
    "W4_FAKE_SENTINEL": "inherited-by-accident",
}


class Boundary:
    """Double de ``subprocess.run`` : journalise, refuse Docker (125) ou simule une échéance."""

    def __init__(self, *, docker_returncode=125, timeout_on_run=False, kill_error=None):
        self.calls: list = []
        self.docker_returncode = docker_returncode
        self.timeout_on_run = timeout_on_run
        self.kill_error = kill_error

    def __call__(self, argv, *args, **kwargs):
        self.calls.append((list(argv), kwargs))
        if Path(str(argv[0])).name == "docker":
            if argv[1] == "kill":
                if self.kill_error:
                    raise self.kill_error
                return subprocess.CompletedProcess(argv, 0, "", "")
            if self.timeout_on_run:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 1))
            return subprocess.CompletedProcess(argv, self.docker_returncode, "", "docker withheld")
        raise AssertionError(f"commande hôte inattendue: {argv[:2]}")


@pytest.fixture
def fake_environment(monkeypatch):
    for name, value in FAKE_SECRETS.items():
        monkeypatch.setenv(name, value)


# ── défaut : jamais d'exécution sur l'hôte ──────────────────────────────────────────────────────────────────────────────


def test_default_verification_never_executes_the_generated_code_on_the_host(monkeypatch, tmp_path, fake_environment):
    boundary = Boundary()
    monkeypatch.setattr(subprocess, "run", boundary)
    root = tmp_path / "generated"
    witness = tmp_path / "host-witness.json"
    root.mkdir()
    (root / "alembic.py").write_text(f"from pathlib import Path\nPath({str(witness)!r}).write_text('x')\n")

    observation = business.verify_business_checkout(str(root), timeout=10)

    assert observation.status == "incomplete" and "Docker" in observation.detail
    assert not witness.exists(), "le code généré n'a pas tourné sur l'hôte"
    assert boundary.calls and all(Path(call[0][0]).name == "docker" for call in boundary.calls)


def test_default_docker_attempt_uses_a_credential_free_client_environment_and_hardened_mounts(
    monkeypatch, tmp_path, fake_environment
):
    boundary = Boundary()
    monkeypatch.setattr(subprocess, "run", boundary)
    root = tmp_path / "generated"
    root.mkdir()

    business.verify_business_checkout(str(root), timeout=10, image="local:never-pulled")

    argv, kwargs = boundary.calls[0]
    joined = " ".join(argv)
    assert "--network none" in joined and "--read-only" in joined and "--cap-drop ALL" in joined
    assert "--pull never" in joined and f"{root.resolve()}:/workspace:ro" in joined and "local:never-pulled" in argv
    assert "--user" in argv
    assert not set(kwargs["env"]) & set(FAKE_SECRETS), "aucune variable d'identification hérité par le client docker"
    for secret in FAKE_SECRETS.values():
        assert secret not in joined and secret not in json.dumps(kwargs["env"])
    assert not [a for a in argv if a.startswith("-e") and "=" not in a and a != "-e"], "aucune variable passée par nom"


def test_the_explicit_trusted_local_runner_does_not_forward_credentials(tmp_path, fake_environment):
    script = "import os,sys; print(sorted(k for k in os.environ if k in %r))" % (sorted(FAKE_SECRETS),)
    env = {**FAKE_SECRETS, "PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"}

    done = business.trusted_local_runner([sys.executable, "-c", script], str(tmp_path), env, 20)

    assert done.stdout.strip() == "[]", done.stdout
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        business.credential_free_env(("PATH", "OPENAI_API_KEY"), source={"PATH": "/bin", "OPENAI_API_KEY": "x"})


def test_a_verification_with_an_explicit_runner_receives_no_credential_either(tmp_path, fake_environment):
    seen = {}

    def runner(argv, cwd, env, timeout):
        seen["env"] = dict(env)
        return subprocess.CompletedProcess(argv, 0, "", "")

    business.verify_business_checkout(str(tmp_path), runner=runner)

    assert not set(seen["env"]) & set(FAKE_SECRETS) and "PYTHONDONTWRITEBYTECODE" in seen["env"]


# ── montages : garde commune W1 ─────────────────────────────────────────────────────────────────────────────────────────


def mount_error(tmp_path, checkout_path, scratch=None):
    scratch = scratch or tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return business.docker_verifier_command(
        image="img:never", name="n", checkout=str(checkout_path), scratch=str(scratch)
    )


def test_a_clean_checkout_and_scratch_build_a_command(tmp_path):
    clean = tmp_path / "clean"
    clean.mkdir()

    argv = mount_error(tmp_path, clean)

    assert argv[:2] == ["docker", "run"] and f"{clean.resolve()}:/workspace:ro" in argv


@pytest.mark.parametrize("where", ["direct", "nested", "ancestor", "scratch-direct"])
def test_git_control_directories_are_refused_whether_direct_nested_ancestor_or_in_the_scratch(tmp_path, where):
    clean = tmp_path / "clean"
    clean.mkdir()
    if where == "direct":
        target = tmp_path / "authority"
        target.mkdir()
        (target / GIT_CONTROL_MARKER).write_text("fixture\n")
        args = (target, None)
    elif where == "nested":
        target = tmp_path / "parent"
        (target / "embedded").mkdir(parents=True)
        (target / "embedded" / GIT_CONTROL_MARKER).write_text("fixture\n")
        args = (target, None)
    elif where == "ancestor":
        (tmp_path / "outer").mkdir()
        (tmp_path / "outer" / GIT_CONTROL_MARKER).write_text("fixture\n")
        inner = tmp_path / "outer" / "inner"
        inner.mkdir()
        args = (inner, None)
    else:
        scratch = tmp_path / "bad-scratch"
        scratch.mkdir()
        (scratch / GIT_CONTROL_MARKER).write_text("fixture\n")
        args = (clean, scratch)

    with pytest.raises(SandboxRefused, match="métadonnées Git de contrôle"):
        mount_error(tmp_path, *args)


def test_a_colon_a_root_a_dangling_link_and_a_link_to_a_control_are_refused(tmp_path):
    colon = tmp_path / "with:colon"
    colon.mkdir()
    with pytest.raises(SandboxRefused, match="':'"):
        mount_error(tmp_path, colon)
    with pytest.raises(SandboxRefused, match="racine"):
        mount_error(tmp_path, "/")
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "does-not-exist-yet")
    with pytest.raises(SandboxRefused, match="vérification impossible"):
        mount_error(tmp_path, dangling)
    control = tmp_path / "authority"
    control.mkdir()
    (control / GIT_CONTROL_MARKER).write_text("fixture\n")
    link = tmp_path / "link-to-control"
    link.symlink_to(control)
    with pytest.raises(SandboxRefused, match="métadonnées Git de contrôle"):
        mount_error(tmp_path, link)


@pytest.mark.skipif(os.geteuid() == 0, reason="root traverse un répertoire sans droit")
def test_a_stat_error_is_a_refusal_not_an_absence(tmp_path):
    locked = tmp_path / "locked"
    (locked / "inside").mkdir(parents=True)
    locked.chmod(0o000)
    try:
        with pytest.raises(SandboxRefused, match="vérification impossible"):
            mount_error(tmp_path, locked / "inside")
    finally:
        locked.chmod(0o700)


def test_a_refused_mount_makes_the_default_verification_incomplete_without_any_docker_call(monkeypatch, tmp_path):
    boundary = Boundary()
    monkeypatch.setattr(subprocess, "run", boundary)
    control = tmp_path / "authority"
    control.mkdir()
    (control / GIT_CONTROL_MARKER).write_text("fixture\n")

    observation = business.verify_business_checkout(str(control))

    assert observation.status == "incomplete" and "garde W1" in observation.detail
    assert boundary.calls == []


# ── échéance : arrêt autonome et conteneur tué PAR NOM ───────────────────────────────────────────────────────────────────


def test_a_deadline_kills_the_container_by_name_and_the_default_path_reports_it_as_incomplete(monkeypatch, tmp_path):
    boundary = Boundary(timeout_on_run=True)
    monkeypatch.setattr(subprocess, "run", boundary)
    root = tmp_path / "generated"
    root.mkdir()

    observation = business.verify_business_checkout(str(root), timeout=5)

    assert observation.status == "incomplete" and "délai" in observation.detail
    run_argv = next(call[0] for call in boundary.calls if call[0][1] == "run")
    name = run_argv[run_argv.index("--name") + 1]
    assert name.startswith("w4-verify-")
    assert [call[0] for call in boundary.calls if call[0][1] == "kill"] == [["docker", "kill", name]]


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, RuntimeError])
def test_any_interruption_kills_the_named_container_and_a_failing_kill_does_not_mask_it(monkeypatch, interruption):
    calls = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "run":
            raise interruption("stop")
        raise FileNotFoundError("docker")  # le kill lui-même échoue : l'interruption d'origine doit rester visible

    with pytest.raises(interruption):
        business.run_in_named_container(["docker", "run", "--name", "n1", "img"], name="n1", timeout=1, runner=runner)

    assert calls[-1] == ["docker", "kill", "n1"]


def test_subprocess_run_is_resolved_at_call_time_not_bound_at_import(monkeypatch):
    boundary = Boundary()
    monkeypatch.setattr(subprocess, "run", boundary)

    business.run_in_named_container(["docker", "run", "--name", "n2", "img"], name="n2", timeout=1)

    assert [call[0][:2] for call in boundary.calls] == [["docker", "run"]]


def test_the_script_stops_itself_at_its_own_deadline_even_if_nobody_kills_it(tmp_path):
    """Arrêt autonome : le processus vérifié se termine à l'échéance (code 124) sans aucune intervention du client."""
    root = tmp_path / "generated"
    root.mkdir()
    (root / "alembic.py").write_text("import time\ntime.sleep(4)\n", encoding="utf-8")  # inoffensif, se termine seul
    started = time.monotonic()

    observation = business.verify_business_checkout(
        str(root), python=sys.executable, runner=business.trusted_local_runner, timeout=3
    )

    assert observation.status == "incomplete" and "échéance autonome" in observation.detail
    assert time.monotonic() - started < 3.9, "arrêté par l'échéance propre du script, pas par la fin du témoin"
    time.sleep(1.2)  # laisse le témoin inoffensif se terminer avant le nettoyage du dossier de test


def test_docker_failures_are_incomplete_never_failed(monkeypatch, tmp_path):
    root = tmp_path / "generated"
    root.mkdir()
    monkeypatch.setattr(subprocess, "run", Boundary(docker_returncode=127))
    assert business.verify_business_checkout(str(root)).status == "incomplete"

    def missing(argv, *args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", missing)
    observation = business.verify_business_checkout(str(root))
    assert observation.status == "incomplete" and "indisponible" in observation.detail
