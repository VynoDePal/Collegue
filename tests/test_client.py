"""
Tests du client Python pour Collègue MCP
"""
import os
import sys
import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock


parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, parent_dir)


from collegue.client import CollegueClient

class TestCollegueClient(unittest.TestCase):
    """Tests unitaires pour le client Python Collègue MCP."""

    def setUp(self):
        self.script_path = os.path.abspath(os.path.join(parent_dir, "collegue", "app.py"))

        self.mock_fastmcp_client = MagicMock()
        self.mock_fastmcp_client.__aenter__ = AsyncMock(return_value=self.mock_fastmcp_client)
        self.mock_fastmcp_client.__aexit__ = AsyncMock(return_value=None)

    @patch('collegue.client.mcp_client.Client')
    async def async_test_client_initialization(self, mock_client_class):
        mock_client_class.return_value = self.mock_fastmcp_client

        async with CollegueClient(server_path=self.script_path) as client:
            self.assertIsNotNone(client)
            self.assertEqual(client.client, self.mock_fastmcp_client)

    @patch('collegue.client.mcp_client.Client')
    async def async_test_list_tools(self, mock_client_class):
        mock_client_class.return_value = self.mock_fastmcp_client

        tool1 = SimpleNamespace(name="analyze_code")
        tool2 = SimpleNamespace(name="create_session")
        tool3 = SimpleNamespace(name="get_session_context")
        tool4 = SimpleNamespace(name="suggest_tools_for_query")

        self.mock_fastmcp_client.list_tools = AsyncMock(return_value=[tool1, tool2, tool3, tool4])

        async with CollegueClient(server_path=self.script_path) as client:
            tools = await client.list_tools()
            self.assertEqual(len(tools), 4)
            self.assertIn("analyze_code", tools)
            self.assertIn("create_session", tools)
            self.assertIn("get_session_context", tools)
            self.assertIn("suggest_tools_for_query", tools)

            self.mock_fastmcp_client.list_tools.assert_called_once()

    @patch('collegue.client.mcp_client.Client')
    async def async_test_create_session(self, mock_client_class):
        mock_client_class.return_value = self.mock_fastmcp_client
        mock_result = MagicMock()
        mock_result.data = {"session_id": "test_session_id", "created_at": "2025-06-12T15:00:00Z"}
        self.mock_fastmcp_client.call_tool = AsyncMock(return_value=mock_result)

        async with CollegueClient(server_path=self.script_path) as client:
            session = await client.create_session()
            self.assertEqual(session["session_id"], "test_session_id")
            self.assertEqual(client.session_id, "test_session_id")

            self.mock_fastmcp_client.call_tool.assert_called_once_with("create_session", {})

    @patch('collegue.client.mcp_client.Client')
    async def async_test_get_session_context(self, mock_client_class):
        mock_client_class.return_value = self.mock_fastmcp_client
        mock_result = MagicMock()
        mock_result.data = {"session_id": "test_session_id", "context": {"files": [], "history": []}}
        self.mock_fastmcp_client.call_tool = AsyncMock(return_value=mock_result)

        async with CollegueClient(server_path=self.script_path) as client:
            client.session_id = "test_session_id"

            context = await client.get_session_context()
            self.assertEqual(context["session_id"], "test_session_id")
            self.assertIn("context", context)

            self.mock_fastmcp_client.call_tool.assert_called_once_with("get_session_context", {"request": {
                "session_id": "test_session_id"
            }})

    @patch('collegue.client.mcp_client.Client')
    async def async_test_analyze_code(self, mock_client_class):
        mock_client_class.return_value = self.mock_fastmcp_client
        mock_result = MagicMock()
        mock_result.data = {
            "structure": {
                "functions": [{"name": "hello_world", "line": 1}],
                "classes": []
            }
        }
        self.mock_fastmcp_client.call_tool = AsyncMock(return_value=mock_result)

        async with CollegueClient(server_path=self.script_path) as client:
            client.session_id = "test_session_id"

            code = "def hello_world(): print('Hello, world!')"
            analysis = await client.analyze_code(code, "python", "test_file.py")
            self.assertIn("structure", analysis)
            self.assertIn("functions", analysis["structure"])
            self.assertEqual(len(analysis["structure"]["functions"]), 1)
            self.assertEqual(analysis["structure"]["functions"][0]["name"], "hello_world")

            self.mock_fastmcp_client.call_tool.assert_called_once_with("analyze_code", {"request": {
                "code": code,
                "language": "python",
                "session_id": "test_session_id",
                "file_path": "test_file.py"
            }})

    @patch('collegue.client.mcp_client.Client')
    async def async_test_suggest_tools_for_query(self, mock_client_class):
        mock_client_class.return_value = self.mock_fastmcp_client
        mock_result = MagicMock()
        mock_result.data = [
            {"name": "analyze_code", "description": "Analyse un extrait de code"},
            {"name": "generate_code", "description": "Génère du code à partir d'une description"}
        ]
        self.mock_fastmcp_client.call_tool = AsyncMock(return_value=mock_result)

        async with CollegueClient(server_path=self.script_path) as client:
            client.session_id = "test_session_id"

            query = "Comment analyser ce code Python?"
            tools = await client.suggest_tools_for_query(query)
            self.assertEqual(len(tools), 2)
            self.assertEqual(tools[0]["name"], "analyze_code")

            self.mock_fastmcp_client.call_tool.assert_called_once_with("suggest_tools_for_query", {"request": {
                "query": query,
                "session_id": "test_session_id"
            }})

    @patch('collegue.client.mcp_client.Client')
    async def async_test_generate_code_from_description(self, mock_client_class):
        mock_client_class.return_value = self.mock_fastmcp_client
        mock_result = MagicMock()
        mock_result.data = {
            "code": "def hello_world():\n    print('Hello, world!')",
            "explanation": "Cette fonction affiche 'Hello, world!'"
        }
        self.mock_fastmcp_client.call_tool = AsyncMock(return_value=mock_result)

        async with CollegueClient(server_path=self.script_path) as client:
            client.session_id = "test_session_id"

            description = "Une fonction qui affiche Hello World"
            language = "python"
            constraints = ["Utiliser print"]
            code_result = await client.generate_code_from_description(description, language, constraints)
            self.assertIn("code", code_result)
            self.assertIn("explanation", code_result)

            self.mock_fastmcp_client.call_tool.assert_called_once_with("generate_code_from_description", {"request": {
                "description": description,
                "language": language,
                "constraints": constraints,
                "session_id": "test_session_id"
            }})

    def test_client_initialization(self):
        """Exécute le test d'initialisation du client."""
        asyncio.run(self.async_test_client_initialization())

    def test_list_tools(self):
        """Exécute le test de la méthode list_tools."""
        asyncio.run(self.async_test_list_tools())

    def test_create_session(self):
        """Exécute le test de la méthode create_session."""
        asyncio.run(self.async_test_create_session())

    def test_get_session_context(self):
        """Exécute le test de la méthode get_session_context."""
        asyncio.run(self.async_test_get_session_context())

    def test_analyze_code(self):
        """Exécute le test de la méthode analyze_code."""
        asyncio.run(self.async_test_analyze_code())

    def test_suggest_tools_for_query(self):
        """Exécute le test de la méthode suggest_tools_for_query."""
        asyncio.run(self.async_test_suggest_tools_for_query())

    def test_generate_code_from_description(self):
        """Exécute le test de la méthode generate_code_from_description."""
        asyncio.run(self.async_test_generate_code_from_description())

if __name__ == "__main__":
    unittest.main()
