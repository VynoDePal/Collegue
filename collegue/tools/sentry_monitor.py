# collegue/tools/sentry_monitor.py

import requests
from pydantic import BaseModel
from .base import BaseTool

class SentryIssue(BaseModel):
    id: str
    short_id: str
    title: str
    # add other fields as needed

class SentryResponse(BaseModel):
    success: bool
    data: list[SentryIssue] = []

class SentryMonitor(BaseTool):
    def __init__(self, token: str, base_url: str, project_id: str):
        self.token = token
        self.base_url = base_url
        self.project_id = project_id  # Assuming this is a string initially

    def _api_get(self, endpoint: str, params=None):
        url = f"{self.base_url}{endpoint}"
        headers = {"Authorization": f"Bearer {self.token}"}
        if params is None:
            params = {}
        # Correctif: Convertir project_id en entier si présent dans params
        if 'project' in params:
            params['project'] = int(params['project'])
        response = requests.get(url, headers=headers, params=params)
        if response.status_code != 200:
            raise ToolExecutionError(f"Erreur API Sentry {response.status_code}: {response.text}")
        return response.json()

    def _list_issues(self):
        endpoint = "/api/0/projects/{org}/{project}/issues/"  # Example, adjust as needed
        params = {'project': self.project_id}  # This will now be converted to int in _api_get
        issues_data = self._api_get(endpoint, params)
        issues = [SentryIssue(id=i['id'], short_id=i['shortId'], title=i['title']) for i in issues_data]
        return issues

    def _execute_core_logic(self, **kwargs):
        # Assuming this calls _list_issues
        issues = self._list_issues()
        return SentryResponse(success=True, data=issues)

# Add other methods as needed to complete the file