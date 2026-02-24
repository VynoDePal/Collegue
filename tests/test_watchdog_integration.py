import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

from collegue.autonomous.watchdog import AutoFixer
from collegue.tools.sentry_monitor import SentryResponse, ProjectInfo, IssueInfo, RepoInfo as SentryRepoInfo, EventInfo
from collegue.tools.github_ops import GitHubResponse, PRInfo
from collegue.tools.github_commands.search import SearchResult

@pytest.fixture
def mock_sentry():
    sentry_mock = MagicMock()
    # Mocking list_projects
    sentry_mock._execute_core_logic.side_effect = [
        # 1. list_projects
        SentryResponse(success=True, command="list_projects", message="OK", projects=[ProjectInfo(id="p1", name="project-1", slug="project-1")]),
        # 2. list_repos
        SentryResponse(success=True, command="list_repos", message="OK", repos=[SentryRepoInfo(id="r1", name="owner/repo", provider="github")]),
        # 3. get_project_issues
        SentryResponse(
            success=True, 
            command="list_issues",
            message="OK",
            issues=[
                IssueInfo(
                    id="i1", short_id="PROJ-1", title="ZeroDivisionError", type="error", level="error",
                    status="unresolved", user_count=1, permalink="http",
                    count=1, first_seen="2024-01-01", last_seen="2024-01-01",
                    metadata={"value": "division by zero", "filename": "main.py"}
                )
            ]
        ),
        # 4. get_issue_stacktrace
        SentryResponse(
            success=True,
            command="issue_events",
            message="OK",
            events=[
                EventInfo(
                    event_id="e1", title="ZeroDivisionError", timestamp="2024-01-01T00:00:00Z",
                    stacktrace='''File "main.py", line 10, in divide
    return a / b
ZeroDivisionError: division by zero'''
                )
            ]
        )
    ]
    return sentry_mock

@pytest.fixture
def mock_github():
    github_mock = MagicMock()
    github_mock._execute_core_logic.side_effect = [
        # 1. get_file (called by ContextPackBuilder)
        GitHubResponse(
            success=True, 
            command="get_file", 
            message="OK", 
            content="def divide(a, b):\n    return a / b\n"
        ),
        # 2. create_branch
        GitHubResponse(
            success=True,
            command="create_branch",
            message="Branch created"
        ),
        # 3. update_file
        GitHubResponse(
            success=True,
            command="update_file",
            message="File updated"
        ),
        # 4. create_pr
        GitHubResponse(
            success=True, 
            command="create_pr", 
            message="PR Created: https://github.com/owner/repo/pull/1",
            pr=PRInfo(id=1, number=1, title="fix", state="open", html_url="https://github.com/owner/repo/pull/1", user="bot", base_branch="main", head_branch="fix/1", created_at="2024-01-01T00:00:00Z", updated_at="2024-01-01T00:00:00Z")
        )
    ]
    return github_mock

@pytest.mark.asyncio
async def test_watchdog_integration_cycle(mock_sentry, mock_github):
    # Setup AutoFixer with mocked tools
    auto_fixer = AutoFixer()
    auto_fixer.sentry = mock_sentry
    auto_fixer.github = mock_github
    
    # Mock the LLM output to provide a simple patch
    with patch("collegue.autonomous.watchdog.generate_text", new_callable=AsyncMock) as mock_llm:
        mock_response = MagicMock()
        mock_response.text = '''```json
{
  "explanation": "Add zero division check.",
  "patches": [
    {
      "filepath": "main.py",
      "search": "def divide(a, b):\\n    return a / b",
      "replace": "def divide(a, b):\\n    if b == 0:\\n        return 0\\n    return a / b"
    }
  ]
}
```'''
        mock_response.annotations = []
        mock_llm.return_value = mock_response
        # Override tokens for testing
        with patch.object(auto_fixer, '_get_sentry_org', return_value='test-org'):
            with patch.object(auto_fixer, '_get_sentry_token', return_value='sentry-token'):
                with patch.object(auto_fixer, '_get_github_token', return_value='gh-token'):
                    with patch.object(auto_fixer, '_get_github_owner', return_value='owner'):
                        with patch.object(auto_fixer, '_get_github_repo', return_value='repo'):
                            
                            # Run one cycle
                            await auto_fixer.run_once()
                            
                            # Assert PR was created
                            assert mock_github._execute_core_logic.call_count == 4
                            pr_call = mock_github._execute_core_logic.call_args_list[3][0][0]
                            assert pr_call.command == "create_pr"
                            assert pr_call.head.startswith("fix/sentry-PROJ-1")
