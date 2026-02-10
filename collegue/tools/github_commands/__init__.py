"""
GitHub Commands Package.

Organizes GitHub API operations by domain:
- RepoCommands: Repository operations
- PRCommands: Pull request operations
- IssueCommands: Issue operations
- BranchCommands: Branch and commit operations
- FileCommands: File content operations
"""
from typing import Optional, List, Dict, Any
from ..base import ToolExecutionError


class GitHubClient:
	"""Base client for GitHub API operations."""

	API_BASE = "https://api.github.com"

	def __init__(self, token: Optional[str] = None, logger=None):
		self.token = token
		self.logger = logger

	def _get_headers(self) -> Dict[str, str]:
		"""Get request headers with authentication."""
		headers = {
			"Accept": "application/vnd.github+json",
			"X-GitHub-Api-Version": "2022-11-28"
		}
		if self.token:
			headers["Authorization"] = f"Bearer {self.token}"
		return headers

	def _api_get(self, endpoint: str, params: Optional[Dict] = None) -> Any:
		"""Make GET request to GitHub API."""
		import requests
		url = f"{self.API_BASE}{endpoint}"
		headers = self._get_headers()

		try:
			response = requests.get(url, headers=headers, params=params, timeout=30)
			if response.status_code == 404:
				raise ToolExecutionError(f"Resource not found: {endpoint}")
			elif response.status_code == 401:
				raise ToolExecutionError("Authentication failed: Invalid or expired token")
			elif response.status_code == 403:
				raise ToolExecutionError("Rate limit exceeded or insufficient permissions")
			response.raise_for_status()
			return response.json()
		except requests.RequestException as e:
			raise ToolExecutionError(f"GitHub API error: {e}")

	def _api_post(self, endpoint: str, data: Dict) -> Any:
		"""Make POST request to GitHub API."""
		import requests
		url = f"{self.API_BASE}{endpoint}"
		headers = self._get_headers()

		try:
			response = requests.post(url, headers=headers, json=data, timeout=30)
			response.raise_for_status()
			return response.json()
		except requests.RequestException as e:
			raise ToolExecutionError(f"GitHub API error: {e}")

	def _api_put(self, endpoint: str, data: Dict) -> Any:
		"""Make PUT request to GitHub API."""
		import requests
		url = f"{self.API_BASE}{endpoint}"
		headers = self._get_headers()

		try:
			response = requests.put(url, headers=headers, json=data, timeout=30)
			response.raise_for_status()
			return response.json()
		except requests.RequestException as e:
			raise ToolExecutionError(f"GitHub API error: {e}")
