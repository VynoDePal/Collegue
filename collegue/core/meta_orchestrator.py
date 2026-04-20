"""
Meta Orchestrator - Tool intelligent utilisant FastMCP sampling with tools

Tool discovery was previously driven by a module-level ``_TOOLS_CACHE`` global
populated lazily on the first request. That global was replaced (issue #211)
by a lifespan-injected :class:`~collegue.core.tools_registry.ToolsRegistry`
that is built once at server startup. See ``collegue/core/tools_registry.py``
for the discovery logic and the concurrency-safe wrapper used as a fallback
when the handler is invoked outside of a proper lifespan context (tests,
ad-hoc scripts).
"""

from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from .tools_registry import ToolsRegistry

try:
    from fastmcp import FastMCP, Context
except ImportError:
    FastMCP = Any
    Context = Any

# Fallback registry used only when ``ctx.lifespan_context`` does not already
# carry one (e.g. in unit tests that invoke the handler directly). The
# ``ToolsRegistry`` itself is asyncio-safe, so concurrent cold-start requests
# will trigger at most one discovery.
_FALLBACK_REGISTRY: ToolsRegistry = ToolsRegistry()


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

        start_time = time.time()
        await ctx.info(
            f"Démarrage orchestration (mode v4 robust): {request.query[:100]}..."
        )

        # 1. Tool registry — populated once by core_lifespan at startup and
        # injected via ``ctx.lifespan_context``. When the handler is called
        # outside of a full lifespan (unit tests, scripts), fall back to a
        # module-level ``ToolsRegistry`` whose lock prevents concurrent
        # cold-start races.
        lc = ctx.lifespan_context or {}
        injected = lc.get("tools_registry")
        if isinstance(injected, ToolsRegistry):
            available_tools = await injected.get()
        elif isinstance(injected, dict):
            # Accept a raw dict too: convenient for unit tests that just want
            # to stub a fixed set of tools without wrapping in ToolsRegistry.
            available_tools = injected
        else:
            available_tools = await _FALLBACK_REGISTRY.get()
        tools_desc = "\\n".join(
            [info["prompt_desc"] for name, info in available_tools.items()]
        )

        # 2. Étape PLANIFICATION (avec Structured Output)
        await ctx.info("Phase 1: Planification...")

        # Récupération du prompt engine depuis le même contexte que le registry
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

            tool_instance = None
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
            finally:
                # Nettoyer explicitement l'instance après usage
                if tool_instance is not None:
                    try:
                        if hasattr(tool_instance, 'cleanup'):
                            tool_instance.cleanup()
                        del tool_instance
                    except Exception:
                        # Ne jamais faire échouer le nettoyage
                        pass

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
