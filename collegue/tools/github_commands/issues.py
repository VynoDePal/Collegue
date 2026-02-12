"""
Issue Commands for GitHub Operations.

Handles issue listing, details, and creation.
"""
from typing import Optional, List
from pydantic import BaseModel
from ..clients import GitHubClient


class IssueInfo(BaseModel):
	"""Issue information."""
	number: int
	title: str
	state: str
	html_url: str
	user: str
	labels: List[str] = []
	assignees: List[str] = []
	comments: int = 0
	created_at: str
	updated_at: str
	body: Optional[str] = None


class IssueCommands(GitHubClient):
	"""Commands for issue operations."""

	def list_issues(self, owner: str, repo: str, state: str = 'open',
					limit: int = 30) -> List[IssueInfo]:
		"""List issues for a repository."""
		data = self._api_get(
			f"/repos/{owner}/{repo}/issues",
			{"state": state, "per_page": limit, "sort": "updated", "direction": "desc"}
		)

		# Filter out pull requests (GitHub returns PRs as issues)
		issues = [i for i in data if 'pull_request' not in i]

		return [IssueInfo(
			number=i['number'],
			title=i['title'],
			state=i['state'],
			html_url=i['html_url'],
			user=i['user']['login'],
			labels=[l['name'] for l in i.get('labels', [])],
			created_at=i['created_at'],
			updated_at=i['updated_at'],
			body=i.get('body', '')[:500] if i.get('body') else None
		) for i in issues[:limit]]

	def get_issue(self, owner: str, repo: str, issue_number: int) -> IssueInfo:
		"""Get issue details."""
		data = self._api_get(f"/repos/{owner}/{repo}/issues/{issue_number}")
		return IssueInfo(
			number=data['number'],
			title=data['title'],
			state=data['state'],
			html_url=data['html_url'],
			user=data['user']['login'],
			labels=[l['name'] for l in data.get('labels', [])],
			created_at=data['created_at'],
			updated_at=data['updated_at'],
			body=data.get('body', '')[:2000] if data.get('body') else None
		)

	def create_issue(self, owner: str, repo: str, title: str,
					 body: Optional[str] = None) -> IssueInfo:
		"""Create a new issue."""
		data = {
			"title": title,
			"body": body or ""
		}
		resp = self._api_post(f"/repos/{owner}/{repo}/issues", data)
		return IssueInfo(
			number=resp['number'],
			title=resp['title'],
			state=resp['state'],
			html_url=resp['html_url'],
			user=resp['user']['login'],
			labels=[l['name'] for l in resp.get('labels', [])],
			created_at=resp['created_at'],
			updated_at=resp['updated_at'],
			body=resp.get('body')
		)
