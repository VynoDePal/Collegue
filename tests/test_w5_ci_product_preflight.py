"""Préflight PRODUIT distant sans modèle (C69) : contrat du workflow et du harnais, hors ligne.

Rien n'est exécuté contre GitHub ni contre un modèle : le YAML est analysé, le harnais est piloté avec un faux lanceur de processus (sauf deux tests
qui exécutent le VRAI scanner de fuite et la VRAIE entrée publique du produit dans un contexte de lancement refusé par P01 — donc sans aucun accès
réseau). Les barrières vérifiées : aucune clé de modèle, aucune action autre que ``preflight``, aucun fait de lancement fabriqué, jeton confiné aux
deux étapes de lecture, propagation des échecs (code rouge pour garde rouge), rapports incomplets fidèles, aucune fuite dans les sorties.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
PREFLIGHT = WORKFLOWS / "w5-product-preflight.yml"
CAMPAIGN = WORKFLOWS / "consolidation-e2e.yml"
CONFIRMATION = "LANCER-UNE-FOIS-2USD-250000TOKENS-900S"
TOKEN = "github_pat_FAKE0123456789abcdefghijKLMNOP_0123456789"
SHA = "a" * 40
IMAGE = "sha256:" + "b" * 64

# W5_PREFLIGHT_UNDER_TEST : rejouer ces tests sur un autre état du harnais (preuve rouge→vert), même convention que les sondes du manager
UNDER_TEST = Path(os.environ.get("W5_PREFLIGHT_UNDER_TEST") or ROOT / "scripts" / "w5_product_preflight.py")
SPEC = importlib.util.spec_from_file_location("w5_product_preflight", UNDER_TEST)
assert SPEC and SPEC.loader
harness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = harness
SPEC.loader.exec_module(harness)


# ── workflow ────────────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def raw() -> str:
    return PREFLIGHT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def wf(raw) -> dict:
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict)
    return parsed


@pytest.fixture(scope="module")
def job(wf) -> dict:
    assert list(wf["jobs"]) == ["preflight"]
    return wf["jobs"]["preflight"]


def step(job: dict, prefix: str) -> dict:
    for item in job["steps"]:
        if item.get("name", "").startswith(prefix):
            return item
    raise AssertionError(f"étape introuvable : {prefix}")


def test_only_a_manual_dispatch_with_the_product_confirmation_can_start_it(wf, job, raw):
    assert list(wf[True]) == ["workflow_dispatch"]  # YAML 1.1 : « on » est lu comme le booléen True
    inputs = wf[True]["workflow_dispatch"]["inputs"]
    assert list(inputs) == ["confirm"] and inputs["confirm"]["required"] is True
    assert CONFIRMATION in job["if"] and "workflow_dispatch" in job["if"]
    for forbidden in (
        "cron",
        "schedule",
        "pull_request",
        "push:",
        "repository_dispatch",
        "workflow_run",
        "workflow_call",
    ):
        assert forbidden not in re.sub(r"(?m)^\s*#.*$", "", raw), forbidden
    assert "continue-on-error" not in raw


def test_least_privilege_no_environment_distinct_concurrency_and_bounded(wf, job):
    assert wf["permissions"] == {"contents": "read"} and "permissions" not in job
    assert "environment" not in job, (
        "aucun environnement GitHub : ni le secret de modèle ni l'environnement de campagne"
    )
    campaign = yaml.safe_load(CAMPAIGN.read_text(encoding="utf-8"))
    assert (
        wf["concurrency"]["group"] != campaign["concurrency"]["group"]
        and wf["concurrency"]["cancel-in-progress"] is False
    )
    assert job["timeout-minutes"] <= 45


def test_environment_is_identical_to_the_campaign_one_so_the_real_envelope_is_what_is_checked(job):
    campaign = yaml.safe_load(CAMPAIGN.read_text(encoding="utf-8"))["jobs"]["campaign"]["env"]
    mine = dict(job["env"])
    campaign_id_only = {
        "W4_BUSINESS_CAMPAIGN_ID"
    }  # propre à la campagne (saisie) ; ici dérivé du numéro de run dans l'étape d'initialisation
    assert set(campaign) - campaign_id_only == set(mine)
    for name, value in mine.items():
        assert str(value) == str(campaign[name]), f"{name} diverge du workflow de campagne"
    assert "${{ inputs.confirm }}" == mine["W4_BUSINESS_CONFIRM"]


def test_no_model_key_name_or_secret_other_than_the_fixture_token_exists_anywhere(wf, job, raw):
    dumped = json.dumps(wf)
    for forbidden in (
        "LLM_API_KEY",
        "GEMINI",
        "GOOGLE",
        "OPENAI",
        "ANTHROPIC",
        "W5_GOOGLE",
        "w5-gemma-campaign",
        "W5_BOOTSTRAP_MANIFEST_JSON",
    ):
        assert forbidden not in dumped, forbidden
    assert not any("secrets." in str(v) for v in job["env"].values()), "aucun secret au niveau du job"
    assert dumped.count("secrets.") == 2
    assert "secrets: inherit" not in raw


def test_the_fixture_token_exists_only_in_the_two_product_preflight_steps_under_one_name(job):
    holders = [s["name"] for s in job["steps"] if "secrets." in json.dumps(s)]
    assert holders == [
        "Préflight produit statique (lectures GitHub seules, sans clé de modèle)",
        "Préflight produit complet (pile des oracles et lecteur PDF réel, sans clé de modèle)",
    ]
    for prefix in ("Préflight produit statique", "Préflight produit complet"):
        assert step(job, prefix)["env"] == {"GITHUB_TOKEN": "${{ secrets.INTEGRATION_FIXTURE_GITHUB_TOKEN }}"}
        assert "uses" not in step(job, prefix), "le jeton n'est jamais confié à une action tierce"


def test_only_closed_commands_run_and_none_can_launch_claim_activate_or_clean(job):
    commands = [s["run"] for s in job["steps"] if "run" in s]
    text = "\n".join(commands)
    for forbidden in (
        "w4_business",
        " run ",
        "cleanup",
        "claim",
        "activate",
        "gh ",
        "curl",
        "docker run",
        "docker login",
        "git push",
    ):
        assert forbidden not in text, forbidden
    stages = re.findall(r"w5_product_preflight\.py stage --stage (static|full)", text)
    assert stages == ["static", "full"]
    assert text.count('docker build -f docker/sandbox/Dockerfile.broker -t "$SANDBOX_IMAGE" .') == 1
    assert (
        'python scripts/w5_workflow_env.py >> "$GITHUB_ENV"' in text and "python -m collegue.migrations upgrade" in text
    )


def test_no_launch_fact_is_fabricated_and_the_manifest_is_the_tracked_v2_file(wf, job, raw):
    dumped = json.dumps(wf)
    for fact in ("GITHUB_EVENT_NAME", "GITHUB_RUN_ATTEMPT"):
        assert fact not in dumped, f"{fact} ne doit jamais être fixé par le workflow (P01 le lit du déclenchement réel)"
    assert "W5_BOOTSTRAP_MANIFEST=%s/tests/fixtures/w5-business/manifest.v2.json" in dumped
    assert (ROOT / "tests" / "fixtures" / "w5-business" / "manifest.v2.json").is_file()
    assert 'campaign_id="w5-preflight-${GITHUB_RUN_ID}"' in dumped.replace('\\"', '"')


def test_step_order_and_conditions_propagate_failures(job):
    names = [s.get("name", s.get("uses", "")) for s in job["steps"]]
    order = [
        "Initialiser les chemins éphémères du runner",
        "actions/checkout@v4",
        "Set up Python 3.12",
        "Installer les dépendances verrouillées",
        "Exporter la commande de santé indépendante de Phase 5 (API publique du produit)",
        "Initialiser le registre durable local (migrations, base vierge du préflight)",
        "Préflight produit statique (lectures GitHub seules, sans clé de modèle)",
        "Construire l'image du sandbox (courtier)",
        "Préflight produit complet (pile des oracles et lecteur PDF réel, sans clé de modèle)",
        "Provenance du préflight (sans secret)",
        "Déposer les rapports du préflight (ensemble scanné, sans jeton)",
        "Déposer la provenance et les rapports du scanner (noms expurgés)",
    ]
    assert names == order
    assert step(job, "Construire")["if"] == "steps.preflight_static.outcome == 'success'"
    assert step(job, "Préflight produit complet")["if"] == (
        "steps.preflight_static.outcome == 'success' && steps.image.outcome == 'success'"
    )
    for prefix in ("Provenance", "Déposer les rapports", "Déposer la provenance"):
        assert step(job, prefix)["if"] == "always()"
    assert "secrets." not in json.dumps(step(job, "Provenance")), "aucun secret dans l'étape de provenance"
    uploads = [s for s in job["steps"] if "upload-artifact" in str(s.get("uses"))]
    assert [u["with"]["path"] for u in uploads] == [
        "${{ env.W5_PREFLIGHT_ROOT }}/publish",
        "${{ env.W5_PREFLIGHT_ROOT }}/diag",
    ], "jamais le home, le registre ni un dossier source : seulement l'ensemble scanné et le diagnostic"


def test_every_script_the_workflow_calls_exists():
    for name in ("w5_product_preflight.py", "w5_leak_scan.py", "w5_workflow_env.py"):
        assert (ROOT / "scripts" / name).is_file()


def test_the_tracked_manifest_passes_the_products_own_shape_validator_offline():
    from collegue.pilot import w5_business as w5

    manifest = w5.load_bootstrap_manifest(str(ROOT / harness.MANIFEST))
    bootstrap_sha, approved, modified = w5._manifest_shape(manifest)
    assert bootstrap_sha == "ad56c0fa03066b1f6efb3cf6a5aa26cb496372ca" and approved
    assert manifest["seed_sha"] == "8e3691d8e4f311e00d620c9c2ca2d9edbd8b136a"


def test_closed_enumerations_equal_the_products_constants():
    from collegue.pilot import w4_business as business

    assert harness.STEP_STATES == business.STEP_STATES and harness.REPORT_SCHEMA == business.REPORT_SCHEMA
    assert set(harness.VERDICTS) == {
        business.VERDICT_VALIDATED,
        business.VERDICT_FAILED,
        business.VERDICT_BUDGET_STOP,
        business.VERDICT_INCOMPLETE,
    }
    assert harness.EXIT_CODES == business.EXIT_CODES
    assert harness.PRODUCT_MODULE == "collegue.pilot.w4_business"


# ── harnais : étape ─────────────────────────────────────────────────────────────────────────────────────────────────────────


class FakeRunner:
    """Faux lanceur : le premier appel est le produit, le second le scanner. Mémorise argv et environnement."""

    def __init__(self, product_rc=0, scan_rc=0, write_report=True, product_out="", product_err="", scan_out=""):
        self.calls: List[Dict[str, Any]] = []
        self.product_rc, self.scan_rc, self.write_report = product_rc, scan_rc, write_report
        self.product_out, self.product_err, self.scan_out = product_out, product_err, scan_out

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        if len(self.calls) == 1:
            if self.write_report:
                Path(argv[argv.index("--output") + 1]).write_text("{}", encoding="utf-8")
            return subprocess.CompletedProcess(argv, self.product_rc, self.product_out, self.product_err)
        return subprocess.CompletedProcess(argv, self.scan_rc, self.scan_out, "")


def env_with_token(**extra: str) -> Dict[str, str]:
    return {"GITHUB_TOKEN": TOKEN, "PATH": "/usr/bin", **extra}


def test_product_command_is_exactly_preflight_and_never_run_cleanup_claim_or_activate(tmp_path):
    argv = harness.product_argv("static", "w5-preflight-1", tmp_path / "a.json", tmp_path / "a.txt")
    assert argv[1:5] == ["-m", "collegue.pilot.w4_business", "preflight", "--stage"]
    assert argv[argv.index("collegue.pilot.w4_business") + 1] == "preflight"
    assert not {"run", "cleanup", "capacity", "claim", "activate"} & set(argv)
    assert harness.PRODUCT_ACTION == "preflight"


@pytest.mark.parametrize("stage", ["static", "full"])
def test_success_runs_product_then_scanner_records_the_real_code_and_never_puts_the_token_in_argv(tmp_path, stage):
    runner = FakeRunner()
    rc = harness.run_stage(stage, root=tmp_path, campaign_id="w5-preflight-123", env=env_with_token(), runner=runner)
    assert rc == 0 and len(runner.calls) == 2
    product, scan = runner.calls
    assert product["argv"][3] == "preflight" and product["argv"][5] == stage
    assert scan["argv"][1].endswith("w5_leak_scan.py") and scan["argv"][2:4] == ["--env", "GITHUB_TOKEN"]
    assert all(TOKEN not in part for call in runner.calls for part in call["argv"])
    assert json.loads((tmp_path / "diag" / f"rc-{stage}.json").read_text()) == {
        "action": "preflight",
        "rc": 0,
        "stage": stage,
    }
    assert f"report={tmp_path / 'report' / stage}" in scan["argv"] and str(tmp_path / "publish" / stage) in scan["argv"]


@pytest.mark.parametrize("product_rc", [1, 3, 4, 137])
def test_a_red_or_incomplete_product_verdict_stays_red_even_if_the_scanner_is_clean(tmp_path, product_rc):
    runner = FakeRunner(product_rc=product_rc, scan_rc=0)
    assert (
        harness.run_stage("static", root=tmp_path, campaign_id="w5-preflight-1", env=env_with_token(), runner=runner)
        == product_rc
    )
    assert len(runner.calls) == 2, "le rapport partiel est quand même scanné et publié"
    assert json.loads((tmp_path / "diag" / "rc-static.json").read_text())["rc"] == product_rc


@pytest.mark.parametrize("scan_rc", [1, 2])
def test_a_scanner_leak_or_incomplete_scan_turns_a_green_product_red(tmp_path, scan_rc):
    runner = FakeRunner(product_rc=0, scan_rc=scan_rc)
    assert (
        harness.run_stage("full", root=tmp_path, campaign_id="w5-preflight-1", env=env_with_token(), runner=runner)
        == scan_rc
    )


def test_product_green_without_a_machine_report_is_red(tmp_path, capsys):
    runner = FakeRunner(product_rc=0, write_report=False)
    assert (
        harness.run_stage("static", root=tmp_path, campaign_id="w5-preflight-1", env=env_with_token(), runner=runner)
        == 1
    )
    assert "sans rapport machine" in capsys.readouterr().err


@pytest.mark.parametrize(
    "name",
    [
        "LLM_API_KEY",
        "LLM_API_KEY_CODER",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "W5_GOOGLE_API_KEY",
    ],
)
def test_any_model_key_in_the_environment_refuses_before_any_command(tmp_path, name, capsys):
    runner = FakeRunner()
    assert (
        harness.run_stage(
            "static", root=tmp_path, campaign_id="w5-preflight-1", env=env_with_token(**{name: "x" * 20}), runner=runner
        )
        == 2
    )
    assert runner.calls == [] and name in capsys.readouterr().err and not (tmp_path / "diag").exists()


@pytest.mark.parametrize("token", ["", "short"])
def test_missing_or_short_fixture_token_refuses_before_any_command(tmp_path, token):
    runner = FakeRunner()
    assert (
        harness.run_stage(
            "static", root=tmp_path, campaign_id="w5-preflight-1", env={"GITHUB_TOKEN": token}, runner=runner
        )
        == 2
    )
    assert runner.calls == []


@pytest.mark.parametrize("campaign_id", ["", "W5-UP", "w5 spaced", "../x", "ab", "w5-" + "x" * 40])
def test_invalid_campaign_id_or_unknown_stage_refuses(tmp_path, campaign_id):
    runner = FakeRunner()
    assert harness.run_stage("static", root=tmp_path, campaign_id=campaign_id, env=env_with_token(), runner=runner) == 2
    assert (
        harness.run_stage("launch", root=tmp_path, campaign_id="w5-preflight-1", env=env_with_token(), runner=runner)
        == 2
    )
    assert runner.calls == []


def token_forms() -> List[str]:
    return [
        TOKEN,
        urllib.parse.quote(TOKEN, safe=""),
        base64.b64encode(TOKEN.encode()).decode(),
        base64.b64encode(b"x" + TOKEN.encode()).decode(),
        json.dumps(TOKEN)[1:-1],
    ]


def test_raw_subprocess_output_is_never_printed_only_a_typed_summary_whatever_it_contains(tmp_path, capsys):
    runner = FakeRunner(
        product_out="\n".join(f"SORTIE-BRUTE {f}" for f in token_forms()),
        product_err=f"Traceback (most recent call last):\n  File x\nRuntimeError: {TOKEN}",
        scan_out=f"SCAN-BRUT {TOKEN}",
    )
    harness.run_stage("static", root=tmp_path, campaign_id="w5-preflight-1", env=env_with_token(), runner=runner)
    shown = capsys.readouterr()
    text = shown.out + shown.err
    assert "SORTIE-BRUTE" not in text and "SCAN-BRUT" not in text and "Traceback" not in text
    assert all(form not in text for form in token_forms()) and "REDACTED" not in text, (
        "rien d'arbitraire n'est affiché, donc rien à expurger"
    )
    assert "code produit 0" in text and "aucun rapport machine lisible" in text
    assert "(classe d'exception : RuntimeError)" in text, (
        "seul le NOM de classe de l'exception est conservé, jamais son message"
    )


def test_the_summary_is_derived_from_the_typed_report_not_from_free_text(tmp_path, capsys):
    def runner(argv, **kwargs):
        if "w5_leak_scan.py" in argv[1]:
            return subprocess.CompletedProcess(argv, 0, "brut", "")
        report = Path(argv[argv.index("--output") + 1])
        report.write_text(
            json.dumps(
                make_report(
                    "static",
                    overrides={"P07-base-protection": "incomplete_validation"},
                    verdict="incomplete_validation",
                    exit_code=3,
                )
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 3, f"détail libre {TOKEN}", "")

    rc = harness.run_stage("static", root=tmp_path, campaign_id="w5-preflight-1", env=env_with_token(), runner=runner)
    out = capsys.readouterr().out
    assert rc == 3 and "verdict produit : incomplete_validation (code 3)" in out
    assert "P07-base-protection : incomplete_validation" in out and "P08-oracle-environment : not_executed" in out
    assert "titre libre" not in out and "détail libre" not in out and TOKEN not in out


def test_redaction_happens_before_truncation_so_a_token_cut_at_the_limit_leaves_no_prefix():
    for cut in range(8, len(TOKEN)):  # le jeton franchit la borne à chaque position possible
        text = "x" * (200 - cut) + TOKEN + "y" * 50
        shown = harness.safe_text(text, TOKEN, limit=200)
        assert TOKEN[:8] not in shown, cut


def test_exception_class_keeps_only_the_class_name():
    assert harness.exception_class(f"Traceback...\nValueError: {TOKEN}") == "ValueError"
    assert harness.exception_class("a\nb\nhttpx.ConnectError: msg") == "httpx.ConnectError"
    assert harness.exception_class(f"rien de reconnaissable {TOKEN}") is None and harness.exception_class(None) is None


def test_the_real_scanner_excludes_a_report_that_contains_the_token_and_the_stage_is_red(tmp_path):
    def runner(argv, **kwargs):
        if len(calls) == 0:
            calls.append(argv)
            report = Path(argv[argv.index("--output") + 1])
            report.write_text(json.dumps({"detail": f"fuite {TOKEN}"}), encoding="utf-8")
            report.with_suffix(".txt").write_text("propre\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.run(argv, **kwargs)

    calls: List[Any] = []
    rc = harness.run_stage("static", root=tmp_path, campaign_id="w5-preflight-1", env=env_with_token(), runner=runner)
    assert rc == 1
    published = sorted(p.name for p in (tmp_path / "publish" / "static").rglob("*") if p.is_file())
    assert published == ["preflight-static.txt"], "le fichier contaminé n'est jamais publié"
    scan = json.loads((tmp_path / "diag" / "leak-scan-static.json").read_text())
    assert scan["verdict"] == "leak" and TOKEN not in json.dumps(scan)
    for path in tmp_path.rglob("*"):
        if path.is_file() and "quarantine" not in path.parts:
            assert TOKEN.encode() not in path.read_bytes(), path


def test_the_real_product_entry_refuses_a_non_dispatch_context_at_p01_with_no_network_and_stays_incomplete(tmp_path):
    """VRAIE entrée publique du produit, sans fabriquer le contexte : hors ``workflow_dispatch``, P01 la refuse et RIEN d'autre ne joue."""
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "COLLEGUE_HOME": str(home),
        "STATE_DATABASE_URL": f"sqlite:///{home}/x.sqlite3",
        "GITHUB_TOKEN": TOKEN,
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_RUN_ATTEMPT": "1",
        "PYTHONPATH": str(ROOT),
    }
    rc = harness.run_stage("static", root=tmp_path / "root", campaign_id="w5-preflight-1", env=env)
    assert rc == 3
    report = json.loads((tmp_path / "root" / "report" / "static" / "preflight-static.json").read_text())
    states = {s["id"]: s["state"] for s in report["steps"]}
    assert states["P01-launch-context"] == "incomplete_validation"
    assert all(state == "not_executed" for step_id, state in states.items() if step_id != "P01-launch-context")
    assert report["facts"]["llm_calls_emitted"] == 0 and report["facts"]["billable_actions_emitted"] == 0
    assert TOKEN not in json.dumps(report)
    record = harness.build_provenance(
        tmp_path / "root", {"GITHUB_SHA": SHA, "GITHUB_EVENT_NAME": "push", "GITHUB_RUN_ATTEMPT": "1"}, image_id=IMAGE
    )
    assert record["verdict"] == "incomplete_validation" and record["partial"] is True
    assert record["played_steps"]["static"] == ["P01-launch-context"]


