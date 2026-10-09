"""Isolement du vérificateur métier : le livrable généré (code NON FIABLE) ne s'exécute jamais sur l'hôte par défaut.

Aucun Docker, aucun réseau, aucun vrai secret : la frontière ``subprocess.run`` est remplacée par un double qui refuse Docker
(code 125) ; les témoins exécutables sont des modules inoffensifs écrits par le test dans un dossier du test.
"""

from __future__ import annotations

import json
import os
import shutil
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


IMAGE = "fixture-owned:never-pull"


def hostile_checkout(folder, tamper, sleep="time.sleep(8)"):
    """Livrable INOFFENSIF complet (référence de l'étape 3) dont l'import de ``app.main`` exécute d'abord ``tamper`` (minuteurs,
    signaux), consigne qu'il est atteint, puis ``sleep``."""
    from w4_business_fixture import stage_files

    checkout = folder / "checkout"
    for relative, content in stage_files(3).items():
        target = checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    marker = folder / "import-reached.txt"
    main = checkout / "app" / "main.py"
    main.write_text(
        f"import pathlib, signal, time\n{tamper}\npathlib.Path({str(marker)!r}).write_text('reached')\n{sleep}\n"
        + main.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return checkout, marker


def docker_cli_exit_status(raw_returncode):
    """Contrat de sortie du CLI Docker : le code du processus principal du conteneur, ou 128 + signal s'il est mort d'un signal.

    ``subprocess`` rend ce second cas en NÉGATIF (``-9``) quand le processus est exécuté localement : une frontière Docker simulée
    doit traduire (``-9`` -> 137), sinon le double expose un code que le vrai CLI ne produit jamais. Capturé en réel sur
    ``python:3.12-slim`` (GNU timeout 9.7 en PID 1, aucun réseau, aucun montage) : TERM honoré -> 124, TERM ignoré puis KILL -> 137
    (``evidence/w4-b-ci-deadline-docker-contract-term-ignored.txt``). GNU ``timeout`` envoie KILL à tout son groupe, lui compris :
    localement il meurt de SIGKILL (-9) là où uutils (Ubuntu 26.04) rend 137 ; la traduction rend les deux équivalents."""
    return 128 - raw_returncode if raw_returncode < 0 else raw_returncode


GNU_TIMEOUT = shutil.which("gnutimeout")  # GNU timeout explicite quand `timeout` est une autre implémentation (uutils)
TIMEOUT_BINARIES = ["timeout"] + (["gnutimeout"] if GNU_TIMEOUT and Path(GNU_TIMEOUT).name != "timeout" else [])


def run_in_place_of_docker(argv, checkout, options, real_run, *, timeout_binary="timeout"):
    """Exécute LOCALEMENT la commande qu'aurait lancée le conteneur (vraie commande publique, vrai superviseur ``timeout``) et
    rend le résultat dans le contrat du CLI Docker (voir :func:`docker_cli_exit_status`). Retourne ``(résultat, brut, durée)``."""
    assert argv[:2] == ["docker", "run"], argv[:2]
    mounts = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-v"]
    scratch = next(value.split(":/scratch:")[0] for value in mounts if ":/scratch:" in value)
    command = [str(a).replace("/scratch/", scratch + "/") for a in argv[argv.index(IMAGE) + 1 :]]
    command = [sys.executable if part == "python" else part for part in command]
    if command[0] == "timeout" and timeout_binary != "timeout":
        command[0] = GNU_TIMEOUT
    env = dict(options.get("env") or {})
    env["PATH"] = str(Path(sys.executable).parent) + ":" + os.environ.get("PATH", "")
    started = time.monotonic()
    result = real_run(command, cwd=checkout, env=env, capture_output=True, text=True, timeout=12)
    raw = result.returncode
    result.returncode = docker_cli_exit_status(raw)
    return result, raw, time.monotonic() - started


def run_public_docker_path_locally(monkeypatch, checkout, *, timeout_binary="timeout", **kwargs):
    """Chemin Docker PUBLIC : la commande réelle est construite ; seule la frontière Docker est remplacée par son exécution
    locale au contrat du CLI Docker (sans le délai du client hôte : un plafond de secours distinct de 12 s subsiste). Isole le
    superviseur de durée."""
    real_run = subprocess.run
    calls = []

    def docker_boundary(argv, **options):
        result, raw, seconds = run_in_place_of_docker(argv, checkout, options, real_run, timeout_binary=timeout_binary)
        calls.append(
            {"head": list(argv[argv.index(IMAGE) + 1 :][:5]), "raw_returncode": raw, "returncode": result.returncode,
             "seconds": seconds, "timeout_binary": timeout_binary}
        )  # fmt: skip
        return result

    monkeypatch.setattr(subprocess, "run", docker_boundary)
    started = time.monotonic()
    observation = business.verify_business_checkout(str(checkout), image=IMAGE, **kwargs)
    return observation, calls, time.monotonic() - started


def test_the_docker_boundary_double_reproduces_the_cli_exit_contract_for_signals():
    assert [docker_cli_exit_status(code) for code in (0, 1, 124, 137, -9, -15, -11)] == [0, 1, 124, 137, 137, 143, 139]


@pytest.mark.parametrize("timeout_binary", TIMEOUT_BINARIES)
@pytest.mark.parametrize(
    "tamper, bound, expected_status",
    [
        ("pass", 4.7, 124),  # témoin ordinaire : TERM honoré
        ("signal.alarm(0)", 4.7, 124),  # le livrable annule un minuteur interne
        ("signal.signal(signal.SIGALRM, signal.SIG_IGN)\nsignal.alarm(0)", 4.7, 124),
        ("signal.signal(signal.SIGTERM, signal.SIG_IGN)", 8.0, 137),  # ignore TERM : le superviseur tue (KILL)
    ],
    ids=["ordinary", "cancels-alarm", "ignores-alarm", "ignores-term"],
)
def test_the_duration_supervisor_lives_outside_the_untrusted_interpreter_and_cannot_be_cancelled_by_it(
    monkeypatch, tmp_path, tamper, bound, expected_status, timeout_binary
):
    checkout, marker = hostile_checkout(tmp_path, tamper, sleep="time.sleep(30)")

    observation, calls, elapsed = run_public_docker_path_locally(
        monkeypatch, checkout, timeout=4, timeout_binary=timeout_binary
    )

    assert marker.exists() and calls, "l'import du livrable a réellement été atteint"
    supervisor = calls[0]["head"]
    assert supervisor[:4] == ["timeout", "--signal=TERM", f"--kill-after={business.WATCHDOG_KILL_AFTER}", "4.000"]
    assert observation.status == "incomplete" and "échéance" in observation.detail
    assert calls[0]["returncode"] == expected_status, calls[0]  # contrat Docker : 124 (TERM) ou 137 (KILL), jamais -9
    assert calls[0]["raw_returncode"] in (expected_status, -(expected_status - 128)), calls[0]
    assert elapsed < bound, (
        f"arrêté par le superviseur ({elapsed:.1f}s), pas à la fin du témoin ou du plafond de secours"
    )


def test_the_container_command_puts_the_supervisor_between_the_image_and_the_script(monkeypatch, tmp_path):
    checkout, _ = hostile_checkout(tmp_path, "pass", sleep="pass")
    seen = []

    def boundary(argv, **options):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 125, "", "withheld")

    monkeypatch.setattr(subprocess, "run", boundary)
    business.verify_business_checkout(str(checkout), image=IMAGE, timeout=30)

    argv = seen[0]
    after_image = argv[argv.index(IMAGE) + 1 :]
    assert after_image[:2] == ["timeout", "--signal=TERM"] and after_image[4:6] == ["python", "-c"]
    assert "--user" in argv and "--network" in argv and "--read-only" in argv, "durcissement et garde W1 inchangés"


