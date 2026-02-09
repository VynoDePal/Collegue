"""
Tests unitaires pour le module optimization des ressources LLM
"""
import unittest
from unittest.mock import MagicMock, patch
import sys
import os
from fastapi.testclient import TestClient
from fastapi import FastAPI


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from collegue.resources.llm.optimization import (
    OptimizationStrategy, PromptOptimization,
    get_optimization, get_all_optimizations,
    optimize_prompt
)

class TestLLMOptimization(unittest.TestCase):
    """Tests pour le module optimization des ressources LLM."""

    def test_get_optimization(self):
        """Teste la récupération d'une stratégie d'optimisation."""
        strategy = get_optimization("chain_of_thought")
        self.assertIsInstance(strategy, PromptOptimization)
        self.assertEqual(strategy.name, "Chain of Thought")

        strategy = get_optimization("nonexistent_strategy")
        self.assertIsNone(strategy)

    def test_get_all_optimizations(self):
        """Teste la récupération de toutes les stratégies d'optimisation."""
        strategies = get_all_optimizations()
        self.assertIsInstance(strategies, list)
        self.assertGreater(len(strategies), 0)
        self.assertIn("chain_of_thought", strategies)
        self.assertIn("few_shot", strategies)
        self.assertIn("zero_shot", strategies)

    def test_optimize_prompt_chain_of_thought(self):
        """Teste l'application de la stratégie Chain of Thought."""
        base_prompt = "Résoudre ce problème mathématique: 5 + 7 * 2"

        optimized_prompt = optimize_prompt(base_prompt, "chain_of_thought")

        self.assertNotEqual(optimized_prompt, base_prompt)
        self.assertIn("étape par étape", optimized_prompt)

    def test_optimize_prompt_few_shot(self):
        """Teste l'application de la stratégie Few-Shot."""
        base_prompt = "Classifie ce texte: 'J'ai adoré ce film!'"

        optimized_prompt = optimize_prompt(base_prompt, "few_shot")

        self.assertNotEqual(optimized_prompt, base_prompt)
        self.assertIn("exemples", optimized_prompt.lower())

    def test_optimize_prompt_with_provider(self):
        """Teste l'application d'une stratégie avec un fournisseur spécifique."""
        base_prompt = "Résoudre ce problème mathématique: 5 + 7 * 2"

        optimized_prompt_openai = optimize_prompt(base_prompt, "chain_of_thought", provider="openai")
        optimized_prompt_anthropic = optimize_prompt(base_prompt, "chain_of_thought", provider="anthropic")

        self.assertNotEqual(optimized_prompt_openai, optimized_prompt_anthropic)

    def test_optimize_prompt_with_examples(self):
        """Teste l'application d'une stratégie avec des exemples personnalisés."""
        base_prompt = "Classifie ce texte: 'J'ai adoré ce film!'"

        examples = "Exemple 1: Ce film était terrible -> Négatif\nExemple 2: J'ai beaucoup aimé cette série -> Positif"

        optimized_prompt = optimize_prompt(base_prompt, "few_shot", examples=examples)

        self.assertIn("Ce film était terrible", optimized_prompt)
        self.assertIn("J'ai beaucoup aimé cette série", optimized_prompt)

    def test_optimize_prompt_nonexistent_strategy(self):
        """Teste l'application d'une stratégie inexistante."""
        base_prompt = "Résoudre ce problème mathématique: 5 + 7 * 2"

        optimized_prompt = optimize_prompt(base_prompt, "nonexistent_strategy")

        self.assertIsNone(optimized_prompt)

class TestLLMOptimizationEndpoints(unittest.TestCase):
    """Tests pour les endpoints FastAPI d'optimisation des prompts LLM."""

    def setUp(self):
        """Configuration avant chaque test."""
        self.app = FastAPI()
        self.app_state = {"resource_manager": MagicMock()}


        from collegue.resources.llm.optimization import register_optimization
        register_optimization(self.app, self.app_state)

        self.client = TestClient(self.app)

    def test_list_optimization_strategies_endpoint(self):
        """Teste l'endpoint de liste des stratégies d'optimisation."""
        response = self.client.get("/resources/llm/optimizations")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("optimizations", data)
        self.assertIsInstance(data["optimizations"], list)
        self.assertGreater(len(data["optimizations"]), 0)
        self.assertIn("chain_of_thought", data["optimizations"])
        self.assertIn("few_shot", data["optimizations"])

    def test_get_optimization_strategy_endpoint(self):
        """Teste l'endpoint de récupération d'une stratégie d'optimisation."""
        response = self.client.get("/resources/llm/optimizations/chain_of_thought")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["name"], "Chain of Thought")
        self.assertEqual(data["strategy"], "chain_of_thought")

    def test_apply_optimization_strategy_endpoint(self):
        """Teste l'endpoint d'application d'une stratégie d'optimisation."""
        response = self.client.post(
            "/resources/llm/optimize",
            params={
                "prompt": "Résoudre ce problème mathématique: 5 + 7 * 2",
                "optimization_id": "chain_of_thought"
            }
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("optimized_prompt", data)
        self.assertIn("étape par étape", data["optimized_prompt"])

    def test_apply_optimization_strategy_endpoint_nonexistent_strategy(self):
        """Teste l'endpoint d'application avec une stratégie inexistante."""
        response = self.client.post(
            "/resources/llm/optimize",
            params={
                "prompt": "Résoudre ce problème mathématique: 5 + 7 * 2",
                "optimization_id": "nonexistent_strategy"
            }
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("error", data)

    def test_apply_optimization_strategy_endpoint_few_shot(self):
        """Teste l'endpoint d'application de la stratégie Few-Shot."""
        examples_str = "Exemple 1: Ce film était terrible -> Négatif\nExemple 2: J'ai beaucoup aimé cette série -> Positif"
        response = self.client.post(
            "/resources/llm/optimize",
            params={
                "prompt": "Classifie ce texte: 'J'ai adoré ce film!'",
                "optimization_id": "few_shot",
                "examples": examples_str
            }
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("optimized_prompt", data)
        self.assertIn("Ce film était terrible", data["optimized_prompt"])

if __name__ == '__main__':
    unittest.main()
