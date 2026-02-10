"""
Specialized request builders for different tool types.

Provides pre-configured builders for common use cases like documentation,
refactoring, and code generation.
"""
from typing import Any, Dict, List, Optional
from .base import LLMRequestBuilder


class DocumentationRequestBuilder(LLMRequestBuilder):
	"""Builder specialized for documentation generation requests."""

	STYLE_INSTRUCTIONS = {
		"standard": "Génère une documentation claire et concise avec descriptions, paramètres et valeurs de retour",
		"detailed": "Génère une documentation très détaillée avec exemples, cas d'usage et notes techniques",
		"minimal": "Génère une documentation minimale avec seulement les informations essentielles",
		"api": "Génère une documentation de style API avec format standardisé pour chaque fonction/classe",
		"tutorial": "Génère une documentation de style tutoriel avec explications pédagogiques"
	}

	FORMAT_INSTRUCTIONS = {
		"markdown": "Utilise le format Markdown avec en-têtes appropriés",
		"rst": "Utilise le format reStructuredText",
		"html": "Génère du HTML bien formaté",
		"docstring": "Génère des docstrings dans le style du langage",
		"json": "Retourne la documentation structurée en JSON"
	}

	LANGUAGE_INSTRUCTIONS = {
		"python": "Utilise les conventions PEP 257 pour les docstrings, inclus les types avec les paramètres",
		"javascript": "Utilise JSDoc format avec @param, @returns, @example",
		"typescript": "Inclus les types TypeScript dans la documentation, utilise @param avec types",
		"java": "Utilise Javadoc format avec @param, @return, @throws",
		"c#": "Utilise XML documentation format avec <summary>, <param>, <returns>",
		"go": "Utilise les conventions Go avec commentaires au-dessus des déclarations",
		"rust": "Utilise les doc comments avec /// et inclus les exemples avec ```"
	}

	def __init__(self, tool_name: str = "documentation", prompt_engine=None):
		super().__init__(tool_name, prompt_engine)
		self._language: Optional[str] = None
		self._style: str = "standard"
		self._format: str = "markdown"
		self._include_examples: bool = False
		self._focus_on: str = "all"
		self._elements: List[Dict[str, str]] = []

	def for_language(self, language: str) -> 'DocumentationRequestBuilder':
		"""Set the target programming language."""
		self._language = language
		return self

	def with_style(self, style: str) -> 'DocumentationRequestBuilder':
		"""Set documentation style (standard, detailed, minimal, api, tutorial)."""
		self._style = style
		return self

	def with_format(self, format_type: str) -> 'DocumentationRequestBuilder':
		"""Set output format (markdown, rst, html, docstring, json)."""
		self._format = format_type
		return self

	def with_examples(self, include: bool = True) -> 'DocumentationRequestBuilder':
		"""Include usage examples."""
		self._include_examples = include
		return self

	def focus_on(self, element_type: str) -> 'DocumentationRequestBuilder':
		"""Focus on specific elements (functions, classes, modules, all)."""
		self._focus_on = element_type
		return self

	def with_elements(self, elements: List[Dict[str, str]]) -> 'DocumentationRequestBuilder':
		"""Add identified code elements to document."""
		self._elements = elements
		return self

	def with_code(self, code: str, language: Optional[str] = None) -> 'DocumentationRequestBuilder':
		"""Add code block to document."""
		if language:
			self._language = language
		self.add_code_block(code, self._language or "python")
		return self

	def build(self) -> str:
		"""Build the complete documentation prompt."""
		if not self._language:
			raise ValueError("Language must be set before building prompt")

		# Start with instructions
		style_desc = self.STYLE_INSTRUCTIONS.get(self._style, self.STYLE_INSTRUCTIONS["standard"])
		format_desc = self.FORMAT_INSTRUCTIONS.get(self._format, self.FORMAT_INSTRUCTIONS["markdown"])

		self.add_instruction(f"Génère une documentation pour le code {self._language} suivant")
		self.add_instruction(f"Style: {style_desc}")
		self.add_instruction(f"Format: {format_desc}")

		# Add elements if available
		if self._elements:
			self.add_context("elements_to_document", self._format_elements(self._elements))

		# Add focus instruction
		if self._focus_on and self._focus_on != "all":
			self.add_instruction(f"Concentre-toi sur les {self._focus_on}")

		# Add examples instruction
		if self._include_examples:
			self.add_instruction("Inclus des exemples d'utilisation pratiques pour chaque élément principal")

		# Add language-specific instructions
		lang_instructions = self.LANGUAGE_INSTRUCTIONS.get(self._language.lower(), "")
		if lang_instructions:
			self.add_instruction(f"Instructions {self._language}: {lang_instructions}")

		return super().build()

	def _format_elements(self, elements: List[Dict[str, str]]) -> str:
		"""Format code elements for the prompt."""
		lines = ["Éléments identifiés à documenter :"]
		for element in elements[:10]:
			lines.append(f"- {element['type']}: {element['name']} (ligne {element['line_number']})")
		return "\n".join(lines)


