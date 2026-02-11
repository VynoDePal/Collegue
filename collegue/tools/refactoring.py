"""
Refactoring - Outil de refactoring de code intelligent
"""
import asyncio
import re
from typing import Optional, Dict, Any, List, Union, Type
from pydantic import BaseModel, Field
from .base import BaseTool, ToolError
from .shared import run_async_from_sync


# Refactoring prompt building - extracted from llm_helpers
REFACTORING_TYPES = {
    "rename": "Renommer des variables, fonctions ou classes pour améliorer la clarté",
    "extract": "Extraire du code en fonctions ou méthodes réutilisables",
    "simplify": "Simplifier la logique complexe et les conditions imbriquées",
    "optimize": "Optimiser les performances et l'efficacité",
    "clean": "Nettoyer le code mort, imports inutilisés et code superflu",
    "modernize": "Moderniser le code vers les patterns contemporains"
}

REFACTORING_LANGUAGE_INSTRUCTIONS = {
    "python": {
        "rename": "Utilise les conventions PEP 8 (snake_case, PascalCase)",
        "extract": "Crée des fonctions avec type hints et docstrings",
        "simplify": "Utilise comprehensions, walrus operator",
        "optimize": "Utilise set, deque, évite les boucles inutiles",
        "clean": "Supprime imports inutiles, utilise f-strings",
        "modernize": "Utilise dataclasses, type hints, pathlib"
    },
    "javascript": {
        "rename": "Utilise camelCase pour variables/fonctions",
        "extract": "Crée des fonctions avec JSDoc, arrow functions",
        "simplify": "Utilise destructuring, template literals",
        "optimize": "Utilise Map/Set, évite les mutations",
        "clean": "Supprime var, utilise const/let",
        "modernize": "Utilise ES6+, async/await, modules ES6"
    },
    "typescript": {
        "rename": "Utilise camelCase avec types explicites",
        "extract": "Crée des fonctions typées avec génériques",
        "simplify": "Utilise union types, optional chaining",
        "optimize": "Types stricts, évite 'any'",
        "clean": "Supprime types redondants",
        "modernize": "Utilise strict mode, utility types"
    }
}


class RefactoringRequest(BaseModel):
    code: str = Field(..., description="Code à refactorer")
    language: str = Field(..., description="Langage de programmation du code")
    session_id: Optional[str] = Field(None, description="Identifiant de session")
    refactoring_type: str = Field(..., description="Type de refactoring à appliquer (rename, extract, simplify, optimize, clean)")
    parameters: Optional[Dict[str, Any]] = Field(None, description="Paramètres spécifiques au type de refactoring")
    file_path: Optional[str] = Field(None, description="Chemin du fichier contenant le code")

class RefactoringResponse(BaseModel):
    refactored_code: str = Field(..., description="Code refactoré")
    original_code: str = Field(..., description="Code original")
    language: str = Field(..., description="Langage du code")
    changes: List[Dict[str, Any]] = Field(..., description="Description des changements effectués")
    explanation: str = Field(..., description="Explication des modifications apportées")
    improvement_metrics: Optional[Dict[str, Any]] = Field(None, description="Métriques d'amélioration")

class LLMRefactoringResult(BaseModel):
    refactored_code: str = Field(..., description="Code refactoré complet")
    changes_summary: str = Field(..., description="Résumé des changements effectués")
    changes_count: int = Field(default=0, description="Nombre de modifications")
    improved_areas: List[str] = Field(
        default_factory=list,
        description="Liste des aspects améliorés (lisibilité, performance, etc.)"
    )
    complexity_reduction: float = Field(
        default=0.0,
        description="Estimation de la réduction de complexité (0.0 à 1.0)",
        ge=0.0,
        le=1.0
    )

