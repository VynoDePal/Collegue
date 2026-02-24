"""
Test Generation - Outil de génération automatique de tests unitaires.

Cet outil analyse le code source et génère des tests unitaires complets
avec différents frameworks et options de personnalisation.

Refactorisé: Le fichier original faisait 767 lignes, maintenant ~200 lignes.
"""
from typing import List, Dict, Any, Optional
import pathlib

from ..base import BaseTool, ToolError, ToolValidationError
from ...core.shared import run_async_from_sync
from .models import TestGenerationRequest, TestGenerationResponse, LLMTestGenerationResult
from .engine import TestGenerationEngine


class TestGenerationTool(BaseTool):
    """
    Outil de génération automatique de tests unitaires.
    
    Supporte 6 langages avec 15+ frameworks de test, génère des tests
    avec mocks, stubs et vise une couverture de code personnalisable.
    """
    
    __test__ = False
    
    tool_name = "test_generation"
    tool_description = "Génère automatiquement des tests unitaires pour du code source"
    tags = {"generation", "testing"}
    request_model = TestGenerationRequest
    response_model = TestGenerationResponse
    supported_languages = ["python", "javascript", "typescript", "java", "c#", "php"]
    long_running = True

    def __init__(self, config=None, app_state=None):
        super().__init__(config, app_state)
        self._engine = TestGenerationEngine(logger=self.logger)

    def get_supported_test_frameworks(self) -> Dict[str, List[str]]:
        """Retourne les frameworks de test supportés par langage."""
        return self._engine.TEST_FRAMEWORKS if hasattr(self._engine, 'TEST_FRAMEWORKS') else {
            "python": ["unittest", "pytest", "nose2"],
            "javascript": ["jest", "mocha", "jasmine", "vitest"],
            "typescript": ["jest", "mocha", "jasmine", "vitest"],
            "java": ["junit", "testng", "spock"],
            "c#": ["nunit", "xunit", "mstest"],
            "php": ["phpunit", "pest", "codeception", "behat", "phpspec", "kahlan"]
        }

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
                    "coverage_target": 0.9
                }
            },
            {
                "title": "Tests JavaScript avec Jest",
                "description": "Générer des tests Jest pour des fonctions asynchrones",
                "request": {
                    "code": "async function fetchUserData(userId) {\n    return fetch(`/api/users/${userId}`);\n}",
                    "language": "javascript",
                    "test_framework": "jest",
                    "include_mocks": True
                }
            },
            {
                "title": "Tests PHP avec PHPUnit",
                "description": "Générer des tests PHPUnit pour un service",
                "request": {
                    "code": "<?php\nclass CartService {\n    public function addItem($id, $qty) {\n        // implementation\n    }\n}",
                    "language": "php",
                    "test_framework": "phpunit"
                }
            }
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
            "Gestion des dépendances et imports"
        ]

    def validate_request(self, request) -> bool:
        """Valide la requête de génération de tests."""
        super().validate_request(request)
        
        # Valider le framework si spécifié
        if hasattr(request, 'test_framework') and request.test_framework:
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

    def _execute_core_logic(self, request: TestGenerationRequest, **kwargs) -> TestGenerationResponse:
        """Exécute la génération de tests (synchrone)."""
        ctx = kwargs.get('ctx')
        
        # Détecter le framework
        framework = self._engine.detect_framework(request.language, request.test_framework)
        
        # Extraire les éléments du code
        elements = self._engine.extract_code_elements(request.code, request.language)
        
        if ctx:
            try:
                # Construire le prompt
                prompt = self._engine.build_prompt(
                    request.code,
                    request.language,
                    framework,
                    request.include_mocks or False,
                    request.coverage_target or 0.8,
                    elements
                )
                
                system_prompt = f"""Tu es un expert en tests unitaires {request.language}.
Génère des tests complets, exécutables et bien structurés.
Utilise le framework {framework}.
Vise une couverture de {request.coverage_target or 0.8:.0%}."""
                
                result = run_async_from_sync(ctx.sample(
                    messages=prompt,
                    system_prompt=system_prompt,
                    temperature=0.5,
                    max_tokens=2000
                ))
                
                test_code = result.text
                
                # Compter les tests générés
                test_count = test_code.count('def test_') + test_code.count('@Test')
                
                # Estimer la couverture
                estimated_coverage = self._engine.estimate_coverage(elements, test_count)
                
                # Générer le chemin du fichier de test
                test_file_path = self._engine.generate_test_file_path(
                    request.file_path, request.language, framework
                )
                
                # Convertir les éléments pour la réponse
                tested_elements = [
                    {'name': e['name'], 'type': e['type']}
                    for e in elements
                ]
                
                return TestGenerationResponse(
                    test_code=test_code,
                    language=request.language,
                    framework=framework,
                    test_file_path=test_file_path,
                    estimated_coverage=estimated_coverage,
                    tested_elements=tested_elements
                )
            
            except Exception as e:
                self.logger.warning(f"Erreur avec ctx.sample(), utilisation du fallback: {e}")
                return self._generate_fallback_response(request, framework, elements)
        else:
            return self._generate_fallback_response(request, framework, elements)

    async def _execute_core_logic_async(self, request: TestGenerationRequest, **kwargs) -> TestGenerationResponse:
        """Version asynchrone de la génération de tests."""
        ctx = kwargs.get('ctx')
        
        # Détecter le framework
        framework = self._engine.detect_framework(request.language, request.test_framework)
        
        if ctx:
            await ctx.info(f"Génération de tests {framework} pour {request.language}...")
        
        # Extraire les éléments
        elements = self._engine.extract_code_elements(request.code, request.language)
        
        prompt = self._engine.build_prompt(
            request.code,
            request.language,
            framework,
            request.include_mocks or False,
            request.coverage_target or 0.8,
            elements
        )
        
        system_prompt = f"""Tu es un expert en tests unitaires {request.language}.
Génère des tests complets, exécutables et bien structurés.
Utilise le framework {framework}."""
        
        try:
            result = await ctx.sample(
                messages=prompt,
                system_prompt=system_prompt,
                temperature=0.5,
                max_tokens=2000
            )
            test_code = result.text
            
            if ctx:
                await ctx.info("Tests générés, calcul de la couverture...")
            
            # Compter les tests
            test_count = test_code.count('def test_') + test_code.count('@Test')
            estimated_coverage = self._engine.estimate_coverage(elements, test_count)
            
            # Générer le chemin
            test_file_path = self._engine.generate_test_file_path(
                request.file_path, request.language, framework
            )
            
            tested_elements = [
                {'name': e['name'], 'type': e['type']}
                for e in elements
            ]
            
            return TestGenerationResponse(
                test_code=test_code,
                language=request.language,
                framework=framework,
                test_file_path=test_file_path,
                estimated_coverage=estimated_coverage,
                tested_elements=tested_elements
            )
        
        except Exception as e:
            self.logger.warning(f"Erreur LLM async, utilisation du fallback: {e}")
            return self._generate_fallback_response(request, framework, elements)

    def _generate_fallback_response(self, request: TestGenerationRequest,
                                   framework: str, elements: List[Dict[str, Any]]) -> TestGenerationResponse:
        """Génère une réponse fallback quand le LLM n'est pas disponible."""
        test_code, test_count = self._engine.generate_fallback_tests(
            request.code, request.language, framework, elements
        )
        
        test_file_path = self._engine.generate_test_file_path(
            request.file_path, request.language, framework
        )
        
        estimated_coverage = self._engine.estimate_coverage(elements, test_count)
        
        tested_elements = [
            {'name': e['name'], 'type': e['type']}
            for e in elements
        ]
        
        return TestGenerationResponse(
            test_code=test_code,
            language=request.language,
            framework=framework,
            test_file_path=test_file_path,
            estimated_coverage=estimated_coverage,
            tested_elements=tested_elements
        )


def generate_tests(request: TestGenerationRequest) -> TestGenerationResponse:
    """Fonction utilitaire pour générer des tests."""
    tool = TestGenerationTool()
    return tool.execute(request)
