import pytest
from unittest.mock import patch, MagicMock
from collegue.tools.sentry_monitor import SentryMonitorTool, SentryRequest
from collegue.tools.clients.base import APIResponse

@pytest.fixture
def mock_sentry_client():
    with patch('collegue.tools.sentry_monitor.SentryMonitorTool._get_sentry_client') as mock:
        client = MagicMock()
        mock.return_value = client
        yield client

def test_list_projects_success(mock_sentry_client):
    tool = SentryMonitorTool()
    
    mock_sentry_client.list_projects.return_value = APIResponse(
        success=True,
        data=[{
            'id': '12345',
            'slug': 'my-project',
            'name': 'My Project',
            'platform': 'python',
            'status': 'active',
            'organization': {'slug': 'my-org'}
        }]
    )
    
    request = SentryRequest(command='list_projects', organization='my-org')
    response = tool.execute(request)
    
    assert response.success
    assert len(response.projects) == 1
    assert response.projects[0].slug == 'my-project'

def test_get_issue_success(mock_sentry_client):
    tool = SentryMonitorTool()
    
    mock_sentry_client.get_issue.return_value = APIResponse(
        success=True,
        data={
            'id': '67890',
            'shortId': 'MYPROJ-1',
            'title': 'ZeroDivisionError',
            'level': 'error',
            'status': 'unresolved',
            'count': 5,
            'userCount': 2,
            'firstSeen': '2023-01-01T00:00:00Z',
            'lastSeen': '2023-01-02T00:00:00Z',
            'permalink': 'https://sentry.io/issue/67890',
            'isUnhandled': True,
            'type': 'error'
        }
    )
    
    request = SentryRequest(command='get_issue', issue_id='67890')
    response = tool.execute(request)
    
    assert response.success
    assert response.issue is not None
    assert response.issue.id == '67890'
    assert response.issue.title == 'ZeroDivisionError'

def test_issue_events_success(mock_sentry_client):
    tool = SentryMonitorTool()
    
    mock_sentry_client.get_issue_events.return_value = APIResponse(
        success=True,
        data=[{
            'event_id': 'abc123def',
            'title': 'ZeroDivisionError',
            'date_created': '2023-01-02T00:00:00Z',
            'entries': [
                {
                    'type': 'exception',
                    'data': {
                        'values': [
                            {
                                'stacktrace': {
                                    'frames': [
                                        {'filename': 'app.py', 'line_no': 10, 'function': 'divide'}
                                    ]
                                }
                            }
                        ]
                    }
                }
            ]
        }]
    )
    
    request = SentryRequest(command='issue_events', issue_id='67890', organization='my-org', limit=1)
    response = tool.execute(request)
    
    assert response.success
    assert len(response.events) == 1
    assert response.events[0].event_id == 'abc123def'
    assert 'app.py' in response.events[0].stacktrace
