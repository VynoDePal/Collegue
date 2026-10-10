"""Monde de test W5 : trois tâches BUILD livrées sur un socle d'exemples, puis les phases R04 / R05 de la campagne réelle.

Frontières simulées (et seulement elles) : transport de planification, codeur de BUILD, « modèle » de R04 (un codeur déterministe
qui joue le rôle du modèle), revue, GitHub (vrai dépôt Git derrière les vrais clients). Les phases testées sont celles de
PRODUCTION (``w5_business.run_improvement_phase`` / ``run_incident_phase``) ; mesure, contrats, politique de fusion, checks,
santé INDÉPENDANTE (la vraie commande autonome), revert, acquittement et reprise sont ceux du produit.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import w4_business_campaign as harness
import w4_business_fixture as fixture

from collegue.pilot import w4_business as business
from collegue.pilot import w5_business as w5

OWNER = REPO = harness.OWNER


async def _deliver_three_tasks(world: harness.World) -> None:
    report = business.CampaignReport("deterministic", "w5-world")
    for number in (1, 2, 3):
        step = report.declare(f"T{number}", f"tâche {number}")
        if number == 1:
            await harness.plan(world, report.declare("plan", "plan"))
            harness.approve(world, report.declare("approve", "approbation"))
        await harness.deliver_task(world, number, step)


def delivered_world(root: Path) -> harness.World:
    """Socle + trois tâches BUILD réellement livrées, prouvées et fusionnées (oracles rouges par assertion puis verts)."""
    world = harness.build_world(Path(root), socle=True)
    asyncio.run(_deliver_three_tasks(world))
    return world


def context_for(world: harness.World) -> Dict[str, Any]:
    return {"project_id": world.project_id, "base_branch": "main", "operator_checkout": world.source}


def campaign_report(*, with_phases: bool = True) -> business.CampaignReport:
    report = business.CampaignReport("campaign", "w5-test")
    for step_id in ("R04-improvement", "R05-incident-rollback"):
        report.declare(step_id, step_id)
    return report


class ModelStandIn(harness.ImprovementAgent):
    """Remplaçant DÉTERMINISTE du modèle pour R04 (en production : le vrai modèle, ``agent=None``). Retire les identifiants
    d'exemple du runbook — jamais le support d'incident."""

    def __init__(self, contents: Optional[Dict[str, str]] = None):
        super().__init__(harness.replace_docs(contents or {w5.R04_DOC: fixture.CLEAN_RUNBOOK_DOC}))


def services_for(
    world: harness.World,
    *,
    model: Optional[harness.ImprovementAgent] = None,
    measure_fn: Any = None,
    reviewer: Any = None,
    deadline_monotonic: Optional[float] = None,
    clock: Any = None,
    manifest: Optional[Dict[str, Any]] = None,
    settings_overrides: Optional[Dict[str, Any]] = None,
) -> w5.PhaseServices:
    """Services de phase sur les ENTRÉES PUBLIQUES (``run_project_from_settings``) et les vrais clients du pont Git."""
    model = model or ModelStandIn()
    calls: list = []
    overrides = {"AUTO_REVERT_HEALTH_COMMAND": business.health_command(), **(settings_overrides or {})}

    async def run_pass(context: Any, *, improve: bool, agent: Any = None, path_allowlist: Any = ()) -> Any:
        targeted = {**overrides, **({"AUTO_MERGE_PATH_ALLOWLIST": ",".join(path_allowlist)} if path_allowlist else {})}
        calls.append({"improve": improve, "path_allowlist": tuple(path_allowlist)})
        return await harness.improvement_pass(
            world,
            agent or model,
            measure_fn=measure_fn,
            settings_overrides=targeted,
            reviewer=reviewer,
            improve=improve,
        )

    async def resume(context: Any) -> Any:
        targeted = {**overrides, "AUTO_MERGE_PATH_ALLOWLIST": ",".join(w5.INCIDENT_ALLOWLIST)}
        _promotion, recovery, _budget = harness.phase5_hooks(world, harness.improvement_settings(world, **targeted))
        return await recovery()

    def verify_tip(context: Any, sha: str) -> Any:
        head = harness.git(world.source, "rev-parse", "HEAD")
        assert head == sha, f"le checkout de l'opérateur doit être resynchronisé sur {sha[:12]} (vu {head[:12]})"
        return business.verify_business_checkout(
            world.source, python=sys.executable, runner=business.trusted_local_runner
        )

    from collegue.pilot import w5_business_ownership as ownership

    owned_manifest = str(world.root / "owned-manifest.json")

    def record_owned(*, project_id: int, event: str, **fields: Any) -> None:
        identity = ownership.identity_of(OWNER, REPO, "main", project_id=int(project_id))
        ownership.append_event(owned_manifest, identity, event, **fields)

    services = w5.PhaseServices(
        run_pass=run_pass,
        clients=world.bridge.clients(),
        manager=world.manager,
        resume_incident=resume,
        verify_tip=verify_tip,
        owner=OWNER,
        repo=REPO,
        deadline_monotonic=deadline_monotonic,
        clock=clock,
        manifest=manifest,
        required_checks=tuple(harness_checks()),
        record_owned=record_owned,
    )
    services.owned_manifest = owned_manifest  # type: ignore[attr-defined]
    services.pass_calls = calls  # type: ignore[attr-defined] - journal des ciblages demandés aux passes publiques
    return services


def harness_checks():
    """Checks requis par la protection du pont de test (les cinq checks du dépôt)."""
    from github_fake_server import FIVE_CHECKS

    return FIVE_CHECKS


def run_phase(
    phase: Any, report: business.CampaignReport, context: Dict[str, Any], services: w5.PhaseServices, step_id: str
):
    """Joue UNE phase au travers de ``CampaignReport.run`` (mêmes états que la campagne réelle) et rend l'étape."""
    report.run(step_id, lambda step: phase(report, context, services))
    return report.step(step_id)


def git_tip(world: harness.World) -> str:
    return world.bridge.branches["main"]


def tree_of(world: harness.World, sha: str) -> str:
    return world.bridge.remote.tree_of(sha)
