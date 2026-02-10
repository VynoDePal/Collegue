"""
GitHub Operations Tool - Gestion des repositories, PRs, issues via l'API GitHub

Permet à Collègue d'interagir avec GitHub sans changer de fenêtre.
"""
import logging
import os
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, Field, field_validator
from .base import BaseTool, ToolExecutionError
from .github_commands import GitHubClient
from .github_commands.repos import RepoCommands, RepoInfo
from .github_commands.prs import PRCommands, PRInfo, FileChange, Comment
from .github_commands.issues import IssueCommands, IssueInfo
from .github_commands.branches import BranchCommands, BranchInfo, CommitInfo
from .github_commands.files import FileCommands

try:
    from fastmcp.server.dependencies import get_http_headers
except Exception:
    get_http_headers = None

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from collegue.autonomous.config_registry import get_config_registry
    HAS_CONFIG_REGISTRY = True
except ImportError:
    HAS_CONFIG_REGISTRY = False


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
        valid = ['list_repos', 'get_repo', 'get_file', 'list_prs', 'get_pr', 'create_pr',
                 'list_issues', 'get_issue', 'create_issue', 'pr_files', 'pr_comments',
                 'repo_branches', 'create_branch', 'update_file',
                 'repo_commits', 'search_code', 'list_workflows', 'workflow_runs']
        if v not in valid:
            raise ValueError(f"Commande invalide. Valides: {valid}")
        return v

class SearchResult(BaseModel):
    name: str
    path: str
    repository: str
    html_url: str
    score: float

