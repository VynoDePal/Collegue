"""
Sentry API client for error tracking operations.

Provides access to Sentry API for listing projects, issues, and releases.
"""
import os
from typing import Any, Dict, List, Optional

from .base import APIClient, APIResponse, APIError


class SentryClient(APIClient):
    """
    Client for Sentry API operations.
    
    Supports both SaaS (sentry.io) and self-hosted Sentry instances.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        organization: Optional[str] = None,
        base_url: str = "https://sentry.io",
        **kwargs
    ):
        # Get token from env if not provided
        auth_token = token or os.environ.get('SENTRY_AUTH_TOKEN')
        if not auth_token:
            raise APIError("Sentry token required. Provide token or set SENTRY_AUTH_TOKEN env var.")

        self.organization = organization or os.environ.get('SENTRY_ORG', '')

        super().__init__(
            base_url=f"{base_url}/api/0",
            auth_token=auth_token,
            **kwargs
        )

    def _get_auth_header(self) -> Dict[str, str]:
        """Sentry uses Bearer token auth."""
        return {"Authorization": f"Bearer {self.auth_token}"}

    def list_projects(self) -> APIResponse:
        """List all projects for the organization."""
        if not self.organization:
            return APIResponse(
                success=False,
                error_message="Organization slug required"
            )

        endpoint = f"organizations/{self.organization}/projects/"
        return self._get(endpoint)

    def list_issues(
        self,
        project: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 25
    ) -> APIResponse:
        """
        List issues for organization or specific project.
        
        Args:
            project: Optional project slug to filter
            query: Sentry search query
            limit: Maximum results to return
        """
        if not self.organization:
            return APIResponse(
                success=False,
                error_message="Organization slug required"
            )

        if project:
            endpoint = f"projects/{self.organization}/{project}/issues/"
        else:
            endpoint = f"organizations/{self.organization}/issues/"

        params = {"limit": limit}
        if query:
            params["query"] = query

        return self._get(endpoint, params=params)

    def get_issue(self, issue_id: str) -> APIResponse:
        """Get details for a specific issue."""
        endpoint = f"issues/{issue_id}/"
        return self._get(endpoint)

    def get_issue_events(self, issue_id: str) -> APIResponse:
        """Get events (stacktraces) for an issue."""
        endpoint = f"issues/{issue_id}/events/"
        return self._get(endpoint)

    def list_releases(
        self,
        project: Optional[str] = None,
        limit: int = 25
    ) -> APIResponse:
        """List releases for organization or project."""
        if not self.organization:
            return APIResponse(
                success=False,
                error_message="Organization slug required"
            )

        if project:
            endpoint = f"projects/{self.organization}/{project}/releases/"
        else:
            endpoint = f"organizations/{self.organization}/releases/"

        return self._get(endpoint, params={"limit": limit})

    def _get(self, endpoint: str, params: Optional[Dict] = None) -> APIResponse:
        """Internal GET request helper."""
        try:
            import requests
            
            url = self._build_url(endpoint)
            headers = self._build_headers()

            def do_request():
                response = requests.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=self.timeout
                )
                response.raise_for_status()
                return response

            response = self._execute_with_retry(do_request, f"GET {endpoint}")
            return self.handle_response(response, endpoint)

        except Exception as e:
            return APIResponse(
                success=False,
                error_message=str(e)
            )

    def get_issue_tags(self, issue_id: str) -> APIResponse:
        """Get tag distribution for an issue."""
        endpoint = f"issues/{issue_id}/tags/"
        return self._get(endpoint)

    def get_project(self, project_slug: str) -> APIResponse:
        """Get details for a specific project."""
        if not self.organization:
            return APIResponse(
                success=False,
                error_message="Organization slug required"
            )
        endpoint = f"projects/{self.organization}/{project_slug}/"
        return self._get(endpoint)

    def list_repos(self) -> APIResponse:
        """List repositories linked to the organization."""
        if not self.organization:
            return APIResponse(
                success=False,
                error_message="Organization slug required"
            )
        endpoint = f"organizations/{self.organization}/repos/"
        return self._get(endpoint)

    def get_project_stats(
        self,
        project: str,
        time_range: str = "24h"
    ) -> APIResponse:
        """Get statistics for a project."""
        if not self.organization:
            return APIResponse(
                success=False,
                error_message="Organization slug required"
            )

        endpoint = f"projects/{self.organization}/{project}/stats/"
        params = {"stat": "received", "resolution": time_range}

        return self._get(endpoint, params=params)
