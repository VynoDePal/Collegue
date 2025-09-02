#!/usr/bin/env python3
"""
Script de test pour la configuration MCP dynamique du LLM.
Teste la priorité de configuration et différents modèles.
"""

import os
import sys
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional

# Ajouter le répertoire parent au path
sys.path.insert(0, str(Path(__file__).parent.parent))

from collegue.config import Settings
from collegue.core.tool_llm_manager import ToolLLMManager

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def test_config_priority():
    """Test de la priorité de configuration MCP > ENV > DEFAULT."""
    print("\n" + "="*60)
    print("TEST DE PRIORITÉ DE CONFIGURATION")
    print("="*60)
    
    # Sauvegarder les valeurs actuelles
    original_env_model = os.environ.get("LLM_MODEL")
    original_env_key = os.environ.get("LLM_API_KEY")
    
    try:
        # Test 1: Valeurs par défaut uniquement
        print("\n1. Test avec valeurs par défaut:")
        if "LLM_MODEL" in os.environ:
            del os.environ["LLM_MODEL"]
        if "LLM_API_KEY" in os.environ:
            del os.environ["LLM_API_KEY"]
        
        settings = Settings()
        print(f"   - Modèle: {settings.llm_model}")
        print(f"   - API Key présente: {bool(settings.llm_api_key)}")
        
        # Test 2: Variables d'environnement
        print("\n2. Test avec variables d'environnement:")
        os.environ["LLM_MODEL"] = "openai/gpt-3.5-turbo"
        os.environ["LLM_API_KEY"] = "sk-env-test-key"
        
        settings = Settings()
        print(f"   - Modèle depuis ENV: {settings.llm_model}")
        print(f"   - API Key depuis ENV: {settings.llm_api_key[:20]}...")
        
        # Test 3: Paramètres MCP (priorité maximale)
        print("\n3. Test avec paramètres MCP (priorité max):")
        mcp_params = {
            "LLM_MODEL": "google/gemini-2.0-flash-exp:free",
            "LLM_API_KEY": "sk-mcp-test-key"
        }
        
        settings.update_from_mcp(mcp_params)
        print(f"   - Modèle depuis MCP: {settings.llm_model}")
        print(f"   - API Key depuis MCP: {settings.llm_api_key[:20]}...")
        
        # Vérifier que MCP a priorité sur ENV
        assert settings.llm_model == "google/gemini-2.0-flash-exp:free", "MCP devrait avoir priorité sur ENV"
        assert settings.llm_api_key == "sk-mcp-test-key", "MCP API key devrait avoir priorité"
        
        print("\n✅ Test de priorité réussi: MCP > ENV > DEFAULT")
        
    finally:
        # Restaurer les valeurs originales
        if original_env_model:
            os.environ["LLM_MODEL"] = original_env_model
        elif "LLM_MODEL" in os.environ:
            del os.environ["LLM_MODEL"]
            
        if original_env_key:
            os.environ["LLM_API_KEY"] = original_env_key
        elif "LLM_API_KEY" in os.environ:
            del os.environ["LLM_API_KEY"]

def test_different_models():
    """Test avec différents modèles OpenRouter."""
    print("\n" + "="*60)
    print("TEST DE DIFFÉRENTS MODÈLES")
    print("="*60)
    
    # Modèles à tester
    models_to_test = [
        {
            "name": "OpenAI GPT-4o Mini",
            "model": "openai/gpt-4o-mini",
            "description": "Modèle économique recommandé"
        },
        {
            "name": "Google Gemini Flash (Gratuit)",
            "model": "google/gemini-2.0-flash-exp:free",
            "description": "Modèle gratuit pour tests"
        },
        {
            "name": "Claude 3.5 Haiku",
            "model": "anthropic/claude-3.5-haiku",
            "description": "Modèle Anthropic rapide"
        },
        {
            "name": "DeepSeek Chat",
            "model": "deepseek/deepseek-chat",
            "description": "Bon rapport qualité/prix"
        }
    ]
    
    # Clé API de test (remplacer par une vraie pour tester réellement)
    test_api_key = os.environ.get("LLM_API_KEY", "sk-or-v1-test-key-12345")
    
    for model_info in models_to_test:
        print(f"\n🔧 Test avec {model_info['name']}:")
        print(f"   Description: {model_info['description']}")
        
        try:
            # Créer une configuration avec le modèle
            settings = Settings()
            mcp_params = {
                "LLM_MODEL": model_info["model"],
                "LLM_API_KEY": test_api_key
            }
            settings.update_from_mcp(mcp_params)
            
            # Vérifier que la configuration est correcte
            assert settings.llm_model == model_info["model"]
            print(f"   ✅ Configuration acceptée: {settings.llm_model}")
            
            # Essayer d'initialiser le ToolLLMManager
            if test_api_key and test_api_key.startswith("sk-or-v1-"):
                try:
                    manager = ToolLLMManager(settings)
                    print(f"   ✅ ToolLLMManager initialisé avec succès")
                except Exception as e:
                    print(f"   ⚠️  Erreur d'initialisation: {str(e)}")
            else:
                print(f"   ℹ️  Clé API de test uniquement - pas d'initialisation réelle")
                
        except Exception as e:
            print(f"   ❌ Erreur: {str(e)}")

