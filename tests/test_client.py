"""
Tests du client Python pour Collègue MCP
"""
import os
import sys
import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock

# Ajouter le répertoire parent au chemin pour pouvoir importer collegue
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, parent_dir)

# Importer le client Collègue
from collegue.client import CollegueClient

class TestCollegueClient(unittest.TestCase):
    """Tests unitaires pour le client Python Collègue MCP."""
    
    def setUp(self):
        """Configuration avant chaque test."""
        # Chemin vers le script app.py pour les tests
        self.script_path = os.path.abspath(os.path.join(parent_dir, "collegue", "app.py"))
        
        # Créer un mock pour le client FastMCP
        self.mock_fastmcp_client = MagicMock()
        # Utiliser AsyncMock pour __aenter__ et __aexit__ car ils sont awaitables
        self.mock_fastmcp_client.__aenter__ = AsyncMock(return_value=self.mock_fastmcp_client)
        self.mock_fastmcp_client.__aexit__ = AsyncMock(return_value=None)
        
        # Chaque méthode de test configurera ses propres mocks
    
    @patch('collegue.client.mcp_client.Client')
    async def async_test_client_initialization(self, mock_client_class):
        """Teste l'initialisation du client."""
        # Configurer le mock
        mock_client_class.return_value = self.mock_fastmcp_client
        
        # Tester l'initialisation avec le chemin du serveur
        async with CollegueClient(server_path=self.script_path) as client:
            self.assertIsNotNone(client)
            self.assertEqual(client.client, self.mock_fastmcp_client)
    
    @patch('collegue.client.mcp_client.Client')
    async def async_test_list_tools(self, mock_client_class):
        """Teste la méthode list_tools."""
        # Configurer le mock
        mock_client_class.return_value = self.mock_fastmcp_client
        
        # Créer des objets mock pour les outils
        tool1 = SimpleNamespace(name="analyze_code")
        tool2 = SimpleNamespace(name="create_session")
        tool3 = SimpleNamespace(name="get_session_context")
        tool4 = SimpleNamespace(name="suggest_tools_for_query")
        
        # Configurer list_tools pour retourner une liste d'objets d'outils
        self.mock_fastmcp_client.list_tools = AsyncMock(return_value=[tool1, tool2, tool3, tool4])
        
        async with CollegueClient(server_path=self.script_path) as client:
            tools = await client.list_tools()
            # Le mock retourne une liste
            self.assertEqual(len(tools), 4)
            self.assertIn("analyze_code", tools)
            self.assertIn("create_session", tools)
            self.assertIn("get_session_context", tools)
            self.assertIn("suggest_tools_for_query", tools)
            
            # Vérifier que la méthode list_tools du client FastMCP a été appelée
            self.mock_fastmcp_client.list_tools.assert_called_once()
    
    @patch('collegue.client.mcp_client.Client')
    async def async_test_create_session(self, mock_client_class):
        """Teste la méthode create_session."""
        # Configurer le mock
        mock_client_class.return_value = self.mock_fastmcp_client
        # Configurer la réponse du mock pour create_session
        mock_result = MagicMock()
        mock_result.data = {"session_id": "test_session_id", "created_at": "2025-06-12T15:00:00Z"}
        self.mock_fastmcp_client.call_tool = AsyncMock(return_value=mock_result)
        
        async with CollegueClient(server_path=self.script_path) as client:
            session = await client.create_session()
            self.assertEqual(session["session_id"], "test_session_id")
            self.assertEqual(client.session_id, "test_session_id")
            
            # Vérifier que la méthode call_tool du client FastMCP a été appelée avec les bons arguments
            self.mock_fastmcp_client.call_tool.assert_called_once_with("create_session", {})
    
    @patch('collegue.client.mcp_client.Client')
    async def async_test_get_session_context(self, mock_client_class):
        """Teste la méthode get_session_context."""
        # Configurer le mock
        mock_client_class.return_value = self.mock_fastmcp_client
        # Configurer la réponse du mock pour get_session_context
        mock_result = MagicMock()
        mock_result.data = {"session_id": "test_session_id", "context": {"files": [], "history": []}}
        self.mock_fastmcp_client.call_tool = AsyncMock(return_value=mock_result)
        
        async with CollegueClient(server_path=self.script_path) as client:
            # Définir un session_id pour le test
            client.session_id = "test_session_id"
            
            context = await client.get_session_context()
            self.assertEqual(context["session_id"], "test_session_id")
            self.assertIn("context", context)
            
            # Vérifier que la méthode call_tool du client FastMCP a été appelée avec les bons arguments
            self.mock_fastmcp_client.call_tool.assert_called_once_with("get_session_context", {"request": {
                "session_id": "test_session_id"
            }})
    
    @patch('collegue.client.mcp_client.Client')
    async def async_test_analyze_code(self, mock_client_class):
        """Teste la méthode analyze_code."""
        # Configurer le mock
        mock_client_class.return_value = self.mock_fastmcp_client
        # Configurer la réponse du mock pour analyze_code
        mock_result = MagicMock()
        mock_result.data = {
            "structure": {
                "functions": [{"name": "hello_world", "line": 1}],
                "classes": []
            }
        }
        self.mock_fastmcp_client.call_tool = AsyncMock(return_value=mock_result)
        
        async with CollegueClient(server_path=self.script_path) as client:
            # Définir un session_id pour le test
            client.session_id = "test_session_id"
            
            code = "def hello_world(): print('Hello, world!')"
            analysis = await client.analyze_code(code, "python", "test_file.py")
            self.assertIn("structure", analysis)
            self.assertIn("functions", analysis["structure"])
            self.assertEqual(len(analysis["structure"]["functions"]), 1)
            self.assertEqual(analysis["structure"]["functions"][0]["name"], "hello_world")
            
            # Vérifier que la méthode call_tool du client FastMCP a été appelée avec les bons arguments
            self.mock_fastmcp_client.call_tool.assert_called_once_with("analyze_code", {"request": {
                "code": code,
                "language": "python",
                "session_id": "test_session_id",
                "file_path": "test_file.py"
            }})
    
    @patch('collegue.client.mcp_client.Client')
    async def async_test_suggest_tools_for_query(self, mock_client_class):
        """Teste la méthode suggest_tools_for_query."""
        # Configurer le mock
        mock_client_class.return_value = self.mock_fastmcp_client
        # Configurer la réponse du mock pour suggest_tools_for_query
        mock_result = MagicMock()
        mock_result.data = [
            {"name": "analyze_code", "description": "Analyse un extrait de code"},
            {"name": "generate_code", "description": "Génère du code à partir d'une description"}
        ]
        self.mock_fastmcp_client.call_tool = AsyncMock(return_value=mock_result)
        
        async with CollegueClient(server_path=self.script_path) as client:
            # Définir un session_id pour le test
            client.session_id = "test_session_id"
            
            query = "Comment analyser ce code Python?"
            tools = await client.suggest_tools_for_query(query)
            self.assertEqual(len(tools), 2)
            self.assertEqual(tools[0]["name"], "analyze_code")
            
            # Vérifier que la méthode call_tool du client FastMCP a été appelée avec les bons arguments
            self.mock_fastmcp_client.call_tool.assert_called_once_with("suggest_tools_for_query", {"request": {
                "query": query,
                "session_id": "test_session_id"
            }})
    
    @patch('collegue.client.mcp_client.Client')
    async def async_test_generate_code_from_description(self, mock_client_class):
        """Teste la méthode generate_code_from_description."""
        # Configurer le mock
        mock_client_class.return_value = self.mock_fastmcp_client
        # Configurer la réponse du mock pour generate_code_from_description
        mock_result = MagicMock()
        mock_result.data = {
            "code": "def hello_world():\n    print('Hello, world!')",
            "explanation": "Cette fonction affiche 'Hello, world!'"
        }
        self.mock_fastmcp_client.call_tool = AsyncMock(return_value=mock_result)
        
        async with CollegueClient(server_path=self.script_path) as client:
            # Définir un session_id pour le test
            client.session_id = "test_session_id"
            
            description = "Une fonction qui affiche Hello World"
            language = "python"
            constraints = ["Utiliser print"]
            code_result = await client.generate_code_from_description(description, language, constraints)
            self.assertIn("code", code_result)
            self.assertIn("explanation", code_result)
            
            # Vérifier que la méthode call_tool du client FastMCP a été appelée avec les bons arguments
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
