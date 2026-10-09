"""Lancement CLI de la campagne sur une base PROTÉGÉE (B25) : la VRAIE chaîne ``plan draft`` → ``plan approve`` → SPEC → ``plan sync``.

Deux raccords que des faux JSON à la place de ``adapter.product`` ne prouvaient pas :

1. la vraie CLI refusait ``--nightly-exact-task-count 3`` (le lanceur de la campagne l'émet) alors que le décomposeur accepte
   ``[1, MAX_TASKS]`` ;
2. ``plan sync --execute`` committe ``SPEC.md`` par un PUT Contents DIRECT sur la base ; le ruleset des bases ``collegue-business/*``
   (PR obligatoire, check requis, base à jour, aucun bypass) le refuse (GH013). La SPEC approuvée est donc matérialisée par une PR
   documentaire sous les protections réelles AVANT ``plan sync``, qui la relit identique.

Ici : la vraie CLI en processus (argparse, validations, ``runtime``, ``github_sync``) avec un transport de planification DÉTERMINISTE, les
VRAIS clients GitHub (``BranchCommands``/``FileCommands``/``PRCommands``) derrière un vrai dépôt Git distant dont la frontière HTTP REFUSE
les écritures directes sur la base (texte du refus réel), PR/checks/strict/merge comme le serveur. Aucun modèle, aucune clé, aucun réseau.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
from types import SimpleNamespace

import pytest
import w4_business_campaign as harness
from test_w4_business_launch import ENV, FakeAdapter
from w3_remote_bridge import make_bridge
from w5_campaign_support import campaign_mode, campaign_source

from collegue.pilot import __main__ as cli
from collegue.pilot import w4_business as business
from collegue.pilot import w5_business as w5
from collegue.pilot import w5_business_policy as fixture_policy
from collegue.pilot import w5_business_spec as spec_publication
from collegue.pilot.w4_business import BudgetStop, CampaignReport
from collegue.planner.decomposer import MAX_TASKS
from collegue.state import ProjectStateManager
from collegue.tools.base import ToolExecutionError

BASE = "collegue-business/777-1"
CYCLE = "w5-public-001"
DRAFT_ARGS = [
    "plan", "draft", "--name", "W5 public", "--problem", business.BUSINESS_PROBLEM, "--owner", "o", "--repo", "r",
    "--base", BASE, "--labels", "autonome", "--milestone", "", "--spec-filename", "SPEC.md", "--deadline-hours", "0.25",
    "--cycle-id", CYCLE, "--format", "json",
]  # fmt: skip


def run_cli(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        try:
            code = cli.main(list(argv))
        except SystemExit as stop:
            code = stop.code
    return code, out.getvalue()


# ── 1. la vraie CLI accepte ce que le lanceur émet ; les invalides restent refusés ──────────────────────────────────────────────


@pytest.fixture
def draft_calls(monkeypatch):
    seen = []

    async def fake_draft(args):
        seen.append((args.nightly_exact_task_count, args.cycle_id, args.base))
        return 0

    monkeypatch.setattr(cli, "_plan_draft", fake_draft)
    return seen


@pytest.mark.parametrize("count", [1, 3, MAX_TASKS])
def test_the_real_cli_accepts_the_task_count_the_launcher_declares(draft_calls, count):
    code, _out = run_cli([*DRAFT_ARGS, "--nightly-exact-task-count", str(count)])

    assert code == 0 and draft_calls == [(count, CYCLE, BASE)]


@pytest.mark.parametrize("count", ["0", "-1", str(MAX_TASKS + 1), "abc", "2.5"])
def test_the_real_cli_still_refuses_invalid_task_counts_before_any_draft(draft_calls, count):
    code, _out = run_cli([*DRAFT_ARGS, "--nightly-exact-task-count", count])

    assert code == 2 and draft_calls == [], "refus à l'analyse des arguments : aucun appel de modèle"


def test_the_nightly_witness_and_the_absence_of_the_option_are_unchanged(draft_calls):
    assert run_cli([*DRAFT_ARGS, "--nightly-exact-task-count", "1"])[0] == 0
    assert run_cli(DRAFT_ARGS)[0] == 0
    assert [call[0] for call in draft_calls] == [1, None]


def test_every_command_line_the_launcher_emits_is_accepted_by_the_real_parser(tmp_path, monkeypatch):
    """Les arguments RÉELS de ``launch_campaign`` (draft, approve, sync, run) traversent le vrai parseur et ses validations."""
    adapter = FakeAdapter(tmp_path)
    emitted = []
    original = adapter.product
    adapter.product = lambda *args, **kw: (emitted.append((args, kw)), original(*args, **kw))[1]
    business.launch_campaign(CampaignReport("campaign", "unit"), adapter=adapter, env=ENV, cycle_id=CYCLE)
    handlers = {}

    async def record_async(tag, args):
        handlers.setdefault(tag, args)
        return 0

    def record_sync(tag, args):
        handlers.setdefault(tag, args)
        return 0

    monkeypatch.setattr(cli, "_plan_draft", lambda args: record_async("_plan_draft", args))
    monkeypatch.setattr(cli, "_run", lambda args: record_async("_run", args))
    monkeypatch.setattr(cli, "_approve_plan", lambda args: record_sync("_approve_plan", args))
    monkeypatch.setattr(cli, "_sync_plan", lambda args: record_sync("_sync_plan", args))

    verbs = []
    for args, kwargs in emitted:
        code, _out = run_cli(args)
        assert code == 0, f"la vraie CLI refuse les arguments du lanceur : {args[:4]} (rc={code})"
        verbs.append(args[1] if args[0] == "plan" else "run")
    assert verbs == ["draft", "approve", "sync", "run"]
    assert handlers["_plan_draft"].nightly_exact_task_count == 3 and handlers["_plan_draft"].cycle_id == CYCLE
    assert handlers["_run"].execute is True and handlers["_run"].base == adapter.config.base_branch


# ── 2. la vraie chaîne sur une base protégée ─────────────────────────────────────────────────────────────────────────────────


class Doubles:
    """Issues, labels et jalons : hors du sujet (la SPEC passe par les VRAIS clients Contents/PR), mais le sync les appelle."""

    def __init__(self):
        self.created = []

    def ensure_label(self, owner, repo, name):
        return None

    def ensure_milestone(self, owner, repo, title):
        return SimpleNamespace(number=1, title=title)

    def create_issue_with_metadata(self, owner, repo, *, title, body, labels, milestone_number):
        self.created.append(title)
        return SimpleNamespace(number=10 + len(self.created))


class ChainAdapter:
    """Même interface que ``NightlyAdapter`` ; ``product`` exécute la VRAIE CLI en processus (le run est consigné, non exécuté)."""

    def __init__(self, chain):
        self.chain = chain
        self.config = dataclasses.replace(
            business.business_config({**ENV, "COLLEGUE_NIGHTLY_MANIFEST": str(chain.tmp / "manifest.json")}),
            repository="fixture/fixture",
        )
        self.calls = []
        self.inner = SimpleNamespace(clients=chain.bridge.clients())

    def guard_fixture(self):
        return business.FIXTURE_SEED_SHA

    def create_base(self, manifest):
        from collegue.pilot.nightly_e2e import _write_manifest

        manifest.base_creation_started = manifest.base_created = True
        manifest.base_sha = self.chain.bridge.branches[BASE]
        _write_manifest(self.config.manifest_path, manifest)
        return manifest.base_sha

    def create_label(self, manifest):
        self.calls.append("label")

    def product(self, *args, accepted_codes=(0,)):
        verb = args[1] if args[0] == "plan" else "run"
        self.calls.append(verb)
        if verb == "run":
            return {"stop_reason": "completed", "opened_prs": []}
        code, out = run_cli(args)
        assert code in accepted_codes, f"la vraie CLI a refusé `{' '.join(args[:3])}` (rc={code})"
        return json.loads(out)

    def clone(self, sha):
        self.calls.append("clone")
        folder = self.chain.tmp / f"clone-{len(self.calls)}" / "fixture"
        folder.mkdir(parents=True)
        return str(folder)

    def cleanup(self):
        self.calls.append("cleanup")


@pytest.fixture
def chain(tmp_path, monkeypatch):
    from collegue.pilot import runtime
    from collegue.planner import github_sync

    campaign = campaign_source(tmp_path / "src")
    bridge = make_bridge(tmp_path, campaign.path, base=BASE)
    trust = tmp_path / "trust"  # le manifeste du socle n'est PAS le manifeste d'état du nightly (chemins distincts)
    trust.mkdir()
    campaign_mode(monkeypatch, bridge, campaign=campaign, directory=trust, base_prefix=None)
    bridge.protect_direct_writes(BASE)  # le ruleset des bases de campagne : PR obligatoire, aucune écriture directe
    url = f"sqlite:///{tmp_path / 'state.db'}"
    ProjectStateManager.from_url(url, create=True)
    transport, settings, doubles = harness.PlanningTransport(), harness.campaign_settings(), Doubles()
    monkeypatch.setattr(runtime, "_settings", lambda: settings)
    monkeypatch.setattr(runtime, "_build_manager", lambda _settings: ProjectStateManager.from_url(url))
    monkeypatch.setattr(runtime, "_build_ctx", lambda _settings: transport)
    real = bridge.clients()

    def sync_clients(token):
        return github_sync.SyncClients(
            issues=doubles, labels=doubles, milestones=doubles, projects=doubles, files=real.files
        )

    monkeypatch.setattr(github_sync, "_default_clients", sync_clients)
    env = {
        **ENV,
        "STATE_DATABASE_URL": url,
        "COLLEGUE_NIGHTLY_MANIFEST": str(tmp_path / "manifest.json"),
    }
    state = SimpleNamespace(
        tmp=tmp_path, bridge=bridge, campaign=campaign, url=url, doubles=doubles, env=env, transport=transport, adapter=None,
    )  # fmt: skip
    state.adapter = ChainAdapter(state)
    return state


def spec_of(chain, project_id):
    return ProjectStateManager.from_url(chain.url).get_project(project_id).spec


def materializer(chain, **kwargs):
    clients = chain.bridge.clients()

    def run(report, context):
        return spec_publication.materialize_approved_spec(
            clients=clients, owner="fixture", repo="fixture", manager=ProjectStateManager.from_url(chain.url),
            project_id=int(context["project_id"]), manifest_path=chain.adapter.config.manifest_path,
            trust_manifest_path=__import__("os").environ[fixture_policy.TRUST_ANCHOR_ENV], **kwargs,
        )  # fmt: skip

    return run


def direct_spec_writes(chain):
    return [
        c for c in chain.bridge.calls if c[0] == "PUT" and "/contents/SPEC.md" in c[1] and c[2].get("branch") == BASE
    ]


def launch(chain, **kwargs):
    return business.launch_campaign(
        CampaignReport("campaign", "unit"), adapter=chain.adapter, env=chain.env, cycle_id=CYCLE, **kwargs
    )


def test_without_the_spec_materialisation_the_real_sync_is_refused_by_the_protected_base_before_any_build(chain):
    """Diagnostic : la vraie chaîne, SANS matérialisation, bute sur le refus réel d'un PUT direct (GH013) ; le BUILD n'est jamais atteint."""
    from collegue.planner.github_sync import SpecSyncError

    with pytest.raises(SpecSyncError, match="GH013.*pull request"):
        launch(chain)

    assert "run" not in chain.adapter.calls and chain.doubles.created == []
    assert len(direct_spec_writes(chain)) == 1, "le PUT direct a été tenté et refusé"
    assert "SPEC.md" not in chain.bridge.remote.files_at(BASE)


def test_the_real_chain_materialises_the_approved_spec_by_a_pull_request_then_the_sync_reads_it_identical(chain):
    report = CampaignReport("campaign", "unit")

    context = business.launch_campaign(
        report, adapter=chain.adapter, env=chain.env, cycle_id=CYCLE, materialize_spec=materializer(chain)
    )

    verbs = [call for call in chain.adapter.calls if call != "clone"]
    assert verbs == ["draft", "approve", "label", "sync", "run"], "draft, approbation, SPEC par PR, label, sync, UN run"
    base_files = chain.bridge.remote.files_at(BASE)
    assert base_files["SPEC.md"] == spec_of(chain, context["project_id"]), (
        "la SPEC distante est celle du snapshot approuvé"
    )
    assert direct_spec_writes(chain) == [], "aucune écriture directe sur la base protégée"
    fact = report.facts["spec_materialization"]
    assert fact["state"] == "merged" and fact["head_branch"] == "collegue-spec/777-1" and fact["pr_number"] == 101
    assert chain.bridge.merged_pr_numbers() == [101] and len(chain.bridge.merge_calls()) == 1
    assert chain.doubles.created and len(chain.doubles.created) == 3, "les trois issues sont créées APRÈS la SPEC"
    assert fact["base_after"] == chain.bridge.branches[BASE] != fact["base_before"]
    # arguments réels conservés : le brouillon porte le cycle (scope durable) et les trois tâches
    snapshot = ProjectStateManager.from_url(chain.url).budget_ledger.snapshot(f"planning:cycle:{CYCLE}")
    assert snapshot is not None and snapshot.strict is True


def test_the_documentary_pr_is_not_a_build_task_and_carries_exactly_one_file(chain):
    business.launch_campaign(
        CampaignReport("campaign", "unit"),
        adapter=chain.adapter,
        env=chain.env,
        cycle_id=CYCLE,
        materialize_spec=materializer(chain),
    )

    pr = chain.bridge.prs[101]
    assert pr["head"]["ref"] == "collegue-spec/777-1" and pr["base"]["ref"] == BASE
    assert [f["filename"] for f in pr["files"]] == ["SPEC.md"]
    assert "collegue-spec:" in pr["body"] and "Closes #" not in pr["body"]
    assert chain.bridge.remote.tree_of(pr["head"]["sha"]) != chain.bridge.remote.tree_of(chain.campaign.bootstrap_sha)


# ── 3. refus : arrêt explicite AVANT tout BUILD ───────────────────────────────────────────────────────────────────────────────


def drafted(chain):
    """Brouillon et approbation par la VRAIE CLI ; retourne l'identifiant du projet."""
    draft = chain.adapter.product(*DRAFT_ARGS_FOR(chain), "--nightly-exact-task-count", "3")
    chain.adapter.product(
        "plan",
        "approve",
        "--project-id",
        str(draft["project_id"]),
        "--expected-plan-hash",
        draft["plan_hash"],
        "--format",
        "json",
    )
    return draft["project_id"]


