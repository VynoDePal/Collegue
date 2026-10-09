"""Entrée CLI réelle (``python -m collegue.pilot.w4_business``) : contrôles SANS clé ≠ validation EFFECTIVE avant lancement.

Aucun socket, aucun Docker, aucune émission LLM : GitHub n'existe qu'à la frontière (faux serveur), les clés sont FACTICES et ne
doivent apparaître dans aucune sortie. Ces tests couvrent le câblage de ``main`` ; ``launch_campaign`` seul ne le couvre pas.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess

import pytest
from test_w4_business_report import (
    FixtureNamedServer,
    accepting_capacity,
    full_clients,
    ok_routes,
)

from collegue.pilot import w4_business as business

FAKE_KEY = "fake-model-key-for-cli-test"
FAKE_ROLE_KEY = "fake-role-key-for-cli-test"
FAKE_GITHUB = "fake-github-token-for-cli-test"


def cli_environment(root, **extra):
    return {
        **business.campaign_environment("cli", str(root)),
        "PATH": os.environ["PATH"],
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_RUN_ID": "4242",
        "GITHUB_RUN_ATTEMPT": "1",
        "W4_BUSINESS_CONFIRM": business.LAUNCH_CONFIRMATION,
        "GITHUB_TOKEN": FAKE_GITHUB,
        "LLM_PROVIDER": "gemini",
        "LLM_MODEL": "gemini-2.5-flash",
        "LLM_API_KEY": FAKE_KEY,
        "LLM_API_KEY_CODER": FAKE_ROLE_KEY,
        "COLLEGUE_NIGHTLY_MANIFEST": str(root / "manifest.json"),
        **extra,
    }


@pytest.fixture
def boundary(monkeypatch):
    """GitHub factice à la frontière ; toute autre sortie (socket, sous-processus) échoue le test."""
    server = FixtureNamedServer()
    server.add_ruleset(1)
    clients = full_clients(server)
    monkeypatch.setattr(business, "_fixture_clients", lambda _token: clients)
    docker_calls = []
    real_run = subprocess.run

    def only_image_checks(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv[:2] == ["docker", "run"] and "importlib.util" in " ".join(argv):
            docker_calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(f"transport externe interdit: {list(argv)[:2]}")

    def no_socket(*args, **kwargs):
        raise AssertionError("socket interdit")

    monkeypatch.setattr(subprocess, "run", only_image_checks)
    monkeypatch.setattr(socket.socket, "connect", no_socket)
    monkeypatch.setattr(business, "route_validator", lambda: ok_routes)
    return SimpleBoundary(server, docker_calls, real_run)


class SimpleBoundary:
    def __init__(self, server, docker_calls, real_run):
        self.server, self.docker_calls, self.real_run = server, docker_calls, real_run


def invoke(monkeypatch, tmp_path, action, *arguments, **extra):
    env = cli_environment(tmp_path, **extra)
    output = tmp_path / f"{action}.json"
    out = io.StringIO()
    monkeypatch.setattr(os, "environ", env)
    with contextlib.redirect_stdout(out):
        code = business.main([action, "--campaign-id", "cli", "--output", str(output), *arguments])
    report = json.loads(output.read_text())
    shown = out.getvalue() + output.read_text()
    for secret in (FAKE_KEY, FAKE_ROLE_KEY, FAKE_GITHUB):
        assert secret not in shown, "une valeur secrète est apparue dans une sortie"
    return code, report, {s["id"]: s for s in report["steps"]}


def test_run_validates_the_effective_configuration_with_the_legitimate_key_instead_of_rejecting_it(
    monkeypatch, tmp_path, boundary
):
    code, report, steps = invoke(monkeypatch, tmp_path, "run")

    assert steps["P03-secret-scope"]["state"] == "succeeded", steps["P03-secret-scope"]
    assert steps["P03-secret-scope"]["evidence"]["llm_secret_names_present"] == ["LLM_API_KEY", "LLM_API_KEY_CODER"]
    assert steps["P05-role-routes"]["state"] == "succeeded"
    assert steps["P05-role-routes"]["evidence"]["credential_required"] is True, (
        "la clé est exigée à l'étape de lancement"
    )
    # raison précise de la validation effective : le worker choisi (clé API facturable) ne tient pas le mode strict
    assert steps["P06-worker-capacity"]["state"] == "incomplete_validation"
    assert (
        "OHSdkAgent" in steps["P06-worker-capacity"]["detail"]
        and "FACTURABLE" in steps["P06-worker-capacity"]["detail"]
    )
    assert code == 3 and report["verdict"] == "incomplete_validation"
    assert report["facts"]["billable_actions_emitted"] == 0 and report["facts"]["stop_point"] == "preflight"
    assert steps["R01-run"]["state"] == "not_executed"


def test_the_keyless_preflight_action_still_rejects_the_same_keys(monkeypatch, tmp_path, boundary):
    code, report, steps = invoke(monkeypatch, tmp_path, "preflight")

    assert steps["P03-secret-scope"]["state"] == "failed"
    assert "LLM_API_KEY" in steps["P03-secret-scope"]["detail"] and code == 1


def test_run_refuses_the_static_stage_so_the_image_check_cannot_be_skipped(monkeypatch, tmp_path, boundary):
    monkeypatch.setattr(os, "environ", cli_environment(tmp_path))
    with pytest.raises(SystemExit) as stop, contextlib.redirect_stderr(io.StringIO()) as err:
        business.main(["run", "--stage", "static", "--campaign-id", "cli"])
    assert stop.value.code == 2 and "image du gate" in err.getvalue()
    assert boundary.docker_calls == []


def test_the_static_preflight_skips_only_the_optional_image_step(monkeypatch, tmp_path, boundary):
    env = cli_environment(tmp_path)
    for name in ("LLM_API_KEY", "LLM_API_KEY_CODER"):
        env.pop(name)
    monkeypatch.setattr(business, "effective_worker_capacity", accepting_capacity)
    output = tmp_path / "static.json"
    monkeypatch.setattr(os, "environ", env)
    with contextlib.redirect_stdout(io.StringIO()):
        code = business.main(["preflight", "--stage", "static", "--output", str(output)])
    steps = {s["id"]: s for s in json.loads(output.read_text())["steps"]}

    assert code == 0 and steps["P08-oracle-environment"]["state"] == "not_executed"
    assert steps["P08-oracle-environment"]["required"] is False and boundary.docker_calls == []


def test_a_validated_run_preflight_checks_the_gate_image_and_launches_exactly_once(monkeypatch, tmp_path, boundary):
    launched = []

    def launch(report, **kwargs):
        launched.append(report)
        report.facts["launch"] = {"project_id": 9}
        raise business.BudgetStop("arrêt de test : aucune émission réelle")

    monkeypatch.setattr(business, "effective_worker_capacity", accepting_capacity)
    monkeypatch.setattr(business, "launch_campaign", launch)

    code, report, steps = invoke(monkeypatch, tmp_path, "run", SANDBOX_IMAGE="registry.example/gate:pinned")

    assert steps["P08-oracle-environment"]["state"] == "succeeded" and steps["P08-oracle-environment"]["required"]
    assert [call[call.index("--cap-drop") + 2] for call in boundary.docker_calls] == ["registry.example/gate:pinned"]
    assert "never" in boundary.docker_calls[0] and len(launched) == 1
    assert steps["R01-run"]["state"] == "budget_stop" and report["facts"]["launch"]["project_id"] == 9
    assert code == 4 and report["verdict"] == "budget_stop"
    assert steps["R04-improvement"]["state"] == "not_executed" and steps["R04-improvement"]["required"]
