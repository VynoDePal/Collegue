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

REAL_ROUTE_VALIDATOR = business.route_validator  # avant que la fixture ne le remplace : l'API publique de routage d'A

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
        "LLM_MODEL": business.MODEL_PRIMARY,
        "LLM_API_KEY": FAKE_KEY,
        "LLM_API_KEY_CODER": FAKE_ROLE_KEY,
        "COLLEGUE_NIGHTLY_MANIFEST": str(root / "manifest.json"),
        **extra,
    }


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    """GitHub factice à la frontière ; toute autre sortie (socket, sous-processus) échoue le test.

    Le répertoire courant est un dossier vierge : un ``.env`` de développeur ne doit pas rendre la configuration ambiguë."""
    monkeypatch.chdir(tmp_path)
    # Les contrôles W5 (socle, modèles, relais, identité) ont leurs propres tests : ces tests-ci isolent le câblage W4 de `main`.
    monkeypatch.setattr("collegue.pilot.w5_business.w5_preflight_checks", lambda *args, **kwargs: [])
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


def invoke(monkeypatch, tmp_path, action, *arguments, drop=(), keep_role_key=False, **extra):
    env = cli_environment(tmp_path, **extra)
    if action == "run" and not keep_role_key:
        env.pop("LLM_API_KEY_CODER")  # contrat de clé W5 : l'étape réelle ne porte QUE LLM_API_KEY
    for name in drop:
        env.pop(name)
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
    # la VRAIE validation de routes d'A (pas le substitut de la fixture) : la route effective de chaque rôle est Gemma 4 chez Google
    monkeypatch.setattr(business, "route_validator", REAL_ROUTE_VALIDATOR)
    code, report, steps = invoke(monkeypatch, tmp_path, "run")

    assert steps["P03-secret-scope"]["state"] == "succeeded", steps["P03-secret-scope"]
    assert steps["P03-secret-scope"]["evidence"]["llm_secret_names_present"] == ["LLM_API_KEY"], (
        "la clé de campagne est acceptée au bon nom, seul son NOM est consigné"
    )
    assert steps["P05-role-routes"]["state"] == "succeeded"
    assert steps["P05-role-routes"]["evidence"]["credential_required"] is True, (
        "la clé est exigée à l'étape de lancement"
    )
    routes = steps["P05-role-routes"]["evidence"]["routes"]
    assert sorted(routes) == ["coder", "planner", "qa", "reviewer"]
    for role, route in routes.items():
        assert (route["provider"], route["model"]) == ("gemini", business.MODEL_PRIMARY), role
        assert route["endpoint"].startswith("https://generativelanguage.googleapis.com/"), role
        assert route["credential_source"] == "global" and route["credential_present"] is True, role
    # capacité RÉELLEMENT bornée : celle du relais budgétaire, prouvée par l'interface publique d'A (pas un OHSdkAgent à clé directe)
    capacity = steps["P06-worker-capacity"]
    assert capacity["state"] == "succeeded", capacity
    assert capacity["evidence"]["effective"]["source"] == "collegue.broker.capability_proof"
    assert capacity["evidence"]["effective"]["accepted"] is True
    # tout le préflight est vert ; l'arrêt a une raison précise et sûre : le socle approuvé (manifeste) n'est pas fourni
    assert all(step["state"] == "succeeded" for step_id, step in steps.items() if step_id.startswith("P0"))
    assert steps["R01-run"]["state"] == "incomplete_validation"
    assert "W5_BOOTSTRAP_MANIFEST absent" in steps["R01-run"]["detail"]
    assert code == 3 and report["verdict"] == "incomplete_validation"
    assert report["facts"]["billable_actions_emitted"] == 0 and report["facts"].get("llm_calls_emitted", 0) == 0
    for later in ("R02-business", "R04-improvement", "R05-incident-rollback"):
        assert steps[later]["state"] == "not_executed", later


def test_run_refuses_a_per_role_key_that_is_not_part_of_the_key_contract(monkeypatch, tmp_path, boundary):
    code, report, steps = invoke(monkeypatch, tmp_path, "run", keep_role_key=True)

    assert steps["P03-secret-scope"]["state"] == "failed" and "hors contrat" in steps["P03-secret-scope"]["detail"]
    assert "LLM_API_KEY_CODER" in steps["P03-secret-scope"]["detail"]
    assert (
        code == 1 and report["facts"]["billable_actions_emitted"] == 0 and steps["R01-run"]["state"] == "not_executed"
    )


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


# ── .env local : la configuration validée est celle qui sera émise, ou le lancement est refusé ─────────────────────────────

FAKE_DOTENV_KEY = "fake-dotenv-key-never-real"


