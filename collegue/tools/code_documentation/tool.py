"""
Code Documentation - Outil de génération automatique de documentation.

Cet outil génère de la documentation pour le code dans différents formats
et styles : Markdown, RST, HTML, docstring, JSON.

Refactorisé: Le fichier original faisait 668 lignes, maintenant ~180 lignes.
"""

from typing import List, Dict, Any
from ..base import BaseTool, ToolError
from ...core.shared import run_async_from_sync
from .models import DocumentationRequest, DocumentationResponse
from .engine import DocumentationEngine
from .config import STYLE_DESCRIPTIONS, FORMAT_DESCRIPTIONS


class DocumentationTool(BaseTool):
    """
    Outil de génération automatique de documentation.

    Analyse le code et génère une documentation complète dans différents
    formats (Markdown, RST, HTML, docstring, JSON) et styles.
    """

    tool_name = "code_documentation"
    tool_description = (
        "Génère automatiquement de la documentation pour le code fourni.\n"
        "\n"
        "PARAMÈTRES REQUIS:\n"
        "- code: Le code source complet à documenter.\n"
        "- language: Le langage de programmation du code (ex: 'python', 'javascript', 'typescript', 'php').\n"
        "\n"
        "PARAMÈTRES OPTIONNELS:\n"
        "- doc_format: Format de sortie. Options: 'markdown', 'rst', 'html', 'docstring', 'json'. Défaut: 'markdown'.\n"
        "- doc_style: Style de la doc. Options: 'standard', 'detailed', 'minimal', 'api', 'tutorial'. Défaut: 'standard'.\n"
        "- include_examples: Booléen. S'il faut inclure des exemples d'utilisation.\n"
        "- focus_on: Filtre les éléments à documenter. Options: 'functions', 'classes', 'modules', 'all'.\n"
        "- file_path: Chemin du fichier (pour le contexte).\n"
        "- session_id: Identifiant de session (pour un contexte continu).\n"
        "\n"
        "UTILISATION:\n"
        "Utile pour générer des fichiers Lisez-moi (README), des docstrings pour décorer / commenter des signatures de méthodes, "
        "ou créer une documentation API robuste."
    )
    tags = {"generation"}
    request_model = DocumentationRequest
    response_model = DocumentationResponse
    supported_languages = [
        "python",
        "javascript",
        "typescript",
        "java",
        "c#",
        "go",
        "rust",
        "php",
    ]
    long_running = True

    def __init__(self, config=None, app_state=None):
        super().__init__(config, app_state)
        self._engine = DocumentationEngine(logger=self.logger)

    def get_supported_formats(self) -> List[str]:
        """Retourne les formats de documentation supportés."""
        return ["markdown", "rst", "html", "docstring", "json"]

    def get_supported_styles(self) -> List[str]:
        """Retourne les styles de documentation supportés."""
        return ["standard", "detailed", "minimal", "api", "tutorial"]

    def get_usage_description(self) -> str:
        return (
            "Outil de génération automatique de documentation qui analyse le code et génère une documentation "
            "complète dans différents formats (Markdown, RST, HTML, docstring, JSON) et styles. Il peut "
            "documenter des fonctions, classes, modules avec des exemples d'utilisation et calcule la "
            "couverture documentaire."
        )

    def get_examples(self) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Documentation Python standard",
                "description": "Générer une documentation Markdown standard pour une classe Python",
                "request": {
                    "code": "class Calculator:\n    def __init__(self):\n        self.result = 0\n    \n    def add(self, x, y):\n        return x + y",
                    "language": "python",
                    "doc_format": "markdown",
                    "doc_style": "standard",
                    "include_examples": True,
                },
            },
            {
                "title": "Documentation API JavaScript",
                "description": "Générer une documentation API pour des fonctions JavaScript",
                "request": {
                    "code": "function fetchUserData(userId) {\n    return fetch(`/api/users/${userId}`);\n}",
                    "language": "javascript",
                    "doc_format": "html",
                    "doc_style": "api",
                },
            },
            {
                "title": "Documentation docstring Python",
                "description": "Générer des docstrings pour des fonctions Python",
                "request": {
                    "code": "def binary_search(arr, target):\n    left, right = 0, len(arr) - 1\n    while left <= right:\n        mid = (left + right) // 2\n        if arr[mid] == target:\n            return mid\n    return -1",
                    "language": "python",
                    "doc_format": "docstring",
                    "doc_style": "standard",
                    "focus_on": "functions",
                },
            },
        ]

    def get_capabilities(self) -> List[str]:
        return [
            "Génération de documentation dans 8+ langages de programmation",
            "5 formats de sortie : Markdown, RST, HTML, docstring, JSON",
            "5 styles de documentation : standard, detailed, minimal, api, tutorial",
            "Analyse automatique des éléments de code (fonctions, classes, modules)",
            "Calcul de la couverture documentaire en pourcentage",
            "Inclusion d'exemples d'utilisation personnalisés",
            "Focus sélectif sur des éléments spécifiques",
            "Génération de suggestions d'amélioration",
        ]

    def get_documentation_style_descriptions(self) -> Dict[str, str]:
        """Retourne les descriptions des styles de documentation."""
        return STYLE_DESCRIPTIONS

    def get_format_descriptions(self) -> Dict[str, str]:
        """Retourne les descriptions des formats de sortie."""
        return FORMAT_DESCRIPTIONS

    def validate_request(self, request) -> bool:
        """Valide la requête de documentation."""
        super().validate_request(request)

        if hasattr(request, "doc_format") and request.doc_format:
            supported_formats = self.get_supported_formats()
            if request.doc_format not in supported_formats:
                raise ToolError(
                    f"Format '{request.doc_format}' non supporté. "
                    f"Formats supportés: {supported_formats}"
                )

        if hasattr(request, "doc_style") and request.doc_style:
            supported_styles = self.get_supported_styles()
            if request.doc_style not in supported_styles:
                raise ToolError(
                    f"Style '{request.doc_style}' non supporté. "
                    f"Styles supportés: {supported_styles}"
                )

        return True

    def _execute_core_logic(
        self, request: DocumentationRequest, **kwargs
    ) -> DocumentationResponse:
        """Exécute la génération de documentation (synchrone)."""
        ctx = kwargs.get("ctx")
        parser = kwargs.get("parser")

        # Analyser les éléments du code
        code_elements = self._engine.analyze_code_elements(
            request.code, request.language, parser
        )

        if ctx:
            try:
                # Construire et envoyer le prompt au LLM
                prompt = self._engine.build_prompt(
                    request.code,
                    request.language,
                    request.doc_style or "standard",
                    request.doc_format or "markdown",
                    request.include_examples or False,
                    request.focus_on or "all",
                    code_elements,
                )

                system_prompt = f"""Tu es un expert en documentation de code {request.language}.
Génère une documentation claire, complète et bien structurée au format {request.doc_format or "markdown"}.
Style de documentation: {request.doc_style or "standard"}."""

                result = run_async_from_sync(
                    ctx.sample(
                        messages=prompt,
                        system_prompt=system_prompt,
                        temperature=0.5,
                        max_tokens=2000,
                    )
                )

                generated_docs = result.text

                # Formater et finaliser la documentation
                formatted_docs = self._engine.format_documentation(
                    generated_docs, request.doc_format or "markdown", request.language
                )

                coverage = self._engine.calculate_coverage(
                    code_elements, formatted_docs
                )
                suggestions = self._engine.generate_suggestions(
                    code_elements,
                    coverage,
                    request.doc_format or "markdown",
                    request.doc_style or "standard",
                    request.include_examples or False,
                )

                return DocumentationResponse(
                    documentation=formatted_docs,
                    language=request.language,
                    format=request.doc_format or "markdown",
                    documented_elements=code_elements,
                    coverage=coverage,
                    suggestions=suggestions,
                )

            except Exception as e:
                self.logger.warning(
                    f"Erreur avec ctx.sample(), utilisation du fallback: {e}"
                )
                return self._generate_fallback_response(request, code_elements)
        else:
            return self._generate_fallback_response(request, code_elements)

    async def _execute_core_logic_async(
        self, request: DocumentationRequest, **kwargs
    ) -> DocumentationResponse:
        """Version asynchrone de la génération de documentation."""
        ctx = kwargs.get("ctx")
        parser = kwargs.get("parser")

        if ctx:
            await ctx.info("Analyse du code...")

        # Analyser les éléments du code
        code_elements = self._engine.analyze_code_elements(
            request.code, request.language, parser
        )

        # Construire le prompt
        prompt = self._engine.build_prompt(
            request.code,
            request.language,
            request.doc_style or "standard",
            request.doc_format or "markdown",
            request.include_examples or False,
            request.focus_on or "all",
            code_elements,
        )

        system_prompt = f"""Tu es un expert en documentation de code {request.language}.
Génère une documentation claire, complète et bien structurée au format {request.doc_format or "markdown"}.
Style de documentation: {request.doc_style or "standard"}."""

        if ctx:
            await ctx.info("Génération de la documentation via LLM...")

        try:
            result = await ctx.sample(
                messages=prompt,
                system_prompt=system_prompt,
                temperature=0.5,
                max_tokens=2000,
            )
            generated_docs = result.text

            if ctx:
                await ctx.info("Documentation générée, formatage...")

            # Formater et finaliser
            formatted_docs = self._engine.format_documentation(
                generated_docs, request.doc_format or "markdown", request.language
            )

            coverage = self._engine.calculate_coverage(code_elements, formatted_docs)
            suggestions = self._engine.generate_suggestions(
                code_elements,
                coverage,
                request.doc_format or "markdown",
                request.doc_style or "standard",
                request.include_examples or False,
            )

            return DocumentationResponse(
                documentation=formatted_docs,
                language=request.language,
                format=request.doc_format or "markdown",
                documented_elements=code_elements,
                coverage=coverage,
                suggestions=suggestions,
            )

        except Exception as e:
            self.logger.warning(f"Erreur LLM async, utilisation du fallback: {e}")
            return self._generate_fallback_response(request, code_elements)

    def _generate_fallback_response(
        self, request: DocumentationRequest, elements: List[Dict[str, str]]
    ) -> DocumentationResponse:
        """Génère une réponse fallback quand le LLM n'est pas disponible."""
        documentation = self._engine.generate_fallback_documentation(
            request.code, request.language, elements, request.doc_format or "markdown"
        )

        coverage = self._engine.calculate_coverage(elements, documentation)

        suggestions = [
            "Documentation générée automatiquement. Recommandation: utiliser un LLM pour une documentation plus riche.",
            "Ajouter des descriptions détaillées pour chaque élément.",
            "Inclure des exemples d'utilisation.",
        ]

        return DocumentationResponse(
            documentation=documentation,
            language=request.language,
            format=request.doc_format or "markdown",
            documented_elements=elements,
            coverage=coverage,
            suggestions=suggestions,
        )


def generate_documentation(
    request: DocumentationRequest, parser=None
) -> DocumentationResponse:
    """Fonction utilitaire pour générer de la documentation."""
    tool = DocumentationTool()
    return tool.execute(request, parser=parser)