def test_the_oracle_image_check_requires_the_supervisor_binary():
    code = None

    def runner(argv):
        nonlocal code
        code = argv[-1]
        return subprocess.CompletedProcess(argv, 0, "", "")

    report = business.CampaignReport("preflight", "unit")
    step = report.declare("P08", "image")
    business.check_oracle_environment(report, step, image="img:x", runner=runner)

    assert "shutil.which('timeout')" in code and "pypdf" in code


# ── échéance globale PARTAGÉE par les phases de vérification ────────────────────────────────────────────────────────────


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def completed(phase_report):
    return subprocess.CompletedProcess([], 0, f"{business.REPORT_MARKER}{json.dumps(phase_report)}\n", "")


def test_the_two_verification_phases_share_the_remaining_global_time(tmp_path):
    clock = FakeClock()
    limits = []

    def runner(argv, cwd, env, limit):
        limits.append(limit)
        clock.now += 40.0
        return completed({"checks": {"ok": True}, "observations": {"audit_id": 7}})

    observation = business.verify_business_checkout(
        str(tmp_path), runner=runner, timeout=120.0, deadline_monotonic=clock.now + 100.0, clock=clock
    )

    assert observation.status == "passed"
    assert limits == [100.0, 60.0], (
        "chaque phase est bornée par le temps RESTANT, jamais par une nouvelle fenêtre de 120 s"
    )


