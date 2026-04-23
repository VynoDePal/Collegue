"""
Test Generation - Outil de génération automatique de tests unitaires.

Cet outil analyse le code source et génère des tests unitaires complets
avec différents frameworks et options de personnalisation.

Refactorisé: Le fichier original faisait 767 lignes, maintenant ~200 lignes.
"""

import time
from typing import List, Dict, Any, Optional
import pathlib

from ..base import BaseTool, ToolError, ToolValidationError
from ...core.shared import run_async_from_sync
from .models import (
    TestGenerationRequest,
    TestGenerationResponse,
    LLMTestGenerationResult,
)
from .engine import TestGenerationEngine


class TestGenerationTool(BaseTool):
    """
    Outil de génération automatique de tests unitaires.

    Supporte 6 langages avec 15+ frameworks de test, génère des tests
    avec mocks, stubs et vise une couverture de code personnalisable.
    """

    __test__ = False

    tool_name = "test_generation"
    tool_description = (
        "Génère automatiquement des tests unitaires exécutables pour du code source.\n"
        "\n"
        "PARAMÈTRES REQUIS:\n"
        "- code: Le code source complet de la ou des fonctions/classes à tester.\n"
        "- language: Le langage de programmation (ex: 'python', 'javascript', 'typescript', 'php').\n"
        "\n"
        "PARAMÈTRES OPTIONNELS:\n"
        "- test_framework: Le framework de test souhaité (ex: 'pytest', 'jest', 'phpunit').\n"
        "- include_mocks: Booléen. Si True, génère des mocks/stubs pour les dépendances externes.\n"
        "- coverage_target: Float (0.0-1.0). Couverture visée (défaut: 0.8 pour 80%).\n"
        "- file_path: Chemin du fichier source original (pour le contexte et générer un bon nom de fichier test).\n"
        "- output_dir: Où sauvegarder les tests générés (sinon renvoie uniquement le code en réponse).\n"
        "- session_id: Identifiant de session.\n"
        "\n"
        "UTILISATION:\n"
        "Ce tool conçoit des tests propres. Spécifiez toujours un `test_framework` si vous connaissez l'écosystème du projet."
    )
    tags = {"generation", "testing"}
    request_model = TestGenerationRequest
    response_model = TestGenerationResponse
    supported_languages = ["python", "javascript", "typescript", "php"]
    long_running = True

    def __init__(self, config=None, app_state=None):
        super().__init__(config, app_state)
        self._engine = TestGenerationEngine(logger=self.logger)

    def get_supported_test_frameworks(self) -> Dict[str, List[str]]:
        """Retourne les frameworks de test supportés par langage."""
        return (
            self._engine.TEST_FRAMEWORKS
            if hasattr(self._engine, "TEST_FRAMEWORKS")
            else {
                "python": ["unittest", "pytest", "nose2"],
                "javascript": ["jest", "mocha", "jasmine", "vitest"],
                "typescript": ["jest", "mocha", "jasmine", "vitest"],
                "php": ["phpunit", "pest", "codeception", "behat", "phpspec", "kahlan"],
            }
        )

    def get_usage_description(self) -> str:
        return (
            "Outil de génération automatique de tests unitaires qui analyse le code source et génère des tests "
            "complets avec différents frameworks. Il peut inclure des mocks, viser une couverture spécifique, "
            "et génère des tests pour fonctions, classes et modules avec estimation de couverture."
        )

    def get_examples(self) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Tests Python avec pytest",
                "description": "Générer des tests pytest pour une classe Python",
                "request": {
                    "code": "class Calculator:\n    def add(self, a, b):\n        return a + b",
                    "language": "python",
                    "test_framework": "pytest",
                    "coverage_target": 0.9,
                },
            },
            {
                "title": "Tests JavaScript avec Jest",
                "description": "Générer des tests Jest pour des fonctions asynchrones",
                "request": {
                    "code": "async function fetchUserData(userId) {\n    return fetch(`/api/users/${userId}`);\n}",
                    "language": "javascript",
                    "test_framework": "jest",
                    "include_mocks": True,
                },
            },
            {
                "title": "Tests PHP avec PHPUnit",
                "description": "Générer des tests PHPUnit pour un service",
                "request": {
                    "code": "<?php\nclass CartService {\n    public function addItem($id, $qty) {\n        // implementation\n    }\n}",
                    "language": "php",
                    "test_framework": "phpunit",
                },
            },
        ]

    def get_capabilities(self) -> List[str]:
        return [
            "Génération de tests dans 6 langages de programmation",
            "Support de 15+ frameworks de test populaires",
            "Génération de tests avec mocks et stubs automatiques",
            "Ciblage de couverture de code personnalisable (0-100%)",
            "Tests pour fonctions, classes, modules et API",
            "Détection automatique des éléments testables",
            "Génération de tests pour cas normaux, limites et erreurs",
            "Support des fonctions synchrones et asynchrones",
            "Génération de tests paramétrés",
            "Estimation de la couverture de code",
            "Intégration avec différents environnements de test",
            "Génération de setups et teardowns appropriés",
            "Support des tests d'intégration",
            "Gestion des dépendances et imports",
        ]

    def validate_request(self, request) -> bool:
        """Valide la requête de génération de tests."""
        super().validate_request(request)

        # Valider le framework si spécifié
        if hasattr(request, "test_framework") and request.test_framework:
            lang = request.language.lower()
            supported = self.get_supported_test_frameworks().get(lang, [])

            if request.test_framework.lower() not in [f.lower() for f in supported]:
                raise ToolValidationError(
                    f"Framework '{request.test_framework}' non supporté pour {lang}. "
                    f"Frameworks disponibles: {supported}"
                )

        return True

    def is_long_running(self) -> bool:
        """Indique si le tool est long à exécuter."""
        return True

    def _build_prompt(self, request: TestGenerationRequest) -> str:
        """Fallback prompt builder used when ``prompt_engine`` is unavailable.

        Delegates to :class:`TestGenerationEngine.build_prompt` so the
        offline path (unit tests, engine init error) preserves the exact
        historical prompt shape — no behavioural regression for callers
        that never had the engine wired in the first place.
        """
        framework = self._engine.detect_framework(
            request.language, request.test_framework
        )
        elements = self._engine.extract_code_elements(
            request.code, request.language
        )
        return self._engine.build_prompt(
            request.code,
            request.language,
            framework,
            request.include_mocks or False,
            request.coverage_target or 0.8,
            elements,
        )

    @staticmethod
    def _extract_test_code_block(text: str) -> str:
        """Return the body of the fenced block that actually contains tests.

        Handles three failure modes seen on Gemini 2.5/3 and Gemma 4:
        - Leading "thought channel" prose before any fence (Gemma 4 large
          variants, per Google's prompt-formatting guide).
        - A single fence wrapping tests — happy path.
        - Two (or more) fences where the model restates the source first
          and puts tests second; picking the first fence would miss the
          actual tests. We therefore pick the fence whose body contains
          ``def test_`` or ``import pytest``; if none match, fall back to
          the longest fence; if no fence, return ``text`` unchanged.
        """
        import re

        blocks = re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.DOTALL)
        if not blocks:
            return text
        test_shaped = [b for b in blocks if "def test_" in b or "import pytest" in b]
        chosen = test_shaped[0] if test_shaped else max(blocks, key=len)
        return chosen.rstrip()

    def _enrich_context_with_elements(
        self,
        request: TestGenerationRequest,
        framework: str,
        elements: List[Dict[str, Any]],
    ) -> TestGenerationRequest:
        """Fold the AST-extracted ``elements`` list and the resolved
        ``framework`` into the request's ``context`` field so the YAML
        template's ``{context}`` placeholder carries the same information
        the hardcoded prompt used to inject inline. Keeps the "MCP value
        add" of element extraction alive even after wiring to templates.
        """
        lines: List[str] = []
        if framework:
            lines.append(f"Target test framework: {framework}")
        if elements:
            lines.append("Elements to cover:")
            for e in elements[:10]:
                if e.get("type") == "function":
                    params = ", ".join(e.get("params", []))
                    lines.append(f"- function {e['name']}({params})")
                elif e.get("type") in ("class", "Class"):
                    methods = ", ".join(e.get("methods", [])[:5])
                    suffix = f" (methods: {methods})" if methods else ""
                    lines.append(f"- class {e['name']}{suffix}")
                else:
                    lines.append(f"- {e.get('type', 'item')} {e.get('name')}")
        existing = getattr(request, "context", None) or ""
        merged = (existing + "\n" + "\n".join(lines)).strip() if lines else existing
        return request.model_copy(update={"context": merged})

    def _execute_core_logic(
        self, request: TestGenerationRequest, **kwargs
    ) -> TestGenerationResponse:
        """Exécute la génération de tests (synchrone)."""
        ctx = kwargs.get("ctx")

        # Détecter le framework
        framework = self._engine.detect_framework(
            request.language, request.test_framework
        )

        # Extraire les éléments du code
        elements = self._engine.extract_code_elements(request.code, request.language)

        if ctx:
            try:
                # Préparer le prompt via le pipeline template + A/B (#233).
                enriched = self._enrich_context_with_elements(request, framework, elements)
                prompt = run_async_from_sync(
                    self.prepare_prompt(enriched, template_name="test_generation")
                )

                started = time.monotonic()
                result = run_async_from_sync(
                    ctx.sample(
                        messages=prompt,
                        temperature=0.5,
                        max_tokens=2000,
                    )
                )
                elapsed = time.monotonic() - started
                test_code = self._extract_test_code_block(result.text or "")
                self.track_last_prompt_performance(
                    execution_time=elapsed,
                    tokens_used=len(test_code) // 4,  # rough token proxy
                    success=bool(test_code),
                )

                # Compter les tests générés
                test_count = test_code.count("def test_") + test_code.count("@Test")

                # Estimer la couverture
                estimated_coverage = self._engine.estimate_coverage(
                    elements, test_count
                )

                # Générer le chemin du fichier de test
                test_file_path = self._engine.generate_test_file_path(
                    request.file_path, request.language, framework
                )

                # Convertir les éléments pour la réponse
                tested_elements = [
                    {"name": e["name"], "type": e["type"]} for e in elements
                ]

                return TestGenerationResponse(
                    test_code=test_code,
                    language=request.language,
                    framework=framework,
                    test_file_path=test_file_path,
                    estimated_coverage=estimated_coverage,
                    tested_elements=tested_elements,
                )

            except Exception as e:
                self.logger.warning(
                    f"Erreur avec ctx.sample(), utilisation du fallback: {e}"
                )
                return self._generate_fallback_response(request, framework, elements)
        else:
            return self._generate_fallback_response(request, framework, elements)

    async def _execute_core_logic_async(
        self, request: TestGenerationRequest, **kwargs
    ) -> TestGenerationResponse:
        """Version asynchrone de la génération de tests."""
        ctx = kwargs.get("ctx")

        # Détecter le framework
        framework = self._engine.detect_framework(
            request.language, request.test_framework
        )

        if ctx:
            await ctx.info(
                f"Génération de tests {framework} pour {request.language}..."
            )

        # Extraire les éléments
        elements = self._engine.extract_code_elements(request.code, request.language)

        # Préparer le prompt via le pipeline template + A/B (#233).
        enriched = self._enrich_context_with_elements(request, framework, elements)
        prompt = await self.prepare_prompt(enriched, template_name="test_generation")

        try:
            started = time.monotonic()
            result = await ctx.sample(
                messages=prompt,
                temperature=0.5,
                max_tokens=2000,
            )
            elapsed = time.monotonic() - started
            test_code = self._extract_test_code_block(result.text or "")
            self.track_last_prompt_performance(
                execution_time=elapsed,
                tokens_used=len(test_code) // 4,
                success=bool(test_code),
            )

            if ctx:
                await ctx.info("Tests générés, calcul de la couverture...")

            # Compter les tests
            test_count = test_code.count("def test_") + test_code.count("@Test")
            estimated_coverage = self._engine.estimate_coverage(elements, test_count)

            # Générer le chemin
            test_file_path = self._engine.generate_test_file_path(
                request.file_path, request.language, framework
            )

            tested_elements = [{"name": e["name"], "type": e["type"]} for e in elements]

            return TestGenerationResponse(
                test_code=test_code,
                language=request.language,
                framework=framework,
                test_file_path=test_file_path,
                estimated_coverage=estimated_coverage,
                tested_elements=tested_elements,
            )

        except Exception as e:
            self.logger.warning(f"Erreur LLM async, utilisation du fallback: {e}")
            return self._generate_fallback_response(request, framework, elements)

    def _generate_fallback_response(
        self,
        request: TestGenerationRequest,
        framework: str,
        elements: List[Dict[str, Any]],
    ) -> TestGenerationResponse:
        """Génère une réponse fallback quand le LLM n'est pas disponible."""
        test_code, test_count = self._engine.generate_fallback_tests(
            request.code, request.language, framework, elements
        )

        test_file_path = self._engine.generate_test_file_path(
            request.file_path, request.language, framework
        )

        estimated_coverage = self._engine.estimate_coverage(elements, test_count)

        tested_elements = [{"name": e["name"], "type": e["type"]} for e in elements]

        return TestGenerationResponse(
            test_code=test_code,
            language=request.language,
            framework=framework,
            test_file_path=test_file_path,
            estimated_coverage=estimated_coverage,
            tested_elements=tested_elements,
        )


def generate_tests(request: TestGenerationRequest) -> TestGenerationResponse:
    """Fonction utilitaire pour générer des tests."""
    tool = TestGenerationTool()
    return tool.execute(request)
