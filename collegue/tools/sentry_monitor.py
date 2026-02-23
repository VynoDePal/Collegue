"""
Sentry Monitor Tool - Récupération des erreurs et monitoring depuis Sentry

Permet à Collègue de récupérer les stacktraces réelles et prioriser le refactoring.
"""
import logging
import os
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, Field, field_validator
from .base import BaseTool, ToolExecutionError
from ..core.shared import validate_sentry_command
from .clients import SentryClient
from .transformers import (
	transform_projects,
	transform_project,
	transform_issues,
	transform_issue,
	transform_sentry_events,
	transform_releases,
	transform_repos,
	transform_tags,
	transform_project_stats,
)
from ..core.auth import resolve_token, resolve_org, register_config_with_github

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


    content: Optional[str] = Field(None, description="Contenu du fichier de configuration pour parse_config")
    format: Optional[str] = Field("ini", description="Format du fichier: 'ini' (.sentryclirc) ou 'properties' (sentry.properties)")

    @field_validator('command')
    @classmethod
    def validate_command(cls, v: str) -> str:
        return validate_sentry_command(v)


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

    tool_name = "sentry_monitor"
    tool_description = "Récupère les erreurs, stacktraces et statistiques depuis Sentry pour prioriser le debugging"
    tags = {"integration", "monitoring"}
    request_model = SentryRequest
    response_model = SentryResponse
    supported_languages = []

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

    def _get_sentry_client(self, request: SentryRequest, token: Optional[str] = None, org: Optional[str] = None) -> SentryClient:
        """Create and configure SentryClient with resolved credentials."""
        return SentryClient(
            token=token or request.token,
            organization=org or request.organization,
            base_url=request.sentry_url or "https://sentry.io"
        )

    def _execute_core_logic(self, request: SentryRequest, **kwargs) -> SentryResponse:
        """Exécute la logique principale."""
        org = resolve_org(request.organization, 'SENTRY_ORG', 'x-sentry-org', 'x-collegue-sentry-org')
        token = resolve_token(request.token, 'SENTRY_AUTH_TOKEN', 'x-sentry-token', 'x-collegue-sentry-token')


        if org and HAS_CONFIG_REGISTRY:
            try:
                github_token = resolve_token(None, 'GITHUB_TOKEN', 'x-github-token', 'x-collegue-github-token')
                register_config_with_github(
                    owner=org,
                    repo=None,
                    github_token=github_token,
                    sentry_org=org,
                    sentry_token=token
                )
            except Exception:
                pass

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
            client = self._get_sentry_client(request, token=token, org=org)
            response = client.list_projects()
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to list projects")
            projects_data = response.data or []
            projects = transform_projects(projects_data)
            return SentryResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(projects)} projet(s) dans '{org}'",
                projects=projects
            )

        elif request.command == 'list_repos':
            client = self._get_sentry_client(request, token=token, org=org)
            response = client.list_repos()
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to list repos")
            repos_data = response.data or []
            repos = transform_repos(repos_data)
            return SentryResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(repos)} dépôt(s) liés à '{org}'",
                repos=repos
            )

        elif request.command == 'get_project':
            if not request.project:
                raise ToolExecutionError("project requis pour get_project")
            client = self._get_sentry_client(request, token=token, org=org)
            response = client.get_project(request.project)
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to get project")
            data = response.data or {}
            project_info = transform_project(data)
            return SentryResponse(
                success=True,
                command=request.command,
                message=f"✅ Projet {project_info.slug}",
                projects=[project_info]
            )

        elif request.command == 'list_issues':
            client = self._get_sentry_client(request, token=token, org=org)
            response = client.list_issues(
                project=request.project,
                query=request.query or "is:unresolved",
                limit=request.limit
            )
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to list issues")
            issues_data = response.data or []
            issues = transform_issues(issues_data, request.limit)
            return SentryResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(issues)} issue(s) trouvée(s)",
                issues=issues
            )

        elif request.command == 'get_issue':
            if not request.issue_id:
                raise ToolExecutionError("issue_id requis pour get_issue")
            client = self._get_sentry_client(request, token=token, org=org)
            response = client.get_issue(request.issue_id)
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to get issue")
            data = response.data or {}
            issue = transform_issue(data)
            return SentryResponse(
                success=True,
                command=request.command,
                message=f"✅ Issue {issue.short_id}: {issue.title[:50]}",
                issue=issue
            )

        elif request.command == 'issue_events':
            if not request.issue_id:
                raise ToolExecutionError("issue_id requis pour issue_events")
            client = self._get_sentry_client(request, token=token, org=org)
            response = client.get_issue_events(request.issue_id, limit=request.limit)
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to get issue events")
            events_data = response.data or []
            # Transform events data to EventInfo objects
            events = transform_sentry_events(events_data, request.limit)
            return SentryResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(events)} événement(s) avec stacktrace",
                events=events
            )

        elif request.command == 'issue_tags':
            if not request.issue_id:
                raise ToolExecutionError("issue_id requis pour issue_tags")
            client = self._get_sentry_client(request, token=token, org=org)
            response = client.get_issue_tags(request.issue_id)
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to get issue tags")
            tags_data = response.data or []
            tags = transform_tags(tags_data)
            return SentryResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(tags)} tag(s) analysé(s)",
                tags=tags
            )

        elif request.command == 'project_stats':
            if not request.project:
                raise ToolExecutionError("project requis pour project_stats")
            client = self._get_sentry_client(request, token=token, org=org)
            response = client.get_project_stats(request.project, request.time_range)
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to get project stats")
            stats_data = response.data or {}
            # Extract stats from response
            total_events = stats_data.get('total', 0)
            unresolved = stats_data.get('unresolved', 0)
            stats = transform_project_stats(stats_data, request.project)
            return SentryResponse(
                success=True,
                command=request.command,
                message=f"✅ Statistiques de '{request.project}'",
                stats=stats
            )

        elif request.command == 'list_releases':
            client = self._get_sentry_client(request, token=token, org=org)
            response = client.list_releases(project=request.project, limit=request.limit)
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to list releases")
            releases_data = response.data or []
            releases = transform_releases(releases_data, request.limit)
            return SentryResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(releases)} release(s)",
                releases=releases
            )

        else:
            raise ToolExecutionError(f"Commande inconnue: {request.command}")