def test_no_verification_phase_starts_after_expiry_and_a_phase_hitting_the_deadline_is_a_budget_stop(tmp_path):
    clock = FakeClock()
    started = []

    def runner(argv, cwd, env, limit):
        started.append(limit)
        clock.now += limit
        raise subprocess.TimeoutExpired(argv, limit)

    with pytest.raises(business.BudgetStop, match="pendant la phase « write »"):
        business.verify_business_checkout(
            str(tmp_path), runner=runner, timeout=120.0, deadline_monotonic=clock.now + 30.0, clock=clock
        )
    assert started == [30.0]
    with pytest.raises(business.BudgetStop, match="avant la phase « write »"):
        business.verify_business_checkout(
            str(tmp_path), runner=runner, timeout=120.0, deadline_monotonic=clock.now - 1.0, clock=clock
        )
    assert started == [30.0], "aucune commande lancée après expiration"


def supervisor_runner(clock, code, *, spend):
    """Double de la frontière d'exécution : rend ``code`` après avoir fait avancer l'horloge de ``spend`` secondes."""

    def runner(argv, cwd, env, limit):
        clock.now += spend(limit) if callable(spend) else spend
        return subprocess.CompletedProcess(argv, code, "", "")

    return runner


@pytest.mark.parametrize("code", [124, 137], ids=["TERM", "TERM-ignored-then-KILL"])
def test_a_supervisor_stop_after_the_global_limit_was_really_reached_is_a_budget_stop(tmp_path, code):
    clock = FakeClock()
    runner = supervisor_runner(clock, code, spend=lambda limit: limit + (3.0 if code == 137 else 0.0))

    with pytest.raises(business.BudgetStop, match="échéance globale atteinte pendant la phase"):
        business.verify_business_checkout(
            str(tmp_path), runner=runner, timeout=120.0, deadline_monotonic=clock.now + 5.0, clock=clock
        )
    # sans échéance globale restreignante, la même durée atteinte reste une validation incomplète (jamais un succès)
    clock2 = FakeClock()
    observation = business.verify_business_checkout(
        str(tmp_path), runner=supervisor_runner(clock2, code, spend=120.0), timeout=120.0, clock=clock2
    )
    assert observation.status == "incomplete" and "échéance de la vérification atteinte" in observation.detail


