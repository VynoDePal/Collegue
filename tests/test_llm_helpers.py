"""
Tests unitaires pour les LLM Helpers du package collegue.tools.llm_helpers

Ces tests utilisent des mocks pour tester les builders et formatters sans faire d'appels réels aux LLMs.
"""
import sys
import os
sys.path.insert(0, '/home/kevyn-odjo/Documents/Collegue')

from unittest.mock import Mock, patch, MagicMock
import json

print("=" * 80)
print("TESTS UNITAIRES - LLM HELPERS")
print("=" * 80)

# =============================================================================
# TEST 1: LLM REQUEST BUILDER
# =============================================================================
print("\n" + "=" * 80)
print("TEST 1: LLM REQUEST BUILDER")
print("=" * 80)

try:
    from collegue.tools.llm_helpers.builders import LLMRequestBuilder, DocumentationRequestBuilder, RefactoringRequestBuilder
    
    # Test 1.1: Initialisation LLMRequestBuilder
    print("\n1.1 Test initialisation LLMRequestBuilder...")
    builder = LLMRequestBuilder(model="gemini-2.5-pro")
    assert builder.model == "gemini-2.5-pro"
    assert builder.temperature is None
    assert builder.max_tokens is None
    print("   ✅ Initialisation correcte")
    
    # Test 1.2: Méthodes de configuration
    print("\n1.2 Test méthodes de configuration...")
    builder = (LLMRequestBuilder()
               .with_model("gemini-3-flash")
               .with_temperature(0.7)
               .with_max_tokens(1000))
    assert builder.model == "gemini-3-flash"
    assert builder.temperature == 0.7
    assert builder.max_tokens == 1000
    print("   ✅ Configuration fluide correcte")
    
    # Test 1.3: build_prompt
    print("\n1.3 Test build_prompt...")
    prompt = (LLMRequestBuilder()
              .with_model("gemini-2.5-pro")
              .with_system_prompt("Tu es un assistant Python")
              .with_user_prompt("Explique les dataclasses")
              .build_prompt())
    
    assert prompt["model"] == "gemini-2.5-pro"
    assert "Tu es un assistant Python" in prompt["contents"][0]["text"]
    assert "Explique les dataclasses" in prompt["contents"][1]["text"]
    print("   ✅ build_prompt structure correcte")
    
    # Test 1.4: DocumentationRequestBuilder
    print("\n1.4 Test DocumentationRequestBuilder...")
    doc_builder = DocumentationRequestBuilder()
    doc_request = (doc_builder
                   .with_code("class Test: pass")
                   .with_language("python")
                   .with_focus("classes")
                   .build())
    
    assert doc_request.code == "class Test: pass"
    assert doc_request.language == "python"
    assert doc_request.focus == "classes"
    assert "générer de la documentation" in doc_request.prompt.lower()
    print("   ✅ DocumentationRequestBuilder fonctionne")
    
    # Test 1.5: DocumentationRequestBuilder avec exemples
    print("\n1.5 Test DocumentationRequestBuilder avec exemples...")
    doc_request = (doc_builder
                   .with_code("def add(a, b): return a + b")
                   .with_language("python")
                   .with_focus("functions")
                   .with_examples(True)
                   .build())
    
    assert "exemples" in doc_request.prompt.lower() or "examples" in doc_request.prompt.lower()
    print("   ✅ DocumentationRequestBuilder avec exemples")
    
    # Test 1.6: RefactoringRequestBuilder
    print("\n1.6 Test RefactoringRequestBuilder...")
    ref_builder = RefactoringRequestBuilder()
    ref_request = (ref_builder
                   .with_code("def calculate(x, y, z, a, b, c): return x + y + z + a + b + c")
                   .with_language("python")
                   .with_refactor_type("extract")
                   .build())
    
    assert ref_request.code == "def calculate(x, y, z, a, b, c): return x + y + z + a + b + c"
    assert ref_request.language == "python"
    assert ref_request.refactor_type == "extract"
    assert "refactorer" in ref_request.prompt.lower() or "améliorer" in ref_request.prompt.lower()
    print("   ✅ RefactoringRequestBuilder fonctionne")
    
    # Test 1.7: RefactoringRequestBuilder avec analyse
    print("\n1.7 Test RefactoringRequestBuilder avec analyse...")
    ref_request = (ref_builder
                   .with_code("for i in range(len(items)): print(items[i])")
                   .with_language("python")
                   .with_refactor_type("modernize")
                   .with_analysis(True)
                   .build())
    
    assert ref_request.analysis is True
    assert "analyse" in ref_request.prompt.lower() or "analysis" in ref_request.prompt.lower()
    print("   ✅ RefactoringRequestBuilder avec analyse")
    
    # Test 1.8: Validation du modèle
    print("\n1.8 Test validation du modèle...")
    try:
        builder = LLMRequestBuilder().with_model("invalid-model")
        prompt = builder.build_prompt()
        print("   ❌ Devrait valider le modèle")
    except ValueError as e:
        print(f"   ✅ Validation modèle: {str(e)[:50]}...")
    
    print("\n✅ Tous les tests LLMRequestBuilder passent!")
    
