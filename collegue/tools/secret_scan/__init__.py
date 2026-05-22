"""
Secret Scan - Outil de détection de secrets dans le code.

Ce module a été refactorisé pour respecter l'architecture modulaire:
- models.py : Modèles Pydantic
- config.py : Patterns regex et configurations
- engine.py : Logique métier pure
- tool.py : Orchestration du Tool

Usage:
    from collegue.tools.secret_scan import SecretScanTool
"""

from .models import SecretFinding, SecretScanRequest, SecretScanResponse
from .tool import SecretScanTool

__all__ = ["SecretScanTool", "SecretScanRequest", "SecretScanResponse", "SecretFinding"]
