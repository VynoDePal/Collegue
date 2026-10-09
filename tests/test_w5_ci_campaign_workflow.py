"""Workflow ponctuel de la campagne W5 : déclenchement unique, clé confinée, enveloppe exacte, rien de récurrent (statique).

Contrôles du fichier YAML seulement (rien n'est exécuté, aucun modèle). Ils complètent ceux que B tient sur le préflight
(``tests/test_w4_business_workflow.py``, propriété B) sans les remplacer : ici le contrat de C — environnement propre à la
campagne, clé uniquement dans l'étape de campagne et sous un seul nom, transport par le courtier, codeur sans réseau, registre
et rapports toujours déposés, aucune variable de prix, nightly intact.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CAMPAIGN = WORKFLOWS / "consolidation-e2e.yml"
CAMPAIGN_STEP = "Campagne réelle (SEULE étape qui reçoit la clé du fournisseur de modèle)"
CONFIRMATION = "LANCER-UNE-FOIS-2USD-250000TOKENS-900S"


@pytest.fixture(scope="module")
def raw() -> str:
    return CAMPAIGN.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def wf(raw) -> dict:
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict)
    return parsed


@pytest.fixture(scope="module")
def job(wf) -> dict:
    assert list(wf["jobs"]) == ["campaign"], "un seul job : l'image du sandbox est partagée par les étapes"
    return wf["jobs"]["campaign"]


def step_named(job: dict, prefix: str) -> dict:
    for step in job["steps"]:
        if step.get("name", "").startswith(prefix):
            return step
    raise AssertionError(f"étape introuvable: {prefix}")


def test_only_a_confirmed_manual_dispatch_can_start_it_and_never_a_repository_event(wf, job, raw):
    assert list(wf[True]) == ["workflow_dispatch"]  # YAML 1.1 : « on » est lu comme le booléen True
    for forbidden in (
        "cron",
        "pull_request",
        "repository_dispatch",
        "workflow_run",
        "workflow_call",
        "continue-on-error",
    ):
        assert forbidden not in raw, forbidden
    inputs = wf[True]["workflow_dispatch"]["inputs"]
    assert inputs["confirm"]["required"] is True and inputs["campaign_id"]["required"] is True
    assert CONFIRMATION in inputs["confirm"]["description"] and CONFIRMATION in job["if"]
    assert re.fullmatch(r"w5-[a-z0-9-]+", inputs["campaign_id"]["default"]), "identifiant NEUF, distinct de celui de W4"
    assert inputs["campaign_id"]["default"] != "w4-business-final"


def test_the_campaign_has_its_own_clean_environment_and_no_write_permission(wf, job):
    assert job["environment"] == "w5-gemma-campaign"
    assert wf["permissions"] == {"contents": "read"} and "permissions" not in job
    assert wf["concurrency"] == {"group": "consolidation-e2e", "cancel-in-progress": False}
    assert job["timeout-minutes"] <= 45, "bornage dur au-delà de l'échéance de 900 s"


def test_the_envelope_models_and_transport_are_exactly_the_agreed_ones(job):
    env = {k: str(v) for k, v in job["env"].items()}
    assert (env["MAX_COST_USD"], env["MAX_TOKENS_BUDGET"], env["COLLEGUE_RUN_DEADLINE_SECONDS"]) == (
        "2",
        "250000",
        "900",
    )
    expected = {
        "BUDGET_MODE": "strict",
        "BUDGET_EXHAUSTED_ACTION": "pause",
        "BUILD_AUTO_MERGE": "true",
        "AUTO_MERGE_ENABLED": "true",
        "AUTO_REVERT_ENABLED": "true",
        "DEPS_REQUIRE_MERGED": "true",
        "STRICT_MAX_INFLIGHT_PRS": "1",
        "TASK_MAX_ATTEMPTS": "1",
        "GATE_ACCEPTANCE_TESTS": "true",
        "REQUIRE_COST_PRICING": "true",
        "LLM_TRANSPORT": "budget_broker",
        "LLM_PROVIDER": "gemini",
        "LLM_MODEL": "gemma-4-31b-it",
        "CODER_FALLBACK_MODELS": "gemma-4-26b-a4b-it",
        "SANDBOX_NETWORK": "none",
        "BROKER_GLOBAL_DEADLINE_SECONDS": "900",
        "BROKER_RUN_DIR": "/tmp/cbk",
        "SANDBOX_IMAGE": "collegue-sandbox-broker:ci",
    }
    for name, value in expected.items():
        assert env[name].lower() == value, name
    assert env["INTEGRATION_E2E_ENABLED"] == "${{ vars.INTEGRATION_E2E_ENABLED }}", (
        "lu par le préflight pour REFUSER la récurrence"
    )
    assert not [k for k in env if k.startswith("LLM_PRICE") or "INTEGRATION_LLM" in env[k]], (
        "aucun prix ni modèle fournis par des variables de dépôt : l'enveloppe est écrite ici et dans le préflight"
    )


def test_the_provider_key_exists_only_in_the_campaign_step_under_one_name_and_never_at_job_level(job, raw):
    holders = [s["name"] for s in job["steps"] if any(re.search(r"API_KEY", k) for k in s.get("env", {}))]
    assert holders == [CAMPAIGN_STEP]
    campaign = step_named(job, "Campagne réelle")
    assert set(campaign["env"]) == {"GITHUB_TOKEN", "LLM_API_KEY"}
    assert campaign["env"]["LLM_API_KEY"] == "${{ secrets.W5_GOOGLE_API_KEY }}", (
        "LLM_API_KEY : le seul nom lu par les réglages du produit"
    )
    assert not any("secrets." in str(v) for v in job["env"].values()), "aucun secret au niveau du job"
    for step in job["steps"]:
        assert "secrets." not in step.get("run", ""), "les secrets passent par env:, jamais par le script"
    assert raw.count("secrets.W5_GOOGLE_API_KEY") == 1
    for stale in ("INTEGRATION_LLM_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        assert stale not in raw, f"{stale} : l'ancienne clé du nightly ne sert pas à cette campagne"
    assert not re.search(r"(?<!W5_)GOOGLE_API_KEY", raw), (
        "GOOGLE_API_KEY est ignoré par les réglages du produit : le nom est LLM_API_KEY"
    )


def test_preflights_receive_only_the_read_token_and_run_before_anything_paid(job):
    names = [s.get("name", s.get("uses", "")) for s in job["steps"]]
    static, full = step_named(job, "Préflight statique"), step_named(job, "Préflight complet")
    campaign = step_named(job, "Campagne réelle")
    assert set(static["env"]) == set(full["env"]) == {"GITHUB_TOKEN"}
    assert names.index(static["name"]) < names.index(full["name"]) < names.index(campaign["name"])
    assert "--stage static" in static["run"] and "--stage full" in full["run"]
    assert full["if"] == "steps.preflight_static.outcome == 'success'"
    assert campaign["if"] == "steps.preflight_full.outcome == 'success'", (
        "la campagne ne part QUE si le préflight complet a réussi"
    )
    image = step_named(job, "Construire l'image du sandbox")
    assert image["if"] == "steps.preflight_static.outcome == 'success'"
    assert names.index(static["name"]) < names.index(image["name"]) < names.index(full["name"])


def test_the_campaign_runs_once_and_a_leaked_key_invalidates_it(job):
    run = step_named(job, "Campagne réelle")["run"]
    assert run.count("w4_business run") == 1, "une seule exécution, aucune boucle ni relance"
    assert not re.search(r"\b(while|until)\b|\bfor\b|\|\|", run.replace("--campaign-id", ""))
    assert "python scripts/w5_leak_scan.py --env LLM_API_KEY" in run, (
        "valeur lue dans l'environnement par le scanner, jamais dans l'argv"
    )
    assert "grep" not in run and "-I" not in run.split("w5_leak_scan.py", 1)[1].split("\n")[0], (
        "ni grep (valeur dans l'argv), ni -I (binaires ignorés)"
    )
    assert "$LLM_API_KEY" not in run and "${LLM_API_KEY" not in run, (
        "la valeur de la clé n'est jamais développée dans une commande"
    )
    assert '--quarantine "$W5_QUARANTINE"' in run and '--stage "$W5_PUBLISH"' in run, (
        "ensemble publiable construit par le scanner"
    )
    assert '--source "report=$W4_REPORT_DIR"' in run and '--source "registry=$COLLEGUE_HOME::*.sqlite3*"' in run
    assert 'if [ "$scan" -ne 0 ]; then status=1; fi' in run, "toute fuite ou analyse incomplète rend la campagne rouge"
    assert run.rstrip().endswith('exit "$status"'), "le code de retour de la campagne est conservé"


def test_cleanup_always_runs_with_the_fixture_token_only_and_only_the_verified_set_is_ever_uploaded(job, raw):
    cleanup = step_named(job, "Nettoyage idempotent")
    assert cleanup["if"].startswith("always()") and set(cleanup["env"]) == {"GITHUB_TOKEN"}
    assert cleanup["run"].strip() == "python -m collegue.pilot.w4_business cleanup"
    uploads = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
    assert [u["with"]["name"] for u in uploads] == [
        "w5-business-report",
        "w5-business-registry",
        "w5-business-diagnostic",
    ]
    assert all(u["if"] == "always()" for u in uploads)
    paths = {u["with"]["name"]: u["with"]["path"] for u in uploads}
    assert paths["w5-business-report"] == "${{ env.W5_PUBLISH }}/report"
    assert paths["w5-business-registry"] == "${{ env.W5_PUBLISH }}/registry"
    assert paths["w5-business-diagnostic"] == "${{ env.W5_DIAG }}"
    # JAMAIS un répertoire source (rapport vivant, registre vivant) : ce que le nettoyage ou une commande interrompue écrivent n'est pas publié
    for path in paths.values():
        assert "W4_REPORT_DIR" not in path and "COLLEGUE_HOME" not in path
    assert "upload-artifact" not in raw.replace("actions/upload-artifact@v4", "")
    names = [s.get("name", "") for s in job["steps"]]
    assert all(names.index(cleanup["name"]) < names.index(u["name"]) for u in uploads)


def test_the_publishable_set_is_built_only_by_the_scanner_and_a_skipped_campaign_proves_the_key_never_existed(job):
    skipped = step_named(job, "Préparer les artefacts publiables")
    assert skipped["if"] == "always() && steps.campaign.outcome == 'skipped'", (
        "preuve positive : l'étape qui reçoit la clé n'a pas tourné"
    )
    assert "--assume-no-key" in skipped["run"] and "--env" not in skipped["run"] and "env" not in skipped
    campaign = step_named(job, "Campagne réelle")
    assert "w5_leak_scan.py --env LLM_API_KEY" in campaign["run"]
    for step in job["steps"]:
        if step not in (campaign, skipped) and "w5_leak_scan" in step.get("run", ""):
            raise AssertionError("seules ces deux étapes construisent l'ensemble publiable")


def test_the_minimal_diagnostic_is_built_from_step_outcomes_and_validated_ids_only(job):
    diag = step_named(job, "Diagnostic minimal sûr")
    assert diag["if"] == "always()" and set(diag["env"]) == {"OUTCOME_STATIC", "OUTCOME_FULL", "OUTCOME_CAMPAIGN"}
    assert all(v.startswith("${{ steps.") and v.endswith(".outcome }}") for v in diag["env"].values())
    run = diag["run"]
    assert (
        "$W5_REPORT" not in run and "cat " not in run and "$W4_REPORT_DIR" not in run and "$COLLEGUE_HOME" not in run
    ), "jamais dérivé d'un fichier"
    assert "LLM_API_KEY" not in run and "secrets." not in run


def test_the_campaign_id_is_validated_before_it_reaches_the_environment_file_and_the_registry_is_private(job):
    first = job["steps"][0]
    assert first["run"].index("campaign_id invalide") < first["run"].index("GITHUB_ENV")
    assert re.search(r"\^\[a-z0-9\]\[a-z0-9-\]\{2,39\}\$", first["run"])
    assert (
        "STATE_DATABASE_URL=sqlite:///%s/home/%s.sqlite3" in first["run"] and "W4_BUSINESS_CAMPAIGN_ID" in first["run"]
    )
    assert "W5_BOOTSTRAP_MANIFEST=%s/bootstrap-manifest.json" in first["run"]
    assert first["env"] == {"W5_BOOTSTRAP_MANIFEST_JSON": "${{ vars.W5_BOOTSTRAP_MANIFEST_JSON }}"}, (
        "variable non secrète"
    )
    migrate = step_named(job, "Initialiser le registre durable")
    assert migrate["run"].strip() == "python -m collegue.migrations upgrade"
    assert job["steps"].index(migrate) < job["steps"].index(step_named(job, "Préflight statique"))


def test_the_other_workflows_never_reference_the_campaign_key_or_environment_and_the_nightly_stays_off():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        if path == CAMPAIGN:
            continue
        text = path.read_text(encoding="utf-8")
        assert "W5_GOOGLE_API_KEY" not in text and "w5-gemma-campaign" not in text, path.name
        assert "GOOGLE_API_KEY" not in text, f"{path.name} : aucune clé fournisseur hors de la campagne"
    nightly = yaml.safe_load((WORKFLOWS / "integration-nightly.yml").read_text(encoding="utf-8"))
    assert nightly["jobs"]["product-e2e"]["if"] == "vars.INTEGRATION_E2E_ENABLED == 'true'"
    assert 'INTEGRATION_E2E_ENABLED: "true"' not in CAMPAIGN.read_text(encoding="utf-8")


def test_the_general_ci_uses_no_real_model_and_keeps_the_five_required_check_names():
    text = (WORKFLOWS / "tests.yml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    names = sorted(job["name"] for job in parsed["jobs"].values())
    assert names == ["Dependency audit", "Docker build", "Pytest (Python ${{ matrix.python-version }})", "Ruff"]
    assert parsed["jobs"]["pytest"]["strategy"]["matrix"]["python-version"] == ["3.11", "3.12"]
    assert "secrets." not in text.replace("secrets.GITHUB_TOKEN", ""), (
        "aucun secret (donc aucun modèle réel) dans la CI générale"
    )


def test_the_campaign_uses_the_audited_broker_image_and_builds_it_before_the_full_preflight(job):
    names = [s.get("name", "") for s in job["steps"]]
    build = step_named(job, "Construire l'image du sandbox")
    assert "docker/sandbox/Dockerfile.broker" in build["run"] and "Dockerfile.openhands" not in build["run"]
    assert '-t "$SANDBOX_IMAGE"' in build["run"] and job["env"]["SANDBOX_IMAGE"].startswith("collegue-sandbox-broker:")
    assert names.index(build["name"]) < names.index(step_named(job, "Préflight complet")["name"])
    assert "BROKER_RUN_DIR" in step_named(job, "Initialiser les chemins")["run"], (
        "racine COURTE des sockets créée et privée"
    )
    assert "chmod 700" in step_named(job, "Initialiser les chemins")["run"]


# ── exécution RÉELLE du script bash de l'étape de campagne (le produit est remplacé par une doublure) ──────────────────────────────


def _run_campaign_step(job, tmp_path, *, campaign_rc, leak_into, name_leak=False):
    import os
    import subprocess
    import sys

    step = step_named(job, "Campagne réelle")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "python"
    shim.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "-m" ] && [ "$2" = "collegue.pilot.w4_business" ]; then\n'
        '  echo "{}" > "$W4_REPORT_DIR/campaign.json"; echo ok > "$W4_REPORT_DIR/campaign.txt"\n'
        '  printf "registre" > "$COLLEGUE_HOME/camp.sqlite3"; printf "workspace" > "$COLLEGUE_HOME/workspace.txt"\n'
        '  if [ -n "$FAKE_LEAK_INTO" ]; then printf "xx%s\\0yy" "$LLM_API_KEY" > "$FAKE_LEAK_INTO"; fi\n'
        '  if [ -n "$FAKE_NAME_LEAK" ]; then echo x > "$W4_REPORT_DIR/$LLM_API_KEY.log"; fi\n'
        '  exit "$FAKE_RC"\n'
        "fi\n"
        f'exec {sys.executable} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    root = tmp_path / "w5"
    report, home = root / "report", root / "home"
    for folder in (report, home, root / "diagnostic"):
        folder.mkdir(parents=True)
    key = "FAKE-" + "step-key-0123456789abcdef"
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "W4_REPORT_DIR": str(report),
        "COLLEGUE_HOME": str(home),
        "W5_PUBLISH": str(root / "publish"),
        "W5_DIAG": str(root / "diagnostic"),
        "W5_QUARANTINE": str(root / "quarantine"),
        "W4_BUSINESS_CAMPAIGN_ID": "w5-test-001",
        "LLM_API_KEY": key,
        "FAKE_RC": str(campaign_rc),
        "FAKE_LEAK_INTO": str(leak_into(report, home)) if leak_into else "",
        "FAKE_NAME_LEAK": "1" if name_leak else "",
    }
    done = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step["run"]], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60
    )
    return done, root, key


def _published(root):
    publish = root / "publish"
    return sorted(str(p.relative_to(publish)) for p in publish.rglob("*") if p.is_file()) if publish.exists() else []


@pytest.mark.parametrize(
    ("campaign_rc", "leak", "name_leak", "expected_rc", "published"),
    [
        (
            0,
            None,
            False,
            0,
            ["registry/camp.sqlite3", "report/campaign.json", "report/campaign.txt"],
        ),  # sain : tout est publié
        (
            3,
            None,
            False,
            3,
            ["registry/camp.sqlite3", "report/campaign.json", "report/campaign.txt"],
        ),  # code de retour conservé
        (
            0,
            lambda r, h: r / "campaign.txt",
            False,
            1,
            ["registry/camp.sqlite3", "report/campaign.json"],
        ),  # fuite dans un rapport
        (
            0,
            lambda r, h: h / "camp.sqlite3",
            False,
            1,
            ["report/campaign.json", "report/campaign.txt"],
        ),  # fuite dans le registre BINAIRE
        (
            3,
            lambda r, h: r / "campaign.txt",
            False,
            1,
            ["registry/camp.sqlite3", "report/campaign.json"],
        ),  # fuite + code non nul
        (
            0,
            None,
            True,
            1,
            ["registry/camp.sqlite3", "report/campaign.json", "report/campaign.txt"],
        ),  # fuite dans un NOM de fichier
    ],
)
def test_the_real_campaign_step_script_publishes_only_verified_files_and_turns_any_leak_into_a_red_run(
    job, tmp_path, campaign_rc, leak, name_leak, expected_rc, published
):
    done, root, key = _run_campaign_step(job, tmp_path, campaign_rc=campaign_rc, leak_into=leak, name_leak=name_leak)
    assert done.returncode == expected_rc, done.stdout + done.stderr
    assert _published(root) == published, (
        "seuls des fichiers vérifiés sont publiés ; le registre ne contient pas les espaces de travail"
    )
    scan = json.loads((root / "diagnostic" / "leak-scan.json").read_text())
    assert scan["verdict"] == ("leak" if (leak or name_leak) else "clean")
    everything = done.stdout + done.stderr + (root / "diagnostic" / "leak-scan.json").read_text()
    assert key not in everything, "la valeur n'est jamais affichée ni écrite, noms de fichiers compris"
    assert not any(key in p for p in _published(root))


def test_a_crashing_scanner_or_a_missing_key_never_publishes_an_unverified_file(job, tmp_path):
    import os
    import subprocess

    # scanner qui plante : la commande rend un code non nul et RIEN n'est publié (aucun fichier n'a été vérifié)
    step = step_named(job, "Campagne réelle")
    root = tmp_path / "w5"
    for folder in ("report", "home", "diagnostic"):
        (root / folder).mkdir(parents=True)
    (root / "report" / "campaign.json").write_text("{}")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "python"
    shim.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "-m" ]; then exit 0; fi\n'
        'if [ "$1" = "scripts/w5_leak_scan.py" ]; then kill -9 $$; fi\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "W4_REPORT_DIR": str(root / "report"),
        "COLLEGUE_HOME": str(root / "home"),
        "W5_PUBLISH": str(root / "publish"),
        "W5_DIAG": str(root / "diagnostic"),
        "W5_QUARANTINE": str(root / "quarantine"),
        "W4_BUSINESS_CAMPAIGN_ID": "w5-test-001",
        "LLM_API_KEY": "FAKE-crash-key-0123456789",
    }
    done = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step["run"]], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60
    )
    assert done.returncode != 0 and _published(root) == []
