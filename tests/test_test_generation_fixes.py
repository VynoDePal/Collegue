import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from collegue.tools.test_generation import TestGenerationTool, TestGenerationRequest, TestGenerationResponse, LLMTestGenerationResult

@pytest.mark.asyncio
async def test_test_generation_success():
    tool = TestGenerationTool()
    
    # Mock context
    mock_ctx = MagicMock()
    mock_ctx.info = AsyncMock()
    mock_ctx.sample = AsyncMock()
    mock_ctx.report_progress = AsyncMock()
    
    # Mock LLM result (structured)
    mock_result = MagicMock()
    mock_result.result = LLMTestGenerationResult(
        test_code="def test_add(): assert 1+1==2",
        test_count=1,
        coverage_estimate=1.0,
        tested_functions=["add"],
        tested_classes=[],
        imports_required=["pytest"]
    )
    mock_ctx.sample.return_value = mock_result
    
    request = TestGenerationRequest(
        code="def add(a,b): return a+b",
        language="python",
        test_framework="pytest"
    )
    
    # Execute
    response = await tool.execute_async(request, ctx=mock_ctx)
    
    assert response.test_code == "def test_add(): assert 1+1==2"
    assert response.language == "python"
    assert response.framework == "pytest"
    assert response.estimated_coverage == 1.0
    assert len(response.tested_elements) == 1
    assert response.tested_elements[0]["name"] == "add"
    
    # Verify prompt engine usage (via ctx.sample being called)
    mock_ctx.sample.assert_called_once()

@pytest.mark.asyncio
async def test_test_generation_fallback_text():
    # Test fallback to text when structured output fails or is disabled
    tool = TestGenerationTool()
    
    mock_ctx = MagicMock()
    mock_ctx.info = AsyncMock()
    mock_ctx.report_progress = AsyncMock()
    mock_ctx.sample = AsyncMock()
    
    # Mock text result
    mock_text_result = MagicMock()
    mock_text_result.text = "def test_sub(): assert 3-1==2"
    
    # sample will be called twice if the first structured attempt fails, 
    # OR once if use_structured_output is False.
    # Let's simulate use_structured_output=False for simplicity or just mock the text return
    mock_ctx.sample.return_value = mock_text_result
    
    request = TestGenerationRequest(
        code="def sub(a,b): return a-b",
        language="python",
        test_framework="pytest"
    )
    
    # Force disable structured output to test text path
    response = await tool.execute_async(request, ctx=mock_ctx, use_structured_output=False)
    
    assert response.test_code == "def test_sub(): assert 3-1==2"
