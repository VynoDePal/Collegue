"""
Unittest test generator for Python code.

Generates standard library unittest-style tests with TestCase classes.
"""
import re
import ast
from typing import Any, Dict, List, Optional

from ..base import TestGenerator, TestCase, TestSuite


class UnittestGenerator(TestGenerator):
	"""Generator for standard library unittest-style tests."""

	framework_name = "unittest"
	file_extension = "_test.py"

	def get_imports(self) -> List[str]:
		"""Get unittest imports."""
		imports = [
			"import unittest",
			"from unittest.mock import Mock, patch, MagicMock",
		]
		return imports

	def detect_testable_functions(self, code: str, language: str) -> List[Dict[str, Any]]:
		"""Analyze Python code and detect testable functions."""
		functions = []

		try:
			tree = ast.parse(code)
		except SyntaxError:
			# Fallback to regex
			return self._detect_with_regex(code)

		for node in ast.walk(tree):
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
				# Skip special methods
				if node.name.startswith('__') and node.name.endswith('__'):
					continue

				# Calculate complexity
				complexity = self._calculate_complexity(node)

				# Extract parameters
				params = []
				for arg in node.args.args:
					if arg.arg not in ('self', 'cls'):
						params.append({'name': arg.arg, 'type': 'Any'})

				functions.append({
					'name': node.name,
					'params': params,
					'complexity': complexity,
					'is_async': isinstance(node, ast.AsyncFunctionDef),
					'is_method': False
				})

		return functions

	def _detect_with_regex(self, code: str) -> List[Dict[str, Any]]:
		"""Fallback detection using regex."""
		functions = []
		pattern = r'^\s*(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)'

		for match in re.finditer(pattern, code, re.MULTILINE):
			name = match.group(1)
			params_str = match.group(2)

			if name.startswith('__') and name.endswith('__'):
				continue

			params = []
			if params_str.strip():
				for param in params_str.split(','):
					param = param.strip()
					if param and param not in ('self', 'cls'):
						params.append({'name': param.split('=')[0].strip(), 'type': 'Any'})

			functions.append({
				'name': name,
				'params': params,
				'complexity': 'medium',
				'is_async': False
			})

		return functions

	def _calculate_complexity(self, node: ast.AST) -> str:
		"""Calculate complexity approximation."""
		count = 1
		for child in ast.walk(node):
			if isinstance(child, (ast.If, ast.While, ast.For)):
				count += 1

		if count > 10:
			return 'high'
		elif count > 5:
			return 'medium'
		return 'low'

	def generate_test_case(
		self,
		func_info: Dict[str, Any],
		code: str,
		language: str
	) -> Optional[TestCase]:
		"""Generate a unittest test case."""
		if not self.should_generate_test(func_info):
			return None

		func_name = func_info['name']
		params = func_info.get('params', [])

		lines = []
		test_name = f'test_{func_name}'

		lines.append(f'    def {test_name}(self):')
		lines.append(f'        """Test {func_name} function."""')
		lines.append('')

		# Setup params
		for param in params:
			value = self._infer_param_value(param['name'])
			lines.append(f'        {param["name"]} = {value}')

		if params:
			lines.append('')

		# Call function and assert
		if params:
			arg_list = ', '.join(p['name'] for p in params)
			lines.append(f'        result = {func_name}({arg_list})')
		else:
			lines.append(f'        result = {func_name}()')

		lines.append('        self.assertIsNotNone(result)')
		lines.append('        # TODO: Add more specific assertions')

		test_code = '\n'.join(lines)

		return TestCase(
			name=test_name,
			code=test_code,
			description=f"Test {func_name}"
		)

	def _infer_param_value(self, param_name: str) -> str:
		"""Infer appropriate test value."""
		hints = {
			'id': '123',
			'name': '"test_name"',
			'email': '"test@example.com"',
			'count': '5',
		}
		for hint, value in hints.items():
			if hint in param_name.lower():
				return value
		return '"value"'

	def generate_suite(
		self,
		filepath: str,
		code: str,
		language: str
	) -> TestSuite:
		"""Generate unittest test suite (as a TestCase class)."""
		import os
		base_name = os.path.basename(filepath)
		if base_name.endswith('.py'):
			test_filename = base_name.replace('.py', self.file_extension)
		else:
			test_filename = f"test_{base_name}.py"

		suite = TestSuite(
			filename=test_filename,
			framework=self.framework_name,
			imports=self.get_imports()
		)

		# Add class definition as setup code
		class_name = base_name.replace('.py', '').replace('.', '_').title() + 'Test'
		suite.setup_code = f'\nclass {class_name}(unittest.TestCase):'

		functions = self.detect_testable_functions(code, language)
		for func_info in functions:
			test_case = self.generate_test_case(func_info, code, language)
			if test_case:
				suite.add_test_case(test_case)

		# Add if __name__ == '__main__' as teardown
		suite.teardown_code = '\n\nif __name__ == "__main__":\n    unittest.main()'

		return suite
