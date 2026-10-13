"""Workflow de la paire JSON W5 : déclenchement unique, secrets confinés, rapports toujours tentés, aucun rerun (propriété C).

Deux familles de contrôles, tous hors ligne et sans fournisseur :

* le fichier YAML (structure, ordre, confinement de la clé et du jeton de la fixture, artefacts) ;
* l'exécution RÉELLE des scripts bash des étapes, avec le CLI de la paire (propriété A) et ``gh`` remplacés par des doublures et le VRAI scanner
  de confiance (``scripts/w5_leak_scan.py``) : codes de sortie, ensemble publié, absence de fuite.

Le CLI lui-même (``scripts/w5_json_pair.py``) n'est pas testé ici : il n'est intégré qu'après ordre du manager. Les contrôles qui le lisent sont
marqués ignorés tant qu'il est absent, jamais comptés comme réussis.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
PAIR = WORKFLOWS / "w5-json-pair.yml"
CLI = ROOT / "scripts" / "w5_json_pair.py"
CONFIRMATION = "LANCER-UNE-PAIRE-JSON-2GENERATIONS-2USD-250000TOKENS-900S"
PAIR_STEP = "Paire réelle (SEULE étape qui reçoit la clé du fournisseur de modèle)"
KEY_SECRET = "W5_JSON_PAIR_GOOGLE_API_KEY"
FIXTURE_SECRET = "INTEGRATION_FIXTURE_GITHUB_TOKEN"
SHA = "a" * 40
CAMPAIGN = "w5-json-pair-001"


@pytest.fixture(scope="module")
def raw() -> str:
    return PAIR.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def code(raw) -> str:
    """Le YAML sans ses commentaires : les contrôles de texte ne lisent pas la documentation."""
    return "\n".join(line for line in raw.splitlines() if not line.lstrip().startswith("#"))


@pytest.fixture(scope="module")
def wf(raw) -> dict:
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict)
    return parsed


@pytest.fixture(scope="module")
def job(wf) -> dict:
    assert list(wf["jobs"]) == ["pair"], "un seul job : aucune image, aucun partage d'artefact entre jobs"
    return wf["jobs"]["pair"]


def step_named(job: dict, prefix: str) -> dict:
    for step in job["steps"]:
        if step.get("name", "").startswith(prefix):
            return step
    raise AssertionError(f"étape introuvable: {prefix}")


def index_of(job: dict, prefix: str) -> int:
    return job["steps"].index(step_named(job, prefix))


# ── déclenchement : un dispatch confirmé, jamais un événement de dépôt ──────────────────────────────────────────────────────────


def test_only_a_confirmed_manual_dispatch_can_start_it_and_never_a_repository_event(wf, job, raw):
    trigger = wf.get("on", wf.get(True))
    assert list(trigger) == ["workflow_dispatch"], (
        "aucun push, pull_request, schedule, workflow_run ni repository_dispatch"
    )
    inputs = trigger["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"confirm", "campaign_id", "expected_sha"}
    assert all(spec["required"] is True and spec["type"] == "string" for spec in inputs.values())
    assert "default" not in inputs["campaign_id"], (
        "aucun identifiant par défaut : un identifiant consommé ne se réutilise pas"
    )
    assert CONFIRMATION in inputs["confirm"]["description"]
    assert job["if"] == f"${{{{ github.event_name == 'workflow_dispatch' && inputs.confirm == '{CONFIRMATION}' }}}}"


def test_no_rerun_or_automation_surface_exists(wf, code):
    for forbidden in (
        "schedule:",
        "workflow_run",
        "repository_dispatch",
        "pull_request",
        "gh run rerun",
        "gh workflow run",
        "continue-on-error",
        "retry",
        "--admin",
        "gh api -X",
        "--method",
    ):
        assert forbidden not in code, forbidden
    assert wf["permissions"] == {"contents": "read"}, (
        "aucun droit d'écriture ni d'administration (pas d'auto-suppression de l'environnement)"
    )
    assert wf["concurrency"] == {"group": "w5-json-pair", "cancel-in-progress": "false"} or wf["concurrency"] == {
        "group": "w5-json-pair",
        "cancel-in-progress": False,
    }, "une seule paire active, jamais annulée en cours d'émission"


def test_it_has_its_own_environment_and_does_not_borrow_another_campaign(job, code):
    assert job["environment"] == "w5-json-pair"
    for foreign in (
        "w5-gemma-campaign",
        "W5_GOOGLE_API_KEY",
        "INTEGRATION_LLM_API_KEY",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "W5_BOOTSTRAP_MANIFEST",
        "W4_BUSINESS_",
    ):
        assert foreign not in code, foreign
    assert not re.search(r"(?<!JSON_PAIR_)GOOGLE_API_KEY", code), (
        "GOOGLE_API_KEY est ignoré par le produit : le nom lu est LLM_API_KEY"
    )
    assert job["env"]["INTEGRATION_E2E_ENABLED"] == "${{ vars.INTEGRATION_E2E_ENABLED }}"
    assert 'INTEGRATION_E2E_ENABLED: "true"' not in code


def test_nothing_heavy_or_business_is_in_this_workflow(job, code):
    for forbidden in (
        "docker",
        "Dockerfile",
        "OpenHands",
        "openhands",
        "SANDBOX_",
        "w4_business",
        "w5_fixture_bootstrap",
        "w5_product_preflight",
        "BUILD_AUTO_MERGE",
        "AUTO_MERGE",
        "CODER_FALLBACK",
        "gemma-4-26b",
        "generativelanguage",
    ):
        assert forbidden not in code, forbidden
    assert job["timeout-minutes"] == "35" or job["timeout-minutes"] == 35


# ── contexte réel, SHA attendu, tentative 1 ────────────────────────────────────────────────────────────────────────────────────


def test_launch_context_values_are_read_from_the_runner_never_fabricated(job, code):
    for name in (
        "GITHUB_EVENT_NAME",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_SHA",
        "GITHUB_REF",
        "GITHUB_RUN_ID",
        "GITHUB_REPOSITORY",
    ):
        assert not re.search(rf"^\s*{name}:", code, re.M), f"{name} ne se fixe pas dans le YAML"
        assert f"printf '{name}=" not in code, f"{name} ne s'exporte pas vers GITHUB_ENV"
    context = step_named(job, "Contexte de lancement réel")["run"]
    for check in (
        '"$GITHUB_EVENT_NAME" != "workflow_dispatch"',
        '"$GITHUB_REF" != "refs/heads/main"',
        '"$GITHUB_SHA" != "$W5_JSON_PAIR_EXPECTED_SHA"',
        '"$GITHUB_RUN_ATTEMPT" != "1"',
    ):
        assert check in context, check
    assert "^[0-9a-f]{40}$" in context and "^[a-z0-9][a-z0-9-]{2,39}$" in context
    assert job["env"]["W5_JSON_PAIR_EXPECTED_SHA"] == "${{ inputs.expected_sha }}"


def test_main_must_still_be_the_validated_commit_with_a_read_only_ephemeral_token(job):
    tip = step_named(job, "Pointe de main inchangée")
    assert tip["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert "repos/$GITHUB_REPOSITORY/commits/main" in tip["run"] and "--jq .sha" in tip["run"]
    assert "exit 1" in tip["run"] and "|| true" not in tip["run"], "une lecture en erreur n'est jamais « inchangé »"
    assert (
        index_of(job, "Contexte de lancement réel")
        < index_of(job, "Pointe de main inchangée")
        < index_of(job, "Préflight")
    )


# ── confinement de la clé et du jeton de la fixture ───────────────────────────────────────────────────────────────────────────


def test_the_provider_key_exists_only_in_the_pair_step_under_one_name(job, raw, code):
    holders = [s["name"] for s in job["steps"] if any(re.search(r"API_KEY", k) for k in s.get("env", {}))]
    assert holders == [PAIR_STEP]
    pair = step_named(job, "Paire réelle")
    assert pair["env"] == {"LLM_API_KEY": f"${{{{ secrets.{KEY_SECRET} }}}}"}, (
        "ni jeton de fixture ni autre variable dans cette étape"
    )
    assert code.count(f"secrets.{KEY_SECRET}") == 1
    assert not any("secrets." in str(v) for v in job["env"].values()), "aucun secret au niveau du job"
    for step in job["steps"]:
        assert "secrets." not in step.get("run", ""), "les secrets passent par env:, jamais par le script"


def test_the_fixture_token_exists_only_in_the_claim_step_and_never_reaches_the_google_step(job, code):
    holders = [s["name"] for s in job["steps"] if "GITHUB_TOKEN" in s.get("env", {})]
    assert len(holders) == 1 and holders[0].startswith("Revendiquer l'identité")
    claim = step_named(job, "Revendiquer l'identité")
    assert claim["env"] == {"GITHUB_TOKEN": f"${{{{ secrets.{FIXTURE_SECRET} }}}}"}
    assert code.count(f"secrets.{FIXTURE_SECRET}") == 1
    assert "GITHUB_TOKEN" not in step_named(job, "Paire réelle")["env"]
    assert "GITHUB_TOKEN" not in job["env"]
    secrets_used = sorted(set(re.findall(r"secrets\.([A-Z0-9_]+)", code)))
    assert secrets_used == sorted([FIXTURE_SECRET, KEY_SECRET])
    assert "secrets.GITHUB_TOKEN" not in code


def test_preflight_and_finalization_have_no_secret_at_all(job):
    preflight = step_named(job, "Préflight")
    assert "env" not in preflight and "secrets." not in str(preflight) and "github.token" not in str(preflight)
    assert (
        "preflight --root" in preflight["run"]
        and "--expected-sha" in preflight["run"]
        and "--confirm" in preflight["run"]
    )
    fallback = step_named(job, "Rapports sans clé")
    assert "env" not in fallback and "secrets." not in str(fallback)
    for name in ("Installer", "Initialiser le registre", "Diagnostic minimal"):
        step = step_named(job, name)
        assert not any(re.search(r"KEY|TOKEN|SECRET", k) for k in step.get("env", {})), name


def test_claim_precedes_the_key_and_the_pair_requires_every_earlier_step(job):
    names = [s.get("name", "") for s in job["steps"]]
    order = [
        "Contexte",
        "Pointe de main",
        "Set up Python",
        "Installer",
        "Préflight",
        "Revendiquer",
        "Initialiser le registre",
        "Paire réelle",
    ]
    positions = [next(i for i, n in enumerate(names) if n.startswith(prefix)) for prefix in order]
    assert positions == sorted(positions)
    assert step_named(job, "Revendiquer")["if"] == "steps.preflight.outcome == 'success'"
    assert step_named(job, "Initialiser le registre")["if"] == "steps.claim.outcome == 'success'"
    assert (
        step_named(job, "Paire réelle")["if"]
        == "steps.claim.outcome == 'success' && steps.migrate.outcome == 'success'"
    )
    assert step_named(job, "Initialiser le registre")["run"].strip() == "python -m collegue.migrations upgrade"
    assert "claim --root" in step_named(job, "Revendiquer")["run"]


def test_the_pair_step_runs_once_bounded_then_finalizes_then_scans_in_the_same_step(job):
    pair = step_named(job, "Paire réelle")
    script = pair["run"]
    assert pair["timeout-minutes"] in (25, "25")
    assert "timeout --signal=TERM --kill-after=30 960 python scripts/w5_json_pair.py run" in script
    assert script.count("w5_json_pair.py run") == 1 and script.count("w5_json_pair.py finalize") == 1
    assert "env -u LLM_API_KEY python scripts/w5_json_pair.py finalize" in script, (
        "la finalisation tourne dans un processus SANS clé"
    )
    assert "-u LLM_API_KEY" not in script.split("w5_json_pair.py run")[0], "seul le conducteur reçoit la clé"
    assert (
        script.index("w5_json_pair.py run")
        < script.index("w5_json_pair.py finalize")
        < script.index("scripts/w5_leak_scan.py")
    )
    assert "--env LLM_API_KEY" in script and "--assume-no-key" not in script
    assert 'if [ "$scan" -ne 0 ]; then status=1; fi' in script and 'exit "$status"' in script
    assert "claim" not in script and "preflight" not in script
    sources = re.findall(r'--source "([^"]+)"', script)
    assert sources == ["report=$W5_JSON_PAIR_ROOT/report", "registry=$COLLEGUE_HOME::*.sqlite3*"], (
        "ni proof/, ni diagnostic/, ni le répertoire racine : seulement les rapports et le registre"
    )


def test_a_pair_that_did_not_start_proves_the_key_never_existed_and_never_searches_for_one(job):
    fallback = step_named(job, "Rapports sans clé")
    assert fallback["if"] == "always() && steps.context.outcome == 'success' && steps.pair.outcome == 'skipped'"
    assert "--assume-no-key" in fallback["run"] and "--env" not in fallback["run"]
    assert "w5_json_pair.py finalize" in fallback["run"] and "w5_json_pair.py run" not in fallback["run"]
    assert index_of(job, "Paire réelle") < index_of(job, "Rapports sans clé")


def test_every_upload_is_always_attempted_and_only_from_the_scanned_set(job):
    uploads = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@v4")]
    assert [u["with"]["name"] for u in uploads] == [
        "w5-json-pair-report",
        "w5-json-pair-registry",
        "w5-json-pair-diagnostic",
    ]
    assert [u["with"]["path"] for u in uploads] == [
        "${{ env.W5_PUBLISH }}/report",
        "${{ env.W5_PUBLISH }}/registry",
        "${{ env.W5_DIAG }}",
    ]
    for upload in uploads:
        assert upload["if"] == "always()" and upload["with"]["if-no-files-found"] == "warn"
        assert "W5_JSON_PAIR_ROOT" not in upload["with"]["path"] and "COLLEGUE_HOME" not in upload["with"]["path"]
    assert index_of(job, "Diagnostic minimal") < job["steps"].index(uploads[0])


def test_the_minimal_diagnostic_is_built_from_step_outcomes_and_validated_ids_only(job):
    step = step_named(job, "Diagnostic minimal")
    assert step["if"] == "always()"
    assert set(step["env"]) == {
        f"OUTCOME_{n}" for n in ("CONTEXT", "MAIN_TIP", "PREFLIGHT", "CLAIM", "MIGRATE", "PAIR")
    }
    assert all(re.fullmatch(r"\$\{\{ steps\.[a-z_]+\.outcome \}\}", v) for v in step["env"].values())
    assert 'if [ "$OUTCOME_CONTEXT" = "success" ]' in step["run"], (
        "l'identifiant saisi n'est recopié qu'une fois validé"
    )
    for dynamic in ("cat ", "<", "$(", "`"):
        assert dynamic not in step["run"].replace('"$(', '"').replace("${W5_DIAG:-$RUNNER_TEMP", ""), (
            f"jamais dérivé d'un fichier : {dynamic!r}"
        )
    assert "GITHUB_RUN_ATTEMPT" in step["run"] and "GITHUB_RUN_ID" in step["run"] and "GITHUB_SHA" in step["run"]


def test_the_other_workflows_never_reference_the_pair_key_or_environment():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        if path == PAIR:
            continue
        text = path.read_text(encoding="utf-8")
        assert KEY_SECRET not in text and "w5-json-pair" not in text, path.name
    text = (WORKFLOWS / "tests.yml").read_text(encoding="utf-8")
    assert "secrets." not in text.replace("secrets.GITHUB_TOKEN", "")


def test_the_general_ci_does_not_run_the_pair_and_keeps_its_five_check_names():
    parsed = yaml.safe_load((WORKFLOWS / "tests.yml").read_text(encoding="utf-8"))
    assert sorted(j["name"] for j in parsed["jobs"].values()) == [
        "Dependency audit",
        "Docker build",
        "Pytest (Python ${{ matrix.python-version }})",
        "Ruff",
    ]


# ── contrat avec le CLI de A (lu seulement s'il est intégré) ─────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not CLI.exists(),
    reason="scripts/w5_json_pair.py (lot A) n'est pas encore intégré : contrôle non joué, non compté comme réussi",
)
def test_the_cli_exposes_the_four_steps_and_the_same_confirmation_phrase(job):
    source = CLI.read_text(encoding="utf-8")
    for command in ("preflight", "claim", "run", "finalize"):
        assert f'"{command}"' in source or f"'{command}'" in source, command
    assert CONFIRMATION in source, "la phrase du workflow est celle du CLI"
    for flag in ("--root", "--campaign-id", "--expected-sha", "--confirm"):
        assert flag in source, flag


# ── exécution RÉELLE des scripts des étapes (CLI et gh remplacés, vrai scanner de confiance) ────────────────────────────────────


def _bash(script: str, env: dict, *, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-eo", "pipefail", "-c", script], cwd=cwd, env=env, capture_output=True, text=True, timeout=90
    )


def _launch_env(tmp_path: Path, **override) -> dict:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "GITHUB_ENV": str(tmp_path / "github_env"),
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": SHA,
        "GITHUB_RUN_ID": "4242",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_REPOSITORY": "VynoDePal/Collegue",
        "W5_JSON_PAIR_CAMPAIGN_ID": CAMPAIGN,
        "W5_JSON_PAIR_EXPECTED_SHA": SHA,
        "W5_JSON_PAIR_CONFIRM": CONFIRMATION,
    }
    env.update(override)
    (tmp_path / "runner").mkdir(exist_ok=True)
    return env


def _github_env(tmp_path: Path) -> dict:
    path = tmp_path / "github_env"
    pairs = [line.split("=", 1) for line in path.read_text().splitlines()] if path.exists() else []
    return {k: v for k, v in pairs}


def test_the_context_step_accepts_only_the_real_dispatch_on_main_at_the_expected_sha_on_the_first_attempt(
    job, tmp_path
):
    step = step_named(job, "Contexte de lancement réel")["run"]
    done = _bash(step, _launch_env(tmp_path))
    assert done.returncode == 0, done.stderr
    exported = _github_env(tmp_path)
    root = tmp_path / "runner" / "w5-json-pair"
    assert exported["W5_JSON_PAIR_ROOT"] == str(root) and exported["COLLEGUE_HOME"] == f"{root}/home"
    assert exported["STATE_DATABASE_URL"] == f"sqlite:///{root}/home/{CAMPAIGN}.sqlite3"
    assert exported["W5_PUBLISH"] == f"{root}/publish" and exported["W5_DIAG"] == f"{root}/diagnostic"
    assert not any(name.startswith("GITHUB_") for name in exported), "aucune valeur de contexte n'est réécrite"
    for folder in ("home", "proof", "report", "diagnostic"):
        assert (root / folder).is_dir() and (root / folder).stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "override",
    [
        {"GITHUB_EVENT_NAME": "push"},
        {"GITHUB_EVENT_NAME": "schedule"},
        {"GITHUB_REF": "refs/heads/codex/w5-json-pair-c"},
        {"GITHUB_SHA": "b" * 40},
        {"GITHUB_RUN_ATTEMPT": "2"},
        {"GITHUB_RUN_ATTEMPT": ""},
        {"W5_JSON_PAIR_EXPECTED_SHA": "A" * 40, "GITHUB_SHA": "A" * 40},
        {"W5_JSON_PAIR_EXPECTED_SHA": "abc123", "GITHUB_SHA": "abc123"},
        {"W5_JSON_PAIR_CAMPAIGN_ID": "Bad_ID"},
        {"W5_JSON_PAIR_CAMPAIGN_ID": "ab"},
        {"W5_JSON_PAIR_CAMPAIGN_ID": "x" * 41},
        {"W5_JSON_PAIR_CAMPAIGN_ID": 'a"b\n","x":"'},
    ],
)
def test_the_context_step_refuses_anything_else_but_still_exports_the_paths_for_the_report_steps(
    job, tmp_path, override
):
    done = _bash(step_named(job, "Contexte de lancement réel")["run"], _launch_env(tmp_path, **override))
    assert done.returncode == 1, done.stdout + done.stderr
    exported = _github_env(tmp_path)
    assert {"W5_JSON_PAIR_ROOT", "W5_PUBLISH", "W5_DIAG", "W5_QUARANTINE"} <= set(exported)
    assert "STATE_DATABASE_URL" not in exported, "aucun registre n'est désigné pour un contexte refusé"


def _fake_gh(tmp_path: Path, *, tip: str, rc: int = 0) -> dict:
    bindir = tmp_path / "ghbin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(
        f'#!/bin/bash\necho "$@" > "{tmp_path}/gh_args"\nprintf \'%s\' "{tip}"\nexit {rc}\n', encoding="utf-8"
    )
    gh.chmod(0o755)
    return {"PATH": f"{bindir}:{os.environ['PATH']}", "GH_TOKEN": "ephemeral-run-token-not-a-key"}


@pytest.mark.parametrize(
    ("tip", "gh_rc", "expected"),
    [(SHA, 0, 0), ("b" * 40, 0, 1), ("", 0, 1), ("not-a-sha", 0, 1), (SHA, 1, 1)],
)
def test_main_tip_step_fails_on_any_drift_or_unreadable_state(job, tmp_path, tip, gh_rc, expected):
    env = _launch_env(tmp_path, **_fake_gh(tmp_path, tip=tip, rc=gh_rc))
    done = _bash(step_named(job, "Pointe de main inchangée")["run"], env)
    assert done.returncode == expected, done.stdout + done.stderr
    assert (tmp_path / "gh_args").read_text().split()[:2] == ["api", "repos/VynoDePal/Collegue/commits/main"]


KEY = "FAKE-" + "pair-key-0123456789abcdef"


def _pair_env(tmp_path: Path, **override) -> tuple[dict, Path]:
    """Environnement de l'étape de paire + doublures de ``python scripts/w5_json_pair.py`` (A) et de ``timeout``."""
    root = tmp_path / "w5-json-pair"
    for folder in ("home", "proof", "report", "diagnostic"):
        (root / folder).mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "python"
    shim.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "scripts/w5_json_pair.py" ]; then\n'
        '  echo "$2 GITHUB_TOKEN=${GITHUB_TOKEN:-absent} KEY=${LLM_API_KEY:+present}" >> "$FAKE_LOG"\n'
        '  case "$2" in\n'
        "    run)\n"
        '      if [ "$FAKE_RUN_WRITES" = "1" ]; then\n'
        '        printf "registre" > "$COLLEGUE_HOME/$W5_JSON_PAIR_CAMPAIGN_ID.sqlite3"; printf "workspace" > "$COLLEGUE_HOME/workspace.txt"\n'
        '        echo "{}" > "$W5_JSON_PAIR_ROOT/report/json-pair.json"\n'
        "      fi\n"
        '      if [ -n "$FAKE_LEAK_INTO" ]; then printf "xx%s\\0yy" "$LLM_API_KEY" > "$FAKE_LEAK_INTO"; fi\n'
        '      if [ -n "$FAKE_NAME_LEAK" ]; then echo x > "$W5_JSON_PAIR_ROOT/report/$LLM_API_KEY.log"; fi\n'
        '      exit "$FAKE_RUN_RC";;\n'
        "    finalize)\n"
        '      echo "{\\"status\\": \\"fake\\"}" > "$W5_JSON_PAIR_ROOT/report/json-pair.json"; echo rapport > "$W5_JSON_PAIR_ROOT/report/json-pair.txt"\n'
        '      echo "{}" > "$W5_JSON_PAIR_ROOT/report/provenance.json"\n'
        '      exit "$FAKE_FINAL_RC";;\n'
        "  esac\n"
        "fi\n"
        f'exec {sys.executable} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    wrapper = bindir / "timeout"
    wrapper.write_text('#!/bin/bash\necho "$@" > "$FAKE_TIMEOUT_ARGS"\nshift 3\nexec "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "W5_JSON_PAIR_ROOT": str(root),
        "W5_JSON_PAIR_CAMPAIGN_ID": CAMPAIGN,
        "COLLEGUE_HOME": str(root / "home"),
        "W5_PUBLISH": str(root / "publish"),
        "W5_DIAG": str(root / "diagnostic"),
        "W5_QUARANTINE": str(root / "quarantine"),
        "LLM_API_KEY": KEY,
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "FAKE_TIMEOUT_ARGS": str(tmp_path / "timeout_args"),
        "FAKE_RUN_RC": "0",
        "FAKE_FINAL_RC": "0",
        "FAKE_RUN_WRITES": "1",
        "FAKE_LEAK_INTO": "",
        "FAKE_NAME_LEAK": "",
    }
    env.update(override)
    return env, root


