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
from .prs import Comment, FileChange, PRCommands, PRInfo
from .repos import RepoCommands, RepoInfo
from .search import SearchCommands, SearchResult
from .workflows import WorkflowCommands, WorkflowRun

__all__ = [
    "GitHubClient",
    "RepoCommands",
    "RepoInfo",
    "PRCommands",
    "PRInfo",
    "FileChange",
    "Comment",
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
]
