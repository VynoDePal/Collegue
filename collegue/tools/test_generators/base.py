"""
Base classes for test generators.

Provides the abstract base class and data models for all test generators.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TestCase:
	"""Represents a single test case."""
	name: str
	code: str
	description: str = ""
	fixtures: List[str] = field(default_factory=list)
	assertions: List[str] = field(default_factory=list)
	metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TestSuite:
	"""Represents a complete test suite for a file."""
	filename: str
	framework: str
	test_cases: List[TestCase] = field(default_factory=list)
	imports: List[str] = field(default_factory=list)
	fixtures: List[str] = field(default_factory=list)
	setup_code: str = ""
	teardown_code: str = ""

	def add_test_case(self, test_case: TestCase) -> None:
		"""Add a test case to the suite."""
		self.test_cases.append(test_case)

	def to_code(self) -> str:
		"""Generate the complete test file code."""
		lines = []

		# Imports
		for imp in self.imports:
			lines.append(imp)
		if self.imports:
			lines.append("")

		# Fixtures/helpers
		if self.fixtures:
			for fixture in self.fixtures:
				lines.append(fixture)
				lines.append("")

		# Setup
		if self.setup_code:
			lines.append(self.setup_code)
			lines.append("")

		# Test cases
		for test_case in self.test_cases:
			if test_case.description:
				lines.append(f"# {test_case.description}")
			lines.append(test_case.code)
			lines.append("")

		# Teardown
		if self.teardown_code:
			lines.append(self.teardown_code)

		return "\n".join(lines)


class TestGenerator(ABC):
	"""
	Abstract base class for all test generators.
	
	Implementations must provide framework-specific test generation logic.
	"""

	framework_name: str = ""
	file_extension: str = ".py"

	def __init__(self, config: Optional[Dict[str, Any]] = None):
		self.config = config or {}
		self.coverage_target = self.config.get('coverage_target', 0.8)
		self.include_mocks = self.config.get('include_mocks', True)

	@abstractmethod
	def detect_testable_functions(self, code: str, language: str) -> List[Dict[str, Any]]:
		"""
		Analyze code and detect functions/methods that need tests.
		
		Returns:
			List of dicts with keys: name, signature, complexity, test_priority
		"""
		pass

	@abstractmethod
	def generate_test_case(
		self,
		func_info: Dict[str, Any],
		code: str,
		language: str
	) -> Optional[TestCase]:
		"""
		Generate a test case for a specific function.
		
		Returns:
			TestCase or None if function shouldn't be tested
		"""
		pass

	@abstractmethod
	def generate_suite(
		self,
		filepath: str,
		code: str,
		language: str
	) -> TestSuite:
		"""
		Generate a complete test suite for a file.
		
		Returns:
			TestSuite containing all test cases
		"""
		pass

	@abstractmethod
	def get_imports(self) -> List[str]:
		"""Get required imports for this framework."""
		pass

	def infer_param_values(self, param_type: str) -> Any:
		"""
		Infer appropriate test values for a parameter type.
		
		Override in subclasses for language-specific types.
		"""
		defaults = {
			'str': '"test_string"',
			'int': '42',
			'float': '3.14',
			'bool': 'True',
			'list': '[]',
			'dict': '{}',
			'None': 'None',
		}
		return defaults.get(param_type, 'None')

	def calculate_priority(self, func_info: Dict[str, Any]) -> int:
		"""
		Calculate test priority based on function characteristics.
		
		Returns:
			Priority score (higher = more important to test)
		"""
		score = 0

		# Public functions are more important
		if not func_info.get('name', '').startswith('_'):
			score += 10

		# Complex functions need more testing
		complexity = func_info.get('complexity', 'low')
		if complexity == 'high':
			score += 15
		elif complexity == 'medium':
			score += 8

		# Functions with many parameters need thorough testing
		param_count = len(func_info.get('params', []))
		score += param_count * 2

		return score

	def should_generate_test(self, func_info: Dict[str, Any]) -> bool:
		"""
		Determine if a function should have tests generated.
		
		Filters out private helpers, simple getters, etc.
		"""
		name = func_info.get('name', '')

		# Skip private functions unless high complexity
		if name.startswith('_') and not name.startswith('__'):
			if func_info.get('complexity') != 'high':
				return False

		# Skip dunder methods
		if name.startswith('__') and name.endswith('__'):
			return False

		# Skip properties
		if func_info.get('is_property', False):
			return False

		return True
