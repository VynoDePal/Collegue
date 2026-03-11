import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from collegue.tools.code_documentation import DocumentationTool, DocumentationRequest

@pytest.mark.asyncio
async def test_documentation_tool_success():
    tool = DocumentationTool()
    
    # Mock context and its sample method
    mock_ctx = MagicMock()
    mock_ctx.info = AsyncMock()
    mock_ctx.report_progress = AsyncMock()
    
    # Mock result from LLM
    mock_result = MagicMock()
    mock_result.text = """# Documentation Calculator

## Classe Calculator

Une classe simple pour effectuer des calculs de base.

### Méthodes

#### `add(x, y)`
Retourne la somme de x et y.

#### `multiply(x, y)`
Retourne le produit de x et y.
"""
    mock_ctx.sample = AsyncMock(return_value=mock_result)
    
    # Request
    code = """
class Calculator:
    def add(self, x, y): return x + y
    def multiply(self, x, y): return x * y
"""
    request = DocumentationRequest(
        code=code,
        language="python",
        doc_format="markdown",
        doc_style="standard"
    )
    
    # Mock prepare_prompt to return a string (as it would from fallback or engine)
    # We are testing that prepare_prompt is CALLED and works.
    # Since we patched the code to use self.prepare_prompt, and BaseTool has a default implementation 
    # that calls _build_prompt if engine is missing (which is the case in this test environment),
    # this should work end-to-end if _build_prompt is correctly defined.
    
    response = await tool.execute_async(request, ctx=mock_ctx)
    
    assert response.documentation
    assert "Calculator" in response.documentation
    assert response.format == "markdown"
    
    # Verify sample was called
    mock_ctx.sample.assert_called_once()
    
    # Verify we detected elements (functions/classes)
    assert len(response.documented_elements) > 0
    assert any(e['name'] == 'Calculator' for e in response.documented_elements)