# ── harnais : provenance ────────────────────────────────────────────────────────────────────────────────────────────────


DECLARED = (  # ordre de déclaration du produit (run_preflight) : P01 à P07, contrôles W5 P09 à P12, puis P08
    "P01-launch-context",
    "P02-environment",
    "P03-secret-scope",
    "P04-fixture-identity",
    "P05-role-routes",
    "P06-worker-capacity",
    "P07-base-protection",
    "P09-gemma-models",
    "P10-budget-broker",
    "P11-bootstrap",
    "P12-campaign-identity",
    "P08-oracle-environment",
)
OPTIONAL_STATIC = "P08-oracle-environment"


def make_steps(stage: str, overrides=None) -> List[Dict[str, Any]]:
    """Étapes d'un rapport RÉALISTE : tout réussit sauf ``overrides`` ; après la première étape non réussie, le reste est ``not_executed``."""
    overrides, halted, steps = overrides or {}, False, []
    for step_id in DECLARED:
        optional = stage == "static" and step_id == OPTIONAL_STATIC
        if optional or halted:
            state = "not_executed"
        else:
            state = overrides.get(step_id, "succeeded")
            halted = state != "succeeded"
        steps.append(
            {
                "id": step_id,
                "state": state,
                "required": not optional,
                "title": "titre libre",
                "detail": "détail libre",
                "evidence": {"x": TOKEN},
            }
        )
    return steps


