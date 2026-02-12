"""
GitHub Operations Tool - Gestion des repositories, PRs, issues via l'API GitHub

Permet à Collègue d'interagir avec GitHub sans changer de fenêtre.
"""
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator
from ..core.auth import register_config_with_github, resolve_token
from .base import BaseTool, ToolExecutionError
from .github_commands.branches import BranchCommands, BranchInfo, CommitInfo
from .github_commands.files import FileCommands
from .github_commands.issues import IssueCommands, IssueInfo
from .github_commands.prs import Comment, FileChange, PRCommands, PRInfo
from .github_commands.repos import RepoCommands, RepoInfo
from .github_commands.search import SearchCommands, SearchResult
from .github_commands.workflows import WorkflowCommands, WorkflowRun
from ..core.shared import validate_github_command


class GitHubRequest(BaseModel):
    command: str = Field(
        ...,
        description="Commande à exécuter. IMPORTANT: list_prs/list_issues/get_repo nécessitent owner ET repo. Commandes: list_repos, get_repo, get_file, create_pr, list_prs, get_pr, create_issue, list_issues, get_issue, pr_files, pr_comments, create_branch, update_file, repo_branches, repo_commits, search_code, list_workflows"
    )
    owner: Optional[str] = Field(
        None,
        description="REQUIS pour la plupart des commandes. Propriétaire du repo (username ou organisation)."
    )
    repo: Optional[str] = Field(
        None,
        description="REQUIS avec owner pour get_repo, list_prs, list_issues, etc. Nom du repository."
    )

    pr_number: Optional[int] = Field(None, description="Numéro de la PR (requis pour get_pr, pr_files, pr_comments)")
    issue_number: Optional[int] = Field(None, description="Numéro de l'issue (requis pour get_issue)")
    branch: Optional[str] = Field(None, description="Nom de la branche (pour filtrer ou créer)")
    state: str = Field("open", description="Filtre par état: 'open', 'closed', ou 'all'")
    query: Optional[str] = Field(None, description="Requête de recherche (requis pour search_code)")
    limit: int = Field(30, description="Nombre max de résultats (1-100)", ge=1, le=100)
    token: Optional[str] = Field(None, description="Token GitHub (utilise automatiquement GITHUB_TOKEN de l'environnement si non fourni)")
    title: Optional[str] = Field(None, description="Titre pour create_pr ou create_issue")
    body: Optional[str] = Field(None, description="Description pour create_pr ou create_issue")
    head: Optional[str] = Field(None, description="Branche source pour create_pr (ex: 'feature-branch')")
    base: Optional[str] = Field(None, description="Branche cible pour create_pr (ex: 'main')")
    path: Optional[str] = Field(None, description="Chemin du fichier pour update_file")
    content: Optional[str] = Field(None, description="Nouveau contenu du fichier pour update_file")
    message: Optional[str] = Field(None, description="Message de commit pour update_file")
    from_branch: Optional[str] = Field(None, description="Branche source pour create_branch (défaut: default branch du repo)")

    @field_validator('command')
    @classmethod
    def validate_command(cls, v: str) -> str:
        return validate_github_command(v)

class GitHubResponse(BaseModel):
    success: bool
    command: str
    message: str
    repos: Optional[List[RepoInfo]] = None
    repo: Optional[RepoInfo] = None
    prs: Optional[List[PRInfo]] = None
    pr: Optional[PRInfo] = None
    issues: Optional[List[IssueInfo]] = None
    issue: Optional[IssueInfo] = None
    files: Optional[List[FileChange]] = None
    comments: Optional[List[Comment]] = None
    branches: Optional[List[BranchInfo]] = None
    commits: Optional[List[CommitInfo]] = None
    workflows: Optional[List[WorkflowRun]] = None
    search_results: Optional[List[SearchResult]] = None
    content: Optional[str] = None
    sha: Optional[str] = None


