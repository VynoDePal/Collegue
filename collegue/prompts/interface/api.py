"""
API - Interface de personnalisation des prompts
"""
import logging
from typing import Dict, List, Optional, Any, Union
from fastapi import APIRouter, HTTPException, Depends, Query, Body, Path
from pydantic import BaseModel

from ..engine import PromptEngine, PromptTemplate, PromptCategory, PromptVariable, PromptVariableType

# Configurer le logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PromptVariableCreate(BaseModel):
    """Modèle pour la création d'une variable de prompt."""
    name: str
    description: str
    type: str = "string"
    required: bool = True
    default: Optional[Any] = None
    options: Optional[List[Any]] = None
    example: Optional[Any] = None


class PromptTemplateCreate(BaseModel):
    """Modèle pour la création d'un template de prompt."""
    name: str
    description: str
    template: str
    variables: List[PromptVariableCreate] = []
    category: str
    tags: List[str] = []
    provider_specific: Dict[str, str] = {}
    examples: List[Dict[str, Any]] = []
    is_public: bool = False


class PromptTemplateUpdate(BaseModel):
    """Modèle pour la mise à jour d'un template de prompt."""
    name: Optional[str] = None
    description: Optional[str] = None
    template: Optional[str] = None
    variables: Optional[List[PromptVariableCreate]] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    provider_specific: Optional[Dict[str, str]] = None
    examples: Optional[List[Dict[str, Any]]] = None
    is_public: Optional[bool] = None


class PromptCategoryCreate(BaseModel):
    """Modèle pour la création d'une catégorie de prompt."""
    id: str
    name: str
    description: str
    parent_id: Optional[str] = None
    icon: Optional[str] = None


class PromptCategoryUpdate(BaseModel):
    """Modèle pour la mise à jour d'une catégorie de prompt."""
    name: Optional[str] = None
    description: Optional[str] = None
    parent_id: Optional[str] = None
    icon: Optional[str] = None


class FormatPromptRequest(BaseModel):
    """Modèle pour la requête de formatage d'un prompt."""
    variables: Dict[str, Any]
    provider: Optional[str] = None


def get_prompt_engine():
    """Récupère l'instance du moteur de prompts."""
    # Dans un contexte réel, cette fonction pourrait être configurée pour
    # récupérer l'instance depuis un état d'application partagé
    return PromptEngine()


