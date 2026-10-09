"""Campagne métier W4 DÉTERMINISTE : l'application d'audit (FastAPI + SQLite + Alembic) construite en trois tâches dépendantes.

Aucun modèle réel, aucune facture, aucune écriture GitHub réelle. Le scénario traverse les ENTRÉES PUBLIQUES du produit —
``plan_project_from_settings`` → ``approve_project_plan_from_settings`` → ``run_project_from_settings`` (exécution, preuve de
livraison, politique de fusion W3, resynchronisation) → ``run_improvement`` (promotion) → ``auto_merge_promotion`` (Phase 5) — avec
des doubles aux SEULES frontières externes :

* transport de sampling (planificateur + QA) : réponses déterministes, réservées et réglées dans le REGISTRE W2 par la vraie
  ``guarded_call`` (comme le transport de production) ;
* agent codeur : écrit les fichiers de référence de la tâche et rapporte un usage déterministe (réglé par ``settle_worker``) ;
* GitHub : vrai dépôt Git distant derrière les vrais clients (``tests/w3_remote_bridge.py``) ;
* sandbox : exécute RÉELLEMENT les oracles scellés et les tests du projet (python du venv, sans Docker) ;
* revue : ``FakeReviewer`` (hors périmètre métier).

Le rapport (``CampaignReport``) distingue réussite / non-exécution / arrêt budget / échec / validation incomplète et conserve les
SHA, empreintes d'oracles, résultats métier, compteurs du registre et le point d'arrêt.

Commande reproductible (hors pytest) : ``PYTHONPATH=.:tests python tests/w4_business_campaign.py --output rapport.json`` depuis la racine du dépôt,
avec les dépendances de test (fastapi, httpx, sqlalchemy, alembic, pypdf).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import w4_business_fixture as fixture
from github_fake_server import HttpError
from github_fakes import FakeRemote, git, make_source_repo
from oracle_sandbox import LocalOracleSandbox
from w3_remote_bridge import _PREFIX, BridgeServer

from collegue.core.llm.budget_guard import current_binding, guarded_call
from collegue.executor import FakeCodeAgent, FakeReviewer
from collegue.executor.delivery_proof import load_delivery_proof
from collegue.monitoring.sampling_usage import record_usage
from collegue.pilot import approve_project_plan_from_settings, plan_project_from_settings, run_project_from_settings
from collegue.pilot import w4_business as business
from collegue.pilot.w4_business import CampaignReport
from collegue.sandbox import SandboxResult
from collegue.state import ProjectStateManager

OWNER = REPO = "fixture"
MODEL = "gemini-2.5-flash"
# Usage déterministe rapporté par le worker, par tâche (tokens d'entrée, de sortie) et coût en dollars.
WORKER_USAGE = {1: (9000, 3000, 0.0125), 2: (11000, 3500, 0.0150), 3: (14000, 4200, 0.0190)}


def campaign_settings(**overrides: Any) -> SimpleNamespace:
    """Réglages de la campagne : enveloppe globale 2 USD / 250000 tokens / 900 s, registre strict, fusion BUILD activée."""
    values: Dict[str, Any] = dict(
        LLM_PROVIDER="gemini",
        LLM_MODEL=MODEL,
        LLM_PRICE_PROMPT_PER_1M=0.3,
        LLM_PRICE_COMPLETION_PER_1M=2.5,
        MAX_COST_USD=business.CAMPAIGN_BOUNDS.max_cost_usd,
        MAX_TOKENS_BUDGET=business.CAMPAIGN_BOUNDS.max_tokens,
        COLLEGUE_RUN_DEADLINE_SECONDS=float(business.CAMPAIGN_BOUNDS.max_seconds),
        BUDGET_MODE="strict",
        BUDGET_EXHAUSTED_ACTION="pause",
        BUDGET_ATTESTED_BYTE_TOKENIZER_MODELS=MODEL,
        GATE_ACCEPTANCE_TESTS=True,
        BUILD_AUTO_MERGE=True,
        AUTO_MERGE_ENABLED=False,
        AUTO_MERGE_CI_TIMEOUT_SECONDS=0,
        AUTO_MERGE_CI_POLL_SECONDS=0,
        DEPS_REQUIRE_MERGED=True,
        STRICT_MAX_INFLIGHT_PRS=1,
        TASK_MAX_ATTEMPTS=1,
        TASK_RETRY_BACKOFF_SECONDS=0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


# ── frontières simulées ──────────────────────────────────────────────────────────────────────────────────────────────────


class PlanningTransport:
    """Transport de sampling DÉTERMINISTE : réponses scriptées, réservation/règlement par la vraie ``guarded_call``.

    C'est la place du transport de production (réservation AVANT émission, règlement avec l'usage réel, retries compris)."""

    def __init__(self, oracles: Optional[Dict[int, str]] = None):
        self.oracles = dict(oracles or fixture.ORACLES)
        self.calls: List[str] = []
        self.usage: List[tuple] = []

    async def aclose(self) -> None:
        return None

    def _answer(self, kind: str, kwargs: Dict[str, Any]) -> Any:
        if kind == "spec":
            data = {
                "title": "Application d'audit",
                "summary": "API d'audits persistés avec export PDF.",
                "objectives": ["persister des audits", "les consulter", "les exporter en PDF"],
                "scope": "FastAPI + SQLite + Alembic",
                "acceptance_criteria": ["migration sur base vierge", "création puis lecture d'un audit", "PDF lisible"],
            }
            return SimpleNamespace(result=data, text=json.dumps(data, ensure_ascii=False))
        if kind == "decompose":
            tasks = [
                {
                    "title": fixture.TITLES[1],
                    "acceptance": "Alembic crée audits et findings sur une base vierge",
                    "depends_on": [],
                },
                {"title": fixture.TITLES[2], "acceptance": "POST /audits puis GET /audits/{id}", "depends_on": [0]},
                {
                    "title": fixture.TITLES[3],
                    "acceptance": "GET /audits/{id}/export.pdf contient les données",
                    "depends_on": [1],
                },
            ]
            data = {"tasks": tasks}
            return SimpleNamespace(result=data, text=json.dumps(data, ensure_ascii=False))
        prompt = str(kwargs.get("messages") or "")
        section = prompt.split("## Contrat de la tâche", 1)[-1].split("## DAG", 1)[0]
        for number, title in fixture.TITLES.items():
            if title in section:
                return SimpleNamespace(text=self.oracles[number], result=None)
        raise AssertionError(f"tâche inconnue dans le contrat QA: {section[:160]!r}")

    async def sample(self, **kwargs: Any) -> Any:
        result_type = getattr(kwargs.get("result_type"), "__name__", "")
        kind = "spec" if result_type == "Spec" else "decompose" if result_type == "_Decomposition" else "qa"
        messages = kwargs.get("messages")
        answer = self._answer(kind, kwargs)
        # Usage déterministe DÉRIVÉ du contenu (≈ 1 token / 4 octets), toujours sous la borne haute du transport.
        prompt_tokens = max(1, len(str(messages).encode("utf-8")) // 4)
        completion_tokens = max(1, len(str(getattr(answer, "text", "")).encode("utf-8")) // 4)
        binding = current_binding()
        self.calls.append(kind)
        self.usage.append((kind, prompt_tokens, completion_tokens))

        async def call() -> Any:
            record_usage(prompt_tokens, completion_tokens, MODEL)
            return answer

        if binding is None:
            return await call()
        return await guarded_call(
            call,
            binding=binding,
            model=MODEL,
            messages=messages,
            max_tokens=int(kwargs.get("max_tokens") or 4096),
            usage_of=lambda _response: (prompt_tokens, completion_tokens, MODEL),
        )


class ReferenceAgent(FakeCodeAgent):
    """Codeur déterministe : écrit les fichiers de référence de la tâche et rapporte un usage fixe (réglé par le produit)."""

    def __init__(self, *, wrong_data_stage_3: bool = False, usage: Optional[Dict[int, tuple]] = None):
        super().__init__()
        self.calls = 0
        self.tasks_seen: List[str] = []
        self.starting_files: List[List[str]] = []
        self.wrong_data_stage_3 = wrong_data_stage_3
        self.usage = usage or WORKER_USAGE

    def implement_issue(self, workspace, issue):
        stage = next(
            (n for n, title in fixture.TITLES.items() if title.startswith(issue.title.split("\n")[0][:20])), None
        )
        if stage is None:
            stage = next(n for n, title in fixture.TITLES.items() if title in issue.title)
        self.calls += 1
        self.tasks_seen.append(fixture.TITLES[stage])
        self.starting_files.append(
            sorted(
                str(p.relative_to(workspace))
                for p in Path(workspace).rglob("*")
                if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts
            )
        )
        source = fixture.WRONG_DATA_STAGE_3 if (self.wrong_data_stage_3 and stage == 3) else fixture.STAGE_FILES[stage]
        self._files = dict(source)
        result = super().implement_issue(workspace, issue)
        prompt, completion, cost = self.usage[stage]
        return dataclasses.replace(
            result, prompt_tokens=prompt, completion_tokens=completion, cost_usd=cost, cost_authoritative=True
        )


HEALTH_COMMAND = "w4-business-health"
COVERAGE_COMMAND = "python -m pytest -q -p no:cacheprovider --cov=app --cov-report=term-missing tests"


class BusinessSandbox:
    """Exécute RÉELLEMENT les commandes du gate, des oracles, de la mesure et de la santé (python du venv, sans Docker).

    * oracles : ``LocalOracleSandbox`` (commande de production, rapport émis par le lanceur de production) ;
    * santé de ``main`` : la sonde MÉTIER (``verify_business_checkout``) sur le clone du commit de fusion — comportement réel
      de l'application (base vierge, migration, HTTP, PDF lu par un vrai lecteur), pas un booléen choisi ;
    * mesure : la commande de couverture telle quelle ; gate : les tests du projet, sans l'étape d'installation réseau."""

    def __init__(self):
        self.oracles = LocalOracleSandbox()
        self.gate_commands: List[str] = []
        self.health_runs: List[Dict[str, Any]] = []

    def _env(self) -> Dict[str, str]:
        return {
            **os.environ,
            "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"],
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def run_tests(self, workspace, command="pytest -q"):
        if "COLLEGUE-ORACLE" in command:
            return self.oracles.run_tests(workspace, command)
        if command == HEALTH_COMMAND:
            observation = business.verify_business_checkout(
                str(workspace), python=sys.executable, runner=business.trusted_local_runner
            )
            record = {
                "status": observation.status,
                "failed_checks": observation.failed,
                "pdf_text_excerpt": observation.observations.get("write.pdf_text_excerpt"),
                "workspace": str(workspace),
            }
            self.health_runs.append(record)
            exit_code = 0 if observation.status == "passed" else 1
            return SandboxResult(exit_code=exit_code, stdout=json.dumps(record, ensure_ascii=False), stderr="")
        self.gate_commands.append(command)
        run = command if "--cov" in command else "python -m pytest -q -p no:cacheprovider tests"
        proc = subprocess.run(
            ["sh", "-c", run], cwd=str(workspace), capture_output=True, text=True, env=self._env(), timeout=180
        )
        return SandboxResult(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


class _Ctx:
    async def aclose(self):
        return None


# ── monde de la campagne ───────────────────────────────────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class World:
    root: Path
    source: str
    bridge: Any
    url: str
    settings: SimpleNamespace
    transport: PlanningTransport
    agent: ReferenceAgent
    sandbox: BusinessSandbox
    project_id: Optional[int] = None
    plan_hash: Optional[str] = None
    origin_url: Optional[str] = None

    def manager(self, *, create: bool = False) -> ProjectStateManager:
        """Une NOUVELLE instance du gestionnaire sur la même base (équivalent d'un redémarrage du process)."""
        return ProjectStateManager.from_url(self.url, create=create)

    def tip(self) -> str:
        return self.bridge.branches["main"]

    def checkout_head(self) -> str:
        return git(self.source, "rev-parse", "HEAD")


class BusinessBridge(BridgeServer):
    """Pont W3 + réglage de dépôt « supprimer la branche de tête après fusion » (comme sur GitHub quand l'option est activée).

    Sans cette option, la branche ``collegue/issue-<n>`` d'une tâche BUILD fusionnée subsiste et une ronde IMPROVE de même
    numéro la RÉUTILISERAIT (voir ``reports/w4-b.md``, constat « collision de branches BUILD/IMPROVE »)."""

    delete_head_after_merge = True

    def _merge(self, number: int, body: Dict[str, Any]) -> Dict[str, Any]:
        head_ref = self.prs[number]["head"]["ref"]
        result = super()._merge(number, body)
        if self.delete_head_after_merge and self.prs[number]["merged"]:
            try:
                del self.branches[head_ref]
            except KeyError:
                pass
        return result

    def _post(self, path: str, data: Dict[str, Any]) -> Any:
        if path == f"{_PREFIX}/git/commits":  # Git Data API : commit sur un tree EXISTANT (revert distant)
            self.calls.append(("POST", path, dict(data)))
            self._maybe_fail("POST", path)
            sha = self.commit([str(p) for p in data["parents"]], tree=str(data["tree"]), message=str(data["message"]))
            return {"sha": sha, "tree": {"sha": data["tree"]}, "parents": [{"sha": p} for p in data["parents"]]}
        return super()._post(path, data)

    def _delete(self, path: str, data: Dict[str, Any]) -> Any:
        match = re.fullmatch(rf"{_PREFIX}/git/refs/heads/(.+)", path)
        if match:  # suppression d'une branche (nettoyage de la branche de revert)
            self.calls.append(("DELETE", path, dict(data)))
            self._maybe_fail("DELETE", path)
            if match.group(1) not in self.branches:
                raise HttpError("Not Found", status_code=404)
            del self.branches[match.group(1)]
            return {}
        return super()._delete(path, data)


def make_business_bridge(root: Path, source: str) -> BusinessBridge:
    remote = FakeRemote(root, source, base="main")
    server = BusinessBridge(remote)
    server.protect()
    server.attach_operator_checkout(source)
    return server


def build_world(
    root: Path, *, settings: Optional[SimpleNamespace] = None, agent: Optional[ReferenceAgent] = None
) -> World:
    root.mkdir(parents=True, exist_ok=True)
    source = make_source_repo(root / "operator", dict(fixture.SEED))
    bridge = make_business_bridge(root, source)
    url = f"sqlite:///{root / 'state.db'}"
    ProjectStateManager.from_url(url, create=True)
    return World(
        root=root,
        source=source,
        bridge=bridge,
        url=url,
        settings=settings or campaign_settings(),
        transport=PlanningTransport(),
        agent=agent or ReferenceAgent(),
        sandbox=BusinessSandbox(),
    )


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── étapes ──────────────────────────────────────────────────────────────────────────────────────────────────────────────


async def plan(world: World, step: Any) -> None:
    result = await plan_project_from_settings(
        "Application d'audit W4",
        business.BUSINESS_PROBLEM,
        owner=OWNER,
        repo=REPO,
        settings_obj=world.settings,
        manager=world.manager(),
        ctx=world.transport,
        decompose_exact_task_count=3,
    )
    world.project_id, world.plan_hash = result.project_id, result.plan_hash
    manager = world.manager()
    tasks = manager.get_tasks(world.project_id)
    assert [t.title.strip() for t in tasks] == [fixture.TITLES[n] for n in (1, 2, 3)], [t.title for t in tasks]
    assert [list(t.depends_on or []) for t in tasks] == [[], [tasks[0].id], [tasks[1].id]], "trois tâches dépendantes"
    project = manager.get_project(world.project_id)
    assert project.acceptance_tests_required is True, "le projet exige durablement ses oracles"
    for number, task in zip((1, 2, 3), tasks):
        assert task.acceptance_test_sha256 == sha256(fixture.ORACLES[number]), (
            "oracle scellé = source fournie par la QA"
        )
    step.evidence.update(
        project_id=world.project_id,
        plan_hash=world.plan_hash,
        oracle_fingerprints={str(n): sha256(fixture.ORACLES[n]) for n in (1, 2, 3)},
        planning_calls=list(world.transport.calls),
    )
    assert world.transport.calls == ["spec", "decompose", "qa", "qa", "qa"]


def approve(world: World, step: Any) -> None:
    approve_project_plan_from_settings(
        world.project_id, world.plan_hash, settings_obj=world.settings, manager=world.manager()
    )
    project = world.manager().get_project(world.project_id)
    assert project.approved_plan_hash == world.plan_hash and project.status == "approved"
    step.evidence["approved_plan_hash"] = project.approved_plan_hash


async def run_pass(world: World, *, max_iterations: Optional[int] = 1, **overrides: Any) -> Any:
    """Une passe du produit sur un NOUVEAU gestionnaire (redémarrage), avec la politique de fusion RÉELLE."""
    kwargs: Dict[str, Any] = dict(
        owner=OWNER,
        repo=REPO,
        dry_run=False,
        settings_obj=world.settings,
        manager=world.manager(),
        sandbox=world.sandbox,
        agent=world.agent,
        reviewer=FakeReviewer(),
        clients=world.bridge.clients(),
        ctx=_Ctx(),
        max_iterations=max_iterations,
    )
    kwargs.update(overrides)
    return await run_project_from_settings(world.project_id, world.source, **kwargs)


def task_statuses(world: World) -> Dict[str, str]:
    return {t.title.strip(): t.status for t in world.manager().get_tasks(world.project_id)}


def proofs_by_pr(world: World) -> Dict[int, Any]:
    manager = world.manager()
    return {
        number: load_delivery_proof(
            manager, world.project_id, owner=OWNER, repo=REPO, pr_number=number, head_sha=pr["head"]["sha"]
        )
        for number, pr in world.bridge.prs.items()
    }


def oracle_summary(proof: Any) -> List[Dict[str, Any]]:
    rows = []
    for item in proof.oracles:
        pre, cand = item.preimage, item.candidate
        rows.append(
            {
                "role": item.role,
                "task_id": item.task_id,
                "source_sha256": item.source_sha256,
                "expected_preimage": item.expected_preimage,
                "preimage": None
                if pre is None
                else {
                    "status": pre.status,
                    "assertion_failures": pre.assertion_failures,
                    "errors": pre.errors,
                    "collection_errors": pre.collection_errors,
                    "skipped": pre.skipped,
                    "executed": pre.executed,
                },
                "candidate": None
                if cand is None
                else {
                    "status": cand.status,
                    "passed": cand.passed,
                    "failed": cand.failed,
                    "errors": cand.errors,
                    "collection_errors": cand.collection_errors,
                    "skipped": cand.skipped,
                    "executed": cand.executed,
                },
            }
        )
    return rows


def registry(world: World) -> Dict[str, Any]:
    return business.registry_counters(world.manager(), world.project_id)


async def deliver_task(world: World, number: int, step: Any, *, merges_before: Optional[int] = None) -> None:
    """Une passe (= un redémarrage) : la tâche ``number`` est livrée, prouvée, fusionnée et le checkout resynchronisé."""
    before_registry = registry(world)
    before_calls = world.agent.calls
    before_merges = len(world.bridge.merge_calls()) if merges_before is None else merges_before
    result = await run_pass(world)
    statuses = task_statuses(world)
    assert world.agent.calls == before_calls + 1, "exactement une tâche exécutée par passe"
    wanted = {fixture.TITLES[n]: "merged" for n in range(1, number + 1)}
    assert {k: v for k, v in statuses.items() if k in wanted} == wanted, (statuses, result.stop_reason)
    assert len(world.bridge.merge_calls()) - before_merges == 1, "exactement UNE fusion émise pour cette tâche"
    proofs = proofs_by_pr(world)
    proof = proofs[100 + number]
    tip = world.tip()
    assert world.bridge.merged_pr_numbers() == [101 + i for i in range(number)]
    assert world.bridge.remote.tree_of(tip) == proof.tree_sha, "main contient EXACTEMENT le contenu prouvé"
    assert world.checkout_head() == tip, "le checkout opérateur est resynchronisé sur la fusion"
    assert proof.passed and proof.phase == "build" and proof.contracts_required
    after_registry = registry(world)
    expected_roles = ["current"] + ["delivered"] * (number - 1)
    assert sorted(o.role for o in proof.oracles) == sorted(expected_roles), (
        "contrats livrés rejoués d'une tâche à l'autre"
    )
    for evidence in proof.oracles:
        if evidence.role == "current":
            assert evidence.preimage.status == "red-assertion", "rouge PAR ASSERTION avant la correction"
            assert evidence.preimage.assertion_failures >= 1 and evidence.preimage.errors == 0
            assert evidence.preimage.collection_errors == 0 and evidence.preimage.skipped == 0
            assert evidence.candidate.status == "green", "vert après la correction, même empreinte"
            assert evidence.source_sha256 == sha256(fixture.ORACLES[number])
    started_with = set(world.agent.starting_files[-1])
    assert set(fixture.stage_files(number - 1)) <= started_with, (
        "la tâche démarre sur les dépendances INTÉGRÉES dans la base"
    )
    step.evidence.update(
        task=fixture.TITLES[number],
        pr_number=100 + number,
        head_sha=proof.head_sha,
        base_sha=proof.base_sha,
        tree_sha=proof.tree_sha,
        merge_tip=tip,
        proof_id=proof.proof_id,
        oracles=oracle_summary(proof),
        started_from_files=world.agent.starting_files[-1],
        registry_before=before_registry,
        registry_after=after_registry,
        stop_reason=result.stop_reason,
    )
    assert after_registry["consumed_tokens"] > before_registry["consumed_tokens"], (
        "la dépense du worker est au registre"
    )
    business.assert_registry_within_bounds(after_registry)


def verify_main(world: World, step: Any, *, notice: bool = True) -> None:
    """Vérification métier du livrable fusionné, sur le checkout opérateur resynchronisé."""
    observation = business.verify_business_checkout(
        world.source, python=sys.executable, require_legal_notice=notice, runner=business.trusted_local_runner
    )
    step.evidence.update(
        status=observation.status,
        checks=observation.checks,
        observations={k: v for k, v in observation.observations.items() if not k.endswith("stderr_tail")},
    )
    if observation.status == "incomplete":
        raise business.IncompleteValidation(observation.detail)
    assert observation.status == "passed", observation.failed


# ── amélioration, incident contrôlé et rollback Phase 5 ────────────────────────────────────────────────────────────────


class ImprovementAgent:
    """Codeur d'amélioration déterministe : ``action(workspace)`` fait le travail ; usage rapporté fixe (réglé par le produit)."""

    budget_enforcement = "test-double"

    def __init__(self, action, *, usage=(2400, 700, 0.0050)):
        self.action = action
        self.usage = usage
        self.calls = 0

    def implement_issue(self, workspace, issue):
        from collegue.executor import AgentResult

        self.calls += 1
        changed = self.action(Path(workspace))
        prompt, completion, cost = self.usage
        return AgentResult(
            success=True,
            files_changed=tuple(changed),
            prompt_tokens=prompt,
            completion_tokens=completion,
            cost_usd=cost,
            cost_authoritative=True,
        )


def reformat_python(workspace: Path) -> List[str]:
    """Le « codeur » reformate le code Python du projet (travail réel : ``ruff format``, lignes ≤ 88)."""
    from collegue.improve.metrics import _find_ruff

    ruff = _find_ruff()
    files = sorted(
        str(p.relative_to(workspace))
        for p in workspace.rglob("*.py")
        if ".git" not in p.parts and "__pycache__" not in p.parts
    )
    subprocess.run([ruff, "format", "--isolated", *files], cwd=workspace, check=True, capture_output=True)
    # Hygiène : la mesure de couverture dépose `.coverage` (binaire) dans le workspace ; la graine ne l'ignore pas et la
    # publication REFUSE un binaire. L'amélioration l'ignore donc explicitement (jamais livré par accident).
    ignore = workspace / ".gitignore"
    text = ignore.read_text(encoding="utf-8")
    if ".coverage" not in text:
        ignore.write_text(text + ".coverage\nhtmlcov/\n", encoding="utf-8")
        files.append(".gitignore")
    return files


def replace_header(content: str):
    def action(workspace: Path) -> List[str]:
        (workspace / "docs").mkdir(exist_ok=True)
        (workspace / "docs" / "export_header.md").write_text(content, encoding="utf-8")
        return ["docs/export_header.md"]

    return action


def improvement_settings(world: World, **overrides: Any) -> SimpleNamespace:
    values = dict(
        vars(world.settings),
        AUTO_MERGE_ENABLED=True,
        AUTO_REVERT_ENABLED=True,
        AUTO_REVERT_HEALTH_COMMAND=HEALTH_COMMAND,
        AUTO_MERGE_METHOD="squash",
        AUTO_MERGE_CI_TIMEOUT_SECONDS=0,
        AUTO_MERGE_CI_POLL_SECONDS=0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


async def improvement_pass(world: World, agent: ImprovementAgent, *, measure_fn=None) -> Any:
    """Une passe IMPROVE par l'entrée publique : handoff strict → ``run_improvement`` → Phase 5 (hook de production)."""
    import functools

    from collegue.improve import run_improvement
    from collegue.improve.metrics import measure

    run_imp = functools.partial(
        run_improvement,
        measure_fn=measure_fn or measure,
        plateau_rounds=1,
        max_iterations=1,
        coverage_command=COVERAGE_COMMAND,
    )
    return await run_pass(
        world,
        max_iterations=None,
        improve=True,
        run_improvement_fn=run_imp,
        agent=agent,
        settings_obj=improvement_settings(world),
    )


def metrics_row(m: Any) -> Dict[str, Any]:
    return {
        "coverage_pct": m.coverage_pct,
        "lint_violations": m.lint_violations,
        "security_findings": m.security_findings,
        "tests_passed": m.tests_passed,
        "composite": round(float(m.composite), 6),
        "review_measured": m.review_measured,
        "review_blocking": m.review_blocking,
    }


async def measure_checkout(world: World) -> Any:
    from collegue.improve.metrics import measure

    return await measure(
        world.source, None, sandbox=world.sandbox, reviewer=FakeReviewer(), coverage_command=COVERAGE_COMMAND
    )


def phase5_hooks(world: World, settings: SimpleNamespace):
    """Hooks Phase 5 de PRODUCTION, câblés comme ``run_project_from_settings`` (promotion = ``auto_merge_promotion``)."""
    from collegue.pilot.automerge import RiskPolicy, auto_merge_promotion
    from collegue.pilot.budget import BudgetTimeController
    from collegue.pilot.guard import RevertPolicy
    from collegue.pilot.phase5_resume import resume_phase5_incident

    policy = RiskPolicy.from_settings(settings)
    revert_policy = RevertPolicy.from_settings(settings)
    budget = BudgetTimeController(settings_obj=settings)
    common = dict(clients=world.bridge.clients(), owner=OWNER, repo=REPO, repo_source=world.source, base="main")

    async def promotion(pr):
        return await auto_merge_promotion(
            pr,
            policy=policy,
            revert_policy=revert_policy,
            sandbox=world.sandbox,
            manager=world.manager(),
            project_id=world.project_id,
            ci_timeout_seconds=0,
            ci_poll_seconds=0,
            continue_fn=budget.should_continue,
            **common,
        )

    async def recovery():
        return await resume_phase5_incident(
            world.project_id,
            manager=world.manager(),
            sandbox=world.sandbox,
            ci_timeout_seconds=0,
            ci_poll_seconds=0,
            continue_fn=budget.should_continue,
            auto_merge_enabled=policy.enabled,
            **common,
        )

    return promotion, recovery, budget


async def direct_improvement(world: World, agent: ImprovementAgent, *, measure_fn) -> Any:
    """``run_improvement`` (entrée publique de la boucle IMPROVE) avec les hooks Phase 5 de production."""
    from collegue.improve import run_improvement

    settings = improvement_settings(world)
    promotion, recovery, budget = phase5_hooks(world, settings)
    return await run_improvement(
        world.project_id,
        world.source,
        None,
        agent=agent,
        owner=OWNER,
        repo=REPO,
        manager=world.manager(),
        budget=budget,
        sandbox=world.sandbox,
        reviewer=FakeReviewer(),
        clients=world.bridge.clients(),
        dry_run=False,
        plateau_rounds=1,
        max_iterations=1,
        measure_fn=measure_fn,
        coverage_command=COVERAGE_COMMAND,
        promotion_hook=promotion,
        recovery_hook=recovery,
    )


class ClaimedGain:
    """Frontière de MESURE simulée pour le seul tour d'incident : baseline réelle, puis la même mesure réelle où l'outil
    « rapporte » trois violations de lint en moins. Le gain est annoncé par la mesure (un changement de documentation
    n'en produit aucun) ; la régression comportementale qui s'ensuit, elle, est RÉELLE."""

    def __init__(self, real):
        self.real = real
        self.calls = 0
        self.snapshots: List[Dict[str, Any]] = []

    async def __call__(self, workspace, ctx, **kwargs):
        from collegue.improve.metrics import composite_score

        result = await self.real(workspace, ctx, **kwargs)
        self.calls += 1
        if self.calls >= 2:
            lint = max(0, result.lint_violations - 3)
            result = dataclasses.replace(
                result,
                lint_violations=lint,
                composite=composite_score(result.coverage_pct, result.security_weighted, lint_violations=lint),
            )
        self.snapshots.append(metrics_row(result))
        return result


def make_report(campaign_id: str = "w4-business-deterministic") -> CampaignReport:
    return CampaignReport("deterministic", campaign_id)


# ── scénario ───────────────────────────────────────────────────────────────────────────────────────────────────────────

STEPS = [
    (
        "D01-plan",
        "Planification : SPEC, trois tâches dépendantes, oracles QA scellés au plan-time (transport simulé, registre W2)",
    ),
    ("D02-approve", "Approbation du hash du plan relu"),
    ("D03-task-1", "Tâche 1 — persistance et migration : livrée, prouvée, fusionnée, checkout resynchronisé"),
    (
        "D04-interrupted-sync",
        "Tâche 2 — fusion confirmée mais resynchronisation interrompue : arrêt durable, aucune tâche suivante",
    ),
    ("D05-restart-resume", "Redémarrage : reprise sans seconde fusion, puis tâche 3 sur la base intégrée"),
    (
        "D06-business",
        "Vérification métier de main : base vierge, migration, audit créé/lu/redémarré, PDF lu par un vrai lecteur",
    ),
    (
        "D07-improve",
        "Amélioration (lint réel) : promue avec preuve IMPROVE ; fusion automatique refusée par la politique de faible risque",
    ),
    (
        "D08-operator-merge",
        "Fusion opérateur de l'amélioration (SIMULÉE : merge humain hors moteur) et resynchronisation",
    ),
    ("D09-no-regression", "Aucune régression : tests, couverture, comportement métier ; lint réduit"),
    (
        "D10-incident-rollback",
        "Incident contrôlé (documentation autorisée) : santé rouge réelle, rollback Phase 5, comportement restauré",
    ),
    ("D11-acknowledge", "Acquittement de l'incident récupéré ; reprise autorisée"),
    ("D12-registry", "Registre W2 : enveloppe 2 USD / 250000 tokens respectée, rien de réservé ni d'inconnu"),
]


def declare_steps(report: CampaignReport) -> None:
    for step_id, title in STEPS:
        report.declare(step_id, title)


def snapshot_registry(report: CampaignReport, label: str, world: World) -> None:
    report.facts.setdefault("registry", {})[label] = registry(world)


async def run_deterministic_campaign(root: Path, *, campaign_id: str = "w4-business-deterministic") -> CampaignReport:
    """Campagne complète. Chaque étape est rapportée ; un échec arrête la suite (étapes restantes ``not_executed``)."""
    from collegue.executor.workspace import resync_repository_base
    from collegue.improve.metrics import measure
    from collegue.pilot import merge_cycle

    report = make_report(campaign_id)
    declare_steps(report)
    world = build_world(Path(root))
    seed_head = world.checkout_head()
    report.facts.update(
        seed_commit=seed_head,
        seed_files=sorted(fixture.SEED),
        bounds=dataclasses.asdict(business.CAMPAIGN_BOUNDS),
        python=sys.version.split()[0],
        simulated=[
            "planning transport (LLM)",
            "coder agent",
            "GitHub (vrai dépôt Git derrière les vrais clients)",
            "revue",
        ],
        simulated_steps=["D08-operator-merge"],
        llm_calls_emitted=0,
        billable_actions_emitted=0,
    )

    async def d01(step):
        await plan(world, step)
        snapshot_registry(report, "after-plan", world)

    async def d03(step):
        await deliver_task(world, 1, step)
        snapshot_registry(report, "after-task-1", world)

    async def d04(step):
        origin = world.bridge.break_origin(world.source)
        world.origin_url = origin
        calls_before = world.agent.calls
        merges_before = len(world.bridge.merge_calls())
        result = await run_pass(world)
        tip = world.tip()
        row = world.manager().get_task_merge(world.manager().get_tasks(world.project_id)[1].id)
        assert world.agent.calls == calls_before + 1 and result.stop_reason == merge_cycle.STOP_SYNC_PENDING, (
            result.stop_reason
        )
        assert len(world.bridge.merge_calls()) - merges_before == 1 and world.bridge.merged_pr_numbers() == [101, 102]
        assert row.state == "merged_unsynced" and row.merge_sha == tip, (
            "fusion distante confirmée, resynchronisation à reprendre"
        )
        assert task_statuses(world)[fixture.TITLES[2]] == "in_review", "livraison NON comptée prête"
        assert world.checkout_head() != tip, "le checkout opérateur est resté périmé"
        assert world.agent.calls == 2, "aucune tâche suivante lancée depuis un clone périmé"
        step.evidence.update(
            stop_reason=result.stop_reason, cycle_state=row.state, merge_sha=row.merge_sha, remote_tip=tip,
            checkout_head=world.checkout_head(), agent_calls=world.agent.calls,
        )  # fmt: skip
        snapshot_registry(report, "after-interrupted-sync", world)

    async def d05(step):
        world.bridge.restore_origin(world.source, world.origin_url)
        before = len(world.bridge.merge_calls())
        await deliver_task(world, 3, step, merges_before=before)
        puts_102 = [c for c in world.bridge.merge_calls() if c[1].endswith("/pulls/102/merge")]
        assert len(puts_102) == 1, "la fusion de la tâche 2 n'a JAMAIS été rejouée"
        assert [r.state for r in world.manager().list_task_merges(world.project_id)] == ["synced"] * 3
        step.evidence["merge_puts_for_pr_102"] = len(puts_102)
        step.evidence["task_2_started_from"] = "n/a (reprise de la synchronisation, pas de ré-exécution)"
        snapshot_registry(report, "after-task-3", world)

    async def d06(step):
        verify_main(world, step)
        report.facts["main_after_build"] = {"tip": world.tip(), "tree": world.bridge.remote.tree_of(world.tip())}

    state: Dict[str, Any] = {}

    async def d07(step):
        state["before"] = await measure_checkout(world)
        agent = ImprovementAgent(reformat_python)
        tip_before = world.tip()
        result = await direct_improvement(world, agent, measure_fn=measure)
        assert result.stop_reason == "auto_merge_blocked" and len(result.promoted) == 1, (
            result.stop_reason,
            result.rejected,
        )
        promoted = result.promoted[0]
        assert not promoted.auto_merged and world.tip() == tip_before, (
            "la politique de faible risque a refusé la fusion automatique"
        )
        assert any("interdit à l'auto-merge" in reason for _dim, reason in result.rejected), result.rejected
        proof = proofs_by_pr(world)[promoted.pr_number]
        assert proof.phase == "improve" and proof.passed and proof.contracts_required
        assert sorted(o.role for o in proof.oracles) == ["delivered"] * 3, "les trois contrats livrés ont été rejoués"
        for evidence in proof.oracles:
            assert evidence.candidate.status == "green"
        state["improve_pr"], state["improve_proof"] = promoted.pr_number, proof
        step.evidence.update(
            pr_number=promoted.pr_number, head_sha=promoted.head_sha, dimension=promoted.dimension, delta=round(promoted.delta, 6),
            proof_id=proof.proof_id, oracles=oracle_summary(proof), policy_refusal=result.rejected[0][1],
            stop_reason=result.stop_reason, metrics_before=metrics_row(state["before"]), main_tip_unchanged=tip_before,
        )  # fmt: skip
        snapshot_registry(report, "after-improvement", world)

    async def d08(step):
        number, proof = state["improve_pr"], state["improve_proof"]
        tip_before = world.tip()
        world.bridge.merge_out_of_band(number)
        assert resync_repository_base(world.source, "main"), "resynchronisation du checkout opérateur"
        tip = world.tip()
        assert tip != tip_before and world.checkout_head() == tip
        assert world.bridge.remote.tree_of(tip) == proof.tree_sha, "main == le contenu testé de l'amélioration"
        step.evidence.update(
            simulated=True,
            actor="opérateur (merge humain simulé)",
            pr_number=number,
            tip_before=tip_before,
            tip_after=tip,
        )

    async def d09(step):
        after = await measure_checkout(world)
        before = state["before"]
        assert after.tests_passed and after.coverage_measured
        assert after.coverage_pct >= before.coverage_pct, "la couverture ne baisse pas"
        assert after.lint_violations < before.lint_violations and after.composite > before.composite
        assert after.security_findings <= before.security_findings
        verify_main(world, step)
        step.evidence.update(metrics_before=metrics_row(before), metrics_after=metrics_row(after))

    async def d10(step):
        tip_before = world.tip()
        tree_before = world.bridge.remote.tree_of(tip_before)
        commits_before = int(git(world.source, "rev-list", "--count", "HEAD"))
        gain = ClaimedGain(measure)
        agent = ImprovementAgent(replace_header(fixture.BROKEN_NOTICE_HEADER))
        health_before = len(world.sandbox.health_runs)
        result = await direct_improvement(world, agent, measure_fn=gain)
        runs = world.sandbox.health_runs[health_before:]
        assert result.stop_reason == "auto_revert_recovered", (result.stop_reason, result.rejected)
        assert len(runs) >= 2 and runs[0]["status"] == "failed" and runs[-1]["status"] == "passed", runs
        assert set(runs[0]["failed_checks"]) <= {"write:legal_notice_present", "reread:legal_notice_present"}
        assert runs[0]["failed_checks"], "la sonde de santé a observé une régression comportementale RÉELLE"
        assert fixture.LEGAL_NOTICE not in str(runs[0]["pdf_text_excerpt"]) and fixture.LEGAL_NOTICE in str(
            runs[-1]["pdf_text_excerpt"]
        )
        tip = world.tip()
        assert tip != tip_before and world.bridge.remote.tree_of(tip) == tree_before, (
            "commit restauré : tree identique à l'avant-incident"
        )
        assert world.checkout_head() == tip
        assert int(git(world.source, "rev-list", "--count", "HEAD")) == commits_before + 2, (
            "fusion de l'incident puis revert"
        )
        incident = world.manager().get_phase5_incident(world.project_id)
        assert incident is not None and incident.state == "recovered", (
            "état durable : incident récupéré, acquittement requis"
        )
        state["incident_revision"] = incident.revision
        verify_main(world, step)  # comportement métier restauré (mention légale incluse)
        step.evidence.update(
            stop_reason=result.stop_reason, tip_before=tip_before, tip_after=tip, tree_before=tree_before,
            tree_after=world.bridge.remote.tree_of(tip), incident_state=incident.state, incident_merge_sha=incident.merge_sha,
            incident_pr=incident.source_pr_number, health_runs=runs, measurement_snapshots=gain.snapshots,
            note="le gain de mesure est annoncé par une mesure SIMULÉE ; la régression, la santé rouge et le rollback sont réels",
        )  # fmt: skip

    async def d11(step):
        manager = world.manager()
        incident = manager.get_phase5_incident(world.project_id)
        assert manager.acknowledge_phase5_incident(world.project_id, expected_revision=incident.revision)
        manager.record_decision(world.project_id, "Incident Phase 5 inspecté et acquitté par l'opérateur.")
        assert world.manager().get_phase5_incident(world.project_id) is None
        _promotion, recovery, _budget = phase5_hooks(world, improvement_settings(world))
        outcome = await recovery()
        assert outcome.found is False and outcome.continue_loop is True, "un nouveau run peut reprendre"
        step.evidence.update(
            acknowledged_revision=incident.revision,
            recovery_found=outcome.found,
            recovery_continue=outcome.continue_loop,
        )

    async def d12(step):
        counters = registry(world)
        business.assert_registry_within_bounds(counters)
        assert counters["reserved_micro_usd"] == counters["reserved_tokens"] == 0
        assert counters["unknown_micro_usd"] == counters["unknown_tokens"] == 0 and counters["blocked_reason"] is None
        report.facts["registry"]["final"] = counters
        step.evidence.update(final=counters, envelope=dataclasses.asdict(business.CAMPAIGN_BOUNDS))

    await report.arun("D01-plan", d01)
    await report.arun("D02-approve", lambda s: _sync(approve, world, s))
    await report.arun("D03-task-1", d03)
    await report.arun("D04-interrupted-sync", d04)
    await report.arun("D05-restart-resume", d05)
    await report.arun("D06-business", d06)
    await report.arun("D07-improve", d07)
    await report.arun("D08-operator-merge", d08)
    await report.arun("D09-no-regression", d09)
    await report.arun("D10-incident-rollback", d10)
    await report.arun("D11-acknowledge", d11)
    await report.arun("D12-registry", d12)
    report.facts.update(
        final_main={"tip": world.tip(), "checkout_head": world.checkout_head()},
        agent_calls=world.agent.calls,
        planning_calls=list(world.transport.calls),
        merge_requests=[c[1] for c in world.bridge.merge_calls()],
        oracle_sources_sha256={str(n): sha256(fixture.ORACLES[n]) for n in (1, 2, 3)},
    )
    return report


async def _sync(fn, world, step):
    fn(world, step)


async def run_negative_witnesses(root: Path, *, campaign_id: str = "w4-business-negative") -> CampaignReport:
    """Témoin négatif : un export PDF VALIDE mais aux mauvaises données n'est JAMAIS livré (oracle rouge par assertion)."""
    report = CampaignReport("deterministic-negative", campaign_id)
    report.declare("N01-plan-and-first-two-tasks", "Plan approuvé ; tâches 1 et 2 livrées normalement")
    report.declare(
        "N02-wrong-data-pdf-refused",
        "Tâche 3 : PDF valide mais données d'un autre audit — livraison refusée par l'oracle",
    )
    report.declare("N03-main-untouched", "main reste celui de la tâche 2 ; aucune PR de la tâche 3 ; registre cohérent")
    world = build_world(Path(root), agent=ReferenceAgent(wrong_data_stage_3=True))

    async def n01(step):
        await plan(world, step)
        approve(world, step)
        await deliver_task(world, 1, step)
        await deliver_task(world, 2, step)

    async def n02(step):
        result = await run_pass(world)
        error = next(
            t.last_error or ""
            for t in world.manager().get_tasks(world.project_id)
            if t.title.strip() == fixture.TITLES[3]
        )
        status = task_statuses(world)[fixture.TITLES[3]]
        assert world.agent.calls == 3 and status not in {"merged", "in_review"}, (status, result.stop_reason)
        assert "ORACLE D'ACCEPTATION REFUSÉ" in error and "AssertionError" in error, error[:400]
        assert "ModuleNotFoundError" not in error and "ImportError" not in error, (
            "un import absent n'est pas une preuve négative"
        )
        step.evidence.update(task_status=status, stop_reason=result.stop_reason, task_error_excerpt=error[:600])

    async def n03(step):
        assert sorted(world.bridge.prs) == [101, 102], "aucune PR n'a été ouverte pour la tâche 3"
        assert world.bridge.merged_pr_numbers() == [101, 102]
        counters = registry(world)
        business.assert_registry_within_bounds(counters)
        step.evidence.update(registry=counters, prs=sorted(world.bridge.prs))

    await report.arun("N01-plan-and-first-two-tasks", n01)
    await report.arun("N02-wrong-data-pdf-refused", n02)
    await report.arun("N03-main-untouched", n03)
    return report


async def run_budget_stop_campaign(root: Path, *, campaign_id: str = "w4-business-budget-stop") -> CampaignReport:
    """Arrêt par BUDGET : le plafond de tokens du registre ne laisse plus de solde utile avant la tâche 3.

    Le worker de la tâche 3 est REFUSÉ avant lancement (``allocate_worker``), aucune dépense n'est ajoutée, le point d'arrêt est
    rapporté ``budget_stop`` et deux redémarrages avec le même registre ne dépensent rien non plus."""
    report = CampaignReport("deterministic-budget-stop", campaign_id)
    report.declare("B01-plan", "Plan approuvé, oracles scellés")
    report.declare("B02-tasks-1-2", "Tâches 1 et 2 livrées et fusionnées (le registre approche le plafond)")
    report.declare("B03-task-3-refused", "Tâche 3 : worker refusé par le registre, arrêt budget durable")
    report.declare("B04-never-reached", "Vérification métier : jamais atteinte")
    usage = {1: (12_000, 4_800, 0.0125), 2: (2_500, 800, 0.0030), 3: (14_000, 4_200, 0.0190)}
    world = build_world(
        Path(root), settings=campaign_settings(MAX_TOKENS_BUDGET=24_000), agent=ReferenceAgent(usage=usage)
    )

    async def b01(step):
        await plan(world, step)
        approve(world, step)

    async def b02(step):
        await deliver_task(world, 1, step)
        await deliver_task(world, 2, step)

    async def b03(step):
        before = registry(world)
        calls = world.agent.calls
        result = await run_pass(world)
        again = await run_pass(world)  # redémarrage, même registre
        after = registry(world)
        assert world.agent.calls == calls, "aucun worker lancé au-delà du plafond"
        assert result.stop_reason == again.stop_reason == "paused_budget", (result.stop_reason, again.stop_reason)
        for key in (
            "consumed_micro_usd", "consumed_tokens", "reserved_micro_usd", "reserved_tokens",
            "unknown_micro_usd", "unknown_tokens",
        ):  # fmt: skip
            assert after[key] == before[key], f"{key} a changé alors qu'aucun worker n'a été lancé"
        step.evidence.update(registry_before=before, registry_after=after, stop_reason=result.stop_reason, restarts=2)
        raise business.BudgetStop(
            f"plafond de tokens du registre atteint ({after['consumed_tokens']}/{after['cap_tokens']})"
        )

    async def b04(step):  # pragma: no cover - doit rester non jouée
        raise AssertionError("étape atteinte alors que le budget est épuisé")

    await report.arun("B01-plan", b01)
    await report.arun("B02-tasks-1-2", b02)
    await report.arun("B03-task-3-refused", b03)
    await report.arun("B04-never-reached", b04)
    report.facts["stop_point"] = "B03-task-3-refused"
    report.facts["registry_final"] = registry(world)
    return report


def main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover - exercé par la commande reproductible
    import argparse
    import asyncio
    import tempfile

    parser = argparse.ArgumentParser(prog="python tests/w4_business_campaign.py")
    parser.add_argument("--output", required=True, help="rapport machine JSON")
    parser.add_argument("--human", help="rapport humain (texte)")
    parser.add_argument("--workdir", help="répertoire de travail (défaut : temporaire)")
    args = parser.parse_args(argv)
    root = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="w4-business-"))
    report = asyncio.run(run_deterministic_campaign(root / "main-campaign"))
    negative = asyncio.run(run_negative_witnesses(root / "negative"))
    merged = report.to_machine()
    merged["negative_witnesses"] = negative.to_machine()
    verdicts = {merged["verdict"], negative.verdict()}
    Path(args.output).write_text(
        json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    text = report.to_human() + negative.to_human()
    if args.human:
        Path(args.human).write_text(text, encoding="utf-8")
    print(text)
    return 0 if verdicts == {business.VERDICT_VALIDATED} else 1


if __name__ == "__main__":  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
