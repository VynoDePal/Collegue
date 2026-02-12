"""
GitHub API Client - Unified HTTP client for GitHub operations.

Provides standardized:
- Authentication handling with Bearer tokens
- Error handling with specific exception types
- Request/response logging
"""
from typing import Any, Dict, Optional

from ..base import ToolExecutionError
from .base import APIClient

try:
	import requests
	_HAS_REQUESTS = True
except ImportError:
	requests = None
	_HAS_REQUESTS = False


class GitHubClient(APIClient):

	API_BASE = "https://api.github.com"

	def __init__(
		self,
		token: Optional[str] = None,
		logger=None,
		timeout: int = 30,
		max_retries: int = 3,
		retry_delay: float = 1.0,
	):
		self.token = token

		super().__init__(
			base_url=self.API_BASE,
			auth_token=token,
			headers={
				"Accept": "application/vnd.github+json",
				"X-GitHub-Api-Version": "2022-11-28",
			},
			timeout=timeout,
			max_retries=max_retries,
			retry_delay=retry_delay,
		)

		if logger is not None:
			self.logger = logger

	def _get_auth_header(self) -> Dict[str, str]:
		if self.auth_token:
			return {"Authorization": f"Bearer {self.auth_token}"}
		return {}

	def _request_json(
		self,
		method: str,
		endpoint: str,
		*,
		params: Optional[Dict[str, Any]] = None,
		json_data: Optional[Dict[str, Any]] = None,
	) -> Any:
		if not _HAS_REQUESTS:
			raise ToolExecutionError(
				"requests non installé. Installez avec: pip install requests"
			)

		url = self._build_url(endpoint)
		headers = self._build_headers()

		def do_request():
			response = requests.request(
				method,
				url,
				headers=headers,
				params=params,
				json=json_data,
				timeout=self.timeout,
			)
			if response.status_code == 404:
				raise ToolExecutionError(f"Ressource introuvable: {endpoint}")
			if response.status_code == 401:
				raise ToolExecutionError(
					"Token GitHub invalide ou expiré, ou authentification requise"
				)
			if response.status_code == 403:
				remaining = response.headers.get('X-RateLimit-Remaining', '?')
				raise ToolExecutionError(
					f"Rate limit GitHub atteint ou permissions insuffisantes. Restant: {remaining}"
				)
			response.raise_for_status()
			return response

		try:
			response = self._execute_with_retry(do_request, f"{method} {endpoint}")
			if not getattr(response, 'content', None):
				return None
			return response.json()
		except ToolExecutionError:
			raise
		except Exception as e:
			raise ToolExecutionError(f"Erreur API GitHub: {e}")

	def _api_get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
		return self._request_json('GET', endpoint, params=params)

	def _api_post(self, endpoint: str, data: Dict[str, Any]) -> Any:
		return self._request_json('POST', endpoint, json_data=data)

	def _api_put(self, endpoint: str, data: Dict[str, Any]) -> Any:
		return self._request_json('PUT', endpoint, json_data=data)
