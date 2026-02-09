"""
Tests unitaires pour les modèles du système de prompts personnalisés
"""
import unittest
import uuid
from datetime import datetime

from collegue.prompts.engine.models import (
    PromptVariable,
    PromptVariableType,
    PromptTemplate,
    PromptCategory,
    PromptExecution,
    PromptLibrary
)


class TestPromptVariable(unittest.TestCase):
    """Tests pour la classe PromptVariable."""

    def test_create_variable(self):
        """Test de création d'une variable de prompt."""
        var = PromptVariable(
            name="test_var",
            description="Variable de test",
            type=PromptVariableType.STRING,
            required=True
        )

        self.assertEqual(var.name, "test_var")
        self.assertEqual(var.description, "Variable de test")
        self.assertEqual(var.type, PromptVariableType.STRING)
        self.assertTrue(var.required)
        self.assertIsNone(var.default)
        self.assertIsNone(var.options)
        self.assertIsNone(var.example)

    def test_variable_with_options(self):
        """Test d'une variable avec options."""
        var = PromptVariable(
            name="level",
            description="Niveau de détail",
            type=PromptVariableType.STRING,
            required=False,
            default="standard",
            options=["simple", "standard", "détaillé"]
        )

        self.assertEqual(var.name, "level")
        self.assertEqual(var.default, "standard")
        self.assertEqual(var.options, ["simple", "standard", "détaillé"])

    def test_variable_validation(self):
        """Test de validation des types de variables."""
        var_string = PromptVariable(
            name="name",
            description="Nom",
            type=PromptVariableType.STRING,
            required=True
        )

        var_int = PromptVariable(
            name="age",
            description="Âge",
            type=PromptVariableType.INTEGER,
            required=True
        )

        var_bool = PromptVariable(
            name="active",
            description="Actif",
            type=PromptVariableType.BOOLEAN,
            required=True
        )

        self.assertEqual(var_string.type, PromptVariableType.STRING)
        self.assertEqual(var_int.type, PromptVariableType.INTEGER)
        self.assertEqual(var_bool.type, PromptVariableType.BOOLEAN)


class TestPromptTemplate(unittest.TestCase):
    """Tests pour la classe PromptTemplate."""

    def test_create_template(self):
        """Test de création d'un template de prompt."""
        template = PromptTemplate(
            id="test_template",
            name="Template de test",
            description="Un template pour les tests",
            template="Ceci est un {test} de template avec {variable}",
            variables=[
                PromptVariable(
                    name="test",
                    description="Variable de test",
                    type=PromptVariableType.STRING,
                    required=True
                ),
                PromptVariable(
                    name="variable",
                    description="Autre variable",
                    type=PromptVariableType.STRING,
                    required=True
                )
            ],
            category="test",
            tags=["test", "exemple"]
        )

        self.assertEqual(template.id, "test_template")
        self.assertEqual(template.name, "Template de test")
        self.assertEqual(template.description, "Un template pour les tests")
        self.assertEqual(template.template, "Ceci est un {test} de template avec {variable}")
        self.assertEqual(len(template.variables), 2)
        self.assertEqual(template.category, "test")
        self.assertEqual(template.tags, ["test", "exemple"])
        self.assertEqual(template.version, "1.0.0")
        self.assertFalse(template.is_public)

    def test_template_with_provider_specific(self):
        """Test d'un template avec des versions spécifiques par fournisseur."""
        template = PromptTemplate(
            id="test_provider",
            name="Test avec fournisseurs",
            description="Template avec versions spécifiques",
            template="Version par défaut: {var}",
            variables=[
                PromptVariable(
                    name="var",
                    description="Variable",
                    type=PromptVariableType.STRING,
                    required=True
                )
            ],
            category="test",
            provider_specific={
                "openai": "Version OpenAI: {var}",
                "anthropic": "Version Anthropic: {var}"
            }
        )

        self.assertEqual(template.template, "Version par défaut: {var}")
        self.assertEqual(template.provider_specific["openai"], "Version OpenAI: {var}")
        self.assertEqual(template.provider_specific["anthropic"], "Version Anthropic: {var}")


