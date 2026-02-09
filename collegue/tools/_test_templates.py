"""
Templates de génération de tests par langage et framework.

Contient les générateurs de code de test fallback (sans LLM) pour :
- Python: unittest, pytest
- JavaScript: jest, mocha
- TypeScript: jest, mocha
- Générique
"""
from typing import Dict, Any, List


def generate_unittest_tests(
    code: str,
    functions: List[Dict[str, Any]],
    classes: List[Dict[str, Any]],
    include_mocks: bool,
) -> str:
    module_name = "module_test"

    test_code = """\"\"\"
Tests unitaires générés automatiquement avec unittest
\"\"\"
import unittest
from unittest.mock import MagicMock, patch
"""

    test_code += f"from {module_name} import "

    elements_to_import = []
    for func in functions:
        elements_to_import.append(func["name"])
    for cls in classes:
        elements_to_import.append(cls["name"])

    if elements_to_import:
        test_code += f", {', '.join(elements_to_import)}\n\n"
    else:
        test_code += "*\n\n"

    if functions:
        test_code += "class TestFunctions(unittest.TestCase):\n"
        test_code += "    \"\"\"Tests pour les fonctions.\"\"\"\n\n"

        for func in functions:
            func_name = func["name"]
            params = func.get("params", "")

            test_code += f"    def test_{func_name}(self):\n"
            test_code += f"        \"\"\"Test pour la fonction {func_name}.\"\"\"\n"

            test_values = _infer_python_test_values(params)

            if include_mocks and params:
                test_code += "        # Créer des mocks pour les dépendances\n"
                test_code += "        mock_dependency = MagicMock()\n\n"

            test_code += "        # Exécuter la fonction à tester\n"
            test_code += f"        result = {func_name}({', '.join(test_values)})\n\n"

            test_code += "        # Vérifier les résultats\n"
            test_code += "        self.assertIsNotNone(result)\n\n"

        test_code += "\n"

    for cls in classes:
        class_name = cls["name"]

        test_code += f"class Test{class_name}(unittest.TestCase):\n"
        test_code += f"    \"\"\"Tests pour la classe {class_name}.\"\"\"\n\n"

        test_code += "    def setUp(self):\n"
        test_code += "        \"\"\"Initialisation des tests.\"\"\"\n"
        test_code += f"        self.instance = {class_name}()\n\n"

        test_code += "    def test_initialization(self):\n"
        test_code += f"        \"\"\"Test de l'initialisation de {class_name}.\"\"\"\n"
        test_code += f"        self.assertIsInstance(self.instance, {class_name})\n\n"

        test_code += "    def test_methods(self):\n"
        test_code += f"        \"\"\"Test des méthodes de {class_name}.\"\"\"\n"
        test_code += "        # Ajouter des tests pour les méthodes spécifiques\n"
        test_code += "        pass\n\n"

    test_code += "if __name__ == '__main__':\n"
    test_code += "    unittest.main()"

    return test_code


def generate_pytest_tests(
    code: str,
    functions: List[Dict[str, Any]],
    classes: List[Dict[str, Any]],
    include_mocks: bool,
) -> str:
    module_name = "module_test"

    test_code = """\"\"\"
Tests unitaires générés automatiquement avec pytest
\"\"\"
import pytest
from unittest.mock import MagicMock, patch
"""

    test_code += f"from {module_name} import "

    elements_to_import = []
    for func in functions:
        elements_to_import.append(func["name"])
    for cls in classes:
        elements_to_import.append(cls["name"])

    if elements_to_import:
        test_code += f", {', '.join(elements_to_import)}\n\n"
    else:
        test_code += "*\n\n"

    if classes:
        test_code += "@pytest.fixture\n"
        for cls in classes:
            class_name = cls["name"]
            test_code += f"def {class_name.lower()}_instance():\n"
            test_code += f"    \"\"\"Fixture pour créer une instance de {class_name}.\"\"\"\n"
            test_code += f"    return {class_name}()\n\n"

    for func in functions:
        func_name = func["name"]
        params = func.get("params", "")

        test_code += f"def test_{func_name}():\n"
        test_code += f"    \"\"\"Test pour la fonction {func_name}.\"\"\"\n"

        test_values = _infer_python_test_values(params)

        if include_mocks and params:
            test_code += "    # Créer des mocks pour les dépendances\n"
            test_code += "    mock_dependency = MagicMock()\n\n"

        test_code += "    # Exécuter la fonction à tester\n"
        test_code += f"    result = {func_name}({', '.join(test_values)})\n\n"

        test_code += "    # Vérifier les résultats\n"
        test_code += "    assert result is not None\n\n"

    for cls in classes:
        class_name = cls["name"]

        test_code += f"def test_{class_name.lower()}_initialization({class_name.lower()}_instance):\n"
        test_code += f"    \"\"\"Test de l'initialisation de {class_name}.\"\"\"\n"
        test_code += f"    assert isinstance({class_name.lower()}_instance, {class_name})\n\n"

        test_code += f"def test_{class_name.lower()}_methods({class_name.lower()}_instance):\n"
        test_code += f"    \"\"\"Test des méthodes de {class_name}.\"\"\"\n"
        test_code += "    # Ajouter des tests pour les méthodes spécifiques\n"
        test_code += "    pass\n\n"

    return test_code


