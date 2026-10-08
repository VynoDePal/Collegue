"""Raccord de la vague 3, BUILD : publication (lot A) → preuve durable → politique de fusion (lot B) → resynchronisation.

Propriété C. Les scénarios passent par les ENTRÉES PUBLIQUES (``run_project_from_settings``, qui enchaîne ``run_project`` →
``execute_issue`` → ``open_pr``, puis le merge-bot) et ne contiennent AUCUNE dispense :

* la preuve est la vraie (``load_delivery_proof`` lit le journal de décisions SQLite d'une NOUVELLE instance du manager) ;
* la resynchronisation et ``verify_local_sync`` tournent pour de vrai (``git fetch``/``reset`` sur le checkout opérateur,
  contre un dépôt Git distant réel) ; aucun ``verify_fn``/``proof_loader`` injecté ;
* le dépôt distant est un vrai dépôt Git (mêmes SHA/arbres/parents de bout en bout), derrière les vrais clients GitHub
  (``tests/w3_remote_bridge.py``) ; le commit de fusion est un vrai objet Git.

Chaque famille adverse a son témoin bénin sur le même chemin et affirme le MOTIF du refus (jamais un ``AttributeError``).
"""

import os
import subprocess
from types import SimpleNamespace

import pytest
from github_fakes import git, make_source_repo
from w3_remote_bridge import make_bridge

from collegue.executor import FakeCodeAgent, FakeReviewer
from collegue.executor.delivery_proof import load_delivery_proof
from collegue.pilot import run_project_from_settings
from collegue.planner import approve_plan
from collegue.sandbox import SandboxResult
from collegue.state import ProjectStateManager

OWNER = REPO = "fixture"


class _Sandbox:
    def run_tests(self, workspace, command="pytest -q"):
        return SandboxResult(exit_code=0, stdout="ok", stderr="")


class _Budget:
    def should_continue(self):
        return SimpleNamespace(action="continue", ok=True)

    def time_remaining_seconds(self):
        return None


class _Ctx:
    async def aclose(self):
        return None


class DeliveringAgent(FakeCodeAgent):
    """Écrit ``delivered-<n>.txt`` et enregistre ce que son workspace contenait AU DÉPART (preuve du contenu livré)."""

    def __init__(self):
        super().__init__()
        self.seen = []
        self.calls = 0

    def implement_issue(self, workspace, issue):
        n = self.calls
        self.calls += 1
        self.seen.append(sorted(name for name in os.listdir(workspace) if name.startswith("delivered-")))
        self._files = {f"delivered-{n}.txt": f"livraison {n}\n"}
        return super().implement_issue(workspace, issue)


@pytest.fixture
def source(tmp_path):
    return make_source_repo(tmp_path / "source", {"README.md": "# fixture\n"})


@pytest.fixture
def bridge(tmp_path, source):
    return make_bridge(tmp_path, source)


@pytest.fixture
def state_url(tmp_path):
    return f"sqlite:///{tmp_path / 'state.db'}"


def open_manager(url, *, create=False):
    """Une NOUVELLE instance du manager sur la même base (équivalent d'un redémarrage du process)."""
    return ProjectStateManager.from_url(url, create=create)


def linear_project(url, n=2):
    manager = open_manager(url, create=True)
    pid = manager.create_project(name="w3", spec="# SPEC\n")
    prev = None
    for i in range(n):
        prev = manager.add_task(pid, title=f"T{i}", depends_on=[prev] if prev else None)
    approve_plan(manager, pid)
    return pid


def sibling_project(url, n=2):
    manager = open_manager(url, create=True)
    pid = manager.create_project(name="w3-siblings", spec="# SPEC\n")
    for i in range(n):
        manager.add_task(pid, title=f"S{i}")
    approve_plan(manager, pid)
    return pid


async def run_pass(url, source, bridge, pid, *, agent=None, settings=None, sandbox=None, **kw):
    """Une passe du produit sur une NOUVELLE instance du manager, avec la politique de fusion RÉELLE."""
    values = dict(
        BUILD_AUTO_MERGE=True,
        AUTO_MERGE_CI_TIMEOUT_SECONDS=0,
        AUTO_MERGE_CI_POLL_SECONDS=0,
    )
    values.update(settings or {})
    return await run_project_from_settings(
        pid,
        source,
        owner=OWNER,
        repo=REPO,
        dry_run=False,
        settings_obj=SimpleNamespace(**values),
        manager=open_manager(url),
        sandbox=sandbox or _Sandbox(),
        agent=agent or DeliveringAgent(),
        reviewer=FakeReviewer(),
        clients=bridge.clients(),
        budget=_Budget(),
        ctx=_Ctx(),
        **kw,
    )


