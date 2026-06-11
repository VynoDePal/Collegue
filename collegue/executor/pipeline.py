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

import logging
from dataclasses import dataclass
from typing import Mapping, Optional

from collegue.executor.agent import AgentResult, CodeAgent, IssueSpec
from collegue.executor.command import CommandRunner
from collegue.executor.pr import PrClients, PrResult, open_pr
from collegue.executor.quality_gate import QualityReport, Reviewer, run_quality_gate
from collegue.executor.runner import ExecutionResult, run_issue
from collegue.executor.workspace import Workspace, apply_seed_diff, prepare_workspace
from collegue.sandbox.executor import TIMEOUT_NOTE

logger = logging.getLogger(__name__)

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
REASON_ENGINE_ERROR = "engine_error"  # exception d'infrastructure interceptée (#435)


def log_tail(text: str, limit: int = 2000) -> str:
    """Dernier segment (borné) d'un log — journalisable sans inonder l'audit (#421)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


# Signatures d'aléa d'INFRASTRUCTURE (réseau, dépôt de paquets, 5xx fournisseur)
# dans un feedback d'échec (#459). Heuristique volontairement étroite : ces
# motifs n'apparaissent pas dans un diagnostic fonctionnel.
_INFRA_NOISE_SIGNATURES = (
    "ReadTimeoutError",
    "ReadTimeout",
    "ConnectTimeoutError",
    "ConnectTimeout",
    "ConnectionError",
    "ConnectionResetError",
    "NewConnectionError",
    "Temporary failure in name resolution",
    "Connection refused",
    "502 Server Error",
    "503 Server Error",
    "504 Server Error",
    # Kill du conteneur sandbox au timeout (#461) : quand pip pend sur PyPI, le
    # conteneur peut être tué avant d'imprimer un traceback réseau — la note du
    # sandbox est alors le seul indice, et ce n'est pas un diagnostic actionnable.
    TIMEOUT_NOTE,
)


def is_infra_noise(feedback: str) -> bool:
    """Vrai si ``feedback`` ressemble à un aléa d'infrastructure (#459).

    Un timeout PyPI pendant le gate produit un traceback réseau SANS ligne
    FAILED : ré-injecté tel quel, il ÉCRASE le diagnostic actionnable de la
    tentative précédente (cas réel FacNor v3 : « email-validator manquant »
    éclipsé par du bruit urllib3 — requeue opérateur nécessaire). Une ligne
    FAILED/ERROR présente = diagnostic fonctionnel, jamais classé bruit.
    """
    text = feedback or ""
    if not text:
        return False
    # « FAILED  » / « ERROR  » avec espace : les formes pytest. (pip écrit
    # « ERROR: » avec deux-points — c'est justement du bruit d'install à classer.)
    if any(line.strip().startswith(("FAILED ", "ERROR ")) for line in text.splitlines()):
        return False
    return any(signature in text for signature in _INFRA_NOISE_SIGNATURES)


def is_infra_gate_failure(outcome: "ExecutionOutcome") -> bool:
    """Vrai si un échec de gate est imputable à un aléa d'infrastructure (#477).

    Deux chemins :

    - le diagnostic court (:func:`failure_feedback`) porte une signature réseau
      (cas nominal #459/#461 : traceback pip sans ligne pytest) ;
    - l'installation des dépendances a échoué (``deps_install_failed``, #439)
      **et** la sortie complète du gate contient une signature réseau. La
      cascade « pip timeout → ModuleNotFoundError à la collecte » produit des
      lignes ``ERROR `` d'apparence fonctionnelle qui désarmaient la grâce #461
      alors que la cause première était un aléa PyPI/DNS (cas réel FacNor v4 :
      échec terminal de la tâche 6 sur un pur ReadTimeoutError pip).

    Un échec d'install SANS signature réseau (requirements invalide, paquet
    inexistant) reste fonctionnel : c'est précisément ce que la passe #439
    doit sanctionner. Et un gate rouge à tests VERTS (revue bloquante,
    adéquation #437) n'est jamais gracié : le verdict est fonctionnel même si
    l'install a connu un aléa réseau en chemin (les deux cas cibles — timeout
    pip, cascade de collecte — ont toujours des tests rouges).
    """
    if outcome.reason != REASON_GATE_FAILED:
        return False
    report = outcome.quality_report
    if report is not None and report.tests_passed:
        # Gate rouge à tests VERTS (revue bloquante, adéquation #437,
        # require_test_changes) : verdict fonctionnel — même si la queue de
        # sortie charrie un aléa réseau d'install, il n'est pas la cause.
        return False
    if is_infra_noise(failure_feedback(outcome)):
        return True
    if report is not None and getattr(report, "deps_install_failed", False):
        output = report.test_output or ""
        return any(signature in output for signature in _INFRA_NOISE_SIGNATURES)
    return False


# #478 : marqueur de troncature du short summary pytest (ASCII « ... » — distinct
# du « … » de log_tail, qui est un autre chemin).
_PYTEST_TRUNCATION = "..."


def _detruncate_summary_line(line: str, output: str) -> str:
    """Restitue le diagnostic complet d'une ligne de short summary tronquée (#478).

    En non-tty, pytest borne « FAILED nodeid - message » à COLUMNS (80 par
    défaut) et tronque avec « ... » — le nom du paquet manquant disparaissait du
    feedback (cas réel FacNor v4 : « requires the httpx pack... », 3 cycles
    brûlés à deviner + requeues opérateur). Le message ENTIER vit dans les
    lignes ``E   …`` du traceback de la même sortie : on l'y reprend (préfixe
    tronqué → première ligne E qui le contient). Best-effort : sans
    correspondance, la ligne tronquée est relayée telle quelle.
    """
    if not line.endswith(_PYTEST_TRUNCATION):
        return line
    head, sep, message = line.partition(" - ")
    prefix = message[: -len(_PYTEST_TRUNCATION)].strip()
    if not sep or not prefix:
        return line
    for raw in output.splitlines():
        candidate = raw.strip()
        if candidate.startswith("E ") and prefix in candidate:
            # Borné : une ligne E géante ne doit pas manger le budget [:700]
            # du feedback et masquer les autres lignes FAILED.
            return f"{head} - {candidate[1:].strip()[:300]}"
    return line


def failure_feedback(outcome: "ExecutionOutcome") -> str:
    """Synthèse **courte et actionnable** d'un échec, pour la tentative suivante (#424).

    Priorité aux lignes ``FAILED``/``ERROR`` de pytest : c'est exactement ce dont
    l'agent a besoin pour corriger la cause. Un feedback verbeux (sortie brute)
    NOIE l'agent au lieu de l'aider — constaté en run réel (FacNor, task 4 :
    feedback bruité → time-out de 40 min ; lignes FAILED seules → convergence).
    À défaut de lignes de tests, queue bornée de la sortie de tests puis des logs
    agent (échec au stage ``run``).

    Exception d'infrastructure (#435, ``outcome.error``) : c'est ELLE le motif —
    les logs agent (potentiellement ceux d'une exécution réussie, si la panne est
    survenue à l'ouverture de PR) seraient un feedback trompeur.

    Adéquation refusée (#437) : les tests sont VERTS — le motif utile est la
    justification du contrôle (« la feature n'est pas implémentée »), pas la
    sortie des tests.
    """
    if outcome.error:
        return log_tail(outcome.error, 400)
    report = outcome.quality_report
    if report is not None and getattr(report, "adequacy_implemented", None) is False:
        justification = getattr(report, "adequacy_justification", "") or "le diff n'implémente pas l'issue"
        return ("ADÉQUATION REFUSÉE — le diff ne réalise pas l'issue : " + justification)[:700]
    if outcome.quality_report is not None and outcome.quality_report.test_output:
        output = outcome.quality_report.test_output
        # « FAILED  » / « ERROR  » avec espace : les formes du short summary
        # pytest, alignées sur is_infra_noise (#477). Sans l'espace, la ligne
        # pip « ERROR: Exception: » (deux-points) était relayée comme diagnostic
        # « fonctionnel » — inactionnable — et le traceback réseau (ReadTimeout…)
        # était jeté : la grâce #461 ne voyait jamais la signature infra.
        fails = [line.strip() for line in output.splitlines() if line.strip().startswith(("FAILED ", "ERROR "))]
        if fails:
            # #478 : filet — le diagnostic complet est repris du traceback quand
            # le short summary a été tronqué à la largeur du terminal.
            return " ; ".join(_detruncate_summary_line(line, output) for line in fails[:6])[:700]
        return log_tail(output, 400)
    return log_tail(outcome.execution.agent_result.logs, 400)


@dataclass
class ExecutionOutcome:
    """Résultat de bout en bout de l'exécution d'une issue."""

    success: bool  # le pipeline est allé jusqu'à la PR (gate passé)
    stage: str  # dernière étape atteinte : run | gate | pr
    workspace: Optional[Workspace]  # None si l'exception a frappé avant la préparation (#435)
    execution: ExecutionResult
    quality_report: Optional[QualityReport] = None
    pr: Optional[PrResult] = None
    final_status: Optional[str] = None  # statut de tâche effectivement écrit (None si dry_run / pas de manager)
    reason: Optional[str] = None  # raison d'échec (no_op | agent_error | gate_failed | engine_error), None si succès
    error: Optional[str] = None  # exception d'infrastructure interceptée (#435), None sinon


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
    seed_diff: Optional[str] = None,
    gate_options: Optional[Mapping[str, object]] = None,
) -> ExecutionOutcome:
    """Exécute une issue de bout en bout (workspace → agent → tests+revue → PR).

    Renvoie un :class:`ExecutionOutcome`. En cas d'arrêt fail-closed (aucun diff,
    ou gate non passé), ``success=False`` et aucune PR n'est ouverte ; l'état ne
    dépasse pas ``in_progress``. ``dry_run`` (défaut) n'écrit rien et n'effectue
    aucune transition d'état.

    **Barrière d'exception (#435)** : une exception d'infrastructure pendant le
    traitement (``WorkspaceError`` au clone, erreur réseau GitHub à l'ouverture de
    PR, bug ponctuel d'un adaptateur) ne remonte PLUS crue — elle est convertie en
    outcome ``failed`` (``reason="engine_error"``, ``stage`` = étape atteinte,
    ``error`` = exception) qui entre dans le chemin retry existant du pilote
    (#420/#424). Une panne ponctuelle d'UNE tâche ne tue plus le run entier alors
    que le reste du DAG est exécutable. Les ``BaseException`` (annulation asyncio,
    arrêt process) propagent, elles, normalement.

    ``seed_diff`` (#436) : diff d'une tentative précédente à RÉ-APPLIQUER sur le
    clone neuf avant l'agent (mémoire de retry — réparation incrémentale au lieu
    de régénération complète). Best-effort : un seed inapplicable est ignoré
    (clone vierge, comportement historique).

    ``gate_options`` (#438) : kwargs additionnels transmis tels quels à
    :func:`run_quality_gate` (``test_command``, ``frontend_gate``…) — c'est le
    canal de configuration du gate par projet/runtime, sans coupler l'exécuteur
    à la config.
    """
    persist = not dry_run  # les transitions d'état n'ont lieu qu'en exécution réelle
    final_status: Optional[str] = None
    stage = STAGE_RUN
    workspace: Optional[Workspace] = None
    execution: Optional[ExecutionResult] = None
    report: Optional[QualityReport] = None

    try:
        workspace = prepare_workspace(repo_source, issue)
        if seed_diff and apply_seed_diff(workspace, seed_diff):
            logger.info("issue #%s : workspace réensemencé avec la meilleure tentative (#436)", issue.number)
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
        stage = STAGE_GATE
        report = await run_quality_gate(
            workspace.path,
            execution.diff,
            ctx,
            sandbox=sandbox,
            reviewer=reviewer,
            issue=issue,
            **dict(gate_options or {}),
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
        stage = STAGE_PR
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
    except Exception as exc:  # barrière volontairement large (#435) — fail-closed, retentable
        error = f"{type(exc).__name__}: {exc}"
        logger.exception(
            "exception d'infrastructure pendant l'issue #%s (stage=%s) — convertie en échec retentable (#435)",
            issue.number,
            stage,
        )
        if execution is None:
            # L'agent n'a jamais tourné (panne au clone / à la transition d'état) :
            # résultat synthétique pour que l'outcome reste exploitable partout.
            execution = ExecutionResult(
                agent_result=AgentResult(success=False, logs=f"[engine] exception avant l'agent — {error}"),
                changed=False,
                diff="",
                files_changed=(),
                success=False,
            )
        return ExecutionOutcome(
            success=False,
            stage=stage,
            workspace=workspace,
            execution=execution,
            quality_report=report,
            final_status=final_status,
            reason=REASON_ENGINE_ERROR,
            error=error,
        )

    return ExecutionOutcome(
        success=True,
        stage=STAGE_PR,
        workspace=workspace,
        execution=execution,
        quality_report=report,
        pr=pr,
        final_status=final_status,
    )
