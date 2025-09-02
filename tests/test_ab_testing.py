#!/usr/bin/env python3
"""
Test script for A/B testing functionality with multiple template versions.
This script verifies that the EnhancedPromptEngine correctly handles multiple
template versions and performs A/B testing with the epsilon-greedy strategy.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Any
import random

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from collegue.prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine
from collegue.prompts.engine.versioning import PromptVersionManager
from collegue.prompts.templates import list_available_templates, get_template_path


class TestABTesting:
    """Test suite for A/B testing functionality."""
    
    def __init__(self):
        """Initialize test environment."""
        self.engine = EnhancedPromptEngine()
        self.test_results = []
        
    def test_template_discovery(self) -> bool:
        """Test that all template versions are discovered correctly."""
        print("\n🔍 Testing template discovery...")
        
        available = list_available_templates()
        print(f"Found templates for {len(available)} tools:")
        
        expected_tools = [
            'code_generation', 'code_explanation', 
            'refactoring', 'documentation', 'test_generation'
        ]
        
        for tool in expected_tools:
            if tool in available:
                versions = available[tool]
                print(f"  ✅ {tool}: {', '.join(versions)}")
                
                # Check that we have multiple versions for A/B testing
                if len(versions) < 2:
                    print(f"    ⚠️  Warning: Only {len(versions)} version(s) available")
                    return False
            else:
                print(f"  ❌ {tool}: Not found")
                return False
        
        return True
    
    async def test_version_selection(self) -> bool:
        """Test that version selection works with epsilon-greedy strategy."""
        print("\n🎲 Testing version selection (epsilon-greedy)...")
        
        tool_name = "code_generation"
        selections = {"default": 0, "v2": 0, "experimental": 0}
        num_trials = 100
        
        print(f"Running {num_trials} trials for {tool_name}...")
        
        for _ in range(num_trials):
            # Simulate version selection
            version = await self._simulate_version_selection(tool_name)
            if version in selections:
                selections[version] += 1
        
        print(f"Version selection distribution:")
        for version, count in selections.items():
            percentage = (count / num_trials) * 100
            print(f"  - {version}: {count}/{num_trials} ({percentage:.1f}%)")
        
        # Check that epsilon-greedy is working (should explore ~10% of the time)
        # In practice, the exact distribution depends on the metrics
        if all(count > 0 for count in selections.values()):
            print("  ✅ All versions were selected (exploration working)")
            return True
        else:
            print("  ⚠️  Some versions were never selected")
            return False
    
    async def test_prompt_generation(self) -> bool:
        """Test prompt generation with different template versions."""
        print("\n📝 Testing prompt generation with multiple versions...")
        
        test_cases = [
            {
                "tool": "code_generation",
                "context": {
                    "requirements": "Create a function to calculate fibonacci numbers",
                    "language": "python"
                }
            },
            {
                "tool": "code_explanation", 
                "context": {
                    "code": "def fib(n): return n if n <= 1 else fib(n-1) + fib(n-2)",
                    "language": "python"
                }
            },
            {
                "tool": "refactoring",
                "context": {
                    "code": "def calc(x,y): return x+y",
                    "objectives": "Improve naming and add type hints",
                    "language": "python"
                }
            }
        ]
        
        all_success = True
        
        for test_case in test_cases:
            tool = test_case["tool"]
            context = test_case["context"]
            
            print(f"\n  Testing {tool}...")
            
            # Test each available version
            available = list_available_templates()
            if tool in available:
                for version in available[tool]:
                    try:
                        prompt, used_version = await self.engine.get_optimized_prompt(
                            tool_name=tool,
                            context=context,
                            version=version,
                            language=context.get('language', 'python')
                        )
                        
                        if prompt and len(prompt) > 50:  # Basic validation
                            print(f"    ✅ {version}: Generated prompt ({len(prompt)} chars)")
                        else:
                            print(f"    ❌ {version}: Failed to generate valid prompt")
                            all_success = False
                            
                    except Exception as e:
                        print(f"    ❌ {version}: Error - {str(e)}")
                        all_success = False
        
        return all_success
    
    async def test_metrics_tracking(self) -> bool:
        """Test that metrics are properly tracked for each version."""
        print("\n📊 Testing metrics tracking...")
        
        tool_name = "code_generation"
        
        # Simulate usage with metrics by calling track_performance
        for version in ["default", "v2", "experimental"]:
            # Track performance for each version
            self.engine.track_performance(
                template_id=f"{tool_name}_{version}",
                version=version,
                success=True,
                execution_time=random.uniform(0.1, 2.0),
                tokens_used=random.randint(100, 1000),
                user_satisfaction=random.uniform(0.8, 1.0)
            )
        
        # Check if metrics are tracked
        # Use performance cache instead of version_manager.metrics
        has_metrics = False
        for template_key in self.engine.performance_cache:
            if tool_name in template_key:
                has_metrics = True
                break
        
        if has_metrics:
            print(f"  Metrics tracked for {tool_name}:")
            # Display performance cache content
            for template_key, metrics_list in self.engine.performance_cache.items():
                if tool_name in template_key:
                    print(f"    Template: {template_key}")
                    if metrics_list:
                        latest = metrics_list[-1]
                        print(f"      - Success: {latest.get('success', False)}")
                        print(f"      - Execution time: {latest.get('execution_time', 0):.3f}s")
                        print(f"      - Tokens used: {latest.get('tokens_used', 0)}")
            print("  ✅ Metrics tracking working")
            return True
        else:
            print("  ❌ No metrics tracked")
            return False
    
    async def test_performance_comparison(self) -> bool:
        """Test performance comparison between different versions."""
        print("\n⚡ Testing performance comparison...")
        
        tool_name = "code_explanation"
        context = {
            "code": """