class TestPromptCategory(unittest.TestCase):
    """Tests pour la classe PromptCategory."""

    def test_create_category(self):
        """Test de création d'une catégorie."""
        category = PromptCategory(
            id="test_category",
            name="Catégorie de test",
            description="Une catégorie pour les tests"
        )

        self.assertEqual(category.id, "test_category")
        self.assertEqual(category.name, "Catégorie de test")
        self.assertEqual(category.description, "Une catégorie pour les tests")
        self.assertIsNone(category.parent_id)
        self.assertIsNone(category.icon)

    def test_category_with_parent(self):
        """Test d'une catégorie avec parent."""
        category = PromptCategory(
            id="sub_category",
            name="Sous-catégorie",
            description="Une sous-catégorie",
            parent_id="parent_category",
            icon="folder"
        )

        self.assertEqual(category.id, "sub_category")
        self.assertEqual(category.parent_id, "parent_category")
        self.assertEqual(category.icon, "folder")


class TestPromptExecution(unittest.TestCase):
    """Tests pour la classe PromptExecution."""

    def test_create_execution(self):
        """Test de création d'une exécution de prompt."""
        execution_id = str(uuid.uuid4())
        template_id = "test_template"

        execution = PromptExecution(
            id=execution_id,
            template_id=template_id,
            variables={"test": "valeur", "variable": "autre"},
            formatted_prompt="Ceci est un valeur de template avec autre",
            execution_time=0.5,
            timestamp=datetime.now(),
            provider="openai"
        )

        self.assertEqual(execution.id, execution_id)
        self.assertEqual(execution.template_id, template_id)
        self.assertEqual(execution.variables, {"test": "valeur", "variable": "autre"})
        self.assertEqual(execution.formatted_prompt, "Ceci est un valeur de template avec autre")
        self.assertEqual(execution.provider, "openai")
        self.assertEqual(execution.execution_time, 0.5)
        self.assertIsNone(execution.result)
        self.assertIsNone(execution.feedback)

    def test_execution_with_error(self):
        """Test d'une exécution avec erreur."""
        execution = PromptExecution(
            id=str(uuid.uuid4()),
            template_id="test_error",
            variables={"var": "test"},
            formatted_prompt="",
            execution_time=0.1,
            timestamp=datetime.now(),
            result="Variable manquante: {autre_var}"
        )

        self.assertEqual(execution.template_id, "test_error")
        self.assertEqual(execution.variables, {"var": "test"})
        self.assertEqual(execution.formatted_prompt, "")
        self.assertEqual(execution.execution_time, 0.1)
        self.assertEqual(execution.result, "Variable manquante: {autre_var}")


class TestPromptLibrary(unittest.TestCase):
    """Tests pour la classe PromptLibrary."""

    def test_create_library(self):
        """Test de création d'une bibliothèque de prompts."""

        templates_dict = {
            "template1": PromptTemplate(
                id="template1",
                name="Template 1",
                description="Premier template",
                template="Template {var1}",
                variables=[
                    PromptVariable(
                        name="var1",
                        description="Variable 1",
                        type=PromptVariableType.STRING,
                        required=True
                    )
                ],
                category="cat1"
            ),
            "template2": PromptTemplate(
                id="template2",
                name="Template 2",
                description="Deuxième template",
                template="Template {var2}",
                variables=[
                    PromptVariable(
                        name="var2",
                        description="Variable 2",
                        type=PromptVariableType.STRING,
                        required=True
                    )
                ],
                category="cat2"
            )
        }

        categories_dict = {
            "cat1": PromptCategory(
                id="cat1",
                name="Catégorie 1",
                description="Première catégorie"
            ),
            "cat2": PromptCategory(
                id="cat2",
                name="Catégorie 2",
                description="Deuxième catégorie"
            )
        }

        library = PromptLibrary(
            templates=templates_dict,
            categories=categories_dict
        )

        self.assertEqual(len(library.templates), 2)
        self.assertEqual(len(library.categories), 2)
        self.assertEqual(library.templates["template1"].id, "template1")
        self.assertEqual(library.templates["template2"].id, "template2")
        self.assertEqual(library.categories["cat1"].id, "cat1")
        self.assertEqual(library.categories["cat2"].id, "cat2")


if __name__ == "__main__":
    unittest.main()
