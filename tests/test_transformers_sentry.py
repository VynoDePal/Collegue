"""
Tests pour le module transformers/sentry.py
"""
import sys
sys.path.insert(0, '/home/kevyn-odjo/Documents/Collegue')

from collegue.tools.transformers.sentry import (
    transform_projects,
    transform_project,
    transform_issues,
    transform_issue,
    transform_events,
    transform_releases,
    transform_repos,
    transform_tags,
    transform_project_stats,
)


class TestTransformProjects:
    """Tests pour transform_projects."""

    def test_transform_empty_list(self):
        """Test avec liste vide."""
        result = transform_projects([])
        assert result == []

    def test_transform_single_project(self):
        """Test avec un seul projet."""
        projects_data = [{
            'id': '123',
            'slug': 'test-project',
            'name': 'Test Project',
            'platform': 'python',
            'status': 'active',
            'organization': {'slug': 'my-org'}
        }]
        result = transform_projects(projects_data)
        assert len(result) == 1
        assert result[0].id == '123'
        assert result[0].slug == 'test-project'
        assert result[0].name == 'Test Project'
        assert result[0].platform == 'python'

    def test_transform_multiple_projects(self):
        """Test avec plusieurs projets."""
        projects_data = [
            {'id': '1', 'slug': 'project-1', 'name': 'Project 1', 'platform': 'python'},
            {'id': '2', 'slug': 'project-2', 'name': 'Project 2', 'platform': 'javascript'},
        ]
        result = transform_projects(projects_data)
        assert len(result) == 2
        assert result[0].slug == 'project-1'
        assert result[1].slug == 'project-2'


class TestTransformProject:
    """Tests pour transform_project."""

    def test_transform_single_project(self):
        """Test transformation d'un projet unique."""
        project_data = {
            'id': '123',
            'slug': 'test-project',
            'name': 'Test Project',
            'platform': 'python',
            'status': 'active',
            'options': {'sentry:release': '1.0.0'},
            'organization': {'slug': 'my-org'}
        }
        result = transform_project(project_data)
        assert result.id == '123'
        assert result.slug == 'test-project'
        assert result.options == {'sentry:release': '1.0.0'}


class TestTransformIssues:
    """Tests pour transform_issues."""

    def test_transform_empty_list(self):
        """Test avec liste vide."""
        result = transform_issues([])
        assert result == []

    def test_transform_single_issue(self):
        """Test avec une seule issue."""
        issues_data = [{
            'id': '456',
            'shortId': 'TEST-123',
            'title': 'Test Error',
            'culprit': '/api/test',
            'level': 'error',
            'status': 'unresolved',
            'count': 10,
            'userCount': 5,
            'firstSeen': '2024-01-01T00:00:00Z',
            'lastSeen': '2024-01-02T00:00:00Z',
            'permalink': 'https://sentry.io/issues/456',
            'isUnhandled': True,
            'type': 'error'
        }]
        result = transform_issues(issues_data)
        assert len(result) == 1
        assert result[0].id == '456'
        assert result[0].short_id == 'TEST-123'
        assert result[0].title == 'Test Error'
        assert result[0].is_unhandled is True

    def test_transform_with_limit(self):
        """Test avec limite."""
        issues_data = [
            {'id': str(i), 'shortId': f'TEST-{i}', 'title': f'Issue {i}',
             'firstSeen': '2024-01-01T00:00:00Z', 'lastSeen': '2024-01-02T00:00:00Z',
             'permalink': f'https://sentry.io/issues/{i}'}
            for i in range(10)
        ]
        result = transform_issues(issues_data, limit=5)
        assert len(result) == 5


