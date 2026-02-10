"""
Template registry for LLM prompts.

Provides a centralized registry for prompt templates used across
different LLM-based tools. Templates can be loaded from YAML files
or defined programmatically.
"""
from typing import Any, Dict, List, Optional
from pathlib import Path
import yaml


class TemplateRegistry:
	"""
	Registry for prompt templates.
	
	Loads and caches templates from YAML files in the templates directory.
	"""

	_instance = None
	_templates: Dict[str, Dict[str, Any]] = {}
	_loaded_files: set = set()

	def __new__(cls):
		if cls._instance is None:
			cls._instance = super().__new__(cls)
		return cls._instance

	def __init__(self):
		self._templates_dir = Path(__file__).parent / 'templates'

	def load_template(self, name: str) -> Optional[Dict[str, Any]]:
		"""
		Load a template by name.
		
		Args:
			name: Template name (e.g., 'documentation', 'refactoring')
		
		Returns:
			Template dict or None if not found
		"""
		# Check cache first
		if name in self._templates:
			return self._templates[name]

		# Try to load from file
		template_file = self._templates_dir / f"{name}.yaml"
		if template_file.exists():
			with open(template_file, 'r', encoding='utf-8') as f:
				template = yaml.safe_load(f)
				self._templates[name] = template
				return template

		return None

	def register_template(self, name: str, template: Dict[str, Any]) -> None:
		"""Register a template programmatically."""
		self._templates[name] = template

	def get_template(self, name: str, variant: Optional[str] = None) -> Optional[str]:
		"""
		Get a specific template or variant.
		
		Args:
			name: Template name
			variant: Optional variant (e.g., 'detailed', 'minimal')
		
		Returns:
			Template string or None
		"""
		template = self.load_template(name)
		if template is None:
			return None

		if variant and 'variants' in template:
			return template['variants'].get(variant, template.get('default', ''))

		return template.get('default', template.get('template', ''))

	def list_templates(self) -> List[str]:
		"""List all available template names."""
		# Get from cache
		names = set(self._templates.keys())

		# Scan directory for more
		if self._templates_dir.exists():
			for f in self._templates_dir.glob('*.yaml'):
				names.add(f.stem)

		return sorted(names)

	def get_template_info(self, name: str) -> Optional[Dict[str, Any]]:
		"""
		Get metadata about a template.
		
		Returns dict with keys like: description, variables, variants
		"""
		template = self.load_template(name)
		if template is None:
			return None

		return {
			'name': name,
			'description': template.get('description', ''),
			'variables': template.get('variables', []),
			'variants': list(template.get('variants', {}).keys()) if 'variants' in template else [],
			'default_variant': template.get('default_variant', 'default')
		}

	def clear_cache(self) -> None:
		"""Clear the template cache."""
		self._templates.clear()
		self._loaded_files.clear()


# Module-level convenience functions
_registry = TemplateRegistry()


def get_template(name: str, variant: Optional[str] = None) -> Optional[str]:
	"""Get a template by name and optional variant."""
	return _registry.get_template(name, variant)


def list_templates() -> List[str]:
	"""List all available template names."""
	return _registry.list_templates()


def register_template(name: str, template: Dict[str, Any]) -> None:
	"""Register a template programmatically."""
	_registry.register_template(name, template)


def get_template_info(name: str) -> Optional[Dict[str, Any]]:
	"""Get metadata about a template."""
	return _registry.get_template_info(name)
