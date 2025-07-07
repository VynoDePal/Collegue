"""
Models - Modèles de données pour le système de prompts personnalisés
"""
from pydantic import BaseModel, Field
from typing import Dict, List, Optional, Any, Union
from enum import Enum
import datetime
import uuid


class PromptVariableType(str, Enum):
    """Types de variables supportés dans les prompts personnalisés."""
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    CODE = "code"
    LIST = "list"
    OBJECT = "object"


class PromptVariable(BaseModel):
    """Définition d'une variable de prompt avec validation."""
    name: str
    description: str
    type: PromptVariableType = PromptVariableType.STRING
    required: bool = True
    default: Optional[Any] = None
    options: Optional[List[Any]] = None  # Pour les variables avec choix limités
    example: Optional[Any] = None


class PromptTemplate(BaseModel):
    """Modèle complet pour un template de prompt personnalisé."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str
    template: str
    variables: List[PromptVariable] = []
    category: str
    tags: List[str] = []
    provider_specific: Dict[str, str] = {}  # Versions spécifiques par fournisseur
    examples: List[Dict[str, Any]] = []
    created_at: datetime.datetime = Field(default_factory=datetime.datetime.now)
    updated_at: datetime.datetime = Field(default_factory=datetime.datetime.now)
    created_by: Optional[str] = None
    is_public: bool = False
    version: str = "1.0.0"


class PromptCategory(BaseModel):
    """Catégorie de prompts pour l'organisation."""
    id: str
    name: str
    description: str
    parent_id: Optional[str] = None
    icon: Optional[str] = None


class PromptExecution(BaseModel):
    """Enregistrement d'une exécution de prompt."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    template_id: str
    variables: Dict[str, Any]
    provider: Optional[str] = None
    formatted_prompt: str
    result: Optional[str] = None
    execution_time: float  # en secondes
    timestamp: datetime.datetime = Field(default_factory=datetime.datetime.now)
    user_id: Optional[str] = None
    feedback: Optional[Dict[str, Any]] = None


class PromptLibrary(BaseModel):
    """Bibliothèque complète de prompts personnalisés."""
    templates: Dict[str, PromptTemplate] = {}
    categories: Dict[str, PromptCategory] = {}
    history: List[PromptExecution] = []
