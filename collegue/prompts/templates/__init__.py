"""
Templates Module - Gestion des templates YAML pour les outils MCP
"""

import os
from pathlib import Path
from typing import Dict, Any, Optional

def get_template_path(tool_name: str, version: str = "default") -> Optional[Path]:
    """
    Récupère le chemin d'un template pour un outil donné.
    
    Args:
        tool_name: Nom de l'outil
        version: Version du template (default, v2, experimental, etc.)
    
    Returns:
        Path vers le fichier template ou None si non trouvé
    """
    template_dir = Path(__file__).parent / "tools" / tool_name
    template_file = template_dir / f"{version}.yaml"
    
    if template_file.exists():
        return template_file
    return None

def list_available_templates() -> Dict[str, list]:
    """
    Liste tous les templates disponibles par outil.
    
    Returns:
        Dict avec les outils comme clés et les versions disponibles comme valeurs
    """
    tools_dir = Path(__file__).parent / "tools"
    available = {}
    
    if tools_dir.exists():
        for tool_dir in tools_dir.iterdir():
            if tool_dir.is_dir():
                tool_name = tool_dir.name
                versions = []
                for template_file in tool_dir.glob("*.yaml"):
                    version_name = template_file.stem
                    versions.append(version_name)
                if versions:
                    available[tool_name] = sorted(versions)
    
    return available

__all__ = ['get_template_path', 'list_available_templates']
