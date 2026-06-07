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

from typing import Any, Dict, List

from ...core.shared import run_async_from_sync
from ..agent_loop import AgentLoopConfig, AgentLoopMixin
from ..base import BaseTool, ToolError
from .config import REFACTORING_LANGUAGE_INSTRUCTIONS
from .engine import RefactoringEngine
from .models import RefactoringRequest, RefactoringResponse


class RefactoringTool(AgentLoopMixin, BaseTool):
    """
    Outil de refactoring de code intelligent.

    Supporte 6 types de refactoring dans 5+ langages avec analyse de métriques
    et calcul des améliorations.
    """

    tool_name = "code_refactoring"
    tool_description = (
        "Refactorise et améliore du code source en appliquant des transformations spécifiques (renommage, extraction, nettoyage, etc.).\n"
        "\n"
        "PARAMÈTRES REQUIS:\n"
        "- code: Le code source brut à modifier. ATTENTION: Passez bien le contenu brut du code ici, PAS un chemin de fichier.\n"
        "- language: Le langage du code (ex: 'python', 'javascript', 'typescript', 'php').\n"
        "- refactoring_type: Le type de transformation à appliquer. Options valides:\n"
        "  * 'rename': Renommer variables/fonctions de façon plus lisible\n"
        "  * 'extract': Extraire du code dupliqué ou complexe en fonctions/méthodes réutilisables\n"
        "  * 'simplify': Simplifier la logique complexe (ifs imbriqués, boucles)\n"
        "  * 'optimize': Optimiser les performances du code\n"
        "  * 'clean': Supprimer le code mort, imports inutilisés, commentaires expirés\n"
        "  * 'modernize': Actualiser vers des syntaxes et patterns plus récents\n"
        "\n"
        "PARAMÈTRES OPTIONNELS:\n"
        "- file_path: Le chemin du fichier (utile pour le contexte linguistique du LLM, bien que le code lui-même doive être dans 'code').\n"
        "- parameters: Dictionnaire pour configurer la transformation (ex: {'naming_convention': 'snake_case', 'remove_unused_imports': True}).\n"
        "\n"
        "UTILISATION:\n"
        "Cet outil retourne le code modifié dans le champ 'refactored_code' de la réponse. "
        "ATTENTION: Il NE modifie PAS les fichiers sur le disque. Vous devrez utiliser un outil d'écriture (ex: github_ops update_file ou replace_file_content) "
        "pour appliquer ces changements après avoir inspecté le résultat."
    )
    tags = {"generation", "quality"}
    request_model = RefactoringRequest
    response_model = RefactoringResponse
    supported_languages = [
        "python",
        "javascript",
        "typescript",
        "java",
        "c#",
        "terraform",
        "hcl",
        "php",
    ]

    agent_config = AgentLoopConfig(
        max_iterations=3,
        initial_temperature=0.7,
        temperature_decay=0.15,
        min_temperature=0.3,
    )

    def __init__(self, config=None, app_state=None):
        super().__init__(config, app_state)
        self._engine = RefactoringEngine(logger=self.logger)

    # --- AgentLoopMixin hooks ---

    async def validate_agent_output(self, output: str, context: Dict[str, Any]) -> List[str]:
        """Valide le code refactoré : syntaxe et taille minimale."""
        errors = []
        language = context.get("language", "python")
        original_code = context.get("original_code", "")

        cleaned = self._engine.extract_code_block(output, language)

        is_valid, error_msg = self._engine.validate_code_syntax(cleaned, language)
        if not is_valid:
            errors.append(f"Syntaxe invalide: {error_msg}")

        if original_code and len(cleaned.strip()) < len(original_code) * 0.3:
            errors.append(
                f"Code refactoré anormalement court ({len(cleaned.strip())} chars "
                f"vs {len(original_code)} original, < 30%)"
            )

        if not cleaned.strip():
            errors.append("Code refactoré vide")

        return errors

    async def assess_agent_quality(self, output: str, context: Dict[str, Any]) -> float:
        """Évalue la qualité du refactoring : syntaxe + réduction de complexité."""
        language = context.get("language", "python")
        original_metrics = context.get("original_metrics", {})

        cleaned = self._engine.extract_code_block(output, language)

        is_valid, _ = self._engine.validate_code_syntax(cleaned, language)
        syntax_score = 1.0 if is_valid else 0.0

        if not cleaned.strip():
            return 0.0

        new_metrics = self._engine.analyze_code_metrics(cleaned, language)

        complexity_score = 0.5
        original_complexity = original_metrics.get("complexity_score", 0)
        if original_complexity > 0:
            reduction = (original_complexity - new_metrics.get("complexity_score", 0)) / original_complexity
            complexity_score = max(0.0, min(1.0, 0.5 + reduction * 0.5))

        return syntax_score * 0.5 + complexity_score * 0.3 + 0.2

    async def build_agent_feedback(
        self, output: str, errors: List[str], quality: float, context: Dict[str, Any]
    ) -> str:
        """Construit un feedback spécifique pour corriger le code refactoré."""
        parts = []

        for error in errors:
            if "Syntaxe invalide" in error:
                parts.append(f"ERREUR DE SYNTAXE: {error}. Corrige l'erreur de syntaxe.")
            elif "anormalement court" in error:
                parts.append(
                    "Le code refactoré est trop court par rapport à l'original. "
                    "Assure-toi de refactoriser le code complet, pas seulement une partie."
                )
            elif "vide" in error:
                parts.append("Le code refactoré est vide. Génère le code refactoré complet.")

        if quality < 0.7:
            language = context.get("language", "python")
            original_metrics = context.get("original_metrics", {})
            cleaned = self._engine.extract_code_block(output, language)
            new_metrics = self._engine.analyze_code_metrics(cleaned, language)
            original_complexity = original_metrics.get("complexity_score", 0)
            new_complexity = new_metrics.get("complexity_score", 0)
            if new_complexity >= original_complexity and original_complexity > 0:
                parts.append(
                    f"La complexité n'a pas diminué (original: {original_complexity}, "
                    f"refactoré: {new_complexity}). Simplifie davantage la logique."
                )

        return "\n".join(parts) if parts else "Améliore la qualité globale du code refactoré."

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
                    "parameters": {"naming_convention": "descriptive"},
                },
            },
            {
                "title": "Extraction de méthodes JavaScript",
                "description": "Extraire du code dupliqué en fonctions réutilisables",
                "request": {
                    "code": "function processUser(user) {\n    if (user.email && user.email.includes('@')) {\n        console.log('Valid email');\n    }\n}",
                    "language": "javascript",
                    "refactoring_type": "extract",
                    "parameters": {"extract_duplicates": True},
                },
            },
            {
                "title": "Nettoyage de code",
                "description": "Nettoyer du code avec imports inutiles et formatage",
                "request": {
                    "code": "import os\nimport sys\nimport json\n\n\n\ndef hello():\n    print('Hello')   \n\n\n",
                    "language": "python",
                    "refactoring_type": "clean",
                    "parameters": {
                        "remove_unused_imports": True,
                        "fix_formatting": True,
                    },
                },
            },
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
            "Fallback local si LLM indisponible",
        ]

    def validate_request(self, request) -> bool:
        """Valide la requête de refactoring."""
        super().validate_request(request)

        if hasattr(request, "refactoring_type"):
            supported_types = self.get_supported_refactoring_types()
            if request.refactoring_type not in supported_types:
                raise ToolError(
                    f"Type de refactoring '{request.refactoring_type}' non supporté. Types supportés: {supported_types}"
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
            "IMPORTANT: Préserve exactement le comportement du code original",
            f"Langage: {language}",
            "",
        ]

        prompt_parts.extend([f"```{language}", code, "```", ""])

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

        changes = [
            {
                "type": request.refactoring_type,
                "description": f"Refactoring basique de type '{request.refactoring_type}' appliqué localement",
                "parameters": request.parameters or {},
            }
        ]

        explanation = (
            "Refactoring local basique appliqué. Recommandation: utiliser un LLM pour un refactoring plus avancé."
        )

        return RefactoringResponse(
            refactored_code=refactored_code,
            original_code=request.code,
            language=request.language,
            changes=changes,
            explanation=explanation,
            improvement_metrics=improvement_metrics,
        )

    def _execute_core_logic(self, request: RefactoringRequest, **kwargs) -> RefactoringResponse:
        """Exécute le refactoring (synchrone)."""
        ctx = kwargs.get("ctx")

        original_metrics = self._engine.analyze_code_metrics(request.code, request.language)

        if ctx:
            try:
                prompt = run_async_from_sync(self.prepare_prompt(request, f"refactoring_{request.refactoring_type}"))
                system_prompt = f"""Tu es un expert en refactoring de code {request.language}.
Applique les meilleures pratiques de refactoring de type '{request.refactoring_type}'.
Réponds UNIQUEMENT avec le code refactoré, sans explications."""

                from ...core.llm.client import sample_with_timeout

                result = run_async_from_sync(
                    sample_with_timeout(
                        ctx,
                        messages=prompt,
                        system_prompt=system_prompt,
                        temperature=0.5,
                        max_tokens=2000,
                    )
                )

                refactored_code = result.text
                new_metrics = self._engine.analyze_code_metrics(refactored_code, request.language)
                improvement_metrics = self._engine.calculate_improvements(original_metrics, new_metrics)
                changes = self._engine.identify_changes(
                    request.refactoring_type,
                    request.code,
                    refactored_code,
                    request.parameters,
                )
                explanation = self._engine.generate_explanation(request.refactoring_type, changes, improvement_metrics)

                return RefactoringResponse(
                    refactored_code=refactored_code,
                    original_code=request.code,
                    language=request.language,
                    changes=changes,
                    explanation=explanation,
                    improvement_metrics=improvement_metrics,
                )

            except Exception as e:
                self.logger.warning(f"Erreur avec ctx.sample(), utilisation du fallback: {e}")
                return self._perform_local_refactoring(request)
        else:
            return self._perform_local_refactoring(request)

    async def _execute_core_logic_async(self, request: RefactoringRequest, **kwargs) -> RefactoringResponse:
        """Version asynchrone avec boucle agentique itérative."""
        ctx = kwargs.get("ctx")

        if ctx:
            await ctx.info("Analyse du code original...")

        original_metrics = self._engine.analyze_code_metrics(request.code, request.language)

        prompt = await self.prepare_prompt(request, f"refactoring_{request.refactoring_type}")
        sys_prompt = (
            None
            if self._last_prompt_template_id
            else (
                f"Tu es un expert en refactoring de code {request.language}.\n"
                f"Applique les meilleures pratiques de refactoring de type '{request.refactoring_type}'.\n"
                "Réponds UNIQUEMENT avec le code refactoré, sans explications."
            )
        )

        if not ctx:
            return self._perform_local_refactoring(request)

        try:
            await ctx.info("Refactoring agentique en cours...")

            agent_result = await self.agent_execute(
                initial_prompt=prompt,
                system_prompt=sys_prompt,
                ctx=ctx,
                context={
                    "language": request.language,
                    "original_code": request.code,
                    "original_metrics": original_metrics,
                    "refactoring_type": request.refactoring_type,
                },
                max_tokens=2000,
            )

            refactored_code = self._engine.extract_code_block(agent_result.best_output, request.language)

            final_errors = await self.validate_agent_output(
                agent_result.best_output,
                {"language": request.language, "original_code": request.code},
            )
            if final_errors:
                self.logger.warning(
                    "Refactoring agentique invalide (%s) — retour au refactoring local.",
                    "; ".join(final_errors),
                )
                if ctx:
                    await ctx.warning(
                        "Code refactoré invalide (" + "; ".join(final_errors) + "). Retour au refactoring local sûr."
                    )
                return self._perform_local_refactoring(request)

            if ctx:
                await ctx.info("Analyse des améliorations...")

            new_metrics = self._engine.analyze_code_metrics(refactored_code, request.language)
            improvement_metrics = self._engine.calculate_improvements(original_metrics, new_metrics)
            changes = self._engine.identify_changes(
                request.refactoring_type,
                request.code,
                refactored_code,
                request.parameters,
            )
            explanation = self._engine.generate_explanation(request.refactoring_type, changes, improvement_metrics)

            response = RefactoringResponse(
                refactored_code=refactored_code,
                original_code=request.code,
                language=request.language,
                changes=changes,
                explanation=explanation,
                improvement_metrics=improvement_metrics,
                agent_iterations=agent_result.total_iterations,
                agent_best_score=agent_result.best_score,
                agent_errors_fixed=agent_result.errors_fixed,
                agent_converged=agent_result.converged,
            )

            self._store_to_memory(
                entry_type="fix_applied",
                category=request.refactoring_type,
                title=f"Refactoring {request.refactoring_type}: {len(changes)} changements",
                data={"changes_count": len(changes), "improvements": improvement_metrics},
                score=agent_result.best_score,
                language=request.language,
            )

            return response

        except Exception as e:
            self.logger.warning(f"Erreur LLM async, utilisation du fallback: {e}")
            return self._perform_local_refactoring(request)