def _published(root: Path) -> list[str]:
    publish = root / "publish"
    return sorted(str(p.relative_to(publish)) for p in publish.rglob("*") if p.is_file()) if publish.exists() else []


SANE = ["registry/w5-json-pair-001.sqlite3", "report/json-pair.json", "report/json-pair.txt", "report/provenance.json"]


@pytest.mark.parametrize(
    ("run_rc", "final_rc", "expected"),
    [
        (0, 0, 0),  # observations obtenues
        (1, 1, 1),  # échec observé
        (3, 3, 3),  # incomplet
        (124, 3, 3),  # timeout du conducteur : le rapport relu dit « incomplet »
        (137, 3, 3),
        (143, 0, 3),  # tué mais rapport « propre » : désaccord = incomplet
        (0, 3, 3),  # désaccord
        (3, 0, 3),
        (0, 1, 3),
        (7, 7, 3),  # code inattendu des deux côtés
        (0, 9, 3),  # la finalisation elle-même plante
    ],
)
def test_the_pair_step_always_finalizes_and_turns_any_disagreement_or_unexpected_code_into_incomplete(
    job, tmp_path, run_rc, final_rc, expected
):
    env, root = _pair_env(tmp_path, FAKE_RUN_RC=str(run_rc), FAKE_FINAL_RC=str(final_rc))
    done = _bash(step_named(job, "Paire réelle")["run"], env)
    assert done.returncode == expected, done.stdout + done.stderr
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert [c.split()[0] for c in calls] == ["run", "finalize"], "la finalisation suit toujours, même après un échec"
    assert all("GITHUB_TOKEN=absent" in c for c in calls), "le jeton de la fixture n'existe pas dans l'étape Google"
    assert "KEY=present" in calls[0] and "KEY=present" not in calls[1], (
        "run reçoit la clé, finalize non (qui précède le scan)"
    )
    assert _published(root) == SANE, "rapports publiés même sur échec ; l'espace de travail n'est pas dans le registre"
    assert json.loads((root / "publish" / "report" / "json-pair.json").read_text()) == {"status": "fake"}, (
        "le fichier publié est celui que finalize a écrit EN DERNIER, puis le scanner a lu : jamais celui de run"
    )
    assert (root / "diagnostic" / "leak-scan.json").exists()
    everything = done.stdout + done.stderr + (root / "diagnostic" / "leak-scan.json").read_text()
    assert KEY not in everything, "la valeur de la clé n'est jamais affichée ni écrite"
    assert (tmp_path / "timeout_args").read_text().split()[:3] == ["--signal=TERM", "--kill-after=30", "960"]


