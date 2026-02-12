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
from .repos import RepoCommands, RepoInfo
from .prs import PRCommands, PRInfo, FileChange, Comment
from .issues import IssueCommands, IssueInfo
from .branches import BranchCommands, BranchInfo, CommitInfo
from .files import FileCommands
from .workflows import WorkflowCommands, WorkflowRun
from .search import SearchCommands, SearchResult

__all__ = [
    'GitHubClient',
    'RepoCommands',
    'RepoInfo',
    'PRCommands',
    'PRInfo',
    'FileChange',
    'Comment',
    'IssueCommands',
    'IssueInfo',
    'BranchCommands',
    'BranchInfo',
    'CommitInfo',
    'FileCommands',
    'WorkflowCommands',
    'WorkflowRun',
    'SearchCommands',
    'SearchResult',
]
