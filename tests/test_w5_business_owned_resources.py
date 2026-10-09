"""Propriété DURABLE des ressources de la campagne (B27) : le nettoyage ne touche QUE ce qu'un état durable de CETTE campagne désigne.

Un préfixe de branche, un marqueur de PR, la base commune et l'ascendance d'un commit sont publics : ils ne prouvent jamais l'appartenance.
Les scénarios POSITIFS créent les vraies preuves — preuves de livraison persistées par ``open_pr`` (BUILD et IMPROVE par les VRAIES entrées
publiques), cycles de fusion, registre d'appartenance écrit par le matérialiseur de SPEC avant chaque création distante — jamais un objet
déclaré propriétaire. Les scénarios NÉGATIFS posent des ressources étrangères que le texte public ferait passer pour siennes.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest
from test_improve_promotion import metrics
from test_w3_integration_build import FilesAgent, run_pass
from test_w3_integration_improve import FilesFeature, improve
from test_w5_business_public_launch import (  # noqa: F401
    BASE,
    CYCLE,
    ChainAdapter,
    chain,
    launch,
    materialize,
    materializer,
)
from test_w5_business_public_launch import drafted as _drafted

from collegue.pilot import w4_business as business
from collegue.pilot import w5_business as w5
from collegue.pilot import w5_business_ownership as ownership
from collegue.pilot import w5_business_policy as fixture_policy
from collegue.pilot import w5_business_spec as spec
from collegue.pilot.nightly_e2e import NightlyManifest, _load_manifest, _write_manifest
from collegue.state import ProjectStateManager
from collegue.tools.base import ToolExecutionError

HEAD = spec.head_branch_for(BASE)


@pytest.fixture(autouse=True)
def single_project_without_oracles(chain, monkeypatch):
    """UN seul projet porte la SPEC, le BUILD et l'amélioration (comme la campagne) ; sans oracles §4.7, la VRAIE livraison par
    ``open_pr`` est exercée avec un codeur déterministe (les oracles métier sont couverts ailleurs)."""
    import w4_business_campaign as harness

    from collegue.pilot import runtime

    settings = harness.campaign_settings(GATE_ACCEPTANCE_TESTS=False)
    monkeypatch.setattr(runtime, "_settings", lambda: settings)


def drafted(chain):
    if not hasattr(chain, "project"):
        chain.project = _drafted(chain)
    return chain.project


def trust(chain):
    return os.environ[fixture_policy.TRUST_ANCHOR_ENV]


def seed_manifest(chain, project_id=None, issues=()):
    """Le manifeste que ``launch_campaign`` écrit : base créée à son sommet initial (avant toute fusion de la campagne)."""
    config = chain.adapter.config
    manifest = NightlyManifest.for_config(config)
    manifest.base_creation_started = manifest.base_created = True
    manifest.base_sha = chain.bridge.branches[BASE]
    manifest.project_id = project_id
    manifest.issue_numbers = list(issues)
    _write_manifest(config.manifest_path, manifest)
    return manifest


def cleanup(chain, manager_env=True):
    env = {**chain.env, fixture_policy.TRUST_ANCHOR_ENV: trust(chain)}
    if not manager_env:
        env.pop("STATE_DATABASE_URL")
    return w5.cleanup_campaign_resources(
        business.CampaignReport("campaign", "unit"),
        clients=chain.bridge.clients(),
        config=chain.adapter.config,
        env=env,
    )


def events(chain, name=None):
    found = ownership.read_events(chain.adapter.config.manifest_path, repo="fixture/fixture")
    return [e for e in found if name is None or e["event"] == name]


def sync_operator(chain):
    from github_fakes import git

    git(chain.campaign.path, "fetch", "-q", "origin")
    git(chain.campaign.path, "reset", "-q", "--hard", f"origin/{BASE}")


# ── 1. branche de SPEC : l'intention n'est jamais une preuve de propriété ───────────────────────────────────────────────────────


def test_a_preexisting_foreign_spec_branch_is_refused_recorded_as_foreign_and_never_deleted(chain):
    project = drafted(chain)
    chain.bridge.branches[HEAD] = chain.bridge.branches[BASE]
    foreign = chain.bridge.write_remote_file(HEAD, "docs/foreign.md", "Travail préexistant étranger\n")

    with pytest.raises(spec.SpecMaterializationError, match="sans que cette campagne l'ait créée"):
        materialize(chain, project)
    done = spec.cleanup_spec_resources(chain.bridge.clients(), "fixture", "fixture", chain.adapter.config.manifest_path)

    assert chain.bridge.branches[HEAD] == foreign
    assert done["branches_etrangeres_conservees"] == [HEAD] and "branches_supprimees" not in done
    assert [e["event"] for e in events(chain)] == ["foreign_branch_seen"], (
        "aucune intention de création n'a été consignée"
    )


def test_a_foreign_branch_that_appears_during_the_creation_is_not_owned_either(chain):
    project = drafted(chain)
    original = chain.bridge._post

    def competing_post(path, data):
        if data.get("ref") == "refs/heads/" + HEAD:
            chain.bridge.branches[HEAD] = chain.bridge.branches[
                BASE
            ]  # acteur concurrent entre l'absence et la création
        return original(path, data)

    chain.bridge._post = competing_post
    with pytest.raises(spec.SpecMaterializationError, match="non possédée"):
        materialize(chain, project)
    spec.cleanup_spec_resources(chain.bridge.clients(), "fixture", "fixture", chain.adapter.config.manifest_path)

    assert HEAD in chain.bridge.branches, (
        "une branche apparue pendant la création n'est jamais supprimée par la campagne"
    )
    assert "SPEC.md" not in chain.bridge.remote.files_at(HEAD), "la SPEC n'est jamais écrite sur une branche étrangère"


def test_an_interrupted_creation_is_cleaned_when_its_exact_progress_is_proven(chain):
    project = drafted(chain)
    clients = chain.bridge.clients()
    chain.bridge.clients = lambda: clients
    real = clients.files.update_file
    clients.files.update_file = lambda *a, **k: (_ for _ in ()).throw(ToolExecutionError("panne avant l'écriture"))

    with pytest.raises(ToolExecutionError):
        materialize(chain, project)
    assert [e["event"] for e in events(chain)] == ["spec_branch_intent", "spec_branch_created"]
    assert chain.bridge.branches[HEAD] == chain.bridge.branches[BASE]

    done = spec.cleanup_spec_resources(clients, "fixture", "fixture", chain.adapter.config.manifest_path)

    assert done["branches_supprimees"] == [HEAD] and HEAD not in chain.bridge.branches
    clients.files.update_file = real


@pytest.mark.parametrize("crash", ["before-write", "before-pr"])
def test_an_interrupted_creation_is_resumed_under_exact_proofs_without_a_second_pr_or_merge(chain, crash):
    project = drafted(chain)
    clients = chain.bridge.clients()
    chain.bridge.clients = lambda: clients
    if crash == "before-write":
        real = clients.files.update_file
        clients.files.update_file = lambda *a, **k: (_ for _ in ()).throw(ToolExecutionError("panne"))
    else:
        real = clients.prs.create_pr
        clients.prs.create_pr = lambda *a, **k: (_ for _ in ()).throw(ToolExecutionError("panne"))
    with pytest.raises(ToolExecutionError):
        materialize(chain, project)
    if crash == "before-write":
        clients.files.update_file = real
    else:
        clients.prs.create_pr = real
    chain.bridge.calls.clear()

    outcome = materialize(chain, project)

    assert outcome.state == "merged" and sorted(chain.bridge.prs) == [101] and len(chain.bridge.merge_calls()) == 1
    writes = [c for c in chain.bridge.calls if c[0] == "PUT" and "/contents/" in c[1]]
    assert len(writes) == (1 if crash == "before-write" else 0), "reprise sans réécriture de ce qui était déjà écrit"
    assert [e["event"] for e in events(chain)].count("spec_branch_intent") == 1, (
        "une seule intention : l'absence n'est établie qu'une fois"
    )


def test_a_branch_moved_by_a_third_party_after_our_write_is_preserved_and_reported(chain):
    project = drafted(chain)
    clients = chain.bridge.clients()
    chain.bridge.clients = lambda: clients
    clients.prs.create_pr = lambda *a, **k: (_ for _ in ()).throw(ToolExecutionError("panne"))
    with pytest.raises(ToolExecutionError):
        materialize(chain, project)
    moved = chain.bridge.write_remote_file(HEAD, "docs/tiers.md", "Un tiers a poussé sur cette branche\n")

    with pytest.raises(spec.SpecMaterializationError, match="nettoyage incomplet"):
        spec.cleanup_spec_resources(clients, "fixture", "fixture", chain.adapter.config.manifest_path)

    assert chain.bridge.branches[HEAD] == moved


def test_the_owned_spec_branch_and_pull_request_of_a_run_are_removed_and_the_merged_pr_is_left(chain):
    business.launch_campaign(
        business.CampaignReport("campaign", "unit"),
        adapter=chain.adapter,
        env=chain.env,
        cycle_id=CYCLE,
        materialize_spec=materializer(chain),
    )
    assert [e["event"] for e in events(chain)] == [
        "spec_branch_intent", "spec_branch_created", "spec_written", "spec_pr", "spec_merged",
    ]  # fmt: skip

    done = spec.cleanup_spec_resources(chain.bridge.clients(), "fixture", "fixture", chain.adapter.config.manifest_path)

    assert done["branches_supprimees"] == [HEAD] and HEAD not in chain.bridge.branches
    assert chain.bridge.prs[101]["merged"], "la PR fusionnée n'est jamais touchée"
    again = spec.cleanup_spec_resources(
        chain.bridge.clients(), "fixture", "fixture", chain.adapter.config.manifest_path
    )
    assert again["deja_absentes"] == [HEAD], "idempotent"


def test_an_open_spec_pr_of_an_interrupted_run_is_closed_with_its_identity_guards(chain):
    project = drafted(chain)
    clients = chain.bridge.clients()
    chain.bridge.clients = lambda: clients
    clients.prs.get_commit_check_details = lambda *a, **k: (_ for _ in ()).throw(
        ToolExecutionError("panne de lecture des checks")
    )
    with pytest.raises(ToolExecutionError):
        materialize(chain, project)
    assert chain.bridge.prs[101]["state"] == "open"

    done = spec.cleanup_spec_resources(chain.bridge.clients(), "fixture", "fixture", chain.adapter.config.manifest_path)

    assert (
        done["pr_fermees"] == [101] and chain.bridge.prs[101]["state"] == "closed" and HEAD not in chain.bridge.branches
    )


# ── 2. base : attribution durable de CHAQUE fusion ───────────────────────────────────────────────────────────────────────────


def test_a_foreign_commit_on_the_base_is_never_adopted_even_with_unchanged_controls(chain):
    project = drafted(chain)
    seed_manifest(chain, project)
    materialize(chain, project)
    recorded = _load_manifest(chain.adapter.config.manifest_path).base_sha
    chain.bridge.write_remote_file(BASE, "docs/foreign-merge.md", "Contribution extérieure à la campagne\n")

    with pytest.raises(spec.SpecMaterializationError, match="n'est attribué à aucune fusion durable"):
        cleanup(chain)

    assert _load_manifest(chain.adapter.config.manifest_path).base_sha == recorded


def test_a_foreign_pull_request_merge_in_the_middle_of_the_chain_is_refused(chain):
    """Fusion étrangère par une VRAIE PR (marqueur public copié) entre deux fusions de la campagne : l'ascendance ne l'explique pas."""
    project = drafted(chain)
    seed_manifest(chain, project)
    clients = chain.bridge.clients()
    chain.bridge.branches["collegue/improve-r9-etranger"] = chain.bridge.branches[BASE]
    tip = chain.bridge.write_remote_file("collegue/improve-r9-etranger", "docs/etranger.md", "x\n")
    pr = clients.prs.create_pr(
        "fixture", "fixture", "PR étrangère", "collegue/improve-r9-etranger", BASE, "<!-- collegue-exec:9 -->"
    )
    clients.prs.merge_pr("fixture", "fixture", pr.number, method="squash", expected_head_sha=tip)
    materialize(chain, project)  # la SPEC est fusionnée APRÈS la fusion étrangère

    with pytest.raises(spec.SpecMaterializationError, match="n'est attribué à aucune fusion durable"):
        cleanup(chain)


def test_the_base_advanced_only_by_attributed_merges_is_adopted(chain):
    project = drafted(chain)
    seed_manifest(chain, project)
    outcome = materialize(chain, project)

    done = cleanup(chain)

    assert done["base"]["base"] == "avancée" and done["base"]["to"] == outcome.merge_sha
    assert done["base"]["attributions"] == ["registre:spec_merged"]


def test_a_merge_without_a_durable_registry_is_not_attributed(chain):
    """Compatibilité fail-closed : sans registre d'appartenance ni preuve de livraison, rien n'explique la fusion de la SPEC."""
    project = drafted(chain)
    seed_manifest(chain, project)
    materialize(chain, project)
    os.remove(ownership.ledger_file(chain.adapter.config.manifest_path))

    with pytest.raises(spec.SpecMaterializationError, match="n'est attribué à aucune fusion durable"):
        cleanup(chain)


# ── 3. PR et têtes : preuves de livraison RÉELLES du produit ────────────────────────────────────────────────────────────────────


def build_world(chain):
    """LE projet de la campagne : ses PR BUILD/IMPROVE sont livrées par la VRAIE entrée publique (preuve persistée par ``open_pr``)."""
    return drafted(chain)


async def deliver_build(chain, pid, *, merge):
    sync_operator(chain)
    await run_pass(
        chain.url, chain.campaign.path, chain.bridge, pid, agent=FilesAgent(files={"docs/livre.md": "# livré\n"}),
        settings={"BUILD_AUTO_MERGE": merge, "TASK_MAX_ATTEMPTS": 1}, base=BASE, max_iterations=1,
    )  # fmt: skip


async def open_improvement(chain, pid):
    sync_operator(chain)
    world = SimpleNamespace(
        source=chain.campaign.path,
        manager=ProjectStateManager.from_url(chain.url),
        url=chain.url,
        project_id=pid,
        bridge=chain.bridge,
    )
    result = await improve(
        world,
        [metrics(80), metrics(90)],
        agent=FilesFeature({"docs/gain.md": "# gain\n"}),
        promotion_hook=None,
        base=BASE,
    )
    assert len(result.promoted) == 1, result.rejected
    return result.promoted[0]


def test_an_owned_open_improvement_pr_is_closed_and_a_foreign_one_with_the_same_public_markers_is_left(chain):
    project = drafted(chain)
    pid = build_world(chain)
    seed_manifest(chain, pid)
    item = asyncio.run(open_improvement(chain, pid))
    own = chain.bridge.prs[item.pr_number]
    clients = chain.bridge.clients()
    foreign_head = "collegue/improve-r999999-foreign"
    chain.bridge.branches[foreign_head] = chain.bridge.branches[BASE]
    foreign_tip = chain.bridge.write_remote_file(foreign_head, "docs/foreign.md", "Travail étranger\n")
    marker = "<!-- collegue-exec:" + own["body"].split("<!-- collegue-exec:")[1].split(" -->")[0] + " -->"
    foreign = clients.prs.create_pr(
        "fixture", "fixture", "PR étrangère", foreign_head, BASE, f"copie du marqueur public\n{marker}"
    )
    assert project

    done = cleanup(chain)

    assert done["residual_pull_requests"] == [{"pr": item.pr_number, "head": own["head"]["ref"]}]
    assert chain.bridge.prs[item.pr_number]["state"] == "closed" and own["head"]["ref"] not in chain.bridge.branches
    assert chain.bridge.prs[foreign.number]["state"] == "open" and chain.bridge.branches[foreign_head] == foreign_tip


def test_an_owned_improvement_whose_head_moved_since_its_proof_is_not_closed(chain):
    pid = build_world(chain)
    seed_manifest(chain, pid)
    item = asyncio.run(open_improvement(chain, pid))
    head = chain.bridge.prs[item.pr_number]["head"]["ref"]
    chain.bridge.write_remote_file(head, "docs/tiers.md", "poussé après la preuve\n")

    done = cleanup(chain)

    assert done["residual_pull_requests"] == [] and chain.bridge.prs[item.pr_number]["state"] == "open"


def test_merged_heads_are_recorded_or_deleted_only_when_the_product_proves_them(chain):
    drafted(chain)
    pid = build_world(chain)
    seed_manifest(chain, pid, issues=[1])
    asyncio.run(
        deliver_build(chain, pid, merge=True)
    )  # BUILD livré et fusionné par le produit (preuve + cycle de fusion réels)
    build_pr = next(
        number for number, pr in chain.bridge.prs.items() if pr["head"]["ref"].startswith("collegue/issue-")
    )
    head = chain.bridge.prs[build_pr]["head"]["ref"]
    # une tête étrangère FUSIONNÉE, mêmes préfixe et marqueur publics, aucun lien au projet
    clients = chain.bridge.clients()
    chain.bridge.branches["collegue/improve-r9-etranger"] = chain.bridge.branches[BASE]
    foreign_tip = chain.bridge.write_remote_file("collegue/improve-r9-etranger", "docs/etranger.md", "x\n")
    foreign = clients.prs.create_pr(
        "fixture", "fixture", "étrangère", "collegue/improve-r9-etranger", BASE, "<!-- collegue-exec:9 -->"
    )
    clients.prs.merge_pr("fixture", "fixture", foreign.number, method="squash", expected_head_sha=foreign_tip)
    manifest = _load_manifest(chain.adapter.config.manifest_path)
    manifest.issue_numbers = [int(head.rsplit("-", 1)[1])]
    _write_manifest(chain.adapter.config.manifest_path, manifest)

    done = spec.reconcile_merged_heads(
        clients, chain.adapter.config, manager=ProjectStateManager.from_url(chain.url), project_id=pid
    )

    assert done == {"recorded": [head], "deleted": []}, (
        "la tête de tâche prouvée est consignée ; la tête étrangère n'est pas touchée"
    )
    assert _load_manifest(chain.adapter.config.manifest_path).head_shas == {head: chain.bridge.branches[head]}
    assert chain.bridge.branches["collegue/improve-r9-etranger"] == foreign_tip


def test_a_base_advanced_by_owned_build_and_improvement_merges_and_the_spec_is_adopted_with_each_attribution(chain):
    project = drafted(chain)
    pid = build_world(chain)
    seed_manifest(chain, pid)
    materialize(chain, project)
    asyncio.run(deliver_build(chain, pid, merge=True))
    item = asyncio.run(open_improvement(chain, pid))
    clients = chain.bridge.clients()
    merged = clients.prs.merge_pr(
        "fixture", "fixture", item.pr_number, method="squash", expected_head_sha=item.head_sha,
        expected_base_branch=BASE, expected_base_sha=chain.bridge.prs[item.pr_number]["base"]["sha"],
    )  # fmt: skip
    assert merged.merged

    done = cleanup(chain)

    assert done["base"]["base"] == "avancée" and done["base"]["to"] == chain.bridge.branches[BASE]
    origins = done["base"]["attributions"]
    assert origins[0] == "registre:spec_merged" and any(
        "preuve:build" in o or "cycle de fusion" in o for o in origins[1:]
    )
    assert any("preuve:improve" in o for o in origins)


def test_one_unattributed_commit_between_attributed_merges_keeps_the_base_and_the_anchors(chain):
    project = drafted(chain)
    pid = build_world(chain)
    seed_manifest(chain, pid)
    materialize(chain, project)
    chain.bridge.write_remote_file(BASE, "docs/trou.md", "commit étranger intercalé\n")
    asyncio.run(deliver_build(chain, pid, merge=True))
    recorded = _load_manifest(chain.adapter.config.manifest_path).base_sha

    with pytest.raises(spec.SpecMaterializationError, match="n'est attribué à aucune fusion durable"):
        cleanup(chain)

    assert _load_manifest(chain.adapter.config.manifest_path).base_sha == recorded


# ── 4. le registre d'appartenance lui-même est fail-closed ────────────────────────────────────────────────────────────────────


def test_a_corrupted_registry_stops_the_cleanup_instead_of_guessing(chain):
    project = drafted(chain)
    seed_manifest(chain, project)
    materialize(chain, project)
    with open(ownership.ledger_file(chain.adapter.config.manifest_path), "a", encoding="utf-8") as handle:
        handle.write("pas du json\n")

    with pytest.raises(spec.SpecMaterializationError, match="inutilisable"):
        spec.cleanup_spec_resources(chain.bridge.clients(), "fixture", "fixture", chain.adapter.config.manifest_path)
    assert HEAD in chain.bridge.branches


def test_a_registry_of_another_repository_or_base_owns_nothing_here(chain, tmp_path):
    ownership.append_event(
        chain.adapter.config.manifest_path, ownership.identity_of("autre", "depot", "collegue-business/x", project_id=1),
        "spec_branch_intent", head=HEAD, base_tip="a" * 40, absent_verified=True, spec_sha256="b" * 64, spec_file="SPEC.md", spec_blob="c" * 40,
    )  # fmt: skip
    chain.bridge.branches[HEAD] = chain.bridge.branches[BASE]

    done = spec.cleanup_spec_resources(chain.bridge.clients(), "fixture", "fixture", chain.adapter.config.manifest_path)

    assert done["spec"] == "aucune ressource possédée" and HEAD in chain.bridge.branches


# ── 5. la base après la fusion de la SPEC : sommet exact, y compris sur réponse perdue et sur reprise ─────────────────────────────


def foreign_after_merge(chain, *, lose_response):
    clients = chain.bridge.clients()
    real = clients.prs.merge_pr

    def merged_then_foreign(*args, **kwargs):
        result = real(*args, **kwargs)
        chain.bridge.write_remote_file(BASE, "docs/parallel.md", "Commit arrivé après la fusion\n")
        if lose_response:
            raise ToolExecutionError("timeout : réponse perdue")
        return result

    clients.prs.merge_pr = merged_then_foreign
    chain.bridge.clients = lambda: clients


@pytest.mark.parametrize("lose_response", [False, True], ids=["response-received", "response-lost"])
def test_a_base_moved_right_after_the_merge_stops_even_when_the_response_was_lost(chain, lose_response):
    project = drafted(chain)
    foreign_after_merge(chain, lose_response=lose_response)

    with pytest.raises(spec.SpecMaterializationError, match="n'est pas le commit de fusion vérifié"):
        materialize(chain, project)

    assert len(chain.bridge.merge_calls()) == 1, "aucune seconde fusion pour « réparer » une réponse ambiguë"


def test_an_idempotent_resume_on_an_identical_spec_does_not_mask_a_base_that_moved_since_the_merge(chain):
    project = drafted(chain)
    materialize(chain, project)
    chain.bridge.write_remote_file(BASE, "docs/apres.md", "Écriture extérieure après la fusion de la SPEC\n")

    with pytest.raises(spec.SpecMaterializationError, match="état distant incohérent"):
        materialize(chain, project)

    assert len(chain.bridge.merge_calls()) == 1 and sorted(chain.bridge.prs) == [101]


def test_the_idempotent_resume_on_an_unmoved_base_stays_a_clean_noop(chain):
    project = drafted(chain)
    first = materialize(chain, project)

    again = materialize(chain, project)

    assert again.state == "already_identical" and again.base_after == first.merge_sha


# ── 6. entrée publique de nettoyage : mêmes preuves durables, sans la mémoire du run ───────────────────────────────────────────────


class RecordingNightly:
    def __init__(self, order):
        self.order = order

    def cleanup(self):
        self.order.append("nightly")
        return {"status": "clean"}


def run_public_cleanup(chain, monkeypatch, capsys, *, state_url=True):
    order = []
    env = {**chain.env, fixture_policy.TRUST_ANCHOR_ENV: trust(chain), "PATH": os.environ["PATH"]}
    if not state_url:
        env.pop("STATE_DATABASE_URL")
    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(business, "business_config", lambda _env: chain.adapter.config)
    monkeypatch.setattr(
        business, "_fixture_clients", lambda _token: chain.bridge.clients()
    )  # NOUVEAUX clients : aucune mémoire du run
    monkeypatch.setattr(business, "NightlyAdapter", lambda config, clients, runner: RecordingNightly(order))
    code = business.main(["cleanup", "--campaign-id", CYCLE])
    out = capsys.readouterr().out
    return code, order, out


def test_the_public_cleanup_entry_replays_the_durable_proofs_before_the_nightly_cleanup(chain, monkeypatch, capsys):
    import json

    business.launch_campaign(
        business.CampaignReport("campaign", "unit"),
        adapter=chain.adapter,
        env=chain.env,
        cycle_id=CYCLE,
        materialize_spec=materializer(chain),
    )
    assert HEAD in chain.bridge.branches  # le nettoyage du run n'a PAS eu lieu (processus mort, étape échouée…)

    code, order, out = run_public_cleanup(chain, monkeypatch, capsys)

    payload = json.loads(out)
    assert code == 0 and order == ["nightly"] and payload["status"] == "clean"
    assert payload["campaign_resources"]["spec"]["branches_supprimees"] == [HEAD] and HEAD not in chain.bridge.branches
    assert payload["campaign_resources"]["base"]["base"] == "avancée"


def test_the_public_cleanup_entry_completes_a_cleanup_that_failed_half_way(chain, monkeypatch, capsys):
    """Le nettoyage du run a supprimé la branche de SPEC puis a échoué : l'entrée publique est idempotente et poursuit."""
    business.launch_campaign(
        business.CampaignReport("campaign", "unit"),
        adapter=chain.adapter,
        env=chain.env,
        cycle_id=CYCLE,
        materialize_spec=materializer(chain),
    )
    spec.cleanup_spec_resources(chain.bridge.clients(), "fixture", "fixture", chain.adapter.config.manifest_path)

    code, order, out = run_public_cleanup(chain, monkeypatch, capsys)

    assert code == 0 and order == ["nightly"] and '"deja_absentes": ["collegue-spec/777-1"]' in out


