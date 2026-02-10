"""
Branch Commands for GitHub Operations.

Handles branch listing, creation, and commit operations.
"""
from typing import Optional, List
from pydantic import BaseModel
from . import GitHubClient
from ..base import ToolExecutionError


class BranchInfo(BaseModel):
	"""Branch information."""
	name: str
	commit_sha: str
	protected: bool = False


class CommitInfo(BaseModel):
	"""Commit information."""
	sha: str
	message: str
	author: str
	date: str
	html_url: str


class BranchCommands(GitHubClient):
	"""Commands for branch operations."""

	def list_branches(self, owner: str, repo: str, limit: int = 30) -> List[BranchInfo]:
		"""List branches for a repository."""
		data = self._api_get(
			f"/repos/{owner}/{repo}/branches",
			{"per_page": limit}
		)
		return [BranchInfo(
			name=b['name'],
			commit_sha=b['commit']['sha'],
			protected=b.get('protected', False)
		) for b in data[:limit]]

	def list_commits(self, owner: str, repo: str, branch: Optional[str] = None,
				   limit: int = 30) -> List[CommitInfo]:
		"""List commits for a repository or branch."""
		params = {"per_page": limit}
		if branch:
			params["sha"] = branch

		data = self._api_get(f"/repos/{owner}/{repo}/commits", params)
		return [CommitInfo(
			sha=c['sha'],
			message=c['commit']['message'],
			author=c['commit']['author']['name'],
			date=c['commit']['author']['date'],
			html_url=c['html_url']
		) for c in data[:limit]]

	def _get_branch_sha(self, owner: str, repo: str, branch: str) -> str:
		"""Get SHA of a branch."""
		try:
			resp = self._api_get(f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
			return resp['object']['sha']
		except ToolExecutionError:
			raise ToolExecutionError(f"Source branch '{branch}' not found")

	def create_branch(self, owner: str, repo: str, branch: str,
					  from_branch: Optional[str] = None) -> BranchInfo:
		"""Create a new branch."""
		if not from_branch:
			# Get default branch
			repo_info = self._api_get(f"/repos/{owner}/{repo}")
			from_branch = repo_info.get('default_branch', 'main')

		sha = self._get_branch_sha(owner, repo, from_branch)

		data = {
			"ref": f"refs/heads/{branch}",
			"sha": sha
		}
		resp = self._api_post(f"/repos/{owner}/{repo}/git/refs", data)
		return BranchInfo(
			name=branch,
			commit_sha=resp['object']['sha'],
			protected=False
		)
