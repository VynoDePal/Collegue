# collegue/tools/sentry_monitor.py

import requests
from typing import List, Dict, Any
from collegue.tools.base import ToolExecutionError

class SentryResponse:
    def __init__(self, success: bool, data: Any = None, error: str = None):
        self.success = success
        self.data = data
        self.error = error

class SentryMonitor:
    def __init__(self, auth_token: str, organization_slug: str):
        self.auth_token = auth_token
        self.organization_slug = organization_slug
        self.base_url = 'https://sentry.io/api/0'
    
    def _api_get(self, endpoint: str) -> Dict:
        headers = {
            'Authorization': f'Bearer {self.auth_token}',
            'Content-Type': 'application/json'
        }
        try:
            response = requests.get(f'{self.base_url}{endpoint}', headers=headers)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            raise ToolExecutionError(f"Erreur réseau Sentry: {e}")
    
    def _list_issues(self, project: str, limit: int = 100) -> List[Dict]:
        # Fix: Convert project to int to ensure it's numeric
        try:
            project_id = int(project)
        except ValueError:
            raise ToolExecutionError("Project must be a numeric ID")
        
        issues = self._api_get(f'/projects/{self.organization_slug}/{project_id}/issues/?limit={limit}')
        return [
            {
                'id': i['id'],
                'short_id': i['shortId'],
                'title': i['title'],
                # Add other fields as needed
            }
            for i in issues
        ]
    
    def _execute_core_logic(self, project: str) -> SentryResponse:
        try:
            issues = self._list_issues(project)
            return SentryResponse(
                success=True,
                data={'issues': issues}
            )
        except ToolExecutionError as e:
            return SentryResponse(
                success=False,
                error=str(e)
            )
    
    # Other methods can be added here if needed