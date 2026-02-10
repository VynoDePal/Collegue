"""
Pytest test generator for Python code.

Generates pytest-style tests with fixtures, parametrize, and modern Python features.
"""
import re
import ast
from typing import Any, Dict, List, Optional

from ..base import TestGenerator, TestCase, TestSuite


class PytestGenerator(TestGenerator):
	"""Generator for pytest-style Python tests."""

	framework_name = "pytest"
	file_extension = "_test.py"

	def __init__(self, config: Optional[Dict[str, Any]] = None):
		super().__init__(config)
		self.use_fixtures = config.get('use_fixtures', True) if config else True
		self.use_parametrize = config.get('use_parametrize', True) if config else True

	def get_imports(self) -> List[str]:
		"""Get pytest imports."""
		imports = ["import pytest"]
		if self.include_mocks:
			imports.append("from unittest.mock import Mock, patch, MagicMock")
		return imports

	def detect_testable_functions(self, code: str, language: str) -> List[Dict[str, Any]]:
		"""Analyze Python code and detect testable functions."""
		functions = []

		try:
			tree = ast.parse(code)
		except SyntaxError:
			# Fallback to regex parsing
			return self._detect_with_regex(code)

		for node in ast.walk(tree):
			if isinstance(node, ast.FunctionDef):
				# Skip private and dunder methods
				if node.name.startswith('__') and node.name.endswith('__'):
					continue

				# Calculate complexity
				complexity = self._calculate_complexity(node)

				# Extract parameters
				params = []
				for arg in node.args.args:
					if arg.arg != 'self' and arg.arg != 'cls':
						param_info = {'name': arg.arg, 'type': 'Any'}
						if arg.annotation and isinstance(arg.annotation, ast.Name):
							param_info['type'] = arg.annotation.id
						params.append(param_info)

				# Extract return type
				return_type = None
				if node.returns and isinstance(node.returns, ast.Name):
					return_type = node.returns.id

				functions.append({
					'name': node.name,
					'params': params,
					'return_type': return_type,
					'complexity': complexity,
					'line_number': node.lineno,
					'is_method': False,
					'is_async': isinstance(node, ast.AsyncFunctionDef)
				})

			elif isinstance(node, ast.AsyncFunctionDef):
				# Handle async functions
				params = [
					{'name': arg.arg, 'type': 'Any'}
					for arg in node.args.args
					if arg.arg not in ('self', 'cls')
				]

				functions.append({
					'name': node.name,
					'params': params,
					'return_type': None,
					'complexity': 'medium',
					'line_number': node.lineno,
					'is_method': False,
					'is_async': True
				})

		return functions

	def _detect_with_regex(self, code: str) -> List[Dict[str, Any]]:
		"""Fallback detection using regex."""
		functions = []

		# Match function definitions
		pattern = r'^\s*(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)'
		for match in re.finditer(pattern, code, re.MULTILINE):
			name = match.group(1)
			params_str = match.group(2)

			# Skip special methods
			if name.startswith('__') and name.endswith('__'):
				continue

			# Parse parameters
			params = []
			if params_str.strip():
				for param in params_str.split(','):
					param = param.strip()
					if param and param not in ('self', 'cls'):
						param_name = param.split(':')[0].split('=')[0].strip()
						params.append({'name': param_name, 'type': 'Any'})

			functions.append({
				'name': name,
				'params': params,
				'return_type': None,
				'complexity': 'medium',
				'line_number': code[:match.start()].count('\n') + 1,
				'is_method': 'self' in params_str or 'cls' in params_str,
				'is_async': code[match.start():match.start()+15].strip().startswith('async')
			})

		return functions

	def _calculate_complexity(self, node: ast.FunctionDef) -> str:
		"""Calculate cyclomatic complexity approximation."""
		complexity = 1

		for child in ast.walk(node):
			if isinstance(child, (ast.If, ast.While, ast.For, ast.ExceptHandler)):
				complexity += 1
			elif isinstance(child, ast.BoolOp):
				complexity += len(child.values) - 1

		if complexity > 10:
			return 'high'
		elif complexity > 5:
			return 'medium'
		return 'low'

	def generate_test_case(
		self,
		func_info: Dict[str, Any],
		code: str,
		language: str
	) -> Optional[TestCase]:
		"""Generate a pytest test case for a function."""
		if not self.should_generate_test(func_info):
			return None

		func_name = func_info['name']
		params = func_info.get('params', [])
		is_async = func_info.get('is_async', False)

		# Generate test name
		test_name = f"test_{func_name}"

		# Generate test code
		lines = [f"def {test_name}():"]
		lines.append(f'    """Test {func_name} function."""')
		lines.append("")

		# Setup with parameter values
		arg_values = []
		for param in params:
			param_name = param['name']
			param_type = param.get('type', 'Any')
			value = self._infer_param_value(param_type, param_name)
			lines.append(f'    {param_name} = {value}')
			arg_values.append(param_name)

		lines.append("")

		# Call the function
		if is_async:
			lines.append(f'    result = asyncio.run({func_name}({", ".join(arg_values)}))')
		else:
			lines.append(f'    result = {func_name}({", ".join(arg_values)})')

		lines.append("")

		# Generate assertions
		assertions = self._generate_assertions(func_info)
		for assertion in assertions:
			lines.append(f'    {assertion}')

		test_code = "\n".join(lines)

		return TestCase(
			name=test_name,
			code=test_code,
			description=f"Test {func_name} with {len(params)} parameters",
			assertions=assertions,
			fixtures=[]
		)

	def _infer_param_value(self, param_type: str, param_name: str) -> str:
		"""Infer an appropriate test value for a parameter."""
		type_defaults = {
			'str': '"test_value"',
			'int': '42',
			'float': '3.14',
			'bool': 'True',
			'list': '[]',
			'List': '[]',
			'dict': '{}',
			'Dict': '{}',
			'tuple': '()',
			'set': 'set()',
			'Optional': 'None',
			'Union': 'None',
			'Any': '"value"',
			'None': 'None',
			'NoneType': 'None',
		}

		# Check type first
		if param_type in type_defaults:
			return type_defaults[param_type]

		# Check param name hints
		hints = {
			'id': '123',
			'name': '"test_name"',
			'email': '"test@example.com"',
			'url': '"https://example.com"',
			'path': '"/test/path"',
			'data': '{}',
			'items': '[]',
			'count': '1',
			'limit': '100',
			'offset': '0',
			'size': '10',
		}

		param_lower = param_name.lower()
		for hint, value in hints.items():
			if hint in param_lower:
				return value

		return '"value"'

	def _generate_assertions(self, func_info: Dict[str, Any]) -> List[str]:
		"""Generate appropriate assertions for a function."""
		assertions = []
		return_type = func_info.get('return_type')

		if return_type:
			if return_type == 'bool':
				assertions.append('assert result is True  # or assert not result')
			elif return_type in ('int', 'float'):
				assertions.append('assert isinstance(result, (int, float))')
			elif return_type == 'str':
				assertions.append('assert isinstance(result, str)')
				assertions.append('assert len(result) > 0')
			elif return_type in ('list', 'List'):
				assertions.append('assert isinstance(result, list)')
			elif return_type in ('dict', 'Dict'):
				assertions.append('assert isinstance(result, dict)')
			elif return_type != 'None':
				assertions.append(f'assert result is not None')
		else:
			# Generic assertions
			assertions.append('assert result is not None')
			assertions.append('# TODO: Add more specific assertions')

		return assertions

	def generate_suite(
		self,
		filepath: str,
		code: str,
		language: str
	) -> TestSuite:
		"""Generate a complete pytest test suite."""
		# Get filename for test file
		import os
		base_name = os.path.basename(filepath)
		if base_name.endswith('.py'):
			test_filename = base_name.replace('.py', self.file_extension)
		else:
			test_filename = f"test_{base_name}.py"

		# Create suite
		suite = TestSuite(
			filename=test_filename,
			framework=self.framework_name,
			imports=self.get_imports()
		)

		# Detect and generate tests for each function
		functions = self.detect_testable_functions(code, language)
		for func_info in functions:
			test_case = self.generate_test_case(func_info, code, language)
			if test_case:
				suite.add_test_case(test_case)

		return suite
