"""
GitHub Commands Package.

Organizes GitHub API operations by domain:
- RepoCommands: Repository operations
- PRCommands: Pull request operations
- IssueCommands: Issue operations
- BranchCommands: Branch and commit operations
- FileCommands: File content operations
"""

from ..clients import GitHubClient
from .branches import BranchCommands, BranchInfo, CommitInfo
from .files import FileCommands
from .issues import IssueCommands, IssueInfo
from .labels import LabelCommands, LabelInfo
from .milestones import MilestoneCommands, MilestoneInfo
from .projects import ProjectCommands, ProjectInfo
from .prs import (
    Comment,
    CommitChecks,
    FileChange,
    MergeResult,
    PRCommands,
    PRFilesSnapshot,
    PRInfo,
    PRNotMergeableError,
)
from .repos import RepoCommands, RepoInfo
from .search import SearchCommands, SearchResult
from .workflows import WorkflowCommands, WorkflowRun

__all__ = [
    "GitHubClient",
    "RepoCommands",
    "RepoInfo",
    "PRCommands",
    "PRInfo",
    "PRNotMergeableError",
    "FileChange",
    "PRFilesSnapshot",
    "CommitChecks",
    "Comment",
    "MergeResult",
    "IssueCommands",
    "IssueInfo",
    "BranchCommands",
    "BranchInfo",
    "CommitInfo",
    "FileCommands",
    "WorkflowCommands",
    "WorkflowRun",
    "SearchCommands",
    "SearchResult",
    "LabelCommands",
    "LabelInfo",
    "MilestoneCommands",
    "MilestoneInfo",
    "ProjectCommands",
    "ProjectInfo",
]