class RefactoringRequestBuilder(LLMRequestBuilder):
	"""Builder specialized for code refactoring requests."""

	REFACTORING_TYPES = {
		"rename": "Renommer des variables, fonctions ou classes pour améliorer la clarté",
		"extract": "Extraire du code en fonctions ou méthodes réutilisables",
		"simplify": "Simplifier la logique complexe et les conditions imbriquées",
		"optimize": "Optimiser les performances et l'efficacité",
		"clean": "Nettoyer le code mort, imports inutilisés et code superflu",
		"modernize": "Moderniser le code vers les patterns contemporains"
	}

	LANGUAGE_INSTRUCTIONS = {
		"python": {
			"rename": "Utilise les conventions PEP 8 (snake_case, PascalCase)",
			"extract": "Crée des fonctions avec type hints et docstrings",
			"simplify": "Utilise comprehensions, walrus operator",
			"optimize": "Utilise set, deque, évite les boucles inutiles",
			"clean": "Supprime imports inutiles, utilise f-strings",
			"modernize": "Utilise dataclasses, type hints, pathlib"
		},
		"javascript": {
			"rename": "Utilise camelCase pour variables/fonctions",
			"extract": "Crée des fonctions avec JSDoc, arrow functions",
			"simplify": "Utilise destructuring, template literals",
			"optimize": "Utilise Map/Set, évite les mutations",
			"clean": "Supprime var, utilise const/let",
			"modernize": "Utilise ES6+, async/await, modules ES6"
		},
		"typescript": {
			"rename": "Utilise camelCase avec types explicites",
			"extract": "Crée des fonctions typées avec génériques",
			"simplify": "Utilise union types, optional chaining",
			"optimize": "Types stricts, évite 'any'",
			"clean": "Supprime types redondants",
			"modernize": "Utilise strict mode, utility types"
		}
	}

	def __init__(self, tool_name: str = "refactoring", prompt_engine=None):
		super().__init__(tool_name, prompt_engine)
		self._refactoring_type: str = "clean"
		self._language: Optional[str] = None
		self._preserve_behavior: bool = True

	def refactor_as(self, refactoring_type: str) -> 'RefactoringRequestBuilder':
		"""Set the type of refactoring to perform."""
		self._refactoring_type = refactoring_type
		return self

	def for_language(self, language: str) -> 'RefactoringRequestBuilder':
		"""Set the target programming language."""
		self._language = language
		return self

	def preserve_behavior(self, preserve: bool = True) -> 'RefactoringRequestBuilder':
		"""Ensure behavior preservation (default: True)."""
		self._preserve_behavior = preserve
		return self

	def with_code(self, code: str, language: Optional[str] = None) -> 'RefactoringRequestBuilder':
		"""Add code block to refactor."""
		if language:
			self._language = language
		self.add_code_block(code, self._language or "python")
		return self

	def build(self) -> str:
		"""Build the refactoring prompt."""
		refactoring_desc = self.REFACTORING_TYPES.get(
			self._refactoring_type,
			"Améliorer la qualité du code"
		)

		self.add_instruction(f"Effectue un refactoring de type '{self._refactoring_type}'")
		self.add_instruction(f"Description: {refactoring_desc}")

		if self._preserve_behavior:
			self.add_instruction("IMPORTANT: Préserve exactement le comportement du code original")

		if self._language:
			self.add_instruction(f"Langage: {self._language}")
			# Add language-specific instructions
			lang_instructions = self.LANGUAGE_INSTRUCTIONS.get(self._language.lower(), {})
			if self._refactoring_type in lang_instructions:
				self.add_instruction(f"Conventions {self._language}: {lang_instructions[self._refactoring_type]}")

		return super().build()