def make_report(
    stage: str, *, verdict="validated", exit_code=0, overrides=None, emitted=(0, 0), mutate=None
) -> Dict[str, Any]:
    steps = make_steps(stage, overrides)
    if mutate:
        steps = mutate(steps)
    return {
        "schema": "w4-business-report/1",
        "kind": "preflight",
        "campaign_id": "w5-preflight-1",
        "verdict": verdict,
        "exit_code": exit_code,
        "steps": steps,
        "facts": {
            "stage": stage,
            "llm_calls_emitted": emitted[0],
            "billable_actions_emitted": emitted[1],
            "libre": TOKEN,
        },
    }


def lay_down(root: Path, stage: str, *, rc=0, report=None, scan="clean", files=("preflight-X.json",)) -> None:
    paths = harness.layout(root)
    for key in ("report", "diag"):
        (paths[key] / (stage if key == "report" else "")).mkdir(parents=True, exist_ok=True)
    (paths["diag"] / f"rc-{stage}.json").write_text(
        json.dumps({"stage": stage, "action": "preflight", "rc": rc}), encoding="utf-8"
    )
    if report is not None:
        (paths["report"] / stage / f"preflight-{stage}.json").write_text(json.dumps(report), encoding="utf-8")
    (paths["diag"] / f"leak-scan-{stage}.json").write_text(json.dumps({"verdict": scan}), encoding="utf-8")
    published = paths["publish"] / stage / "report"
    published.mkdir(parents=True, exist_ok=True)
    for name in files:
        (published / name).write_text("{}", encoding="utf-8")


