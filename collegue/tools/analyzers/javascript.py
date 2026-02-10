"""
JavaScript/TypeScript Analyzer for Repo Consistency Check.

Analyzes JS/TS code for:
- Unused imports
- Unused variables
"""
import re
from typing import List, Dict, Any
from .base import BaseAnalyzer
from ..shared import ConsistencyIssue


class JavaScriptAnalyzer(BaseAnalyzer):
	"""Analyzer for JavaScript and TypeScript code."""

	def analyze_unused_imports(self, code: str, filepath: str) -> List[ConsistencyIssue]:
		"""Detect unused imports in JavaScript/TypeScript code."""
		issues = []

		import_patterns = [
			r"import\s*\{([^}]+)\}\s*from\s*['\"]([^'\"]+)['\"]",
			r"import\s+(\w+)\s+from\s*['\"]([^'\"]+)['\"]",
			r"import\s*\*\s*as\s+(\w+)\s+from\s*['\"]([^'\"]+)['\"]",
		]

		imports = {}
		lines = code.split('\n')

		for i, line in enumerate(lines, 1):
			for pattern in import_patterns:
				match = re.search(pattern, line)
				if match:
					names_str = match.group(1)

					for name_part in names_str.split(','):
						name_part = name_part.strip()
						if ' as ' in name_part:
							name = name_part.split(' as ')[1].strip()
						else:
							name = name_part.strip()
						if name and re.match(r'^\w+$', name):
							imports[name] = (i, match.group(0))

		for name, (line, import_stmt) in imports.items():
			pattern = rf'\b{re.escape(name)}\b'
			matches = list(re.finditer(pattern, code))

			usage_count = 0
			for m in matches:
				match_line = code[:m.start()].count('\n') + 1
				if match_line != line:
					usage_count += 1

			if usage_count == 0:
				issues.append(ConsistencyIssue(
					kind="unused_import",
					severity="low",
					path=filepath,
					line=line,
					message=f"Import '{name}' non utilisé",
					confidence=85,
					suggested_fix=f"Supprimer '{name}' de l'import",
					engine="regex-analyzer"
				))

		return issues

	def analyze_unused_vars(self, code: str, filepath: str) -> List[ConsistencyIssue]:
		"""Detect unused variables in JavaScript/TypeScript code."""
		issues = []

		decl_patterns = [
			r"(?:const|let|var)\s+(\w+)\s*=",
			r"(?:const|let|var)\s+\{([^}]+)\}\s*=",
		]

		declarations = {}
		lines = code.split('\n')

		for i, line in enumerate(lines, 1):
			for pattern in decl_patterns:
				matches = re.finditer(pattern, line)
				for match in matches:
					names_str = match.group(1)

					for name in re.findall(r'\b(\w+)\b', names_str):
						if not name.startswith('_') and name not in ('const', 'let', 'var'):
							declarations[name] = i

		for name, line in declarations.items():
			pattern = rf'\b{re.escape(name)}\b'
			matches = list(re.finditer(pattern, code))

			usage_count = sum(1 for m in matches if code[:m.start()].count('\n') + 1 != line)

			if usage_count == 0:
				issues.append(ConsistencyIssue(
					kind="unused_var",
					severity="medium",
					path=filepath,
					line=line,
					message=f"Variable '{name}' déclarée mais jamais utilisée",
					confidence=75,
					suggested_fix=f"Supprimer ou préfixer avec _ : _{name}",
					engine="regex-analyzer"
				))

		return issues
