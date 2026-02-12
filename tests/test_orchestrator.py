"""
Tests unitaires pour le ToolOrchestrator
"""
import pytest

pytest.skip(
	"ToolOrchestrator supprimé volontairement (migration FastMCP)",
	allow_module_level=True,
)

import sys
import os
import unittest
import asyncio
from pathlib import Path


parent_dir = str(Path(__file__).parent.parent.absolute())
sys.path.insert(0, parent_dir)

from collegue.core.orchestrator import ToolOrchestrator

class TestToolOrchestrator(unittest.TestCase):
    """Tests unitaires pour la classe ToolOrchestrator"""

    def setUp(self):
        """Initialisation avant chaque test"""
        self.orchestrator = ToolOrchestrator()


        def add(a, b, context=None):
            return {"result": a + b}

        def multiply(a, b, context=None):
            return {"result": a * b}

        async def async_subtract(a, b, context=None):
            await asyncio.sleep(0.1)
            return {"result": a - b}


        self.orchestrator.register_tool(
            "add", add, "Additionne deux nombres",
            category="math", required_args=["a", "b"]
        )
        self.orchestrator.register_tool(
            "multiply", multiply, "Multiplie deux nombres",
            category="math", required_args=["a", "b"]
        )
        self.orchestrator.register_tool(
            "subtract", async_subtract, "Soustrait deux nombres",
            category="math", required_args=["a", "b"]
        )

    def test_register_tool(self):
        self.assertIn("add", self.orchestrator.tools)
        self.assertIn("multiply", self.orchestrator.tools)
        self.assertIn("subtract", self.orchestrator.tools)


        self.assertEqual(self.orchestrator.tools["add"]["description"], "Additionne deux nombres")
        self.assertEqual(self.orchestrator.tools["add"]["category"], "math")
        self.assertEqual(self.orchestrator.tools["add"]["required_args"], ["a", "b"])


        def new_add(a, b):
            return a + b

        result = self.orchestrator.register_tool("add", new_add, "Nouvel outil d'addition")
        self.assertFalse(result)

    def test_get_tool(self):
        tool = self.orchestrator.get_tool("add")
        self.assertIsNotNone(tool)
        self.assertEqual(tool["name"], "add")
        self.assertEqual(tool["description"], "Additionne deux nombres")

        tool = self.orchestrator.get_tool("nonexistent")
        self.assertIsNone(tool)

    def test_list_tools(self):
        tools = self.orchestrator.list_tools()

        self.assertEqual(len(tools), 3)
        tool_names = [tool["name"] for tool in tools]
        self.assertIn("add", tool_names)
        self.assertIn("multiply", tool_names)
        self.assertIn("subtract", tool_names)

        math_tools = self.orchestrator.list_tools(category="math")
        self.assertEqual(len(math_tools), 3)

        other_tools = self.orchestrator.list_tools(category="other")
        self.assertEqual(len(other_tools), 0)

    def test_execute_tool(self):
        result = self.orchestrator.execute_tool("add", {"a": 5, "b": 3})
        self.assertEqual(result["result"], 8)

        result = self.orchestrator.execute_tool("add", {"a": 5})
        self.assertIn("error", result)
        self.assertIn("Arguments requis manquants", result["error"])

        result = self.orchestrator.execute_tool("nonexistent", {})
        self.assertIn("error", result)
        self.assertIn("Outil non trouvé", result["error"])

    def test_execute_tool_async(self):
        async def run_async_test():
            result = await self.orchestrator.execute_tool_async("subtract", {"a": 10, "b": 4})
            self.assertEqual(result["result"], 6)

            result = await self.orchestrator.execute_tool_async("add", {"a": 7, "b": 2})
            self.assertEqual(result["result"], 9)

        asyncio.run(run_async_test())

    def test_validate_args(self):
        valid_args = {"a": 5, "b": 3}
        is_valid, missing = self.orchestrator._validate_args("add", valid_args)
        self.assertTrue(is_valid)
        self.assertEqual(missing, [])

        invalid_args = {"a": 5}
        is_valid, missing = self.orchestrator._validate_args("add", invalid_args)
        self.assertFalse(is_valid)
        self.assertEqual(missing, ["b"])

    def test_suggest_tools(self):
        query = "Comment additionner deux nombres ?"
        suggestions = self.orchestrator.suggest_tools(query)

        tool_names = [tool["name"] for tool in suggestions]
        self.assertIn("add", tool_names)

        query = "Quel temps fait-il aujourd'hui ?"
        suggestions = self.orchestrator.suggest_tools(query)
        self.assertEqual(len(suggestions), 0)

    def test_create_tool_chain(self):
        tools_chain = [
            {
                "name": "add",
                "args": {"a": 5, "b": 3},
                "result_mapping": {"b": "result"}
            },
            {
                "name": "multiply",
                "args": {"a": 2}
            }
        ]

        result = self.orchestrator.create_tool_chain("math_chain", tools_chain)
        self.assertTrue(result)

        self.assertIn("math_chain", self.orchestrator.tools)

        async def run_chain_test():
            result = await self.orchestrator.execute_tool_async("math_chain", {})
            print("Résultat complet:", result)
            if "results" in result:
                print("Contenu de results:", result["results"])
                if len(result["results"]) > 0:
                    print("Dernier résultat:", result["results"][-1])

            if isinstance(result, dict) and "results" in result:
                last_result = result["results"][-1]
                if isinstance(last_result, dict) and "result" in last_result:
                    self.assertEqual(last_result["result"], 16)
                else:
                    self.assertEqual(last_result, 16)
            else:
                self.assertEqual(result, 16)

        asyncio.run(run_chain_test())

    def test_extract_result_value(self):
        result = {"result": 42, "status": "success"}
        value = self.orchestrator._extract_result_value(result, "result")
        self.assertEqual(value, 42)

        result = {"data": {"items": [{"id": 1, "value": "test"}]}}
        value = self.orchestrator._extract_result_value(result, "data.items.0.value")
        self.assertEqual(value, "test")

        value = self.orchestrator._extract_result_value(result, "nonexistent")
        self.assertIsNone(value)

if __name__ == "__main__":
    unittest.main()
