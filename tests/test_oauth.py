"""
Test OAuth - Tests pour l'authentification OAuth du MCP Collègue
"""
import unittest
from unittest.mock import patch, MagicMock
import sys
import os


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from collegue.config import settings
from collegue.core.auth import OAuthManager


class TestOAuthManager(unittest.TestCase):
    """Tests pour le gestionnaire d'authentification OAuth."""

    def setUp(self):
        settings.OAUTH_ENABLED = False
        settings.OAUTH_JWKS_URI = None
        settings.OAUTH_PUBLIC_KEY = None

    def test_oauth_disabled(self):
        """Test quand l'authentification OAuth est désactivée."""
        oauth_manager = OAuthManager()
        self.assertFalse(oauth_manager.is_enabled())
        self.assertIsNone(oauth_manager.get_auth_provider())

    @patch('collegue.core.auth.BearerAuthProvider')
    def test_oauth_with_jwks(self, mock_provider):
        """Test quand l'authentification OAuth est configurée avec JWKS."""
        settings.OAUTH_ENABLED = True
        settings.OAUTH_JWKS_URI = "https://example.com/.well-known/jwks.json"
        settings.OAUTH_ISSUER = "https://example.com/"
        settings.OAUTH_AUDIENCE = "test-app"
        settings.OAUTH_REQUIRED_SCOPES = ["read", "write"]

        mock_instance = MagicMock()
        mock_provider.return_value = mock_instance

        oauth_manager = OAuthManager()

        self.assertTrue(oauth_manager.is_enabled())
        self.assertIsNotNone(oauth_manager.get_auth_provider())

        mock_provider.assert_called_once_with(
            jwks_uri="https://example.com/.well-known/jwks.json",
            issuer="https://example.com/",
            algorithm="RS256",
            audience="test-app",
            required_scopes=["read", "write"]
        )

    @patch('collegue.core.auth.BearerAuthProvider')
    def test_oauth_with_public_key(self, mock_provider):
        """Test quand l'authentification OAuth est configurée avec une clé publique."""
        settings.OAUTH_ENABLED = True
        settings.OAUTH_PUBLIC_KEY = "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----"
        settings.OAUTH_ISSUER = "https://example.com/"
        settings.OAUTH_AUDIENCE = "test-app"
        settings.OAUTH_REQUIRED_SCOPES = ["read"]

        mock_instance = MagicMock()
        mock_provider.return_value = mock_instance

        oauth_manager = OAuthManager()

        self.assertTrue(oauth_manager.is_enabled())
        self.assertIsNotNone(oauth_manager.get_auth_provider())

        mock_provider.assert_called_once_with(
            public_key="-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----",
            issuer="https://example.com/",
            algorithm="RS256",
            audience="test-app",
            required_scopes=["read"]
        )


class TestOAuthConfig(unittest.TestCase):
    """Tests pour la configuration OAuth."""

    def test_oauth_settings_defaults(self):
        """Test des valeurs par défaut des paramètres OAuth."""
        self.assertFalse(settings.OAUTH_ENABLED)
        self.assertIsNone(settings.OAUTH_JWKS_URI)
        self.assertIsNone(settings.OAUTH_ISSUER)
        self.assertEqual(settings.OAUTH_ALGORITHM, "RS256")
        self.assertIsNone(settings.OAUTH_AUDIENCE)
        self.assertEqual(settings.OAUTH_REQUIRED_SCOPES, [])
        self.assertIsNone(settings.OAUTH_PUBLIC_KEY)


if __name__ == '__main__':
    unittest.main()
