"""
Search Commands for GitHub Operations.

Handles code search on GitHub.
"""
from typing import Optional, List
from pydantic import BaseModel
from ..clients import GitHubClient


class SearchResult(BaseModel):
	name: str
	path: str
	repository: str
	html_url: str
	score: float


class SearchCommands(GitHubClient):
	def search_code(
		self,
		query: str,
		owner: Optional[str] = None,
		repo: Optional[str] = None,
		limit: int = 30
	) -> List[SearchResult]:
		q = query
		if owner and repo:
			q += f" repo:{owner}/{repo}"
		elif owner:
			q += f" user:{owner}"

		data = self._api_get("/search/code", {"q": q, "per_page": limit})
		items = data.get('items', [])

		return [SearchResult(
			name=i['name'],
			path=i['path'],
			repository=i['repository']['full_name'],
			html_url=i['html_url'],
			score=i.get('score', 0)
		) for i in items[:limit]]
