"""
Adapter for test_generators package - Provides _test_templates-compatible interface.

This module bridges the gap between the old _test_templates API (simple functions)
and the new test_generators package (object-oriented TestGenerator classes).
"""
from typing import Any, Dict, List, Optional
from .test_generators import (
    PytestGenerator,
    JestGenerator,
    MochaGenerator,
    UnittestGenerator,
    TestSuite,
    TestCase,
)


def _functions_to_test_cases(functions: List[Dict[str, Any]], language: str = "python") -> List[TestCase]:
    """Convert legacy function dicts to TestCase objects."""
    test_cases = []
    for func in functions:
        func_name = func.get("name", "unknown")
        test_cases.append(TestCase(
            name=f"test_{func_name}",
            code=f"# Test for {func_name}",
            description=f"Test {func_name} functionality"
        ))
    return test_cases


def _classes_to_test_cases(classes: List[Dict[str, Any]], language: str = "python") -> List[TestCase]:
    """Convert legacy class dicts to TestCase objects."""
    test_cases = []
    for cls in classes:
        class_name = cls.get("name", "Unknown")
        test_cases.append(TestCase(
            name=f"test_{class_name}",
            code=f"# Test for class {class_name}",
            description=f"Test {class_name} functionality"
        ))
    return test_cases


def generate_unittest_tests(code: str, functions: List[Dict[str, Any]],
                          classes: List[Dict[str, Any]], include_mocks: bool = False) -> str:
    """Generate unittest tests using UnittestGenerator."""
    generator = UnittestGenerator(config={'include_mocks': include_mocks})
    suite = TestSuite(
        filename="test_module.py",
        framework="unittest",
        test_cases=[],
        imports=["import unittest", "from unittest.mock import Mock, patch, MagicMock"]
    )
    func_cases = _functions_to_test_cases(functions, "python")
    class_cases = _classes_to_test_cases(classes, "python")
    for case in func_cases + class_cases:
        suite.add_test_case(case)
    return suite.to_code()


def generate_pytest_tests(code: str, functions: List[Dict[str, Any]],
                        classes: List[Dict[str, Any]], include_mocks: bool = False) -> str:
    """Generate pytest tests using PytestGenerator."""
    generator = PytestGenerator(config={'include_mocks': include_mocks})
    suite = TestSuite(
        filename="test_module.py",
        framework="pytest",
        test_cases=[],
        imports=["import pytest", "from unittest.mock import Mock, patch, MagicMock"]
    )
    func_cases = _functions_to_test_cases(functions, "python")
    class_cases = _classes_to_test_cases(classes, "python")
    for case in func_cases + class_cases:
        suite.add_test_case(case)
    return suite.to_code()


def generate_jest_tests(code: str, functions: List[Dict[str, Any]],
                      classes: List[Dict[str, Any]], include_mocks: bool = False) -> str:
    """Generate Jest tests using JestGenerator."""
    generator = JestGenerator(config={'include_mocks': include_mocks})
    suite = TestSuite(
        filename="test_module.test.js",
        framework="jest",
        test_cases=[],
        imports=[]
    )
    func_cases = _functions_to_test_cases(functions, "javascript")
    class_cases = _classes_to_test_cases(classes, "javascript")
    for case in func_cases + class_cases:
        suite.add_test_case(case)
    return suite.to_code()


def generate_mocha_tests(code: str, functions: List[Dict[str, Any]],
                       classes: List[Dict[str, Any]], include_mocks: bool = False) -> str:
    """Generate Mocha tests using MochaGenerator."""
    generator = MochaGenerator(config={'include_mocks': include_mocks})
    suite = TestSuite(
        filename="test_module.test.js",
        framework="mocha",
        test_cases=[],
        imports=["const { expect } = require('chai');"]
    )
    func_cases = _functions_to_test_cases(functions, "javascript")
    class_cases = _classes_to_test_cases(classes, "javascript")
    for case in func_cases + class_cases:
        suite.add_test_case(case)
    return suite.to_code()


def generate_typescript_jest_tests(code: str, functions: List[Dict[str, Any]],
                                  classes: List[Dict[str, Any]],
                                  interfaces: List[Dict[str, Any]],
                                  types: List[Dict[str, Any]],
                                  include_mocks: bool = False) -> str:
    """Generate TypeScript Jest tests."""
    generator = JestGenerator(config={'include_mocks': include_mocks})
    suite = TestSuite(
        filename="test_module.test.ts",
        framework="jest",
        test_cases=[],
        imports=[]
    )
    func_cases = _functions_to_test_cases(functions, "typescript")
    class_cases = _classes_to_test_cases(classes, "typescript")
    for case in func_cases + class_cases:
        suite.add_test_case(case)
    return suite.to_code()


def generate_typescript_mocha_tests(code: str, functions: List[Dict[str, Any]],
                                   classes: List[Dict[str, Any]],
                                   interfaces: List[Dict[str, Any]],
                                   types: List[Dict[str, Any]],
                                   include_mocks: bool = False) -> str:
    """Generate TypeScript Mocha tests."""
    generator = MochaGenerator(config={'include_mocks': include_mocks})
    suite = TestSuite(
        filename="test_module.test.ts",
        framework="mocha",
        test_cases=[],
        imports=["import { expect } from 'chai';"]
    )
    func_cases = _functions_to_test_cases(functions, "typescript")
    class_cases = _classes_to_test_cases(classes, "typescript")
    for case in func_cases + class_cases:
        suite.add_test_case(case)
    return suite.to_code()


def generate_generic_tests(code: str, language: str, framework: str) -> str:
    """Generate generic tests for unsupported language/framework combinations."""
    return f"""# Tests générés pour {language} avec {framework}
# Note: Framework non supporté nativement

# Placeholder pour les tests
# Code source à tester:
{code}
"""
