"""
Meta Orchestrator - Tool intelligent utilisant FastMCP sampling with tools
"""

from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from .memory_manager import TTLCache

try:
    from fastmcp import FastMCP, Context
except ImportError:
    FastMCP = Any
    Context = Any

# Cache global pour les outils découverts (avec TTL de 1 heure)
_TOOLS_CACHE = None
_MAX_TOOLS_CACHE_SIZE = 50
_TOOLS_CACHE_TTL = 3600  # 1 heure


class OrchestratorStep(BaseModel):
    tool: str = Field(..., description="Nom de l'outil à exécuter")
    reason: str = Field(..., description="Raison de l'utilisation de cet outil")
    params: Dict[str, Any] = Field(..., description="Paramètres d'appel de l'outil")


class OrchestratorPlan(BaseModel):
    steps: List[OrchestratorStep] = Field(
        ..., description="Liste séquentielle des étapes à exécuter"
    )


class OrchestratorRequest(BaseModel):
    query: str = Field(..., description="Requête utilisateur à traiter")
    tools: Optional[List[str]] = Field(
        None, description="Liste des tools à utiliser (vide = auto-détection)"
    )
    context: Optional[Dict[str, Any]] = Field(None, description="Contexte additionnel")


class OrchestratorResponse(BaseModel):
    result: str = Field(..., description="Résultat généré par le LLM")
    tools_used: List[str] = Field(default_factory=list, description="Tools utilisés")
    execution_time: float = Field(..., description="Temps d'exécution en secondes")
    confidence: float = Field(default=0.8, description="Score de confiance 0-1")


