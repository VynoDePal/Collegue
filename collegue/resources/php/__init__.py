"""
Ressources PHP - Module pour les références et la documentation PHP
"""
from typing import Any

try:
	from fastmcp import FastMCP
except ImportError:
	FastMCP = Any


def register(app: FastMCP, app_state: dict):
	"""
	Enregistre les ressources PHP dans l'application FastMCP.

	Args:
		app: L'application FastMCP
		app_state: L'état de l'application
	"""
	from .standard_library import register_stdlib
	from .frameworks import register_frameworks
	from .best_practices import register_best_practices

	register_stdlib(app, app_state)
	register_frameworks(app, app_state)
	register_best_practices(app, app_state)