def test_a_run_that_leaves_no_registry_still_yields_reports_and_the_exit_code_of_the_report(job, tmp_path):
    env, root = _pair_env(tmp_path, FAKE_RUN_WRITES="0", FAKE_RUN_RC="3", FAKE_FINAL_RC="3")
    done = _bash(step_named(job, "Paire réelle")["run"], env)
    assert done.returncode == 3, done.stdout + done.stderr
    assert _published(root) == ["report/json-pair.json", "report/json-pair.txt", "report/provenance.json"]


@pytest.mark.parametrize(
    ("leak_target", "name_leak", "published"),
    [
        ("report/extra.log", False, SANE),
        (
            "home/w5-json-pair-001.sqlite3",
            False,
            ["report/json-pair.json", "report/json-pair.txt", "report/provenance.json"],
        ),
        ("", True, SANE),
    ],
)
def test_a_key_in_a_report_the_binary_registry_or_a_file_name_turns_the_run_red_and_is_never_published(
    job, tmp_path, leak_target, name_leak, published
):
    # la fuite est écrite par ``run`` dans un fichier que ``finalize`` ne réécrit pas ; le scanner juge ce qui reste
    env, root = _pair_env(tmp_path, FAKE_NAME_LEAK="1" if name_leak else "")
    if leak_target:
        env["FAKE_LEAK_INTO"] = str(root / leak_target)
    done = _bash(step_named(job, "Paire réelle")["run"], env)
    assert done.returncode == 1, done.stdout + done.stderr
    names = _published(root)
    assert not any(KEY in n for n in names)
    assert names == published
    scan = json.loads((root / "diagnostic" / "leak-scan.json").read_text())
    assert scan["verdict"] == "leak"
    assert KEY not in done.stdout + done.stderr + (root / "diagnostic" / "leak-scan.json").read_text()


