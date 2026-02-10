"""
Pull Request Commands for GitHub Operations.

Handles PR listing, details, files, comments, and creation.
"""
from typing import Optional, List
from pydantic import BaseModel
from . import GitHubClient


class PRInfo(BaseModel):
	"""Pull request information."""
	number: int
	title: str
	state: str
	html_url: str
	user: str
	base_branch: str
	head_branch: str
	created_at: str
	updated_at: str
	labels: List[str] = []
	draft: bool = False


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
	patch: Optional[str] = None


class BranchInfo(BaseModel):
	"""Branch information."""
	name: str
	commit: str
	protected: bool = False


class FileChange(BaseModel):
	"""File change in a PR."""
	filename: str
	status: str
	additions: int
	deletions: int
	changes: int = 0
	patch: Optional[str] = None


class Comment(BaseModel):
	"""PR comment information."""
	id: int
	user: str
	body: str
	created_at: str
	path: Optional[str] = None
	line: Optional[int] = None


class PRCommands(GitHubClient):
	"""Commands for pull request operations."""

	def list_prs(self, owner: str, repo: str, state: str = 'open',
				 limit: int = 30) -> List[PRInfo]:
		"""List pull requests for a repository."""
		data = self._api_get(
			f"/repos/{owner}/{repo}/pulls",
			{"state": state, "per_page": limit, "sort": "updated", "direction": "desc"}
		)

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

	def get_pr(self, owner: str, repo: str, pr_number: int) -> PRInfo:
		"""Get pull request details."""
		data = self._api_get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
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
			labels=[l['name'] for l in data.get('labels', [])],
			draft=data.get('draft', False)
		)

	def get_pr_files(self, owner: str, repo: str, pr_number: int,
					 limit: int = 100) -> List[FileChange]:
		"""Get files changed in a pull request."""
		data = self._api_get(
			f"/repos/{owner}/{repo}/pulls/{pr_number}/files",
			{"per_page": limit}
		)

		return [FileChange(
			filename=f['filename'],
			status=f['status'],
			additions=f.get('additions', 0),
			deletions=f.get('deletions', 0),
			changes=f.get('changes', 0),
			patch=f.get('patch', '')[:2000] if f.get('patch') else None
		) for f in data[:limit]]

	def get_pr_comments(self, owner: str, repo: str, pr_number: int,
					  limit: int = 100) -> List[Comment]:
		"""Get comments on a pull request."""
		review_comments = self._api_get(
			f"/repos/{owner}/{repo}/pulls/{pr_number}/comments",
			{"per_page": limit}
		)
		issue_comments = self._api_get(
			f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
			{"per_page": limit}
		)

		all_comments = []

		for c in review_comments:
			all_comments.append(Comment(
				id=c['id'],
				user=c['user']['login'],
				body=c.get('body', ''),
				created_at=c['created_at'],
				path=c.get('path'),
				line=c.get('line')
			))

		for c in issue_comments:
			all_comments.append(Comment(
				id=c['id'],
				user=c['user']['login'],
				body=c.get('body', ''),
				created_at=c['created_at']
			))

		all_comments.sort(key=lambda x: x.created_at)
		return all_comments[:limit]

	def create_pr(self, owner: str, repo: str, title: str, head: str,
				  base: str, body: Optional[str] = None) -> PRInfo:
		"""Create a new pull request."""
		data = {
			"title": title,
			"head": head,
			"base": base,
			"body": body or ""
		}
		resp = self._api_post(f"/repos/{owner}/{repo}/pulls", data)
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
