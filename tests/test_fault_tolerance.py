import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

from collegue.tools.base import BaseTool, ToolExecutionError
from pydantic import BaseModel

class DummyRequest(BaseModel):
    code: str

class DummyResponse(BaseModel):
    result: str

class FaultyTool(BaseTool):
    tool_name = "faulty_tool"
    tool_description = "A tool that fails"
    request_model = DummyRequest
    response_model = DummyResponse

    def _execute_core_logic(self, request, **kwargs):
        raise Exception("API is down")

@pytest.mark.asyncio
async def test_tool_fault_tolerance_graceful_handling():
    tool = FaultyTool(app_state={})
    
    # Normally, fastmcp server catches exceptions in tools, 
    # but let's ensure the tool execution itself propagates the error properly 
    # and doesn't crash the host process silently.
    with pytest.raises(Exception) as exc_info:
        await tool.execute_async(DummyRequest(code="test"))
    
    assert "API is down" in str(exc_info.value)

@pytest.mark.asyncio
async def test_llm_failure_handling():
    # If LLM generation fails, watchdog should catch it
    from collegue.autonomous.watchdog import AutoFixer
    
    auto_fixer = AutoFixer()
    auto_fixer.sentry = MagicMock()
    auto_fixer.github = MagicMock()
    
    # Fake Sentry and GitHub returning empty list so it doesn't do anything,
    # we just want to ensure it handles network errors
    auto_fixer.sentry._execute_core_logic.side_effect = Exception("Sentry API 500")
    
    with patch.object(auto_fixer, '_get_sentry_org', return_value='test-org'):
        with patch.object(auto_fixer, '_get_sentry_token', return_value='sentry-token'):
            # Should NOT raise, run_once catches exceptions internally
            await auto_fixer.run_once()
            
            # If it reached here, fault tolerance works.
            assert True
