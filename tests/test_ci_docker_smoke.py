"""Smoke Docker de la CI : il doit prouver que le service est PRÊT, pas seulement lancé.

Défaut d'origine (tests.yml, job « Docker build ») :

    docker run --rm -d --name collegue-smoke ... collegue:ci &
    sleep 5
    docker logs collegue-smoke || true
    docker stop collegue-smoke || true

Le job était vert quoi qu'il arrive : conteneur déjà mort (``--rm`` supprimait même
ses logs), service jamais prêt, ou démarrage encore en cours après 5 s. Le log CI du
run 29215033828 (main 51ab3fc) s'arrête sur « Validation du modèle LLM 'test-model'
(provider=gemini) en cours... » : le MCP n'avait même pas fini de démarrer.

Les tests exécutent le vrai script contre un binaire ``docker`` factice (aucun
Docker, aucun réseau, aucun LLM) qui simule succès, crash, démarrage indisponible…
"""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = ROOT / "scripts" / "ci_docker_smoke.sh"
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
CONTAINER = "collegue-smoke"

# Codes de sortie documentés du script.
EXIT_RUN_FAILED = 10
EXIT_EXITED_EARLY = 11
EXIT_NOT_READY = 12
EXIT_DIED_AFTER_READY = 13

_DOCKER_STUB = r"""#!/bin/bash
# Docker factice : journalise chaque appel et simule le comportement du conteneur.
state="${STUB_STATE:?}"
scenario="${STUB_SCENARIO:-success}"
echo "$*" >> "$state/calls.log"
bump() { local f="$state/$1"; local n=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$f"; echo "$n"; }

case "$1" in
  run)
    if [ "$scenario" = "run-fails" ]; then
      echo "docker: Error response from daemon: simulated run failure" >&2
      exit 125
    fi
    touch "$state/container"
    echo "0123456789abcdef"
    ;;
  inspect)
    n=$(bump inspect_n)
    case "$scenario" in
      crash) echo "false ${STUB_EXIT_CODE:-1} false" ;;
      crash-late) if [ "$n" -gt 2 ]; then echo "false 3 false"; else echo "true 0 false"; fi ;;
      dies-after-ready) if [ -f "$state/ready" ]; then echo "false 137 true"; else echo "true 0 false"; fi ;;
      *) echo "true 0 false" ;;
    esac
    ;;
  exec)
    case "$*" in
      *mcp-ready*)
        # Commande du healthcheck Compose exécutée dans le conteneur.
        [ "$scenario" = "healthcheck-fails" ] && exit 1
        [ -f "$state/ready" ] || exit 1
        exit 0
        ;;
      *:4122/*)
        n=$(bump health_n)
        case "$scenario" in
          never-ready) exit 7 ;;
          health-invalid) echo '{"status":"degraded"}'; exit 0 ;;
          *) if [ "$n" -le "${STUB_READY_AFTER:-2}" ]; then exit 7; fi; echo '{"status":"ok"}'; exit 0 ;;
        esac
        ;;
      *:4121/*)
        case "$scenario" in
          mcp-down) printf '\n000'; exit 7 ;;
          mcp-bad-status) printf 'unavailable\n503'; exit 0 ;;
          *) touch "$state/ready"; printf 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n200'; exit 0 ;;
        esac
        ;;
      *) echo "stub docker: exec inattendu: $*" >&2; exit 99 ;;
    esac
    ;;
  logs)
    echo "STUB-CONTAINER-STDOUT scenario=$scenario"
    echo "STUB-CONTAINER-STDERR Traceback simulated" >&2
    ;;
  stop)
    if [ "${STUB_STOP_FAIL:-0}" = "1" ]; then echo "stop failed" >&2; exit 1; fi
    ;;
  rm)
    if [ "${STUB_RM_FAIL:-0}" = "1" ]; then echo "rm failed" >&2; exit 1; fi
    rm -f "$state/container"
    ;;
  *)
    echo "stub docker: commande inattendue: $*" >&2
    exit 99
    ;;
esac
"""