@pytest.mark.parametrize("code", [124, 137], ids=["124", "137"])
@pytest.mark.parametrize("with_global_deadline", [True, False], ids=["global-deadline-left", "no-global-deadline"])
def test_an_early_termination_is_never_reported_as_an_expiration(tmp_path, code, with_global_deadline):
    """Contre-épreuve du manager : 137 après ~1 s sur 19 s restantes n'est NI un dépassement NI un budget épuisé."""
    clock = FakeClock()
    kwargs = {"deadline_monotonic": clock.now + 20.0} if with_global_deadline else {}

    observation = business.verify_business_checkout(
        str(tmp_path), runner=supervisor_runner(clock, code, spend=1.0), timeout=120.0, clock=clock, **kwargs
    )

    assert observation.status == "incomplete", "preuve non établie : ni succès ni BudgetStop"
    assert "interruption précoce" in observation.detail and "pas une expiration" in observation.detail
    assert f"code {code}" in observation.detail and "cause non établie" in observation.detail
    assert "échéance globale" not in observation.detail and "OOM" not in observation.detail, "aucune cause prétendue"


def test_the_measured_duration_is_compared_with_the_imposed_limit_with_a_small_tolerance(tmp_path):
    clock = FakeClock()
    tolerance = business.DEADLINE_MEASURE_TOLERANCE
    just_in = supervisor_runner(clock, 124, spend=lambda limit: limit - tolerance / 2)
    with pytest.raises(business.BudgetStop):
        business.verify_business_checkout(
            str(tmp_path), runner=just_in, timeout=60.0, deadline_monotonic=clock.now + 10.0, clock=clock
        )
    clock = FakeClock()
    too_early = supervisor_runner(clock, 124, spend=lambda limit: limit - 2 * tolerance)
    observation = business.verify_business_checkout(
        str(tmp_path), runner=too_early, timeout=60.0, deadline_monotonic=clock.now + 10.0, clock=clock
    )
    assert observation.status == "incomplete" and "interruption précoce" in observation.detail


@pytest.mark.parametrize("timeout_binary", TIMEOUT_BINARIES)
def test_a_fixture_killing_itself_early_through_the_public_docker_path_is_incomplete_not_a_budget_stop(
    monkeypatch, tmp_path, timeout_binary
):
    """Chemin PUBLIC et vrai ``timeout`` : la mort de l'import par SIGKILL est rendue au contrat Docker (137), même si le code
    local brut est négatif (-9)."""
    checkout, marker = hostile_checkout(tmp_path, "import os", sleep="os.kill(os.getpid(), signal.SIGKILL)")

    observation, calls, _ = run_public_docker_path_locally(
        monkeypatch, checkout, timeout=120, deadline_monotonic=time.monotonic() + 20.0, timeout_binary=timeout_binary
    )

    assert marker.exists() and [c["returncode"] for c in calls] == [137], calls
    assert calls[0]["raw_returncode"] in (-9, 137)
    assert observation.status == "incomplete" and "interruption précoce" in observation.detail
    assert "pas une expiration" in observation.detail


@pytest.mark.parametrize("timeout_binary", TIMEOUT_BINARIES)
def test_a_term_ignoring_fixture_reaching_the_global_deadline_is_a_budget_stop_via_the_kill_after(
    monkeypatch, tmp_path, timeout_binary
):
    checkout, marker = hostile_checkout(
        tmp_path, "signal.signal(signal.SIGTERM, signal.SIG_IGN)", sleep="time.sleep(30)"
    )
    started = time.monotonic()

    with pytest.raises(business.BudgetStop, match="échéance globale atteinte pendant la phase"):
        run_public_docker_path_locally(
            monkeypatch,
            checkout,
            timeout=120,
            deadline_monotonic=time.monotonic() + 2.0,
            timeout_binary=timeout_binary,
        )

    assert marker.exists() and time.monotonic() - started < 9.0, "TERM ignoré : tué par KILL (137), durée atteinte"


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
