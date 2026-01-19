"""
Sentry Monitor Tool - Récupération des erreurs et monitoring depuis Sentry

Permet à Collègue de récupérer les stacktraces réelles et prioriser le refactoring.
"""
import logging
import os
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, Field, field_validator
from .base import BaseTool, ToolExecutionError

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


class SentryRequest(BaseModel):
    """Modèle de requête pour les opérations Sentry.
    
    PARAMÈTRES REQUIS PAR COMMANDE:
    - list_projects: organization (utilise SENTRY_ORG si non fourni)
    - list_issues: organization (+ project optionnel pour filtrer)
    - get_issue: issue_id
    - issue_events: issue_id (récupère les stacktraces)
    - issue_tags: issue_id
    - project_stats: organization + project
    - list_releases: organization (+ project optionnel)
    - parse_config: content (contenu du fichier .sentryclirc ou sentry.properties)
    """
    command: str = Field(
        ...,
        description="Commande Sentry. list_issues nécessite organization. get_issue/issue_events nécessitent issue_id. Commandes: list_projects, list_issues, get_issue, issue_events, project_stats, list_releases, issue_tags, parse_config"
    )
    organization: Optional[str] = Field(
        None, 
        description="REQUIS pour la plupart des commandes. Slug de l'organisation Sentry (utilise SENTRY_ORG de l'environnement si non fourni)"
    )
    project: Optional[str] = Field(
        None, 
        description="Slug du projet Sentry (requis pour project_stats, optionnel pour filtrer list_issues)"
    )
    issue_id: Optional[str] = Field(
        None, 
        description="REQUIS pour get_issue, issue_events, issue_tags. ID numérique de l'issue Sentry"
    )
    query: Optional[str] = Field(None, description="Filtres de recherche Sentry (ex: 'is:unresolved', 'level:error')")
    time_range: str = Field("24h", description="Période d'analyse: '1h', '24h', '7d', '14d', '30d'")
    limit: int = Field(25, description="Nombre max de résultats (1-100)", ge=1, le=100)
    token: Optional[str] = Field(None, description="Token Sentry (utilise automatiquement SENTRY_AUTH_TOKEN de l'environnement si non fourni)")
    sentry_url: Optional[str] = Field(None, description="URL Sentry self-hosted (défaut: https://sentry.io)")
    
    # Paramètres pour parse_config
    content: Optional[str] = Field(None, description="Contenu du fichier de configuration pour parse_config")
    format: Optional[str] = Field("ini", description="Format du fichier: 'ini' (.sentryclirc) ou 'properties' (sentry.properties)")

    @field_validator('command')
    @classmethod
    def validate_command(cls, v: str) -> str:
        valid = ['list_projects', 'list_issues', 'get_issue', 'issue_events', 
                 'project_stats', 'list_releases', 'issue_tags', 'parse_config', 
                 'get_project', 'list_repos']
        if v not in valid:
            raise ValueError(f"Commande invalide. Valides: {valid}")
        return v


class ConfigInfo(BaseModel):
    """Information de configuration extraite."""
    token: Optional[str] = None
    organization: Optional[str] = None
    project: Optional[str] = None
    sentry_url: Optional[str] = None


class RepoInfo(BaseModel):
    """Information sur un repository Sentry (intégration)."""
    id: str
    name: str
    provider: Optional[str] = None
    url: Optional[str] = None
    status: str = "active"


class ProjectInfo(BaseModel):
    """Information sur un projet Sentry."""
    id: str
    slug: str
    name: str
    platform: Optional[str] = None
    status: str = "active"
    options: Optional[Dict[str, Any]] = None
    organization: Optional[Dict[str, Any]] = None

class IssueInfo(BaseModel):
    """Information sur une issue Sentry."""
    id: str
    short_id: str
    title: str
    culprit: Optional[str] = None
    level: str
    status: str
    count: int
    user_count: int
    first_seen: str
    last_seen: str
    permalink: str
    is_unhandled: bool = False
    type: str = "error"


