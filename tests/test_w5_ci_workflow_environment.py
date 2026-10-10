"""Environnement RÉELLEMENT produit par le workflow de campagne W5 (propriété C), puis accepté par le VRAI préflight du produit.

Constat C52/C53 : le workflow n'initialisait pas ``AUTO_REVERT_HEALTH_COMMAND`` ; le préflight (P02) refusait donc la campagne avant toute
étape utile, alors que les tests de chaque côté étaient verts. Ces tests REJOUENT les étapes ``run`` du vrai fichier YAML (``bash -e -o pipefail``,
``$GITHUB_ENV`` au format multiligne de GitHub, dossier temporaire, sans clé ni réseau) jusqu'au préflight statique, reconstruisent l'environnement
que verrait la suite, puis :

* le font valider par ``validate_campaign_environment`` ET par ``run_preflight`` (vraies fonctions du produit, routes d'A et capacité du relais ;
  GitHub = faux serveur en lecture seule ; aucun appel de modèle, aucun socket) ;
* vérifient que chaque variable lue par les scripts du workflow est définie quelque part (job, étape ou ``$GITHUB_ENV``), que la clé du fournisseur n'existe
  qu'à l'étape de campagne et que la santé de Phase 5 est EXACTEMENT ``health_command()`` — jamais une commande de repli.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from test_w4_business_report import FixtureNamedServer, full_clients, ok_runner

from collegue.pilot import w4_business as business

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "consolidation-e2e.yml"
SCRIPT = ROOT / "scripts" / "w5_workflow_env.py"
CAMPAIGN_ID = "w5-gemma-probe"
RUN_TAG = "9999-1"
#: Valeurs que GitHub substitue aux expressions ``${{ … }}`` : variable de dépôt non fixée = chaîne vide, comme chez GitHub.
EXPRESSIONS = {
    "inputs.confirm": business.LAUNCH_CONFIRMATION,
    "inputs.campaign_id": CAMPAIGN_ID,
    "vars.INTEGRATION_E2E_ENABLED": "",
    "vars.W5_BOOTSTRAP_MANIFEST_JSON": "{}",
    "secrets.INTEGRATION_FIXTURE_GITHUB_TOKEN": "fake-fixture-read-token",
    "secrets.W5_GOOGLE_API_KEY": "fake-google-key-never-used",
}
#: Variables fournies par GitHub lui-même (ou par le shell) : leur absence du YAML est normale.
PLATFORM = {
    "GITHUB_ENV",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_SHA",
    "GITHUB_EVENT_NAME",
    "RUNNER_TEMP",
    "HOME",
    "PATH",
    "PWD",
}


def resolve(value: str) -> str:
    def substitute(match):
        key = match.group(1).strip()
        assert key in EXPRESSIONS, f"expression GitHub non prévue par le test : {key!r} (la déclarer explicitement)"
        return EXPRESSIONS[key]

    return re.sub(r"\$\{\{\s*([^}]+?)\s*\}\}", substitute, str(value))


def load_github_env(path: Path) -> dict:
    """Lecteur du fichier ``$GITHUB_ENV`` (``NOM=valeur`` et ``NOM<<DÉLIMITEUR`` multiligne), comme le runner."""
    rows, out, i = path.read_text(encoding="utf-8").split("\n"), {}, 0
    while i < len(rows):
        row, i = rows[i], i + 1
        if not row:
            continue
        if "<<" in row.split("=", 1)[0]:
            name, delimiter = row.split("<<", 1)
            value = []
            while i < len(rows) and rows[i] != delimiter:
                value.append(rows[i])
                i += 1
            assert i < len(rows), "délimiteur de $GITHUB_ENV absent"
            i += 1
            out[name] = "\n".join(value)
        else:
            name, value = row.split("=", 1)
            out[name] = value
    return out


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture
def replay(tmp_path, workflow):
    """Rejoue les étapes d'initialisation du vrai workflow (jusqu'au préflight statique) ; rend ``(env, étapes rejouées)``."""
    job = workflow["jobs"]["campaign"]
    env = {k: os.environ[k] for k in ("PATH", "LANG") if k in os.environ}
    env["PATH"] = (
        os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    )  # `python` du workflow = l'interpréteur des tests
    env.update({k: resolve(v) for k, v in job["env"].items()})
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    github_env = tmp_path / "github-env"
    github_env.touch()
    env.update(
        GITHUB_ENV=str(github_env),
        RUNNER_TEMP=str(runner_temp),
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_RUN_ID="999900001",
        GITHUB_RUN_ATTEMPT="1",
        GITHUB_SHA="0" * 40,
        HOME=str(tmp_path),
    )
    replayed = []
    for step in job["steps"]:
        if step.get("id") == "preflight_static":
            break
        script = step.get("run", "")
        if not script or "GITHUB_ENV" not in script:
            continue  # étapes sans effet sur l'environnement (installation, migrations…) : couvertes par la CI ; ici seulement l'environnement
        step_env = {**env, **{k: resolve(v) for k, v in step.get("env", {}).items()}}
        child = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", script],
            env=step_env,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert child.returncode == 0, (step.get("name"), child.stderr)
        env.update(load_github_env(github_env))
        replayed.append(step["name"])
    return env, replayed


def test_the_workflow_exports_the_independent_health_command_from_the_public_api_before_any_preflight(replay, workflow):
    env, replayed = replay
    assert any("santé indépendante" in name for name in replayed), replayed
    assert env["AUTO_REVERT_HEALTH_COMMAND"] == business.health_command(), (
        "EXACTEMENT la commande publique du produit : ni repli, ni valeur dégradée, ni réécriture"
    )
    assert "\n" not in env["AUTO_REVERT_HEALTH_COMMAND"] and env["AUTO_REVERT_HEALTH_COMMAND"].startswith("python -c ")
    assert business.validate_campaign_environment(env) == [], (
        "l'environnement du workflow est accepté par la vérification de l'enveloppe"
    )

    steps = workflow["jobs"]["campaign"]["steps"]
    names = [s.get("name", "") for s in steps]
    export = next(i for i, n in enumerate(names) if "santé indépendante" in n)
    install = next(i for i, n in enumerate(names) if n.startswith("Installer les dépendances"))
    first_preflight = next(i for i, s in enumerate(steps) if s.get("id") == "preflight_static")
    assert install < export < first_preflight, "après l'installation du produit, avant tout préflight"
    assert steps[export]["run"].strip() == 'python scripts/w5_workflow_env.py >> "$GITHUB_ENV"'
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count("AUTO_REVERT_HEALTH_COMMAND") <= 1, (
        "aucune commande de santé écrite en dur dans le YAML (au plus un commentaire)"
    )
    assert "AUTO_REVERT_HEALTH_COMMAND" not in workflow["jobs"]["campaign"]["env"]
    assert "secrets." not in steps[export]["run"] and "env" not in steps[export], "aucun secret dans l'étape d'export"


@pytest.mark.parametrize("stage", ["static", "full"])
def test_the_exact_workflow_environment_is_accepted_by_the_real_preflight_without_any_model_call(
    replay, monkeypatch, tmp_path, stage
):
    env, _ = replay
    # variables propres aux étapes de préflight du workflow (jeton de LECTURE factice ; AUCUNE clé de modèle à ce stade)
    step_env = {
        k: resolve(v)
        for k, v in next(
            s
            for s in yaml.safe_load(WORKFLOW.read_text())["jobs"]["campaign"]["steps"]
            if s.get("id") == "preflight_static"
        )["env"].items()
    }
    env = {**env, **step_env}
    assert "LLM_API_KEY" not in env, "la clé n'existe pas avant l'étape de campagne"

    monkeypatch.chdir(tmp_path)  # aucun .env
    emitted = []

    async def forbidden(*args, **kwargs):
        emitted.append(kwargs)
        raise AssertionError("appel LLM émis pendant le préflight")

    def no_network(*args, **kwargs):
        raise AssertionError("connexion sortante pendant le préflight")

    monkeypatch.setattr("collegue.core.llm.client.sample_with_timeout", forbidden)
    monkeypatch.setattr("collegue.core.llm.budget_guard.guarded_call", forbidden)
    monkeypatch.setattr(socket.socket, "connect", no_network)

    server = FixtureNamedServer()
    server.add_ruleset(1)
    report = business.run_preflight(
        env,
        clients=full_clients(server),
        campaign_id=CAMPAIGN_ID,
        run_tag=RUN_TAG,
        image_runner=ok_runner,
        stage=stage,
    )
    states = {s.id: s.state for s in report.steps}
    expected = {"succeeded"} if stage == "full" else {"succeeded", "not_executed"}
    assert set(states.values()) <= expected, [(s.id, s.state, s.detail) for s in report.steps]
    assert states["P02-environment"] == states["P05-role-routes"] == states["P06-worker-capacity"] == "succeeded"
    if stage == "static":
        assert states["P08-oracle-environment"] == "not_executed", "l'étape statique ne fait pas intervenir l'image"
    assert report.step("P06-worker-capacity").evidence["effective"]["source"] == "collegue.broker.capability_proof"
    assert report.verdict() == "validated" and report.exit_code() == 0
    assert emitted == [] and report.facts["llm_calls_emitted"] == report.facts["billable_actions_emitted"] == 0
    assert all(call[0] == "GET" for call in server.calls), "lectures seules côté GitHub"
    shown = json.dumps(report.to_machine()) + report.to_human()
    assert "fake-fixture-read-token" not in shown, "aucun jeton dans le rapport"


def test_a_workflow_without_the_export_is_still_refused_by_p02_with_the_precise_reason(replay, monkeypatch, tmp_path):
    """Témoin négatif : sans la commande de santé, le vrai préflight REFUSE (aucun repli, aucune dégradation)."""
    env, _ = replay
    env = {k: v for k, v in env.items() if k != "AUTO_REVERT_HEALTH_COMMAND"}
    monkeypatch.chdir(tmp_path)
    server = FixtureNamedServer()
    server.add_ruleset(1)
    report = business.run_preflight(
        env,
        clients=full_clients(server),
        campaign_id=CAMPAIGN_ID,
        run_tag=RUN_TAG,
        image_runner=ok_runner,
        stage="static",
    )
    assert report.step("P02-environment").state == "incomplete_validation"
    assert "AUTO_REVERT_HEALTH_COMMAND" in report.step("P02-environment").detail
    assert report.exit_code() == 3 and server.calls == [], "refus avant toute lecture GitHub"


def test_every_variable_read_by_the_workflow_scripts_is_defined_by_the_job_a_step_or_github_env(replay, workflow):
    env, _ = replay
    job = workflow["jobs"]["campaign"]
    for step in job["steps"]:
        script = step.get("run", "")
        defined = set(env) | set(step.get("env", {})) | PLATFORM
        for name in set(re.findall(r"\$\{?([A-Z][A-Z0-9_]+)\}?", script)):
            assert name in defined, f"{step.get('name')}: ${name} lu mais défini nulle part (job, étape ou $GITHUB_ENV)"
    # chemins de la suite : tous sous le dossier éphémère du runner, jamais dans le dépôt ni dans un cache utilisateur
    root = Path(env["RUNNER_TEMP"]) / "w5-business"
    for name in (
        "COLLEGUE_HOME",
        "SANDBOX_PIP_CACHE_DIR",
        "W4_REPORT_DIR",
        "W5_BOOTSTRAP_MANIFEST",
        "W5_PUBLISH",
        "W5_DIAG",
        "W5_QUARANTINE",
    ):
        assert Path(env[name]).is_relative_to(root), name
    assert env["STATE_DATABASE_URL"] == f"sqlite:///{root}/home/{CAMPAIGN_ID}.sqlite3"
    assert Path(env["W5_BOOTSTRAP_MANIFEST"]).read_text(encoding="utf-8") == "{}", (
        "le manifeste vient de la variable de dépôt, tel quel"
    )


def test_the_model_key_is_absent_from_the_derived_environment_and_exists_only_in_the_campaign_step(replay, workflow):
    env, _ = replay
    assert not [n for n in env if re.search(r"API_KEY|SECRET|PASSWORD|(^|_)TOKEN$", n)], sorted(env)
    holders = [s["name"] for s in workflow["jobs"]["campaign"]["steps"] if "LLM_API_KEY" in s.get("env", {})]
    assert holders == ["Campagne réelle (SEULE étape qui reçoit la clé du fournisseur de modèle)"]


# ── le script de confiance lui-même ───────────────────────────────────────────────────────────────────────────────────────


def test_the_script_writes_a_github_env_block_that_round_trips_and_nothing_else(tmp_path):
    out = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert out.returncode == 0, out.stderr
    target = tmp_path / "env"
    target.write_text(out.stdout, encoding="utf-8")
    assert load_github_env(target) == {"AUTO_REVERT_HEALTH_COMMAND": business.health_command()}
    assert out.stderr == "" and "KEY" not in out.stdout.split("<<")[0]


def test_a_multiline_value_round_trips_through_the_github_env_format(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("w5_workflow_env_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = "ligne 1\nNOM=pas une variable\nW5_EOF_x\nligne 4"
    target = tmp_path / "env"
    target.write_text(module.github_env_lines({"MULTI": value, "AUTRE": "simple"}), encoding="utf-8")
    assert load_github_env(target) == {"MULTI": value, "AUTRE": "simple"}


@pytest.mark.parametrize("bad", ["", "   ", "a\rb"], ids=["empty", "blank", "carriage-return"])
def test_the_script_refuses_an_unexportable_value_and_writes_nothing(bad, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("w5_workflow_env_refusal", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main([], derive=lambda: {"AUTO_REVERT_HEALTH_COMMAND": bad}) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "environnement dérivé refusé" in captured.err


def test_an_unimportable_product_fails_the_step_instead_of_exporting_a_fallback(capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("w5_workflow_env_broken", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def broken():
        raise ImportError("collegue absent")

    assert module.main([], derive=broken) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "environnement dérivé impossible" in captured.err