def register_meta_orchestrator(app: FastMCP):

    @app.tool(
        name="smart_orchestrator",
        description=(
            "Orchestrateur intelligent qui analyse une requête complexe, choisit les tools appropriés, planifie "
            "leur exécution et synthétise une réponse finale.\n"
            "\n"
            "PARAMÈTRE REQUIS:\n"
            "- query: La demande ou question complexe formulée en langage naturel.\n"
            "\n"
            "PARAMÈTRES OPTIONNELS:\n"
            "- tools: Liste des noms d'outils à restreindre. Si vide, l'orchestrateur auto-découvre et choisit les outils pertinents.\n"
            "- context: Contexte additionnel pour guider l'orchestrateur (ex: {'files_changed': ['app/main.py']}).\n"
            "\n"
            "UTILISATION:\n"
            "Déléguez à cet outil toute tâche nécessitant l'enchaînement de plusieurs actions complexes (ex: 'trouve le bug et corrige-le'). "
            "Attention: Ce tool consomme beaucoup de tokens ("
            "Plan -> Execute -> Synthesize"
            "). Ne l'utilisez pas pour des requêtes simples (vérifier l'état d'un pod, lire un fichier), "
            "faites-le vous-même directement."
        ),
        tags={"meta"},
        task=True,
    )
    async def smart_orchestrator(
        request: OrchestratorRequest, ctx: Context
    ) -> OrchestratorResponse:
        """
        Orchestrateur intelligent utilisant une approche Plan -> Exécute -> Synthétise.
        Plus robuste pour les tâches complexes et évite les erreurs de protocole natif.
        """
        import time
        import json
        import importlib
        import inspect
        import pkgutil
        import collegue.tools
        from collegue.tools.base import BaseTool

        global _TOOLS_CACHE, _MAX_TOOLS_CACHE_SIZE, _TOOLS_CACHE_TTL
        start_time = time.time()
        await ctx.info(
            f"Démarrage orchestration (mode v4 robust): {request.query[:100]}..."
        )

        # 1. Découverte des tools (avec Cache TTL)
        if _TOOLS_CACHE is None:
            # Construire le cache dans une variable locale pour éviter les race conditions
            _tools_cache_local = TTLCache(
                max_size=_MAX_TOOLS_CACHE_SIZE,
                ttl_seconds=_TOOLS_CACHE_TTL,
                name="tools_discovery"
            )
            try:
                for _, name, _ in pkgutil.iter_modules(collegue.tools.__path__):
                    if name.startswith("_") or name == "base":
                        continue
                    try:
                        module = importlib.import_module(f"collegue.tools.{name}")
                        for _, obj in inspect.getmembers(module):
                            if (
                                inspect.isclass(obj)
                                and issubclass(obj, BaseTool)
                                and obj != BaseTool
                                and obj.__module__ == module.__name__
                            ):
                                try:
                                    # Instantiation temporaire pour métadonnées
                                    temp_instance = obj({})
                                    tool_name = temp_instance.get_name()
                                    if tool_name == "smart_orchestrator":
                                        continue

                                    schema = temp_instance.get_request_model().model_json_schema()
                                    props = schema.get("properties", {})
                                    required = schema.get("required", [])

                                    args_desc = []
                                    for prop_name, prop_info in props.items():
                                        req_mark = (
                                            "(REQUIS)"
                                            if prop_name in required
                                            else "(optionnel)"
                                        )
                                        prop_type = prop_info.get("type", "any")
                                        prop_desc = prop_info.get("description", "")
                                        args_desc.append(
                                            f"    - {prop_name} ({prop_type}): {prop_desc} {req_mark}"
                                        )

                                    formatted_args = "\\n".join(args_desc)

                                    _tools_cache_local.set(tool_name, {
                                        "class": obj,
                                        "description": temp_instance.get_description(),
                                        "prompt_desc": f"{tool_name}: {temp_instance.get_description()}\\n  Arguments:\\n{formatted_args}",
                                        "schema": schema,
                                    })
                                    
                                    # Nettoyer explicitement l'instance temporaire
                                    if hasattr(temp_instance, 'cleanup'):
                                        temp_instance.cleanup()
                                    del temp_instance
                                    
                                except Exception as e:
                                    await ctx.warning(f"Skip {name}: {e}")
                    except Exception:
                        pass
            except Exception as e:
                await ctx.error(f"Erreur discovery: {e}")
            
            # Assigner le cache complet une fois rempli (évite les race conditions)
            _TOOLS_CACHE = _tools_cache_local

        available_tools = _TOOLS_CACHE
        tools_desc = "\\n".join(
            [info["prompt_desc"] for name, info in available_tools.items()]
        )

        # 2. Étape PLANIFICATION (avec Structured Output)
        await ctx.info("Phase 1: Planification...")

        # Récupération du prompt engine depuis le contexte
        lc = ctx.lifespan_context or {}
        prompt_engine = lc.get("prompt_engine")

        # Construction du prompt
        context_str = (
            json.dumps(request.context, default=str) if request.context else "Aucun"
        )

        system_prompt = "Tu es un architecte logiciel expert chargé de planifier l'exécution d'une tâche complexe."

        user_prompt = f"""Requête utilisateur: "{request.query}"

Contexte:
{context_str}

Outils disponibles:
{tools_desc}

RÈGLES :
1. Utilise le CONTENU du contexte pour les paramètres 'content' si nécessaire.
2. Ne propose que des étapes réalisables avec les outils listés.
3. Sois efficace et direct.
"""

        if prompt_engine:
            # TODO: Utiliser prompt_engine pour récupérer un template optimisé si disponible
            pass

        steps = []
        try:
            # Utilisation de structured output natif
            plan_result = await ctx.sample(
                messages=[user_prompt],
                system_prompt=system_prompt,
                result_type=OrchestratorPlan,
                temperature=0.2,
                max_tokens=2000,
            )

            if isinstance(plan_result.result, OrchestratorPlan):
                steps = plan_result.result.steps
                await ctx.info(f"Plan généré: {len(steps)} étapes")
            else:
                # Fallback texte si le modèle ne supporte pas structured output
                await ctx.warning(
                    "Structured output non supporté, fallback parsing manuel"
                )
                # ... (code parsing simplifié si besoin, mais on suppose un modèle capable ici)
                raise ValueError("Le modèle n'a pas retourné un plan structuré")

        except Exception as e:
            await ctx.error(f"Echec planification: {e}")
            return OrchestratorResponse(
                result=f"Erreur de planification: {e}",
                tools_used=[],
                execution_time=time.time() - start_time,
                confidence=0.0,
            )

        # 3. Étape EXÉCUTION
        execution_results = []
        tools_used_list = []

        tool_kwargs = {
            "parser": lc.get("parser"),
            "context_manager": lc.get("context_manager"),
            "prompt_engine": prompt_engine,  # Injection critique !
            "ctx": ctx,
        }

        for i, step in enumerate(steps):
            tool_name = step.tool
            params = step.params

            if tool_name not in available_tools:
                msg = f"Étape {i + 1}: Tool '{tool_name}' inconnu."
                execution_results.append(msg)
                continue

            await ctx.info(f"Étape {i + 1}: {tool_name} ({step.reason})")

            try:
                tool_class = available_tools[tool_name]["class"]
                tool_instance = tool_class({})  # Nouvelle instance propre
                req_model = tool_instance.get_request_model()

                # Validation Pydantic
                req_obj = req_model(**params)

                # Exécution avec injection correcte des dépendances
                result = await tool_instance.execute_async(req_obj, **tool_kwargs)

                res_dict = result.dict() if hasattr(result, "dict") else str(result)
                execution_results.append(
                    {"step": i + 1, "tool": tool_name, "result": res_dict}
                )
                tools_used_list.append(tool_name)

            except Exception as e:
                err_msg = f"Erreur exécution {tool_name}: {e}"
                await ctx.error(err_msg)
                execution_results.append({"step": i + 1, "error": err_msg})

        # 4. Étape SYNTHÈSE
        await ctx.info("Phase 3: Synthèse...")

        synth_prompt = f"""Requête: "{request.query}"

Résultats d'exécution:
{json.dumps(execution_results, indent=2, default=str)}

Synthétise une réponse finale pour l'utilisateur."""

        try:
            final = await ctx.sample(messages=[synth_prompt], temperature=0.5)
            return OrchestratorResponse(
                result=final.text,
                tools_used=list(set(tools_used_list)),
                execution_time=time.time() - start_time,
                confidence=1.0,
            )
        except Exception as e:
            return OrchestratorResponse(
                result=f"Erreur synthèse: {e}",
                tools_used=tools_used_list,
                execution_time=time.time() - start_time,
                confidence=0.5,
            )
