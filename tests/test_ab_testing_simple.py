#!/usr/bin/env python3
"""
Script simplifié de test pour le système A/B testing des prompts.
Teste les fonctionnalités de base disponibles dans le système actuel.
"""

import asyncio
import sys
import os
import random
from pathlib import Path

# Ajouter le chemin du projet
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collegue.prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine


class SimpleABTestingSuite:
    """Suite de tests simplifiée pour A/B testing."""
    
    def __init__(self):
        """Initialise la suite de tests."""
        self.engine = EnhancedPromptEngine()
        self.results = []
    
    def list_available_templates(self) -> dict:
        """Liste les templates disponibles par outil."""
        tools_dir = Path(__file__).parent.parent / "collegue/prompts/templates/tools"
        available = {}
        
        if tools_dir.exists():
            for tool_dir in tools_dir.iterdir():
                if tool_dir.is_dir():
                    tool_name = tool_dir.name
                    versions = []
                    for file in tool_dir.glob("*.yaml"):
                        version_name = file.stem
                        versions.append(version_name)
                    if versions:
                        available[tool_name] = sorted(versions)
        
        return available
    
    async def test_template_loading(self) -> bool:
        """Test que les templates sont correctement chargés."""
        print("\n🔍 Testing template loading...")
        
        # Vérifier que les templates sont chargés
        templates = self.engine.templates
        if templates:
            print(f"  ✅ {len(templates)} templates loaded successfully")
            for template_id in list(templates.keys())[:5]:  # Afficher les 5 premiers
                print(f"    - {template_id}")
            return True
        else:
            print("  ❌ No templates loaded")
            return False
    
    async def test_prompt_generation_basic(self) -> bool:
        """Test basique de génération de prompts."""
        print("\n📝 Testing basic prompt generation...")
        
        test_cases = [
            {
                "tool": "code_generation",
                "context": {
                    "description": "Create a Python function to calculate fibonacci",
                    "language": "python",
                    "requirements": ["Handle edge cases", "Use memoization"],
                }
            },
            {
                "tool": "code_explanation",
                "context": {
                    "code": "def quicksort(arr): return arr if len(arr) <= 1 else quicksort([x for x in arr[1:] if x < arr[0]]) + [arr[0]] + quicksort([x for x in arr[1:] if x >= arr[0]])",
                    "language": "python",
                }
            }
        ]
        
        success_count = 0
        for test_case in test_cases:
            tool = test_case["tool"]
            context = test_case["context"]
            
            print(f"\n  Testing {tool}...")
            
            try:
                # Utiliser la méthode sans spécifier de version
                prompt, version = await self.engine.get_optimized_prompt(
                    tool_name=tool,
                    context=context,
                    language=context.get('language', 'python')
                )
                
                if prompt and len(prompt) > 50:
                    print(f"    ✅ Generated prompt with version '{version}' ({len(prompt)} chars)")
                    success_count += 1
                else:
                    print(f"    ❌ Failed to generate valid prompt")
                    
            except Exception as e:
                print(f"    ❌ Error: {str(e)}")
        
        return success_count == len(test_cases)
    
    async def test_language_optimization(self) -> bool:
        """Test l'optimisation par langage."""
        print("\n🌐 Testing language optimization...")
        
        context = {
            "description": "Create a sorting function",
            "requirements": ["Efficient", "Handle edge cases"]
        }
        
        languages = ["python", "javascript", "typescript"]
        success_count = 0
        
        for language in languages:
            try:
                prompt, version = await self.engine.get_optimized_prompt(
                    tool_name="code_generation",
                    context=context,
                    language=language
                )
                
                if prompt:
                    print(f"  ✅ {language}: Generated optimized prompt")
                    success_count += 1
                else:
                    print(f"  ❌ {language}: Failed to generate prompt")
                    
            except Exception as e:
                print(f"  ❌ {language}: Error - {str(e)}")
        
        return success_count == len(languages)
    
    async def test_performance_tracking(self) -> bool:
        """Test le tracking basique des performances."""
        print("\n📊 Testing performance tracking...")
        
        template_id = "code_generation_default"
        
        # Simuler plusieurs exécutions
        for i in range(5):
            self.engine.track_performance(
                template_id=template_id,
                version="default",
                success=random.choice([True, False]),
                execution_time=random.uniform(0.1, 2.0),
                tokens_used=random.randint(100, 1000)
            )
        
        # Vérifier que les métriques sont enregistrées
        if template_id in self.engine.performance_cache:
            metrics = self.engine.performance_cache[template_id]
            print(f"  ✅ Tracked {len(metrics)} executions for {template_id}")
            
            # Afficher quelques statistiques
            successes = sum(1 for m in metrics if m.get('success', False))
            avg_time = sum(m.get('execution_time', 0) for m in metrics) / len(metrics)
            print(f"    - Success rate: {successes}/{len(metrics)} ({successes*100/len(metrics):.1f}%)")
            print(f"    - Average execution time: {avg_time:.3f}s")
            return True
        else:
            print("  ❌ No metrics tracked")
            return False
    
    async def test_fallback_mechanism(self) -> bool:
        """Test le mécanisme de fallback."""
        print("\n🔄 Testing fallback mechanism...")
        
        # Tester avec un outil inexistant
        try:
            prompt, version = await self.engine.get_optimized_prompt(
                tool_name="nonexistent_tool",
                context={"test": "data"},
                language="python"
            )
            print("  ❌ Should have raised an error for nonexistent tool")
            return False
        except ValueError as e:
            print(f"  ✅ Correctly raised error: {str(e)}")
            return True
        except Exception as e:
            print(f"  ❌ Unexpected error: {str(e)}")
            return False
    
    async def run_all_tests(self):
        """Exécute tous les tests."""
        print("=" * 60)
        print("🧪 Simple A/B Testing Verification Suite")
        print("=" * 60)
        
        tests = [
            ("Template Loading", self.test_template_loading),
            ("Basic Prompt Generation", self.test_prompt_generation_basic),
            ("Language Optimization", self.test_language_optimization),
            ("Performance Tracking", self.test_performance_tracking),
            ("Fallback Mechanism", self.test_fallback_mechanism),
        ]
        
        results = []
        for test_name, test_func in tests:
            try:
                result = await test_func()
                results.append((test_name, result))
            except Exception as e:
                print(f"\n❌ {test_name} failed with error: {e}")
                results.append((test_name, False))
        
        # Afficher le résumé
        print("\n" + "=" * 60)
        print("📋 Test Summary")
        print("=" * 60)
        
        passed = 0
        for test_name, result in results:
            status = "✅ PASS" if result else "❌ FAIL"
            print(f"  {status}: {test_name}")
            if result:
                passed += 1
        
        total = len(results)
        percentage = (passed / total * 100) if total > 0 else 0
        
        print(f"\n📊 Results: {passed}/{total} tests passed ({percentage:.1f}%)")
        
        if passed == total:
            print("🎉 All tests passed successfully!")
        elif passed > 0:
            print("⚠️  Some tests failed. Please review the output above.")
        else:
            print("❌ All tests failed. Major issues detected.")
        
        return passed == total


async def main():
    """Point d'entrée principal."""
    suite = SimpleABTestingSuite()
    success = await suite.run_all_tests()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
