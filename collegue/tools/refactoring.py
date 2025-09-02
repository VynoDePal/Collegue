"""
Refactoring - Outil de refactoring et d'amélioration de code
"""
import asyncio
from typing import Optional, Dict, Any, List, Union, Type
from pydantic import BaseModel, Field
from .base import BaseTool, ToolError


class RefactoringRequest(BaseModel):
    """Modèle de requête pour le refactoring de code."""
    code: str = Field(..., description="Code à refactorer")
    language: str = Field(..., description="Langage de programmation du code")
    session_id: Optional[str] = Field(None, description="Identifiant de session")
    refactoring_type: str = Field(..., description="Type de refactoring à appliquer (rename, extract, simplify, optimize, clean)")
    parameters: Optional[Dict[str, Any]] = Field(None, description="Paramètres spécifiques au type de refactoring")
    file_path: Optional[str] = Field(None, description="Chemin du fichier contenant le code")


class RefactoringResponse(BaseModel):
    """Modèle de réponse pour le refactoring de code."""
    refactored_code: str = Field(..., description="Code refactoré")
    original_code: str = Field(..., description="Code original")
    language: str = Field(..., description="Langage du code")
    changes: List[Dict[str, Any]] = Field(..., description="Description des changements effectués")
    explanation: str = Field(..., description="Explication des modifications apportées")
    improvement_metrics: Optional[Dict[str, Any]] = Field(None, description="Métriques d'amélioration")