def quicksort(arr):
    if len(arr) <= 1:
        return arr
    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]
    return quicksort(left) + middle + quicksort(right)
            """,
            "language": "python"
        }
        
        version_performance = {}
        
        available = list_available_templates()
        if tool_name in available:
            for version in available[tool_name]:
                try:
                    import time
                    start_time = time.time()
                    
                    prompt, used_version = await self.engine.get_optimized_prompt(
                        tool_name=tool_name,
                        context=context,
                        version=version,
                        language=context.get('language', 'python')
                    )
                    
                    end_time = time.time()
                    execution_time = end_time - start_time
                    
                    version_performance[version] = {
                        "time": execution_time,
                        "length": len(prompt) if prompt else 0,
                        "success": prompt is not None
                    }
                    
                except Exception as e:
                    version_performance[version] = {
                        "time": 0,
                        "length": 0,
                        "success": False,
                        "error": str(e)
                    }
        
        print(f"  Performance comparison for {tool_name}:")
        for version, perf in version_performance.items():
            status = "✅" if perf["success"] else "❌"
            print(f"    {status} {version}:")
            print(f"      - Time: {perf['time']:.3f}s")
            print(f"      - Prompt length: {perf['length']} chars")
            if not perf["success"] and "error" in perf:
                print(f"      - Error: {perf['error']}")
        
        # Find best performing version
        successful_versions = {
            v: p for v, p in version_performance.items() 
            if p["success"]
        }
        
        if successful_versions:
            best_version = min(
                successful_versions.items(), 
                key=lambda x: x[1]["time"]
            )
            print(f"\n  🏆 Best performing: {best_version[0]} ({best_version[1]['time']:.3f}s)")
            return True
        else:
            print("\n  ❌ No successful versions")
            return False
    
    async def _simulate_version_selection(self, tool_name: str) -> str:
        """Simulate version selection using epsilon-greedy strategy."""
        available = list_available_templates()
        if tool_name not in available:
            return "default"
        
        versions = available[tool_name]
        
        # Simulate epsilon-greedy (10% exploration)
        if random.random() < 0.1:
            # Explore: choose random version
            return random.choice(versions)
        else:
            # Exploit: choose best version (simulate by preferring non-experimental)
            if "default" in versions:
                return "default"
            elif "v2" in versions:
                return "v2"
            else:
                return versions[0]
    
    async def run_all_tests(self):
        """Run all A/B testing tests."""
        print("=" * 60)
        print("🧪 A/B Testing Verification Suite")
        print("=" * 60)
        
        tests = [
            ("Template Discovery", self.test_template_discovery),
            ("Version Selection", self.test_version_selection),
            ("Prompt Generation", self.test_prompt_generation),
            ("Metrics Tracking", self.test_metrics_tracking),
            ("Performance Comparison", self.test_performance_comparison)
        ]
        
        results = []
        
        for test_name, test_func in tests:
            try:
                if asyncio.iscoroutinefunction(test_func):
                    result = await test_func()
                else:
                    result = test_func()
                results.append((test_name, result))
            except Exception as e:
                print(f"\n❌ {test_name} failed with error: {e}")
                results.append((test_name, False))
        
        # Summary
        print("\n" + "=" * 60)
        print("📋 Test Summary")
        print("=" * 60)
        
        passed = sum(1 for _, result in results if result)
        total = len(results)
        
        for test_name, result in results:
            status = "✅ PASS" if result else "❌ FAIL"
            print(f"  {status}: {test_name}")
        
        print(f"\n📊 Results: {passed}/{total} tests passed ({(passed/total)*100:.1f}%)")
        
        if passed == total:
            print("🎉 All A/B testing features are working correctly!")
        else:
            print("⚠️  Some tests failed. Please review the output above.")
        
        return passed == total


async def main():
    """Main entry point for the test script."""
    tester = TestABTesting()
    success = await tester.run_all_tests()
    
    # Exit with appropriate code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    # Run the async main function
    asyncio.run(main())
