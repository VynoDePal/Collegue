import pytest
import asyncio
from unittest.mock import patch, MagicMock
from typing import Optional
from pydantic import BaseModel

from collegue.tools.base import BaseTool
from collegue.tools.github_ops import GitHubOpsTool, GitHubRequest, GitHubResponse

class DummyPromptEngine:
    pass

class DummyParser:
    pass

class DummyContextManager:
    pass

class DummyContext:
    def __init__(self, lifespan_context=None):
        self.lifespan_context = lifespan_context or {}
    
    async def report_progress(self, progress, total):
        pass

class DummyRequest(BaseModel):
    pass

class DummyResponse(BaseModel):
    pass

class MyTestTool(BaseTool):
    tool_name = "my_test"
    request_model = DummyRequest
    response_model = DummyResponse

    def _execute_core_logic(self, request, **kwargs):
        return DummyResponse()

@pytest.mark.asyncio
async def test_basetool_dependency_injection():
    # Verify that lifespan_context dependencies are injected properly
    prompt_engine = DummyPromptEngine()
    parser = DummyParser()
    context_manager = DummyContextManager()

    lifespan_context = {
        'prompt_engine': prompt_engine,
        'parser': parser,
        'context_manager': context_manager
    }

    ctx = DummyContext(lifespan_context=lifespan_context)
    
    # FastMCP uses lc.get() and passes via kwargs
    kwargs = {
        "parser": lifespan_context.get('parser'),
        "context_manager": lifespan_context.get('context_manager'),
        "prompt_engine": lifespan_context.get('prompt_engine'),
        "ctx": ctx,
    }

    tool = MyTestTool({})
    # Initially None
    assert tool.prompt_engine is None

    # Execute
    request = DummyRequest()
    await tool.execute_async(request, **kwargs)

    # After execution, dependencies should be set
    assert tool.prompt_engine is prompt_engine
    assert getattr(tool, 'parser', None) is parser
    assert tool.context_manager is context_manager

@pytest.mark.asyncio
async def test_github_ops_tool_stateless_initialization():
    # We want to ensure that command initializations are not polluting self
    tool = GitHubOpsTool({})
    
    # The tool shouldn't have _repos, _prs etc attributes anymore
    assert not hasattr(tool, '_repos')
    assert not hasattr(tool, '_prs')
    assert not hasattr(tool, '_issues')
    
    # Mock resolve_token and the commands so it doesn't make real API calls
    with patch('collegue.tools.github_ops.resolve_token', return_value='fake_token'), \
         patch('collegue.tools.github_ops.RepoCommands') as MockRepoCmds, \
         patch('collegue.tools.github_ops.PRCommands'), \
         patch('collegue.tools.github_ops.IssueCommands'), \
         patch('collegue.tools.github_ops.BranchCommands'), \
         patch('collegue.tools.github_ops.FileCommands'), \
         patch('collegue.tools.github_ops.WorkflowCommands'), \
         patch('collegue.tools.github_ops.SearchCommands'):
         
        mock_repos_cmd = MagicMock()
        mock_repos_cmd.list_repos.return_value = []
        MockRepoCmds.return_value = mock_repos_cmd
        
        # Create request
        req = GitHubRequest(command='list_repos', owner='fakeowner', limit=10)
        
        await tool.execute_async(req, ctx=DummyContext())
        
        # Ensure they were not attached to tool
        assert not hasattr(tool, '_repos')
        assert not hasattr(tool, '_prs')
        assert not hasattr(tool, '_issues')
        
        # Ensure list_repos was called
        mock_repos_cmd.list_repos.assert_called_once_with('fakeowner', 10)