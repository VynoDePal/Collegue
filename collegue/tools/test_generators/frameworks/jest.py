"""
Jest test generator for JavaScript/TypeScript code.

Generates Jest-style tests with describe blocks, mocks, and modern JS features.
"""
import re
from typing import Any, Dict, List, Optional

from ..base import TestGenerator, TestCase, TestSuite


class JestGenerator(TestGenerator):
	"""Generator for Jest-style JavaScript/TypeScript tests."""

	framework_name = "jest"
	file_extension = ".test.js"

	def __init__(self, config: Optional[Dict[str, Any]] = None):
		super().__init__(config)
		self.use_describe = config.get('use_describe', True) if config else True
		self.language = config.get('language', 'javascript') if config else 'javascript'

		# Adjust extension for TypeScript
		if self.language == 'typescript':
			self.file_extension = ".test.ts"

	def get_imports(self) -> List[str]:
		"""Get Jest imports."""
		imports = []
		if self.language == 'typescript':
			imports.append('import { describe, it, expect, jest, beforeEach, afterEach } from "@jest/globals";')
		# For JS, Jest globals are available automatically
		return imports

	def detect_testable_functions(self, code: str, language: str) -> List[Dict[str, Any]]:
		"""Analyze JavaScript/TypeScript code and detect testable functions."""
		functions = []

		# Pattern for function declarations
		patterns = [
			# function name(params) or async function name(params)
			(r'(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)', False),
			# const name = (params) => or const name = async (params) =>
			(r'(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[^=]+)\s*=>', False),
		]

		for pattern, is_exported in patterns:
			for match in re.finditer(pattern, code, re.MULTILINE):
				name = match.group(1)
				params_str = match.group(2) if len(match.groups()) > 1 else ""

				# Skip special methods
				if name in ('constructor', 'render', 'if', 'while', 'for', 'switch'):
					continue

				# Parse parameters
				params = []
				if params_str and params_str.strip():
					for param in params_str.split(','):
						param = param.strip()
						if param and param not in ('this', 'self'):
							params.append({'name': param.split('=')[0].split(':')[0].strip(), 'type': 'any'})

				functions.append({
					'name': name,
					'params': params,
					'complexity': 'medium',
					'is_async': 'async' in code[max(0, match.start()-10):match.start()]
				})

		return functions

	def generate_test_case(
		self,
		func_info: Dict[str, Any],
		code: str,
		language: str
	) -> Optional[TestCase]:
		"""Generate a Jest test case."""
		if not self.should_generate_test(func_info):
			return None

		func_name = func_info['name']
		params = func_info.get('params', [])
		is_async = func_info.get('is_async', False)

		lines = []

		# Generate test
		test_name = f'should handle {func_name} correctly'
		if is_async:
			lines.append(f'it("{test_name}", async () => ' + '{')
		else:
			lines.append(f'it("{test_name}", () => ' + '{')

		# Setup
		for param in params:
			lines.append(f'    const {param["name"]} = {self._infer_value(param["type"], param["name"])};')

		# Call function
		if params:
			arg_list = ', '.join(p['name'] for p in params)
			if is_async:
				lines.append(f'    const result = await {func_name}({arg_list});')
			else:
				lines.append(f'    const result = {func_name}({arg_list});')
		else:
			if is_async:
				lines.append(f'    const result = await {func_name}();')
			else:
				lines.append(f'    const result = {func_name}();')

		lines.append('    expect(result).toBeDefined();')
		lines.append('    // TODO: Add more specific assertions')
		lines.append('});')

		test_code = '\n'.join(lines)

		return TestCase(
			name=f'test_{func_name}',
			code=test_code,
			description=f"Test {func_name}",
			assertions=['expect(result).toBeDefined()']
		)

	def _infer_value(self, param_type: str, param_name: str) -> str:
		"""Infer test value for parameter."""
		hints = {
			'id': '"test-id-123"',
			'name': '"Test Name"',
			'email': '"test@example.com"',
			'url': '"https://example.com"',
			'count': '5',
			'limit': '10',
			'enabled': 'true',
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
		"""Generate Jest test suite."""
		import os
		base_name = os.path.basename(filepath)
		if language == 'typescript':
			test_filename = base_name.replace('.ts', '.test.ts').replace('.tsx', '.test.tsx')
		else:
			test_filename = base_name.replace('.js', '.test.js')

		suite = TestSuite(
			filename=test_filename,
			framework=self.framework_name,
			imports=self.get_imports()
		)

		functions = self.detect_testable_functions(code, language)
		for func_info in functions:
			test_case = self.generate_test_case(func_info, code, language)
			if test_case:
				suite.add_test_case(test_case)

		return suite

