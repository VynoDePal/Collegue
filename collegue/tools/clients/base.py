"""
Base API client with retry logic and standardized error handling.

Provides a foundation for all external API clients in the tools package.
"""
import time
import logging
from abc import ABC
from dataclasses import dataclass
from typing import Any, Dict, Optional, Callable, TypeVar, Generic
from enum import Enum


class HTTPMethod(Enum):
	GET = "GET"
	POST = "POST"
	PUT = "PUT"
	DELETE = "DELETE"
	PATCH = "PATCH"


@dataclass
class APIResponse:
	success: bool
	data: Any = None
	error_message: Optional[str] = None
	status_code: int = 0
	headers: Dict[str, str] = None
	retry_count: int = 0

	def __post_init__(self):
		if self.headers is None:
			self.headers = {}


class APIError(Exception):

	def __init__(
		self,
		message: str,
		status_code: int = 0,
		response_data: Any = None,
		endpoint: str = ""
	):
		super().__init__(message)
		self.status_code = status_code
		self.response_data = response_data
		self.endpoint = endpoint


T = TypeVar('T')


class APIClient(ABC):

	def __init__(
		self,
		base_url: str,
		auth_token: Optional[str] = None,
		headers: Optional[Dict[str, str]] = None,
		max_retries: int = 3,
		retry_delay: float = 1.0,
		timeout: int = 30
	):
		self.base_url = base_url.rstrip('/')
		self.auth_token = auth_token
		self.default_headers = headers or {}
		self.max_retries = max_retries
		self.retry_delay = retry_delay
		self.timeout = timeout
		self.logger = logging.getLogger(f"clients.{self.__class__.__name__}")

	def _get_auth_header(self) -> Dict[str, str]:
		if self.auth_token:
			return {"Authorization": f"Bearer {self.auth_token}"}
		return {}

	def _build_url(self, endpoint: str) -> str:
		endpoint = endpoint.lstrip('/')
		return f"{self.base_url}/{endpoint}"

	def _build_headers(self, extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
		headers = {
			"Content-Type": "application/json",
			**self.default_headers,
			**self._get_auth_header()
		}
		if extra_headers:
			headers.update(extra_headers)
		return headers

	def _should_retry(self, error: Exception, status_code: int, retry_count: int) -> bool:
		if retry_count >= self.max_retries:
			return False

		if isinstance(error, (ConnectionError, TimeoutError)):
			return True

		try:
			import requests  # type: ignore
			if isinstance(error, requests.RequestException):
				if getattr(error, 'response', None) is None:
					return True
		except Exception:
			pass

		if status_code in (429, 500, 502, 503, 504):
			return True

		return False

	def _execute_with_retry(
		self,
		operation: Callable[[], T],
		operation_name: str = "request"
	) -> T:

		last_error = None

		for attempt in range(self.max_retries + 1):
			try:
				return operation()
			except Exception as e:
				last_error = e
				status_code = getattr(e, 'status_code', 0)
				response = getattr(e, 'response', None)
				if response is not None:
					status_code = getattr(response, 'status_code', status_code)

				if not self._should_retry(e, status_code, attempt):
					break

				delay = self.retry_delay * (2 ** attempt)
				self.logger.warning(
					f"{operation_name} failed (attempt {attempt + 1}), retrying in {delay}s: {e}"
				)
				time.sleep(delay)

		# All retries exhausted
		error_msg = f"{operation_name} failed after {self.max_retries + 1} attempts: {last_error}"
		self.logger.error(error_msg)
		raise APIError(error_msg)

	def handle_response(self, response: Any, endpoint: str) -> APIResponse:
		try:
			# Try to parse JSON
			if hasattr(response, 'json'):
				data = response.json()
			else:
				data = response

			status_code = getattr(response, 'status_code', 200)

			if 200 <= status_code < 300:
				return APIResponse(
					success=True,
					data=data,
					status_code=status_code,
					headers=getattr(response, 'headers', {})
				)
			else:
				return APIResponse(
					success=False,
					error_message=f"HTTP {status_code}",
					status_code=status_code,
					data=data
				)

		except Exception as e:
			return APIResponse(
				success=False,
				error_message=str(e),
				data=getattr(response, 'text', str(response))
			)

	def log_request(self, method: str, endpoint: str, **kwargs):
		self.logger.debug(f"{method} {endpoint}")

	def log_response(self, response: APIResponse, endpoint: str):
		status = "OK" if response.success else "FAIL"
		self.logger.debug(f"{endpoint} -> {status} ({response.status_code})")
