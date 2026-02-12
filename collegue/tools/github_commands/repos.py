"""
Repository Commands for GitHub Operations.

Handles repository listing and details.
"""
from typing import Optional, List
from pydantic import BaseModel
from ..clients import GitHubClient


class RepoInfo(BaseModel):
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


class RepoCommands(GitHubClient):
	def list_repos(self, owner: Optional[str], limit: int = 30) -> List[RepoInfo]:
		if owner:
			endpoint = f"/users/{owner}/repos"
		else:
			endpoint = "/user/repos"

		data = self._api_get(endpoint, {"per_page": limit, "sort": "updated"})

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

	def get_repo(self, owner: str, repo: str) -> RepoInfo:
		data = self._api_get(f"/repos/{owner}/{repo}")
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
