"""
Web - Interface web pour la personnalisation des prompts
"""
import os
import logging
import json
from typing import Dict, List, Optional, Any
from fastapi import APIRouter, Request, Depends, HTTPException, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from ..engine import PromptEngine
from ..engine.models import PromptVariable, PromptVariableType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

current_dir = os.path.dirname(os.path.abspath(__file__))
templates_dir = os.path.join(current_dir, "templates")
static_dir = os.path.join(current_dir, "static")

os.makedirs(templates_dir, exist_ok=True)
os.makedirs(static_dir, exist_ok=True)

templates = Jinja2Templates(directory=templates_dir)


def get_prompt_engine():
    """Récupère l'instance du moteur de prompts."""
    return PromptEngine()


def register_web_interface(app, app_state):
    """Enregistre l'interface web pour la personnalisation des prompts."""

    router = APIRouter(prefix="/prompts/ui", tags=["prompts_ui"])

    app.mount("/prompts/static", StaticFiles(directory=static_dir), name="prompts_static")

    prompt_engine = app_state.get("prompt_engine")
    if not prompt_engine:
        prompt_engine = PromptEngine()
        app_state["prompt_engine"] = prompt_engine

    @router.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        """Page d'accueil de l'interface de personnalisation des prompts."""
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "title": "Système de Prompts Personnalisés"}
        )

    @router.get("/templates", response_class=HTMLResponse)
    async def list_templates_page(
        request: Request,
        category: Optional[str] = None,
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Page listant tous les templates de prompts."""
        if category:
            templates_list = engine.get_templates_by_category(category)
            title = f"Templates de la catégorie {category}"
        else:
            templates_list = engine.get_all_templates()
            title = "Tous les templates"

        categories = engine.get_all_categories()

        return templates.TemplateResponse(
            "templates_list.html",
            {
                "request": request,
                "title": title,
                "templates": templates_list,
                "categories": categories,
                "current_category": category
            }
        )

    @router.get("/templates/new", response_class=HTMLResponse)
    async def create_template_page(
        request: Request,
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Page de création d'un nouveau template."""
        categories = engine.get_all_categories()
        return templates.TemplateResponse(
            "template_form.html",
            {
                "request": request,
                "title": "Nouveau template",
                "categories": categories,
                "template": None,
                "action": "/prompts/ui/templates/new",
                "is_new": True,
            },
        )

    @router.get("/templates/{template_id}", response_class=HTMLResponse)
    async def view_template_page(
        request: Request,
        template_id: str,
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Page de visualisation d'un template spécifique."""
        template = engine.get_template(template_id)
        if not template:
            raise HTTPException(status_code=404, detail=f"Template {template_id} non trouvé")

        return templates.TemplateResponse(
            "template_view.html",
            {"request": request, "title": template.name, "template": template}
        )

    @router.post("/templates/new")
    async def create_template(
        request: Request,
        name: str = Form(...),
        description: str = Form(...),
        template: str = Form(...),
        category: str = Form(...),
        variables: str = Form("[]"),
        tags: str = Form(""),
        is_public: str = Form("false"),
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Traitement du formulaire de création de template."""
        try:
            variables_data = json.loads(variables)
            prompt_variables = []
            for var in variables_data:
                var_type = PromptVariableType(var.get("type", "string"))
                prompt_variables.append(
                    PromptVariable(
                        name=var["name"],
                        description=var["description"],
                        type=var_type,
                        required=var.get("required", True),
                        default=var.get("default"),
                        example=var.get("example")
                    )
                )

            tags_list = [tag.strip() for tag in tags.split(",") if tag.strip()]

            template_data = {
                "name": name,
                "description": description,
                "template": template,
                "category": category,
                "variables": prompt_variables,
                "tags": tags_list,
                "is_public": is_public.lower() == "true"
            }

            new_template = engine.create_template(template_data)
            logger.info(f"Template créé avec succès: {new_template.id}")

            redirect_url = f"/prompts/ui/templates/{new_template.id}"
            return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)

        except Exception as e:
            logger.error(f"Erreur lors de la création du template: {str(e)}")
            raise HTTPException(
                status_code=400,
                detail=f"Erreur lors de la création du template: {str(e)}"
            )

    @router.get("/templates/{template_id}/edit", response_class=HTMLResponse)
    async def edit_template_page(
        request: Request,
        template_id: str,
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Page d'édition d'un template existant."""
        template = engine.get_template(template_id)
        if not template:
            raise HTTPException(status_code=404, detail=f"Template {template_id} non trouvé")

        categories = engine.get_all_categories()
        variable_types = ["string", "integer", "float", "boolean", "code", "list", "object"]

        return templates.TemplateResponse(
            "template_form.html",
            {
                "request": request,
                "title": f"Éditer {template.name}",
                "template": template,
                "categories": categories,
                "variable_types": variable_types,
                "is_new": False
            }
        )

    @router.post("/templates/{template_id}/edit")
    async def update_template(
        request: Request,
        template_id: str,
        name: str = Form(...),
        description: str = Form(...),
        template: str = Form(...),
        category: str = Form(...),
        variables: str = Form("[]"),
        tags: str = Form(""),
        is_public: str = Form("false"),
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Traitement du formulaire de mise à jour de template."""
        try:
            existing_template = engine.get_template(template_id)
            if not existing_template:
                raise HTTPException(status_code=404, detail=f"Template {template_id} non trouvé")

            variables_data = json.loads(variables)
            prompt_variables = []
            for var in variables_data:
                var_type = PromptVariableType(var.get("type", "string"))
                prompt_variables.append(
                    PromptVariable(
                        name=var["name"],
                        description=var["description"],
                        type=var_type,
                        required=var.get("required", True),
                        default=var.get("default"),
                        example=var.get("example")
                    )
                )

            tags_list = [tag.strip() for tag in tags.split(",") if tag.strip()]

            template_data = {
                "id": template_id,
                "name": name,
                "description": description,
                "template": template,
                "category": category,
                "variables": prompt_variables,
                "tags": tags_list,
                "is_public": is_public.lower() == "true"
            }

            updated_template = engine.update_template(template_id, template_data)
            logger.info(f"Template mis à jour avec succès: {updated_template.id}")

            redirect_url = f"/prompts/ui/templates/{updated_template.id}"
            return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)

        except Exception as e:
            logger.error(f"Erreur lors de la mise à jour du template: {str(e)}")
            raise HTTPException(
                status_code=400,
                detail=f"Erreur lors de la mise à jour du template: {str(e)}"
            )

    @router.get("/categories", response_class=HTMLResponse)
    async def list_categories_page(
        request: Request,
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Page listant toutes les catégories de prompts."""
        categories = engine.get_all_categories()

        return templates.TemplateResponse(
            "categories_list.html",
            {"request": request, "title": "Catégories", "categories": categories}
        )

    @router.get("/categories/new", response_class=HTMLResponse)
    async def create_category_page(
        request: Request
    ):
        """Page de création d'une nouvelle catégorie."""
        return templates.TemplateResponse(
            "category_form.html",
            {
                "request": request,
                "title": "Nouvelle catégorie",
                "category": None,
                "action": "/prompts/ui/categories/new",
                "is_new": True
            }
        )

    @router.post("/categories/new")
    async def create_category(
        request: Request,
        id: str = Form(...),
        name: str = Form(...),
        description: str = Form(...),
        parent_id: Optional[str] = Form(None),
        icon: Optional[str] = Form(None),
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Traitement du formulaire de création de catégorie."""
        try:
            category_data = {
                "id": id,
                "name": name,
                "description": description,
                "parent_id": parent_id if parent_id else None,
                "icon": icon if icon else None
            }

            new_category = engine.create_category(category_data)
            logger.info(f"Catégorie créée avec succès: {new_category.id}")

            redirect_url = "/prompts/ui/categories"
            return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)

        except Exception as e:
            logger.error(f"Erreur lors de la création de la catégorie: {str(e)}")
            raise HTTPException(
                status_code=400,
                detail=f"Erreur lors de la création de la catégorie: {str(e)}"
            )

    @router.get("/history", response_class=HTMLResponse)
    async def view_history_page(
        request: Request,
        limit: int = 50,
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Page affichant l'historique des exécutions de prompts."""
        history = engine.get_execution_history(limit)

        return templates.TemplateResponse(
            "history.html",
            {"request": request, "title": "Historique", "history": history}
        )

    @router.get("/playground", response_class=HTMLResponse)
    async def playground_page(
        request: Request,
        template_id: Optional[str] = None,
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Page du playground pour tester les templates."""
        templates_list = engine.get_all_templates()
        providers = ["openai", "anthropic", "local", "huggingface", "azure"]

        selected_template = None
        if template_id:
            selected_template = engine.get_template(template_id)

        return templates.TemplateResponse(
            "playground.html",
            {
                "request": request,
                "title": "Playground",
                "templates": templates_list,
                "selected_template": selected_template,
                "providers": providers
            }
        )

    @router.post("/playground", response_class=HTMLResponse)
    async def execute_playground(
        request: Request,
        template_id: str = Form(...),
        variables: str = Form("{}"),
        provider: Optional[str] = Form(None),
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Traitement du formulaire du playground pour exécuter un template."""
        templates_list = engine.get_all_templates()
        providers = ["openai", "anthropic", "local", "huggingface", "azure"]

        variables_dict = {}
        try:
            variables_dict = json.loads(variables)
        except json.JSONDecodeError:
            variables_dict = {}

        try:
            template = engine.get_template(template_id)
            if not template:
                return templates.TemplateResponse(
                    "playground.html",
                    {
                        "request": request,
                        "title": "Playground",
                        "templates": templates_list,
                        "selected_template": None,
                        "providers": providers,
                        "variables": variables_dict,
                        "selected_provider": provider,
                        "error": f"Template {template_id} non trouvé"
                    },
                    status_code=200
                )

            formatted_prompt = engine.format_prompt(template_id, variables_dict, provider)

            return templates.TemplateResponse(
                "playground.html",
                {
                    "request": request,
                    "title": "Playground",
                    "templates": templates_list,
                    "selected_template": template,
                    "providers": providers,
                    "variables": variables_dict,
                    "selected_provider": provider,
                    "result": formatted_prompt
                }
            )

        except Exception as e:
            logger.error(f"Erreur lors de l'exécution du template: {str(e)}")
            return templates.TemplateResponse(
                "playground.html",
                {
                    "request": request,
                    "title": "Playground",
                    "templates": templates_list,
                    "selected_template": None,
                    "providers": providers,
                    "variables": variables_dict,
                    "selected_provider": provider,
                    "error": f"Erreur: {str(e)}"
                },
                status_code=200
            )

    @router.get("/categories/{category_id}/edit", response_class=HTMLResponse)
    async def edit_category_page(
        request: Request,
        category_id: str,
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Page d'édition d'une catégorie existante."""
        category = engine.get_category(category_id)
        if not category:
            raise HTTPException(status_code=404, detail=f"Catégorie {category_id} non trouvée")

        return templates.TemplateResponse(
            "category_form.html",
            {
                "request": request,
                "title": f"Édition de la catégorie {category.name}",
                "category": category,
                "action": f"/prompts/ui/categories/{category_id}/edit",
                "is_new": False
            }
        )

    @router.post("/categories/{category_id}/edit")
    async def update_category(
        request: Request,
        category_id: str,
        name: str = Form(...),
        description: str = Form(...),
        engine: PromptEngine = Depends(get_prompt_engine)
    ):
        """Traitement du formulaire de mise à jour de catégorie."""
        try:
            existing_category = engine.get_category(category_id)
            if not existing_category:
                raise HTTPException(status_code=404, detail=f"Catégorie {category_id} non trouvée")

            category_data = {
                "id": category_id,
                "name": name,
                "description": description
            }

            updated_category = engine.update_category(category_id, category_data)
            logger.info(f"Catégorie mise à jour avec succès: {updated_category.id}")

            redirect_url = "/prompts/ui/categories"
            return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)

        except Exception as e:
            logger.error(f"Erreur lors de la mise à jour de la catégorie: {str(e)}")
            raise HTTPException(
                status_code=400,
                detail=f"Erreur lors de la mise à jour de la catégorie: {str(e)}"
            )

    app.include_router(router)