@pytest.fixture
def stub(tmp_path: Path):
    """Installe le docker factice ; retourne (env, state_dir, log_dir)."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(_DOCKER_STUB, encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    state = tmp_path / "state"
    state.mkdir()
    log_dir = tmp_path / "smoke-logs"
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "STUB_STATE": str(state),
            "SMOKE_LOG_DIR": str(log_dir),
            "SMOKE_TIMEOUT_SECONDS": "2",
            "SMOKE_POLL_INTERVAL_SECONDS": "0.1",
        }
    )
    return env, state, log_dir


def _calls(state: Path) -> list[str]:
    path = state / "calls.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _run_script(env: dict[str, str], **overrides: str) -> subprocess.CompletedProcess[str]:
    env = {**env, **overrides}
    return subprocess.run(
        ["bash", str(SMOKE_SCRIPT), "collegue:ci"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _workflow_smoke_step() -> dict:
    workflow = yaml.safe_load(TESTS_WORKFLOW.read_text(encoding="utf-8"))
    for step in workflow["jobs"]["docker-build"]["steps"]:
        if str(step.get("name", "")).startswith("Smoke test"):
            return step
    raise AssertionError("étape « Smoke test » introuvable dans tests.yml")


def _run_workflow_step(env: dict[str, str], **overrides: str) -> subprocess.CompletedProcess[str]:
    """Exécute le `run:` de l'étape du workflow avec le shell par défaut de GitHub (bash -e)."""

    step = _workflow_smoke_step()
    script = Path(env["STUB_STATE"]).parent / "step.sh"
    script.write_text(step["run"], encoding="utf-8")
    return subprocess.run(
        ["bash", "-e", str(script)],
        cwd=ROOT,
        env={**env, **overrides},
        capture_output=True,
        text=True,
        timeout=120,
    )


# --- Le script -------------------------------------------------------------------


def test_success_requires_health_and_mcp_readiness_then_cleans_up(stub) -> None:
    env, state, log_dir = stub

    completed = _run_script(env, STUB_SCENARIO="success", STUB_READY_AFTER="2")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "::warning::" not in completed.stdout  # nettoyage propre : aucun avertissement
    calls = _calls(state)
    # La disponibilité est réellement interrogée (et pas seulement `docker run` réussi).
    assert sum(":4122/_health" in call for call in calls) >= 3
    assert any(":4121/mcp/" in call for call in calls)
    # Logs conservés, nettoyage effectué.
    log = (log_dir / f"{CONTAINER}.log").read_text(encoding="utf-8")
    assert "STUB-CONTAINER-STDOUT" in log
    assert "STUB-CONTAINER-STDERR" in log
    assert not (state / "container").exists()
    assert any(call.startswith("rm ") and CONTAINER in call for call in calls)


def test_success_also_runs_the_compose_healthcheck_command_inside_the_container(stub) -> None:
    env, state, _ = stub

    completed = _run_script(env, STUB_SCENARIO="success")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert any(call.startswith("exec ") and "entrypoint.sh mcp-ready" in call for call in _calls(state))


def test_failing_compose_healthcheck_means_not_ready(stub) -> None:
    """Santé et MCP répondent mais la commande de healthcheck du Compose échoue : pas prêt."""

    env, state, _ = stub

    completed = _run_script(env, STUB_SCENARIO="healthcheck-fails")

    assert completed.returncode == EXIT_NOT_READY, completed.stdout + completed.stderr
    assert not (state / "container").exists()


def test_smoke_runs_exactly_the_healthcheck_command_declared_in_compose() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    declared = compose["services"]["collegue-app"]["healthcheck"]["test"]

    assert declared[0] == "CMD-SHELL"
    assert declared[1] in SMOKE_SCRIPT.read_text(encoding="utf-8")


def test_container_is_started_without_network_and_without_auto_removal(stub) -> None:
    """Aucun appel LLM/API possible (--network none) ; pas de --rm pour garder les logs d'un crash."""

    env, state, _ = stub

    completed = _run_script(env, STUB_SCENARIO="success")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    run_call = next(call for call in _calls(state) if call.startswith("run "))
    assert "--network none" in run_call
    assert "--rm" not in run_call.split()
    assert "-d" in run_call.split()
    # Profil du catalogue supporté, valeurs FACTICES ; aucune clé hôte ni option qui contournerait la validation.
    assert "-e LLM_PROVIDER=gemini" in run_call
    assert "-e LLM_MODEL=test-model" in run_call
    assert "-e LLM_API_KEY=test-key" in run_call
    assert "anthropic" not in run_call.lower()
    assert " -e LLM_API_KEY " not in run_call and "--env-file" not in run_call
    assert run_call.rstrip().endswith("collegue:ci")