def test_a_crashing_scanner_never_publishes_an_unverified_file_and_the_step_is_red(job, tmp_path):
    env, root = _pair_env(tmp_path)
    shim = Path(env["PATH"].split(":")[0]) / "python"
    text = shim.read_text().replace(
        'if [ "$1" = "scripts/w5_json_pair.py" ]; then',
        'if [ "$1" = "scripts/w5_leak_scan.py" ]; then kill -9 $$; fi\nif [ "$1" = "scripts/w5_json_pair.py" ]; then',
        1,
    )
    shim.write_text(text)
    done = _bash(step_named(job, "Paire réelle")["run"], env)
    assert done.returncode != 0 and _published(root) == []


def test_a_missing_or_too_short_key_makes_the_scan_incomplete_and_the_step_red(job, tmp_path):
    env, root = _pair_env(tmp_path, LLM_API_KEY="short")
    done = _bash(step_named(job, "Paire réelle")["run"], env)
    assert done.returncode == 1 and _published(root) == [], done.stdout + done.stderr


@pytest.mark.parametrize("final_rc", [0, 1, 3, 9])
def test_the_fallback_step_builds_the_published_set_without_a_key_whatever_finalize_returns(job, tmp_path, final_rc):
    env, root = _pair_env(tmp_path, FAKE_FINAL_RC=str(final_rc))
    env.pop("LLM_API_KEY")
    (root / "home" / "w5-json-pair-001.sqlite3").write_text("registre")
    done = _bash(step_named(job, "Rapports sans clé")["run"], env)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _published(root) == SANE
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert [c.split()[0] for c in calls] == ["finalize"] and "KEY=present" not in calls[0]