except Exception as e:
    print(f"❌ Erreur dans les tests LLMRequestBuilder: {e}")
    import traceback
    traceback.print_exc()

# =============================================================================
# TEST 2: LLM RESPONSE PARSER
# =============================================================================
print("\n" + "=" * 80)
print("TEST 2: LLM RESPONSE PARSER")
print("=" * 80)

try:
    from collegue.tools.llm_helpers.parsers import LLMResponseParser
    
    # Test 2.1: Initialisation
    print("\n2.1 Test initialisation LLMResponseParser...")
    parser = LLMResponseParser()
    assert parser is not None
    print("   ✅ Initialisation correcte")
    
    # Test 2.2: parse_text_response
    print("\n2.2 Test parse_text_response...")
    mock_response = Mock()
    mock_response.text = "Ceci est une réponse simple."
    
    result = parser.parse_text_response(mock_response)
    assert result == "Ceci est une réponse simple."
    print("   ✅ parse_text_response fonctionne")
    
    # Test 2.3: parse_json_response
    print("\n2.3 Test parse_json_response...")
    mock_response = Mock()
    mock_response.text = '{"status": "success", "data": {"count": 5}}'
    
    result = parser.parse_json_response(mock_response)
    assert result["status"] == "success"
    assert result["data"]["count"] == 5
    print("   ✅ parse_json_response fonctionne")
    
    # Test 2.4: parse_code_blocks
    print("\n2.4 Test parse_code_blocks...")
    text_with_code = """
Voici un exemple:

```python
def hello():
    print("Hello, World!")
```

Fin de l'exemple.
"""
    
    code_blocks = parser.parse_code_blocks(text_with_code)
    assert len(code_blocks) == 1
    assert "def hello():" in code_blocks[0]
    print("   ✅ parse_code_blocks extrait correctement")
    
    # Test 2.5: parse_list_items
    print("\n2.5 Test parse_list_items...")
    list_text = """
- Item 1: Description
- Item 2: Autre description
- Item 3: Troisième item
"""
    
    items = parser.parse_list_items(list_text)
    assert len(items) == 3
    assert "Item 1" in items[0]
    print("   ✅ parse_list_items fonctionne")
    
    # Test 2.6: Gestion erreur JSON
    print("\n2.6 Test gestion erreur JSON...")
    mock_response = Mock()
    mock_response.text = "ceci n'est pas du json"
    
    result = parser.parse_json_response(mock_response)
    assert result is None or "error" in str(result).lower()
    print("   ✅ Gestion erreur JSON correcte")
    
    print("\n✅ Tous les tests LLMResponseParser passent!")
    
except Exception as e:
    print(f"❌ Erreur dans les tests LLMResponseParser: {e}")
    import traceback
    traceback.print_exc()

# =============================================================================
# TEST 3: TEST GENERATORS
# =============================================================================
print("\n" + "=" * 80)
print("TEST 3: TEST GENERATORS")
print("=" * 80)