def test_crash_before_ready_fails_with_logs_and_cleanup(stub) -> None:
    env, state, log_dir = stub

    completed = _run_script(env, STUB_SCENARIO="crash", STUB_EXIT_CODE="1")

    assert completed.returncode == EXIT_EXITED_EARLY, completed.stdout + completed.stderr
    log = (log_dir / f"{CONTAINER}.log").read_text(encoding="utf-8")
    assert "Traceback simulated" in log
    assert "Traceback simulated" in completed.stdout + completed.stderr  # visible dans le log CI
    assert not (state / "container").exists()


def test_crash_reports_the_container_exit_code(stub) -> None:
    env, _, _ = stub

    completed = _run_script(env, STUB_SCENARIO="crash", STUB_EXIT_CODE="137")

    assert completed.returncode == EXIT_EXITED_EARLY
    assert "137" in completed.stdout + completed.stderr


def test_exit_code_zero_before_ready_is_still_a_failure(stub) -> None:
    """Un service qui se termine « proprement » avant d'être prêt n'est pas un service démarré."""

    env, _, _ = stub

    completed = _run_script(env, STUB_SCENARIO="crash", STUB_EXIT_CODE="0")

    assert completed.returncode == EXIT_EXITED_EARLY


def test_crash_after_a_few_polls_is_detected(stub) -> None:
    env, _, _ = stub

    completed = _run_script(env, STUB_SCENARIO="crash-late", STUB_READY_AFTER="50", SMOKE_TIMEOUT_SECONDS="5")

    assert completed.returncode == EXIT_EXITED_EARLY


def test_service_never_ready_times_out_in_bounded_time(stub) -> None:
    env, state, log_dir = stub

    started = time.monotonic()
    completed = _run_script(env, STUB_SCENARIO="never-ready", SMOKE_TIMEOUT_SECONDS="2")
    elapsed = time.monotonic() - started

    assert completed.returncode == EXIT_NOT_READY, completed.stdout + completed.stderr
    assert elapsed < 20
    assert (log_dir / f"{CONTAINER}.log").exists()
    assert not (state / "container").exists()


def test_invalid_health_body_is_not_ready(stub) -> None:
    env, _, _ = stub

    completed = _run_script(env, STUB_SCENARIO="health-invalid")

    assert completed.returncode == EXIT_NOT_READY


@pytest.mark.parametrize("scenario", ["mcp-down", "mcp-bad-status"])
def test_health_ok_but_mcp_unavailable_is_not_ready(stub, scenario: str) -> None:
    env, state, _ = stub

    completed = _run_script(env, STUB_SCENARIO=scenario)

    assert completed.returncode == EXIT_NOT_READY, completed.stdout + completed.stderr
    assert not (state / "container").exists()


def test_container_dying_right_after_ready_is_a_failure(stub) -> None:
    env, _, _ = stub

    completed = _run_script(env, STUB_SCENARIO="dies-after-ready")

    assert completed.returncode == EXIT_DIED_AFTER_READY, completed.stdout + completed.stderr


def test_docker_run_failure_is_reported_and_cleaned(stub) -> None:
    env, state, _ = stub

    completed = _run_script(env, STUB_SCENARIO="run-fails")

    assert completed.returncode == EXIT_RUN_FAILED, completed.stdout + completed.stderr
    assert "simulated run failure" in completed.stdout + completed.stderr
    assert any(call.startswith("rm ") for call in _calls(state))


