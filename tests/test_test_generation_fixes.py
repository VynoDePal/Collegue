import pytest
from unittest.mock import MagicMock, AsyncMock
from collegue.tools.test_generation import TestGenerationTool, TestGenerationRequest, TestGenerationResponse

@pytest.mark.asyncio
async def test_test_generation_success():
    """Quand ctx.sample() renvoie du texte LLM exploitable, le tool doit le
    propager tel quel dans `response.test_code`. Le chemin structuré
    (LLMTestGenerationResult) n'est plus câblé dans
    `_execute_core_logic_async` — le tool lit `result.text`.
    """
    tool = TestGenerationTool()

    mock_ctx = MagicMock()
    mock_ctx.info = AsyncMock()
    mock_ctx.sample = AsyncMock()
    mock_ctx.report_progress = AsyncMock()

    mock_result = MagicMock()
    mock_result.text = "def test_add(): assert 1+1==2"
    mock_ctx.sample.return_value = mock_result

    request = TestGenerationRequest(
        code="def add(a,b): return a+b",
        language="python",
        test_framework="pytest",
    )

    response = await tool.execute_async(request, ctx=mock_ctx)

    assert response.test_code == "def test_add(): assert 1+1==2"
    assert response.language == "python"
    assert response.framework == "pytest"
    # Le tool compte les def test_ dans le texte → 1 fonction détectée
    assert len(response.tested_elements) >= 1
    assert any(e["name"] == "add" for e in response.tested_elements)

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
