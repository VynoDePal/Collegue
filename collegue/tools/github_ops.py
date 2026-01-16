"""
GitHub Operations Tool - Gestion des repositories, PRs, issues via l'API GitHub

Permet à Collègue d'interagir avec GitHub sans changer de fenêtre.
"""
import logging
import os
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, Field, field_validator
from .base import BaseTool, ToolExecutionError

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


class GitHubRequest(BaseModel):
    """Modèle de requête pour les opérations GitHub."""
    command: str = Field(
        ...,
        description="Commande: list_repos, get_repo, list_prs, get_pr, list_issues, get_issue, pr_files, pr_comments, repo_branches, repo_commits, search_code"
    )
    owner: Optional[str] = Field(None, description="Propriétaire du repo (user ou org)")
    repo: Optional[str] = Field(None, description="Nom du repository")
    pr_number: Optional[int] = Field(None, description="Numéro de la PR")
    issue_number: Optional[int] = Field(None, description="Numéro de l'issue")
    branch: Optional[str] = Field(None, description="Nom de la branche")
    state: str = Field("open", description="État: open, closed, all")
    query: Optional[str] = Field(None, description="Requête de recherche")
    limit: int = Field(30, description="Limite de résultats", ge=1, le=100)
    token: Optional[str] = Field(None, description="GitHub token (utilise GITHUB_TOKEN si non fourni)")
    
    @field_validator('command')
    @classmethod
    def validate_command(cls, v: str) -> str:
        valid = ['list_repos', 'get_repo', 'list_prs', 'get_pr', 'list_issues', 
                 'get_issue', 'pr_files', 'pr_comments', 'repo_branches', 
                 'repo_commits', 'search_code', 'list_workflows', 'workflow_runs']
        if v not in valid:
            raise ValueError(f"Commande invalide. Valides: {valid}")
        return v


class RepoInfo(BaseModel):
    """Information sur un repository."""
    name: str
    full_name: str
    description: Optional[str] = None
    html_url: str
    default_branch: str
    language: Optional[str] = None
    stars: int = 0
    forks: int = 0
    open_issues: int = 0
    is_private: bool = False
    updated_at: str


class PRInfo(BaseModel):
    """Information sur une Pull Request."""
    number: int
    title: str
    state: str
    html_url: str
    user: str
    base_branch: str
    head_branch: str
    created_at: str
    updated_at: str
    mergeable: Optional[bool] = None
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    labels: List[str] = []
    draft: bool = False


class IssueInfo(BaseModel):
    """Information sur une issue."""
    number: int
    title: str
    state: str
    html_url: str
    user: str
    created_at: str
    updated_at: str
    labels: List[str] = []
    assignees: List[str] = []
    comments: int = 0
    body: Optional[str] = None


class FileChange(BaseModel):
    """Fichier modifié dans une PR."""
    filename: str
    status: str  # added, removed, modified, renamed
    additions: int
    deletions: int
    patch: Optional[str] = None


class Comment(BaseModel):
    """Commentaire sur une PR ou issue."""
    id: int
    user: str
    body: str
    created_at: str
    html_url: str


class BranchInfo(BaseModel):
    """Information sur une branche."""
    name: str
    sha: str
    protected: bool = False


class CommitInfo(BaseModel):
    """Information sur un commit."""
    sha: str
    message: str
    author: str
    date: str
    html_url: str


class WorkflowRun(BaseModel):
    """Information sur une exécution de workflow."""
    id: int
    name: str
    status: str
    conclusion: Optional[str] = None
    html_url: str
    created_at: str
    head_branch: str


class SearchResult(BaseModel):
    """Résultat de recherche de code."""
    name: str
    path: str
    repository: str
    html_url: str
    score: float


class GitHubResponse(BaseModel):
    """Modèle de réponse pour les opérations GitHub."""
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