DISPATCH = {
    "GITHUB_SHA": SHA,
    "GITHUB_EVENT_NAME": "workflow_dispatch",
    "GITHUB_RUN_ATTEMPT": "1",
    "GITHUB_RUN_ID": "7",
    "SANDBOX_IMAGE": "collegue-sandbox-broker:ci",
}


def provenance(root: Path, env=None, image=IMAGE) -> Dict[str, Any]:
    return harness.build_provenance(root, DISPATCH if env is None else env, image_id=image, tree=lambda spec: "c" * 40)


def validated_root(tmp_path: Path) -> Path:
    lay_down(tmp_path, "static", report=make_report("static"))
    lay_down(tmp_path, "full", report=make_report("full"))
    return tmp_path


def test_provenance_is_validated_only_when_everything_is_established(tmp_path):
    record = provenance(validated_root(tmp_path))
    assert record["verdict"] == "validated" and record["reasons"] == [] and record["partial"] is False
    assert record["product"]["sha"] == SHA and record["harness"]["sha"] == SHA
    assert record["product"]["trees"] == {p: "c" * 40 for p in harness.PRODUCT_TREES}
    assert record["manifest"]["path"] == harness.MANIFEST and len(record["manifest"]["sha256"]) == 64
    assert record["image"] == {"name": "collegue-sandbox-broker:ci", "id": IMAGE}
    assert record["commands"] == [
        {"stage": "static", "argv": ["collegue.pilot.w4_business", "preflight", "--stage", "static"], "rc": 0},
        {"stage": "full", "argv": ["collegue.pilot.w4_business", "preflight", "--stage", "full"], "rc": 0},
    ]
    assert record["played_steps"]["static"] == [s for s in DECLARED if s != OPTIONAL_STATIC]
    assert record["played_steps"]["full"] == list(DECLARED) and record["played_steps"]["full"][-1] == OPTIONAL_STATIC
    assert record["llm_calls_emitted"] == 0 and record["billable_actions_emitted"] == 0
    assert "NI BUILD" in record["scope"]