def generate_jest_tests(
    code: str,
    functions: List[Dict[str, Any]],
    classes: List[Dict[str, Any]],
    include_mocks: bool,
) -> str:
    test_code = "// Tests générés automatiquement avec Jest\n\n"

    if include_mocks:
        test_code += "// Mocks\njest.mock('./module');\n\n"

    if functions:
        test_code += "// Tests des fonctions\n"
        test_code += "describe('Functions', () => {\n"
        for func in functions:
            func_name = func["name"]
            test_code += f"  test('{func_name} should work correctly', () => {{\n"
            test_code += f"    // TODO: Implémenter le test pour {func_name}\n"
            test_code += f"    expect({func_name}).toBeDefined();\n"
            test_code += "  });\n\n"
        test_code += "});\n\n"

    if classes:
        test_code += "// Tests des classes\n"
        for cls in classes:
            class_name = cls["name"]
            test_code += f"describe('{class_name}', () => {{\n"
            test_code += f"  test('should instantiate correctly', () => {{\n"
            test_code += f"    const instance = new {class_name}();\n"
            test_code += f"    expect(instance).toBeInstanceOf({class_name});\n"
            test_code += "  });\n"
            test_code += "});\n\n"

    return test_code


def generate_mocha_tests(
    code: str,
    functions: List[Dict[str, Any]],
    classes: List[Dict[str, Any]],
    include_mocks: bool,
) -> str:
    test_code = "// Tests générés automatiquement avec Mocha\n"
    test_code += "const { expect } = require('chai');\n\n"

    if include_mocks:
        test_code += "// Configuration des mocks\nconst sinon = require('sinon');\n\n"

    if functions:
        test_code += "describe('Functions', function() {\n"
        for func in functions:
            func_name = func["name"]
            test_code += f"  describe('{func_name}', function() {{\n"
            test_code += f"    it('should work correctly', function() {{\n"
            test_code += f"      // TODO: Implémenter le test pour {func_name}\n"
            test_code += f"      expect({func_name}).to.exist;\n"
            test_code += "    });\n"
            test_code += "  });\n"
        test_code += "});\n\n"

    if classes:
        for cls in classes:
            class_name = cls["name"]
            test_code += f"describe('{class_name}', function() {{\n"
            test_code += f"  it('should instantiate correctly', function() {{\n"
            test_code += f"    const instance = new {class_name}();\n"
            test_code += f"    expect(instance).to.be.instanceOf({class_name});\n"
            test_code += "  });\n"
            test_code += "});\n\n"

    return test_code