def DRAFT_ARGS_FOR(chain):
    return [a if a not in ("o", "r") else "fixture" for a in DRAFT_ARGS]


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.on_sleep = None

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(seconds, 1.0)
        if self.on_sleep:
            self.on_sleep()


def materialize(chain, project_id, *, clock=None, **kwargs):
    clock = clock or FakeClock()
    return spec_publication.materialize_approved_spec(
        clients=chain.bridge.clients(), owner="fixture", repo="fixture", manager=ProjectStateManager.from_url(chain.url),
        project_id=project_id, manifest_path=chain.adapter.config.manifest_path,
        trust_manifest_path=__import__("os").environ[fixture_policy.TRUST_ANCHOR_ENV],
        deadline_monotonic=lambda: 1900.0, clock=spec_publication.Clock(now=clock.now, sleep=clock.sleep), poll_seconds=10.0, **kwargs,
    )  # fmt: skip


def reapply(chain, monkeypatch, **kwargs):
    """Rejoue la politique de campagne avec un autre état de check (on retire d'abord l'enveloppe posée par la fixture)."""
    chain.bridge.__dict__.pop("set_checks", None)
    campaign_mode(
        monkeypatch, chain.bridge, campaign=chain.campaign, directory=chain.tmp / "trust", base_prefix=None, **kwargs
    )