def test_the_fallback_step_fails_when_the_scan_cannot_run(job, tmp_path):
    env, root = _pair_env(tmp_path)
    env.pop("LLM_API_KEY")
    (root / "report").rmdir()
    shim = Path(env["PATH"].split(":")[0]) / "python"
    shim.write_text(shim.read_text().replace('echo "{\\"status\\": \\"fake\\"}" >', ": #"), encoding="utf-8")
    done = _bash(step_named(job, "Rapports sans clé")["run"], env)
    assert done.returncode != 0 and _published(root) == []


def _diagnostic(job, tmp_path, **outcomes):
    diag = tmp_path / "diag"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "W5_DIAG": str(diag),
        "GITHUB_RUN_ID": "4242",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_SHA": SHA,
        "W5_JSON_PAIR_CAMPAIGN_ID": outcomes.pop("campaign", CAMPAIGN),
        "W5_JSON_PAIR_EXPECTED_SHA": SHA,
        **{f"OUTCOME_{k.upper()}": v for k, v in outcomes.items()},
    }
    done = _bash(step_named(job, "Diagnostic minimal")["run"], env)
    return done, diag


ALL_SUCCESS = {
    "context": "success",
    "main_tip": "success",
    "preflight": "success",
    "claim": "success",
    "migrate": "success",
    "pair": "failure",
}


