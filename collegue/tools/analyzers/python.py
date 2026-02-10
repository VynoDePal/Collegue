"""
Python Analyzer for Repo Consistency Check.

Analyzes Python code for:
- Unused imports
- Unused variables
- Dead code (functions/classes never used)
"""
import ast
import re
from typing import List, Dict, Any, Optional, Set, Tuple
from .base import BaseAnalyzer, AnalyzerError


class ConsistencyIssue:
    """Issue de cohérence détectée (copie locale pour éviter import circulaire)."""

    def __init__(self, kind: str, severity: str, path: str, line: int, message: str,
                 confidence: int = 80, suggested_fix: str = None, engine: str = "",
                 column: int = None):
        self.kind = kind
        self.severity = severity
        self.path = path
        self.line = line
        self.message = message
        self.confidence = confidence
        self.suggested_fix = suggested_fix
        self.engine = engine
        self.column = column


class PythonAnalyzer(BaseAnalyzer):
	"""Analyzer for Python code."""

	def analyze_unused_imports(self, code: str, filepath: str) -> List[ConsistencyIssue]:
		"""Detect unused imports in Python code."""
		issues = []
		try:
			tree = ast.parse(code)
		except SyntaxError as e:
			self._log_warning(f"Syntax error in {filepath}: {e}")
			return issues

		imports = {}
		for node in ast.walk(tree):
			if isinstance(node, ast.Import):
				for alias in node.names:
					name = alias.asname or alias.name.split('.')[0]
					imports[name] = node.lineno
			elif isinstance(node, ast.ImportFrom):
				for alias in node.names:
					name = alias.asname or alias.name
					imports[name] = node.lineno

		all_content = re.sub(r'""".*?"""', '', code, flags=re.DOTALL)
		all_content = re.sub(r"'''.*?'''", '', all_content, flags=re.DOTALL)

		for name, line in imports.items():
			pattern = rf'\b{re.escape(name)}\b'
			matches = list(re.finditer(pattern, all_content))
			usage_count = sum(1 for m in matches
							if not self._is_in_import_statement(m.start(), code))

			if usage_count <= 1:
				issues.append(ConsistencyIssue(
					kind="unused_import",
					severity="low",
					path=filepath,
					line=line,
					message=f"Import '{name}' non utilisé",
					confidence=85,
					suggested_fix=f"Supprimer 'import {name}'",
					engine="ast-analyzer"
				))

		return issues

	def analyze_unused_vars(self, code: str, filepath: str) -> List[ConsistencyIssue]:
		"""Detect unused variables in Python code."""
		issues = []
		try:
			tree = ast.parse(code)
		except SyntaxError:
			return issues

		declared = {}
		used = set()

		for node in ast.walk(tree):
			if isinstance(node, ast.Name):
				if isinstance(node.ctx, ast.Store):
					declared[node.id] = node.lineno
				elif isinstance(node.ctx, ast.Load):
					used.add(node.id)

		for name, line in declared.items():
			if name not in used and not name.startswith('_'):
				issues.append(ConsistencyIssue(
					kind="unused_var",
					severity="medium",
					path=filepath,
					line=line,
					message=f"Variable '{name}' déclarée mais jamais utilisée",
					confidence=80,
					suggested_fix=f"Supprimer ou utiliser '{name}'",
					engine="ast-analyzer"
				))

		return issues

	def _is_in_import_statement(self, pos: int, code: str) -> bool:
		"""Check if position is within an import statement."""
		lines = code[:pos].split('\n')
		if not lines:
			return False
		current_line = lines[-1]
		return bool(re.match(r'^\s*(from\s+\S+\s+)?import\s', current_line))

	def analyze_dead_code(self, code: str, filepath: str, all_contents: str = None) -> List[ConsistencyIssue]:
		"""Detect dead code (unused functions/classes) in Python."""
		issues = []
		try:
			tree = ast.parse(code)
		except SyntaxError:
			return issues

		if all_contents is None:
			all_contents = code

		definitions = {}
		for node in ast.walk(tree):
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
				if not node.name.startswith('_'):
					definitions[node.name] = (node.lineno, 'function')
			elif isinstance(node, ast.ClassDef):
				if not node.name.startswith('_'):
					definitions[node.name] = (node.lineno, 'class')

		for name, (line, kind) in definitions.items():
			patterns = [
				rf'\b{re.escape(name)}\s*\(',
				rf'\b{re.escape(name)}\b',
			]

			usage_count = 0
			for pattern in patterns:
				matches = list(re.finditer(pattern, all_contents))
				usage_count += len(matches)

			if usage_count <= 1:
				issues.append(ConsistencyIssue(
					kind="dead_code",
					severity="medium",
					path=filepath,
					line=line,
					message=f"{kind.capitalize()} '{name}' défini(e) mais jamais utilisé(e)",
					confidence=70,
					suggested_fix="Supprimer si inutile, ou vérifier si exporté/utilisé ailleurs",
					engine="usage-analyzer"
				))

		return issues
