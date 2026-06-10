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
from collegue.executor.pipeline import ExecutionOutcome, execute_issue
from collegue.executor.pr import PrClients, PrResult, build_pr_body, exec_marker, open_pr
from collegue.executor.quality_gate import (
    AdequacyChecker,
    AdequacyOutcome,
    ExpertReviewer,
    FakeAdequacyChecker,
    FakeReviewer,
    LLMAdequacyChecker,
    QualityReport,
    Reviewer,
    ReviewFindingLite,
    ReviewOutcome,
    frontend_gate_command,
    installability_command,
    issue_expects_code,
    outcome_from_review,
    run_quality_gate,
    tests_touched,
)
from collegue.executor.revert import (
    RevertError,
    RevertResult,
    prepare_revert,
    revert_commit,
    revert_pr_preview,
)
from collegue.executor.runner import ExecutionResult, run_issue
from collegue.executor.workspace import (
    Workspace,
    WorkspaceError,
    branch_for_issue,
    cleanup_workspace,
    prepare_workspace,
)

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
    "cleanup_workspace",
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
    "frontend_gate_command",
    "installability_command",
    "outcome_from_review",
    "AdequacyChecker",
    "AdequacyOutcome",
    "FakeAdequacyChecker",
    "LLMAdequacyChecker",
    "issue_expects_code",
    "tests_touched",
    # E4 — ouverture de PR
    "PrClients",
    "PrResult",
    "open_pr",
    "build_pr_body",
    "exec_marker",
    # E5 — assemblage du pipeline
    "execute_issue",
    "ExecutionOutcome",
    # H1 (Phase 5) — primitive de revert (capacité locale, sans push)
    "RevertResult",
    "RevertError",
    "revert_commit",
    "prepare_revert",
    "revert_pr_preview",
]