def no_remote_mutation_but_the_spec_path(chain):
    return [c for c in chain.bridge.calls if c[0] != "GET"]


def test_a_red_required_check_stops_before_any_merge_or_build(chain, monkeypatch):
    reapply(chain, monkeypatch, check_state="failure")
    project = drafted(chain)

    with pytest.raises(spec_publication.SpecMaterializationError, match="check requis refusé"):
        materialize(chain, project)

    assert chain.bridge.merge_calls() == [] and "SPEC.md" not in chain.bridge.remote.files_at(BASE)


def test_an_absent_required_check_waits_inside_the_global_deadline_then_stops(chain, monkeypatch):
    reapply(chain, monkeypatch, check_state=None)
    project = drafted(chain)
    clock = FakeClock()

    with pytest.raises(spec_publication.SpecDeadline, match="échéance globale"):
        materialize(chain, project, clock=clock)

    assert clock.t >= 1900.0 - 10.0, "l'attente a consommé la fenêtre, jamais au-delà"
    assert chain.bridge.merge_calls() == []


def test_a_forged_check_without_a_matching_job_is_refused(chain, monkeypatch):
    reapply(chain, monkeypatch, job=False)
    project = drafted(chain)

    with pytest.raises(spec_publication.SpecMaterializationError, match="n'est pas un job"):
        materialize(chain, project)

    assert chain.bridge.merge_calls() == []