class RefactoringTool(BaseTool):
    tool_name = "code_refactoring"
    tool_description = "Refactorise et améliore le code selon différents types de transformations"
    tags = {"generation", "quality"}
    request_model = RefactoringRequest
    response_model = RefactoringResponse
    supported_languages = ["python", "javascript", "typescript", "java", "c#"]

    def get_supported_refactoring_types(self) -> List[str]:
        return ["rename", "extract", "simplify", "optimize", "clean", "modernize"]

    def get_usage_description(self) -> str:
        return ("Outil de refactorisation qui améliore la qualité du code en appliquant différents types de "
                "transformations : renommage, extraction, simplification, optimisation, nettoyage et modernisation. "
                "Il fournit des métriques d'amélioration et des explications détaillées des changements.")

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
                },
                "expected_response": "Variables renommées avec des noms plus clairs et descriptifs"
            },
            {
                "title": "Extraction de méthodes JavaScript",
                "description": "Extraire du code dupliqué en fonctions réutilisables",
                "request": {
                    "code": "function processUser(user) {\n    if (user.email && user.email.includes('@')) {\n        console.log('Valid email');\n    }\n    // ... later in code\n    if (user.email && user.email.includes('@')) {\n        console.log('Valid email');\n    }\n}",
                    "language": "javascript",
                    "refactoring_type": "extract",
                    "parameters": {"extract_duplicates": True}
                },
                "expected_response": "Code dupliqué extrait en fonction isValidEmail réutilisable"
            },
            {
                "title": "Simplification de code complexe",
                "description": "Simplifier une logique complexe avec conditions multiples",
                "request": {
                    "code": "if (user.age > 18):\n    if user.hasLicense:\n        if user.hasInsurance:\n            if user.hasExperience:\n                return True\n    return False",
                    "language": "python",
                    "refactoring_type": "simplify",
                    "parameters": {"reduce_nesting": True}
                },
                "expected_response": "Conditions simplifiées avec logique AND et retour direct"
            },
            {
                "title": "Optimisation de performances",
                "description": "Optimiser du code pour améliorer les performances",
                "request": {
                    "code": "def find_user(users, target_id):\n    for user in users:\n        if user['id'] == target_id:\n            return user\n    return None",
                    "language": "python",
                    "refactoring_type": "optimize",
                    "parameters": {"optimize_loops": True, "use_efficient_structures": True}
                },
                "expected_response": "Code optimisé avec structures de données plus efficaces"
            },
            {
                "title": "Nettoyage de code",
                "description": "Nettoyer du code avec imports inutiles et formatage",
                "request": {
                    "code": "import os\nimport sys\nimport json\nfrom datetime import datetime\n\n\n\ndef hello():\n    print('Hello')   \n\n\n\n",
                    "language": "python",
                    "refactoring_type": "clean",
                    "parameters": {"remove_unused_imports": True, "fix_formatting": True}
                },
                "expected_response": "Code nettoyé avec imports utilisés et formatage correct"
            },
            {
                "title": "Modernisation de code TypeScript",
                "description": "Moderniser du code JavaScript vers TypeScript moderne",
                "request": {
                    "code": "function User(name, age) {\n    this.name = name;\n    this.age = age;\n}\nUser.prototype.greet = function() {\n    return 'Hello ' + this.name;\n};",
                    "language": "typescript",
                    "refactoring_type": "modernize",
                    "parameters": {"use_classes": True, "add_types": True, "modern_syntax": True}
                },
                "expected_response": "Code modernisé avec classes TypeScript et types"
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
            "Fallback local si LLM indisponible",
            "Respect des conventions et bonnes pratiques par langage",
            "Optimisation des performances et réduction de la complexité"
        ]

    def get_refactoring_type_descriptions(self) -> Dict[str, str]:
        return {
            "rename": "Renomme variables, fonctions et classes avec des noms plus descriptifs et clairs",
            "extract": "Extrait le code dupliqué en fonctions/méthodes réutilisables pour réduire la duplication",
            "simplify": "Simplifie la logique complexe, réduit la complexité cyclomatique et améliore la lisibilité",
            "optimize": "Optimise les performances du code, améliore l'efficacité et utilise des structures appropriées",
            "clean": "Nettoie le code en supprimant les éléments inutiles et en améliorant le formatage",
            "modernize": "Met à jour le code pour utiliser les patterns et syntaxes modernes du langage"
        }

    def validate_request(self, request: BaseModel) -> bool:
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
        return True

    def _execute_core_logic(self, request: RefactoringRequest, **kwargs) -> RefactoringResponse:
        ctx = kwargs.get('ctx')
        parser = kwargs.get('parser')

        original_metrics = self._analyze_code_metrics(request.code, request.language)

        if ctx:
            try:
                prompt = self._build_refactoring_prompt(request)
                system_prompt = f"""Tu es un expert en refactoring de code {request.language}.
Applique les meilleures pratiques de refactoring de type '{request.refactoring_type}'.
Réponds UNIQUEMENT avec le code refactoré, sans explications."""

                # Use ctx.sample() via run_async_from_sync for sync context
                result = run_async_from_sync(ctx.sample(
                    messages=prompt,
                    system_prompt=system_prompt,
                    temperature=0.5,
                    max_tokens=2000
                ))

                refactored_code = result.text
                new_metrics = self._analyze_code_metrics(refactored_code, request.language)
                improvement_metrics = self._calculate_improvements(original_metrics, new_metrics)
                changes = self._identify_changes(request, refactored_code)

                explanation = self._generate_explanation(request.refactoring_type, changes, improvement_metrics)

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
                return self._perform_local_refactoring(request, parser)
        else:
            return self._perform_local_refactoring(request, parser)

    async def _execute_core_logic_async(self, request: RefactoringRequest, **kwargs) -> RefactoringResponse:
        ctx = kwargs.get('ctx')
        llm_manager = kwargs.get('llm_manager')
        parser = kwargs.get('parser')
        use_structured_output = kwargs.get('use_structured_output', True)

        if ctx:
            await ctx.info("Analyse du code original...")

        original_metrics = self._analyze_code_metrics(request.code, request.language)

        prompt = self._build_refactoring_prompt(request)
        system_prompt = f"""Tu es un expert en refactoring de code {request.language}.
Applique les meilleures pratiques de refactoring de type '{request.refactoring_type}'."""

        if ctx:
            await ctx.info("Refactoring en cours via LLM...")

        try:

            if ctx is not None and use_structured_output:
                try:
                    self.logger.debug("Utilisation du structured output avec LLMRefactoringResult")
                    # Use ctx.sample() directly with result_type for structured output
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

                        changes = [{"type": area, "description": f"Amélioration: {area}"}
                                   for area in result_data.improved_areas]

                        improvement_metrics = {
                            "complexity_reduction": result_data.complexity_reduction,
                            "changes_count": result_data.changes_count,
                            "improved_areas": result_data.improved_areas
                        }

                        return RefactoringResponse(
                            refactored_code=result_data.refactored_code,
                            original_code=request.code,
                            language=request.language,
                            changes=changes,
                            explanation=result_data.changes_summary,
                            improvement_metrics=improvement_metrics
                        )
                except Exception as e:
                    self.logger.warning(f"Structured output a échoué, fallback vers texte brut: {e}")

            # Use ctx.sample() directly for text generation
            result = await ctx.sample(
                messages=prompt,
                system_prompt=system_prompt + "\nRéponds UNIQUEMENT avec le code refactoré.",
                temperature=0.5,
                max_tokens=2000
            )
            refactored_code = result.text

            if ctx:
                await ctx.info("Analyse des améliorations...")

            new_metrics = self._analyze_code_metrics(refactored_code, request.language)
            improvement_metrics = self._calculate_improvements(original_metrics, new_metrics)
            changes = self._identify_changes(request, refactored_code)
            explanation = self._generate_explanation(request.refactoring_type, changes, improvement_metrics)

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
            return self._perform_local_refactoring(request, parser)

    def _build_refactoring_prompt(self, request) -> str:
        """Build refactoring prompt."""
        language = request.language
        refactoring_type = request.refactoring_type
        code = request.code

        refactoring_desc = REFACTORING_TYPES.get(refactoring_type, "Améliorer la qualité du code")

        prompt_parts = [
            f"Effectue un refactoring de type '{refactoring_type}'",
            f"Description: {refactoring_desc}",
            f"IMPORTANT: Préserve exactement le comportement du code original",
            f"Langage: {language}",
            f""
        ]

        # Add code block
        prompt_parts.extend([
            f"```{language}",
            code,
            f"```",
            f""
        ])

        # Add language-specific instructions
        lang_instructions = REFACTORING_LANGUAGE_INSTRUCTIONS.get(language.lower(), {})
        if refactoring_type in lang_instructions:
            prompt_parts.append(f"Conventions {language}: {lang_instructions[refactoring_type]}")

        return "\n".join(prompt_parts)

    def _get_refactoring_instructions(self, language: str, refactoring_type: str) -> str:
        instructions = {
            "python": {
                "rename": "Utilise les conventions PEP 8 pour les noms (snake_case pour variables/fonctions, PascalCase pour classes)",
                "extract": "Crée des fonctions avec type hints et docstrings appropriés",
                "simplify": "Utilise list/dict comprehensions, walrus operator (:=) quand approprié",
                "optimize": "Utilise les structures de données appropriées (set, deque), évite les boucles inutiles",
                "clean": "Supprime les imports inutiles, utilise f-strings, respecte PEP 8",
                "modernize": "Utilise dataclasses, type hints, pathlib, context managers"
            },
            "javascript": {
                "rename": "Utilise camelCase pour variables/fonctions, PascalCase pour classes/constructeurs",
                "extract": "Crée des fonctions avec JSDoc, utilise arrow functions quand approprié",
                "simplify": "Utilise destructuring, template literals, ternary operators",
                "optimize": "Utilise Map/Set quand approprié, évite les mutations inutiles",
                "clean": "Supprime var, utilise const/let, évite les variables globales",
                "modernize": "Utilise ES6+, async/await, modules ES6, classes"
            },
            "typescript": {
                "rename": "Utilise camelCase avec types explicites, interfaces pour les structures",
                "extract": "Crée des fonctions typées avec génériques quand nécessaire",
                "simplify": "Utilise union types, optional chaining, nullish coalescing",
                "optimize": "Utilise des types stricts, évite 'any', optimise les imports",
                "clean": "Supprime les types redondants, utilise des interfaces appropriées",
                "modernize": "Utilise strict mode, utility types, decorators quand approprié"
            }
        }

        return instructions.get(language.lower(), {}).get(refactoring_type, "")

    def _analyze_code_metrics(self, code: str, language: str) -> Dict[str, Any]:
        lines = code.split('\n')
        non_empty_lines = [line for line in lines if line.strip()]

        metrics = {
            "total_lines": len(lines),
            "code_lines": len(non_empty_lines),
            "comment_lines": 0,
            "function_count": 0,
            "class_count": 0,
            "complexity_score": 0
        }

        comment_patterns = {
            "python": ["#"],
            "javascript": ["//", "/*"],
            "typescript": ["//", "/*"],
            "java": ["//", "/*"],
            "c#": ["//", "/*"]
        }

        patterns = comment_patterns.get(language.lower(), ["#", "//"])
        for line in lines:
            for pattern in patterns:
                if pattern in line:
                    metrics["comment_lines"] += 1
                    break

        if language.lower() == "python":
            metrics["function_count"] = sum(1 for line in non_empty_lines if line.strip().startswith("def "))
            metrics["class_count"] = sum(1 for line in non_empty_lines if line.strip().startswith("class "))
        else:
            metrics["function_count"] = sum(1 for line in non_empty_lines if "function " in line.lower())
            metrics["class_count"] = sum(1 for line in non_empty_lines if "class " in line.lower())


        complexity_indicators = ["if ", "for ", "while ", "switch ", "case ", "try ", "catch ", "except "]
        for line in non_empty_lines:
            for indicator in complexity_indicators:
                if indicator in line.lower():
                    metrics["complexity_score"] += 1

        return metrics

    def _calculate_improvements(self, original: Dict[str, Any], refactored: Dict[str, Any]) -> Dict[str, Any]:
        improvements = {}

        for key in ["code_lines", "complexity_score"]:
            if original[key] > 0:
                change = ((refactored[key] - original[key]) / original[key]) * 100
                improvements[f"{key}_change_percent"] = round(change, 2)

        improvements.update({
            "lines_reduced": original["code_lines"] - refactored["code_lines"],
            "complexity_reduced": original["complexity_score"] - refactored["complexity_score"],
            "comments_added": refactored["comment_lines"] - original["comment_lines"],
            "functions_extracted": refactored["function_count"] - original["function_count"]
        })

        return improvements

    def _identify_changes(self, request: RefactoringRequest, refactored_code: str) -> List[Dict[str, Any]]:
        changes = []

        change_descriptions = {
            "rename": "Variables, fonctions et classes renommées pour plus de clarté",
            "extract": "Code dupliqué extrait en fonctions réutilisables",
            "simplify": "Logique complexe simplifiée et optimisée",
            "optimize": "Performances améliorées et inefficacités supprimées",
            "clean": "Code nettoyé et formaté selon les bonnes pratiques",
            "modernize": "Code mis à jour avec les patterns modernes"
        }

        changes.append({
            "type": request.refactoring_type,
            "description": change_descriptions.get(request.refactoring_type, "Code modifié"),
            "parameters": request.parameters or {}
        })

        original_lines = len(request.code.split('\n'))
        refactored_lines = len(refactored_code.split('\n'))

        if refactored_lines != original_lines:
            changes.append({
                "type": "line_count_change",
                "description": f"Nombre de lignes modifié: {original_lines} → {refactored_lines}",
                "parameters": {"original": original_lines, "refactored": refactored_lines}
            })

        return changes

    def _generate_explanation(self, refactoring_type: str, changes: List[Dict[str, Any]],
                            improvements: Dict[str, Any]) -> str:
        explanation_parts = [
            f"Refactoring de type '{refactoring_type}' appliqué avec succès."
        ]

        if improvements.get("lines_reduced", 0) > 0:
            explanation_parts.append(f"Réduction de {improvements['lines_reduced']} lignes de code.")

        if improvements.get("complexity_reduced", 0) > 0:
            explanation_parts.append(f"Complexité réduite de {improvements['complexity_reduced']} points.")

        if improvements.get("comments_added", 0) > 0:
            explanation_parts.append(f"Ajout de {improvements['comments_added']} lignes de commentaires.")

        for change in changes:
            explanation_parts.append(change["description"])

        return " ".join(explanation_parts)

    def _perform_local_refactoring(self, request: RefactoringRequest, parser=None) -> RefactoringResponse:
        if request.refactoring_type == "clean":
            refactored_code = self._clean_code_basic(request.code, request.language)
        elif request.refactoring_type == "simplify":
            refactored_code = self._simplify_code_basic(request.code, request.language)
        else:

            refactored_code = self._clean_code_basic(request.code, request.language)

        original_metrics = self._analyze_code_metrics(request.code, request.language)
        new_metrics = self._analyze_code_metrics(refactored_code, request.language)
        improvement_metrics = self._calculate_improvements(original_metrics, new_metrics)

        changes = [{
            "type": request.refactoring_type,
            "description": f"Refactoring basique de type '{request.refactoring_type}' appliqué localement",
            "parameters": request.parameters or {}
        }]

        explanation = f"Refactoring local basique appliqué. Recommandation: utiliser un LLM pour un refactoring plus avancé."

        return RefactoringResponse(
            refactored_code=refactored_code,
            original_code=request.code,
            language=request.language,
            changes=changes,
            explanation=explanation,
            improvement_metrics=improvement_metrics
        )

    def _clean_code_basic(self, code: str, language: str) -> str:
        lines = code.split('\n')
        cleaned_lines = []

        for line in lines:

            cleaned_line = line.rstrip()

            if cleaned_line == "" and cleaned_lines and cleaned_lines[-1] == "":
                continue

            cleaned_lines.append(cleaned_line)


        while cleaned_lines and cleaned_lines[-1] == "":
            cleaned_lines.pop()

        return '\n'.join(cleaned_lines)

    def _simplify_code_basic(self, code: str, language: str) -> str:
        cleaned = self._clean_code_basic(code, language)


        if language.lower() == "python":

            cleaned = re.sub(r'\s*==\s*True\b', '', cleaned)
            cleaned = re.sub(r'\s*==\s*False\b', ' is False', cleaned)
            cleaned = re.sub(r'\s*!=\s*True\b', ' is not True', cleaned)

        return cleaned


def refactor_code(request: RefactoringRequest, parser=None, llm_manager=None) -> RefactoringResponse:
    tool = RefactoringTool()
    return tool.execute(request, parser=parser, llm_manager=llm_manager)