class EventInfo(BaseModel):
    """Information sur un événement/stacktrace."""
    event_id: str
    title: str
    message: Optional[str] = None
    platform: Optional[str] = None
    timestamp: str
    tags: Dict[str, str] = {}
    context: Dict[str, Any] = {}
    stacktrace: Optional[str] = None
    user: Optional[Dict[str, Any]] = None
    request: Optional[Dict[str, Any]] = None


class ReleaseInfo(BaseModel):
    """Information sur une release."""
    version: str
    short_version: str
    date_created: str
    first_event: Optional[str] = None
    last_event: Optional[str] = None
    new_groups: int = 0
    url: Optional[str] = None


class ProjectStats(BaseModel):
    """Statistiques d'un projet."""
    project: str
    total_events: int = 0
    total_issues: int = 0
    unresolved_issues: int = 0
    events_24h: int = 0
    users_affected_24h: int = 0


class TagDistribution(BaseModel):
    """Distribution d'un tag."""
    key: str
    name: str
    values: List[Dict[str, Any]] = []


class SentryResponse(BaseModel):
    """Modèle de réponse pour les opérations Sentry."""
    success: bool
    command: str
    message: str
    projects: Optional[List[ProjectInfo]] = None
    issues: Optional[List[IssueInfo]] = None
    issue: Optional[IssueInfo] = None
    events: Optional[List[EventInfo]] = None
    releases: Optional[List[ReleaseInfo]] = None
    repos: Optional[List[RepoInfo]] = None
    stats: Optional[ProjectStats] = None
    tags: Optional[List[TagDistribution]] = None
    config: Optional[ConfigInfo] = None


