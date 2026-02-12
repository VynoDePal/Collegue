"""
Clients package - Unified API clients for external services.

Provides reusable HTTP clients with standardized:
- Authentication handling
- Retry logic with exponential backoff
- Error handling and response parsing
- Request/response logging
"""
from .base import APIClient, APIResponse, APIError
from .sentry import SentryClient
from .kubernetes import KubernetesClient
from .postgres import PostgresClient
from .github import GitHubClient

__all__ = [
    'APIClient',
    'APIResponse', 
    'APIError',
    'SentryClient',
    'KubernetesClient',
    'PostgresClient',
    'GitHubClient',
]
