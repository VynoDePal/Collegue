"""
Test des composants principaux du Core Engine de Collègue MCP

Architecture actuelle:
- Les composants (CodeParser, ResourceManager, PromptEngine) sont
  initialisés dans le lifespan FastMCP et accessibles via ctx.lifespan_context.
- Les tools sont enregistrés via register_tools(app) sans app_state.
- L'orchestration utilise le meta_orchestrator FastMCP natif.
"""
import sys
import os
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from collegue.core import CodeParser
from collegue.core import register_core
from collegue.tools import register_tools, _discover_tools
from collegue.config import Settings
from fastmcp import FastMCP


class TestCoreRegistration(unittest.TestCase):
    """Tests pour l'enregistrement des composants core."""

    def test_register_core_no_error(self):
        """register_core(app) ne lève pas d'erreur."""
        app = FastMCP("test-core")
        register_core(app)

    def test_register_tools_no_error(self):
        """register_tools(app) enregistre les tools sans erreur."""
        app = FastMCP("test-tools")
        register_tools(app)


class TestToolDiscovery(unittest.TestCase):
    """Tests pour la découverte automatique des tools."""

    def test_discover_tools(self):
        """Les tools BaseTool sont découverts automatiquement."""
        tools = _discover_tools()
        self.assertGreater(len(tools), 0, "Au moins un tool doit être découvert")

        tool_names = [t.__name__ for t in tools]
        self.assertIn("SecretScanTool", tool_names)
        self.assertIn("RefactoringTool", tool_names)

    def test_all_tools_have_required_attrs(self):
        """Chaque tool découvert a les attributs requis."""
        tools = _discover_tools()
        for tool_class in tools:
            instance = tool_class({})
            self.assertTrue(instance.get_name(), f"{tool_class.__name__} doit avoir un nom")
            self.assertTrue(instance.get_description(), f"{tool_class.__name__} doit avoir une description")
            self.assertIsNotNone(instance.get_request_model(), f"{tool_class.__name__} doit avoir un request_model")
            self.assertIsNotNone(instance.get_response_model(), f"{tool_class.__name__} doit avoir un response_model")


class TestCodeParser(unittest.TestCase):
    """Tests pour le CodeParser."""

    def setUp(self):
        self.parser = CodeParser()

    def test_parse_python(self):
        """Le parser analyse correctement du code Python."""
        code = '''
def calculate_sum(a, b):
    """Calculate the sum of two numbers."""
    return a + b

class Calculator:
    def __init__(self):
        self.history = []

    def add(self, a, b):
        result = a + b
        self.history.append(result)
        return result
'''
        result = self.parser.parse(code, "python")
        self.assertEqual(result["language"], "python")
        self.assertTrue(result.get("ast_valid", False))

    def test_parse_javascript(self):
        """Le parser détecte le JavaScript."""
        code = "function hello() { return 'Hello'; }"
        result = self.parser.parse(code, "javascript")
        self.assertEqual(result["language"], "javascript")


class TestToolExecution(unittest.TestCase):
    """Tests pour l'exécution des tools avec le nouveau système."""

    def test_refactoring_tool_local_fallback(self):
        """Le RefactoringTool fonctionne en mode local (sans LLM)."""
        from collegue.tools.refactoring import RefactoringTool, RefactoringRequest

        tool = RefactoringTool({})
        request = RefactoringRequest(
            code="x = True\nif x == True:\n    print('yes')",
            language="python",
            refactoring_type="simplify",
        )
        result = tool.execute(request, ctx=None)
        self.assertIsNotNone(result)

    def test_secret_scan_tool(self):
        """Le SecretScanTool détecte les secrets."""
        from collegue.tools.secret_scan import SecretScanTool, SecretScanRequest

        tool = SecretScanTool({})
        request = SecretScanRequest(
            content="API_KEY=sk-test-1234567890abcdef",
            scan_type="content",
        )
        result = tool.execute(request, ctx=None)
        self.assertFalse(result.clean, "Un secret doit être détecté")
        self.assertGreater(result.total_findings, 0)

    def test_documentation_tool(self):
        """Le DocumentationTool génère de la documentation."""
        from collegue.tools.documentation import DocumentationTool, DocumentationRequest

        tool = DocumentationTool({})
        request = DocumentationRequest(
            code="def add(a, b): return a + b",
            language="python",
        )
        result = tool.execute(request, ctx=None)
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