def generate_typescript_jest_tests(
    code: str,
    functions: List[Dict[str, Any]],
    classes: List[Dict[str, Any]],
    interfaces: List[Dict[str, Any]],
    types: List[Dict[str, Any]],
    include_mocks: bool,
) -> str:
    module_name = "module"

    test_code = "/**\n * Tests unitaires générés automatiquement avec Jest pour TypeScript\n */\n"

    test_code += "import { expect } from '@jest/globals';\n"

    if functions or classes or interfaces or types:
        test_code += "// Import du module à tester\n"

        elements_to_import = []
        for func in functions:
            elements_to_import.append(func["name"])
        for cls in classes:
            elements_to_import.append(cls["name"])
        for interface in interfaces:
            elements_to_import.append(interface["name"])
        for t in types:
            elements_to_import.append(t["name"])

        if elements_to_import:
            test_code += f"import {{ {', '.join(elements_to_import)} }} from './{module_name}';\n\n"

    if include_mocks:
        test_code += "// Configuration des mocks\n"
        test_code += "jest.mock('./module');\n\n"

    for func in functions:
        func_name = func["name"]

        test_code += f"// Tests pour la fonction {func_name}\n"
        test_code += f"describe('{func_name}', () => {{\n"

        test_code += "  it('should be defined', () => {\n"
        test_code += f"    expect({func_name}).toBeDefined();\n"
        test_code += "  });\n\n"

        test_code += "  it('should return expected result', () => {\n"

        param_values = _infer_ts_param_values(func.get("params", ""))
        return_type = func.get("return_type", "void")

        func_call = f"{func_name}({', '.join(param_values)})"

        if return_type and return_type != "void":
            if "string" in return_type:
                test_code += f"    expect(typeof {func_call}).toBe('string');\n"
            elif "number" in return_type:
                test_code += f"    expect(typeof {func_call}).toBe('number');\n"
            elif "boolean" in return_type:
                test_code += f"    expect(typeof {func_call}).toBe('boolean');\n"
            elif "[]" in return_type or "Array" in return_type:
                test_code += f"    expect(Array.isArray({func_call})).toBe(true);\n"
            elif "Promise" in return_type:
                test_code += f"    return {func_call}.then(result => {{\n"
                test_code += "      expect(result).toBeDefined();\n"
                test_code += "    });\n"
            else:
                test_code += f"    expect({func_call}).toBeDefined();\n"
        else:
            test_code += f"    {func_call};\n"
            test_code += "    expect(true).toBe(true); // Vérifier que la fonction s'exécute sans erreur\n"

        test_code += "  });\n"

        test_code += "\n  it('should handle error cases', () => {\n"
        test_code += "    // Ajouter des tests pour les cas d'erreur\n"
        test_code += "  });\n"

        test_code += "});\n\n"

    for cls in classes:
        class_name = cls["name"]

        test_code += f"// Tests pour la classe {class_name}\n"
        test_code += f"describe('{class_name}', () => {{\n"
        test_code += f"  let instance: {class_name};\n\n"

        test_code += "  beforeEach(() => {\n"
        test_code += f"    instance = new {class_name}();\n"
        test_code += "  });\n\n"

        test_code += "  it('should initialize correctly', () => {\n"
        test_code += f"    expect(instance).toBeInstanceOf({class_name});\n"
        test_code += "  });\n\n"

        test_code += "  it('should have expected methods', () => {\n"
        test_code += "    // Ajouter des tests pour les méthodes spécifiques\n"
        test_code += "  });\n"
        test_code += "});\n\n"

    for interface in interfaces:
        interface_name = interface["name"]

        test_code += f"// Tests pour l'interface {interface_name}\n"
        test_code += f"describe('{interface_name}', () => {{\n"
        test_code += f"  let mockImplementation: {interface_name};\n\n"

        test_code += "  beforeEach(() => {\n"
        test_code += f"    mockImplementation = {{\n"
        test_code += "      // Implémenter les propriétés requises par l'interface\n"
        test_code += "    }} as " + interface_name + ";\n"
        test_code += "  });\n\n"

        test_code += "  it('should be able to create an implementation', () => {\n"
        test_code += f"    expect(mockImplementation).toBeDefined();\n"
        test_code += "  });\n\n"

        if interface.get("extends"):
            parent_interface = interface.get("extends")
            test_code += f"\n  it('should extend {parent_interface}', () => {{\n"
            test_code += "    // Vérifier que l'implémentation contient les propriétés de l'interface parente\n"
            test_code += f"    const parentProps: Array<keyof {parent_interface}> = [];\n"
            test_code += "    parentProps.forEach(prop => {\n"
            test_code += "      expect(mockImplementation[prop]).toBeDefined();\n"
            test_code += "    });\n"
            test_code += "  });\n"

        test_code += "});\n\n"

    for t in types:
        type_name = t["name"]

        test_code += f"// Tests pour le type {type_name}\n"
        test_code += f"describe('{type_name}', () => {{\n"
        test_code += f"  let instance: {type_name};\n\n"

        test_code += "  beforeEach(() => {\n"
        test_code += f"    instance = {{\n"
        test_code += "      // Initialiser avec des valeurs valides pour ce type\n"
        test_code += "    }} as " + type_name + ";\n"
        test_code += "  });\n\n"

        test_code += "  it('should be a valid type', () => {\n"
        test_code += f"    const typeCheck = (value: {type_name}): boolean => true;\n"
        test_code += "    expect(typeCheck(instance)).toBe(true);\n"
        test_code += "  });\n"

        test_code += "});\n\n"

    return test_code


