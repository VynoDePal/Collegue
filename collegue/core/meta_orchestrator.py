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
        Orchestrateur intelligent utilisant une approche Plan -> Exécute -> Synthétise.
        Plus robuste pour les tâches complexes et évite les erreurs de protocole natif.
        """
        import time
        import json
        import importlib
        import inspect
        import re
        import pkgutil
        import collegue.tools
        
        # Lazy import pour éviter les cycles
        from collegue.tools.base import BaseTool

        start_time = time.time()
        await ctx.info(f"Démarrage orchestration (mode v3 robust): {request.query[:100]}...")

        # 1. Découverte des tools disponibles
        available_tools = {}
        try:
            for _, name, _ in pkgutil.iter_modules(collegue.tools.__path__):
                if name.startswith('_') or name == 'base':
                    continue
                try:
                    module = importlib.import_module(f'collegue.tools.{name}')
                    for obj_name, obj in inspect.getmembers(module):
                        if (inspect.isclass(obj) and issubclass(obj, BaseTool) and 
                            obj != BaseTool and obj.__module__ == module.__name__):
                            try:
                                instance = obj({})
                                if instance.get_name() == "smart_orchestrator": continue
                                
                                # Extraction du schéma pour le prompt
                                schema = instance.get_request_model().model_json_schema()
                                props = schema.get("properties", {})
                                required = schema.get("required", [])
                                
                                # Construction d'une description riche des arguments
                                args_desc = []
                                for prop_name, prop_info in props.items():
                                    req_mark = "(REQUIS)" if prop_name in required else "(optionnel)"
                                    prop_type = prop_info.get("type", "any")
                                    prop_desc = prop_info.get("description", "")
                                    args_desc.append(f"    - {prop_name} ({prop_type}): {prop_desc} {req_mark}")
                                
                                formatted_args = "\n".join(args_desc)
                                
                                available_tools[instance.get_name()] = {
                                    "class": obj,
                                    "description": instance.get_description(),
                                    "schema": schema,
                                    "prompt_desc": f"{instance.get_name()}: {instance.get_description()}\n  Arguments:\n{formatted_args}"
                                }
                            except Exception as e:
                                await ctx.warning(f"Impossible d'inspecter {obj_name}: {e}")
                except Exception as e:
                    pass
        except Exception as e:
            await ctx.error(f"Erreur découverte tools: {e}")

        tools_desc = "\n".join([info['prompt_desc'] for name, info in available_tools.items()])
        
        # 2. Étape PLANIFICATION
        await ctx.info("Phase 1: Planification...")
        
        context_str = json.dumps(request.context, default=str) if request.context else "Aucun"
        
        plan_prompt = (
            f"""Tu es un architecte logiciel expert.
Requête utilisateur: "{request.query}"

Contexte (fichiers/code):
""" + context_str + f"""

Outils disponibles et leurs signatures (RESPECTE SCRUPULEUSEMENT LES ARGUMENTS):
{tools_desc}

RÈGLES IMPORTANTES :
1. Tu es dans un environnement Docker ISOLÉ.
2. N'utilise PAS de chemins de fichiers absolus (sauf /tmp/...).
3. Utilise le CONTENU fourni dans le contexte pour remplir les paramètres 'content' (et non 'code').

EXEMPLE DE PLAN :
{{
  "steps": [
    {{
      "tool": "secret_scan",
      "reason": "Analyse de sécurité requise",
      "params": {{ "content": "..." }} 
    }}
  ]
}}

