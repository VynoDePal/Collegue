import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import collegue.core.meta_orchestrator as mo
from collegue.core.meta_orchestrator import (
    OrchestratorPlan,
    OrchestratorRequest,
    OrchestratorStep,
    register_meta_orchestrator,
)


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

    # Inject a fake tools registry via the lifespan_context — same pattern
    # the orchestrator uses in production now that _TOOLS_CACHE is gone (#211).
    mock_tools_cache = {
        "mock_tool": {
            "class": lambda x: MockToolInstance(),
            "description": "Mock tool",
            "prompt_desc": "mock_tool: Mock tool",
            "schema": {},
        }
    }

    mock_ctx = MockContext()
    mock_ctx.lifespan_context = {"tools_registry": mock_tools_cache}

    # Plan response
    plan_response = MagicMock()
    plan_response.result = OrchestratorPlan(
        steps=[OrchestratorStep(tool="mock_tool", reason="Test execution", params={"input_data": "test_payload"})]
    )

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


@pytest.mark.asyncio
async def test_orchestrator_enriches_params_from_context():
    """When the LLM plan omits required fields, context data fills them in."""
    app = MagicMock()
    register_meta_orchestrator(app)

    tool_decorator = app.tool.return_value
    smart_orchestrator_func = tool_decorator.call_args[0][0]

    # Tool that records received params
    captured_params = {}

    class RecordingModel:
        def __init__(self, **kwargs):
            captured_params.update(kwargs)

    class RecordingTool:
        def __init__(self, config=None):
            pass

        def get_request_model(self):
            return RecordingModel

        async def execute_async(self, req, **kwargs):
            class Result:
                def model_dump(self):
                    return {"status": "success"}

            return Result()

        def cleanup(self):
            pass

    mock_tools_cache = {
        "code_review": {
            "class": lambda x=None: RecordingTool(),
            "description": "Code review",
            "prompt_desc": "code_review: Reviews code quality",
            "schema": {},
        }
    }

    mock_ctx = MockContext()
    mock_ctx.lifespan_context = {"tools_registry": mock_tools_cache}

    # LLM generates plan with only "code" param, omitting "language"
    plan_response = MagicMock()
    plan_response.result = OrchestratorPlan(
        steps=[
            OrchestratorStep(
                tool="code_review",
                reason="Review code quality",
                params={"code": "def foo(): pass"},
            )
        ]
    )

    synth_response = MagicMock()
    synth_response.text = "Code review done"

    mock_ctx.sample.side_effect = [plan_response, synth_response]

    # User provides context with language info
    request = OrchestratorRequest(
        query="Review this code",
        context={"language": "python", "file_path": "main.py"},
    )

    response = await smart_orchestrator_func(request, mock_ctx)

    # Verify context values were injected as fallback
    assert captured_params.get("language") == "python"
    assert captured_params.get("file_path") == "main.py"
    # Verify LLM param was NOT overridden
    assert captured_params.get("code") == "def foo(): pass"


def test_parse_plan_from_text_fenced_json():
    """Fallback parser extracts plan from fenced JSON code block."""
    from collegue.core.meta_orchestrator import _parse_plan_from_text

    text = """Here is my plan:

```json
{
  "steps": [
    {"tool": "code_review", "reason": "Check quality", "params": {"code": "x=1"}}
  ]
}
```
"""
    plan = _parse_plan_from_text(text)
    assert plan is not None
    assert len(plan.steps) == 1
    assert plan.steps[0].tool == "code_review"


def test_parse_plan_from_text_inline_json():
    """Fallback parser extracts plan from inline JSON (no fences)."""
    from collegue.core.meta_orchestrator import _parse_plan_from_text

    text = 'I will use code_review. {"steps": [{"tool": "code_review", "reason": "Review", "params": {"code": "y=2"}}]}'
    plan = _parse_plan_from_text(text)
    assert plan is not None
    assert len(plan.steps) == 1
    assert plan.steps[0].tool == "code_review"


def test_parse_plan_from_text_steps_array():
    """Fallback parser extracts plan from a bare JSON array of steps."""
    from collegue.core.meta_orchestrator import _parse_plan_from_text

    text = '[{"tool": "code_review", "reason": "Review", "params": {"code": "z=3"}}]'
    plan = _parse_plan_from_text(text)
    assert plan is not None
    assert len(plan.steps) == 1


def test_parse_plan_from_text_empty():
    """Fallback parser returns None for empty/nonsense text."""
    from collegue.core.meta_orchestrator import _parse_plan_from_text

    assert _parse_plan_from_text("") is None
    assert _parse_plan_from_text("no json here at all") is None
    assert _parse_plan_from_text(None) is None


@pytest.mark.asyncio
async def test_orchestrator_uses_fallback_parser_when_structured_output_fails():
    """Orchestrator parses plan from LLM text when result_type returns None."""
    app = MagicMock()
    register_meta_orchestrator(app)

    tool_decorator = app.tool.return_value
    smart_orchestrator_func = tool_decorator.call_args[0][0]

    captured_params = {}

    class RecordingModel:
        def __init__(self, **kwargs):
            captured_params.update(kwargs)

    class RecordingTool:
        def __init__(self, config=None):
            pass

        def get_request_model(self):
            return RecordingModel

        async def execute_async(self, req, **kwargs):
            class Result:
                def model_dump(self):
                    return {"status": "ok"}

            return Result()

        def cleanup(self):
            pass

    mock_tools_cache = {
        "code_review": {
            "class": lambda x=None: RecordingTool(),
            "description": "Code review",
            "prompt_desc": "code_review: Reviews code quality",
            "schema": {},
        }
    }

    mock_ctx = MockContext()
    mock_ctx.lifespan_context = {"tools_registry": mock_tools_cache}

    call_count = [0]

    async def mock_sample(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            # Planning: return text (not structured) with embedded JSON
            class TextResult:
                result = None  # Structured output failed
                text = (
                    "Here is the plan:\n"
                    "```json\n"
                    '{"steps": [{"tool": "code_review", "reason": "Review code",'
                    ' "params": {"code": "def foo(): pass"}}]}\n'
                    "```"
                )

            return TextResult()
        else:

            class SynthResult:
                text = "Review complete"
                result = None

            return SynthResult()

    mock_ctx.sample = mock_sample

    request = OrchestratorRequest(
        query="Review this code",
        context={"language": "python"},
    )

    response = await smart_orchestrator_func(request, mock_ctx)

    # Should have used the fallback parser and executed code_review
    assert "code_review" in response.tools_used
    assert response.confidence > 0


def test_parse_plan_normalizes_field_aliases():
    """Verify _parse_plan_from_text normalizes tool_name/description to tool/reason."""
    from collegue.core.meta_orchestrator import _parse_plan_from_text

    # tool_name + description (common LLM output)
    plan = _parse_plan_from_text(
        '{"steps": [{"tool_name": "code_review", "description": "Review", "params": {"code": "x"}}]}'
    )
    assert plan is not None
    assert plan.steps[0].tool == "code_review"
    assert plan.steps[0].reason == "Review"

    # name + rationale
    plan = _parse_plan_from_text('[{"name": "test_generation", "rationale": "Generate tests"}]')
    assert plan is not None
    assert plan.steps[0].tool == "test_generation"
    assert plan.steps[0].params == {}

    # Correct fields still work
    plan = _parse_plan_from_text('{"steps": [{"tool": "code_review", "reason": "R", "params": {}}]}')
    assert plan is not None
    assert plan.steps[0].tool == "code_review"
