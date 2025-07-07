"""
Code Explanation - Outil d'explication et d'analyse de code
"""
from typing import Optional, Dict, Any, List, Type
from pydantic import BaseModel, Field
from .base import BaseTool, ToolError


class CodeExplanationRequest(BaseModel):
    """Modèle de requête pour l'explication de code."""
    code: str = Field(..., description="Code à expliquer")
    language: Optional[str] = Field(None, description="Langage de programmation du code")
    session_id: Optional[str] = Field(None, description="Identifiant de session")
    detail_level: Optional[str] = Field("medium", description="Niveau de détail de l'explication (basic, medium, detailed)")
    focus_on: Optional[List[str]] = Field(None, description="Aspects spécifiques à expliquer (algorithmes, structures, etc.)")


class CodeExplanationResponse(BaseModel):
    """Modèle de réponse pour l'explication de code."""
    explanation: str = Field(..., description="Explication du code")
    language: str = Field(..., description="Langage du code analysé")
    complexity: Optional[str] = Field(None, description="Évaluation de la complexité du code")
    key_components: Optional[List[Dict[str, str]]] = Field(None, description="Composants clés identifiés dans le code")
    suggestions: Optional[List[str]] = Field(None, description="Suggestions d'amélioration")


class CodeExplanationTool(BaseTool):
    """Outil d'explication et d'analyse de code."""

    def get_name(self) -> str:
        """Retourne le nom unique de l'outil."""
        return "code_explanation"

    def get_description(self) -> str:
        """Retourne la description de l'outil."""
        return "Analyse et explique du code dans différents langages de programmation"

    def get_request_model(self) -> Type[BaseModel]:
        """Retourne le modèle Pydantic pour les requêtes."""
        return CodeExplanationRequest

    def get_response_model(self) -> Type[BaseModel]:
        """Retourne le modèle Pydantic pour les réponses."""
        return CodeExplanationResponse

    def get_supported_languages(self) -> List[str]:
        """Retourne la liste des langages supportés."""
        return ["python", "javascript", "typescript", "java", "c#", "go", "rust", "php", "ruby"]

    def get_usage_description(self) -> str:
        """Description détaillée de l'utilisation de l'outil d'explication de code."""
        return ("Outil d'analyse et d'explication de code qui peut analyser du code dans plusieurs langages "
                "et fournir des explications détaillées, identifier les composants clés, évaluer la complexité "
                "et proposer des améliorations.")

    def get_examples(self) -> List[Dict[str, Any]]:
        """Exemples d'utilisation spécifiques à l'outil d'explication de code."""
        return [
            {
                "title": "Explication de code Python simple",
                "description": "Expliquer une fonction Python basique",
                "request": {
                    "code": "def fibonacci(n):\n    if n <= 1:\n        return n\n    return fibonacci(n-1) + fibonacci(n-2)",
                    "language": "python",
                    "detail_level": "detailed"
                },
                "expected_response": "Explication détaillée de la fonction récursive de Fibonacci avec analyse de complexité"
            },
            {
                "title": "Analyse de code JavaScript",
                "description": "Analyser du code JavaScript avec focus spécifique",
                "request": {
                    "code": "const users = data.filter(user => user.age > 18).map(user => user.name);",
                    "language": "javascript",
                    "detail_level": "medium",
                    "focus_on": ["functional programming", "array methods"]
                },
                "expected_response": "Explication du chaînage de méthodes et des concepts de programmation fonctionnelle"
            },
            {
                "title": "Analyse rapide",
                "description": "Explication basique pour une compréhension rapide",
                "request": {
                    "code": "SELECT * FROM users WHERE age > 18;",
                    "language": "sql",
                    "detail_level": "basic"
                },
                "expected_response": "Explication simple de la requête SQL"
            }
        ]

    def get_capabilities(self) -> List[str]:
        """Capacités spécifiques de l'outil d'explication de code."""
        return [
            "Analyse de code dans 9+ langages de programmation",
            "Évaluation de la complexité algorithmique",
            "Identification des composants clés et patterns",
            "Suggestions d'amélioration et optimisation",
            "Niveaux de détail ajustables (basic, medium, detailed)",
            "Focus sur des aspects spécifiques du code",
            "Détection des bonnes et mauvaises pratiques"
        ]

    def get_limitations(self) -> List[str]:
        """Limitations spécifiques de l'outil d'explication de code."""
        return [
            "Qualité d'analyse dépendante de la clarté du code fourni",
            "Performance variable selon la complexité et la taille du code",
            "Certains langages moins bien supportés que d'autres",
            "Analyse contextuelle limitée pour du code fragmenté",
            "Nécessite parfois des informations supplémentaires pour du code métier spécifique"
        ]

    def get_best_practices(self) -> List[str]:
        """Bonnes pratiques pour l'utilisation de l'outil d'explication de code."""
        return [
            "Fournir du code bien formaté et indenté",
            "Spécifier le langage de programmation pour une analyse optimale",
            "Utiliser le niveau de détail approprié selon vos besoins",
            "Spécifier les aspects à analyser avec 'focus_on' pour une analyse ciblée",
            "Inclure le contexte ou les commentaires importants",
            "Utiliser un session_id pour le suivi de conversations longues",
            "Commencer par 'basic' puis affiner avec 'detailed' si nécessaire"
        ]

    def _execute_core_logic(self, request: CodeExplanationRequest, **kwargs) -> CodeExplanationResponse:
        """
        Exécute la logique principale d'explication de code.

        Args:
            request: Requête d'explication validée
            **kwargs: Services additionnels (llm_manager, parser, etc.)

        Returns:
            CodeExplanationResponse: L'explication du code
        """
        llm_manager = kwargs.get('llm_manager')
        parser = kwargs.get('parser')

        # Détection automatique du langage si non spécifié
        detected_language = self._detect_language(request.code, parser) if not request.language else request.language

        # Validation du langage détecté
        if detected_language:
            self.validate_language(detected_language)

        # Utilisation du LLM si disponible
        if llm_manager is not None:
            try:
                prompt = self._build_explanation_prompt(request, detected_language)
                explanation = llm_manager.sync_generate(prompt)

                # Analyse structurelle du code
                key_components = self._analyze_code_structure(request.code, detected_language, parser)
                complexity = self._evaluate_complexity(request.code, detected_language)
                suggestions = self._generate_improvement_suggestions(request.code, detected_language)

                return CodeExplanationResponse(
                    explanation=explanation,
                    language=detected_language,
                    complexity=complexity,
                    key_components=key_components,
                    suggestions=suggestions
                )
            except Exception as e:
                self.logger.warning(f"Erreur avec LLM, utilisation du fallback: {e}")
                return self._generate_fallback_explanation(request, detected_language, parser)
        else:
            # Analyse locale sans LLM
            return self._generate_fallback_explanation(request, detected_language, parser)

    def _detect_language(self, code: str, parser=None) -> str:
        """
        Détecte automatiquement le langage du code.

        Args:
            code: Code à analyser
            parser: Parser pour assistance à la détection

        Returns:
            Langage détecté
        """
        # Utilisation du parser si disponible
        if parser and hasattr(parser, 'detect_language'):
            try:
                return parser.detect_language(code)
            except:
                pass

        # Détection basique par mots-clés
        language_indicators = {
            'python': ['def ', 'import ', 'from ', '__init__', 'print(', 'if __name__'],
            'javascript': ['function ', 'var ', 'let ', 'const ', 'console.log', '=> ', 'require('],
            'typescript': ['interface ', 'type ', ': string', ': number', ': boolean', 'export ', 'import {'],
            'java': ['public class', 'private ', 'public static void main', 'System.out.println'],
            'c#': ['using System', 'public class', 'private ', 'Console.WriteLine'],
            'go': ['package ', 'func ', 'import ', 'fmt.Print'],
            'rust': ['fn ', 'let mut', 'println!', 'use ', '-> '],
            'php': ['<?php', '$', 'echo ', 'function '],
            'ruby': ['def ', 'end', 'puts ', 'require ']
        }

        code_lower = code.lower()
        scores = {}

        for lang, indicators in language_indicators.items():
            score = sum(1 for indicator in indicators if indicator.lower() in code_lower)
            if score > 0:
                scores[lang] = score

        if scores:
            return max(scores, key=scores.get)

        return "unknown"

    def _build_explanation_prompt(self, request: CodeExplanationRequest, language: str) -> str:
        """
        Construit le prompt pour l'explication avec le LLM.

        Args:
            request: Requête d'explication
            language: Langage détecté

        Returns:
            Prompt optimisé
        """
        prompt_parts = [
            f"Analyse et explique le code {language} suivant :",
            f"```{language}",
            request.code,
            "```"
        ]

        # Niveau de détail
        detail_instructions = {
            "basic": "Fournis une explication simple et concise.",
            "medium": "Fournis une explication détaillée avec les concepts principaux.",
            "detailed": "Fournis une explication très détaillée incluant les nuances techniques."
        }

        level = request.detail_level or "medium"
        prompt_parts.append(detail_instructions.get(level, detail_instructions["medium"]))

        # Focus spécifique
        if request.focus_on:
            focus_str = ", ".join(request.focus_on)
            prompt_parts.append(f"Concentre-toi particulièrement sur : {focus_str}")

        # Instructions par langage
        language_instructions = self._get_language_analysis_instructions(language)
        if language_instructions:
            prompt_parts.append(language_instructions)

        return "\n".join(prompt_parts)

    def _get_language_analysis_instructions(self, language: str) -> str:
        """Instructions spécifiques par langage pour l'analyse."""
        instructions = {
            "python": "Explique les concepts Python comme les décorateurs, list comprehensions, et idiomes pythoniques.",
            "javascript": "Explique les concepts JS comme les closures, prototypes, et gestion asynchrone.",
            "typescript": "Explique le système de types, interfaces, et différences avec JavaScript.",
            "java": "Explique les concepts OOP, polymorphisme, et gestion mémoire.",
            "c#": "Explique les propriétés, LINQ, et intégration .NET.",
            "go": "Explique les goroutines, channels, et idiomes Go.",
            "rust": "Explique ownership, borrowing, et sécurité mémoire."
        }
        return instructions.get(language.lower(), "")

    def _analyze_code_structure(self, code: str, language: str, parser=None) -> List[Dict[str, str]]:
        """
        Analyse la structure du code.

        Returns:
            Liste des composants identifiés
        """
        components = []

        # Utilisation du parser si disponible
        if parser and hasattr(parser, f'parse_{language.lower()}'):
            try:
                parse_method = getattr(parser, f'parse_{language.lower()}')
                parsed = parse_method(code)

                # Extraction des fonctions
                if 'functions' in parsed:
                    for func in parsed['functions']:
                        components.append({
                            "type": "function",
                            "name": func.get('name', 'unnamed'),
                            "description": f"Fonction avec {len(func.get('params', []))} paramètres"
                        })

                # Extraction des classes
                if 'classes' in parsed:
                    for cls in parsed['classes']:
                        components.append({
                            "type": "class",
                            "name": cls.get('name', 'unnamed'),
                            "description": f"Classe avec {len(cls.get('methods', []))} méthodes"
                        })

                return components
            except Exception as e:
                self.logger.debug(f"Erreur parsing avec parser: {e}")

        # Analyse basique par regex/pattern
        return self._basic_structure_analysis(code, language)

    def _basic_structure_analysis(self, code: str, language: str) -> List[Dict[str, str]]:
        """Analyse structurelle basique sans parser."""
        components = []
        lines = code.split('\n')

        if language.lower() == 'python':
            for i, line in enumerate(lines):
                line = line.strip()
                if line.startswith("def "):
                    func_name = line.split("def ")[1].split("(")[0].strip()
                    components.append({
                        "type": "function",
                        "name": func_name,
                        "description": f"Fonction Python à la ligne {i+1}"
                    })
                elif line.startswith("class "):
                    class_name = line.split("class ")[1].split("(")[0].split(":")[0].strip()
                    components.append({
                        "type": "class",
                        "name": class_name,
                        "description": f"Classe Python à la ligne {i+1}"
                    })

        elif language.lower() in ['javascript', 'typescript']:
            for i, line in enumerate(lines):
                line = line.strip()
                if 'function ' in line:
                    # Extraction du nom de fonction
                    if line.startswith('function '):
                        func_name = line.split('function ')[1].split('(')[0].strip()
                    else:
                        func_name = "anonymous"
                    components.append({
                        "type": "function",
                        "name": func_name,
                        "description": f"Fonction {language} à la ligne {i+1}"
                    })
                elif 'class ' in line:
                    class_name = line.split('class ')[1].split(' ')[0].split('{')[0].strip()
                    components.append({
                        "type": "class",
                        "name": class_name,
                        "description": f"Classe {language} à la ligne {i+1}"
                    })

        return components

    def _evaluate_complexity(self, code: str, language: str) -> str:
        """
        Évalue la complexité du code.

        Returns:
            Niveau de complexité (low, medium, high)
        """
        lines = code.split('\n')
        non_empty_lines = [line for line in lines if line.strip()]

        # Facteurs de complexité
        complexity_score = 0

        # Taille du code
        if len(non_empty_lines) > 100:
            complexity_score += 2
        elif len(non_empty_lines) > 50:
            complexity_score += 1

        # Structures de contrôle
        control_structures = ['if ', 'for ', 'while ', 'switch ', 'case ', 'try ', 'catch ', 'except ']
        for line in non_empty_lines:
            for structure in control_structures:
                if structure in line.lower():
                    complexity_score += 0.5

        # Fonctions/méthodes
        if language.lower() == 'python':
            function_count = sum(1 for line in non_empty_lines if line.strip().startswith('def '))
        else:
            function_count = sum(1 for line in non_empty_lines if 'function ' in line.lower())

        complexity_score += function_count * 0.3

        # Classification
        if complexity_score <= 3:
            return "low"
        elif complexity_score <= 8:
            return "medium"
        else:
            return "high"

    def _generate_improvement_suggestions(self, code: str, language: str) -> List[str]:
        """Génère des suggestions d'amélioration."""
        suggestions = []

        # Suggestions génériques
        if len(code.split('\n')) > 50:
            suggestions.append("Considérer diviser le code en fonctions plus petites")

        if not any(comment in code for comment in ['#', '//', '/*', '"""']):
            suggestions.append("Ajouter des commentaires pour expliquer la logique")

        # Suggestions par langage
        language_suggestions = {
            "python": [
                "Utiliser des type hints pour une meilleure documentation",
                "Considérer l'utilisation de dataclasses ou Pydantic",
                "Vérifier la conformité PEP 8"
            ],
            "javascript": [
                "Utiliser const/let au lieu de var",
                "Considérer l'utilisation d'async/await",
                "Ajouter une gestion d'erreurs avec try/catch"
            ],
            "typescript": [
                "Utiliser des interfaces pour définir les structures",
                "Éviter l'utilisation d'any quand possible",
                "Configurer des règles ESLint strictes"
            ]
        }

        lang_specific = language_suggestions.get(language.lower(), [])
        suggestions.extend(lang_specific[:2])  # Limiter à 2 suggestions spécifiques

        return suggestions

    def _generate_fallback_explanation(self, request: CodeExplanationRequest, language: str, parser=None) -> CodeExplanationResponse:
        """Génère une explication sans LLM."""

        # Analyse structurelle
        key_components = self._analyze_code_structure(request.code, language, parser)
        complexity = self._evaluate_complexity(request.code, language)
        suggestions = self._generate_improvement_suggestions(request.code, language)

        # Explication basique
        explanation_parts = [
            f"Ce code {language} contient {len(request.code.split())} lignes.",
        ]

        if key_components:
            funcs = [c for c in key_components if c['type'] == 'function']
            classes = [c for c in key_components if c['type'] == 'class']

            if funcs:
                explanation_parts.append(f"Il définit {len(funcs)} fonction(s): {', '.join(f['name'] for f in funcs[:3])}{'...' if len(funcs) > 3 else ''}.")

            if classes:
                explanation_parts.append(f"Il contient {len(classes)} classe(s): {', '.join(c['name'] for c in classes[:3])}{'...' if len(classes) > 3 else ''}.")

        explanation_parts.append(f"La complexité du code est évaluée comme {complexity}.")

        explanation = " ".join(explanation_parts)

        return CodeExplanationResponse(
            explanation=explanation,
            language=language,
            complexity=complexity,
            key_components=key_components,
            suggestions=suggestions
        )


# Fonction de compatibilité pour l'ancien système
def explain_code(request: CodeExplanationRequest, parser=None, llm_manager=None) -> CodeExplanationResponse:
    """
    Fonction de compatibilité avec l'ancien système.

    Args:
        request: Requête d'explication de code
        parser: Parser pour l'analyse
        llm_manager: Service LLM

    Returns:
        Réponse d'explication de code
    """
    tool = CodeExplanationTool()
    return tool.execute(request, parser=parser, llm_manager=llm_manager)