def generate_typescript_mocha_tests(
    code: str,
    functions: List[Dict[str, Any]],
    classes: List[Dict[str, Any]],
    interfaces: List[Dict[str, Any]],
    types: List[Dict[str, Any]],
    include_mocks: bool,
) -> str:
    module_name = "module"

    test_code = "/**\n * Tests unitaires générés automatiquement avec Mocha pour TypeScript\n */\n"

    test_code += "import { expect } from 'chai';\n"

    if functions or classes or interfaces or types:
        test_code += "// Import du module à tester\n"

        elements_to_import = []
        for func in functions:
            elements_to_import.append(func["name"])
        for cls in classes:
            elements_to_import.append(cls["name"])
        for interface in interfaces:
            elements_to_import.append(interface["name"])
        for t in types:
            elements_to_import.append(t["name"])

        if elements_to_import:
            test_code += f"import {{ {', '.join(elements_to_import)} }} from './{module_name}';\n\n"

    if include_mocks:
        test_code += "// Configuration des mocks\n"
        test_code += "import * as sinon from 'sinon';\n\n"

    for func in functions:
        func_name = func["name"]

        test_code += f"// Tests pour la fonction {func_name}\n"
        test_code += f"describe('{func_name}', () => {{\n"

        test_code += "  it('should be defined', () => {\n"
        test_code += f"    expect({func_name}).to.exist;\n"
        test_code += "  });\n\n"

        test_code += "  it('should return expected result', () => {\n"

        param_values = _infer_ts_param_values(func.get("params", ""))
        return_type = func.get("return_type", "void")

        func_call = f"{func_name}({', '.join(param_values)})"

        if return_type and return_type != "void":
            if "string" in return_type:
                test_code += f"    expect(typeof {func_call}).toBe('string');\n"
            elif "number" in return_type:
                test_code += f"    expect(typeof {func_call}).toBe('number');\n"
            elif "boolean" in return_type:
                test_code += f"    expect(typeof {func_call}).toBe('boolean');\n"
            elif "[]" in return_type or "Array" in return_type:
                test_code += f"    expect(Array.isArray({func_call})).toBe(true);\n"
            elif "Promise" in return_type:
                test_code += f"    return {func_call}.then(result => {{\n"
                test_code += "      expect(result).to.exist;\n"
                test_code += "    });\n"
            else:
                test_code += f"    expect({func_call}).to.exist;\n"
        else:
            test_code += f"    {func_call};\n"
            test_code += "    expect(true).toBe(true); // Vérifier que la fonction s'exécute sans erreur\n"

        test_code += "  });\n"

        test_code += "\n  it('should handle error cases', () => {\n"
        test_code += "    // Ajouter des tests pour les cas d'erreur\n"
        test_code += "  });\n"

        test_code += "});\n\n"

    for cls in classes:
        class_name = cls["name"]

        test_code += f"// Tests pour la classe {class_name}\n"
        test_code += f"describe('{class_name}', () => {{\n"
        test_code += f"  let instance: {class_name};\n\n"

        test_code += "  beforeEach(() => {\n"
        test_code += f"    instance = new {class_name}();\n"
        test_code += "  });\n\n"

        test_code += "  it('should initialize correctly', () => {\n"
        test_code += f"    expect(instance).toBeInstanceOf({class_name});\n"
        test_code += "  });\n\n"

        test_code += "  it('should have expected methods', () => {\n"
        test_code += "    // Ajouter des tests pour les méthodes spécifiques\n"
        test_code += "  });\n"
        test_code += "});\n\n"

    for interface in interfaces:
        interface_name = interface["name"]

        test_code += f"// Tests pour l'interface {interface_name}\n"
        test_code += f"describe('{interface_name}', () => {{\n"
        test_code += f"  let mockImplementation: {interface_name};\n\n"

        test_code += "  beforeEach(() => {\n"
        test_code += f"    mockImplementation = {{\n"
        test_code += "      // Implémenter les propriétés requises par l'interface\n"
        test_code += "    }} as " + interface_name + ";\n"
        test_code += "  });\n\n"

        test_code += "  it('should be able to create an implementation', () => {\n"
        test_code += f"    expect(mockImplementation).to.exist;\n"
        test_code += "  });\n\n"

        if interface.get("extends"):
            parent_interface = interface.get("extends")
            test_code += f"\n  it('should extend {parent_interface}', () => {{\n"
            test_code += "    // Vérifier que l'implémentation contient les propriétés de l'interface parente\n"
            test_code += f"    const parentProps: Array<keyof {parent_interface}> = [];\n"
            test_code += "    parentProps.forEach(prop => {\n"
            test_code += "      expect(mockImplementation[prop]).to.exist;\n"
            test_code += "    });\n"
            test_code += "  });\n"

        test_code += "});\n\n"

    for t in types:
        type_name = t["name"]

        test_code += f"// Tests pour le type {type_name}\n"
        test_code += f"describe('{type_name}', () => {{\n"
        test_code += f"  let instance: {type_name};\n\n"

        test_code += "  beforeEach(() => {\n"
        test_code += f"    instance = {{\n"
        test_code += "      // Initialiser avec des valeurs valides pour ce type\n"
        test_code += "    }} as " + type_name + ";\n"
        test_code += "  });\n\n"

        test_code += "  it('should be a valid type', () => {\n"
        test_code += f"    const typeCheck = (value: {type_name}): boolean => true;\n"
        test_code += "    expect(typeCheck(instance)).toBe(true);\n"
        test_code += "  });\n"

        test_code += "});\n\n"

    return test_code


