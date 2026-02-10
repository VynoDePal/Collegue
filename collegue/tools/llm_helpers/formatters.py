"""
Formatters for LLM responses.

Provides standardized formatting of LLM outputs into various formats:
- Markdown with structure
- JSON with schema validation hints
- Plain text cleanup
"""
import json
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class ResponseFormatter(ABC):
	"""Base class for response formatters."""

	@abstractmethod
	def format(self, raw_response: str, **kwargs) -> str:
		"""Format the raw response into the target format."""
		pass


class MarkdownFormatter(ResponseFormatter):
	"""
	Formatter for markdown documentation output.
	
	Cleans and structures markdown from LLM responses.
	"""

	def __init__(
		self,
		remove_fences: bool = True,
		enforce_headers: bool = False,
		max_header_level: int = 3
	):
		self.remove_fences = remove_fences
		self.enforce_headers = enforce_headers
		self.max_header_level = max_header_level

	def format(self, raw_response: str, **kwargs) -> str:
		"""Format markdown response."""
		text = raw_response.strip()

		if self.remove_fences:
			text = self._remove_code_fences(text)

		if self.enforce_headers:
			text = self._normalize_headers(text)

		return text

	def _remove_code_fences(self, text: str) -> str:
		"""Remove markdown code fences if the whole content is wrapped."""
		if text.startswith('```markdown'):
			text = text[11:]  # Remove ```markdown
			if text.endswith('```'):
				text = text[:-3]
		return text.strip()

	def _normalize_headers(self, text: str) -> str:
		"""Ensure headers don't exceed max level."""
		lines = text.split('\n')
		result = []

		for line in lines:
			match = re.match(r'^(#{1,6})\s+(.+)$', line)
			if match:
				level = len(match.group(1))
				if level > self.max_header_level:
					line = '#' * self.max_header_level + ' ' + match.group(2)
			result.append(line)

		return '\n'.join(result)

	@staticmethod
	def extract_section(text: str, section_name: str) -> Optional[str]:
		"""Extract a specific section by name."""
		pattern = rf'^#{1,6}\s*{re.escape(section_name)}.*?$(.*?)(?=^#{1,6}|\Z)'
		match = re.search(pattern, text, re.MULTILINE | re.IGNORECASE | re.DOTALL)
		return match.group(1).strip() if match else None

	@staticmethod
	def add_table_of_contents(text: str) -> str:
		"""Add TOC based on headers."""
		headers = re.findall(r'^#{2,3}\s+(.+)$', text, re.MULTILINE)
		if not headers:
			return text

		toc = ["## Table of Contents\n"]
		for header in headers:
			anchor = header.lower().replace(' ', '-').replace('.', '')
			level = 2 if header in re.findall(r'^##\s+(.+)$', text, re.MULTILINE) else 3
			indent = "  " if level == 3 else ""
			toc.append(f"{indent}- [{header}](#{anchor})")

		return '\n'.join(toc) + '\n\n' + text


class JSONFormatter(ResponseFormatter):
	"""
	Formatter for JSON structured output.
	
	Cleans and validates JSON from LLM responses.
	"""

	def __init__(
		self,
		indent: int = 2,
		sort_keys: bool = False,
		ensure_ascii: bool = False
	):
		self.indent = indent
		self.sort_keys = sort_keys
		self.ensure_ascii = ensure_ascii

	def format(self, raw_response: str, **kwargs) -> str:
		"""Format and pretty-print JSON."""
		try:
			# Try to extract JSON if wrapped in markdown
			clean = raw_response.strip()
			if clean.startswith('```json'):
				clean = clean[7:]
				if clean.endswith('```'):
					clean = clean[:-3]
			elif clean.startswith('```'):
				clean = clean[3:]
				if clean.endswith('```'):
					clean = clean[:-3]

			# Try to find JSON object/array
			json_match = re.search(r'([\{\[].*[\}\]])', clean, re.DOTALL)
			if json_match:
				clean = json_match.group(1)

			data = json.loads(clean)
			return json.dumps(
				data,
				indent=self.indent,
				sort_keys=self.sort_keys,
				ensure_ascii=self.ensure_ascii
			)
		except json.JSONDecodeError as e:
			# Return original with error marker if parsing fails
			return f"<!-- JSON Parse Error: {e} -->\n{raw_response}"

	@staticmethod
	def validate_schema(data: Dict, required_fields: List[str]) -> bool:
		"""Validate that required fields exist in data."""
		return all(field in data for field in required_fields)

	@staticmethod
	def extract_field(raw_response: str, field_path: str) -> Optional[Any]:
		"""
		Extract a field from JSON using dot notation path.
		
		Args:
			field_path: Dot-separated path like 'data.items.0.name'
		"""
		try:
			# Clean and parse
			clean = raw_response.strip()
			if clean.startswith('```'):
				clean = clean.split('\n', 1)[1].rsplit('```', 1)[0]

			data = json.loads(clean)

			# Navigate path
			parts = field_path.split('.')
			current = data
			for part in parts:
				if part.isdigit():
					current = current[int(part)]
				else:
					current = current.get(part)
				if current is None:
					return None

			return current
		except (json.JSONDecodeError, KeyError, IndexError, TypeError):
			return None


class PlainTextFormatter(ResponseFormatter):
	"""
	Simple formatter that cleans up whitespace and structure.
	"""

	def __init__(
		self,
		remove_extra_whitespace: bool = True,
		wrap_lines: Optional[int] = None
	):
		self.remove_extra_whitespace = remove_extra_whitespace
		self.wrap_lines = wrap_lines

	def format(self, raw_response: str, **kwargs) -> str:
		"""Clean and format plain text."""
		text = raw_response

		if self.remove_extra_whitespace:
			# Replace multiple newlines with single
			text = re.sub(r'\n{3,}', '\n\n', text)
			# Remove trailing whitespace
			text = '\n'.join(line.rstrip() for line in text.split('\n'))

		if self.wrap_lines:
			text = self._wrap_text(text, self.wrap_lines)

		return text.strip()

	def _wrap_text(self, text: str, width: int) -> str:
		"""Simple text wrapping."""
		lines = text.split('\n')
		result = []

		for line in lines:
			if len(line) <= width:
				result.append(line)
			else:
				# Simple wrap at word boundaries
				words = line.split(' ')
				current = ''
				for word in words:
					if len(current) + len(word) + 1 <= width:
						current += (' ' if current else '') + word
					else:
						if current:
							result.append(current)
						current = word
				if current:
					result.append(current)

		return '\n'.join(result)