def register_prompt_interface(app, app_state):
    """Enregistre les endpoints de l'interface de personnalisation des prompts."""
    
    # Créer un router pour les endpoints de prompts personnalisés
    router = APIRouter(prefix="/prompts", tags=["prompts"])
    
    # Initialiser le moteur de prompts et le stocker dans l'état de l'application
    prompt_engine = PromptEngine()
    app_state["prompt_engine"] = prompt_engine
    
    @router.get("/templates")
    async def list_templates(
        category: Optional[str] = None,
        tags: Optional[List[str]] = Query(None),
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Liste tous les templates de prompts disponibles."""
        if category:
            templates = engine.get_templates_by_category(category)
        elif tags:
            templates = engine.get_templates_by_tags(tags)
        else:
            templates = engine.get_all_templates()
        
        return {
            "count": len(templates),
            "templates": [t.model_dump() for t in templates]
        }
    
    @router.get("/templates/{template_id}")
    async def get_template(
        template_id: str = Path(..., description="ID du template à récupérer"),
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Récupère un template spécifique par son ID."""
        template = engine.get_template(template_id)
        if not template:
            raise HTTPException(status_code=404, detail=f"Template {template_id} non trouvé")
        
        return template.model_dump()
    
    @router.post("/templates")
    async def create_template(
        template_data: PromptTemplateCreate,
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Crée un nouveau template de prompt."""
        try:
            # Convertir le modèle Pydantic en dictionnaire
            data = template_data.model_dump()
            
            # Créer le template
            template = engine.create_template(data)
            
            return {
                "message": "Template créé avec succès",
                "template_id": template.id,
                "template": template.model_dump()
            }
        except Exception as e:
            logger.error(f"Erreur lors de la création du template: {str(e)}")
            raise HTTPException(status_code=400, detail=f"Erreur lors de la création du template: {str(e)}")
    
    @router.put("/templates/{template_id}")
    async def update_template(
        template_id: str,
        template_data: PromptTemplateUpdate,
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Met à jour un template existant."""
        # Vérifier que le template existe
        if not engine.get_template(template_id):
            raise HTTPException(status_code=404, detail=f"Template {template_id} non trouvé")
        
        try:
            # Convertir le modèle Pydantic en dictionnaire
            data = template_data.model_dump(exclude_unset=True)
            
            # Mettre à jour le template
            updated_template = engine.update_template(template_id, data)
            
            return {
                "message": "Template mis à jour avec succès",
                "template": updated_template.model_dump()
            }
        except Exception as e:
            logger.error(f"Erreur lors de la mise à jour du template: {str(e)}")
            raise HTTPException(status_code=400, detail=f"Erreur lors de la mise à jour du template: {str(e)}")
    
    @router.delete("/templates/{template_id}")
    async def delete_template(
        template_id: str,
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Supprime un template."""
        # Vérifier que le template existe
        if not engine.get_template(template_id):
            raise HTTPException(status_code=404, detail=f"Template {template_id} non trouvé")
        
        success = engine.delete_template(template_id)
        
        if success:
            return {"message": f"Template {template_id} supprimé avec succès"}
        else:
            raise HTTPException(status_code=500, detail=f"Erreur lors de la suppression du template {template_id}")
    
    @router.post("/templates/{template_id}/format")
    async def format_template(
        template_id: str,
        format_request: FormatPromptRequest,
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Formate un template avec les variables fournies."""
        formatted = engine.format_prompt(
            template_id, 
            format_request.variables, 
            format_request.provider
        )
        
        if formatted:
            return {"formatted_prompt": formatted}
        else:
            raise HTTPException(
                status_code=400, 
                detail=f"Erreur lors du formatage du template {template_id}"
            )
    
    @router.get("/categories")
    async def list_categories(engine: PromptEngine = Depends(get_prompt_engine)):
        """Liste toutes les catégories de prompts."""
        categories = engine.get_all_categories()
        
        return {
            "count": len(categories),
            "categories": [c.model_dump() for c in categories]
        }
    
    @router.post("/categories")
    async def create_category(
        category_data: PromptCategoryCreate,
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Crée une nouvelle catégorie de prompts."""
        try:
            # Convertir le modèle Pydantic en dictionnaire
            data = category_data.model_dump()
            
            # Créer la catégorie
            category = engine.create_category(data)
            
            return {
                "message": "Catégorie créée avec succès",
                "category": category.model_dump()
            }
        except Exception as e:
            logger.error(f"Erreur lors de la création de la catégorie: {str(e)}")
            raise HTTPException(status_code=400, detail=f"Erreur lors de la création de la catégorie: {str(e)}")
    
    @router.get("/history")
    async def get_execution_history(
        limit: int = 20,
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Récupère l'historique des exécutions de prompts."""
        history = engine.get_execution_history(limit)
        
        return {
            "count": len(history),
            "history": [h.model_dump() for h in history]
        }
    
    # Enregistrer le router dans l'application
    app.include_router(router)
    
    # Enregistrer dans le gestionnaire de ressources
    if "resource_manager" in app_state:
        app_state["resource_manager"].register_resource(
            "prompts",
            {
                "description": "Système de prompts personnalisés",
                "engine": prompt_engine,
                "get_template": prompt_engine.get_template,
                "get_all_templates": prompt_engine.get_all_templates,
                "get_templates_by_category": prompt_engine.get_templates_by_category,
                "format_prompt": prompt_engine.format_prompt
            }
        )
