"""Assemblage de l'exécuteur d'une issue de bout en bout (E5, epic #362).

``execute_issue`` enchaîne les briques E1→E4 en un point d'entrée **isolé** :

    prepare_workspace (E2) → run_issue (E2) → run_quality_gate (E3) → open_pr (E4)

et **synchronise l'état** de la tâche : ``todo → in_progress`` au démarrage,
``→ in_review`` quand la PR est ouverte. **Jamais** ``done`` automatiquement : le
merge reste **humain**, et la **CI gate** le merge (la PR est ouverte mais ne peut
être mergée qu'avec la CI verte + approbation — sémantique GitHub, pas gérée ici).

**Fail-closed** : si l'agent ne produit aucun diff, ou si le gate qualité ne passe
pas, on **s'arrête** — aucune PR, l'état **ne dépasse pas** ``in_progress``.

``dry_run=True`` (défaut) : pipeline complet jusqu'à un **aperçu** de PR, **sans
aucune écriture** (ni GitHub, ni transition d'état) — utile pour visualiser ce qui
serait fait.

Module **isolé** : non importé par ``app.py``. Le pilote (Phase 3) appellera
``execute_issue`` sur le graphe de tâches (en respectant dépendances + budget) et
prendra en charge le déplacement de carte de board (il détient le mapping des
items du board) — délibérément hors périmètre ici.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from collegue.executor.agent import CodeAgent, IssueSpec
from collegue.executor.command import CommandRunner
from collegue.executor.pr import PrClients, PrResult, open_pr
from collegue.executor.quality_gate import QualityReport, Reviewer, run_quality_gate
from collegue.executor.runner import ExecutionResult, run_issue
from collegue.executor.workspace import Workspace, prepare_workspace

TASK_STATUS_TODO = "todo"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_IN_REVIEW = "in_review"

# Étapes possibles d'arrêt/aboutissement du pipeline.
STAGE_RUN = "run"  # exécution de l'agent (diff)
STAGE_GATE = "gate"  # gate qualité (tests + revue)
STAGE_PR = "pr"  # ouverture de PR

# Raisons d'échec portées par l'outcome (#421). Un ``success=False`` indifférencié
# rendait le no-op de l'agent (souvent transitoire, ex. fenêtre 503 du provider)
# indiscernable d'un vrai échec : ni retry intelligent, ni post-mortem possibles.
REASON_NO_OP = "no_op"  # l'agent a tourné sans erreur mais n'a produit AUCUN diff
REASON_AGENT_ERROR = "agent_error"  # le process agent a échoué (exit ≠ 0 / timeout)
REASON_GATE_FAILED = "gate_failed"  # tests rouges ou revue bloquante


def log_tail(text: str, limit: int = 2000) -> str:
    """Dernier segment (borné) d'un log — journalisable sans inonder l'audit (#421)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


def failure_feedback(outcome: "ExecutionOutcome") -> str:
    """Synthèse **courte et actionnable** d'un échec, pour la tentative suivante (#424).

    Priorité aux lignes ``FAILED``/``ERROR`` de pytest : c'est exactement ce dont
    l'agent a besoin pour corriger la cause. Un feedback verbeux (sortie brute)
    NOIE l'agent au lieu de l'aider — constaté en run réel (FacNor, task 4 :
    feedback bruité → time-out de 40 min ; lignes FAILED seules → convergence).
    À défaut de lignes de tests, queue bornée de la sortie de tests puis des logs
    agent (échec au stage ``run``).
    """
    if outcome.quality_report is not None and outcome.quality_report.test_output:
        output = outcome.quality_report.test_output
        fails = [line.strip() for line in output.splitlines() if line.strip().startswith(("FAILED", "ERROR"))]
        if fails:
            return " ; ".join(fails[:6])[:700]
        return log_tail(output, 400)
    return log_tail(outcome.execution.agent_result.logs, 400)


@dataclass
class ExecutionOutcome:
    """Résultat de bout en bout de l'exécution d'une issue."""

    success: bool  # le pipeline est allé jusqu'à la PR (gate passé)
    stage: str  # dernière étape atteinte : run | gate | pr
    workspace: Workspace
    execution: ExecutionResult
    quality_report: Optional[QualityReport] = None
    pr: Optional[PrResult] = None
    final_status: Optional[str] = None  # statut de tâche effectivement écrit (None si dry_run / pas de manager)
    reason: Optional[str] = None  # raison d'échec (no_op | agent_error | gate_failed), None si succès


def _set_status(manager, task_id, status: str, *, enabled: bool) -> Optional[str]:
    """Transition d'état si activée (réel + manager + task_id). Retourne le statut écrit."""
    if not enabled or manager is None or task_id is None:
        return None
    manager.update_task_status(task_id, status)
    return status


async def execute_issue(
    issue: IssueSpec,
    repo_source: str,
    ctx,
    *,
    agent: CodeAgent,
    owner: str,
    repo: str,
    base: str = "main",
    sandbox=None,
    reviewer: Optional[Reviewer] = None,
    runner: Optional[CommandRunner] = None,
    clients: Optional[PrClients] = None,
    manager: Optional[object] = None,
    task_id: Optional[int] = None,
    project_id: Optional[int] = None,
    dry_run: bool = True,
) -> ExecutionOutcome:
    """Exécute une issue de bout en bout (workspace → agent → tests+revue → PR).

    Renvoie un :class:`ExecutionOutcome`. En cas d'arrêt fail-closed (aucun diff,
    ou gate non passé), ``success=False`` et aucune PR n'est ouverte ; l'état ne
    dépasse pas ``in_progress``. ``dry_run`` (défaut) n'écrit rien et n'effectue
    aucune transition d'état.
    """
    persist = not dry_run  # les transitions d'état n'ont lieu qu'en exécution réelle
    final_status: Optional[str] = None

    workspace = prepare_workspace(repo_source, issue)
    final_status = _set_status(manager, task_id, TASK_STATUS_IN_PROGRESS, enabled=persist) or final_status

    # E2 : exécution de l'agent + capture du diff (l'état est piloté ici, pas par run_issue).
    execution = run_issue(agent, workspace, issue, runner=runner)
    if not execution.changed:
        # #421 : distinguer le no-op (agent OK, zéro diff — souvent transitoire)
        # de l'erreur du process agent (exit ≠ 0) — la couche retry en dépend.
        reason = REASON_NO_OP if execution.agent_result.success else REASON_AGENT_ERROR
        return ExecutionOutcome(
            success=False,
            stage=STAGE_RUN,
            workspace=workspace,
            execution=execution,
            final_status=final_status,
            reason=reason,
        )

    # E3 : gate qualité (fail-closed).
    report = await run_quality_gate(
        workspace.path, execution.diff, ctx, sandbox=sandbox, reviewer=reviewer, issue=issue
    )
    if not report.passed:
        return ExecutionOutcome(
            success=False,
            stage=STAGE_GATE,
            workspace=workspace,
            execution=execution,
            quality_report=report,
            final_status=final_status,
            reason=REASON_GATE_FAILED,
        )

    # E4 : ouverture de PR (dry_run respecté).
    pr = open_pr(
        workspace,
        report,
        issue,
        owner,
        repo,
        files_changed=execution.files_changed,
        base=base,
        clients=clients,
        dry_run=dry_run,
        manager=manager,
        project_id=project_id,
    )
    final_status = _set_status(manager, task_id, TASK_STATUS_IN_REVIEW, enabled=persist) or final_status

    return ExecutionOutcome(
        success=True,
        stage=STAGE_PR,
        workspace=workspace,
        execution=execution,
        quality_report=report,
        pr=pr,
        final_status=final_status,
    )