def waiting_for_checks(chain, then):
    """Les checks n'existent pas à l'ouverture de la PR ; pendant la 1re attente, ``then`` agit (acteur externe) puis les checks passent au vert."""
    from github_fake_server import FIVE_CHECKS

    chain.bridge.auto_green = False
    clock = FakeClock()

    def during_wait():
        then()
        chain.bridge.set_checks(chain.bridge.prs[101]["head"]["sha"], {name: "success" for name in FIVE_CHECKS})

    clock.on_sleep = during_wait
    return clock


def test_a_base_that_moves_while_waiting_stops_the_merge(chain):
    project = drafted(chain)
    clock = waiting_for_checks(
        chain, lambda: chain.bridge.write_remote_file(BASE, "docs/autre.md", "# un autre contributeur\n")
    )

    with pytest.raises(spec_publication.SpecMaterializationError, match="a bougé pendant l'attente"):
        materialize(chain, project, clock=clock)

    assert chain.bridge.merge_calls() == [] and "SPEC.md" not in chain.bridge.remote.files_at(BASE)


def test_a_head_that_moves_while_waiting_stops_the_merge(chain):
    project = drafted(chain)
    clock = waiting_for_checks(
        chain, lambda: chain.bridge.write_remote_file("collegue-spec/777-1", "docs/late.md", "# tard\n")
    )

    with pytest.raises(spec_publication.SpecMaterializationError, match="a changé pendant l'attente"):
        materialize(chain, project, clock=clock)

    assert chain.bridge.merge_calls() == []


