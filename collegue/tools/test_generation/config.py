"""
Configuration et constantes pour l'outil Test Generation.
"""

# Frameworks de test supportés par langage
TEST_FRAMEWORKS = {
    "python": ["unittest", "pytest", "nose2"],
    "javascript": ["jest", "mocha", "jasmine", "vitest"],
    "typescript": ["jest", "mocha", "jasmine", "vitest"],
    "java": ["junit", "testng", "spock"],
    "c#": ["nunit", "xunit", "mstest"],
    "php": ["phpunit", "pest", "codeception", "behat", "phpspec", "kahlan"]
}

# Framework par défaut pour chaque langage
DEFAULT_FRAMEWORKS = {
    "python": "pytest",
    "javascript": "jest",
    "typescript": "jest",
    "java": "junit",
    "c#": "nunit",
    "php": "phpunit"
}

# Templates d'imports par langage/framework
IMPORT_TEMPLATES = {
    "python": {
        "pytest": "import pytest\nfrom {module} import {class_name}",
        "unittest": "import unittest\nfrom {module} import {class_name}",
    },
    "javascript": {
        "jest": "const {class_name} = require('./{module}');",
        "mocha": "const {assert} = require('chai');\nconst {class_name} = require('./{module}');",
    },
    "typescript": {
        "jest": "import {{ {class_name} }} from './{module}';",
    },
    "java": {
        "junit": "import org.junit.jupiter.api.Test;\nimport org.junit.jupiter.api.BeforeEach;\nimport static org.junit.jupiter.api.Assertions.*;",
    },
    "c#": {
        "nunit": "using NUnit.Framework;",
        "xunit": "using Xunit;",
    },
    "php": {
        "phpunit": "use PHPUnit\\Framework\\TestCase;",
        "pest": "",
    }
}

# Templates de tests par langage/framework
TEST_TEMPLATES = {
    "python": {
        "pytest": '''
def test_{function_name}():
    # Arrange
    {arrange_code}
    
    # Act
    {act_code}
    
    # Assert
    {assert_code}
''',
        "unittest": '''
class Test{class_name}(unittest.TestCase):
    def test_{function_name}(self):
        # Arrange
        {arrange_code}
        
        # Act
        {act_code}
        
        # Assert
        {assert_code}
''',
    },
    "javascript": {
        "jest": '''
describe('{class_name}', () => {{
    test('{function_name}', () => {{
        // Arrange
        {arrange_code}
        
        // Act
        {act_code}
        
        // Assert
        {assert_code}
    }});
}});
''',
    },
    "typescript": {
        "jest": '''
describe('{class_name}', () => {{
    test('{function_name}', () => {{
        // Arrange
        {arrange_code}
        
        // Act
        {act_code}
        
        // Assert
        {assert_code}
    }});
}});
''',
    },
    "php": {
        "phpunit": '''
class {class_name}Test extends TestCase
{{
    public function test{function_name}(): void
    {{
        // Arrange
        {arrange_code}
        
        // Act
        {act_code}
        
        // Assert
        {assert_code}
    }}
}}
''',
        "pest": '''
test('{function_name}', function () {{
    // Arrange
    {arrange_code}
    
    // Act
    {act_code}
    
    // Assert
    {assert_code}
}});
''',
    }
}

# Instructions de génération de tests par langage
LANGUAGE_TEST_INSTRUCTIONS = {
    "python": """
- Utilise pytest (fonctions de test) ou unittest (classes de test)
- Inclus des tests pour cas normaux, cas limites et cas d'erreur
- Utilise parametrize pour les cas de test multiples
- Nomme les tests avec préfixe test_
- Utilise des fixtures si nécessaire
- Assure-toi que les tests sont exécutables immédiatement
""",
    "javascript": """
- Utilise Jest ou framework spécifié
- Utilise describe/test ou it pour structurer
- Inclus des tests pour cas normaux, cas limites et erreurs
- Utilise async/await pour les fonctions asynchrones
- Mock les dépendances externes avec jest.mock()
""",
    "typescript": """
- Utilise Jest ou framework spécifié
- Inclus les types dans les tests
- Utilise describe/test pour structurer
- Mock les dépendances avec des types appropriés
""",
    "java": """
- Utilise JUnit 5 (Jupiter)
- Utilise @Test, @BeforeEach, @AfterEach
- Utilise assertEquals, assertTrue, assertThrows, etc.
- Inclus des tests paramétrés si pertinent
""",
    "c#": """
- Utilise NUnit ou XUnit selon le framework demandé
- Utilise [Test], [SetUp], [TearDown]
- Utilise Assert.AreEqual, Assert.IsTrue, etc.
- Inclus des cas de test variés
""",
    "php": """
- Utilise PHPUnit ou Pest selon le framework demandé
- Pour PHPUnit: extends TestCase, méthodes test*CamelCase
- Pour Pest: syntaxe fonctionnelle test('nom', fn() {})
- Inclus des tests pour cas normaux, exceptions et cas limites
- Utilise les assertions appropriées (assertEquals, assertTrue, expect()->toBe())
"""
}