TÂCHE :
Génère un plan d'exécution JSON valide pour répondre à la demande.
Retourne UNIQUEMENT le JSON brut.
"""
        )
        
        steps = []
        try:
            # On demande un format texte simple qu'on parsera nous-mêmes
            plan_result = await ctx.sample(
                messages=[plan_prompt],
                temperature=0.1, # Très déterministe
                max_tokens=2000
            )
            
            raw_text = plan_result.text.strip()
            
            # Nettoyage et extraction JSON bourrin
            # 1. Enlever les blocs de code
            clean_text = re.sub(r"```.*?```", "", raw_text, flags=re.DOTALL) # Si le json est dedans, ça l'enlève... attention
            
            # Mieux: Chercher le premier { et le dernier }
            match = re.search(r"(\{.*\})", raw_text, re.DOTALL)
            if match:
                json_candidate = match.group(1)
            else:
                # Fallback: peut-être que le JSON est dans un bloc markdown qu'on a raté
                match_md = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw_text, re.DOTALL)
                if match_md:
                    json_candidate = match_md.group(1)
                else:
                    json_candidate = raw_text

            # Nettoyage fin
            json_candidate = re.sub(r",\s*([\]}])", r"\1", json_candidate) # Trailing commas
            
            try:
                plan = json.loads(json_candidate)
                steps = plan.get("steps", [])
                await ctx.info(f"Plan généré valide: {len(steps)} étapes")
            except json.JSONDecodeError as e:
                await ctx.error(f"Echec parsing JSON: {e}")
                await ctx.info(f"Texte reçu: {raw_text[:200]}...")
                # Dernier recours: si le JSON est tronqué, on essaie de fermer les accolades
                if "steps" in json_candidate:
                     # On essaie de récupérer au moins la première étape manuellement avec regex
                     step_matches = re.finditer(r'\{\s*"tool":\s*"([^"]+)",\s*"reason":\s*"([^"]+)",\s*"params":\s*(\{.*?\})\s*\}', json_candidate, re.DOTALL)
                     for m in step_matches:
                         try:
                             steps.append({
                                 "tool": m.group(1),
                                 "reason": m.group(2),
                                 "params": json.loads(m.group(3))
                             })
                         except: pass
                     if steps:
                         await ctx.info(f"Récupération regex réussie: {len(steps)} étapes")

        except Exception as e:
            await ctx.error(f"Erreur fatale planification: {e}")
            return OrchestratorResponse(
                result=f"Erreur de planification: {e}",
                tools_used=[],
                execution_time=time.time() - start_time,
                confidence=0.1
            )

        if not steps and "steps" not in raw_text:
             return OrchestratorResponse(
                result=f"Impossible de générer un plan. Réponse du modèle: {raw_text}",
                tools_used=[],
                execution_time=time.time() - start_time,
                confidence=0.1
            )

        # 3. Étape EXÉCUTION
        execution_results = []
        tools_used_list = []
        
        lc = ctx.lifespan_context or {}
        tool_kwargs = {
            "parser": lc.get('parser'),
            "context_manager": lc.get('context_manager'),
            "ctx": ctx,
        }

        # Mapping des alias de paramètres pour la robustesse
        PARAM_ALIASES = {
            "content": ["code", "text", "source", "body"],
            "file_path": ["filepath", "file", "path", "filename"],
        }

        for i, step in enumerate(steps):
            tool_name = step.get("tool")
            params = step.get("params", {})
            reason = step.get("reason", "")
            
            if tool_name not in available_tools:
                msg = f"Étape {i+1}: Tool '{tool_name}' inconnu, ignoré."
                await ctx.warning(msg)
                execution_results.append(msg)
                continue

            await ctx.info(f"Étape {i+1}/{len(steps)}: {tool_name}")
            
            try:
                tool_class = available_tools[tool_name]["class"]
                tool_instance = tool_class({})
                req_model = tool_instance.get_request_model()
                
                # Correction automatique des paramètres via alias
                schema = available_tools[tool_name]["schema"]
                required_props = schema.get("required", [])
                
                for req_prop in required_props:
                    if req_prop not in params and req_prop in PARAM_ALIASES:
                        # On cherche si un alias est présent
                        for alias in PARAM_ALIASES[req_prop]:
                            if alias in params:
                                await ctx.warning(f"Auto-fix param: '{alias}' -> '{req_prop}' pour {tool_name}")
                                params[req_prop] = params.pop(alias)
                                break

                # Validation et exécution
                req_obj = req_model(**params)
                result = await tool_instance.execute_async(req_obj, **tool_kwargs)
                
                # Sérialisation
                res_dict = result.dict() if hasattr(result, 'dict') else str(result)
                
                execution_results.append({
                    "step": i+1,
                    "tool": tool_name,
                    "result": res_dict
                })
                tools_used_list.append(tool_name)
                
            except Exception as e:
                err_msg = f"Erreur exécution {tool_name}: {e}"
                await ctx.error(err_msg)
                execution_results.append({"step": i+1, "error": err_msg})

        # 4. Étape SYNTHÈSE
        await ctx.info("Phase 3: Synthèse...")
        
        synth_prompt = (
            f"""Tu es un expert technique.
Requête originale: "{request.query}"

Résultats de l'exécution:
{json.dumps(execution_results, indent=2, default=str)}

Synthétise une réponse complète pour l'utilisateur.
"""
        )
        try:
            final_result = await ctx.sample(
                messages=[synth_prompt],
                temperature=0.5,
                max_tokens=2000
            )
            
            return OrchestratorResponse(
                result=final_result.text,
                tools_used=list(set(tools_used_list)),
                execution_time=time.time() - start_time,
                confidence=0.9 if execution_results else 0.5
            )
            
        except Exception as e:
             return OrchestratorResponse(
                result=f"Erreur synthèse: {e}. Résultats bruts: {execution_results}",
                tools_used=tools_used_list,
                execution_time=time.time() - start_time,
                confidence=0.3
            )

    def remove_orchestrator_from_core():
        pass
