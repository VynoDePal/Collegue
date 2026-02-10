"""
Base Scanner for IaC Guardrails.

Provides common functionality for IaC-specific scanners.
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from ..base import ToolError


class ScannerError(ToolError):
	"""Erreur spécifique au scanner."""
	pass


class IacFinding:
	"""Résultat de détection d'une issue IaC (copie locale pour éviter import circulaire)."""

	def __init__(
		self,
		rule_id: str,
		rule_title: str,
		severity: str,
		path: str,
		line: int,
		message: str,
		remediation: str = None,
		references: List[str] = None,
		engine: str = ""
	):
		self.rule_id = rule_id
		self.rule_title = rule_title
		self.severity = severity
		self.path = path
		self.line = line
		self.message = message
		self.remediation = remediation
		self.references = references or []
		self.engine = engine


class BaseScanner(ABC):
	"""Classe de base pour les scanners IaC."""

	def __init__(self, logger=None):
		self.logger = logger

	@abstractmethod
	def scan(self, content: str, filepath: str, profile: str) -> List[IacFinding]:
		"""Scanne le contenu et retourne les findings."""
		pass

	def _log_debug(self, message: str):
		if self.logger:
			self.logger.debug(message)

	def _log_warning(self, message: str):
		if self.logger:
			self.logger.warning(message)