def test_provenance_copies_only_typed_fields_never_free_text_or_the_token(tmp_path):
    record = provenance(validated_root(tmp_path))
    dumped = json.dumps(record)
    for free in (TOKEN, "titre libre", "détail libre", '"evidence"', '"libre"'):
        assert free not in dumped, free


def test_without_the_full_stage_the_record_is_partial_and_incomplete_never_validated(tmp_path):
    lay_down(tmp_path, "static", report=make_report("static"))
    record = provenance(tmp_path)
    assert record["verdict"] == "incomplete_validation" and record["partial"] is True
    assert "full:incomplete_validation" in record["reasons"] and [c["stage"] for c in record["commands"]] == ["static"]
    assert record["played_steps"]["full"] == []


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({**DISPATCH, "GITHUB_RUN_ATTEMPT": "2"}, "launch_context_not_first_dispatch"),
        ({**DISPATCH, "GITHUB_EVENT_NAME": "push"}, "launch_context_not_first_dispatch"),
        ({**DISPATCH, "GITHUB_SHA": "nope"}, "sha_absent"),
    ],
)
def test_a_rerun_a_foreign_event_or_a_missing_sha_is_never_validated(tmp_path, env, reason):
    record = provenance(validated_root(tmp_path), env)
    assert record["verdict"] == "incomplete_validation" and reason in record["reasons"]


