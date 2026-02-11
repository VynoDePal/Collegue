"""
Test OAuth - Tests pour la configuration OAuth du MCP Collègue

L'authentification JWT est configurée directement dans app.py
via FastMCP(auth=JWTVerifier(...)). Les tests de OAuthManager
ont été supprimés car la classe n'existe plus.
"""
import unittest
import sys
import os


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from collegue.config import settings


class TestOAuthConfig(unittest.TestCase):
    """Tests pour la configuration OAuth."""

    def test_oauth_settings_types(self):
        """Test des types et structure des paramètres OAuth."""
        self.assertIsInstance(settings.OAUTH_ENABLED, bool)
        self.assertIsInstance(settings.OAUTH_ALGORITHM, str)
        self.assertEqual(settings.OAUTH_ALGORITHM, "RS256")
        self.assertIsInstance(settings.OAUTH_REQUIRED_SCOPES, list)


if __name__ == '__main__':
    unittest.main()