def test_checks_that_become_green_during_the_wait_let_the_merge_proceed(chain):
    """Témoin des deux tests précédents : la même attente, sans acteur externe, aboutit."""
    project = drafted(chain)
    clock = waiting_for_checks(chain, lambda: None)

    outcome = materialize(chain, project, clock=clock)

    assert outcome.state == "merged" and clock.t > 1000.0, "l'attente a eu lieu avant la fusion"


def test_a_base_whose_controls_diverge_from_the_trust_anchor_creates_nothing(chain):
    project = drafted(chain)
    chain.bridge.write_remote_file(BASE, ".github/workflows/fixture-tests.yml", "name: altéré\n")
    before = len(no_remote_mutation_but_the_spec_path(chain))

    with pytest.raises(spec_publication.SpecMaterializationError, match="diverge du socle de confiance"):
        materialize(chain, project)

    assert len(no_remote_mutation_but_the_spec_path(chain)) == before and chain.bridge.prs == {}


def test_a_divergent_spec_already_on_the_base_is_never_overwritten(chain):
    project = drafted(chain)
    chain.bridge.write_remote_file(BASE, "SPEC.md", "# une autre SPEC\n")
    before = len(no_remote_mutation_but_the_spec_path(chain))

    with pytest.raises(spec_publication.SpecMaterializationError, match="contenu divergent"):
        materialize(chain, project)

    assert len(no_remote_mutation_but_the_spec_path(chain)) == before and chain.bridge.prs == {}


def test_an_identical_spec_already_on_the_base_creates_no_pull_request(chain):
    project = drafted(chain)
    chain.bridge.write_remote_file(BASE, "SPEC.md", spec_of(chain, project))

    outcome = materialize(chain, project)

    assert (
        outcome.state == "already_identical"
        and chain.bridge.prs == {}
        and no_remote_mutation_but_the_spec_path(chain) == []
    )


def test_a_lost_merge_response_is_reconciled_without_a_second_merge_or_pull_request(chain):
    project = drafted(chain)
    clients = chain.bridge.clients()
    real_merge = clients.prs.merge_pr

    def lost_response(*args, **kwargs):
        real_merge(*args, **kwargs)
        raise ToolExecutionError("timeout : réponse perdue")

    clients.prs.merge_pr = lost_response
    chain.bridge.clients = lambda: clients

    outcome = materialize(chain, project)

    assert outcome.state == "merged" and len(chain.bridge.merge_calls()) == 1 and sorted(chain.bridge.prs) == [101]
    again = materialize(chain, project)  # reprise après la fusion : la SPEC est déjà identique
    assert (
        again.state == "already_identical"
        and len(chain.bridge.merge_calls()) == 1
        and sorted(chain.bridge.prs) == [101]
    )