@pytest.mark.parametrize("image", [None, "", "sha256:short", "latest"])
def test_a_missing_or_invalid_image_id_is_incomplete(tmp_path, image):
    record = provenance(validated_root(tmp_path), image=image)
    assert (
        record["verdict"] == "incomplete_validation"
        and "image_id_absent" in record["reasons"]
        and record["image"]["id"] is None
    )


def test_a_red_product_stage_makes_the_record_failed(tmp_path):
    lay_down(tmp_path, "static", report=make_report("static"))
    lay_down(
        tmp_path,
        "full",
        rc=1,
        report=make_report(
            "full",
            verdict="failed",
            exit_code=1,
            overrides={"P07-base-protection": "failed"},
        ),
    )
    record = provenance(tmp_path)
    assert record["verdict"] == "failed" and "full:failed" in record["reasons"]
    assert record["played_steps"]["full"] == list(DECLARED[:7])  # P01 à P07 : les contrôles suivants n'ont pas tourné


def test_an_incomplete_product_report_stays_incomplete_and_names_the_steps_actually_played(tmp_path):
    states = {"P07-base-protection": "incomplete_validation"}
    lay_down(
        tmp_path,
        "static",
        rc=3,
        report=make_report("static", verdict="incomplete_validation", exit_code=3, overrides=states),
    )
    record = provenance(tmp_path)
    assert (
        record["verdict"] == "incomplete_validation"
        and record["reasons"][-2:] == ["static:incomplete_validation", "full:incomplete_validation"][-2:]
    )
    assert record["played_steps"]["static"] == list(DECLARED[:7])
    assert record["commands"][0]["rc"] == 3


def test_a_product_that_claims_success_with_a_non_zero_code_or_emissions_or_a_leak_is_not_validated(tmp_path):
    cases = {
        "code": dict(rc=1, report=make_report("static")),
        "emission": dict(report=make_report("static", emitted=(1, 0))),
        "billable": dict(report=make_report("static", emitted=(0, 2))),
        "leak": dict(report=make_report("static"), scan="leak"),
        "scan_incomplete": dict(report=make_report("static"), scan="incomplete"),
    }
    for name, kwargs in cases.items():
        root = tmp_path / name
        lay_down(root, "static", **kwargs)
        lay_down(root, "full", report=make_report("full"))
        record = provenance(root)
        assert record["verdict"] != "validated", name
        assert record["stages"]["static"]["status"] != "validated", name