def write_dotenv(folder):
    (folder / ".env").write_text(
        f"LLM_PROVIDER_CODER=openai\nLLM_MODEL_CODER=gpt-5.4\nLLM_API_KEY_CODER={FAKE_DOTENV_KEY}\n", encoding="utf-8"
    )


def test_a_local_dotenv_makes_the_effective_configuration_ambiguous_and_is_refused_unread(monkeypatch, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    write_dotenv(work)
    monkeypatch.chdir(work)

    with pytest.raises(business.IncompleteValidation, match=r"configuration locale ambiguë.*\.env") as refusal:
        business.effective_settings({"PATH": os.environ["PATH"], "LLM_PROVIDER": "gemini"})

    assert FAKE_DOTENV_KEY not in str(refusal.value) and "gpt-5.4" not in str(refusal.value), (
        "le fichier n'est ni lu ni cité"
    )
    with pytest.raises(
        business.IncompleteValidation
    ):  # l'environnement ne lève PAS l'ambiguïté : le produit lit aussi le fichier
        business.effective_settings(
            {"PATH": os.environ["PATH"], "LLM_PROVIDER_CODER": "gemini", "LLM_MODEL_CODER": "x"}
        )
    other = tmp_path / "other"
    other.mkdir()
    settings = business.effective_settings({"PATH": os.environ["PATH"], "LLM_PROVIDER": "gemini"}, cwd=str(other))
    assert settings.LLM_PROVIDER == "gemini", (
        "témoin : sans .env la validation reste exactement celle de l'environnement"
    )


def test_the_clean_directory_control_matches_the_product_settings(monkeypatch, tmp_path):
    from collegue.config import Settings

    monkeypatch.chdir(tmp_path)
    env = {
        "PATH": os.environ["PATH"],
        "LLM_PROVIDER": "gemini",
        "LLM_MODEL": "gemini-2.5-flash",
        "LLM_API_KEY": FAKE_KEY,
    }
    monkeypatch.setattr(os, "environ", dict(env))

    product = Settings()
    checked = business.effective_settings(env)

    for name in ("LLM_PROVIDER", "LLM_MODEL", "LLM_PROVIDER_CODER", "LLM_MODEL_CODER", "LLM_BASE_URL_CODER"):
        assert getattr(checked, name, None) == getattr(product, name, None)


def test_the_keyless_preflight_refuses_a_local_dotenv_before_the_worker_check_and_never_leaks_it(
    monkeypatch, tmp_path, boundary
):
    work = tmp_path / "work"
    work.mkdir()
    write_dotenv(work)
    monkeypatch.chdir(work)

    code, report, steps = invoke(
        monkeypatch, tmp_path, "preflight", "--stage", "static", drop=("LLM_API_KEY", "LLM_API_KEY_CODER")
    )

    assert steps["P03-secret-scope"]["state"] == "succeeded"
    assert steps["P05-role-routes"]["state"] == "incomplete_validation"
    assert "configuration locale ambiguë" in steps["P05-role-routes"]["detail"]
    assert steps["P06-worker-capacity"]["state"] == "not_executed"
    assert code == 3 and FAKE_DOTENV_KEY not in json.dumps(report)
    assert boundary.docker_calls == []


def test_run_in_a_directory_with_a_dotenv_never_launches(monkeypatch, tmp_path, boundary):
    work = tmp_path / "work"
    work.mkdir()
    write_dotenv(work)
    monkeypatch.chdir(work)
    launched = []
    monkeypatch.setattr(business, "launch_campaign", lambda *a, **k: launched.append(a))

    code, report, steps = invoke(monkeypatch, tmp_path, "run")

    assert steps["P05-role-routes"]["state"] == "incomplete_validation" and launched == []
    assert report["facts"]["billable_actions_emitted"] == 0 and code == 3


# ── entrée CLI composée : échéance partagée, registre relu après l'arrêt de la vérification ────────────────────────────────


class ControlledClock:
    def __init__(self, now=5000.0):
        self.now = now

    def __call__(self):
        return self.now


def spend_in_registry(url, *, usd=0.125, tokens=750):
    from collegue.state import ProjectStateManager

    manager = ProjectStateManager.from_url(url, create=True)
    project_id = manager.create_project(name="cli accounting", spec="x")
    ledger = manager.budget_ledger
    scope = ledger.scope_for_project(project_id, max_cost_usd=2, max_tokens=250000, strict=True)
    reservation = ledger.reserve(scope.scope_key, usd=0.2, tokens=1000)
    ledger.commit(reservation.reservation_id, usd=usd, tokens=tokens)
    return project_id


def fake_launch(monkeypatch, tmp_path, project_id, clock, *, advance):
    clone = tmp_path / "clone" / "fixture"
    clone.mkdir(parents=True)

    def launch(report, **kwargs):
        report.facts["launch"] = {"project_id": project_id}
        clock.now += advance
        return {"project_id": project_id, "final_checkout": str(clone)}

    monkeypatch.setattr(business, "launch_campaign", launch)
    return clone


def test_the_cli_shares_one_global_deadline_with_the_verification_and_reads_the_spent_registry(
    monkeypatch, tmp_path, boundary
):
    url = business.campaign_environment("cli", str(tmp_path))["STATE_DATABASE_URL"]
    project_id = spend_in_registry(url)
    clock = ControlledClock()
    monkeypatch.setattr(business.time, "monotonic", clock)
    monkeypatch.setattr(business, "effective_worker_capacity", accepting_capacity)
    clone = fake_launch(monkeypatch, tmp_path, project_id, clock, advance=901.0)  # le BUILD consomme TOUTE l'enveloppe

    code, report, steps = invoke(monkeypatch, tmp_path, "run")

    assert steps["R01-run"]["state"] == "succeeded"
    assert steps["R02-business"]["state"] == "budget_stop", (
        "aucune nouvelle vérification après l'expiration de l'enveloppe"
    )
    assert "avant la phase" in steps["R02-business"]["detail"]
    assert steps["R03-registry"]["state"] == "succeeded"
    assert report["facts"]["registry_final"]["consumed_micro_usd"] == 125_000
    assert report["facts"]["registry_final"]["consumed_tokens"] == 750
    assert code == 4 and report["verdict"] == "budget_stop"
    assert steps["R04-improvement"]["state"] == "not_executed"
    assert not [c for c in boundary.docker_calls if "importlib.util" not in " ".join(c)], (
        "aucun conteneur de vérification"
    )
    assert not clone.exists(), "le clone généré est supprimé même à l'expiration"


def test_the_cli_hands_the_remaining_time_not_a_new_window_to_the_verification(monkeypatch, tmp_path, boundary):
    url = business.campaign_environment("cli", str(tmp_path))["STATE_DATABASE_URL"]
    project_id = spend_in_registry(url)
    clock = ControlledClock()
    monkeypatch.setattr(business.time, "monotonic", clock)
    monkeypatch.setattr(business, "effective_worker_capacity", accepting_capacity)
    fake_launch(monkeypatch, tmp_path, project_id, clock, advance=800.0)
    received = {}

    def verify(checkout, **kwargs):
        received.update(kwargs)
        remaining = kwargs["deadline_monotonic"] - clock()
        received["remaining"] = remaining
        return business.BusinessObservation("passed", {}, {}, [])

    monkeypatch.setattr(business, "verify_business_checkout", verify)

    invoke(monkeypatch, tmp_path, "run")

    assert received["deadline_monotonic"] == 5000.0 + business.CAMPAIGN_BOUNDS.max_seconds
    assert received["remaining"] == pytest.approx(100.0), (
        "il reste 100 s sur les 900 s, pas une nouvelle fenêtre de 120 s"
    )


def test_an_early_sigkill_of_the_verifier_is_incomplete_with_the_registry_read_and_never_a_budget_stop(
    monkeypatch, tmp_path, boundary
):
    """CLI composée : 137 après ~1 s sur un reste de ~890 s n'est ni une expiration ni un budget épuisé."""
    import subprocess as sp

    url = business.campaign_environment("cli", str(tmp_path))["STATE_DATABASE_URL"]
    project_id = spend_in_registry(url)
    clock = ControlledClock()
    monkeypatch.setattr(business.time, "monotonic", clock)
    monkeypatch.setattr(business, "effective_worker_capacity", accepting_capacity)
    fake_launch(monkeypatch, tmp_path, project_id, clock, advance=10.0)

    def docker_path(checkout, *, deadline_monotonic, clock, **kwargs):
        def runner(argv, cwd, env, limit):
            clock.now += 1.0  # le conteneur disparaît après 1 s (tué : 128 + SIGKILL)
            return sp.CompletedProcess(argv, 137, "", "")

        return business._observe(
            checkout,
            runner,
            python="python",
            require_legal_notice=True,
            reference=None,
            timeout=120.0,
            database_dir="/scratch",
            deadline_monotonic=deadline_monotonic,
            clock=clock,
        )

    monkeypatch.setattr(
        business,
        "verify_business_checkout",
        lambda checkout, **kw: docker_path(checkout, **{**kw, "clock": clock}),
    )

    code, report, steps = invoke(monkeypatch, tmp_path, "run")

    assert steps["R02-business"]["state"] == "incomplete_validation"
    assert (
        "interruption précoce" in steps["R02-business"]["detail"]
        and "pas une expiration" in steps["R02-business"]["detail"]
    )
    assert steps["R03-registry"]["state"] == "succeeded"
    assert report["facts"]["registry_final"]["consumed_tokens"] == 750
    assert code == 3 and report["verdict"] == "incomplete_validation" and report["verdict"] != "budget_stop"