def test_a_merge_refused_by_the_server_is_an_explicit_stop(chain):
    project = drafted(chain)
    chain.bridge.merge_block_message = "Required status check Fixture tests is failing"

    with pytest.raises(spec_publication.SpecMaterializationError, match="fusion de la PR #101 refusée"):
        materialize(chain, project)

    assert "SPEC.md" not in chain.bridge.remote.files_at(BASE)


def test_a_residual_spec_branch_is_resumed_only_when_it_is_exactly_the_base_plus_the_spec(chain):
    project = drafted(chain)
    chain.bridge.branches["collegue-spec/777-1"] = chain.bridge.branches[BASE]
    chain.bridge.write_remote_file("collegue-spec/777-1", "docs/autre.md", "# autre\n")
    before = len(no_remote_mutation_but_the_spec_path(chain))

    with pytest.raises(spec_publication.SpecMaterializationError, match="ni réutilisée ni réécrite"):
        materialize(chain, project)

    assert len(no_remote_mutation_but_the_spec_path(chain)) == before and chain.bridge.prs == {}


def test_a_resumed_spec_branch_with_exactly_the_spec_is_not_rewritten(chain):
    project = drafted(chain)
    chain.bridge.branches["collegue-spec/777-1"] = chain.bridge.branches[BASE]
    chain.bridge.write_remote_file("collegue-spec/777-1", "SPEC.md", spec_of(chain, project))
    chain.bridge.calls.clear()

    outcome = materialize(chain, project)

    assert outcome.state == "merged"
    assert [c for c in chain.bridge.calls if c[0] == "PUT" and "/contents/" in c[1]] == [], (
        "branche reprise sans réécriture"
    )


def test_a_sync_failure_after_the_spec_stops_before_any_build(chain):
    chain.doubles.ensure_label = lambda *a: (_ for _ in ()).throw(RuntimeError("GitHub indisponible"))

    with pytest.raises(RuntimeError, match="GitHub indisponible"):
        launch(chain, materialize_spec=materializer(chain))

    assert "run" not in chain.adapter.calls and chain.bridge.merged_pr_numbers() == [101]


def test_the_global_deadline_reaching_the_spec_step_is_a_budget_stop_not_a_failure(chain):
    clients = chain.bridge.clients()
    config = chain.adapter.config

    class Expired:
        def __call__(self):
            return 0.0  # échéance dépassée depuis longtemps

    import os

    env = {**chain.env, fixture_policy.TRUST_ANCHOR_ENV: os.environ[fixture_policy.TRUST_ANCHOR_ENV]}
    project = drafted(chain)
    with pytest.raises(BudgetStop, match="échéance globale"):
        w5.materialize_spec_for_launch(clients=clients, config=config, env=env, project_id=project, deadline=Expired())
    assert chain.bridge.prs == {}


# ── 4. le nettoyage connaît ces ressources ───────────────────────────────────────────────────────────────────────────────────


def test_the_cleanup_removes_the_spec_branch_closes_residual_pull_requests_and_adopts_the_advanced_base(chain):
    from collegue.pilot.nightly_e2e import _load_manifest

    business.launch_campaign(
        CampaignReport("campaign", "unit"),
        adapter=chain.adapter,
        env=chain.env,
        cycle_id=CYCLE,
        materialize_spec=materializer(chain),
    )
    clients = chain.bridge.clients()
    chain.bridge.branches["collegue/improve-r1-abc"] = chain.bridge.branches[BASE]
    chain.bridge.write_remote_file("collegue/improve-r1-abc", "docs/gain.md", "# gain\n")
    residual = clients.prs.create_pr(
        "fixture", "fixture", "amélioration", "collegue/improve-r1-abc", BASE, "x\n<!-- collegue-exec:1 -->"
    )
    recorded = _load_manifest(chain.adapter.config.manifest_path).base_sha

    report = CampaignReport("campaign", "unit")
    done = w5.cleanup_campaign_resources(
        report,
        clients=clients,
        config=chain.adapter.config,
        env={**chain.env, fixture_policy.TRUST_ANCHOR_ENV: __import__("os").environ[fixture_policy.TRUST_ANCHOR_ENV]},
    )

    assert "collegue-spec/777-1" not in chain.bridge.branches and done["spec"]["branch"] == "supprimée"
    assert done["residual_pull_requests"] == [{"pr": residual.number, "head": "collegue/improve-r1-abc"}]
    assert (
        chain.bridge.prs[residual.number]["state"] == "closed"
        and "collegue/improve-r1-abc" not in chain.bridge.branches
    )
    assert chain.bridge.prs[101]["merged"], "la PR fusionnée de la SPEC n'est jamais touchée"
    new_base = _load_manifest(chain.adapter.config.manifest_path).base_sha
    assert new_base == chain.bridge.branches[BASE] != recorded and done["base"]["base"] == "avancée"


