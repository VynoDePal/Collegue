"""
GitHub Operations Tool - Gestion des repositories, PRs, issues via l'API GitHub

Permet à Collègue d'interagir avec GitHub sans changer de fenêtre.
"""
import logging
import os
import base64
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, Field, field_validator
from .base import BaseTool, ToolExecutionError

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


class GitHubRequest(BaseModel):
    """Modèle de requête pour les opérations GitHub.
    
    PARAMÈTRES REQUIS PAR COMMANDE:
    - list_repos: owner (optionnel, liste vos repos si omis)
    - get_repo: owner + repo
    - list_prs: owner + repo
    - get_pr: owner + repo + pr_number
    - create_pr: owner + repo + title + head + base
    - pr_files: owner + repo + pr_number
    - pr_comments: owner + repo + pr_number
    - list_issues: owner + repo
    - get_issue: owner + repo + issue_number
    - create_issue: owner + repo + title + body
    - repo_branches: owner + repo
    - create_branch: owner + repo + branch + from_branch (ou sha)
    - update_file: owner + repo + path + message + content + branch
    - repo_commits: owner + repo
    - search_code: query (owner/repo optionnels pour filtrer)
    - list_workflows: owner + repo
    """
    command: str = Field(
        ...,
        description="Commande à exécuter. IMPORTANT: list_prs/list_issues/get_repo nécessitent owner ET repo. Commandes: list_repos, get_repo, create_pr, list_prs, get_pr, create_issue, list_issues, get_issue, pr_files, pr_comments, create_branch, update_file, repo_branches, repo_commits, search_code, list_workflows"
    )
    owner: Optional[str] = Field(
        None, 
        description="REQUIS pour la plupart des commandes. Propriétaire du repo (username ou organisation)."
    )
    repo: Optional[str] = Field(
        None, 
        description="REQUIS avec owner pour get_repo, list_prs, list_issues, etc. Nom du repository."
    )
    # Paramètres existants
    pr_number: Optional[int] = Field(None, description="Numéro de la PR (requis pour get_pr, pr_files, pr_comments)")
    issue_number: Optional[int] = Field(None, description="Numéro de l'issue (requis pour get_issue)")
    branch: Optional[str] = Field(None, description="Nom de la branche (pour filtrer ou créer)")
    state: str = Field("open", description="Filtre par état: 'open', 'closed', ou 'all'")
    query: Optional[str] = Field(None, description="Requête de recherche (requis pour search_code)")
    limit: int = Field(30, description="Nombre max de résultats (1-100)", ge=1, le=100)
    token: Optional[str] = Field(None, description="Token GitHub (utilise automatiquement GITHUB_TOKEN de l'environnement si non fourni)")

    # Nouveaux paramètres pour l'écriture
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
        valid = ['list_repos', 'get_repo', 'list_prs', 'get_pr', 'create_pr',
                 'list_issues', 'get_issue', 'create_issue', 'pr_files', 'pr_comments', 
                 'repo_branches', 'create_branch', 'update_file',
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
        return (
            "Interagit avec l'API GitHub. IMPORTANT: Pour list_prs, list_issues, get_repo, "
            "vous DEVEZ fournir 'owner' ET 'repo' (ex: owner='microsoft', repo='vscode'). "
            "Commandes: list_repos, get_repo, list_prs, get_pr, list_issues, repo_branches, search_code"
        )
    
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
            self.logger.debug(f"Token GitHub configuré (longueur: {len(gh_token)})")
        else:
            self.logger.warning("Aucun token GitHub configuré - accès limité aux repos publics")
        return headers
    
    def _has_token(self, token: Optional[str] = None) -> bool:
        """Vérifie si un token est disponible."""
        return bool(token or os.environ.get('GITHUB_TOKEN'))
    
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
                has_token = self._has_token(token)
                if has_token:
                    raise ToolExecutionError(
                        "Token GitHub invalide ou expiré. Vérifiez que GITHUB_TOKEN est correct dans la config MCP env."
                    )
                else:
                    raise ToolExecutionError(
                        "Authentification requise. Configurez GITHUB_TOKEN dans le bloc 'env' de votre config MCP. "
                        "Pour les repos publics, fournissez owner explicitement (ex: owner='microsoft')."
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
    
    def _api_post(self, endpoint: str, data: Dict, token: Optional[str] = None) -> Any:
        """Effectue une requête POST à l'API GitHub."""
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
        """Effectue une requête PUT à l'API GitHub."""
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
        """Crée une Pull Request."""
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
        """Crée une Issue."""
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
        """Récupère le SHA d'une branche."""
        try:
            resp = self._api_get(f"/repos/{owner}/{repo}/git/ref/heads/{branch}", token)
            return resp['object']['sha']
        except ToolExecutionError:
            raise ToolExecutionError(f"Branche source '{branch}' introuvable")

    def _create_branch(self, owner: str, repo: str, branch: str, from_branch: Optional[str], token: Optional[str]) -> BranchInfo:
        """Crée une nouvelle branche."""
        # 1. Obtenir le SHA de la branche source
        if not from_branch:
            # Récupérer la branche par défaut
            repo_info = self._get_repo(owner, repo, token)
            from_branch = repo_info.default_branch
        
        sha = self._get_branch_sha(owner, repo, from_branch, token)
        
        # 2. Créer la ref
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
        """Crée ou met à jour un fichier."""
        # 1. Vérifier si le fichier existe pour avoir le SHA (update)
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
            # Si 404, c'est une création, sha reste None
            
        # 2. Encoder le contenu en base64
        content_b64 = base64.b64encode(content.encode('utf-8')).decode('utf-8')
        
        # 3. Préparer les données
        data = {
            "message": message,
            "content": content_b64
        }
        if sha:
            data["sha"] = sha
        if branch:
            data["branch"] = branch
            
        # 4. Envoyer le PUT
        resp = self._api_put(f"/repos/{owner}/{repo}/contents/{path}", data, token)
        
        return {
            "content": resp['content'],
            "commit": resp['commit']
        }

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
        
        elif request.command == 'create_pr':
            if not request.owner or not request.repo or not request.title or not request.head or not request.base:
                raise ToolExecutionError("owner, repo, title, head, base requis pour create_pr")
            pr = self._create_pr(request.owner, request.repo, request.title, request.head, request.base, request.body, request.token)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ PR créée: {pr.html_url}",
                pr=pr
            )

        elif request.command == 'create_issue':
            if not request.owner or not request.repo or not request.title:
                raise ToolExecutionError("owner, repo, title requis pour create_issue")
            issue = self._create_issue(request.owner, request.repo, request.title, request.body, request.token)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ Issue créée: {issue.html_url}",
                issue=issue
            )

        elif request.command == 'create_branch':
            if not request.owner or not request.repo or not request.branch:
                raise ToolExecutionError("owner, repo, branch requis pour create_branch")
            b_info = self._create_branch(request.owner, request.repo, request.branch, request.from_branch, request.token)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ Branche '{request.branch}' créée",
                branches=[b_info]
            )

        elif request.command == 'update_file':
            if not request.owner or not request.repo or not request.path or not request.message or request.content is None:
                raise ToolExecutionError("owner, repo, path, message, content requis pour update_file")
            result = self._update_file(request.owner, request.repo, request.path, request.message, request.content, request.branch, request.token)
            return GitHubResponse(
                success=True,
                command=request.command,
                message=f"✅ Fichier '{request.path}' mis à jour",
                files=[FileChange(filename=request.path, status="updated", additions=0, deletions=0)]
            )

        else:
            raise ToolExecutionError(f"Commande inconnue: {request.command}")
