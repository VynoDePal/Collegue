import pytest
import sys
import json
from unittest.mock import MagicMock, AsyncMock, patch

from collegue.core.meta_orchestrator import register_meta_orchestrator, OrchestratorRequest, OrchestratorPlan, OrchestratorStep
import collegue.core.meta_orchestrator as mo

class MockContext:
    def __init__(self):
        self.lifespan_context = {}
        self.info = AsyncMock()
        self.error = AsyncMock()
        self.warning = AsyncMock()
        self.sample = AsyncMock()

class MockToolInstance:
    def __init__(self):
        pass
    def get_name(self):
        return "mock_tool"
    async def execute_async(self, request, **kwargs):
        class Result:
            def dict(self):
                return {"status": "success"}
        return Result()
    def get_request_model(self):
        class DummyModel:
            def __init__(self, **kwargs):
                pass
        return DummyModel

@pytest.mark.asyncio
async def test_orchestrator_planning_and_execution():
    app = MagicMock()
    register_meta_orchestrator(app)
    
    tool_decorator = app.tool.return_value
    smart_orchestrator_func = tool_decorator.call_args[0][0]
    
    # Mock _TOOLS_CACHE
    mock_tools_cache = {
        "mock_tool": {
            "class": lambda x: MockToolInstance(),
            "description": "Mock tool",
            "prompt_desc": "mock_tool: Mock tool",
            "schema": {}
        }
    }
    
    mo._TOOLS_CACHE = mock_tools_cache
    
    mock_ctx = MockContext()
    
    # Plan response
    plan_response = MagicMock()
    plan_response.result = OrchestratorPlan(steps=[
        OrchestratorStep(
            tool="mock_tool", 
            reason="Test execution", 
            params={"input_data": "test_payload"}
        )
    ])
    
    # Synthesis response
    synth_response = MagicMock()
    synth_response.text = "Final synthesis result"
    
    mock_ctx.sample.side_effect = [plan_response, synth_response]
    
    request = OrchestratorRequest(query="Run test")
    
    response = await smart_orchestrator_func(request, mock_ctx)
    
    assert response.result == "Final synthesis result"
    assert response.tools_used == ["mock_tool"]
    assert response.confidence == 1.0
    
    assert mock_ctx.sample.call_count == 2
    mock_ctx.info.assert_any_call("Phase 1: Planification...")
