"""
Tests pour le module auth.py
"""
import sys
import os
sys.path.insert(0, '/home/kevyn-odjo/Documents/Collegue')

from collegue.tools.auth import (
    get_token_from_http_headers,
    get_org_from_http_headers,
    resolve_token,
    resolve_org,
)


class TestGetTokenFromHttpHeaders:
    """Tests pour get_token_from_http_headers."""

    def test_no_headers_available(self):
        """Test quand get_http_headers n'est pas disponible."""
        result = get_token_from_http_headers('x-token')
        assert result is None

    def test_no_matching_header(self):
        """Test quand aucun header ne correspond."""
        result = get_token_from_http_headers('x-token', 'x-other')
        assert result is None


class TestResolveToken:
    """Tests pour resolve_token."""

    def test_request_token_priority(self):
        """Test que request_token a la priorité."""
        result = resolve_token('request-token', 'ENV_VAR', 'x-header')
        assert result == 'request-token'

    def test_env_var_fallback(self):
        """Test fallback sur variable d'environnement."""
        os.environ['TEST_TOKEN'] = 'env-token'
        result = resolve_token(None, 'TEST_TOKEN', 'x-header')
        assert result == 'env-token'
        del os.environ['TEST_TOKEN']

    def test_no_token_found(self):
        """Test quand aucun token n'est trouvé."""
        result = resolve_token(None, 'NONEXISTENT_VAR', 'x-header')
        assert result is None


class TestResolveOrg:
    """Tests pour resolve_org."""

    def test_request_org_priority(self):
        """Test que request_org a la priorité."""
        result = resolve_org('my-org', 'SENTRY_ORG', 'x-sentry-org')
        assert result == 'my-org'

    def test_env_var_fallback(self):
        """Test fallback sur variable d'environnement."""
        os.environ['SENTRY_ORG'] = 'env-org'
        result = resolve_org(None, 'SENTRY_ORG', 'x-sentry-org')
        assert result == 'env-org'
        del os.environ['SENTRY_ORG']

    def test_no_org_found(self):
        """Test quand aucune org n'est trouvée."""
        result = resolve_org(None, 'NONEXISTENT_ORG', 'x-sentry-org')
        assert result is None


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])