def test_the_public_cleanup_entry_never_makes_an_unknown_resource_deletable_to_get_a_green_cleanup(
    chain, monkeypatch, capsys
):
    business.launch_campaign(
        business.CampaignReport("campaign", "unit"),
        adapter=chain.adapter,
        env=chain.env,
        cycle_id=CYCLE,
        materialize_spec=materializer(chain),
    )
    moved = chain.bridge.write_remote_file(HEAD, "docs/tiers.md", "Un tiers a poussé sur la branche de SPEC\n")

    with pytest.raises(spec.SpecMaterializationError, match="nettoyage incomplet"):
        run_public_cleanup(chain, monkeypatch, capsys)

    assert chain.bridge.branches[HEAD] == moved, "ressource non prouvée conservée"
    assert chain.bridge.branches[BASE], "la base et les ancres sont conservées"


def test_the_public_cleanup_entry_without_the_state_database_touches_nothing_that_needs_the_product_proofs(
    chain, monkeypatch, capsys
):
    pid = drafted(chain)
    seed_manifest(chain, pid)
    asyncio.run(open_improvement(chain, pid))
    improvement = next(pr for pr in chain.bridge.prs.values() if pr["head"]["ref"].startswith("collegue/improve-"))

    code, order, out = run_public_cleanup(chain, monkeypatch, capsys, state_url=False)

    assert code == 0 and improvement["state"] == "open", (
        "sans preuve de livraison lisible, la PR d'amélioration n'est pas fermée"
    )


