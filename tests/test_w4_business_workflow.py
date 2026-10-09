"""Workflow ponctuel de la campagne métier W4 : déclenchement unique, secrets confinés, enveloppe exacte, aucune récurrence.

Contrôles statiques du fichier YAML (rien n'est exécuté). Ils interdisent par construction : une planification, un déclencheur de
dépôt, une relance automatique, une clé de modèle hors de l'étape de campagne, une enveloppe différente de celle du préflight, et
l'activation de ``INTEGRATION_E2E_ENABLED``. Le workflow du nightly n'est PAS modifié par cette campagne.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from collegue.pilot import w4_business as business

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "consolidation-e2e.yml"
NIGHTLY = ROOT / ".github" / "workflows" / "integration-nightly.yml"


@pytest.fixture(scope="module")
def raw() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def wf(raw) -> dict:
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict)
    return parsed


@pytest.fixture(scope="module")
def job(wf) -> dict:
    assert list(wf["jobs"]) == ["campaign"], "un seul job : l'image du sandbox est partagée par les étapes"
    return wf["jobs"]["campaign"]


def step_named(job, prefix: str) -> dict:
    for step in job["steps"]:
        if step.get("name", "").startswith(prefix):
            return step
    raise AssertionError(f"étape introuvable: {prefix}")


def test_only_a_manual_dispatch_can_start_it_and_never_a_schedule_or_a_repository_event(wf, raw):
    triggers = wf[True]  # YAML 1.1 : la clé « on » est lue comme le booléen True
    assert list(triggers) == ["workflow_dispatch"]
    assert "cron" not in raw and "schedule" not in raw.replace("planification", "") and "pull_request" not in raw
    assert "repository_dispatch" not in raw and "workflow_run" not in raw and "workflow_call" not in raw


def test_the_dispatch_requires_the_exact_confirmation_and_a_campaign_id(wf, job):
    inputs = wf[True]["workflow_dispatch"]["inputs"]
    assert inputs["confirm"]["required"] is True and inputs["campaign_id"]["required"] is True
    assert business.LAUNCH_CONFIRMATION in inputs["confirm"]["description"]
    assert business.LAUNCH_CONFIRMATION in job["if"] and "workflow_dispatch" in job["if"]
    assert job["env"]["W4_BUSINESS_CONFIRM"] == "${{ inputs.confirm }}"


def test_permissions_are_read_only_and_runs_never_overlap_nor_get_cancelled_midway(wf, job):
    assert wf["permissions"] == {"contents": "read"} and "permissions" not in job
    assert wf["concurrency"] == {"group": "consolidation-e2e", "cancel-in-progress": False}
    assert job["timeout-minutes"] <= 45, "bornage dur au-delà de l'échéance de 900 s"


def test_the_job_envelope_is_exactly_the_one_the_preflight_enforces(job):
    env = {k: str(v) for k, v in job["env"].items()}
    for name, expected in business.CAMPAIGN_SETTINGS.items():
        assert env[name].lower() == expected, name
    assert (env["MAX_COST_USD"], env["MAX_TOKENS_BUDGET"], env["COLLEGUE_RUN_DEADLINE_SECONDS"]) == (
        "2",
        "250000",
        "900",
    )
    assert env["INTEGRATION_E2E_ENABLED"] == "${{ vars.INTEGRATION_E2E_ENABLED }}", (
        "lu par le préflight pour REFUSER la récurrence"
    )
    assert "true" not in {v.lower() for k, v in env.items() if k == "INTEGRATION_E2E_ENABLED"}


def test_the_model_key_exists_only_in_the_campaign_step_and_never_at_job_level_or_in_a_script(job, raw):
    holders = [
        s["name"] for s in job["steps"] if any("LLM_API_KEY" in k or "GEMINI_API_KEY" in k for k in s.get("env", {}))
    ]
    assert holders == ["Campagne réelle (SEULE étape qui reçoit la clé du fournisseur de modèle)"]
    assert not any("secrets." in str(v) for v in job["env"].values()), "aucun secret au niveau du job"
    for step in job["steps"]:
        assert "secrets." not in step.get("run", ""), "les secrets passent par env:, jamais par le script"


def test_preflights_receive_only_the_read_token_and_run_before_anything_paid(job):
    names = [s.get("name", s.get("uses", "")) for s in job["steps"]]
    static, full = step_named(job, "Préflight statique"), step_named(job, "Préflight complet")
    campaign = step_named(job, "Campagne réelle")
    assert set(static["env"]) == set(full["env"]) == {"GITHUB_TOKEN"}
    assert names.index(static["name"]) < names.index(full["name"]) < names.index(campaign["name"])
    assert "--stage static" in static["run"] and "--stage full" in full["run"]
    assert static["id"] == "preflight_static" and full["id"] == "preflight_full"
    assert full["if"] == "steps.preflight_static.outcome == 'success'"
    assert campaign["if"] == "steps.preflight_full.outcome == 'success'", (
        "la campagne ne part QUE si le préflight complet a réussi"
    )
    image = step_named(job, "Construire l'image du sandbox")
    assert image["if"] == "steps.preflight_static.outcome == 'success'"
    assert names.index(static["name"]) < names.index(image["name"]) < names.index(full["name"])


def test_nothing_is_tolerated_or_retried(job, raw):
    assert "continue-on-error" not in raw
    assert not any(
        "retry" in str(s.get("uses", "")).lower() or "nick-fields" in str(s.get("uses", "")) for s in job["steps"]
    )
    campaign = step_named(job, "Campagne réelle")
    assert (
        campaign["run"].count("w4_business run") == 1
        and "for " not in campaign["run"]
        and "while " not in campaign["run"]
    )
    assert "||" not in campaign["run"]


def test_the_campaign_id_is_validated_before_it_reaches_the_environment_file(job):
    first = job["steps"][0]
    assert first["run"].index("campaign_id invalide") < first["run"].index("GITHUB_ENV")
    assert re.search(r"\^\[a-z0-9\]\[a-z0-9-\]\{2,39\}\$", first["run"])


def test_cleanup_always_runs_with_the_fixture_token_only_and_the_report_is_always_uploaded(job):
    cleanup = step_named(job, "Nettoyage idempotent")
    assert cleanup["if"].startswith("always()") and set(cleanup["env"]) == {"GITHUB_TOKEN"}
    assert cleanup["run"].strip() == "python -m collegue.pilot.w4_business cleanup"
    upload = step_named(job, "Déposer le rapport")
    assert upload["if"] == "always()" and upload["uses"].startswith("actions/upload-artifact@")
    assert job["steps"].index(cleanup) < job["steps"].index(upload)


def test_the_durable_registry_is_migrated_empty_and_private_to_the_campaign(job):
    init, migrate = job["steps"][0], step_named(job, "Initialiser le registre durable")
    assert "STATE_DATABASE_URL=sqlite:///%s/home/%s.sqlite3" in init["run"] and "W4_BUSINESS_CAMPAIGN_ID" in init["run"]
    assert migrate["run"].strip() == "python -m collegue.migrations upgrade"
    assert job["steps"].index(migrate) < job["steps"].index(step_named(job, "Préflight statique"))


def test_the_nightly_workflow_is_untouched_by_this_campaign_and_still_disabled_by_default(wf):
    nightly = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))
    e2e = nightly["jobs"]["product-e2e"]
    assert e2e["if"] == "vars.INTEGRATION_E2E_ENABLED == 'true'", "le nightly n'est jamais activé par ce lot"
    assert "w4_business" not in NIGHTLY.read_text(encoding="utf-8")
    assert 'INTEGRATION_E2E_ENABLED: "true"' not in WORKFLOW.read_text(encoding="utf-8")
