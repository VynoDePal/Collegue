"""
Collègue MCP - Un assistant de développement intelligent inspiré par Junie
"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from fastmcp import FastMCP
from collegue.config import settings
import os
import sys

# Ajouter le répertoire parent au chemin pour pouvoir importer collegue
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# Initialisation de l'application FastMCP
app = FastMCP(
    host=settings.HOST,
    port=settings.PORT
)

# Compatibilité FastAPI → FastMCP
# De nombreux modules historiques utilisent les décorateurs FastAPI
# (`@app.get`, `@app.post`, etc.) et `app.include_router`.  
# FastMCP n'expose que `@app.resource`/`@app.tool`.  
# Nous ajoutons donc de petits wrapper afin d'éviter les erreurs sans
# devoir réécrire immédiatement tous les modules.
# ---------------------------------------------------------------------------

# Génère un décorateur pour un verbe HTTP donné qui se re-mappe vers
# `app.resource(path)`.
def _http_method(method: str):
    def _decorator(path: str, **kwargs):
        def _wrapper(func):
            # FastMCP ne supporte pas encore la distinction des méthodes HTTP
            # On enregistre simplement comme ressource.
            # FastMCP tools exigent des signatures strictes; nous ne les exposons pas
            # automatiquement. On renvoie simplement la fonction.
            # Les kwargs comme status_code, include_in_schema sont ignorés pour compatibilité
            return func
        return _wrapper
    return _decorator

# Utilitaire pour convertir un chemin style HTTP en nom de ressource FastMCP
def _norm(path: str) -> str:
    """Convertit "/api/foo/{bar}" -> "api.foo.{bar}" (sans slash initial).
    FastMCP exige des noms de ressources valides sans '/'."""
    return path.lstrip("/").replace("/", ".") or "root"

# Crée les alias @app.get, @app.post, etc.
for _m in ("get", "post", "put", "delete", "patch", "options", "head"):
    if not hasattr(app, _m):
        setattr(app, _m, _http_method(_m.upper()))

# Remplacement simple de include_router: on applique le préfixe puis
# on ajoute chaque route via app.resource. Si la structure du router
# est inconnue, on ignore silencieusement (au pire, pas d'endpoint).
def _include_router(router, prefix: str = "", **kwargs):
    # Gestion minimale : s'il possède `.routes` iterable avec objets
    # ayant `.path`, `.endpoint` et éventuellement `.methods`.
    routes = getattr(router, "routes", [])
    for r in routes:
        path = prefix + getattr(r, "path", "")
        endpoint = getattr(r, "endpoint", None)
        # Nous ignorons l'enregistrement des routes FastAPI héritées.
        # Elles ne sont pas nécessaires comme outils MCP.
        if endpoint:
            pass

# N'ajoute l'attribut que s'il n'existe pas déjà.
if not hasattr(app, "include_router"):
    setattr(app, "include_router", _include_router)

# Fallback/override pour les appels legacy `app.mount(...)`
def _mount(path: str = "", app_to_mount=None, **kwargs):
    # Ignore tous les paramètres supplémentaires (ex: name=...)
    return app_to_mount

# Remplace systématiquement la méthode mount pour éviter les conflits
setattr(app, "mount", _mount)

# Endpoint de santé HTTP standard pour le healthcheck Docker
# Doit être défini APRÈS que app.get ait été créé par la boucle setattr ci-dessus.
@app.get("/_health", status_code=200, include_in_schema=False)
async def http_health_check():
    return {"status": "ok"}


# Création d'un dictionnaire d'état pour stocker les composants partagés
app_state = {}

# Import et initialisation du ToolLLMManager (gestionnaire LLM centralisé)
from collegue.core.tool_llm_manager import ToolLLMManager
app_state["llm_manager"] = ToolLLMManager()

# Importation des modules
from collegue.core import register_core
from collegue.tools import register_tools
from collegue.resources import register_resources
from collegue.prompts import register_prompts

# Enregistrement des composants
register_core(app, app_state)
register_tools(app, app_state)
register_resources(app, app_state)
register_prompts(app, app_state)

# ---------------------------------------------------------------------------
# Exposer les ressources du ResourceManager comme ressources MCP natives
# ---------------------------------------------------------------------------
if "resource_manager" in app_state:
    rm = app_state["resource_manager"]
    registered = app_state.setdefault("_registered_resources", set())

    for _rid in rm.list_resources():
        if _rid in registered:
            continue

        @app.resource(
            f"resource:/{_rid}",  # Format d'URI plus simple
            name=_rid,
            description=f"Ressource pour {_rid}",
            mime_type="application/json"
        )
        async def _make():
            return rm.get_resource(_rid)

        registered.add(_rid)

# ---------------------------------------------------------------------------
# Exposer les templates du PromptEngine comme prompts MCP
# ---------------------------------------------------------------------------
if "prompt_engine" in app_state:
    pe = app_state["prompt_engine"]
    registered_prompts = app_state.setdefault("_registered_prompts", set())

    for _tid, _tmpl in pe.library.templates.items():
        if _tid in registered_prompts:
            continue

        # Nom lisible : utiliser le champ "name" du template s'il existe, sinon l'ID
        prompt_name = getattr(_tmpl, "name", None) or _tid

        def _create_prompt_func(template_id):
            async def _inner():
                template = pe.get_template(template_id)
                return template.template if template else ""

            return _inner

        app.prompt(name=prompt_name)(_create_prompt_func(_tid))
        registered_prompts.add(_tid)

# Enregistrer un resource template dynamique pour récupérer un template par ID
if "prompt_engine" in app_state and "_template_endpoint" not in app_state:
    pe = app_state["prompt_engine"]

    @app.resource(
        "prompt-template://{template_id}",
        name="prompt_template",
        description="Récupère un template de prompt par ID",
        mime_type="text/plain"
    )
    async def get_prompt_template(template_id: str):
        template = pe.get_template(template_id)
        return template.template if template else "Template not found"

    app_state["_template_endpoint"] = True


@app.resource(
    "system://health",  # Utiliser un URI avec un schéma
    name="health_check", # Le nom peut rester, il est utilisé pour l'identification interne
    description="Simple health check endpoint",
    mime_type="text/plain"
)
async def health_endpoint(): # Renommer la fonction pour éviter tout conflit potentiel
    return "OK"

# Point d'entrée pour le serveur
if __name__ == "__main__":
    app.run()
