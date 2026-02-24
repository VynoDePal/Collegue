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
from .tool import SecretScanTool
from .models import SecretScanRequest, SecretScanResponse, SecretFinding

__all__ = [
    'SecretScanTool',
    'SecretScanRequest',
    'SecretScanResponse',
    'SecretFinding'
]
