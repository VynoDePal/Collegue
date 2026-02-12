"""
Workflow Commands for GitHub Operations.

Handles GitHub Actions workflow runs.
"""
from typing import Optional, List
from pydantic import BaseModel
from ..clients import GitHubClient


class WorkflowRun(BaseModel):
	id: int
	name: str
	status: str
	conclusion: Optional[str] = None
	html_url: str
	created_at: str
	head_branch: str


class WorkflowCommands(GitHubClient):
	def list_workflows(self, owner: str, repo: str, limit: int = 30) -> List[WorkflowRun]:
		data = self._api_get(f"/repos/{owner}/{repo}/actions/runs", {"per_page": limit})
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
