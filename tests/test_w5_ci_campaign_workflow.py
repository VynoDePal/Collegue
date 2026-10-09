"""Workflow ponctuel de la campagne W5 : déclenchement unique, clé confinée, enveloppe exacte, rien de récurrent (statique).

Contrôles du fichier YAML seulement (rien n'est exécuté, aucun modèle). Ils complètent ceux que B tient sur le préflight
(``tests/test_w4_business_workflow.py``, propriété B) sans les remplacer : ici le contrat de C — environnement propre à la
campagne, clé uniquement dans l'étape de campagne et sous un seul nom, transport par le courtier, codeur sans réseau, registre
et rapports toujours déposés, aucune variable de prix, nightly intact.
"""

from __future__ import annotations

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
    assert set(campaign["env"]) == {"GITHUB_TOKEN", "GOOGLE_API_KEY"}
    assert campaign["env"]["GOOGLE_API_KEY"] == "${{ secrets.W5_GOOGLE_API_KEY }}"
    assert not any("secrets." in str(v) for v in job["env"].values()), "aucun secret au niveau du job"
    for step in job["steps"]:
        assert "secrets." not in step.get("run", ""), "les secrets passent par env:, jamais par le script"
    assert raw.count("secrets.W5_GOOGLE_API_KEY") == 1
    for stale in ("INTEGRATION_LLM_API_KEY", "GEMINI_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        assert stale not in raw, f"{stale} : l'ancienne clé du nightly ne sert pas à cette campagne"


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
    assert "grep -rIlF" in run and "$GOOGLE_API_KEY" in run and "rm -f" in run, "aucune clé dans un fichier déposé"
    assert run.rstrip().endswith('exit "$status"'), "le code de retour de la campagne est conservé"


def test_cleanup_always_runs_with_the_fixture_token_only_and_report_and_registry_are_always_uploaded(job):
    cleanup = step_named(job, "Nettoyage idempotent")
    assert cleanup["if"].startswith("always()") and set(cleanup["env"]) == {"GITHUB_TOKEN"}
    assert cleanup["run"].strip() == "python -m collegue.pilot.w4_business cleanup"
    uploads = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
    assert [u["with"]["name"] for u in uploads] == ["w5-business-report", "w5-business-registry"]
    assert all(u["if"] == "always()" for u in uploads)
    assert all(job["steps"].index(cleanup) < job["steps"].index(u) for u in uploads)
    assert uploads[1]["with"]["path"] == "${{ env.COLLEGUE_HOME }}", "le registre durable (budget, échéance, identité)"


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