class TestTransformIssue:
    """Tests pour transform_issue."""

    def test_transform_single_issue(self):
        """Test transformation d'une issue unique."""
        issue_data = {
            'id': '456',
            'shortId': 'TEST-123',
            'title': 'Test Error',
            'culprit': '/api/test',
            'level': 'error',
            'status': 'unresolved',
            'count': 10,
            'userCount': 5,
            'firstSeen': '2024-01-01T00:00:00Z',
            'lastSeen': '2024-01-02T00:00:00Z',
            'permalink': 'https://sentry.io/issues/456',
            'isUnhandled': True,
            'type': 'error'
        }
        result = transform_issue(issue_data)
        assert result.id == '456'
        assert result.short_id == 'TEST-123'
        assert result.level == 'error'
        assert result.status == 'unresolved'


class TestTransformEvents:
    """Tests pour transform_events."""

    def test_transform_empty_list(self):
        """Test avec liste vide."""
        result = transform_events([])
        assert result == []

    def test_transform_single_event(self):
        """Test avec un seul événement."""
        events_data = [{
            'id': '789',
            'eventID': 'abc123',
            'title': 'Exception',
            'message': 'Something went wrong',
            'culprit': '/api/test',
            'level': 'error',
            'dateCreated': '2024-01-01T00:00:00Z',
            'tags': [{'key': 'environment', 'value': 'production'}],
            'user': {'id': 'user123', 'email': 'test@example.com'},
            'entries': []
        }]
        result = transform_events(events_data)
        assert len(result) == 1
        assert result[0].id == '789'
        assert result[0].event_id == 'abc123'
        assert result[0].level == 'error'


class TestTransformReleases:
    """Tests pour transform_releases."""

    def test_transform_empty_list(self):
        """Test avec liste vide."""
        result = transform_releases([])
        assert result == []

    def test_transform_single_release(self):
        """Test avec une seule release."""
        releases_data = [{
            'version': '1.0.0',
            'shortVersion': '1.0.0',
            'dateCreated': '2024-01-01T00:00:00Z',
            'firstEvent': '2024-01-01T01:00:00Z',
            'lastEvent': '2024-01-02T00:00:00Z',
            'newGroups': 5,
            'url': 'https://sentry.io/releases/1.0.0'
        }]
        result = transform_releases(releases_data)
        assert len(result) == 1
        assert result[0].version == '1.0.0'
        assert result[0].new_groups == 5


class TestTransformRepos:
    """Tests pour transform_repos."""

    def test_transform_empty_list(self):
        """Test avec liste vide."""
        result = transform_repos([])
        assert result == []

    def test_transform_single_repo(self):
        """Test avec un seul repo."""
        repos_data = [{
            'id': 'repo123',
            'name': 'my-org/my-repo',
            'provider': {'id': 'github'},
            'url': 'https://github.com/my-org/my-repo',
            'status': 'active'
        }]
        result = transform_repos(repos_data)
        assert len(result) == 1
        assert result[0].id == 'repo123'
        assert result[0].name == 'my-org/my-repo'
        assert result[0].provider == 'github'


class TestTransformTags:
    """Tests pour transform_tags."""

    def test_transform_empty_list(self):
        """Test avec liste vide."""
        result = transform_tags([])
        assert result == []

    def test_transform_single_tag(self):
        """Test avec un seul tag."""
        tags_data = [{
            'key': 'browser',
            'name': 'Browser',
            'topValues': [
                {'value': 'Chrome', 'count': 100},
                {'value': 'Firefox', 'count': 50}
            ]
        }]
        result = transform_tags(tags_data)
        assert len(result) == 1
        assert result[0].key == 'browser'
        assert result[0].name == 'Browser'
        assert len(result[0].values) == 2


class TestTransformProjectStats:
    """Tests pour transform_project_stats."""

    def test_transform_stats(self):
        """Test transformation des stats."""
        stats_data = {
            'total': 150,
            'unresolved': 10,
            '24h': 25
        }
        result = transform_project_stats(stats_data, 'my-project')
        assert result.project == 'my-project'
        assert result.total_events == 150
        assert result.unresolved_issues == 10
        assert result.events_24h == 25


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])