class WorkflowRun(BaseModel):
    id: int
    name: str
    status: str
    conclusion: Optional[str] = None
    html_url: str
    created_at: str
    head_branch: str

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
    API_BASE = "https://api.github.com"

    tool_name = "github_ops"
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

    def _init_commands(self, token: Optional[str] = None):
        """Initialize command instances with token."""
        if self._repos is None:
            self._repos = RepoCommands(token=token, logger=self.logger)
            self._prs = PRCommands(token=token, logger=self.logger)
            self._issues = IssueCommands(token=token, logger=self.logger)
            self._branches = BranchCommands(token=token, logger=self.logger)
            self._files = FileCommands(token=token, logger=self.logger)

    def _get_token_from_http_headers(self) -> Optional[str]:
        if get_http_headers is None:
            return None
        headers = get_http_headers() or {}
        return headers.get("x-github-token") or headers.get("x-collegue-github-token")

    def _get_headers(self, token: Optional[str] = None) -> Dict[str, str]:
        gh_token = token or os.environ.get('GITHUB_TOKEN') or self._get_token_from_http_headers()
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        if gh_token:
            headers["Authorization"] = f"Bearer {gh_token}"
            self.logger.debug(f"Token GitHub configuré (longueur: {len(gh_token)})")
        else:
            self.logger.warning("Aucun token GitHub configuré - accès limité aux repos publics")
        return headers

    def _has_token(self, token: Optional[str] = None) -> bool:
        return bool(token or os.environ.get('GITHUB_TOKEN') or self._get_token_from_http_headers())

    def _list_workflows(self, owner: str, repo: str, token: Optional[str], limit: int) -> List[WorkflowRun]:
        """List workflow runs for a repository."""
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
        """Search code on GitHub."""
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

    def _api_get(self, endpoint: str, token: Optional[str] = None, params: Optional[Dict] = None) -> Any:
        if not HAS_REQUESTS:
            raise ToolExecutionError("requests non installé. Installez avec: pip install requests")

        url = f"{self.API_BASE}{endpoint}"
        headers = self._get_headers(token)

        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)

            if response.status_code == 404:
                raise ToolExecutionError(f"Ressource introuvable: {endpoint}")
            elif response.status_code == 401:
                has_token = self._has_token(token)
                if has_token:
                    raise ToolExecutionError(
                        "Token GitHub invalide ou expiré. Vérifiez que GITHUB_TOKEN est correct dans la config MCP env."
                    )
                else:
                    raise ToolExecutionError(
                        "Authentification requise. Fournissez request.token, ou configurez GITHUB_TOKEN côté serveur, "
                        "ou envoyez un header X-GitHub-Token via mcp-remote. Pour les repos publics, fournissez owner "
                        "explicitement (ex: owner='microsoft')."
                    )
            elif response.status_code == 403:
                remaining = response.headers.get('X-RateLimit-Remaining', '?')
                raise ToolExecutionError(f"Rate limit GitHub atteint. Restant: {remaining}")
            elif response.status_code >= 400:
                raise ToolExecutionError(f"Erreur API GitHub {response.status_code}: {response.text[:200]}")

            return response.json()
        except requests.RequestException as e:
            raise ToolExecutionError(f"Erreur réseau GitHub: {e}")

    def _list_repos(self, owner: Optional[str], token: Optional[str], limit: int) -> List[RepoInfo]:
        if owner:
            endpoint = f"/users/{owner}/repos"
        else:
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
        data = self._api_get(f"/repos/{owner}/{repo}/pulls/{pr_number}/files", token, {"per_page": limit})

        return [FileChange(
            filename=f['filename'],
            status=f['status'],
            additions=f.get('additions', 0),
            deletions=f.get('deletions', 0),
            patch=f.get('patch', '')[:2000] if f.get('patch') else None
        ) for f in data[:limit]]

    def _get_pr_comments(self, owner: str, repo: str, pr_number: int, token: Optional[str], limit: int) -> List[Comment]:
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

        all_comments.sort(key=lambda x: x.created_at)
        return all_comments[:limit]

    def _list_issues(self, owner: str, repo: str, state: str, token: Optional[str], limit: int) -> List[IssueInfo]:
        data = self._api_get(f"/repos/{owner}/{repo}/issues", token, {
            "state": state,
            "per_page": limit,
            "sort": "updated",
            "direction": "desc"
        })

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
        data = self._api_get(f"/repos/{owner}/{repo}/branches", token, {"per_page": limit})
        return [BranchInfo(
            name=b['name'],
            sha=b['commit']['sha'],
            protected=b.get('protected', False)
        ) for b in data[:limit]]

    def _list_commits(self, owner: str, repo: str, branch: Optional[str], token: Optional[str], limit: int) -> List[CommitInfo]:
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

    def _api_post(self, endpoint: str, data: Dict, token: Optional[str] = None) -> Any:
        if not HAS_REQUESTS:
            raise ToolExecutionError("requests non installé. Installez avec: pip install requests")

        url = f"{self.API_BASE}{endpoint}"
        headers = self._get_headers(token)

        try:
            response = requests.post(url, headers=headers, json=data, timeout=30)

            if response.status_code == 404:
                raise ToolExecutionError(f"Ressource introuvable: {endpoint}")
            elif response.status_code == 401:
                raise ToolExecutionError("Token GitHub invalide ou expiré (requis pour l'écriture)")
            elif response.status_code == 403:
                raise ToolExecutionError("Permission refusée (scope insuffisant ?)")
            elif response.status_code >= 400:
                raise ToolExecutionError(f"Erreur API GitHub {response.status_code}: {response.text[:200]}")

            return response.json()
        except requests.RequestException as e:
            raise ToolExecutionError(f"Erreur réseau GitHub: {e}")

    def _api_put(self, endpoint: str, data: Dict, token: Optional[str] = None) -> Any:
        if not HAS_REQUESTS:
            raise ToolExecutionError("requests non installé")

        url = f"{self.API_BASE}{endpoint}"
        headers = self._get_headers(token)

        try:
            response = requests.put(url, headers=headers, json=data, timeout=30)

            if response.status_code >= 400:
                raise ToolExecutionError(f"Erreur API GitHub {response.status_code}: {response.text[:200]}")

            return response.json()
        except requests.RequestException as e:
            raise ToolExecutionError(f"Erreur réseau GitHub: {e}")

    def _create_pr(self, owner: str, repo: str, title: str, head: str, base: str, body: Optional[str], token: Optional[str]) -> PRInfo:
        data = {
            "title": title,
            "head": head,
            "base": base,
            "body": body or ""
        }
        resp = self._api_post(f"/repos/{owner}/{repo}/pulls", data, token)
        return PRInfo(
            number=resp['number'],
            title=resp['title'],
            state=resp['state'],
            html_url=resp['html_url'],
            user=resp['user']['login'],
            base_branch=resp['base']['ref'],
            head_branch=resp['head']['ref'],
            created_at=resp['created_at'],
            updated_at=resp['updated_at'],
            draft=resp.get('draft', False)
        )

    def _create_issue(self, owner: str, repo: str, title: str, body: Optional[str], token: Optional[str]) -> IssueInfo:
        data = {
            "title": title,
            "body": body or ""
        }
        resp = self._api_post(f"/repos/{owner}/{repo}/issues", data, token)
        return IssueInfo(
            number=resp['number'],
            title=resp['title'],
            state=resp['state'],
            html_url=resp['html_url'],
            user=resp['user']['login'],
            created_at=resp['created_at'],
            updated_at=resp['updated_at'],
            body=resp.get('body')
        )

    def _get_branch_sha(self, owner: str, repo: str, branch: str, token: Optional[str]) -> str:
        try:
            resp = self._api_get(f"/repos/{owner}/{repo}/git/ref/heads/{branch}", token)
            return resp['object']['sha']
        except ToolExecutionError:
            raise ToolExecutionError(f"Branche source '{branch}' introuvable")

    def _create_branch(self, owner: str, repo: str, branch: str, from_branch: Optional[str], token: Optional[str]) -> BranchInfo:
        if not from_branch:
            repo_info = self._get_repo(owner, repo, token)
            from_branch = repo_info.default_branch

        sha = self._get_branch_sha(owner, repo, from_branch, token)

        data = {
            "ref": f"refs/heads/{branch}",
            "sha": sha
        }
        resp = self._api_post(f"/repos/{owner}/{repo}/git/refs", data, token)

        return BranchInfo(
            name=branch,
            sha=resp['object']['sha'],
            protected=False
        )

    def _update_file(self, owner: str, repo: str, path: str, message: str, content: str, branch: Optional[str], token: Optional[str]) -> Dict:
        sha = None
        try:
            url = f"/repos/{owner}/{repo}/contents/{path}"
            if branch:
                url += f"?ref={branch}"

            current = self._api_get(url, token)
            sha = current['sha']
        except ToolExecutionError as e:
            if "introuvable" not in str(e) and "404" not in str(e):
                raise e

        content_b64 = base64.b64encode(content.encode('utf-8')).decode('utf-8')

        data = {
            "message": message,
            "content": content_b64
        }
        if sha:
            data["sha"] = sha
        if branch:
            data["branch"] = branch

        resp = self._api_put(f"/repos/{owner}/{repo}/contents/{path}", data, token)

        return {
            "content": resp['content'],
            "commit": resp['commit']
        }

    def _get_file_content(self, owner: str, repo: str, path: str, branch: Optional[str], token: Optional[str]) -> Dict:
        url = f"/repos/{owner}/{repo}/contents/{path}"
        if branch:
            url += f"?ref={branch}"

        data = self._api_get(url, token)

        return {
            "content": data.get('content', ''),
            "sha": data.get('sha', ''),
            "name": data.get('name', ''),
            "path": data.get('path', ''),
            "size": data.get('size', 0),
            "encoding": data.get('encoding', 'base64')
        }

    def _execute_core_logic(self, request: GitHubRequest, **kwargs) -> GitHubResponse:
        token = request.token or os.environ.get('GITHUB_TOKEN') or self._get_token_from_http_headers()
        self._init_commands(token)

        if token and request.owner and HAS_CONFIG_REGISTRY:
            try:
                sentry_org = os.environ.get('SENTRY_ORG')
                if get_http_headers:
                    headers = get_http_headers() or {}
                    sentry_org = sentry_org or headers.get('x-sentry-org')
                if sentry_org:
                    get_config_registry().register(
                        sentry_org=sentry_org,
                        github_token=token,
                        github_owner=request.owner,
                        github_repo=request.repo
                    )
            except Exception:
                pass

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
            workflows = self._list_workflows(request.owner, request.repo, token, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(workflows)} workflow run(s)",
                workflows=workflows
            )

        elif request.command == 'workflow_runs':
            if not request.owner or not request.repo:
                raise ToolExecutionError("owner et repo requis pour workflow_runs")
            workflows = self._list_workflows(request.owner, request.repo, token, request.limit)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(workflows)} workflow run(s)",
                workflows=workflows
            )

        elif request.command == 'search_code':
            if not request.query:
                raise ToolExecutionError("query requis pour search_code")
            results = self._search_code(request.query, request.owner, request.repo, token, request.limit)
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
