"""
Base Analyzer for Repo Consistency Check.

Provides common functionality for language-specific analyzers.
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Any
from ..base import ToolError


class AnalyzerError(ToolError):
	"""Erreur spécifique à l'analyseur."""
	pass

class BaseAnalyzer(ABC):
	def __init__(self, logger=None):
		self.logger = logger

	@abstractmethod
	def analyze_unused_imports(self, code: str, filepath: str) -> List[Dict[str, Any]]:
		"""Détecte les imports non utilisés."""
		pass

	@abstractmethod
	def analyze_unused_vars(self, code: str, filepath: str) -> List[Dict[str, Any]]:
		"""Détecte les variables non utilisées."""
		pass

	def _log_debug(self, message: str):
		if self.logger:
			self.logger.debug(message)

	def _log_warning(self, message: str):
		if self.logger:
			self.logger.warning(message)
