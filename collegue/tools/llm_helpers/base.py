"""
Base classes for LLM helpers.

Provides LLMRequestBuilder for constructing prompts and LLMResponseParser
for parsing various LLM output formats.
"""
import re
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable, Self


@dataclass
class CodeBlock:
	"""Represents a code block extracted from LLM response."""
	language: str
	code: str
	metadata: Dict[str, Any] = field(default_factory=dict)


class LLMRequestBuilder:
	"""
	Builder pattern for constructing structured LLM prompts.
	
	Example:
		builder = LLMRequestBuilder("documentation", prompt_engine)
		prompt = (builder
			.add_context("language", "python")
			.add_code_block(code, "python")
			.add_constraint("Include docstrings")
			.build())
	"""

	def __init__(
		self,
		tool_name: str,
		prompt_engine: Optional[Any] = None,
		template: Optional[str] = None
	):
		self.tool_name = tool_name
		self.prompt_engine = prompt_engine
		self.template = template
		self._context: Dict[str, Any] = {}
		self._code_blocks: List[CodeBlock] = []
		self._constraints: List[str] = []
		self._instructions: List[str] = []
		self._examples: List[Dict[str, str]] = []

	def add_context(self, key: str, value: Any) -> Self:
		"""Add a context key-value pair."""
		self._context[key] = value
		return self

	def add_code_block(self, code: str, language: str = "text", metadata: Optional[Dict] = None) -> Self:
		"""Add a code block to the prompt."""
		self._code_blocks.append(CodeBlock(
			language=language,
			code=code,
			metadata=metadata or {}
		))
		return self

	def add_constraint(self, constraint: str) -> Self:
		"""Add a constraint instruction."""
		self._constraints.append(constraint)
		return self

	def add_instruction(self, instruction: str) -> Self:
		"""Add a general instruction."""
		self._instructions.append(instruction)
		return self

	def add_example(self, input_desc: str, output: str, description: str = "") -> Self:
		"""Add an example to the prompt."""
		self._examples.append({
			"input": input_desc,
			"output": output,
			"description": description
		})
		return self

	def build(self) -> str:
		"""Build the final prompt string."""
		parts = []

		# Header with tool context
		parts.append(f"# {self.tool_name.replace('_', ' ').title()}")
		parts.append("")

		# Instructions
		if self._instructions:
			parts.append("## Instructions")
			for instruction in self._instructions:
				parts.append(f"- {instruction}")
			parts.append("")

		# Context
		if self._context:
			parts.append("## Context")
			for key, value in self._context.items():
				parts.append(f"**{key}**: {value}")
			parts.append("")

		# Code blocks
		if self._code_blocks:
			parts.append("## Code")
			for block in self._code_blocks:
				parts.append(f"```{block.language}")
				parts.append(block.code)
				parts.append("```")
				parts.append("")

		# Constraints
		if self._constraints:
			parts.append("## Constraints")
			for constraint in self._constraints:
				parts.append(f"- {constraint}")
			parts.append("")

		# Examples
		if self._examples:
			parts.append("## Examples")
			for i, example in enumerate(self._examples, 1):
				if example["description"]:
					parts.append(f"### Example {i}: {example['description']}")
				else:
					parts.append(f"### Example {i}")
				parts.append(f"**Input**: {example['input']}")
				parts.append(f"**Output**: {example['output']}")
				parts.append("")

		return "\n".join(parts)

	async def build_optimized(self, language: Optional[str] = None) -> str:
		"""Build using the prompt engine if available."""
		if self.prompt_engine is None:
			return self.build()

		from ...prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine

		if isinstance(self.prompt_engine, EnhancedPromptEngine):
			try:
				prompt, version = await self.prompt_engine.get_optimized_prompt(
					tool_name=self.tool_name,
					context=self._context,
					language=language
				)
				return prompt
			except Exception:
				return self.build()

		return self.build()


class LLMResponseParser:
	"""
	Parser for cleaning and extracting data from LLM responses.
	
	Handles various output formats:
	- JSON blocks with markdown fences
	- Code blocks
	- Plain text with structure hints
	"""

	@staticmethod
	def clean_markdown(raw: str) -> str:
		"""Remove markdown fences and clean up response."""
		clean = raw.strip()

		# Remove opening fence
		if clean.startswith('```'):
			lines = clean.split('\n', 1)
			if len(lines) > 1:
				clean = lines[1]
			else:
				clean = ""

		# Remove closing fence
		if clean.endswith('```'):
			clean = clean.rsplit('```', 1)[0]

		return clean.strip()

	@staticmethod
	def parse_json(raw: str, strict: bool = False) -> Dict[str, Any]:
		"""
		Parse JSON from LLM response, handling markdown fences.
		
		Args:
			raw: Raw LLM response string
			strict: If True, raise on parse errors. If False, return empty dict.
		
		Returns:
			Parsed JSON as dictionary
		
		Raises:
			json.JSONDecodeError: If strict=True and parsing fails
		"""
		clean = LLMResponseParser.clean_markdown(raw)

		# Try to find JSON block
		json_match = re.search(r'\{.*\}', clean, re.DOTALL)
		if json_match:
			clean = json_match.group(0)

		try:
			return json.loads(clean)
		except json.JSONDecodeError as e:
			if strict:
				raise
			return {}

	@staticmethod
	def parse_code_blocks(raw: str) -> List[CodeBlock]:
		"""
		Extract all code blocks from response.
		
		Returns:
			List of CodeBlock objects with language and code
		"""
		pattern = r'```(\w+)?\n(.*?)```'
		matches = re.findall(pattern, raw, re.DOTALL)

		blocks = []
		for lang, code in matches:
			blocks.append(CodeBlock(
				language=lang or "text",
				code=code.strip()
			))
		return blocks

	@staticmethod
	def extract_first_code_block(raw: str, language: Optional[str] = None) -> Optional[str]:
		"""
		Extract the first code block, optionally filtering by language.
		
		Args:
			raw: Raw response string
			language: Optional language filter (e.g., 'python', 'json')
		
		Returns:
			Code string or None if not found
		"""
		blocks = LLMResponseParser.parse_code_blocks(raw)

		if language:
			for block in blocks:
				if block.language.lower() == language.lower():
					return block.code
			return None

		return blocks[0].code if blocks else None

	@staticmethod
	def parse_list(raw: str, delimiter: str = r'[-*]') -> List[str]:
		"""
		Parse a markdown list from response.
		
		Args:
			raw: Raw response containing list
			delimiter: Regex pattern for list item markers
		
		Returns:
			List of cleaned item strings
		"""
		pattern = f'^{delimiter}\\s*(.+)$'
		matches = re.findall(pattern, raw, re.MULTILINE)
		return [m.strip() for m in matches]

	@staticmethod
	def extract_section(raw: str, section_title: str) -> Optional[str]:
		"""
		Extract content under a specific section heading.
		
		Args:
			raw: Raw response text
			section_title: Section heading to find (without #)
		
		Returns:
			Content under section or None
		"""
		pattern = f'##?\\s*{re.escape(section_title)}\\s*\\n(.*?)(?=##|\\Z)'
		match = re.search(pattern, raw, re.IGNORECASE | re.DOTALL)
		return match.group(1).strip() if match else None