class GitHubOpsTool(BaseTool):
    tool_name = "github_ops"
    tags = {"integration", "devops"}
    tool_description = (
        "Interagit avec l'API GitHub. IMPORTANT: Pour list_prs, list_issues, get_repo, "
        "vous DEVEZ fournir 'owner' ET 'repo' (ex: owner='microsoft', repo='vscode'). "
        "Commandes: list_repos, get_repo, list_prs, get_pr, list_issues, repo_branches, search_code"
    )
    request_model = GitHubRequest
    response_model = GitHubResponse
    supported_languages = []

    def __init__(self, config=None, app_state=None):
        super().__init__(config, app_state)
        self._repos = None
        self._prs = None
        self._issues = None
        self._branches = None
        self._files = None
        self._workflows = None
        self._search = None

    def _init_commands(self, token: Optional[str] = None):
        if self._repos is None:
            self._repos = RepoCommands(token=token, logger=self.logger)
            self._prs = PRCommands(token=token, logger=self.logger)
            self._issues = IssueCommands(token=token, logger=self.logger)
            self._branches = BranchCommands(token=token, logger=self.logger)
            self._files = FileCommands(token=token, logger=self.logger)
            self._workflows = WorkflowCommands(token=token, logger=self.logger)
            self._search = SearchCommands(token=token, logger=self.logger)

    def _execute_core_logic(self, request: GitHubRequest, **kwargs) -> GitHubResponse:
        token = resolve_token(
            request.token,
            'GITHUB_TOKEN',
            'x-github-token',
            'x-collegue-github-token'
        )
        self._init_commands(token)

        if token and request.owner:
            register_config_with_github(
                owner=request.owner,
                repo=request.repo,
                github_token=token,
            )

        if request.command == 'list_repos':
            repos = self._repos.list_repos(request.owner, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(repos)} repository(ies) trouvé(s)",
                repos=repos
            )

        elif request.command == 'get_repo':
            if not request.owner or not request.repo:
                raise ToolExecutionError("owner et repo requis pour get_repo")
            repo = self._repos.get_repo(request.owner, request.repo)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ Repository {repo.full_name}",
                repo=repo
            )

        elif request.command == 'list_prs':
            if not request.owner or not request.repo:
                raise ToolExecutionError("owner et repo requis pour list_prs")
            prs = self._prs.list_prs(request.owner, request.repo, request.state, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(prs)} PR(s) {request.state}",
                prs=prs
            )

        elif request.command == 'get_pr':
            if not request.owner or not request.repo or not request.pr_number:
                raise ToolExecutionError("owner, repo et pr_number requis pour get_pr")
            pr = self._prs.get_pr(request.owner, request.repo, request.pr_number)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ PR #{pr.number}: {pr.title}",
                pr=pr
            )

        elif request.command == 'pr_files':
            if not request.owner or not request.repo or not request.pr_number:
                raise ToolExecutionError("owner, repo et pr_number requis pour pr_files")
            files = self._prs.get_pr_files(request.owner, request.repo, request.pr_number, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(files)} fichier(s) modifié(s) dans PR #{request.pr_number}",
                files=files
            )

        elif request.command == 'pr_comments':
            if not request.owner or not request.repo or not request.pr_number:
                raise ToolExecutionError("owner, repo et pr_number requis pour pr_comments")
            comments = self._prs.get_pr_comments(request.owner, request.repo, request.pr_number, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(comments)} commentaire(s) sur PR #{request.pr_number}",
                comments=comments
            )

        elif request.command == 'list_issues':
            if not request.owner or not request.repo:
                raise ToolExecutionError("owner et repo requis pour list_issues")
            issues = self._issues.list_issues(request.owner, request.repo, request.state, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(issues)} issue(s) {request.state}",
                issues=issues
            )

        elif request.command == 'get_issue':
            if not request.owner or not request.repo or not request.issue_number:
                raise ToolExecutionError("owner, repo et issue_number requis pour get_issue")
            issue = self._issues.get_issue(request.owner, request.repo, request.issue_number)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ Issue #{issue.number}: {issue.title}",
                issue=issue
            )

        elif request.command == 'repo_branches':
            if not request.owner or not request.repo:
                raise ToolExecutionError("owner et repo requis pour repo_branches")
            branches = self._branches.list_branches(request.owner, request.repo, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(branches)} branche(s)",
                branches=branches
            )

        elif request.command == 'repo_commits':
            if not request.owner or not request.repo:
                raise ToolExecutionError("owner et repo requis pour repo_commits")
            commits = self._branches.list_commits(request.owner, request.repo, request.branch, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(commits)} commit(s) récent(s)",
                commits=commits
            )

        elif request.command == 'list_workflows':
            if not request.owner or not request.repo:
                raise ToolExecutionError("owner et repo requis pour list_workflows")
            workflows = self._workflows.list_workflows(request.owner, request.repo, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(workflows)} workflow run(s)",
                workflows=workflows
            )

        elif request.command == 'workflow_runs':
            if not request.owner or not request.repo:
                raise ToolExecutionError("owner et repo requis pour workflow_runs")
            workflows = self._workflows.list_workflows(request.owner, request.repo, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(workflows)} workflow run(s)",
                workflows=workflows
            )

        elif request.command == 'search_code':
            if not request.query:
                raise ToolExecutionError("query requis pour search_code")
            results = self._search.search_code(request.query, request.owner, request.repo, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(results)} résultat(s) pour '{request.query}'",
                search_results=results
            )

        elif request.command == 'create_pr':
            if not request.owner or not request.repo or not request.title or not request.head or not request.base:
                raise ToolExecutionError("owner, repo, title, head, base requis pour create_pr")
            pr = self._prs.create_pr(request.owner, request.repo, request.title, request.head, request.base, request.body)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ PR créée: {pr.html_url}",
                pr=pr
            )

        elif request.command == 'create_issue':
            if not request.owner or not request.repo or not request.title:
                raise ToolExecutionError("owner, repo, title requis pour create_issue")
            issue = self._issues.create_issue(request.owner, request.repo, request.title, request.body)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ Issue créée: {issue.html_url}",
                issue=issue
            )

        elif request.command == 'create_branch':
            if not request.owner or not request.repo or not request.branch:
                raise ToolExecutionError("owner, repo, branch requis pour create_branch")
            b_info = self._branches.create_branch(request.owner, request.repo, request.branch, request.from_branch)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ Branche '{request.branch}' créée",
                branches=[b_info]
            )

        elif request.command == 'update_file':
            if not request.owner or not request.repo or not request.path or not request.message or request.content is None:
                raise ToolExecutionError("owner, repo, path, message, content requis pour update_file")
            result = self._files.update_file(request.owner, request.repo, request.path, request.message, request.content, request.branch)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ Fichier '{request.path}' mis à jour",
                files=[FileChange(filename=request.path, status="updated", additions=0, deletions=0)]
            )

        elif request.command == 'get_file':
            if not request.owner or not request.repo or not request.path:
                raise ToolExecutionError("owner, repo et path requis pour get_file")
            file_data = self._files.get_file_content(request.owner, request.repo, request.path, request.branch)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ Fichier '{request.path}' récupéré",
                content=file_data.get('content'),
                sha=file_data.get('sha')
            )

        else:
            raise ToolExecutionError(f"Commande inconnue: {request.command}")