class GitHubOpsTool(BaseTool):
    """
    Outil d'interaction avec l'API GitHub.
    
    Fonctionnalités:
    - Lister et inspecter les repositories
    - Gérer les Pull Requests (liste, détails, fichiers, commentaires)
    - Gérer les Issues
    - Voir les branches et commits
    - Vérifier les workflows CI/CD
    - Rechercher du code
    """
    
    API_BASE = "https://api.github.com"
    
    def get_name(self) -> str:
        return "github_ops"
    
    def get_description(self) -> str:
        return "Interagit avec GitHub: repos, PRs, issues, branches, CI/CD, recherche de code"
    
    def get_request_model(self) -> Type[BaseModel]:
        return GitHubRequest
    
    def get_response_model(self) -> Type[BaseModel]:
        return GitHubResponse
    
    def get_supported_languages(self) -> List[str]:
        return []
    
    def _get_headers(self, token: Optional[str] = None) -> Dict[str, str]:
        """Construit les headers pour l'API GitHub."""
        gh_token = token or os.environ.get('GITHUB_TOKEN')
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        if gh_token:
            headers["Authorization"] = f"Bearer {gh_token}"
        return headers
    
    def _api_get(self, endpoint: str, token: Optional[str] = None, params: Optional[Dict] = None) -> Any:
        """Effectue une requête GET à l'API GitHub."""
        if not HAS_REQUESTS:
            raise ToolExecutionError("requests non installé. Installez avec: pip install requests")
        
        url = f"{self.API_BASE}{endpoint}"
        headers = self._get_headers(token)
        
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            
            if response.status_code == 404:
                raise ToolExecutionError(f"Ressource introuvable: {endpoint}")
            elif response.status_code == 401:
                raise ToolExecutionError("Token GitHub invalide ou expiré")
            elif response.status_code == 403:
                remaining = response.headers.get('X-RateLimit-Remaining', '?')
                raise ToolExecutionError(f"Rate limit GitHub atteint. Restant: {remaining}")
            elif response.status_code >= 400:
                raise ToolExecutionError(f"Erreur API GitHub {response.status_code}: {response.text[:200]}")
            
            return response.json()
        except requests.RequestException as e:
            raise ToolExecutionError(f"Erreur réseau GitHub: {e}")
    
    def _list_repos(self, owner: Optional[str], token: Optional[str], limit: int) -> List[RepoInfo]:
        """Liste les repositories."""
        if owner:
            # Repos d'un user/org spécifique
            endpoint = f"/users/{owner}/repos"
        else:
            # Repos de l'utilisateur authentifié
            endpoint = "/user/repos"
        
        data = self._api_get(endpoint, token, {"per_page": limit, "sort": "updated"})
        
        return [RepoInfo(
            name=r['name'],
            full_name=r['full_name'],
            description=r.get('description'),
            html_url=r['html_url'],
            default_branch=r.get('default_branch', 'main'),
            language=r.get('language'),
            stars=r.get('stargazers_count', 0),
            forks=r.get('forks_count', 0),
            open_issues=r.get('open_issues_count', 0),
            is_private=r.get('private', False),
            updated_at=r['updated_at']
        ) for r in data[:limit]]
    
    def _get_repo(self, owner: str, repo: str, token: Optional[str]) -> RepoInfo:
        """Récupère les détails d'un repository."""
        data = self._api_get(f"/repos/{owner}/{repo}", token)
        return RepoInfo(
            name=data['name'],
            full_name=data['full_name'],
            description=data.get('description'),
            html_url=data['html_url'],
            default_branch=data.get('default_branch', 'main'),
            language=data.get('language'),
            stars=data.get('stargazers_count', 0),
            forks=data.get('forks_count', 0),
            open_issues=data.get('open_issues_count', 0),
            is_private=data.get('private', False),
            updated_at=data['updated_at']
        )
    
    def _list_prs(self, owner: str, repo: str, state: str, token: Optional[str], limit: int) -> List[PRInfo]:
        """Liste les Pull Requests."""
        data = self._api_get(f"/repos/{owner}/{repo}/pulls", token, {
            "state": state,
            "per_page": limit,
            "sort": "updated",
            "direction": "desc"
        })
        
        return [PRInfo(
            number=pr['number'],
            title=pr['title'],
            state=pr['state'],
            html_url=pr['html_url'],
            user=pr['user']['login'],
            base_branch=pr['base']['ref'],
            head_branch=pr['head']['ref'],
            created_at=pr['created_at'],
            updated_at=pr['updated_at'],
            labels=[l['name'] for l in pr.get('labels', [])],
            draft=pr.get('draft', False)
        ) for pr in data[:limit]]
    
    def _get_pr(self, owner: str, repo: str, pr_number: int, token: Optional[str]) -> PRInfo:
        """Récupère les détails d'une PR."""
        data = self._api_get(f"/repos/{owner}/{repo}/pulls/{pr_number}", token)
        return PRInfo(
            number=data['number'],
            title=data['title'],
            state=data['state'],
            html_url=data['html_url'],
            user=data['user']['login'],
            base_branch=data['base']['ref'],
            head_branch=data['head']['ref'],
            created_at=data['created_at'],
            updated_at=data['updated_at'],
            mergeable=data.get('mergeable'),
            additions=data.get('additions', 0),
            deletions=data.get('deletions', 0),
            changed_files=data.get('changed_files', 0),
            labels=[l['name'] for l in data.get('labels', [])],
            draft=data.get('draft', False)
        )
    
    def _get_pr_files(self, owner: str, repo: str, pr_number: int, token: Optional[str], limit: int) -> List[FileChange]:
        """Récupère les fichiers modifiés dans une PR."""
        data = self._api_get(f"/repos/{owner}/{repo}/pulls/{pr_number}/files", token, {"per_page": limit})
        
        return [FileChange(
            filename=f['filename'],
            status=f['status'],
            additions=f.get('additions', 0),
            deletions=f.get('deletions', 0),
            patch=f.get('patch', '')[:2000] if f.get('patch') else None
        ) for f in data[:limit]]
    
    def _get_pr_comments(self, owner: str, repo: str, pr_number: int, token: Optional[str], limit: int) -> List[Comment]:
        """Récupère les commentaires d'une PR."""
        # Commentaires de review + commentaires généraux
        review_comments = self._api_get(f"/repos/{owner}/{repo}/pulls/{pr_number}/comments", token, {"per_page": limit})
        issue_comments = self._api_get(f"/repos/{owner}/{repo}/issues/{pr_number}/comments", token, {"per_page": limit})
        
        all_comments = []
        for c in review_comments + issue_comments:
            all_comments.append(Comment(
                id=c['id'],
                user=c['user']['login'],
                body=c['body'][:1000] if c.get('body') else "",
                created_at=c['created_at'],
                html_url=c['html_url']
            ))
        
        # Trier par date
        all_comments.sort(key=lambda x: x.created_at)
        return all_comments[:limit]
    
    def _list_issues(self, owner: str, repo: str, state: str, token: Optional[str], limit: int) -> List[IssueInfo]:
        """Liste les issues (sans les PRs)."""
        data = self._api_get(f"/repos/{owner}/{repo}/issues", token, {
            "state": state,
            "per_page": limit,
            "sort": "updated",
            "direction": "desc"
        })
        
        # Filtrer les PRs (elles apparaissent aussi dans /issues)
        issues = [i for i in data if 'pull_request' not in i]
        
        return [IssueInfo(
            number=i['number'],
            title=i['title'],
            state=i['state'],
            html_url=i['html_url'],
            user=i['user']['login'],
            created_at=i['created_at'],
            updated_at=i['updated_at'],
            labels=[l['name'] for l in i.get('labels', [])],
            assignees=[a['login'] for a in i.get('assignees', [])],
            comments=i.get('comments', 0),
            body=i.get('body', '')[:500] if i.get('body') else None
        ) for i in issues[:limit]]
    
    def _get_issue(self, owner: str, repo: str, issue_number: int, token: Optional[str]) -> IssueInfo:
        """Récupère les détails d'une issue."""
        data = self._api_get(f"/repos/{owner}/{repo}/issues/{issue_number}", token)
        return IssueInfo(
            number=data['number'],
            title=data['title'],
            state=data['state'],
            html_url=data['html_url'],
            user=data['user']['login'],
            created_at=data['created_at'],
            updated_at=data['updated_at'],
            labels=[l['name'] for l in data.get('labels', [])],
            assignees=[a['login'] for a in data.get('assignees', [])],
            comments=data.get('comments', 0),
            body=data.get('body', '')[:2000] if data.get('body') else None
        )
    
    def _list_branches(self, owner: str, repo: str, token: Optional[str], limit: int) -> List[BranchInfo]:
        """Liste les branches d'un repo."""
        data = self._api_get(f"/repos/{owner}/{repo}/branches", token, {"per_page": limit})
        return [BranchInfo(
            name=b['name'],
            sha=b['commit']['sha'],
            protected=b.get('protected', False)
        ) for b in data[:limit]]
    
    def _list_commits(self, owner: str, repo: str, branch: Optional[str], token: Optional[str], limit: int) -> List[CommitInfo]:
        """Liste les commits d'un repo/branche."""
        params = {"per_page": limit}
        if branch:
            params["sha"] = branch
        
        data = self._api_get(f"/repos/{owner}/{repo}/commits", token, params)
        return [CommitInfo(
            sha=c['sha'][:7],
            message=c['commit']['message'].split('\n')[0][:100],
            author=c['commit']['author']['name'],
            date=c['commit']['author']['date'],
            html_url=c['html_url']
        ) for c in data[:limit]]
    
    def _list_workflows(self, owner: str, repo: str, token: Optional[str], limit: int) -> List[WorkflowRun]:
        """Liste les exécutions de workflows CI/CD."""
        data = self._api_get(f"/repos/{owner}/{repo}/actions/runs", token, {"per_page": limit})
        runs = data.get('workflow_runs', [])
        
        return [WorkflowRun(
            id=r['id'],
            name=r['name'],
            status=r['status'],
            conclusion=r.get('conclusion'),
            html_url=r['html_url'],
            created_at=r['created_at'],
            head_branch=r['head_branch']
        ) for r in runs[:limit]]
    
    def _search_code(self, query: str, owner: Optional[str], repo: Optional[str], token: Optional[str], limit: int) -> List[SearchResult]:
        """Recherche du code sur GitHub."""
        q = query
        if owner and repo:
            q += f" repo:{owner}/{repo}"
        elif owner:
            q += f" user:{owner}"
        
        data = self._api_get("/search/code", token, {"q": q, "per_page": limit})
        items = data.get('items', [])
        
        return [SearchResult(
            name=i['name'],
            path=i['path'],
            repository=i['repository']['full_name'],
            html_url=i['html_url'],
            score=i.get('score', 0)
        ) for i in items[:limit]]
    
    def _execute_core_logic(self, request: GitHubRequest, **kwargs) -> GitHubResponse:
        """Exécute la logique principale."""
        
        if request.command == 'list_repos':
            repos = self._list_repos(request.owner, request.token, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(repos)} repository(ies) trouvé(s)",
                repos=repos
            )
        
        elif request.command == 'get_repo':
            if not request.owner or not request.repo:
                raise ToolExecutionError("owner et repo requis pour get_repo")
            repo = self._get_repo(request.owner, request.repo, request.token)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ Repository {repo.full_name}",
                repo=repo
            )
        
        elif request.command == 'list_prs':
            if not request.owner or not request.repo:
                raise ToolExecutionError("owner et repo requis pour list_prs")
            prs = self._list_prs(request.owner, request.repo, request.state, request.token, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(prs)} PR(s) {request.state}",
                prs=prs
            )
        
        elif request.command == 'get_pr':
            if not request.owner or not request.repo or not request.pr_number:
                raise ToolExecutionError("owner, repo et pr_number requis pour get_pr")
            pr = self._get_pr(request.owner, request.repo, request.pr_number, request.token)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ PR #{pr.number}: {pr.title}",
                pr=pr
            )
        
        elif request.command == 'pr_files':
            if not request.owner or not request.repo or not request.pr_number:
                raise ToolExecutionError("owner, repo et pr_number requis pour pr_files")
            files = self._get_pr_files(request.owner, request.repo, request.pr_number, request.token, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(files)} fichier(s) modifié(s) dans PR #{request.pr_number}",
                files=files
            )
        
        elif request.command == 'pr_comments':
            if not request.owner or not request.repo or not request.pr_number:
                raise ToolExecutionError("owner, repo et pr_number requis pour pr_comments")
            comments = self._get_pr_comments(request.owner, request.repo, request.pr_number, request.token, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(comments)} commentaire(s) sur PR #{request.pr_number}",
                comments=comments
            )
        
        elif request.command == 'list_issues':
            if not request.owner or not request.repo:
                raise ToolExecutionError("owner et repo requis pour list_issues")
            issues = self._list_issues(request.owner, request.repo, request.state, request.token, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(issues)} issue(s) {request.state}",
                issues=issues
            )
        
        elif request.command == 'get_issue':
            if not request.owner or not request.repo or not request.issue_number:
                raise ToolExecutionError("owner, repo et issue_number requis pour get_issue")
            issue = self._get_issue(request.owner, request.repo, request.issue_number, request.token)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ Issue #{issue.number}: {issue.title}",
                issue=issue
            )
        
        elif request.command == 'repo_branches':
            if not request.owner or not request.repo:
                raise ToolExecutionError("owner et repo requis pour repo_branches")
            branches = self._list_branches(request.owner, request.repo, request.token, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(branches)} branche(s)",
                branches=branches
            )
        
        elif request.command == 'repo_commits':
            if not request.owner or not request.repo:
                raise ToolExecutionError("owner et repo requis pour repo_commits")
            commits = self._list_commits(request.owner, request.repo, request.branch, request.token, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(commits)} commit(s) récent(s)",
                commits=commits
            )
        
        elif request.command == 'list_workflows':
            if not request.owner or not request.repo:
                raise ToolExecutionError("owner et repo requis pour list_workflows")
            workflows = self._list_workflows(request.owner, request.repo, request.token, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(workflows)} workflow run(s)",
                workflows=workflows
            )
        
        elif request.command == 'workflow_runs':
            # Alias pour list_workflows
            if not request.owner or not request.repo:
                raise ToolExecutionError("owner et repo requis pour workflow_runs")
            workflows = self._list_workflows(request.owner, request.repo, request.token, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(workflows)} workflow run(s)",
                workflows=workflows
            )
        
        elif request.command == 'search_code':
            if not request.query:
                raise ToolExecutionError("query requis pour search_code")
            results = self._search_code(request.query, request.owner, request.repo, request.token, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(results)} résultat(s) pour '{request.query}'",
                search_results=results
            )
        
        else:
            raise ToolExecutionError(f"Commande inconnue: {request.command}")
