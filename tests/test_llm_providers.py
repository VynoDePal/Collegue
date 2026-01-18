"""
Tests unitaires pour les fournisseurs LLM
"""
import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import asyncio
from fastapi.testclient import TestClient
from fastapi import FastAPI

# Ajouter le répertoire parent au chemin pour pouvoir importer les modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from collegue.resources.llm.providers import (
    initialize_llm_client, generate_text,
    get_default_model_config, get_available_models,
    LLMConfig, LLMProvider, LLMResponse
)

class TestLLMProviders(unittest.TestCase):
    """Tests pour le module providers des ressources LLM."""
    
    def test_get_default_model_config(self):
        config = get_default_model_config("gpt-4")
        self.assertIsInstance(config, LLMConfig)
        self.assertEqual(config.provider, LLMProvider.OPENAI)
        self.assertEqual(config.model_name, "gpt-4")
        
        config = get_default_model_config("nonexistent_model")
        self.assertIsNone(config)
    
    def test_get_available_models(self):
        models = get_available_models()
        self.assertIsInstance(models, list)
        self.assertGreater(len(models), 0)
        self.assertIn("gpt-4", models)
        self.assertIn("claude-3-opus", models)
    
    @patch('builtins.__import__')
    def test_initialize_openai_client(self, mock_import):
        config = LLMConfig(
            provider=LLMProvider.OPENAI,
            model_name="gpt-4",
            api_key="test_key"
        )
        
        mock_openai = MagicMock()
        mock_model = MagicMock()
        mock_model.list.return_value = MagicMock(data=[])
        mock_openai.Model = mock_model
        
        def side_effect(name, *args, **kwargs):
            if name == 'openai':
                return mock_openai
            return unittest.mock.DEFAULT
        
        mock_import.side_effect = side_effect
        
        with patch.dict('sys.modules', {'openai': mock_openai}):
            client = initialize_llm_client(config)
        
        self.assertEqual(mock_openai.api_key, "test_key")
        self.assertEqual(client, mock_openai)
    
    @patch('builtins.__import__')
    def test_initialize_anthropic_client(self, mock_import):
        config = LLMConfig(
            provider=LLMProvider.ANTHROPIC,
            model_name="claude-3-opus-20240229",
            api_key="test_key"
        )
        
        mock_anthropic = MagicMock()
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        
        def side_effect(name, *args, **kwargs):
            if name == 'anthropic':
                return mock_anthropic
            return unittest.mock.DEFAULT
        
        mock_import.side_effect = side_effect
        
        # Initialisation du client
        with patch.dict('sys.modules', {'anthropic': mock_anthropic}):
            client = initialize_llm_client(config)
        
        # Vérifications
        mock_anthropic.Anthropic.assert_called_once_with(api_key="test_key")
        self.assertEqual(client, mock_client)

class TestLLMProvidersAsync(unittest.IsolatedAsyncioTestCase):
    """Tests asynchrones pour les fonctions async du module providers."""
    
    @patch('collegue.resources.llm.providers.initialize_llm_client')
    @patch('collegue.resources.llm.providers.llm_clients', {})
    async def test_generate_text_openai(self, mock_initialize):
        """Teste la génération de texte avec OpenAI."""
        # Configuration pour OpenAI
        config = LLMConfig(
            provider=LLMProvider.OPENAI,
            model_name="gpt-4",
            api_key="test_key"
        )
        
        # Mock pour le client OpenAI
        mock_client = MagicMock()
        mock_chat_completion = MagicMock()
        mock_client.ChatCompletion.create.return_value = mock_chat_completion
        mock_chat_completion.choices = [
            MagicMock(message=MagicMock(content="Test response"), finish_reason="stop")
        ]
        mock_chat_completion.usage = {"total_tokens": 10}
        mock_chat_completion.model = "gpt-4"
        
        mock_initialize.return_value = mock_client
        
        # Génération de texte
        response = await generate_text(config, "Test prompt", "System prompt")
        
        # Vérifications
        self.assertIsInstance(response, LLMResponse)
        self.assertEqual(response.text, "Test response")
        self.assertEqual(response.provider, LLMProvider.OPENAI)
        self.assertEqual(response.model, "gpt-4")
    
    @patch('collegue.resources.llm.providers.initialize_llm_client')
    @patch('collegue.resources.llm.providers.llm_clients', {})
    async def test_generate_text_anthropic(self, mock_initialize):
        """Teste la génération de texte avec Anthropic."""
        # Configuration pour Anthropic
        config = LLMConfig(
            provider=LLMProvider.ANTHROPIC,
            model_name="claude-3-opus-20240229",
            api_key="test_key"
        )
        
        # Mock pour le client Anthropic
        mock_client = MagicMock()
        mock_message = MagicMock()
        mock_client.messages.create.return_value = mock_message
        mock_message.content = [MagicMock(text="Test response")]
        mock_message.stop_reason = "end_turn"
        mock_message.usage = MagicMock(input_tokens=5, output_tokens=5)
        
        mock_initialize.return_value = mock_client
        
        # Génération de texte
        response = await generate_text(config, "Test prompt", "System prompt")
        
        # Vérifications
        self.assertIsInstance(response, LLMResponse)
        self.assertEqual(response.text, "Test response")
        self.assertEqual(response.provider, LLMProvider.ANTHROPIC)
        self.assertEqual(response.model, "claude-3-opus-20240229")

class TestLLMProvidersEndpoints(unittest.TestCase):
    """Tests pour les endpoints FastAPI des fournisseurs LLM."""
    
    def setUp(self):
        self.app = FastAPI()
        self.app_state = {"resource_manager": MagicMock()}
        
        from collegue.resources.llm.providers import register_providers
        register_providers(self.app, self.app_state)
        
        self.client = TestClient(self.app)
    
    def test_list_models_endpoint(self):
        response = self.client.get("/resources/llm/models")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("models", data)
        self.assertIsInstance(data["models"], list)
        self.assertGreater(len(data["models"]), 0)
        self.assertIn("gpt-4", data["models"])
        self.assertIn("claude-3-opus", data["models"])
    
    def test_get_model_config_endpoint(self):
        response = self.client.get("/resources/llm/models/gpt-4")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["provider"], "openai")
        self.assertEqual(data["model_name"], "gpt-4")
    
    @patch('collegue.resources.llm.providers.generate_text')
    def test_generate_text_endpoint(self, mock_generate):
        mock_response = LLMResponse(
            text="Test response",
            model="gpt-4",
            provider=LLMProvider.OPENAI,
            usage={"total_tokens": 10},
            finish_reason="stop"
        )
        
        mock_generate.return_value = mock_response
        
        config = {
            "provider": "openai",
            "model_name": "gpt-4",
            "api_key": "test_key",
            "max_tokens": 4096,
            "temperature": 0.7
        }
        
        response = self.client.post(
            "/resources/llm/generate?prompt=Test%20prompt&system_prompt=System%20prompt",
            json=config
        )
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["text"], "Test response")
        self.assertEqual(data["model"], "gpt-4")
        self.assertEqual(data["provider"], "openai")

if __name__ == '__main__':
    unittest.main()