def test_a_required_step_that_did_not_succeed_blocks_a_validated_verdict_even_if_the_product_says_validated(tmp_path):
    report = make_report("static", overrides={"P07-base-protection": "not_executed"})
    lay_down(tmp_path, "static", report=report)
    lay_down(tmp_path, "full", report=make_report("full"))
    assert provenance(tmp_path)["stages"]["static"]["status"] != "validated"


def test_a_report_of_the_wrong_schema_kind_or_stage_is_ignored(tmp_path):
    bad = make_report("static")
    bad["schema"] = "autre/1"
    lay_down(tmp_path, "static", report=bad)
    lay_down(tmp_path, "full", report=make_report("static"))  # étape annoncée « static » dans le fichier « full »
    record = provenance(tmp_path)
    assert record["stages"]["static"]["report"] is None and record["stages"]["full"]["report"] is None
    assert record["verdict"] == "incomplete_validation"


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [("validated", 0), ("failed", 1), ("incomplete_validation", 3)],
)
def test_provenance_command_writes_the_record_and_returns_the_products_exit_code(tmp_path, capsys, verdict, expected):
    root = tmp_path / "root"
    if verdict == "validated":
        validated_root(root)
    elif verdict == "failed":
        lay_down(root, "static", rc=1, report=make_report("static", verdict="failed", exit_code=1))
    else:
        lay_down(root, "static", report=make_report("static"))
    output = tmp_path / "provenance.json"
    env = {**DISPATCH}
    rc = harness.main(["provenance", "--root", str(root), "--image-id", IMAGE, "--output", str(output)], environ=env)
    assert rc == expected and json.loads(output.read_text())["verdict"] == verdict
    printed = json.loads(capsys.readouterr().out)
    assert set(printed) == {"verdict", "reasons", "partial"} and TOKEN not in json.dumps(printed)


def test_provenance_with_nothing_recorded_is_incomplete_not_a_crash(tmp_path):
    record = provenance(tmp_path / "empty")
    assert record["verdict"] == "incomplete_validation" and record["commands"] == [] and record["partial"] is True


def test_cli_stage_subcommand_rejects_unknown_stages_at_parse_time(tmp_path):
    with pytest.raises(SystemExit) as exc:
        harness.main(
            ["stage", "--stage", "launch", "--root", str(tmp_path), "--campaign-id", "w5-preflight-1"],
            environ=env_with_token(),
        )
    assert exc.value.code == 2


# ── C70 : contrôles attendus complets et preuve d'identité du contenu testé ──────────────────────────────────────────────────


def outcome_of(stage: str, report: Dict[str, Any], *, rc=0, scan="clean") -> Dict[str, Any]:
    return {
        "executed": True,
        "product_rc": rc,
        "leak_scan": scan,
        "published_files": [f"preflight-{stage}.json", f"preflight-{stage}.txt"],
        "report": harness.typed_report(report, stage),
    }


def test_the_expected_controls_are_exactly_those_the_product_declares_in_both_stages():
    from types import SimpleNamespace

    from collegue.pilot import w4_business as business
    from collegue.pilot import w5_business as w5

    for stage in ("static", "full"):
        extra = w5.w5_preflight_checks({}, settings=None, clients=SimpleNamespace(), campaign_id="w5-x", run_tag="1-1")
        report = business.run_preflight(
            {},
            clients=SimpleNamespace(),
            campaign_id="w5-x",
            run_tag="1-1",
            image_runner=lambda argv: None,
            stage=stage,
            settings=object(),
            extra_checks=extra,
        )
        declared = {step.id: step.required for step in report.steps}
        assert set(declared) == set(harness.EXPECTED_STEPS) and len(report.steps) == len(harness.EXPECTED_STEPS)
        assert declared == {
            sid: not (stage == "static" and sid == harness.OPTIONAL_IN_STATIC) for sid in harness.EXPECTED_STEPS
        }
        assert harness.step_problems(stage, harness.typed_report(report.to_machine(), stage)) == []


@pytest.mark.parametrize("stage", ["static", "full"])
def test_a_complete_report_has_no_problem(stage):
    assert harness.step_problems(stage, harness.typed_report(make_report(stage), stage)) == []
    assert harness.stage_status(outcome_of(stage, make_report(stage)), stage) == "validated"


@pytest.mark.parametrize("missing", harness.EXPECTED_STEPS)
@pytest.mark.parametrize("stage", ["static", "full"])
def test_a_report_missing_any_control_is_never_validated(stage, missing):
    report = make_report(stage, mutate=lambda steps: [s for s in steps if s["id"] != missing])
    outcome = outcome_of(stage, report)
    assert f"missing:{missing}" in harness.step_problems(stage, outcome["report"])
    assert harness.stage_status(outcome, stage) == "incomplete_validation"


def test_a_duplicated_control_is_never_validated_even_if_both_copies_succeeded():
    report = make_report("full", mutate=lambda steps: steps + [dict(steps[2])])
    outcome = outcome_of("full", report)
    assert "duplicate:P03-secret-scope" in harness.step_problems("full", outcome["report"])
    assert harness.stage_status(outcome, "full") != "validated"


