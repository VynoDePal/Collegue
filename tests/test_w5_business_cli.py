"""Câblage de ``main run`` (W5) : revendication, phases R04 / R05 sur les services de production, nettoyage unique, échéance.

Les frontières externes (GitHub, commandes produit, préflight déjà jugé) sont des doubles ; l'ORDRE et les paramètres que
``main`` transmet à ``run_campaign`` sont ceux de production.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_w4_business_launch import ENV, GOOD_COUNTERS, FakeAdapter, validated_preflight

from collegue.pilot import w4_business as business
from collegue.pilot import w5_business as w5


@pytest.fixture
def wired(monkeypatch, tmp_path):
    events = []
    adapter = FakeAdapter(tmp_path)
    original_cleanup = adapter.cleanup
    adapter.cleanup = lambda: (events.append("cleanup"), original_cleanup())[1]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(business, "_real_preflight", lambda env, campaign_id, stage: validated_preflight())
    monkeypatch.setattr(business, "_fixture_clients", lambda token: "clients")
    monkeypatch.setattr(business, "NightlyAdapter", lambda config, clients, runner: adapter)
    monkeypatch.setattr(business, "registry_reader", lambda env: lambda context: dict(GOOD_COUNTERS))
    monkeypatch.setattr(business, "verify_in_container", lambda report, context, **kw: events.append(("verify", kw)))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"bootstrap_sha": "b" * 40}), encoding="utf-8")
    services = SimpleNamespace(name="services-de-production")
    seen = {}

    def production_services(**kwargs):
        seen["services_kwargs"] = kwargs
        return services

    monkeypatch.setattr(w5, "load_bootstrap_manifest", lambda path: {"bootstrap_sha": "b" * 40, "path": path})
    monkeypatch.setattr(w5, "production_services", production_services)
    monkeypatch.setattr(
        w5, "revalidate_and_claim", lambda clients, env, campaign_id, report: events.append(("claim", campaign_id))
    )
    monkeypatch.setattr(
        w5, "activate_budget", lambda env, campaign_id, report, **kwargs: events.append(("activate", campaign_id))
    )
    monkeypatch.setattr(
        w5,
        "materialize_spec_for_launch",
        lambda **kwargs: events.append(("spec", kwargs["project_id"], kwargs["deadline"])),
    )
    monkeypatch.setattr(
        w5, "cleanup_campaign_resources", lambda report, **kwargs: events.append(("resources", sorted(kwargs))) or {}
    )
    monkeypatch.setattr(w5, "run_improvement_phase", lambda report, context, svc: events.append(("R04", svc)))
    monkeypatch.setattr(w5, "run_incident_phase", lambda report, context, svc: events.append(("R05", svc)))
    env = {
        **ENV,
        **business.campaign_environment("cli-w5", str(tmp_path)),
        "W5_BOOTSTRAP_MANIFEST": str(manifest),
        "GITHUB_RUN_ID": "777",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    monkeypatch.setattr(os, "environ", env)
    return SimpleNamespace(events=events, adapter=adapter, services=services, seen=seen, tmp=tmp_path)


def run_main(tmp_path, *extra):
    output = tmp_path / "report.json"
    with contextlib.redirect_stdout(io.StringIO()):
        code = business.main(["run", "--campaign-id", "w5-camp-001", "--output", str(output), *extra])
    return code, json.loads(output.read_text())


def test_main_claims_the_identity_then_runs_both_phases_on_production_services_then_cleans_once(wired):
    code, report = run_main(wired.tmp)

    kinds = [e if isinstance(e, str) else e[0] for e in wired.events]
    assert kinds.count("cleanup") == 1 and kinds[-1] == "cleanup", kinds
    assert (
        kinds.index("claim")
        < kinds.index("activate")
        < kinds.index("spec")
        < kinds.index("verify")
        < kinds.index("R04")
        < kinds.index("R05")
        < kinds.index("resources")
        < kinds.index("cleanup")
    ), "la SPEC est matérialisée après l'activation et AVANT tout BUILD ; les ressources de campagne avant le nettoyage nightly"
    assert ("claim", "w5-camp-001") in wired.events, "l'identifiant de la campagne est revendiqué"
    assert ("R04", wired.services) in wired.events and ("R05", wired.services) in wired.events
    steps = {s["id"]: s["state"] for s in report["steps"]}
    assert steps["R04-improvement"] == steps["R05-incident-rollback"] == steps["R06-cleanup"] == "succeeded"
    assert code == 0 and report["verdict"] == "validated"
    assert report["facts"]["scope"]["not_wired"] == []


def test_main_gives_the_global_deadline_to_the_verification_and_to_the_production_services(wired):
    run_main(wired.tmp)

    verify_kwargs = next(e[1] for e in wired.events if isinstance(e, tuple) and e[0] == "verify")
    deadline = verify_kwargs["deadline_monotonic"]
    assert wired.seen["services_kwargs"]["deadline_monotonic"] == deadline, (
        "UNE seule échéance pour toutes les opérations"
    )
    assert wired.seen["services_kwargs"]["manifest"]["bootstrap_sha"] == "b" * 40
    assert wired.seen["services_kwargs"]["image"] == business.DEFAULT_VERIFIER_IMAGE


def test_main_removes_the_operator_checkout_at_the_late_cleanup_and_never_the_claim(wired):
    checkout_parent = wired.tmp / "operator"
    (checkout_parent / "fixture").mkdir(parents=True)
    original_clone = wired.adapter.clone
    wired.adapter.clone = lambda sha: (
        str(checkout_parent / "fixture") if not (checkout_parent / "used").exists() else original_clone(sha)
    )

    run_main(wired.tmp)

    assert not checkout_parent.exists(), "le clone de l'opérateur est supprimé au nettoyage, pas avant"


def test_a_cleanup_error_makes_main_fail_but_the_original_cause_remains_readable(wired):
    wired.adapter.cleanup = lambda: (_ for _ in ()).throw(RuntimeError("GitHub indisponible"))

    code, report = run_main(wired.tmp)

    assert code == 1 and report["verdict"] == "failed" and report["facts"]["verdict_before_cleanup"] == "validated"
    assert "GitHub indisponible" in next(s["detail"] for s in report["steps"] if s["id"] == "R06-cleanup")


def test_main_run_still_refuses_the_static_stage(wired):
    with pytest.raises(SystemExit) as stop, contextlib.redirect_stderr(io.StringIO()):
        business.main(["run", "--stage", "static", "--campaign-id", "w5-camp-001"])
    assert stop.value.code == 2 and Path(wired.tmp).exists()


def test_the_durable_window_reported_by_the_activation_only_tightens_the_campaign_deadline(wired, monkeypatch):
    import time

    started = time.monotonic()
    monkeypatch.setattr(w5, "activate_budget", lambda env, campaign_id, report, **kwargs: kwargs["on_remaining"](5.0))

    run_main(wired.tmp)

    verify_kwargs = next(e[1] for e in wired.events if isinstance(e, tuple) and e[0] == "verify")
    assert verify_kwargs["deadline_monotonic"] <= started + 5.0 + 2.0, "l'échéance durable resserre la fenêtre"
    assert wired.seen["services_kwargs"]["deadline_monotonic"] == verify_kwargs["deadline_monotonic"]


def test_a_longer_durable_window_never_extends_the_campaign_deadline(wired, monkeypatch):
    import time

    started = time.monotonic()
    monkeypatch.setattr(
        w5, "activate_budget", lambda env, campaign_id, report, **kwargs: kwargs["on_remaining"](10_000.0)
    )

    run_main(wired.tmp)

    verify_kwargs = next(e[1] for e in wired.events if isinstance(e, tuple) and e[0] == "verify")
    assert verify_kwargs["deadline_monotonic"] <= started + business.CAMPAIGN_BOUNDS.max_seconds + 2.0


def test_collection_and_cleanup_continue_after_the_generation_deadline_without_generating(wired, monkeypatch):
    """Le nettoyage ne génère rien : son exécuteur de commandes a SA fenêtre, il n'hérite pas de l'échéance de 900 s expirée."""
    runners = []
    monkeypatch.setattr(
        business, "NightlyAdapter", lambda config, clients, runner: (runners.append(runner), wired.adapter)[1]
    )
    monkeypatch.setattr(w5, "activate_budget", lambda env, campaign_id, report, **kwargs: kwargs["on_remaining"](0.0))

    run_main(wired.tmp)

    generation_runner, cleanup_runner = runners[0], runners[-1]
    assert generation_runner is not cleanup_runner, "le nettoyage a son propre exécuteur"
    with pytest.raises(business.BudgetStop, match="échéance globale"):
        generation_runner(["true"])
    assert cleanup_runner(["true"]).returncode == 0, "le nettoyage et la collecte continuent après l'échéance"


# ── ciblage des passes publiques : réglage produit resserré, jamais élargi ───────────────────────────────────────────────────


async def test_the_production_pass_narrows_the_product_allowlist_to_the_phase_target_and_changes_nothing_else(
    monkeypatch, tmp_path
):
    calls = []

    async def fake_entry(project_id, repo_source, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(stop_reason="done")

    import collegue.pilot as pilot

    monkeypatch.setattr(pilot, "run_project_from_settings", fake_entry)
    env = business.campaign_environment("tg", str(tmp_path))
    run = w5.production_run_pass(owner="o", repo="r", env=env)
    context = {"project_id": 3, "operator_checkout": str(tmp_path), "base_branch": "collegue-business/x"}

    await run(context, improve=True, agent=None, path_allowlist=w5.R04_ALLOWLIST)
    await run(context, improve=True, agent="incident", path_allowlist=w5.INCIDENT_ALLOWLIST)
    await run(context, improve=False)

    narrowed, incident, plain = calls
    assert narrowed["settings_obj"].AUTO_MERGE_PATH_ALLOWLIST == w5.R04_DOC, (
        "R04 ne peut fusionner que le runbook cible"
    )
    assert (
        incident["settings_obj"].AUTO_MERGE_PATH_ALLOWLIST == ",".join(w5.INCIDENT_DOCS)
        and incident["agent"] == "incident"
    )
    assert "settings_obj" not in plain and "agent" not in plain, (
        "sans ciblage : réglages du processus, aucun agent injecté"
    )
    for kwargs in (narrowed, incident):
        settings = kwargs["settings_obj"]  # le reste de la politique est celui de l'environnement validé, non élargi
        assert settings.AUTO_MERGE_ENABLED is True and settings.AUTO_REVERT_ENABLED is True
        assert settings.AUTO_MERGE_MAX_LOC == 50 and settings.AUTO_MERGE_METHOD == "squash"
        assert settings.BUDGET_MODE == "strict"  # (LLM_TRANSPORT : réglage du lot A, absent de cette branche)
    assert all(kwargs["dry_run"] is False and kwargs["owner"] == "o" for kwargs in calls)


async def test_a_targeted_pass_without_the_validated_environment_is_refused_not_run_untargeted():
    run = w5.production_run_pass(owner="o", repo="r")
    with pytest.raises(w5.IncompleteValidation, match="ciblage de la passe"):
        await run(
            {"project_id": 1, "operator_checkout": "/x", "base_branch": "b"},
            improve=True,
            path_allowlist=w5.R04_ALLOWLIST,
        )