def test_an_owned_merged_improvement_head_is_deleted_with_the_tip_guard_and_a_moved_one_is_kept(chain):
    pid = drafted(chain)
    seed_manifest(chain, pid)
    item = asyncio.run(open_improvement(chain, pid))
    clients = chain.bridge.clients()
    merged = clients.prs.merge_pr(
        "fixture", "fixture", item.pr_number, method="squash", expected_head_sha=item.head_sha,
        expected_base_branch=BASE, expected_base_sha=chain.bridge.prs[item.pr_number]["base"]["sha"],
    )  # fmt: skip
    assert merged.merged
    head = chain.bridge.prs[item.pr_number]["head"]["ref"]
    manager = ProjectStateManager.from_url(chain.url)

    done = spec.reconcile_merged_heads(clients, chain.adapter.config, manager=manager, project_id=pid)

    assert done == {"recorded": [], "deleted": [head]} and head not in chain.bridge.branches


def test_a_materialiser_that_does_not_establish_the_exact_post_merge_tip_lets_no_build_start(tmp_path):
    from test_w4_business_launch import ENV, FakeAdapter

    adapter = FakeAdapter(tmp_path)

    with pytest.raises(business.BaseMovedError, match="sommet exact"):
        business.launch_campaign(
            business.CampaignReport("campaign", "unit"),
            adapter=adapter,
            env=ENV,
            materialize_spec=lambda report, context: None,
        )

    assert "run" not in adapter.calls and "sync" not in adapter.calls
