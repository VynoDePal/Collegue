import pytest
from unittest.mock import patch, MagicMock
from collegue.tools.clients.sentry import SentryClient
from collegue.tools.clients.base import APIError

class TestSentrySecurity:
    
    @pytest.fixture
    def client(self):
        return SentryClient(token="fake", organization="org")

    def test_list_issues_path_traversal(self, client):
        """Test that list_issues rejects path traversal in project."""
        with pytest.raises(APIError, match="Invalid characters in project"):
            client.list_issues(project="../../etc/passwd")

    def test_get_issue_path_traversal(self, client):
        """Test that get_issue rejects path traversal in issue_id."""
        with pytest.raises(APIError, match="Invalid characters in issue_id"):
            client.get_issue(issue_id="../123")

    def test_get_project_path_traversal(self, client):
        """Test that get_project rejects path traversal in project_slug."""
        with pytest.raises(APIError, match="Invalid characters in project_slug"):
            client.get_project(project_slug="; drop table")

    def test_list_releases_path_traversal(self, client):
        """Test that list_releases rejects path traversal in project."""
        with pytest.raises(APIError, match="Invalid characters in project"):
            client.list_releases(project="valid/../../invalid")

    def test_valid_slugs(self, client):
        """Test that valid slugs are accepted."""
        # We mock _get to avoid making actual requests
        with patch.object(client, '_get') as mock_get:
            mock_get.return_value = MagicMock(success=True)
            
            # These should not raise exceptions
            client.list_issues(project="my-project")
            client.list_issues(project="my_project_123")
            client.get_issue(issue_id="123456")
            client.get_project(project_slug="valid-slug")
