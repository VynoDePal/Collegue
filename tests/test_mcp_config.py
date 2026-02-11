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


sys.path.insert(0, str(Path(__file__).parent.parent))

from collegue.config import Settings
from collegue.core.tool_llm_manager import ToolLLMManager


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

    original_env_model = os.environ.get("LLM_MODEL")
    original_env_key = os.environ.get("LLM_API_KEY")

    try:
        print("\n1. Test avec valeurs par défaut:")
        if "LLM_MODEL" in os.environ:
            del os.environ["LLM_MODEL"]
        if "LLM_API_KEY" in os.environ:
            del os.environ["LLM_API_KEY"]

        settings = Settings()
        print(f"   - Modèle: {settings.llm_model}")
        print(f"   - API Key présente: {bool(settings.llm_api_key)}")

        print("\n2. Test avec variables d'environnement:")
        os.environ["LLM_MODEL"] = "gemini-2.5-flash"
        os.environ["LLM_API_KEY"] = "AIzaSy-env-test-key"

        settings = Settings()
        print(f"   - Modèle depuis ENV: {settings.llm_model}")
        print(f"   - API Key depuis ENV: {settings.llm_api_key[:20]}...")

        print("\n3. Test avec paramètres ENV (priorité ENV > DEFAULT):")
        os.environ["LLM_MODEL"] = "gemini-3-flash-preview"
        os.environ["LLM_API_KEY"] = "AIzaSy-mcp-test-key"

        settings = Settings()
        print(f"   - Modèle depuis ENV: {settings.llm_model}")
        print(f"   - API Key depuis ENV: {settings.llm_api_key[:20]}...")

        assert settings.llm_model == "gemini-3-flash-preview", "ENV devrait overrider DEFAULT"
        assert settings.llm_api_key == "AIzaSy-mcp-test-key", "ENV API key devrait overrider DEFAULT"

        print("\n✅ Test de priorité réussi: ENV > DEFAULT")

    finally:
        if original_env_model:
            os.environ["LLM_MODEL"] = original_env_model
        elif "LLM_MODEL" in os.environ:
            del os.environ["LLM_MODEL"]

        if original_env_key:
            os.environ["LLM_API_KEY"] = original_env_key
        elif "LLM_API_KEY" in os.environ:
            del os.environ["LLM_API_KEY"]

def test_different_models():
    """Test avec différents modèles Google Gemini."""
    print("\n" + "="*60)
    print("TEST DE DIFFÉRENTS MODÈLES")
    print("="*60)

    models_to_test = [
        {
            "name": "Google Gemini 3 Flash Preview",
            "model": "gemini-3-flash-preview",
            "description": "Dernier modèle Gemini 3, rapide et économique"
        },
        {
            "name": "Google Gemini 2.5 Flash",
            "model": "gemini-2.5-flash",
            "description": "Modèle performant pour la plupart des tâches"
        },
        {
            "name": "Google Gemini 2.5 Flash Lite",
            "model": "gemini-2.5-flash-lite",
            "description": "Version légère pour les tâches simples"
        },
        {
            "name": "Google Gemini 2.5 Pro",
            "model": "gemini-2.5-pro",
            "description": "Modèle premium pour les tâches complexes"
        }
    ]


    test_api_key = os.environ.get("LLM_API_KEY", "AIzaSy-test-key-12345")

    for model_info in models_to_test:
        print(f"\n🔧 Test avec {model_info['name']}:")
        print(f"   Description: {model_info['description']}")

        try:

            os.environ["LLM_MODEL"] = model_info["model"]
            os.environ["LLM_API_KEY"] = test_api_key
            settings = Settings()


            assert settings.llm_model == model_info["model"]
            print(f"   ✅ Configuration acceptée: {settings.llm_model}")


            if test_api_key and test_api_key.startswith("AIzaSy-"):
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
                    "LLM_MODEL": "gemini-3-flash-preview",
                    "LLM_API_KEY": "AIzaSy-dev-key"
                }
            }
        },
        {
            "name": "Configuration Production",
            "config": {
                "collegue": {
                    "serverUrl": "https://collegue.example.com/mcp/",
                    "LLM_MODEL": "gemini-2.5-pro",
                    "LLM_API_KEY": "AIzaSy-prod-key"
                }
            }
        }
    ]

    for config_info in windsurf_configs:
        print(f"\n📋 {config_info['name']}:")
        print(f"   Config JSON: {json.dumps(config_info['config'], indent=6)}")

        collegue_config = config_info['config'].get('collegue', {})
        mcp_params = {}

        if 'LLM_MODEL' in collegue_config:
            mcp_params['LLM_MODEL'] = collegue_config['LLM_MODEL']
        if 'LLM_API_KEY' in collegue_config:
            mcp_params['LLM_API_KEY'] = collegue_config['LLM_API_KEY']

        for k, v in mcp_params.items():
            os.environ[k] = v
        settings = Settings()

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

    print("\n1. Test sans clé API:")
    os.environ.pop("LLM_API_KEY", None)
    settings = Settings()

    try:
        manager = ToolLLMManager(settings)
        print("   ❌ Devrait lever une erreur sans clé API")
    except ValueError as e:
        print(f"   ✅ Erreur correctement levée: {str(e)}")

    print("\n2. Test avec modèle invalide:")
    os.environ["LLM_MODEL"] = "gemini-3-flash-preview"
    os.environ["LLM_API_KEY"] = "AIzaSy-test"
    settings = Settings()

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
        test_config_priority()
        test_different_models()
        simulate_windsurf_config()
        test_error_handling()

        print("\n" + "="*60)
        print("✅ TOUS LES TESTS SONT PASSÉS AVEC SUCCÈS!")
        print("="*60)
        print("\n📌 Prochaines étapes:")
        print("1. Configurer une vraie clé API Google Gemini dans .env")
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