def test_the_cleanup_refuses_to_adopt_a_base_whose_controls_were_altered(chain):
    business.launch_campaign(
        CampaignReport("campaign", "unit"),
        adapter=chain.adapter,
        env=chain.env,
        cycle_id=CYCLE,
        materialize_spec=materializer(chain),
    )
    chain.bridge.write_remote_file(BASE, ".github/CODEOWNERS", "* @intrus\n")
    clients = chain.bridge.clients()

    with pytest.raises(spec_publication.SpecMaterializationError, match="contrôles de la base courante altérés"):
        w5.cleanup_campaign_resources(
            CampaignReport("campaign", "unit"), clients=clients, config=chain.adapter.config,
            env={**chain.env, fixture_policy.TRUST_ANCHOR_ENV: __import__("os").environ[fixture_policy.TRUST_ANCHOR_ENV]},
        )  # fmt: skip


def test_the_cleanup_without_any_spec_resource_is_a_noop_for_the_spec(chain):
    clients = chain.bridge.clients()
    done = spec_publication.cleanup_spec_resources(clients, "fixture", "fixture", chain.adapter.config.manifest_path)
    assert done == {"spec": "aucune ressource consignée"}


def test_merged_task_heads_are_recorded_for_the_nightly_cleanup_and_merged_improvement_heads_are_deleted(chain):
    """Le dépôt fixture conserve les têtes fusionnées : sans SHA consigné au manifeste, le nettoyage nightly refuse (« SHA non prouvé »)
    et conserve la base. Les têtes de tâche prouvées (PR fusionnée, marqueur, sommet = tête) sont consignées ; une tête déplacée non."""
    import os

    from collegue.pilot.nightly_e2e import _load_manifest

    business.launch_campaign(
        CampaignReport("campaign", "unit"),
        adapter=chain.adapter,
        env=chain.env,
        cycle_id=CYCLE,
        materialize_spec=materializer(chain),
    )
    bridge, clients = chain.bridge, chain.bridge.clients()
    manifest = _load_manifest(chain.adapter.config.manifest_path)
    assert manifest.issue_numbers == [11, 12, 13]
    heads = {}
    for number, name in ((11, "collegue/issue-11"), (12, "collegue/issue-12"), (13, "collegue/improve-r1-abc")):
        bridge.branches[name] = bridge.branches[BASE]
        heads[name] = bridge.write_remote_file(name, f"docs/{number}.md", f"# {number}\n")
        marker = f"<!-- collegue-exec:{number} -->"
        pr = clients.prs.create_pr("fixture", "fixture", f"PR {number}", name, BASE, f"corps\n{marker}")
        bridge.prs[pr.number].update(merged=True, state="closed")
    bridge.write_remote_file(
        "collegue/issue-12", "docs/deplace.md", "# déplacée après la PR\n"
    )  # tête déplacée : aucune preuve
    env = {**chain.env, fixture_policy.TRUST_ANCHOR_ENV: os.environ[fixture_policy.TRUST_ANCHOR_ENV]}

    done = w5.cleanup_campaign_resources(
        CampaignReport("campaign", "unit"), clients=clients, config=chain.adapter.config, env=env
    )

    saved = _load_manifest(chain.adapter.config.manifest_path).head_shas
    assert saved == {"collegue/issue-11": heads["collegue/issue-11"]}, "seule la tête prouvée est consignée"
    assert done["merged_heads"] == {"recorded": ["collegue/issue-11"], "deleted": ["collegue/improve-r1-abc"]}
    assert "collegue/improve-r1-abc" not in bridge.branches and "collegue/issue-12" in bridge.branches
