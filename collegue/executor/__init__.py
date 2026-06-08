"""Exécuteur d'une issue de bout en bout (Phase 2, epic #362).

Automatise **une** issue : workspace en sandbox → agent codeur (OpenHands) →
tests + revue experte → Pull Request gatée par la CI. E1 pose le contrat de
l'agent codeur (:class:`CodeAgent`) et son adaptateur OpenHands.

Module **isolé** : non importé par ``app.py``. Le pilote (Phase 3) câblera
l'exécuteur sur le graphe de tâches. ``OpenHandsAgent`` est importé directement
ici, mais ne tire **aucune** dépendance OpenHands (OpenHands tourne comme
processus dans le sandbox, jamais importé en Python) — ``collegue.executor``
reste donc importable partout sans installer OpenHands.
"""

from collegue.executor.agent import AgentResult, CodeAgent, FakeCodeAgent, IssueSpec
from collegue.executor.command import CommandRunner, LocalCommandRunner
from collegue.executor.openhands_agent import OpenHandsAgent
from collegue.executor.pr import PrClients, PrResult, build_pr_body, exec_marker, open_pr
from collegue.executor.quality_gate import (
    ExpertReviewer,
    FakeReviewer,
    QualityReport,
    Reviewer,
    ReviewFindingLite,
    ReviewOutcome,
    outcome_from_review,
    run_quality_gate,
)
from collegue.executor.runner import ExecutionResult, run_issue
from collegue.executor.workspace import Workspace, WorkspaceError, branch_for_issue, prepare_workspace

__all__ = [
    # E1 — contrat agent
    "IssueSpec",
    "AgentResult",
    "CodeAgent",
    "FakeCodeAgent",
    "OpenHandsAgent",
    # E2 — workspace + exécution
    "CommandRunner",
    "LocalCommandRunner",
    "Workspace",
    "WorkspaceError",
    "branch_for_issue",
    "prepare_workspace",
    "ExecutionResult",
    "run_issue",
    # E3 — gate qualité
    "Reviewer",
    "ReviewOutcome",
    "ReviewFindingLite",
    "FakeReviewer",
    "ExpertReviewer",
    "QualityReport",
    "run_quality_gate",
    "outcome_from_review",
    # E4 — ouverture de PR
    "PrClients",
    "PrResult",
    "open_pr",
    "build_pr_body",
    "exec_marker",
]
