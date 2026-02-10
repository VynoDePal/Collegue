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
	"""Standardized API response wrapper."""
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
	"""Exception for API errors with context."""

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
	"""
	Abstract base class for API clients.

	Provides:
	- Retry logic with exponential backoff
	- Authentication handling
	- Request/response logging
	- Standardized error handling
	"""

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
		"""Get authentication headers. Override in subclasses for specific auth."""
		if self.auth_token:
			return {"Authorization": f"Bearer {self.auth_token}"}
		return {}

	def _build_url(self, endpoint: str) -> str:
		"""Build full URL from endpoint."""
		endpoint = endpoint.lstrip('/')
		return f"{self.base_url}/{endpoint}"

	def _build_headers(self, extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
		"""Build complete headers for request."""
		headers = {
			"Content-Type": "application/json",
			**self.default_headers,
			**self._get_auth_header()
		}
		if extra_headers:
			headers.update(extra_headers)
		return headers

	def _should_retry(self, error: Exception, status_code: int, retry_count: int) -> bool:
		"""Determine if request should be retried."""
		if retry_count >= self.max_retries:
			return False

		# Retry on network errors
		if isinstance(error, (ConnectionError, TimeoutError)):
			return True

		# Retry on specific HTTP status codes
		if status_code in (429, 500, 502, 503, 504):
			return True

		return False

	def _execute_with_retry(
		self,
		operation: Callable[[], T],
		operation_name: str = "request"
	) -> T:
		"""
		Execute operation with retry logic.

		Args:
			operation: Callable that performs the actual request
			operation_name: Name for logging

		Returns:
			Result of operation

		Raises:
			APIError: After max retries exceeded
		"""
		last_error = None

		for attempt in range(self.max_retries + 1):
			try:
				return operation()
			except Exception as e:
				last_error = e
				status_code = getattr(e, 'status_code', 0)

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
		"""
		Handle and parse API response.

		Override in subclasses for service-specific response handling.
		"""
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
		"""Log outgoing request."""
		self.logger.debug(f"{method} {endpoint}")

	def log_response(self, response: APIResponse, endpoint: str):
		"""Log incoming response."""
		status = "OK" if response.success else "FAIL"
		self.logger.debug(f"{endpoint} -> {status} ({response.status_code})")
