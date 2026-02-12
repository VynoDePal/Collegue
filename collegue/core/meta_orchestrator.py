"""
Meta Orchestrator - Tool intelligent utilisant FastMCP sampling with tools
"""
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

try:
    from fastmcp import FastMCP, Context
except ImportError:
    FastMCP = Any
    Context = Any


class OrchestratorRequest(BaseModel):
    query: str = Field(..., description="Requête utilisateur à traiter")
    tools: Optional[List[str]] = Field(None, description="Liste des tools à utiliser (vide = auto-détection)")
    context: Optional[Dict[str, Any]] = Field(None, description="Contexte additionnel")


class OrchestratorResponse(BaseModel):
    result: str = Field(..., description="Résultat généré par le LLM")
    tools_used: List[str] = Field(default_factory=list, description="Tools utilisés")
    execution_time: float = Field(..., description="Temps d'exécution en secondes")
    confidence: float = Field(default=0.8, description="Score de confiance 0-1")


def register_meta_orchestrator(app: FastMCP):

    @app.tool(
        name="smart_orchestrator",
        description="Orchestrateur intelligent qui choisit et exécute automatiquement les tools appropriés",
        tags={"meta"},
        task=True,
    )
    async def smart_orchestrator(
        request: OrchestratorRequest,
        ctx: Context
    ) -> OrchestratorResponse:
        """
        Orchestrateur intelligent utilisant FastMCP sampling with tools.        
        Le LLM choisit automatiquement les tools appropriés et les exécute
        en fonction de la requête utilisateur.
        """
        import time
        start_time = time.time()
        
        await ctx.info(f"Analyse de la requête: {request.query[:100]}...")
        
        try:
            result = await ctx.sample(
                messages=[
                    f"Tu es un assistant de développement intelligent.",
                    f"Requête utilisateur: {request.query}",
                    f"Analyse cette requête et utilise les tools appropriés pour y répondre.",
                    f"Contexte additionnel: {request.context}" if request.context else "",
                    f"Tools disponibles: {', '.join(request.tools) if request.tools else 'Auto-détection'}"
                ],
                system_prompt="""Tu es un orchestrateur de développement intelligent.
Analyse la requête de l'utilisateur et utilise automatiquement les tools MCP disponibles pour fournir la meilleure réponse possible.

Instructions:
1. Identifie les besoins de la requête
2. Choisis les tools appropriés (documentation, refactoring, tests, etc.)
3. Exécute les tools dans le bon ordre
4. Synthétise les résultats en une réponse claire

Sois concis mais complet. Focus sur la résolution du problème de l'utilisateur.""",
                temperature=0.5,
                max_tokens=2000
            )
            
            execution_time = time.time() - start_time
            
            tools_used = []
            if hasattr(result, 'history') and result.history:
                for msg in result.history:
                    if hasattr(msg, 'content') and isinstance(msg.content, list):
                        for content in msg.content:
                            if hasattr(content, 'type') and content.type == 'tool_result':
                                tools_used.append(content.toolUseId.split('_')[0] if '_' in str(content.toolUseId) else str(content.toolUseId))
            
            await ctx.info("Orchestration terminée avec succès")
            
            return OrchestratorResponse(
                result=result.text,
                tools_used=tools_used,
                execution_time=execution_time,
                confidence=0.8
            )
            
        except Exception as e:
            await ctx.info(f"Erreur lors de l'orchestration: {str(e)[:50]}")
            execution_time = time.time() - start_time
            
            return OrchestratorResponse(
                result=f"Erreur lors du traitement: {str(e)}",
                tools_used=[],
                execution_time=execution_time,
                confidence=0.1
            )

def remove_orchestrator_from_core():
    pass