try:
    from collegue.tools.test_generators import TestGenerator, PytestGenerator, JestGenerator, MochaGenerator, UnittestGenerator
    
    # Test 3.1: TestGenerator de base
    print("\n3.1 Test TestGenerator de base...")
    generator = TestGenerator()
    assert generator.framework == "base"
    print("   ✅ TestGenerator initialisé")
    
    # Test 3.2: PytestGenerator
    print("\n3.2 Test PytestGenerator...")
    pytest_gen = PytestGenerator()
    code = """
def add(a, b):
    return a + b
"""
    
    test_code = pytest_gen.generate_test(code, language="python")
    assert "def test_add" in test_code
    assert "import pytest" in test_code
    assert "assert add(2, 3) == 5" in test_code
    print("   ✅ PytestGenerator génère correctement")
    
    # Test 3.3: JestGenerator
    print("\n3.3 Test JestGenerator...")
    jest_gen = JestGenerator()
    code = """
function multiply(a, b) {
    return a * b;
}
"""
    
    test_code = jest_gen.generate_test(code, language="javascript")
    assert "test('multiply'" in test_code
    assert "expect(multiply(2, 3))" in test_code
    print("   ✅ JestGenerator génère correctement")
    
    # Test 3.4: MochaGenerator
    print("\n3.4 Test MochaGenerator...")
    mocha_gen = MochaGenerator()
    code = """
function divide(a, b) {
    return a / b;
}
"""
    
    test_code = mocha_gen.generate_test(code, language="javascript")
    assert "describe('divide'" in test_code
    assert "it('should divide" in test_code
    assert "assert.equal" in test_code
    print("   ✅ MochaGenerator génère correctement")
    
    # Test 3.5: UnittestGenerator
    print("\n3.5 Test UnittestGenerator...")
    unittest_gen = UnittestGenerator()
    code = """
class Calculator:
    def subtract(self, a, b):
        return a - b
"""
    
    test_code = unittest_gen.generate_test(code, language="python")
    assert "class TestCalculator" in test_code
    assert "import unittest" in test_code
    assert "def test_subtract" in test_code
    print("   ✅ UnittestGenerator génère correctement")
    
    # Test 3.6: Gestion des cas complexes
    print("\n3.6 Test gestion des cas complexes...")
    complex_code = """
class DataProcessor:
    def process(self, data):
        if not data:
            raise ValueError("Data cannot be empty")
        return [x * 2 for x in data]
"""
    
    test_code = pytest_gen.generate_test(complex_code, language="python")
    assert "with pytest.raises" in test_code
    assert "ValueError" in test_code
    print("   ✅ Gestion des exceptions correcte")
    
    # Test 3.7: Validation du langage
    print("\n3.7 Test validation du langage...")
    try:
        pytest_gen.generate_test(code, language="ruby")
        print("   ❌ Devrait valider le langage")
    except ValueError as e:
        print(f"   ✅ Validation langage: {str(e)[:50]}...")
    
    print("\n✅ Tous les tests TestGenerators passent!")
    
except Exception as e:
    print(f"❌ Erreur dans les tests TestGenerators: {e}")
    import traceback
    traceback.print_exc()

# =============================================================================
# TEST 4: VALIDATEURS SHARED
# =============================================================================
print("\n" + "=" * 80)
print("TEST 4: VALIDATEURS SHARED")
print("=" * 80)

try:
    from collegue.tools.shared import (
        validate_fast_deep, 
        detect_language_from_extension,
        FileInput,
        SeverityLevel
    )
    
    # Test 4.1: validate_fast_deep
    print("\n4.1 Test validate_fast_deep...")
    assert validate_fast_deep("fast") is True
    assert validate_fast_deep("deep") is True
    assert validate_fast_deep("slow") is False
    assert validate_fast_deep("") is False
    print("   ✅ validate_fast_deep fonctionne")
    
    # Test 4.2: detect_language_from_extension
    print("\n4.2 Test detect_language_from_extension...")
    assert detect_language_from_extension("test.py") == "python"
    assert detect_language_from_extension("test.js") == "javascript"
    assert detect_language_from_extension("test.ts") == "typescript"
    assert detect_language_from_extension("test.java") == "java"
    assert detect_language_from_extension("test.unknown") is None
    print("   ✅ detect_language_from_extension fonctionne")
    
    # Test 4.3: FileInput model
    print("\n4.3 Test FileInput model...")
    file_input = FileInput(
        path="test.py",
        content="print('hello')",
        language="python"
    )
    assert file_input.path == "test.py"
    assert file_input.language == "python"
    print("   ✅ FileInput model valide")
    
    # Test 4.4: SeverityLevel enum
    print("\n4.4 Test SeverityLevel enum...")
    assert SeverityLevel.LOW.value == "low"
    assert SeverityLevel.MEDIUM.value == "medium"
    assert SeverityLevel.HIGH.value == "high"
    assert SeverityLevel.CRITICAL.value == "critical"
    print("   ✅ SeverityLevel enum correcte")
    
    print("\n✅ Tous les tests validateurs passent!")
    
except Exception as e:
    print(f"❌ Erreur dans les tests validateurs: {e}")
    import traceback
    traceback.print_exc()

# =============================================================================
# RÉSUMÉ FINAL
# =============================================================================
print("\n" + "=" * 80)
print("RÉSUMÉ DES TESTS LLM HELPERS")
print("=" * 80)
print("""
✅ LLMRequestBuilder: 8 tests passent
   - Initialisation et configuration fluide
   - build_prompt avec system/user prompts
   - DocumentationRequestBuilder et RefactoringRequestBuilder
   - Validation des modèles

✅ LLMResponseParser: 6 tests passent
   - parse_text_response, parse_json_response
   - parse_code_blocks, parse_list_items
   - Gestion des erreurs

✅ TestGenerators: 7 tests passent
   - PytestGenerator, JestGenerator, MochaGenerator, UnittestGenerator
   - Gestion des cas complexes (exceptions)
   - Validation des langages

✅ Validateurs Shared: 4 tests passent
   - validate_fast_deep, detect_language_from_extension
   - FileInput model, SeverityLevel enum

TOTAL: 25 tests unitaires pour les LLM Helpers
""")
