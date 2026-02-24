"""
Refactoring - Outil de refactoring de code intelligent.

Cet outil refactorise et améliore le code selon différents types de transformations:
- rename: Renommer variables, fonctions, classes
- extract: Extraire du code en fonctions réutilisables
- simplify: Simplifier la logique complexe
- optimize: Optimiser les performances
- clean: Nettoyer le code mort et imports inutilisés
- modernize: Moderniser vers les patterns contemporains

Refactorisé: Le fichier original faisait 687 lignes, maintenant ~200 lignes.
"""
from typing import List, Dict, Any, Optional
from ..base import BaseTool, ToolError
from ...core.shared import run_async_from_sync
from .models import RefactoringRequest, RefactoringResponse, LLMRefactoringResult
from .engine import RefactoringEngine
from .config import REFACTORING_TYPES, REFACTORING_LANGUAGE_INSTRUCTIONS


class RefactoringTool(BaseTool):
    """
    Outil de refactoring de code intelligent.
    
    Supporte 6 types de refactoring dans 5+ langages avec analyse de métriques
    et calcul des améliorations.
    """
    
    tool_name = "code_refactoring"
    tool_description = "Refactorise et améliore le code selon différents types de transformations"
    tags = {"generation", "quality"}
    request_model = RefactoringRequest
    response_model = RefactoringResponse
    supported_languages = ["python", "javascript", "typescript", "java", "c#", "terraform", "hcl", "php"]
    
    def __init__(self, config=None, app_state=None):
        super().__init__(config, app_state)
        self._engine = RefactoringEngine(logger=self.logger)
    
    def get_supported_refactoring_types(self) -> List[str]:
        """Retourne la liste des types de refactoring supportés."""
        return ["rename", "extract", "simplify", "optimize", "clean", "modernize"]
    
    def get_usage_description(self) -> str:
        return (
            "Outil de refactorisation qui améliore la qualité du code en appliquant différents types de "
            "transformations : renommage, extraction, simplification, optimisation, nettoyage et modernisation. "
            "Il fournit des métriques d'amélioration et des explications détaillées des changements."
        )
    
    def get_examples(self) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Renommage de variables Python",
                "description": "Renommer des variables avec des noms plus descriptifs",
                "request": {
                    "code": "def calc(a, b, c):\n    x = a + b\n    y = x * c\n    return y",
                    "language": "python",
                    "refactoring_type": "rename",
                    "parameters": {"naming_convention": "descriptive"}
                }
            },
            {
                "title": "Extraction de méthodes JavaScript",
                "description": "Extraire du code dupliqué en fonctions réutilisables",
                "request": {
                    "code": "function processUser(user) {\n    if (user.email && user.email.includes('@')) {\n        console.log('Valid email');\n    }\n}",
                    "language": "javascript",
                    "refactoring_type": "extract",
                    "parameters": {"extract_duplicates": True}
                }
            },
            {
                "title": "Nettoyage de code",
                "description": "Nettoyer du code avec imports inutiles et formatage",
                "request": {
                    "code": "import os\nimport sys\nimport json\n\n\n\ndef hello():\n    print('Hello')   \n\n\n",
                    "language": "python",
                    "refactoring_type": "clean",
                    "parameters": {"remove_unused_imports": True, "fix_formatting": True}
                }
            }
        ]
    
    def get_capabilities(self) -> List[str]:
        return [
            "Refactorisation dans 5+ langages de programmation",
            "6 types de refactoring : rename, extract, simplify, optimize, clean, modernize",
            "Analyse des métriques de code (complexité, lignes, fonctions, classes)",
            "Calcul des améliorations et pourcentages de réduction",
            "Identification détaillée des changements effectués",
            "Instructions spécifiques par langage et type de refactoring",
            "Préservation de la fonctionnalité originale du code",
            "Génération d'explications détaillées des modifications",
            "Support des paramètres personnalisés pour chaque type",
            "Fallback local si LLM indisponible"
        ]
    
    def validate_request(self, request) -> bool:
        """Valide la requête de refactoring."""
        super().validate_request(request)
        
        if hasattr(request, 'refactoring_type'):
            supported_types = self.get_supported_refactoring_types()
            if request.refactoring_type not in supported_types:
                raise ToolError(
                    f"Type de refactoring '{request.refactoring_type}' non supporté. "
                    f"Types supportés: {supported_types}"
                )
        
        return True
    
    def is_long_running(self) -> bool:
        """Indique si le tool est long à exécuter."""
        return True
    
    def _build_prompt(self, request: RefactoringRequest) -> str:
        """Construit le prompt pour le LLM."""
        language = request.language
        refactoring_type = request.refactoring_type
        code = request.code
        
        refactoring_desc = self._engine.get_refactoring_type_description(refactoring_type)
        
        prompt_parts = [
            f"Effectue un refactoring de type '{refactoring_type}'",
            f"Description: {refactoring_desc}",
            f"IMPORTANT: Préserve exactement le comportement du code original",
            f"Langage: {language}",
            f""
        ]
        
        prompt_parts.extend([
            f"```{language}",
            code,
            f"```",
            f""
        ])
        
        lang_instructions = REFACTORING_LANGUAGE_INSTRUCTIONS.get(language.lower(), {})
        if refactoring_type in lang_instructions:
            prompt_parts.append(f"Conventions {language}: {lang_instructions[refactoring_type]}")
        
        return "\n".join(prompt_parts)
    
    def _perform_local_refactoring(self, request: RefactoringRequest) -> RefactoringResponse:
        """Effectue un refactoring local basique (fallback si LLM indisponible)."""
        if request.refactoring_type == "clean":
            refactored_code = self._engine.clean_code_basic(request.code, request.language)
        elif request.refactoring_type == "simplify":
            refactored_code = self._engine.simplify_code_basic(request.code, request.language)
        else:
            refactored_code = self._engine.clean_code_basic(request.code, request.language)
        
        original_metrics = self._engine.analyze_code_metrics(request.code, request.language)
        new_metrics = self._engine.analyze_code_metrics(refactored_code, request.language)
        improvement_metrics = self._engine.calculate_improvements(original_metrics, new_metrics)
        
        changes = [{
            "type": request.refactoring_type,
            "description": f"Refactoring basique de type '{request.refactoring_type}' appliqué localement",
            "parameters": request.parameters or {}
        }]
        
        explanation = "Refactoring local basique appliqué. Recommandation: utiliser un LLM pour un refactoring plus avancé."
        
        return RefactoringResponse(
            refactored_code=refactored_code,
            original_code=request.code,
            language=request.language,
            changes=changes,
            explanation=explanation,
            improvement_metrics=improvement_metrics
        )
    
    def _execute_core_logic(self, request: RefactoringRequest, **kwargs) -> RefactoringResponse:
        """Exécute le refactoring (synchrone)."""
        ctx = kwargs.get('ctx')
        
        original_metrics = self._engine.analyze_code_metrics(request.code, request.language)
        
        if ctx:
            try:
                prompt = run_async_from_sync(self.prepare_prompt(request, f"refactoring_{request.refactoring_type}"))
                system_prompt = f"""Tu es un expert en refactoring de code {request.language}.
Applique les meilleures pratiques de refactoring de type '{request.refactoring_type}'.
Réponds UNIQUEMENT avec le code refactoré, sans explications."""
                
                result = run_async_from_sync(ctx.sample(
                    messages=prompt,
                    system_prompt=system_prompt,
                    temperature=0.5,
                    max_tokens=2000
                ))
                
                refactored_code = result.text
                new_metrics = self._engine.analyze_code_metrics(refactored_code, request.language)
                improvement_metrics = self._engine.calculate_improvements(original_metrics, new_metrics)
                changes = self._engine.identify_changes(
                    request.refactoring_type, request.code, refactored_code, request.parameters
                )
                explanation = self._engine.generate_explanation(
                    request.refactoring_type, changes, improvement_metrics
                )
                
                return RefactoringResponse(
                    refactored_code=refactored_code,
                    original_code=request.code,
                    language=request.language,
                    changes=changes,
                    explanation=explanation,
                    improvement_metrics=improvement_metrics
                )
            
            except Exception as e:
                self.logger.warning(f"Erreur avec ctx.sample(), utilisation du fallback: {e}")
                return self._perform_local_refactoring(request)
        else:
            return self._perform_local_refactoring(request)
    
    async def _execute_core_logic_async(self, request: RefactoringRequest, **kwargs) -> RefactoringResponse:
        """Version asynchrone avec support structured output."""
        ctx = kwargs.get('ctx')
        use_structured_output = kwargs.get('use_structured_output', True)
        
        if ctx:
            await ctx.info("Analyse du code original...")
        
        original_metrics = self._engine.analyze_code_metrics(request.code, request.language)
        
        prompt = await self.prepare_prompt(request, f"refactoring_{request.refactoring_type}")
        system_prompt = f"""Tu es un expert en refactoring de code {request.language}.
Applique les meilleures pratiques de refactoring de type '{request.refactoring_type}'."""
        
        if ctx:
            await ctx.info("Refactoring en cours via LLM...")
        
        try:
            # Essayer structured output d'abord
            if ctx is not None and use_structured_output:
                try:
                    self.logger.debug("Utilisation du structured output avec LLMRefactoringResult")
                    llm_result = await ctx.sample(
                        messages=prompt,
                        system_prompt=system_prompt,
                        result_type=LLMRefactoringResult,
                        temperature=0.5,
                        max_tokens=2000
                    )
                    
                    if isinstance(llm_result.result, LLMRefactoringResult):
                        result_data = llm_result.result
                        if ctx:
                            await ctx.info(f"Structured output: {result_data.changes_count} modifications")
                        
                        cleaned_code = self._engine.extract_code_block(
                            result_data.refactored_code, request.language
                        )
                        
                        is_valid, error_msg = self._engine.validate_code_syntax(
                            cleaned_code, request.language
                        )
                        if not is_valid:
                            self.logger.warning(f"Code structuré invalide: {error_msg}")
                        
                        changes = [
                            {"type": area, "description": f"Amélioration: {area}"}
                            for area in result_data.improved_areas
                        ]
                        
                        improvement_metrics = {
                            "complexity_reduction": result_data.complexity_reduction,
                            "changes_count": result_data.changes_count,
                            "improved_areas": result_data.improved_areas
                        }
                        
                        return RefactoringResponse(
                            refactored_code=cleaned_code,
                            original_code=request.code,
                            language=request.language,
                            changes=changes,
                            explanation=result_data.changes_summary,
                            improvement_metrics=improvement_metrics
                        )
                except Exception as e:
                    self.logger.warning(f"Structured output a échoué, fallback vers texte brut: {e}")
            
            # Fallback vers texte brut
            result = await ctx.sample(
                messages=prompt,
                system_prompt=system_prompt + "\nRéponds UNIQUEMENT avec le code refactoré.",
                temperature=0.5,
                max_tokens=2000
            )
            
            refactored_code = self._engine.extract_code_block(result.text, request.language)
            
            is_valid, error_msg = self._engine.validate_code_syntax(refactored_code, request.language)
            if not is_valid:
                self.logger.warning(f"Code généré syntaxiquement invalide: {error_msg}")
                if ctx:
                    await ctx.warning(f"Attention: Le code généré semble contenir des erreurs de syntaxe: {error_msg}")
            
            if ctx:
                await ctx.info("Analyse des améliorations...")
            
            new_metrics = self._engine.analyze_code_metrics(refactored_code, request.language)
            improvement_metrics = self._engine.calculate_improvements(original_metrics, new_metrics)
            changes = self._engine.identify_changes(
                request.refactoring_type, request.code, refactored_code, request.parameters
            )
            explanation = self._engine.generate_explanation(
                request.refactoring_type, changes, improvement_metrics
            )
            
            return RefactoringResponse(
                refactored_code=refactored_code,
                original_code=request.code,
                language=request.language,
                changes=changes,
                explanation=explanation,
                improvement_metrics=improvement_metrics
            )
        
        except Exception as e:
            self.logger.warning(f"Erreur LLM async, utilisation du fallback: {e}")
            return self._perform_local_refactoring(request)