class SentryMonitorTool(BaseTool):
    """
    Outil de monitoring et récupération d'erreurs depuis Sentry.
    
    Fonctionnalités:
    - Lister les projets et issues
    - Récupérer les détails d'une issue avec stacktrace
    - Voir les statistiques de projet
    - Lister les releases
    - Analyser les tags (browser, OS, etc.)
    - Parser les fichiers de configuration locaux (.sentryclirc, sentry.properties)
    """
    
    def get_name(self) -> str:
        return "sentry_monitor"
    
    def get_description(self) -> str:
        return "Récupère les erreurs, stacktraces et statistiques depuis Sentry pour prioriser le debugging"
    
    def get_request_model(self) -> Type[BaseModel]:
        return SentryRequest
    
    def get_response_model(self) -> Type[BaseModel]:
        return SentryResponse
    
    def get_supported_languages(self) -> List[str]:
        return []

    def _parse_sentryclirc(self, content: str) -> ConfigInfo:
        """Parse un contenu style INI (.sentryclirc)."""
        import configparser
        import io
        
        config = configparser.ConfigParser()
        try:
            config.read_string(content)
        except configparser.Error as e:
            raise ToolExecutionError(f"Erreur de parsing .sentryclirc: {e}")
            
        info = ConfigInfo()
        
        if 'auth' in config:
            info.token = config['auth'].get('token')
            
        if 'defaults' in config:
            info.organization = config['defaults'].get('org')
            info.project = config['defaults'].get('project')
            info.sentry_url = config['defaults'].get('url')
            
        return info

    def _parse_sentry_properties(self, content: str) -> ConfigInfo:
        """Parse un contenu style Java Properties (sentry.properties)."""
        info = ConfigInfo()
        
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('!'):
                continue
                
            if '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()
                
                if key == 'auth.token':
                    info.token = value
                elif key == 'defaults.org':
                    info.organization = value
                elif key == 'defaults.project':
                    info.project = value
                elif key == 'defaults.url':
                    info.sentry_url = value
                    
        return info

    def _get_token_from_http_headers(self) -> Optional[str]:
        if get_http_headers is None:
            return None
        headers = get_http_headers() or {}
        return headers.get("x-sentry-token") or headers.get("x-collegue-sentry-token")

    def _get_org_from_http_headers(self) -> Optional[str]:
        if get_http_headers is None:
            return None
        headers = get_http_headers() or {}
        return headers.get("x-sentry-org") or headers.get("x-collegue-sentry-org")

    def _get_url_from_http_headers(self) -> Optional[str]:
        if get_http_headers is None:
            return None
        headers = get_http_headers() or {}
        return headers.get("x-sentry-url") or headers.get("x-collegue-sentry-url")
    
    def _get_base_url(self, sentry_url: Optional[str] = None) -> str:
        """Retourne l'URL de base de l'API Sentry."""
        url = sentry_url or os.environ.get('SENTRY_URL') or self._get_url_from_http_headers() or 'https://sentry.io'
        return f"{url.rstrip('/')}/api/0"
    
    def _get_headers(self, token: Optional[str] = None) -> Dict[str, str]:
        """Construit les headers pour l'API Sentry."""
        sentry_token = token or os.environ.get('SENTRY_AUTH_TOKEN') or self._get_token_from_http_headers()
        
        if not sentry_token:
            raise ToolExecutionError(
                "Token Sentry requis. Fournissez request.token, ou définissez SENTRY_AUTH_TOKEN côté serveur, "
                "ou envoyez un header X-Sentry-Token via mcp-remote."
            )
        
        return {
            "Authorization": f"Bearer {sentry_token}",
            "Content-Type": "application/json"
        }
    
    def _api_get(self, endpoint: str, token: Optional[str] = None, 
                 sentry_url: Optional[str] = None, params: Optional[Dict] = None) -> Any:
        """Effectue une requête GET à l'API Sentry."""
        if not HAS_REQUESTS:
            raise ToolExecutionError("requests non installé. Installez avec: pip install requests")
        
        base_url = self._get_base_url(sentry_url)
        url = f"{base_url}{endpoint}"
        headers = self._get_headers(token)
        
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            
            if response.status_code == 404:
                raise ToolExecutionError(f"Ressource introuvable: {endpoint}")
            elif response.status_code == 401:
                raise ToolExecutionError("Token Sentry invalide ou expiré")
            elif response.status_code == 403:
                raise ToolExecutionError("Accès refusé. Vérifiez les permissions du token.")
            elif response.status_code >= 400:
                raise ToolExecutionError(f"Erreur API Sentry {response.status_code}: {response.text[:200]}")
            
            return response.json()
        except requests.RequestException as e:
            raise ToolExecutionError(f"Erreur réseau Sentry: {e}")
    
    def _get_project(self, org: str, project_slug: str, token: Optional[str], sentry_url: Optional[str]) -> ProjectInfo:
        """Récupère les détails d'un projet."""
        data = self._api_get(f"/projects/{org}/{project_slug}/", token, sentry_url)
        
        return ProjectInfo(
            id=data['id'],
            slug=data['slug'],
            name=data['name'],
            platform=data.get('platform'),
            status=data.get('status', 'active'),
            options=data.get('options', {}),
            organization=data.get('organization', {})
        )

    def _list_projects(self, org: str, token: Optional[str], sentry_url: Optional[str]) -> List[ProjectInfo]:
        """Liste les projets d'une organisation."""
        data = self._api_get(f"/organizations/{org}/projects/", token, sentry_url)
        
        return [ProjectInfo(
            id=p['id'],
            slug=p['slug'],
            name=p['name'],
            platform=p.get('platform'),
            status=p.get('status', 'active'),
            organization=p.get('organization', {})
        ) for p in data]
    
    def _list_issues(self, org: str, project: Optional[str], query: Optional[str],
                     token: Optional[str], sentry_url: Optional[str], limit: int) -> List[IssueInfo]:
        """Liste les issues d'un projet."""
        params = {"limit": limit}
        
        if project:
            params["project"] = project
        if query:
            params["query"] = query
        else:
            params["query"] = "is:unresolved"
        
        data = self._api_get(f"/organizations/{org}/issues/", token, sentry_url, params)
        
        return [IssueInfo(
            id=i['id'],
            short_id=i['shortId'],
            title=i['title'],
            culprit=i.get('culprit'),
            level=i.get('level', 'error'),
            status=i.get('status', 'unresolved'),
            count=i.get('count', 0),
            user_count=i.get('userCount', 0),
            first_seen=i['firstSeen'],
            last_seen=i['lastSeen'],
            permalink=i['permalink'],
            is_unhandled=i.get('isUnhandled', False),
            type=i.get('type', 'error')
        ) for i in data[:limit]]
    
    def _get_issue(self, issue_id: str, token: Optional[str], sentry_url: Optional[str], 
                    org: Optional[str] = None) -> IssueInfo:
        """Récupère les détails d'une issue."""
        # L'API Sentry requiert l'organisation dans le path
        if org:
            endpoint = f"/organizations/{org}/issues/{issue_id}/"
        else:
            endpoint = f"/issues/{issue_id}/"
        data = self._api_get(endpoint, token, sentry_url)
        
        return IssueInfo(
            id=data['id'],
            short_id=data['shortId'],
            title=data['title'],
            culprit=data.get('culprit'),
            level=data.get('level', 'error'),
            status=data.get('status', 'unresolved'),
            count=data.get('count', 0),
            user_count=data.get('userCount', 0),
            first_seen=data['firstSeen'],
            last_seen=data['lastSeen'],
            permalink=data['permalink'],
            is_unhandled=data.get('isUnhandled', False),
            type=data.get('type', 'error')
        )
    
    def _get_issue_events(self, issue_id: str, token: Optional[str], 
                          sentry_url: Optional[str], limit: int,
                          org: Optional[str] = None) -> List[EventInfo]:
        """Récupère les événements/stacktraces d'une issue."""
        # L'API Sentry requiert l'organisation dans le path + full=true pour la stacktrace
        if org:
            endpoint = f"/organizations/{org}/issues/{issue_id}/events/"
        else:
            endpoint = f"/issues/{issue_id}/events/"
        data = self._api_get(endpoint, token, sentry_url, {"limit": limit, "full": "true"})
        
        events = []
        for e in data[:limit]:
            # Extraire la stacktrace si disponible
            stacktrace = None
            entries = e.get('entries', [])
            for entry in entries:
                if entry.get('type') == 'exception':
                    exc_data = entry.get('data', {})
                    values = exc_data.get('values', [])
                    if values:
                        frames = values[0].get('stacktrace', {}).get('frames', [])
                        if frames:
                            # Formater la stacktrace (derniers frames)
                            st_lines = []
                            for frame in frames[-10:]:
                                filename = frame.get('filename', '?')
                                lineno = frame.get('lineNo', '?')
                                func = frame.get('function', '?')
                                context = frame.get('context', [])
                                st_lines.append(f"  File \"{filename}\", line {lineno}, in {func}")
                                if context:
                                    for ctx in context[-3:]:
                                        if isinstance(ctx, list) and len(ctx) >= 2:
                                            st_lines.append(f"    {ctx[1]}")
                            stacktrace = "\n".join(st_lines)
                    break
            
            # Extraire les tags
            tags = {}
            for tag in e.get('tags', []):
                if isinstance(tag, dict):
                    tags[tag.get('key', '')] = tag.get('value', '')
            
            # Extraire le contexte utilisateur
            user_ctx = e.get('user')
            if user_ctx:
                user_ctx = {
                    'id': user_ctx.get('id'),
                    'email': user_ctx.get('email'),
                    'username': user_ctx.get('username'),
                    'ip_address': user_ctx.get('ip_address')
                }
            
            # Extraire la requête HTTP
            request_ctx = None
            for entry in entries:
                if entry.get('type') == 'request':
                    req_data = entry.get('data', {})
                    request_ctx = {
                        'url': req_data.get('url'),
                        'method': req_data.get('method'),
                        'headers': dict(list(req_data.get('headers', []))[:5]) if req_data.get('headers') else None
                    }
                    break
            
            events.append(EventInfo(
                event_id=e['eventID'],
                title=e.get('title', ''),
                message=e.get('message'),
                platform=e.get('platform'),
                timestamp=e.get('dateCreated', ''),
                tags=tags,
                context=e.get('context', {}),
                stacktrace=stacktrace,
                user=user_ctx,
                request=request_ctx
            ))
        
        return events
    
    def _get_issue_tags(self, issue_id: str, token: Optional[str], sentry_url: Optional[str],
                         org: Optional[str] = None) -> List[TagDistribution]:
        """Récupère la distribution des tags d'une issue."""
        if org:
            endpoint = f"/organizations/{org}/issues/{issue_id}/tags/"
        else:
            endpoint = f"/issues/{issue_id}/tags/"
        data = self._api_get(endpoint, token, sentry_url)
        
        return [TagDistribution(
            key=t['key'],
            name=t.get('name', t['key']),
            values=t.get('topValues', [])[:10]
        ) for t in data]
    
    def _get_project_stats(self, org: str, project: str, time_range: str,
                           token: Optional[str], sentry_url: Optional[str]) -> ProjectStats:
        """Récupère les statistiques d'un projet."""
        # Convertir time_range en stat period
        stat_periods = {
            '1h': '1h', '24h': '24h', '7d': '7d', '14d': '14d', '30d': '30d'
        }
        period = stat_periods.get(time_range, '24h')
        
        # Récupérer les stats
        stats_data = self._api_get(
            f"/projects/{org}/{project}/stats/",
            token, sentry_url,
            {"stat": "received", "resolution": "1d"}
        )
        
        # Compter les events récents
        total_events = sum(point[1] for point in stats_data) if stats_data else 0
        
        # Récupérer le nombre d'issues non résolues
        issues_data = self._api_get(
            f"/organizations/{org}/issues/",
            token, sentry_url,
            {"project": project, "query": "is:unresolved", "limit": 1}
        )
        
        return ProjectStats(
            project=project,
            total_events=total_events,
            unresolved_issues=len(issues_data) if issues_data else 0,
            events_24h=stats_data[-1][1] if stats_data else 0
        )
    
    def _list_releases(self, org: str, project: Optional[str],
                       token: Optional[str], sentry_url: Optional[str], limit: int) -> List[ReleaseInfo]:
        """Liste les releases."""
        endpoint = f"/organizations/{org}/releases/"
        params = {"per_page": limit}
        if project:
            params["project"] = project
        
        data = self._api_get(endpoint, token, sentry_url, params)
        
        return [ReleaseInfo(
            version=r['version'],
            short_version=r.get('shortVersion', r['version'][:20]),
            date_created=r['dateCreated'],
            first_event=r.get('firstEvent'),
            last_event=r.get('lastEvent'),
            new_groups=r.get('newGroups', 0),
            url=r.get('url')
        ) for r in data[:limit]]
    
    def _list_repos(self, org: str, token: Optional[str], sentry_url: Optional[str]) -> List[RepoInfo]:
        """Liste les dépôts liés à l'organisation."""
        data = self._api_get(f"/organizations/{org}/repos/", token, sentry_url)
        
        return [RepoInfo(
            id=r['id'],
            name=r['name'],
            provider=r.get('provider', {}).get('id') if isinstance(r.get('provider'), dict) else r.get('provider'),
            url=r.get('url'),
            status=r.get('status', 'active')
        ) for r in data]

    def _execute_core_logic(self, request: SentryRequest, **kwargs) -> SentryResponse:
        """Exécute la logique principale."""
        org = request.organization or os.environ.get('SENTRY_ORG') or self._get_org_from_http_headers()
        token = request.token or os.environ.get('SENTRY_AUTH_TOKEN') or self._get_token_from_http_headers()
        
        # Enregistrer la configuration pour le watchdog multi-utilisateur
        if org and HAS_CONFIG_REGISTRY:
            try:
                github_token = os.environ.get('GITHUB_TOKEN')
                if get_http_headers:
                    headers = get_http_headers() or {}
                    github_token = github_token or headers.get('x-github-token')
                get_config_registry().register(
                    sentry_org=org,
                    sentry_token=token,
                    github_token=github_token
                )
            except Exception:
                pass  # Silently ignore registry errors
        
        if not org and request.command not in ['get_issue', 'parse_config']:
            raise ToolExecutionError(
                "Organisation Sentry requise. Fournissez organization ou définissez SENTRY_ORG."
            )
        
        if request.command == 'parse_config':
            if not request.content:
                raise ToolExecutionError("content requis pour parse_config")
            
            if request.format == 'properties':
                info = self._parse_sentry_properties(request.content)
            else:
                info = self._parse_sentryclirc(request.content)
                
            return SentryResponse(
                success=True,
                command=request.command,
                message="✅ Configuration Sentry parsée avec succès",
                config=info
            )

        if request.command == 'list_projects':
            projects = self._list_projects(org, request.token, request.sentry_url)
            return SentryResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(projects)} projet(s) dans '{org}'",
                projects=projects
            )
            
        elif request.command == 'list_repos':
            repos = self._list_repos(org, request.token, request.sentry_url)
            return SentryResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(repos)} dépôt(s) liés à '{org}'",
                repos=repos
            )
            
        elif request.command == 'get_project':
            if not request.project:
                raise ToolExecutionError("project requis pour get_project")
            project_info = self._get_project(org, request.project, request.token, request.sentry_url)
            return SentryResponse(
                success=True,
                command=request.command,
                message=f"✅ Projet {project_info.slug}",
                projects=[project_info]
            )
        
        elif request.command == 'list_issues':
            issues = self._list_issues(
                org, request.project, request.query,
                request.token, request.sentry_url, request.limit
            )
            return SentryResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(issues)} issue(s) trouvée(s)",
                issues=issues
            )
        
        elif request.command == 'get_issue':
            if not request.issue_id:
                raise ToolExecutionError("issue_id requis pour get_issue")
            issue = self._get_issue(request.issue_id, token, request.sentry_url, org)
            return SentryResponse(
                success=True,
                command=request.command,
                message=f"✅ Issue {issue.short_id}: {issue.title[:50]}",
                issue=issue
            )
        
        elif request.command == 'issue_events':
            if not request.issue_id:
                raise ToolExecutionError("issue_id requis pour issue_events")
            events = self._get_issue_events(
                request.issue_id, token, request.sentry_url, request.limit, org
            )
            return SentryResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(events)} événement(s) avec stacktrace",
                events=events
            )
        
        elif request.command == 'issue_tags':
            if not request.issue_id:
                raise ToolExecutionError("issue_id requis pour issue_tags")
            tags = self._get_issue_tags(request.issue_id, token, request.sentry_url, org)
            return SentryResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(tags)} tag(s) analysé(s)",
                tags=tags
            )
        
        elif request.command == 'project_stats':
            if not request.project:
                raise ToolExecutionError("project requis pour project_stats")
            stats = self._get_project_stats(
                org, request.project, request.time_range,
                request.token, request.sentry_url
            )
            return SentryResponse(
                success=True,
                command=request.command,
                message=f"✅ Statistiques de '{request.project}'",
                stats=stats
            )
        
        elif request.command == 'list_releases':
            releases = self._list_releases(
                org, request.project, request.token, request.sentry_url, request.limit
            )
            return SentryResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(releases)} release(s)",
                releases=releases
            )
        
        else:
            raise ToolExecutionError(f"Commande inconnue: {request.command}")