def test_cleanup_failure_never_masks_the_original_failure(stub) -> None:
    env, _, _ = stub

    completed = _run_script(env, STUB_SCENARIO="crash", STUB_STOP_FAIL="1", STUB_RM_FAIL="1")

    assert completed.returncode == EXIT_EXITED_EARLY, completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("failing", "needle"),
    [("STUB_STOP_FAIL", "docker stop a échoué"), ("STUB_RM_FAIL", "docker rm -f a échoué")],
)
def test_cleanup_failure_after_success_is_reported_but_not_fatal(stub, failing: str, needle: str) -> None:
    env, _, _ = stub

    completed = _run_script(env, STUB_SCENARIO="success", **{failing: "1"})

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"::warning::{needle}" in completed.stdout


def test_missing_image_argument_is_a_usage_error(stub) -> None:
    env, state, _ = stub

    completed = subprocess.run(
        ["bash", str(SMOKE_SCRIPT)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30
    )

    assert completed.returncode == 2
    assert _calls(state) == []


# --- Le workflow ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("crash", EXIT_EXITED_EARLY),
        ("never-ready", EXIT_NOT_READY),
        ("mcp-down", EXIT_NOT_READY),
        ("run-fails", EXIT_RUN_FAILED),
    ],
)
def test_workflow_step_fails_when_the_service_is_not_up(stub, scenario: str, expected: int) -> None:
    """Reproduit le défaut : l'ancienne étape restait verte quel que soit le scénario."""

    env, _, _ = stub

    completed = _run_workflow_step(env, STUB_SCENARIO=scenario)

    assert completed.returncode == expected, completed.stdout + completed.stderr


def test_workflow_step_passes_when_the_service_is_ready(stub) -> None:
    env, state, log_dir = stub

    completed = _run_workflow_step(env, STUB_SCENARIO="success")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (log_dir / f"{CONTAINER}.log").exists()
    assert not (state / "container").exists()


def test_workflow_uploads_smoke_logs_even_on_failure() -> None:
    workflow = yaml.safe_load(TESTS_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["docker-build"]["steps"]

    upload = next(
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/upload-artifact@") and "smoke" in str(step.get("with", {}))
    )
    assert upload["if"] == "always()"
    assert "smoke-logs" in upload["with"]["path"]
    smoke_index = steps.index(_workflow_smoke_step())
    assert steps.index(upload) > smoke_index


def test_smoke_script_syntax_is_valid() -> None:
    completed = subprocess.run(["bash", "-n", str(SMOKE_SCRIPT)], capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr


# --- Prémisse : la config du smoke ne déclenche aucun appel LLM -----------------


def test_supported_smoke_profile_startup_validation_makes_no_remote_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Le smoke démarre avec LLM_PROVIDER=gemini + LLM_MODEL=test-model + LLM_API_KEY=test-key (factices) parce que
    ``validate_llm_config`` valide le routage LOCALEMENT. Aucune émission réseau ni appel de SDK distant n'est toléré ;
    c'est la VRAIE validation de l'application qui est appelée, pas une copie de sa logique. Ce n'est pas une preuve de
    disponibilité du modèle. Si la validation redevenait distante, ce test le signalerait."""

    import asyncio
    import socket
    import sys

    from collegue import app as collegue_app

    def _no_network(*args, **kwargs):
        raise AssertionError("émission réseau interdite pendant la validation LLM du smoke")

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket, "getaddrinfo", _no_network)

    # Aucun SDK distant : toute instanciation de client ou de génération est une faute.
    imported_before = set(sys.modules)
    for module_name, attrs in {
        "openai": ("OpenAI", "AsyncOpenAI"),
        "anthropic": ("Anthropic", "AsyncAnthropic"),
        "google.genai": ("Client",),
    }.items():
        module = sys.modules.get(module_name)
        if module is None:
            continue
        for attr in attrs:
            if hasattr(module, attr):

                def _forbidden(*args, _n=f"{module_name}.{attr}", **kwargs):
                    raise AssertionError(f"SDK distant interdit pendant la validation du smoke : {_n}")

                monkeypatch.setattr(module, attr, _forbidden)

    for var in ("LLM_BASE_URL", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(collegue_app.settings, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(collegue_app.settings, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(collegue_app.settings, "LLM_MODEL", "test-model")

    assert asyncio.run(collegue_app.validate_llm_config()) is True
    # La validation ne doit pas avoir importé un SDK distant pour l'occasion.
    assert not ({"anthropic"} & (set(sys.modules) - imported_before))