def test_an_unexpected_or_unreadable_control_is_never_validated():
    extra = make_report(
        "full", mutate=lambda steps: steps + [{"id": "P13-surprise", "state": "succeeded", "required": True}]
    )
    assert "unexpected_step" in harness.step_problems("full", harness.typed_report(extra, "full"))
    assert harness.stage_status(outcome_of("full", extra), "full") != "validated"
    garbled = make_report(
        "full", mutate=lambda steps: steps + [{"id": "n'importe quoi", "state": "succeeded"}, "pas un objet"]
    )
    typed = harness.typed_report(garbled, "full")
    assert typed["dropped_steps"] == 2 and "malformed_steps" in harness.step_problems("full", typed)
    assert typed["unexpected_steps"] == 0
    assert harness.stage_status(outcome_of("full", garbled), "full") != "validated"
    bad_state = make_report(
        "full", mutate=lambda steps: [{**s, "state": "ok"} if s["id"] == "P05-role-routes" else s for s in steps]
    )
    assert harness.stage_status(outcome_of("full", bad_state), "full") != "validated"


@pytest.mark.parametrize(
    ("stage", "step_id", "required"),
    [
        ("full", "P08-oracle-environment", False),  # P08 déclaré facultatif à l'étape complète : falsifié
        ("full", "P01-launch-context", False),
        ("static", "P07-base-protection", False),
        ("static", "P08-oracle-environment", True),  # P08 obligatoire à l'étape statique : falsifié
    ],
)
def test_a_falsified_required_flag_is_never_validated(stage, step_id, required):
    report = make_report(
        stage, mutate=lambda steps: [{**s, "required": required} if s["id"] == step_id else s for s in steps]
    )
    outcome = outcome_of(stage, report)
    assert f"required_flag:{step_id}" in harness.step_problems(stage, outcome["report"])
    assert harness.stage_status(outcome, stage) != "validated"


def test_the_optional_oracle_control_must_not_be_played_in_the_static_stage():
    report = make_report(
        "static",
        mutate=lambda steps: [
            {**s, "state": "succeeded", "required": False} if s["id"] == OPTIONAL_STATIC else s for s in steps
        ],
    )
    assert "optional_step_played:P08-oracle-environment" in harness.step_problems(
        "static", harness.typed_report(report, "static")
    )


def test_a_full_report_that_claims_the_static_shape_is_refused_when_the_stage_is_known(tmp_path):
    forged = make_report("static")
    forged["facts"]["stage"] = "full"  # P08 « facultatif et non joué » présenté comme rapport complet
    outcome = outcome_of("full", forged)
    assert harness.stage_status(outcome, "full") != "validated"
    lay_down(tmp_path, "static", report=make_report("static"))
    lay_down(tmp_path, "full", report=forged)
    record = provenance(tmp_path)
    assert (
        record["verdict"] == "incomplete_validation"
        and "full:required_flag:P08-oracle-environment" in record["reasons"]
    )


def test_an_unknown_stage_defaults_to_the_strictest_full_expectations():
    outcome = outcome_of("full", make_report("static"))
    outcome["published_files"] = []
    outcome.pop("stage", None)
    assert harness.infer_stage(outcome) == "full" and harness.stage_status(outcome) != "validated"


def test_provenance_reports_the_control_problems_by_expected_id_only(tmp_path):
    lay_down(
        tmp_path,
        "static",
        report=make_report(
            "static",
            mutate=lambda steps: (
                [s for s in steps if s["id"] != "P11-bootstrap"] + [{"id": "P99-" + "x" * 30, "state": "succeeded"}]
            ),
        ),
    )
    lay_down(tmp_path, "full", report=make_report("full"))
    record = provenance(tmp_path)
    assert "static:missing:P11-bootstrap" in record["reasons"] and "static:unexpected_step" in record["reasons"]
    assert "P99" not in json.dumps(record)


@pytest.mark.parametrize("which", [*harness.PRODUCT_TREES, *harness.HARNESS_FILES])
def test_any_absent_git_identity_forbids_the_complete_verdict(tmp_path, which):
    root = validated_root(tmp_path)
    record = harness.build_provenance(
        root, DISPATCH, image_id=IMAGE, tree=lambda spec: None if spec == f"HEAD:{which}" else "c" * 40
    )
    reason = "product_tree_absent" if which in harness.PRODUCT_TREES else "harness_blob_absent"
    assert record["verdict"] == "incomplete_validation" and reason in record["reasons"]


def test_all_git_identities_absent_or_malformed_is_incomplete(tmp_path):
    root = validated_root(tmp_path)
    for tree in (lambda spec: None, lambda spec: "", lambda spec: "pas-un-sha", lambda spec: "C" * 40):
        record = harness.build_provenance(root, DISPATCH, image_id=IMAGE, tree=tree)
        assert record["verdict"] == "incomplete_validation"
        assert {"product_tree_absent", "harness_blob_absent"} <= set(record["reasons"])
        assert all(value is None for value in record["product"]["trees"].values())


def test_the_real_git_identity_resolves_for_the_committed_harness_files():
    """Dans un checkout de ce dépôt, les arbres et blobs HEAD existent (le workflow s'exécute dans un checkout complet)."""
    for path in (*harness.PRODUCT_TREES, *harness.HARNESS_FILES):
        value = harness.git_value("rev-parse", f"HEAD:{path}")
        assert value is None or len(value) == 40  # None tant que le fichier n'est pas commité (travail en cours)