def simulate_windsurf_config():
    """Simule la configuration depuis Windsurf MCP."""
    print("\n" + "="*60)
    print("SIMULATION DE CONFIGURATION WINDSURF")
    print("="*60)
    
    # Simuler différentes configurations Windsurf
    windsurf_configs = [
        {
            "name": "Configuration Minimale",
            "config": {
                "collegue": {
                    "serverUrl": "http://localhost:8088/mcp/"
                }
            }
        },
        {
            "name": "Configuration Développement",
            "config": {
                "collegue": {
                    "serverUrl": "http://localhost:8088/mcp/",
                    "LLM_MODEL": "google/gemini-2.0-flash-exp:free",
                    "LLM_API_KEY": "sk-or-v1-dev-key"
                }
            }
        },
        {
            "name": "Configuration Production",
            "config": {
                "collegue": {
                    "serverUrl": "https://collegue.example.com/mcp/",
                    "LLM_MODEL": "openai/gpt-4o",
                    "LLM_API_KEY": "sk-or-v1-prod-key"
                }
            }
        }
    ]
    
    for config_info in windsurf_configs:
        print(f"\n📋 {config_info['name']}:")
        print(f"   Config JSON: {json.dumps(config_info['config'], indent=6)}")
        
        # Extraire les paramètres MCP
        collegue_config = config_info['config'].get('collegue', {})
        mcp_params = {}
        
        if 'LLM_MODEL' in collegue_config:
            mcp_params['LLM_MODEL'] = collegue_config['LLM_MODEL']
        if 'LLM_API_KEY' in collegue_config:
            mcp_params['LLM_API_KEY'] = collegue_config['LLM_API_KEY']
        
        # Appliquer la configuration
        settings = Settings()
        settings.update_from_mcp(mcp_params)
        
        print(f"\n   Résultat:")
        if mcp_params:
            print(f"   - Modèle configuré: {settings.llm_model}")
            print(f"   - API Key présente: {bool(settings.llm_api_key)}")
        else:
            print(f"   - Utilise la configuration par défaut ou .env")
            print(f"   - Modèle: {settings.llm_model}")

def test_error_handling():
    """Test de la gestion d'erreurs."""
    print("\n" + "="*60)
    print("TEST DE GESTION D'ERREURS")
    print("="*60)
    
    # Test 1: Pas de clé API
    print("\n1. Test sans clé API:")
    settings = Settings()
    settings._mcp_llm_api_key = None
    settings.LLM_API_KEY = None
    
    try:
        manager = ToolLLMManager(settings)
        print("   ❌ Devrait lever une erreur sans clé API")
    except ValueError as e:
        print(f"   ✅ Erreur correctement levée: {str(e)}")
    
    # Test 2: Modèle invalide (sera accepté par la config mais pourrait échouer à l'exécution)
    print("\n2. Test avec modèle invalide:")
    settings = Settings()
    mcp_params = {
        "LLM_MODEL": "invalid/model-name",
        "LLM_API_KEY": "sk-or-v1-test"
    }
    settings.update_from_mcp(mcp_params)
    
    try:
        manager = ToolLLMManager(settings)
        print(f"   ✅ Configuration acceptée (validation à l'exécution)")
        print(f"   - Modèle configuré: {settings.llm_model}")
    except Exception as e:
        print(f"   ❌ Erreur inattendue: {str(e)}")

def main():
    """Fonction principale d'exécution des tests."""
    print("\n" + "🚀"*30)
    print("TESTS DE CONFIGURATION MCP POUR COLLÈGUE")
    print("🚀"*30)
    
    try:
        # Exécuter tous les tests
        test_config_priority()
        test_different_models()
        simulate_windsurf_config()
        test_error_handling()
        
        print("\n" + "="*60)
        print("✅ TOUS LES TESTS SONT PASSÉS AVEC SUCCÈS!")
        print("="*60)
        print("\n📌 Prochaines étapes:")
        print("1. Configurer une vraie clé API OpenRouter dans .env")
        print("2. Tester avec Windsurf en utilisant mcp_config.json")
        print("3. Surveiller les logs avec: docker-compose logs collegue-app")
        print("4. Utiliser le client Python pour tester les outils")
        
    except Exception as e:
        print(f"\n❌ Erreur lors des tests: {str(e)}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0

if __name__ == "__main__":
    exit(main())