def statuses(url, pid):
    return {t.title: t.status for t in open_manager(url).get_tasks(pid)}


def git_out(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def has_object(directory, sha):
    return (
        subprocess.run(
            ["git", "cat-file", "-e", sha], cwd=directory, capture_output=True, env={"GIT_DIR": str(directory)}
        ).returncode
        == 0
    )


# ── témoin bénin : livraison réelle → fusion au SHA exact → synchronisation réelle → tâche suivante sur le contenu livré ──


async def test_benign_build_delivers_merges_at_the_exact_sha_and_the_next_task_starts_from_the_delivered_content(
    bridge, source, state_url
):
    pid = linear_project(state_url, 2)
    agent = DeliveringAgent()

    result = await run_pass(state_url, source, bridge, pid, agent=agent)

    assert result.stop_reason == "completed", result
    assert statuses(state_url, pid) == {"T0": "merged", "T1": "merged"}
    # une fusion par PR, avec le SHA de tête EXACT de la PR (jamais une fusion sans SHA)
    bodies = bridge.merge_bodies()
    assert len(bodies) == 2
    for body, number in zip(bodies, sorted(bridge.prs), strict=True):
        assert body["sha"] == bridge.prs[number]["head"]["sha"]
    # la preuve est RELISIBLE par une nouvelle instance : tête/arbre/base de la livraison réellement publiée
    reloaded = open_manager(state_url)
    for number, pr in bridge.prs.items():
        proof = load_delivery_proof(reloaded, pid, owner=OWNER, repo=REPO, pr_number=number, head_sha=pr["head"]["sha"])
        assert proof.passed is True and proof.phase == "build"
        assert proof.tree_sha == bridge.commits[pr["head"]["sha"]]["tree"]
    # le commit de fusion est un VRAI objet Git du dépôt distant ; main distante = checkout opérateur resynchronisé
    tip = bridge.branches["main"]
    assert bridge.remote.tree_of(tip) == bridge.commits[bridge.prs[102]["head"]["sha"]]["tree"]
    assert has_object(bridge.remote.dir, tip)
    assert git_out(source, "rev-parse", "HEAD") == tip
    # la 2ᵉ tâche est partie du contenu LIVRÉ (la 1ʳᵉ fusion était déjà dans le clone) ; la 1ʳᵉ n'avait rien
    assert agent.seen == [[], ["delivered-0.txt"]]
    # cycles de fusion : une ligne par tâche, synchronisée, avec l'origine moteur et les ancres de la preuve
    cycles = open_manager(state_url).list_task_merges(pid)
    assert [(c.state, c.origin) for c in cycles] == [("synced", "engine")] * 2
    assert all(c.merge_sha and c.proof_id for c in cycles)


# ── refus : chaque cas muté APRÈS la publication, zéro appel de fusion, motif affirmé ──────────────────────────────


def _adverse_late_push(bridge, source, pid, url):
    head = bridge.prs[101]["head"]
    bridge.write_remote_file(head["ref"], "late.txt", "poussé après la preuve\n")
    bridge.green(101)  # la CI du nouveau commit serait verte : seule la liaison de la preuve à la tête doit l'arrêter


def _adverse_base_moved(bridge, source, pid, url):
    bridge.write_remote_file("main", "autre.txt", "un autre contributeur\n")


def _adverse_check_missing(bridge, source, pid, url):
    bridge.set_checks(bridge.prs[101]["head"]["sha"], {"Ruff": "success", "Pytest (Python 3.11)": "success"})


def _adverse_check_skipped(bridge, source, pid, url):
    bridge.green(101, **{"Docker build": "skipped"})


def _adverse_check_pending(bridge, source, pid, url):
    bridge.green(101, **{"Docker build": "in_progress"})


def _adverse_check_failed(bridge, source, pid, url):
    bridge.green(101, **{"Pytest (Python 3.12)": "failure"})


def _adverse_wrong_app(bridge, source, pid, url):
    bridge.set_checks(bridge.prs[101]["head"]["sha"], {n: "success" for n in bridge.required_names()}, app_id=99999)


def _adverse_custom_role_bypass(bridge, source, pid, url):
    bridge.actor_role = "custom-bypass-role"  # rôle personnalisé pouvant contourner une protection classique
    bridge.protect(strict=True, enforce_admins=False)


def _adverse_not_strict(bridge, source, pid, url):
    bridge.protect(strict=False, enforce_admins=True)


def _adverse_no_protection(bridge, source, pid, url):
    bridge.classic = None


ADVERSE = [
    ("late_push", _adverse_late_push, "preuve de livraison"),
    ("base_moved", _adverse_base_moved, "a avancé depuis les contrôles"),
    ("check_missing", _adverse_check_missing, "checks requis absents"),
    ("check_skipped", _adverse_check_skipped, "checks requis non réussis"),
    ("check_pending", _adverse_check_pending, "checks en attente"),
    ("check_failed", _adverse_check_failed, "checks requis non réussis"),
    ("wrong_app", _adverse_wrong_app, "checks requis absents"),
    ("custom_role_bypass", _adverse_custom_role_bypass, "aucune protection stricte"),
    ("not_strict", _adverse_not_strict, "aucune protection stricte"),
]


@pytest.mark.parametrize("name, mutate, motive", ADVERSE, ids=[a[0] for a in ADVERSE])
async def test_a_published_delivery_is_not_merged_when_the_remote_or_the_policy_no_longer_matches_its_proof(
    name, mutate, motive, bridge, source, state_url, caplog
):
    pid = linear_project(state_url, 1)
    first = await run_pass(state_url, source, bridge, pid, settings={"BUILD_AUTO_MERGE": False})
    assert first.stop_reason == "awaiting_merge" and statuses(state_url, pid) == {"T0": "in_review"}
    assert bridge.merge_calls() == []

    mutate(bridge, source, pid, state_url)
    caplog.clear()
    with caplog.at_level("WARNING"):
        await run_pass(state_url, source, bridge, pid)

    assert bridge.merge_calls() == [], "aucun appel de fusion n'est émis quand la validation échoue"
    assert statuses(state_url, pid) == {"T0": "in_review"}
    assert bridge.merged_pr_numbers() == []
    assert motive in caplog.text, caplog.text[-1500:]
    assert all(c.state != "synced" for c in open_manager(state_url).list_task_merges(pid))


async def test_the_same_delivery_without_any_adverse_change_is_merged_by_the_same_path(bridge, source, state_url):
    """Témoin bénin des refus ci-dessus : publier d'abord (sans fusion), fusionner ensuite par une NOUVELLE passe."""
    pid = linear_project(state_url, 1)
    await run_pass(state_url, source, bridge, pid, settings={"BUILD_AUTO_MERGE": False})
    assert bridge.merge_calls() == []

    await run_pass(state_url, source, bridge, pid)

    assert len(bridge.merge_calls()) == 1 and statuses(state_url, pid) == {"T0": "merged"}
    assert git_out(source, "rev-parse", "HEAD") == bridge.branches["main"]


# ── course au PUT : la base bouge AU MOMENT de l'appel de fusion ───────────────────────────────────────────────────


async def test_a_base_that_moves_during_the_merge_call_is_refused_by_the_server_precondition(bridge, source, state_url):
    pid = linear_project(state_url, 1)
    await run_pass(state_url, source, bridge, pid, settings={"BUILD_AUTO_MERGE": False})
    moved = {}

    def advance(server):
        moved["sha"] = server.write_remote_file("main", "concurrent.txt", "autre fusion\n")

    bridge.before_merge = advance
    await run_pass(state_url, source, bridge, pid)

    assert (
        len(bridge.merge_calls()) >= 1
    )  # l'appel part avec le SHA de tête exact ; le serveur le refuse (règle « à jour »)
    assert bridge.merge_calls()[0][2]["sha"] == bridge.prs[101]["head"]["sha"]
    assert bridge.merged_pr_numbers() == []
    assert bridge.branches["main"] == moved["sha"], "la base n'a reçu que le commit de l'autre contributeur"
    assert statuses(state_url, pid) == {"T0": "in_review"}
    assert all(c.state != "synced" for c in open_manager(state_url).list_task_merges(pid))


# ── crash APRÈS la fusion distante, AVANT l'écriture locale : une seule fusion, reprise par réconciliation ──────────


class Crash(BaseException):
    """Mort du process entre le succès distant et l'écriture de l'état (n'est pas une ``Exception`` : rien ne l'avale)."""


async def test_a_crash_after_the_remote_merge_is_reconciled_on_restart_without_a_second_merge(
    bridge, source, state_url
):
    pid = linear_project(state_url, 2)

    def die(server, result):
        raise Crash()

    bridge.after_merge = die
    with pytest.raises(Crash):
        await run_pass(state_url, source, bridge, pid)

    # état durable laissé par le crash : intention écrite AVANT l'appel, fusion distante faite, rien d'autre
    pending = open_manager(state_url).list_task_merges(pid)
    assert [(c.state, c.merge_sha) for c in pending] == [("merge_pending", None)]
    assert len(bridge.merge_calls()) == 1 and bridge.merged_pr_numbers() == [101]
    assert statuses(state_url, pid) == {"T0": "in_review", "T1": "todo"}

    agent = DeliveringAgent()
    agent.calls = 1  # la tâche suivante porte le fichier delivered-1.txt (la 1ʳᵉ a déjà livré delivered-0.txt)
    result = await run_pass(state_url, source, bridge, pid, agent=agent)

    assert result.stop_reason == "completed"
    assert statuses(state_url, pid) == {"T0": "merged", "T1": "merged"}
    assert [b["sha"] for b in bridge.merge_bodies()] == [bridge.prs[101]["head"]["sha"], bridge.prs[102]["head"]["sha"]]
    assert agent.seen == [["delivered-0.txt"]], "la tâche suivante part du contenu livré ET resynchronisé"
    assert git_out(source, "rev-parse", "HEAD") == bridge.branches["main"]


# ── resynchronisation en échec : blocage durable, y compris pour les tâches indépendantes ──────────────────────────


async def test_a_failed_resync_blocks_every_task_until_the_checkout_is_resynchronised_without_a_second_merge(
    bridge, source, state_url
):
    """Deux PR en vol autorisées (``STRICT_MAX_INFLIGHT_PRS=2``) : S0 et S1 sont livrées AVANT la fusion de S0 ; la fusion de S0
    est suivie d'un échec réel de resynchronisation ; S2 (indépendante, jamais commencée) ne doit PAS démarrer."""
    settings = {"STRICT_MAX_INFLIGHT_PRS": 2}
    pid = sibling_project(state_url, 3)
    agent = DeliveringAgent()
    good_origin = bridge.break_origin(source)  # ``git fetch origin`` échoue VRAIMENT : aucun faux booléen

    first = await run_pass(state_url, source, bridge, pid, agent=agent, settings=settings)

    assert first.stop_reason == "merge_sync_pending"
    assert len(bridge.merge_calls()) == 1 and agent.calls == 2, "S0 et S1 étaient déjà livrées ; S2 n'est pas lancée"
    cycle = open_manager(state_url).list_task_merges(pid)[0]
    assert cycle.state == "merged_unsynced" and cycle.merge_sha and cycle.last_error
    assert statuses(state_url, pid) == {
        "S0": "in_review",
        "S1": "in_review",
        "S2": "todo",
    }  # S0 jamais « merged » sans sync

    # nouvelle instance du manager, origine toujours cassée : le blocage est DURABLE, pas un état de process
    second = await run_pass(state_url, source, bridge, pid, agent=agent, settings=settings)
    assert second.stop_reason == "merge_sync_pending"
    assert len(bridge.merge_calls()) == 1 and agent.calls == 2
    assert statuses(state_url, pid) == {"S0": "in_review", "S1": "in_review", "S2": "todo"}

    bridge.restore_origin(source, good_origin)
    await run_pass(state_url, source, bridge, pid, agent=agent, settings=settings)

    final = statuses(state_url, pid)
    assert final["S0"] == "merged", "la reprise a resynchronisé S0 sans la refusionner"
    assert (
        bridge.merged_pr_numbers().count(101) == 1 and bridge.merge_bodies()[0]["sha"] == bridge.prs[101]["head"]["sha"]
    )
    assert git_out(source, "rev-parse", "HEAD") == bridge.branches["main"], "le checkout est sur la fusion réelle"
    assert agent.seen[2] == ["delivered-0.txt"], (
        "S2 ne démarre qu'après la resynchronisation, sur le contenu livré par S0"
    )
    # sûreté : la PR de S1 a été validée sur l'ANCIENNE base ; la base a avancé avec S0, donc elle n'est jamais fusionnée
    assert 102 not in bridge.merged_pr_numbers() and final["S1"] == "in_review"


# ── fusion MANUELLE / hors moteur, sans cycle initial : reprise durable depuis un NOUVEAU manager ───────────────────


async def test_a_manual_merge_with_a_failed_sync_stays_blocking_even_when_pr_discovery_disappears(
    bridge, source, state_url
):
    strict = {"BUILD_AUTO_MERGE": False, "DEPS_REQUIRE_MERGED": True}  # mode strict : une PR en vol à la fois
    pid = sibling_project(state_url, 2)
    agent = DeliveringAgent()
    first = await run_pass(state_url, source, bridge, pid, agent=agent, settings=strict)
    assert first.stop_reason == "awaiting_merge" and agent.calls == 1
    assert open_manager(state_url).list_task_merges(pid) == [], "aucune fusion du moteur : pas de cycle initial"

    bridge.merge_out_of_band(101)  # un humain fusionne la PR hors moteur
    good_origin = bridge.break_origin(source)  # le checkout ne peut pas se resynchroniser

    second = await run_pass(state_url, source, bridge, pid, agent=agent, settings=strict)
    cycles = open_manager(state_url).list_task_merges(pid)
    assert [(c.origin, c.state, c.head_sha, c.proof_id) for c in cycles] == [
        ("external", "merged_unsynced", None, None)
    ], "la fusion externe est consignée avec son SHA connu, SANS preuve inventée"
    assert second.stop_reason == "repo_sync_failed", (
        second.stop_reason
    )  # le pilote lit la fusion et échoue à resynchroniser
    assert agent.calls == 1, "la tâche indépendante n'est pas lancée sur un clone périmé"

    # la découverte des PR devient indisponible, puis la PR disparaît : le blocage ne dépend plus de GitHub
    bridge.fail("GET", r"/pulls", status=503, times=None)
    third = await run_pass(state_url, source, bridge, pid, agent=agent, settings=strict)
    assert third.stop_reason == "merge_sync_pending" and agent.calls == 1  # barrière du runtime, sans GitHub
    bridge.failures.clear()
    del bridge.prs[101]
    fourth = await run_pass(state_url, source, bridge, pid, agent=agent, settings=strict)
    assert fourth.stop_reason == "merge_sync_pending" and agent.calls == 1
    assert [c.state for c in open_manager(state_url).list_task_merges(pid)] == ["merged_unsynced"]

    bridge.restore_origin(source, good_origin)
    last = await run_pass(state_url, source, bridge, pid, agent=agent, settings=strict)

    assert [c.state for c in open_manager(state_url).list_task_merges(pid)] == ["synced"]
    assert statuses(state_url, pid)["S0"] == "merged" and agent.calls == 2
    assert agent.seen[1] == ["delivered-0.txt"], "S1 démarre sur le contenu fusionné à la main, après resynchronisation"
    assert len(bridge.merge_calls()) == 1, "le moteur n'a émis aucune fusion : seul l'appel hors moteur existe"
    assert last.stop_reason == "awaiting_merge"  # S1 livrée, en attente de sa fusion


# ── contrats scellés (oracles QA du plan) : rejoués à la livraison, conservés d'une tâche à l'autre ───────────────────


class OracleQACtx:
    """Sampling QA déterministe : une source pytest par titre de tâche, lue dans le contrat du prompt."""

    def __init__(self, sources):
        self._sources = sources
        self.calls = []

    async def aclose(self):
        return None

    async def sample(self, **kwargs):
        self.calls.append(kwargs)
        prompt = str(kwargs.get("messages") or "")
        section = prompt.split("## Contrat de la tâche", 1)[-1].split("## DAG", 1)[0]
        for title, source in self._sources.items():
            if f'"title":"{title}\\n"' in section:
                return SimpleNamespace(text=source)
        raise AssertionError(f"titre inconnu dans le contrat QA: {section[:200]!r}")


def _file_oracle(name):
    return f"from pathlib import Path\n\n\ndef test_contract():\n    assert (Path.cwd() / '{name}').is_file()\n"


async def plan_with_oracles(monkeypatch, url, sources, *, depends=True):
    """Plan RÉEL (persistance, hash, approbation) dont les oracles QA sont scellés au plan-time."""
    from collegue.pilot import approve_project_plan_from_settings, plan_project_from_settings
    from collegue.planner.spec_generator import Spec

    async def _generate(problem, ctx, **kw):
        return Spec(title="W3", summary=problem, acceptance_criteria=["AC1"])

    async def _decompose(spec, ctx, *, manager, project_id, **kw):
        previous = None
        for title in sources:
            previous = manager.add_task(
                project_id,
                title=title,
                acceptance=f"critère {title}",
                depends_on=[previous] if previous and depends else None,
            )
        return manager.get_tasks(project_id)

    monkeypatch.setattr("collegue.planner.spec_generator.generate_spec", _generate)
    monkeypatch.setattr("collegue.planner.decomposer.decompose", _decompose)
    ctx = OracleQACtx(sources)
    plan = await plan_project_from_settings(
        "W3-QA",
        "construire une application testable",
        owner=OWNER,
        repo=REPO,
        settings_obj=SimpleNamespace(GATE_ACCEPTANCE_TESTS=True, LLM_PROVIDER="test", LLM_MODEL="qa-fixture"),
        manager=open_manager(url, create=True),
        ctx=ctx,
    )
    approve_project_plan_from_settings(
        plan.project_id, plan.plan_hash, settings_obj=SimpleNamespace(), manager=open_manager(url)
    )
    assert open_manager(url).get_project(plan.project_id).acceptance_tests_required is True
    return plan.project_id, ctx


CONTRACT_SETTINGS = {"GATE_ACCEPTANCE_TESTS": False, "TASK_MAX_ATTEMPTS": 1, "TASK_RETRY_BACKOFF_SECONDS": 0}


def oracle_sandbox():
    from oracle_sandbox import LocalOracleSandbox

    return LocalOracleSandbox(fallback=_Sandbox())


async def test_sealed_contracts_are_replayed_and_kept_across_tasks_even_when_the_gate_flag_is_off(
    monkeypatch, bridge, source, state_url
):
    """Le projet EXIGE durablement ses oracles : ``GATE_ACCEPTANCE_TESTS=false`` au run ne les désactive pas ; la tâche 2
    rejoue le contrat LIVRÉ de la tâche 1 ; la preuve de chaque PR porte l'oracle (rouge sur la préimage, vert sur le candidat)."""
    pid, qa = await plan_with_oracles(
        monkeypatch, state_url, {"A": _file_oracle("delivered-0.txt"), "B": _file_oracle("delivered-1.txt")}
    )
    sandbox = oracle_sandbox()

    result = await run_pass(state_url, source, bridge, pid, sandbox=sandbox, settings=CONTRACT_SETTINGS)

    assert result.stop_reason == "completed", [t.last_error for t in open_manager(state_url).get_tasks(pid)]
    assert statuses(state_url, pid) == {"A": "merged", "B": "merged"}
    assert len(qa.calls) == 2, "aucun nouvel échantillonnage QA pendant le run"
    reloaded = open_manager(state_url)
    proofs = {
        n: load_delivery_proof(reloaded, pid, owner=OWNER, repo=REPO, pr_number=n, head_sha=pr["head"]["sha"])
        for n, pr in bridge.prs.items()
    }
    assert all(p.passed and p.contracts_required and p.verdict("contracts").passed for p in proofs.values())
    first, second = proofs[101], proofs[102]
    assert [o.role for o in first.oracles] == ["current"]
    assert sorted(o.role for o in second.oracles) == ["current", "delivered"], (
        "la tâche 2 a rejoué le contrat livré de la tâche 1"
    )
    for proof in proofs.values():
        current = next(o for o in proof.oracles if o.role == "current")
        assert current.preimage.status == "red-assertion" and current.candidate.status == "green"
        assert current.expected_preimage == "red-assertion"


async def test_a_task_that_breaks_a_delivered_contract_is_not_published(monkeypatch, bridge, source, state_url):
    pid, _ = await plan_with_oracles(
        monkeypatch, state_url, {"A": _file_oracle("delivered-0.txt"), "B": _file_oracle("delivered-1.txt")}
    )
    sandbox = oracle_sandbox()

    class BreakingAgent(DeliveringAgent):
        def implement_issue(self, workspace, issue):
            if self.calls == 1:  # tâche B : livre son fichier mais SUPPRIME celui que le contrat livré de A exige
                os.remove(os.path.join(workspace, "delivered-0.txt"))
            return super().implement_issue(workspace, issue)

    result = await run_pass(
        state_url, source, bridge, pid, agent=BreakingAgent(), sandbox=sandbox, settings=CONTRACT_SETTINGS
    )

    final = statuses(state_url, pid)
    assert final["A"] == "merged" and final["B"] != "in_review" and final["B"] != "merged", final
    assert sorted(bridge.prs) == [101], "aucune PR n'est ouverte pour la tâche qui casse le contrat livré"
    errors = [t.last_error or "" for t in open_manager(state_url).get_tasks(pid) if t.title == "B"]
    # seul le contrat LIVRÉ de A peut échouer (le contrat courant de B est satisfait : delivered-1.txt existe)
    assert errors and "ORACLE D'ACCEPTATION REFUSÉ" in errors[0] and "AssertionError" in errors[0], errors
    assert result.stop_reason != "completed"


# ── intégrité du contenu publié : ce qui est testé EST ce qui est livré (ou rien n'est livré) ────────────────────────


class FilesAgent(FakeCodeAgent):
    """Agent qui écrit un jeu de fichiers puis applique ``extra(workspace)`` (binaire, lien, mode, résidu…)."""

    def __init__(self, files=None, extra=None):
        super().__init__(files if files is not None else {"delivered-0.txt": "livraison 0\n"})
        self._extra = extra

    def implement_issue(self, workspace, issue):
        result = super().implement_issue(workspace, issue)
        if self._extra:
            self._extra(workspace)
        return result


def _write_binary(workspace):
    with open(os.path.join(workspace, "blob.bin"), "wb") as handle:
        handle.write(b"\xff\xfe\x00\x01")


def _write_link(workspace):
    os.symlink("delivered-0.txt", os.path.join(workspace, "link.txt"))


def _write_executable(workspace):
    path = os.path.join(workspace, "run.sh")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\necho ok\n")
    os.chmod(path, 0o755)


@pytest.mark.parametrize(
    "name, extra",
    [("binary", _write_binary), ("symlink", _write_link), ("executable", _write_executable)],
)
async def test_unrepresentable_formats_are_refused_before_anything_is_written_to_the_remote(
    name, extra, bridge, source, state_url
):
    pid = linear_project(state_url, 1)
    await run_pass(
        state_url,
        source,
        bridge,
        pid,
        agent=FilesAgent(extra=extra),
        settings={"BUILD_AUTO_MERGE": False, "TASK_MAX_ATTEMPTS": 1},
    )

    assert bridge.prs == {} and bridge.remote.writes == [], "rien n'est poussé : ni fichier, ni PR"
    assert not [b for b in bridge.branches if b.startswith("collegue/")]
    task = open_manager(state_url).get_tasks(pid)[0]
    assert task.status != "in_review" and "LIVRAISON REFUSÉE" in (task.last_error or ""), task.last_error


async def test_the_same_delivery_without_an_unrepresentable_file_is_published(bridge, source, state_url):
    pid = linear_project(state_url, 1)
    await run_pass(state_url, source, bridge, pid, agent=FilesAgent(), settings={"BUILD_AUTO_MERGE": False})
    assert sorted(bridge.prs) == [101] and statuses(state_url, pid) == {"T0": "in_review"}


def _hidden_package(workspace, *, nested_git, ignored):
    package = os.path.join(workspace, "hidden_pkg")
    os.makedirs(package, exist_ok=True)
    with open(os.path.join(package, "__init__.py"), "w", encoding="utf-8") as handle:
        handle.write("VALUE = 7\n")
    if nested_git:  # dépôt imbriqué inerte : `git clean -f` ne le purge pas sans `-ff`
        os.makedirs(os.path.join(package, ".git"), exist_ok=True)
        with open(os.path.join(package, ".git", "HEAD"), "w", encoding="utf-8") as handle:
            handle.write("ref: refs/heads/main\n")
    if ignored:
        with open(os.path.join(workspace, ".gitignore"), "w", encoding="utf-8") as handle:
            handle.write("hidden_pkg/\n")


HIDDEN_ORACLE = (
    "import importlib.util\n\n\ndef test_contract():\n    assert importlib.util.find_spec('hidden_pkg') is not None\n"
)


@pytest.mark.parametrize("nested_git", [False, True], ids=["ignored_package", "ignored_package_with_nested_git"])
async def test_an_ignored_input_that_the_oracle_needs_cannot_make_the_delivery_pass(
    nested_git, monkeypatch, bridge, source, state_url
):
    pid, _ = await plan_with_oracles(monkeypatch, state_url, {"R": HIDDEN_ORACLE})
    agent = FilesAgent(extra=lambda ws: _hidden_package(ws, nested_git=nested_git, ignored=True))

    await run_pass(state_url, source, bridge, pid, agent=agent, sandbox=oracle_sandbox(), settings=CONTRACT_SETTINGS)

    assert bridge.prs == {} and bridge.remote.writes == [], (
        "le module ignoré n'est pas livré : la livraison est refusée"
    )
    task = open_manager(state_url).get_tasks(pid)[0]
    assert task.status != "in_review" and "ORACLE D'ACCEPTATION REFUSÉ" in (task.last_error or ""), task.last_error


async def test_the_same_package_tracked_instead_of_ignored_is_delivered_and_the_oracle_passes(
    monkeypatch, bridge, source, state_url
):
    pid, _ = await plan_with_oracles(monkeypatch, state_url, {"R": HIDDEN_ORACLE})
    agent = FilesAgent(extra=lambda ws: _hidden_package(ws, nested_git=False, ignored=False))

    await run_pass(state_url, source, bridge, pid, agent=agent, sandbox=oracle_sandbox(), settings=CONTRACT_SETTINGS)

    assert sorted(bridge.prs) == [101] and statuses(state_url, pid) == {"R": "merged"}
    assert "hidden_pkg/__init__.py" in bridge.remote.files_at("main")


# ── PR existante : ré-exécution identique idempotente, vrai conflit conservé ──────────────────────────────────────────


async def test_an_identical_rerun_on_an_existing_pr_keeps_a_single_valid_proof_and_still_merges(
    bridge, source, state_url
):
    pid = linear_project(state_url, 1)
    await run_pass(state_url, source, bridge, pid, agent=FilesAgent(), settings={"BUILD_AUTO_MERGE": False})
    head = bridge.prs[101]["head"]["sha"]
    manager = open_manager(state_url)
    manager.update_task_status(
        manager.get_tasks(pid)[0].id, "todo"
    )  # crash avant l'écriture du statut : la tâche est rejouée

    await run_pass(state_url, source, bridge, pid, agent=FilesAgent(), settings={"BUILD_AUTO_MERGE": False})
    assert sorted(bridge.prs) == [101], "la PR existante est réutilisée, pas dupliquée"
    entries = [e for e in open_manager(state_url).get_decision_journal(pid, "delivery-proof:v1:")]
    assert len({e.summary for e in entries}) == 1, "une seule preuve pour cette tête (identifiant stable)"
    proof = load_delivery_proof(open_manager(state_url), pid, owner=OWNER, repo=REPO, pr_number=101, head_sha=head)
    assert proof.passed is True

    await run_pass(state_url, source, bridge, pid)
    assert statuses(state_url, pid) == {"T0": "merged"} and len(bridge.merge_calls()) == 1


async def test_a_rerun_with_different_content_never_adopts_the_existing_pr(bridge, source, state_url):
    pid = linear_project(state_url, 1)
    await run_pass(state_url, source, bridge, pid, agent=FilesAgent(), settings={"BUILD_AUTO_MERGE": False})
    head = bridge.prs[101]["head"]["sha"]
    manager = open_manager(state_url)
    manager.update_task_status(manager.get_tasks(pid)[0].id, "todo")

    await run_pass(
        state_url,
        source,
        bridge,
        pid,
        agent=FilesAgent({"delivered-0.txt": "AUTRE contenu\n"}),
        settings={"BUILD_AUTO_MERGE": False, "TASK_MAX_ATTEMPTS": 1},
    )

    task = open_manager(state_url).get_tasks(pid)[0]
    assert task.status != "in_review", (
        "le contenu différent n'est jamais présenté comme la livraison de la PR existante"
    )
    assert bridge.prs[101]["head"]["sha"] == head and sorted(bridge.prs) == [101]
    assert bridge.merge_calls() == []
    assert load_delivery_proof(
        open_manager(state_url), pid, owner=OWNER, repo=REPO, pr_number=101, head_sha=head
    ).passed


# ── PR sans preuve (ouverte hors moteur / ancienne PR) : jamais reconstruite depuis son texte ──────────────────────


async def test_a_pr_opened_outside_the_engine_has_no_proof_and_is_never_merged_even_with_green_checks(
    bridge, source, state_url, caplog
):
    from collegue.executor.workspace import branch_for_issue

    pid = linear_project(state_url, 1)
    manager = open_manager(state_url)
    task = manager.get_tasks(pid)[0]
    branch = branch_for_issue(task.issue_number or task.id)
    bridge.branches[branch] = bridge.branches["main"]
    bridge.write_remote_file(branch, "delivered-0.txt", "livraison 0\n")
    pr = bridge.clients().prs.create_pr(
        OWNER, REPO, "titre", branch, "main", "Preuve de livraison : OK (affirmé dans le corps)"
    )
    manager.update_task_status(task.id, "in_review")
    assert bridge.prs[pr.number]["head"]["sha"] == bridge.branches[branch]

    with caplog.at_level("WARNING"):
        await run_pass(state_url, source, bridge, pid)

    assert bridge.merge_calls() == [] and statuses(state_url, pid) == {"T0": "in_review"}
    assert "aucune preuve de livraison" in caplog.text and "jamais reconstruite depuis le texte de la PR" in caplog.text
    assert open_manager(state_url).list_task_merges(pid) == []


async def test_a_proof_of_an_earlier_head_does_not_cover_the_head_that_the_pr_now_has(bridge, source, state_url):
    pid = linear_project(state_url, 1)
    await run_pass(state_url, source, bridge, pid, settings={"BUILD_AUTO_MERGE": False})
    old_head = bridge.prs[101]["head"]["sha"]
    new_head = bridge.write_remote_file(bridge.prs[101]["head"]["ref"], "late.txt", "tardif\n")
    bridge.green(101)
    reloaded = open_manager(state_url)

    assert load_delivery_proof(reloaded, pid, owner=OWNER, repo=REPO, pr_number=101, head_sha=old_head).passed is True
    from collegue.executor.delivery_proof import DeliveryProofError

    with pytest.raises(DeliveryProofError, match="aucune preuve"):
        load_delivery_proof(reloaded, pid, owner=OWNER, repo=REPO, pr_number=101, head_sha=new_head)
    with pytest.raises(DeliveryProofError):  # autre PR, autre projet
        load_delivery_proof(reloaded, pid, owner=OWNER, repo=REPO, pr_number=999, head_sha=old_head)
    with pytest.raises(DeliveryProofError):
        load_delivery_proof(reloaded, pid + 1, owner=OWNER, repo=REPO, pr_number=101, head_sha=old_head)
