"""
Mocha test generator for JavaScript/TypeScript code.

Generates Mocha-style tests with describe/it blocks and chai assertions.
"""
import re
from typing import Any, Dict, List, Optional

from ..base import TestGenerator, TestCase, TestSuite


class MochaGenerator(TestGenerator):
	"""Generator for Mocha-style JavaScript/TypeScript tests."""

	framework_name = "mocha"
	file_extension = ".test.js"

	def __init__(self, config: Optional[Dict[str, Any]] = None):
		super().__init__(config)
		self.language = config.get('language', 'javascript') if config else 'javascript'
		self.assertion_lib = config.get('assertion_lib', 'chai') if config else 'chai'

		if self.language == 'typescript':
			self.file_extension = ".test.ts"

	def get_imports(self) -> List[str]:
		"""Get Mocha imports."""
		imports = [
			"const { describe, it, before, after, beforeEach, afterEach } = require('mocha');",
		]

		if self.assertion_lib == 'chai':
			imports.append("const { expect, assert } = require('chai');")
		elif self.assertion_lib == 'should':
			imports.append("const should = require('chai').should();")

		return imports

	def detect_testable_functions(self, code: str, language: str) -> List[Dict[str, Any]]:
		"""Analyze JS/TS code and detect testable functions."""
		functions = []

		patterns = [
			(r'(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)', False),
			(r'(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[^=]+)\s*=>', False),
		]

		for pattern, _ in patterns:
			for match in re.finditer(pattern, code, re.MULTILINE):
				name = match.group(1)
				params_str = match.group(2) if len(match.groups()) > 1 else ""

				if name in ('constructor', 'render'):
					continue

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
		"""Generate a Mocha test case."""
		if not self.should_generate_test(func_info):
			return None

		func_name = func_info['name']
		params = func_info.get('params', [])
		is_async = func_info.get('is_async', False)

		lines = []

		test_name = f'should handle {func_name} correctly'
		if is_async:
			lines.append(f'it("{test_name}", async function() ' + '{')
		else:
			lines.append(f'it("{test_name}", function() ' + '{')

		# Setup params
		for param in params:
			lines.append(f'    const {param["name"]} = {self._infer_value(param["name"])};')

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

		# Assertions based on assertion library
		if self.assertion_lib == 'chai':
			lines.append('    expect(result).to.exist;')
			lines.append('    // TODO: Add more specific assertions')
		else:
			lines.append('    assert.ok(result);')
			lines.append('    // TODO: Add more specific assertions')

		lines.append('});')

		test_code = '\n'.join(lines)

		return TestCase(
			name=f'test_{func_name}',
			code=test_code,
			description=f"Test {func_name}"
		)

	def _infer_value(self, param_name: str) -> str:
		"""Infer test value."""
		hints = {
			'id': '"test-123"',
			'name': '"Test"',
			'count': '1',
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
		"""Generate Mocha test suite."""
		import os
		base_name = os.path.basename(filepath)
		if language == 'typescript':
			test_filename = base_name.replace('.ts', '.test.ts')
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
