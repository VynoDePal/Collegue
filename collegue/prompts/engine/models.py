"""
Models - Modèles de données pour le système de prompts personnalisés
"""
from pydantic import BaseModel, Field
from typing import Dict, List, Optional, Any, Union
from enum import Enum
import datetime
import uuid


class PromptVariableType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    CODE = "code"
    LIST = "list"
    OBJECT = "object"

class PromptVariable(BaseModel):
    name: str
    description: str
    type: PromptVariableType = PromptVariableType.STRING
    required: bool = True
    default: Optional[Any] = None
    options: Optional[List[Any]] = None
    example: Optional[Any] = None

class PromptTemplate(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str
    template: str
    variables: List[PromptVariable] = []
    category: str
    tags: List[str] = []
    provider_specific: Dict[str, str] = {}
    examples: List[Dict[str, Any]] = []
    created_at: datetime.datetime = Field(default_factory=datetime.datetime.now)
    updated_at: datetime.datetime = Field(default_factory=datetime.datetime.now)
    created_by: Optional[str] = None
    is_public: bool = False
    version: str = "1.0.0"

class PromptCategory(BaseModel):
    id: str
    name: str
    description: str
    parent_id: Optional[str] = None
    icon: Optional[str] = None

class PromptExecution(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    template_id: str
    variables: Dict[str, Any]
    provider: Optional[str] = None
    formatted_prompt: str
    result: Optional[str] = None
    execution_time: float
    timestamp: datetime.datetime = Field(default_factory=datetime.datetime.now)
    user_id: Optional[str] = None
    feedback: Optional[Dict[str, Any]] = None

class PromptLibrary(BaseModel):
    templates: Dict[str, PromptTemplate] = {}
    categories: Dict[str, PromptCategory] = {}
    history: List[PromptExecution] = []
