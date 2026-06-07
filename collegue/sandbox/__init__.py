"""Sandbox d'exécution isolé du moteur autonome (C8, brief §6).

Exécute le code généré dans un conteneur Docker durci — jamais sur l'hôte.
Module **isolé**, non câblé au runtime (le pilote Phase 3 le câblera ;
l'intégration OpenHands est différée en Phase 2).
"""

from collegue.sandbox.executor import (
    DEFAULT_SANDBOX_IMAGE,
    DockerSandbox,
    SandboxResult,
    SandboxUnavailable,
)

__all__ = [
    "DockerSandbox",
    "SandboxResult",
    "SandboxUnavailable",
    "DEFAULT_SANDBOX_IMAGE",
]
