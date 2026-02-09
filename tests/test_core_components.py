"""
Test des composants principaux du Core Engine de Collègue MCP
"""
import sys
import os
from pathlib import Path


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from collegue.core import ToolOrchestrator
from collegue.core import ContextManager
from collegue.core import CodeParser
from collegue.tools import register_tools
from collegue.core import register_core
from collegue.config import Settings
from fastmcp import FastMCP
import asyncio
from unittest.mock import patch
from collegue.core.tool_llm_manager import ToolLLMManager
from unittest import mock
from unittest.mock import patch, MagicMock, AsyncMock


settings = Settings()
app = FastMCP(title="TestApp", description="Test App", version="0.1.0")


app_state = {}


register_core(app, app_state)
register_tools(app, app_state)


orchestrator = app_state["orchestrator"]
context_manager = app_state["context_manager"]

async def test_components_integration():
    """Test d'intégration des trois composants principaux"""
    print("=== Test d'intégration des composants du Core Engine ===")

    session_id = "test_session_123"
    context_manager.create_context(session_id, metadata={"user_id": "test_user", "session_name": "Test Session"})
    print(f"Session créée avec ID: {session_id}")

    llm_manager = ToolLLMManager()
    orchestrator.app_state["llm_manager"] = llm_manager

    def analyze_code(code, language=None, context=None):
        """Analyse le code fourni"""
        print(f"Analyse du code avec langue: {language}")
        parser = CodeParser()
        result = parser.parse(code, language)
        if context and "session_id" in context:
            context_manager.add_code_to_context(context["session_id"], code)
        return result

    def get_context(session_id, context=None):
        """Récupère le contexte d'une session"""
        return context_manager.get_context(session_id)

    async def suggest_tools_for_query(query, session_id=None, context=None):
        """Suggère des outils en fonction d'une requête"""
        session_context = None
        if session_id:
            session_context = context_manager.get_context(session_id)
        return orchestrator.suggest_tools(query, session_context)

    orchestrator.register_tool(
        "analyze_code",
        analyze_code,
        "Analyse le code fourni et extrait sa structure",
        category="code_analysis"
    )

    orchestrator.register_tool(
        "get_context",
        get_context,
        "Récupère le contexte d'une session",
        category="context_management"
    )

    orchestrator.register_tool(
        "suggest_tools_for_query",
        suggest_tools_for_query,
        "Suggère des outils en fonction d'une requête",
        category="tool_suggestion"
    )

    tools_chain = [
        {
            "name": "analyze_code",
            "args": {
                "code": "def hello(name):\n    return f'Hello, {name}!'",
                "language": "python"
            }
        },
        {
            "name": "get_context",
            "args": {
                "session_id": session_id
            }
        }
    ]

    chain_created = orchestrator.create_tool_chain("analyze_and_get_context", tools_chain)
    print(f"Chaîne d'outils créée: {chain_created}")

    print("\n--- Test 1: Analyse de code ---")
    code_sample = """
def calculate_sum(a, b):
    \"\"\"Calculate the sum of two numbers.\"\"\"
    return a + b

class Calculator:
    def __init__(self):
        self.history = []

    def add(self, a, b):
        result = a + b
        self.history.append(result)
        return result
"""

    result = orchestrator.execute_tool("analyze_code", {
        "code": code_sample,
        "language": "python",
        "context": {"session_id": session_id}
    })

    print("Résultat brut de l'exécution:")
    print(result)

    if "error" in result:
        print(f"Erreur lors de l'analyse: {result['error']}")
    else:
        print("\nRésultat de l'analyse:")
        print(f"- Langage détecté: {result['language']}")
        print(f"- Nombre de fonctions: {len(result.get('functions', []))}")
        print(f"- Nombre de classes: {len(result.get('classes', []))}")
        print(f"- Validité AST: {result.get('ast_valid', False)}")


    print("\n--- Test 2: Récupération du contexte ---")
    context_result = orchestrator.execute_tool("get_context", {"session_id": session_id})

    print("Résultat brut de l'exécution:")
    print(context_result)

    if "error" in context_result:
        print(f"Erreur lors de la récupération du contexte: {context_result['error']}")
    else:
        print("\nContexte de session:")
        print(f"- Session ID: {context_result['session_id']}")
        print(f"- Utilisateur: {context_result['metadata'].get('user_id', 'N/A')}")
        print(f"- Nombre d'entrées de code: {len(context_result.get('code_history', []))}")


    print("\n--- Test 3: Suggestion d'outils ---")
    suggestions_result = await orchestrator.execute_tool_async("suggest_tools_for_query", {
        "query": "analyser le code python",
        "session_id": session_id
    })

    print("Résultat brut de l'exécution:")
    print(suggestions_result)

    if "error" in suggestions_result:
        print(f"Erreur lors de la suggestion d'outils: {suggestions_result['error']}")
    else:
        print("\nSuggestions d'outils:")

        if isinstance(suggestions_result, list):
            for suggestion in suggestions_result:
                print(f"- Outil: {suggestion.get('name', 'N/A')}, Score: {suggestion.get('relevance', 'N/A')}")
        elif isinstance(suggestions_result, dict) and 'suggestions' in suggestions_result:
             for suggestion in suggestions_result['suggestions']:
                print(f"- Outil: {suggestion.get('name', 'N/A')}, Score: {suggestion.get('relevance', 'N/A')}")
        else:
            print(f"Format de réponse inattendu pour les suggestions: {suggestions_result}")


    print("\n--- Test 4: Exécution d'une chaîne d'outils ---")
    chain_result = await orchestrator.execute_tool_async("analyze_and_get_context", {
        "context": {"session_id": session_id}
    })

    print("Résultat brut de l'exécution:")
    print(chain_result)

    if "error" in chain_result:
        print(f"Erreur lors de l'exécution de la chaîne d'outils: {chain_result['error']}")
    else:
        print("\nRésultat de la chaîne d'outils:")
        if "result" in chain_result and isinstance(chain_result["result"], dict):
            result_data = chain_result["result"]
            if "completed_steps" in result_data and "total_steps" in result_data:
                print(f"- Étapes complétées: {result_data['completed_steps']}/{result_data['total_steps']}")
            else:
                print(f"- Résultat: {result_data}")
        else:
            print(f"- Résultat: {chain_result}")


    print("\n--- Test 5: Historique d'exécution ---")
    history = orchestrator.get_execution_history(limit=3)

    print(f"Dernières {len(history)} exécutions:")
    for entry in history:
        print(f"- {entry['timestamp']}: {entry['tool_name']} (succès: {entry['success']})")

    print("\n--- Mocking ToolLLMManager for LLM tool tests ---")
    with patch.object(ToolLLMManager, 'sync_generate') as mock_sync_generate:
        mock_sync_generate.return_value = "Mocked LLM Response for the tool."


        print("\n--- Test 8: Refactoring (LLM) ---")

        from collegue.tools.refactoring import refactor_code, RefactoringRequest

        refactoring_request = RefactoringRequest(
            code="def add(a, b): return a + b",
            language="python",
            refactoring_type="optimize",
            session_id=session_id,
            parameters={}
        )

        class MockRefactorLLMService:
            def __init__(self):
                self.llm_config = MagicMock()
                self.llm_config.model_name = "test-model"
                self.sync_generate = MagicMock(return_value="""
                def add(a: int, b: int) -> int:
                    \"\"\"Add two numbers together and return the result.\"\"\"
                    return a + b
                """)

        mock_llm_service = MockRefactorLLMService()
        mock_parser = MagicMock()
        response = refactor_code(refactoring_request, parser=mock_parser, llm_manager=mock_llm_service)

        refactor_result = response.model_dump()
        print(f"Résultat brut: {refactor_result}")

        mock_llm_service.sync_generate.assert_called_once()

        assert "refactored_code" in refactor_result, "Response should contain 'refactored_code' field"
        assert "changes" in refactor_result, "Response should contain 'changes' field"
        print(f"Mocked LLM call for refactor_code: OK (Output: {refactor_result.get('refactored_code','')[:50]}...)")


        print("\n--- Test 9: Documentation Generation (LLM) ---")

        from collegue.tools.documentation import generate_documentation, DocumentationRequest

        doc_request = DocumentationRequest(
            code="def add(a, b): return a + b",
            language="python",
            doc_style="numpy",
            session_id=session_id
        )

        class MockDocLLMService:
            def __init__(self):
                self.llm_config = MagicMock()
                self.llm_config.model_name = "test-model"
                self.sync_generate = MagicMock(return_value="""
                    def add(a, b):
                        \"\"\"Add two numbers together.

                        Parameters
                        ----------
                        a : int
                            First number to add
                        b : int
                            Second number to add

                        Returns
                        -------
                        int
                            The sum of a and b
                        \"\"\"
                        return a + b
                    """)

        mock_llm_service = MockDocLLMService()
        mock_parser = MagicMock()
        response = generate_documentation(doc_request, parser=mock_parser, llm_manager=mock_llm_service)

        doc_result = response.model_dump()
        print(f"Résultat brut: {doc_result}")

        mock_llm_service.sync_generate.assert_called_once()

        assert "documentation" in doc_result, "Response should contain 'documentation' field"
        print(f"Mocked LLM call for generate_documentation: OK (Output: {doc_result.get('documentation','')[:50]}...)")


        print("\n--- Test 10: Test Generation (LLM) ---")

        from collegue.tools.test_generation import generate_tests, TestGenerationRequest

        test_request = TestGenerationRequest(
            code="def add(a, b): return a + b",
            language="python",
            test_framework="pytest",
            session_id=session_id
        )

        class MockTestLLMService:
            def __init__(self):
                self.llm_config = MagicMock()
                self.llm_config.model_name = "test-model"
                self.sync_generate = MagicMock(return_value="""
                def test_add():
                    assert add(1, 2) == 3
                    assert add(0, 0) == 0
                    assert add(-1, 1) == 0
                """)

        mock_llm_service = MockTestLLMService()
        mock_parser = MagicMock()
        response = generate_tests(test_request, parser=mock_parser, llm_manager=mock_llm_service)

        test_result = response.model_dump()
        print(f"Résultat brut: {test_result}")

        mock_llm_service.sync_generate.assert_called_once()

        assert "test_code" in test_result, "Response should contain 'test_code' field"
        print(f"Mocked LLM call for generate_tests: OK (Output: {test_result.get('test_code','')[:50]}...)")

    print("\n--- Nettoyage: Fermer la session ---")
    orchestrator.execute_tool("close_session", {"session_id": session_id})
    print(f"Session {session_id} fermée.")
    print("\n=== Tests terminés avec succès ===")

if __name__ == "__main__":
    asyncio.run(test_components_integration())