def generate_generic_tests(code: str, language: str, framework: str) -> str:
    return f"""
// Tests générés automatiquement pour {language}
// Framework: {framework}

// TODO: Implémenter les tests spécifiques pour {language}
// Code source à tester:
/*
{code}
*/

// Exemple de structure de test:
function testExample() {{
    // Ajouter les tests appropriés ici
    console.log("Tests à implémenter pour {language}");
}}
"""


def _infer_python_test_values(params: str) -> List[str]:
    test_values = []
    for param in params.split(","):
        param = param.strip()
        if not param:
            continue
        if "str" in param or "name" in param or "text" in param:
            test_values.append('"test"')
        elif "int" in param or "num" in param or "count" in param:
            test_values.append("42")
        elif "float" in param or "price" in param or "amount" in param:
            test_values.append("3.14")
        elif "bool" in param or "flag" in param or "is_" in param:
            test_values.append("True")
        elif "list" in param or "array" in param:
            test_values.append("[1, 2, 3]")
        elif "dict" in param or "map" in param:
            test_values.append('{"key": "value"}')
        else:
            test_values.append("None")
    return test_values


def _infer_ts_param_values(params: str) -> List[str]:
    param_values = []
    if not params:
        return param_values
    for param in params.split(","):
        param = param.strip()
        if ":" in param:
            param_name, param_type = param.split(":", 1)
            param_name = param_name.strip()
            param_type = param_type.strip()
            if "string" in param_type:
                param_values.append(f"'test{param_name}'")
            elif "number" in param_type:
                param_values.append("42")
            elif "boolean" in param_type:
                param_values.append("true")
            elif "[]" in param_type or "Array" in param_type:
                param_values.append("[]")
            elif "object" in param_type or "{" in param_type:
                param_values.append("{}")
            else:
                param_values.append("undefined")
        else:
            param_values.append("undefined")
    return param_values