class RefactoringTool(BaseTool):
    """Outil de refactoring et d'amélioration de code."""

    def get_name(self) -> str:
        """Retourne le nom unique de l'outil."""
        return "code_refactoring"

    def get_description(self) -> str:
        """Retourne la description de l'outil."""
        return "Refactorise et améliore le code selon différents types de transformations"

    def get_request_model(self) -> Type[BaseModel]:
        """Retourne le modèle Pydantic pour les requêtes."""
        return RefactoringRequest

    def get_response_model(self) -> Type[BaseModel]:
        """Retourne le modèle Pydantic pour les réponses."""
        return RefactoringResponse

    def get_supported_languages(self) -> List[str]:
        """Retourne la liste des langages supportés."""
        return ["python", "javascript", "typescript", "java", "c#"]

    def get_supported_refactoring_types(self) -> List[str]:
        """Retourne les types de refactoring supportés."""
        return ["rename", "extract", "simplify", "optimize", "clean", "modernize"]

    def get_usage_description(self) -> str:
        """Description détaillée de l'utilisation de l'outil de refactorisation."""
        return ("Outil de refactorisation qui améliore la qualité du code en appliquant différents types de "
                "transformations : renommage, extraction, simplification, optimisation, nettoyage et modernisation. "
                "Il fournit des métriques d'amélioration et des explications détaillées des changements.")

    def get_examples(self) -> List[Dict[str, Any]]:
        """Exemples d'utilisation spécifiques à l'outil de refactorisation."""
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
        """Capacités spécifiques de l'outil de refactorisation."""
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
        """Descriptions détaillées des types de refactoring."""
        return {
            "rename": "Renomme variables, fonctions et classes avec des noms plus descriptifs et clairs",
            "extract": "Extrait le code dupliqué en fonctions/méthodes réutilisables pour réduire la duplication",
            "simplify": "Simplifie la logique complexe, réduit la complexité cyclomatique et améliore la lisibilité",
            "optimize": "Optimise les performances du code, améliore l'efficacité et utilise des structures appropriées",
            "clean": "Nettoie le code en supprimant les éléments inutiles et en améliorant le formatage",
            "modernize": "Met à jour le code pour utiliser les patterns et syntaxes modernes du langage"
        }

    def validate_request(self, request: BaseModel) -> bool:
        """Validation étendue pour les requêtes de refactoring."""
        # Validation de base
        super().validate_request(request)

        # Validation du type de refactoring
        if hasattr(request, 'refactoring_type'):
            supported_types = self.get_supported_refactoring_types()
            if request.refactoring_type not in supported_types:
                raise ToolError(
                    f"Type de refactoring '{request.refactoring_type}' non supporté. "
                    f"Types supportés: {supported_types}"
                )

        return True

    def _execute_core_logic(self, request: RefactoringRequest, **kwargs) -> RefactoringResponse:
        """
        Exécute la logique principale de refactoring.

        Args:
            request: Requête de refactoring validée
            **kwargs: Services additionnels (llm_manager, parser, etc.)

        Returns:
            RefactoringResponse: Le code refactorisé
        """
        llm_manager = kwargs.get('llm_manager')
        parser = kwargs.get('parser')

        # Analyse du code original
        original_metrics = self._analyze_code_metrics(request.code, request.language)

        # Utilisation du LLM si disponible
        if llm_manager is not None:
            try:
                # Utiliser le nouveau système de prompts avec prepare_prompt
                context = {
                    "code": request.code,
                    "language": request.language,
                    "refactoring_type": request.refactoring_type,
                    "parameters": str(request.parameters) if request.parameters else "default settings",
                    "file_path": request.file_path or "unknown"
                }
                
                # Essayer d'utiliser prepare_prompt (nouveau système)
                try:
                    if asyncio.iscoroutinefunction(self.prepare_prompt):
                        # Si c'est une méthode asynchrone, l'exécuter de manière synchrone
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            # Si une boucle est déjà en cours, créer une tâche
                            import concurrent.futures
                            with concurrent.futures.ThreadPoolExecutor() as executor:
                                future = executor.submit(asyncio.run, self.prepare_prompt(request, context=context))
                                prompt = future.result()
                        else:
                            prompt = loop.run_until_complete(self.prepare_prompt(request, context=context))
                    else:
                        prompt = self.prepare_prompt(request, context=context)
                except Exception as e:
                    self.logger.debug(f"Fallback vers _build_refactoring_prompt: {e}")
                    prompt = self._build_refactoring_prompt(request)
                
                refactored_code = llm_manager.sync_generate(prompt)

                # Analyse du code refactorisé
                new_metrics = self._analyze_code_metrics(refactored_code, request.language)

                # Calcul des améliorations
                improvement_metrics = self._calculate_improvements(original_metrics, new_metrics)

                # Identification des changements
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
                self.logger.warning(f"Erreur avec LLM, utilisation du fallback: {e}")
                return self._perform_local_refactoring(request, parser)
        else:
            # Refactoring local sans LLM
            return self._perform_local_refactoring(request, parser)

    def _build_refactoring_prompt(self, request: RefactoringRequest) -> str:
        """
        Construit le prompt pour le refactoring avec le LLM.

        Args:
            request: Requête de refactoring

        Returns:
            Prompt optimisé
        """
        refactoring_instructions = {
            "rename": "Renomme les variables, fonctions et classes avec des noms plus descriptifs et clairs",
            "extract": "Extrait les blocs de code répétitifs en fonctions/méthodes réutilisables",
            "simplify": "Simplifie la logique complexe, réduis la complexité cyclomatique",
            "optimize": "Optimise les performances du code, améliore l'efficacité",
            "clean": "Nettoie le code en supprimant les éléments inutiles et en améliorant la lisibilité",
            "modernize": "Met à jour le code pour utiliser les patterns et syntaxes modernes du langage"
        }

        prompt_parts = [
            f"Refactorise le code {request.language} suivant selon le type '{request.refactoring_type}':",
            f"Instructions: {refactoring_instructions.get(request.refactoring_type, 'Améliore le code')}",
            "",
            f"```{request.language}",
            request.code,
            "```"
        ]

        # Paramètres spécifiques
        if request.parameters:
            prompt_parts.insert(-3, f"Paramètres spécifiques: {request.parameters}")

        # Instructions par langage
        language_instructions = self._get_refactoring_instructions(request.language, request.refactoring_type)
        if language_instructions:
            prompt_parts.insert(-3, f"Instructions {request.language}: {language_instructions}")

        prompt_parts.extend([
            "",
            "Fournis uniquement le code refactorisé sans explications supplémentaires.",
            "Préserve la fonctionnalité originale du code."
        ])

        return "\n".join(prompt_parts)

    def _get_refactoring_instructions(self, language: str, refactoring_type: str) -> str:
        """Instructions spécifiques par langage et type de refactoring."""
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
        """Analyse les métriques du code."""
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

        # Comptage des commentaires
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

        # Comptage des fonctions et classes
        if language.lower() == "python":
            metrics["function_count"] = sum(1 for line in non_empty_lines if line.strip().startswith("def "))
            metrics["class_count"] = sum(1 for line in non_empty_lines if line.strip().startswith("class "))
        else:
            metrics["function_count"] = sum(1 for line in non_empty_lines if "function " in line.lower())
            metrics["class_count"] = sum(1 for line in non_empty_lines if "class " in line.lower())

        # Score de complexité basique
        complexity_indicators = ["if ", "for ", "while ", "switch ", "case ", "try ", "catch ", "except "]
        for line in non_empty_lines:
            for indicator in complexity_indicators:
                if indicator in line.lower():
                    metrics["complexity_score"] += 1

        return metrics

    def _calculate_improvements(self, original: Dict[str, Any], refactored: Dict[str, Any]) -> Dict[str, Any]:
        """Calcule les améliorations entre l'original et le code refactorisé."""
        improvements = {}

        # Changement en pourcentage
        for key in ["code_lines", "complexity_score"]:
            if original[key] > 0:
                change = ((refactored[key] - original[key]) / original[key]) * 100
                improvements[f"{key}_change_percent"] = round(change, 2)

        # Métriques d'amélioration
        improvements.update({
            "lines_reduced": original["code_lines"] - refactored["code_lines"],
            "complexity_reduced": original["complexity_score"] - refactored["complexity_score"],
            "comments_added": refactored["comment_lines"] - original["comment_lines"],
            "functions_extracted": refactored["function_count"] - original["function_count"]
        })

        return improvements

    def _identify_changes(self, request: RefactoringRequest, refactored_code: str) -> List[Dict[str, Any]]:
        """Identifie les changements effectués."""
        changes = []

        # Changement principal basé sur le type
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

        # Analyse basique des différences
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
        """Génère l'explication des modifications."""
        explanation_parts = [
            f"Refactoring de type '{refactoring_type}' appliqué avec succès."
        ]

        # Résumé des améliorations
        if improvements.get("lines_reduced", 0) > 0:
            explanation_parts.append(f"Réduction de {improvements['lines_reduced']} lignes de code.")

        if improvements.get("complexity_reduced", 0) > 0:
            explanation_parts.append(f"Complexité réduite de {improvements['complexity_reduced']} points.")

        if improvements.get("comments_added", 0) > 0:
            explanation_parts.append(f"Ajout de {improvements['comments_added']} lignes de commentaires.")

        # Description des changements
        for change in changes:
            explanation_parts.append(change["description"])

        return " ".join(explanation_parts)

    def _perform_local_refactoring(self, request: RefactoringRequest, parser=None) -> RefactoringResponse:
        """Effectue un refactoring local basique sans LLM."""

        # Refactoring basique selon le type
        if request.refactoring_type == "clean":
            refactored_code = self._clean_code_basic(request.code, request.language)
        elif request.refactoring_type == "simplify":
            refactored_code = self._simplify_code_basic(request.code, request.language)
        else:
            # Pour les autres types, on nettoie au minimum
            refactored_code = self._clean_code_basic(request.code, request.language)

        # Calcul des métriques
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
        """Nettoyage basique du code."""
        lines = code.split('\n')
        cleaned_lines = []

        for line in lines:
            # Suppression des espaces en fin de ligne
            cleaned_line = line.rstrip()

            # Suppression des lignes vides multiples
            if cleaned_line == "" and cleaned_lines and cleaned_lines[-1] == "":
                continue

            cleaned_lines.append(cleaned_line)

        # Suppression des lignes vides en fin de fichier
        while cleaned_lines and cleaned_lines[-1] == "":
            cleaned_lines.pop()

        return '\n'.join(cleaned_lines)

    def _simplify_code_basic(self, code: str, language: str) -> str:
        """Simplification basique du code."""
        # Pour l'instant, on applique juste le nettoyage
        # Une vraie simplification nécessiterait une analyse syntaxique avancée
        cleaned = self._clean_code_basic(code, language)

        # Simplifications basiques par langage
        if language.lower() == "python":
            # Remplacement de certains patterns simples
            cleaned = cleaned.replace("== True", "")
            cleaned = cleaned.replace("== False", " is False")
            cleaned = cleaned.replace("!= True", " is not True")

        return cleaned


# Fonction de compatibilité pour l'ancien système
def refactor_code(request: RefactoringRequest, parser=None, llm_manager=None) -> RefactoringResponse:
    """
    Fonction de compatibilité avec l'ancien système.

    Args:
        request: Requête de refactoring
        parser: Parser pour l'analyse
        llm_manager: Service LLM

    Returns:
        Réponse de refactoring
    """
    tool = RefactoringTool()
    return tool.execute(request, parser=parser, llm_manager=llm_manager)