def test_the_diagnostic_carries_provenance_and_step_outcomes_only(job, tmp_path):
    done, diag = _diagnostic(job, tmp_path, **ALL_SUCCESS)
    assert done.returncode == 0, done.stderr
    assert json.loads((diag / "outcomes.json").read_text()) == {
        "campaign_id": CAMPAIGN,
        "run_id": "4242",
        "run_attempt": "1",
        "sha": SHA,
        "expected_sha": SHA,
        "context": "success",
        "main_tip": "success",
        "preflight": "success",
        "claim": "success",
        "migrate": "success",
        "pair": "failure",
        "publishable": "verified-files-only",
    }


def test_an_unvalidated_identifier_is_never_copied_into_the_diagnostic(job, tmp_path):
    hostile = 'x","pair":"success'
    done, diag = _diagnostic(
        job,
        tmp_path,
        campaign=hostile,
        **{
            **ALL_SUCCESS,
            "context": "failure",
            "main_tip": "skipped",
            "preflight": "skipped",
            "claim": "skipped",
            "migrate": "skipped",
            "pair": "skipped",
        },
    )
    assert done.returncode == 0, done.stderr
    parsed = json.loads((diag / "outcomes.json").read_text())
    assert (
        parsed["campaign_id"] == "unvalidated"
        and parsed["expected_sha"] == "unvalidated"
        and parsed["pair"] == "skipped"
    )
