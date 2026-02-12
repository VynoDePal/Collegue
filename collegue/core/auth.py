"""
Auth utilities for Collegue tools.

Centralise les fonctions d'authentification et de résolution de tokens
pour éviter la duplication entre sentry_monitor.py et github_ops.py.
"""
import os
from typing import Optional, Dict, Any


def get_token_from_http_headers(*header_names: str) -> Optional[str]:
    try:
        from fastmcp.server.dependencies import get_http_headers
    except Exception:
        return None
    
    if get_http_headers is None:
        return None
    
    headers = get_http_headers() or {}
    for name in header_names:
        value = headers.get(name)
        if value:
            return value
    return None


def get_org_from_http_headers(*header_names: str) -> Optional[str]:
    return get_token_from_http_headers(*header_names)


def resolve_token(
    request_token: Optional[str],
    env_var: str,
    *header_names: str
) -> Optional[str]:
    return (
        request_token
        or os.environ.get(env_var)
        or get_token_from_http_headers(*header_names)
    )


def resolve_org(
    request_org: Optional[str],
    env_var: str,
    *header_names: str
) -> Optional[str]:
    return resolve_token(request_org, env_var, *header_names)


def resolve_postgres_url(
	request_url: Optional[str],
	*header_names: str,
) -> Optional[str]:
	return (
		request_url
		or os.environ.get('POSTGRES_URL')
		or os.environ.get('DATABASE_URL')
		or get_token_from_http_headers(*header_names)
	)


def register_config_with_github(
    owner: str,
    repo: Optional[str],
    github_token: Optional[str],
    sentry_org: Optional[str] = None,
    sentry_token: Optional[str] = None
) -> None:
    try:
        from collegue.autonomous.config_registry import get_config_registry
    except ImportError:
        return
    
    try:
        from fastmcp.server.dependencies import get_http_headers
    except Exception:
        get_http_headers = None
    
    if sentry_org is None and get_http_headers:
        headers = get_http_headers() or {}
        sentry_org = headers.get('x-sentry-org') or headers.get('x-collegue-sentry-org')
    
    if sentry_org:
        get_config_registry().register(
            sentry_org=sentry_org,
            sentry_token=sentry_token,
            github_token=github_token,
            github_owner=owner,
            github_repo=repo
        )
