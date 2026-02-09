"""
Test Generation - Outil de génération automatique de tests unitaires

Intègre optionnellement run_tests pour valider les tests générés.
"""
import asyncio
import tempfile
import shutil
from typing import Optional, Dict, Any, List, Type
from pydantic import BaseModel, Field, validator, field_validator
import os
import pathlib
from .base import BaseTool, ToolError, ToolValidationError, ToolExecutionError
from ._run_tests import RunTestsTool, RunTestsRequest


class TestGenerationRequest(BaseModel):
    """Modèle de requête pour la génération de tests unitaires."""
    code: str = Field(..., description="Code à tester", min_length=1)
    language: str = Field(..., description="Langage de programmation du code")
    session_id: Optional[str] = Field(None, description="Identifiant de session")
    test_framework: Optional[str] = Field(None, description="Framework de test à utiliser (unittest, pytest, jest, etc.)")
    file_path: Optional[str] = Field(None, description="Chemin du fichier contenant le code")
    output_dir: Optional[str] = Field(None, description="Répertoire de sortie pour les tests générés")
    include_mocks: Optional[bool] = Field(False, description="Inclure des mocks dans les tests")
    coverage_target: Optional[float] = Field(0.8, description="Couverture de code cible (0.0-1.0)", ge=0.0, le=1.0)
    validate_tests: Optional[bool] = Field(False, description="Valider les tests générés en les exécutant avec run_tests")
    working_dir: Optional[str] = Field(None, description="Répertoire de travail pour la validation (défaut: répertoire temporaire)")

    @field_validator('language')
    def validate_language_field(cls, v):
        """Valide que le langage est fourni et non vide."""
        if not v or not v.strip():
            raise ValueError("Le langage ne peut pas être vide")
        return v.strip().lower()

    @field_validator('coverage_target')
    def validate_coverage_target(cls, v):
        """Valide que la cible de couverture est dans la plage valide."""
        if v is not None and (v < 0.0 or v > 1.0):
            raise ValueError("La cible de couverture doit être entre 0.0 et 1.0")
        return v


class TestValidationResult(BaseModel):
    """Résultat de la validation des tests générés."""
    validated: bool = Field(..., description="True si les tests ont été validés")
    success: bool = Field(..., description="True si tous les tests passent")
    total: int = Field(0, description="Nombre total de tests")
    passed: int = Field(0, description="Nombre de tests passés")
    failed: int = Field(0, description="Nombre de tests échoués")
    errors: int = Field(0, description="Nombre d'erreurs")
    error_message: Optional[str] = Field(None, description="Message d'erreur si la validation a échoué")
    duration: float = Field(0.0, description="Durée d'exécution en secondes")


class TestGenerationResponse(BaseModel):
    """Modèle de réponse pour la génération de tests unitaires."""
    test_code: str = Field(..., description="Code de test généré")
    language: str = Field(..., description="Langage du code de test")
    framework: str = Field(..., description="Framework de test utilisé")
    test_file_path: Optional[str] = Field(None, description="Chemin du fichier de test généré")
    estimated_coverage: float = Field(..., description="Estimation de la couverture de code")
    tested_elements: List[Dict[str, str]] = Field(..., description="Éléments testés (fonctions, classes, etc.)")
    validation_result: Optional[TestValidationResult] = Field(None, description="Résultat de validation si validate_tests=True")


class LLMTestGenerationResult(BaseModel):
    """
    Modèle de sortie structurée pour ctx.sample() avec result_type (FastMCP 2.14.1+).
    
    Ce modèle force le LLM à produire une réponse JSON validée par Pydantic,
    garantissant que tous les champs requis sont présents et correctement typés.
    """
    test_code: str = Field(..., description="Code de test complet et exécutable")
    test_count: int = Field(..., description="Nombre de tests générés")
    coverage_estimate: float = Field(
        default=0.8,
        description="Estimation de la couverture de code (0.0 à 1.0)",
        ge=0.0,
        le=1.0
    )
    tested_functions: List[str] = Field(
        default_factory=list,
        description="Liste des noms de fonctions testées"
    )
    tested_classes: List[str] = Field(
        default_factory=list,
        description="Liste des noms de classes testées"
    )
    imports_required: List[str] = Field(
        default_factory=list,
        description="Imports nécessaires pour les tests"
    )


class TestGenerationTool(BaseTool):
    """Outil de génération automatique de tests unitaires."""

    def get_name(self) -> str:
        return "test_generation"

    def get_description(self) -> str:
        return "Génère automatiquement des tests unitaires pour du code source"

    def get_request_model(self) -> Type[BaseModel]:
        return TestGenerationRequest

    def get_response_model(self) -> Type[BaseModel]:
        return TestGenerationResponse

    def get_supported_languages(self) -> List[str]:
        return ["python", "javascript", "typescript", "java", "c#"]

    def is_long_running(self) -> bool:
        """Cet outil génère des tests complets via LLM et peut prendre du temps."""
        return True

    def get_supported_test_frameworks(self) -> Dict[str, List[str]]:
        return {
            "python": ["unittest", "pytest", "nose2"],
            "javascript": ["jest", "mocha", "jasmine", "vitest"],
            "typescript": ["jest", "mocha", "jasmine", "vitest"],
            "java": ["junit", "testng", "spock"],
            "c#": ["nunit", "xunit", "mstest"]
        }

    def get_usage_description(self) -> str:
        return ("Outil de génération automatique de tests unitaires qui analyse le code source et génère des tests "
                "complets avec différents frameworks. Il peut inclure des mocks, viser une couverture spécifique, "
                "et génère des tests pour fonctions, classes et modules avec estimation de couverture.")

    def get_examples(self) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Tests Python avec pytest",
                "description": "Générer des tests pytest pour une classe Python",
                "request": {
                    "code": "class Calculator:\n    def __init__(self):\n        self.result = 0\n    \n    def add(self, a, b):\n        return a + b\n    \n    def divide(self, a, b):\n        if b == 0:\n            raise ValueError('Division by zero')\n        return a / b",
                    "language": "python",
                    "test_framework": "pytest",
                    "include_mocks": False,
                    "coverage_target": 0.9
                },
                "expected_response": "Tests pytest complets avec cas normaux et cas d'erreur"
            },
            {
                "title": "Tests JavaScript avec Jest",
                "description": "Générer des tests Jest pour des fonctions asynchrones",
                "request": {
                    "code": "async function fetchUserData(userId) {\n    const response = await fetch(`/api/users/${userId}`);\n    if (!response.ok) {\n        throw new Error('User not found');\n    }\n    return response.json();\n}\n\nfunction validateEmail(email) {\n    return /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(email);\n}",
                    "language": "javascript",
                    "test_framework": "jest",
                    "include_mocks": True,
                    "coverage_target": 0.85
                },
                "expected_response": "Tests Jest avec mocks pour fetch et tests de validation"
            },
            {
                "title": "Tests TypeScript avec types",
                "description": "Générer des tests TypeScript avec gestion des types",
                "request": {
                    "code": "interface User {\n    id: number;\n    name: string;\n    email: string;\n}\n\nclass UserService {\n    private users: User[] = [];\n    \n    addUser(user: User): void {\n        if (!user.name || !user.email) {\n            throw new Error('Invalid user data');\n        }\n        this.users.push(user);\n    }\n    \n    findUser(id: number): User | null {\n        return this.users.find(u => u.id === id) || null;\n    }\n}",
                    "language": "typescript",
                    "test_framework": "jest",
                    "include_mocks": False,
                    "coverage_target": 0.8
                },
                "expected_response": "Tests TypeScript avec types et gestion des cas d'erreur"
            },
            {
                "title": "Tests Java avec JUnit",
                "description": "Générer des tests JUnit pour une classe Java",
                "request": {
                    "code": "public class StringUtils {\n    public static boolean isEmpty(String str) {\n        return str == null || str.trim().isEmpty();\n    }\n    \n    public static String capitalize(String str) {\n        if (isEmpty(str)) {\n            return str;\n        }\n        return str.substring(0, 1).toUpperCase() + str.substring(1).toLowerCase();\n    }\n}",
                    "language": "java",
                    "test_framework": "junit",
                    "include_mocks": False,
                    "coverage_target": 0.95
                },
                "expected_response": "Tests JUnit complets avec cas limites et assertions"
            },
            {
                "title": "Tests avec mocks avancés",
                "description": "Générer des tests avec mocks complexes pour une classe de service",
                "request": {
                    "code": "class EmailService:\n    def __init__(self, smtp_client):\n        self.smtp_client = smtp_client\n    \n    def send_email(self, to, subject, body):\n        if not self._validate_email(to):\n            raise ValueError('Invalid email address')\n        \n        return self.smtp_client.send({\n            'to': to,\n            'subject': subject,\n            'body': body\n        })\n    \n    def _validate_email(self, email):\n        return '@' in email and '.' in email",
                    "language": "python",
                    "test_framework": "unittest",
                    "include_mocks": True,
                    "coverage_target": 0.9
                },
                "expected_response": "Tests unittest avec mocks pour smtp_client et validation"
            },
            {
                "title": "Tests avec couverture maximale",
                "description": "Générer des tests visant une couverture très élevée",
                "request": {
                    "code": "def process_data(data):\n    if not data:\n        return None\n    \n    result = []\n    for item in data:\n        if isinstance(item, str):\n            result.append(item.upper())\n        elif isinstance(item, int):\n            result.append(item * 2)\n        else:\n            result.append(str(item))\n    \n    return result",
                    "language": "python",
                    "test_framework": "pytest",
                    "include_mocks": False,
                    "coverage_target": 1.0
                },
                "expected_response": "Tests pytest exhaustifs couvrant tous les cas et branches"
            }
        ]

    def get_capabilities(self) -> List[str]:
        """Capacités spécifiques de l'outil de génération de tests."""
        return [
            "Génération de tests dans 5+ langages de programmation",
            "Support de 15+ frameworks de test populaires",
            "Génération de tests avec mocks et stubs automatiques",
            "Ciblage de couverture de code personnalisable (0-100%)",
            "Tests pour fonctions, classes, modules et API",
            "Génération de cas de test normaux et cas limites",
            "Tests d'erreurs et de gestion d'exceptions",
            "Estimation de couverture de code réelle",
            "Tests asynchrones pour JavaScript/TypeScript",
            "Tests avec injection de dépendances",
            "Génération de données de test appropriées",
            "Tests de performance basiques",
            "Tests d'intégration pour services",
            "Support des annotations et décorateurs",
            "Génération de setup et teardown automatiques",
            "Tests avec assertions appropriées par framework"
        ]

    def get_framework_descriptions(self) -> Dict[str, Dict[str, str]]:
        """Descriptions des frameworks de test par langage."""
        return {
            "python": {
                "unittest": "Framework de test standard Python, intégré à la bibliothèque standard",
                "pytest": "Framework de test avancé avec syntaxe simple et plugins étendus",
                "nose2": "Successeur de nose, extension d'unittest avec fonctionnalités avancées"
            },
            "javascript": {
                "jest": "Framework de test complet avec mocks, snapshots et couverture intégrée",
                "mocha": "Framework de test flexible avec support de différents styles d'assertions",
                "jasmine": "Framework de test behavior-driven avec syntaxe expressive",
                "vitest": "Framework de test rapide optimisé pour Vite et projets modernes"
            },
            "typescript": {
                "jest": "Framework de test avec support TypeScript natif et types",
                "mocha": "Framework flexible avec support TypeScript via ts-node",
                "jasmine": "Framework BDD avec support TypeScript et types",
                "vitest": "Framework moderne avec support TypeScript excellent"
            },
            "java": {
                "junit": "Framework de test standard Java avec annotations et assertions",
                "testng": "Framework de test avancé avec groupes et tests paramétrés",
                "spock": "Framework de test pour Groovy/Java avec syntaxe expressive"
            },
            "c#": {
                "nunit": "Framework de test .NET avec attributs et contraintes",
                "xunit": "Framework de test moderne pour .NET avec isolation des tests",
                "mstest": "Framework de test Microsoft intégré à Visual Studio"
            }
        }

    def get_required_config_keys(self) -> List[str]:
        """Retourne les clés de configuration requises."""
        return []

    def _validate_generated_tests(
        self, 
        test_code: str, 
        language: str, 
        framework: str,
        source_code: str,
        working_dir: Optional[str] = None
    ) -> TestValidationResult:
        """
        Valide les tests générés en les exécutant avec run_tests.
        
        Args:
            test_code: Code des tests générés
            language: Langage de programmation
            framework: Framework de test
            source_code: Code source original à tester
            working_dir: Répertoire de travail (optionnel)
        
        Returns:
            TestValidationResult: Résultat de la validation
        """
        temp_dir = None
        try:
            temp_dir = tempfile.mkdtemp(prefix="collegue_test_validation_")
            
            if language == "python":
                source_ext = ".py"
                test_prefix = "test_"
                source_filename = "module_under_test.py"
                test_filename = "test_module_under_test.py"
            elif language in ["javascript", "typescript"]:
                source_ext = ".ts" if language == "typescript" else ".js"
                test_prefix = ""
                source_filename = f"module_under_test{source_ext}"
                test_filename = f"module_under_test.test{source_ext}"
            else:
                return TestValidationResult(
                    validated=False,
                    success=False,
                    error_message=f"Validation non supportée pour le langage: {language}"
                )
            
            source_path = os.path.join(temp_dir, source_filename)
            with open(source_path, 'w', encoding='utf-8') as f:
                f.write(source_code)
            
            test_path = os.path.join(temp_dir, test_filename)
            
            adapted_test_code = test_code
            if language == "python":
                adapted_test_code = f"import sys\nsys.path.insert(0, '{temp_dir}')\n" + test_code
                adapted_test_code = adapted_test_code.replace(
                    "from module_test import", 
                    "from module_under_test import"
                )
            
            with open(test_path, 'w', encoding='utf-8') as f:
                f.write(adapted_test_code)
            
            self.logger.info(f"Tests écrits dans: {test_path}")
            
            run_tests_tool = RunTestsTool()
            run_request = RunTestsRequest(
                target=test_filename,
                language=language,
                framework=framework,
                working_dir=temp_dir,
                timeout=60,
                verbose=False
            )
            
            run_response = run_tests_tool._execute_core_logic(run_request)
            
            return TestValidationResult(
                validated=True,
                success=run_response.success,
                total=run_response.total,
                passed=run_response.passed,
                failed=run_response.failed,
                errors=run_response.errors,
                duration=run_response.duration,
                error_message=run_response.stderr if not run_response.success else None
            )
            
        except Exception as e:
            self.logger.error(f"Erreur lors de la validation des tests: {e}")
            return TestValidationResult(
                validated=False,
                success=False,
                error_message=str(e)
            )
        finally:
            if temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                except Exception as e:
                    self.logger.warning(f"Impossible de supprimer le répertoire temporaire: {e}")

    def _execute_core_logic(self, request: TestGenerationRequest, **kwargs) -> TestGenerationResponse:
        """
        Exécute la logique principale de génération de tests.

        Args:
            request: Requête de génération de tests validée
            **kwargs: Services additionnels (llm_manager, parser, etc.)

        Returns:
            TestGenerationResponse: Les tests générés
        """
        llm_manager = kwargs.get('llm_manager')
        parser = kwargs.get('parser')

        self.validate_language(request.language)

        if llm_manager is not None:
            try:
                framework = request.test_framework or self._get_default_test_framework(request.language)
                context = {
                    "code": request.code,
                    "language": request.language,
                    "test_framework": framework,
                    "include_mocks": str(request.include_mocks),
                    "coverage_target": str(int(request.coverage_target * 100)) + "%" if request.coverage_target else "80%",
                    "file_path": request.file_path or "unknown"
                }
                
                try:
                    if asyncio.iscoroutinefunction(self.prepare_prompt):
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            import concurrent.futures
                            with concurrent.futures.ThreadPoolExecutor() as executor:
                                future = executor.submit(asyncio.run, self.prepare_prompt(request, context=context))
                                prompt = future.result()
                        else:
                            prompt = loop.run_until_complete(self.prepare_prompt(request, context=context))
                    else:
                        prompt = self.prepare_prompt(request, context=context)
                except Exception as e:
                    self.logger.debug(f"Fallback vers _build_test_generation_prompt: {e}")
                    prompt = self._build_test_generation_prompt(request)
                
                generated_tests = llm_manager.sync_generate(prompt)

                framework = request.test_framework or self._get_default_test_framework(request.language)
                tested_elements = self._extract_tested_elements(request.code, request.language, parser)
                estimated_coverage = self._estimate_coverage(tested_elements, request.code, request.coverage_target)
                test_file_path = None
                if request.file_path and request.output_dir:
                    test_file_path = self._generate_test_file_path(
                        request.file_path, request.output_dir, framework
                    )

                validation_result = None
                if request.validate_tests:
                    self.logger.info("Validation des tests générés avec run_tests...")
                    validation_result = self._validate_generated_tests(
                        test_code=generated_tests,
                        language=request.language,
                        framework=framework,
                        source_code=request.code,
                        working_dir=request.working_dir
                    )
                
                return TestGenerationResponse(
                    test_code=generated_tests,
                    language=request.language,
                    framework=framework,
                    test_file_path=test_file_path,
                    estimated_coverage=estimated_coverage,
                    tested_elements=tested_elements,
                    validation_result=validation_result
                )
            except Exception as e:
                self.logger.warning(f"Erreur avec LLM, utilisation du fallback: {e}")
                return self._generate_fallback_tests(request, parser)
        else:
            return self._generate_fallback_tests(request, parser)

    async def _execute_core_logic_async(self, request: TestGenerationRequest, **kwargs) -> TestGenerationResponse:
        """
        Version async de la logique de génération de tests (FastMCP 2.14+).
        
        Utilise ctx.sample() pour les appels LLM avec support structured output (result_type),
        avec fallback vers ToolLLMManager si ctx non disponible.
        
        Args:
            request: Requête de génération de tests validée
            **kwargs: Services additionnels incluant ctx, llm_manager, parser
        
        Returns:
            TestGenerationResponse: Les tests générés
        """
        ctx = kwargs.get('ctx')
        llm_manager = kwargs.get('llm_manager')
        parser = kwargs.get('parser')
        use_structured_output = kwargs.get('use_structured_output', True)
        
        self.validate_language(request.language)
        
        framework = request.test_framework or self._get_default_test_framework(request.language)
        
        prompt = self._build_test_generation_prompt(request)
        system_prompt = f"""Tu es un expert en tests unitaires {request.language} avec {framework}.
Génère des tests complets, bien structurés et couvrant les cas limites."""
        
        if ctx:
            await ctx.info("Génération des tests via LLM...")
        
        try:
            if ctx is not None and use_structured_output:
                try:
                    self.logger.debug("Utilisation du structured output avec LLMTestGenerationResult")
                    llm_result = await self.sample_llm(
                        prompt=prompt,
                        ctx=ctx,
                        llm_manager=llm_manager,
                        system_prompt=system_prompt,
                        result_type=LLMTestGenerationResult,
                        temperature=0.5
                    )
                    
                    if isinstance(llm_result, LLMTestGenerationResult):
                        generated_tests = llm_result.test_code
                        tested_elements = []
                        for func in llm_result.tested_functions:
                            tested_elements.append({"type": "function", "name": func})
                        for cls in llm_result.tested_classes:
                            tested_elements.append({"type": "class", "name": cls})
                        
                        if ctx:
                            await ctx.info(f"Structured output: {llm_result.test_count} tests générés")
                        
                        test_file_path = None
                        if request.file_path and request.output_dir:
                            test_file_path = self._generate_test_file_path(
                                request.file_path, request.output_dir, framework
                            )
                        
                        validation_result = None
                        if request.validate_tests:
                            if ctx:
                                await ctx.info("Validation des tests générés...")
                            validation_result = self._validate_generated_tests(
                                test_code=generated_tests,
                                language=request.language,
                                framework=framework,
                                source_code=request.code,
                                working_dir=request.working_dir
                            )
                        
                        return TestGenerationResponse(
                            test_code=generated_tests,
                            language=request.language,
                            framework=framework,
                            test_file_path=test_file_path,
                            estimated_coverage=llm_result.coverage_estimate,
                            tested_elements=tested_elements,
                            validation_result=validation_result
                        )
                except Exception as e:
                    self.logger.warning(f"Structured output a échoué, fallback vers texte brut: {e}")
            
            generated_tests = await self.sample_llm(
                prompt=prompt,
                ctx=ctx,
                llm_manager=llm_manager,
                system_prompt=system_prompt + "\nRéponds UNIQUEMENT avec le code de test, sans explications.",
                temperature=0.5
            )
            
            if ctx:
                await ctx.info("Tests générés, analyse...")
            
            tested_elements = self._extract_tested_elements(request.code, request.language, parser)
            
            estimated_coverage = self._estimate_coverage(tested_elements, request.code, request.coverage_target)
            
            test_file_path = None
            if request.file_path and request.output_dir:
                test_file_path = self._generate_test_file_path(
                    request.file_path, request.output_dir, framework
                )
            
            validation_result = None
            if request.validate_tests:
                if ctx:
                    await ctx.info("Validation des tests générés...")
                validation_result = self._validate_generated_tests(
                    test_code=generated_tests,
                    language=request.language,
                    framework=framework,
                    source_code=request.code,
                    working_dir=request.working_dir
                )
            
            return TestGenerationResponse(
                test_code=generated_tests,
                language=request.language,
                framework=framework,
                test_file_path=test_file_path,
                estimated_coverage=estimated_coverage,
                tested_elements=tested_elements,
                validation_result=validation_result
            )
            
        except Exception as e:
            self.logger.warning(f"Erreur LLM async, utilisation du fallback: {e}")
            return self._generate_fallback_tests(request, parser)

    def _build_test_generation_prompt(self, request: TestGenerationRequest) -> str:
        """
        Construit le prompt optimisé pour le LLM.

        Args:
            request: Requête de génération de tests

        Returns:
            Prompt optimisé pour le LLM
        """
        framework = request.test_framework or self._get_default_test_framework(request.language)

        prompt_parts = [
            f"Génère des tests unitaires complets pour le code {request.language} suivant :",
            f"Code à tester:\n{request.code}",
            f"Framework de test à utiliser: {framework}"
        ]

        if request.include_mocks:
            prompt_parts.append("Inclure des mocks pour les dépendances externes.")

        if request.coverage_target:
            coverage_percent = int(request.coverage_target * 100)
            prompt_parts.append(f"Viser une couverture de code d'au moins {coverage_percent}%.")

        language_instructions = self._get_test_framework_instructions(request.language, framework)
        if language_instructions:
            prompt_parts.append(f"Instructions spécifiques: {language_instructions}")

        prompt_parts.extend([
            "Génère des tests qui couvrent :",
            "- Les cas nominaux (entrées valides)",
            "- Les cas d'erreur (entrées invalides)",
            "- Les cas limites (valeurs extrêmes)",
            "Fournis uniquement le code de test sans explications supplémentaires."
        ])

        return "\n\n".join(prompt_parts)

    def _get_default_test_framework(self, language: str) -> str:
        """Retourne le framework de test par défaut pour un langage donné."""
        framework_map = {
            "python": "pytest",
            "javascript": "jest",
            "typescript": "jest",
            "java": "junit",
            "c#": "nunit"
        }
        return framework_map.get(language.lower(), "generic")

    def _get_available_test_frameworks(self, language: str) -> List[str]:
        """Retourne la liste des frameworks de test disponibles pour un langage donné."""
        frameworks_map = {
            "python": ["unittest", "pytest"],
            "javascript": ["jest", "mocha", "jasmine"],
            "typescript": ["jest", "mocha", "jasmine"],
            "java": ["junit", "testng"],
            "c#": ["nunit", "xunit", "mstest"]
        }
        return frameworks_map.get(language.lower(), ["generic"])

    def _get_test_framework_instructions(self, language: str, framework: str) -> str:
        """
        Retourne les instructions spécifiques pour un framework de test.

        Args:
            language: Langage de programmation
            framework: Framework de test

        Returns:
            Instructions spécifiques au framework
        """
        instructions_map = {
            ("python", "pytest"): "Utilise les fixtures pytest et assert statements. Préfixe les fonctions de test par 'test_'.",
            ("python", "unittest"): "Hérite de unittest.TestCase. Utilise les méthodes assert* de la classe.",
            ("javascript", "jest"): "Utilise describe() et test(). Utilise expect() pour les assertions.",
            ("typescript", "jest"): "Utilise describe() et test() avec typage TypeScript. Utilise expect() pour les assertions.",
            ("java", "junit"): "Utilise les annotations @Test. Utilise les méthodes assert* de JUnit.",
            ("c#", "nunit"): "Utilise les annotations [Test] et [TestCase]. Utilise Assert.* pour les assertions."
        }
        return instructions_map.get((language.lower(), framework.lower()), "")

    def _generate_python_tests(self, code: str, test_framework: str, parsed_elements: List[Dict[str, Any]], include_mocks: bool) -> str:
        """
        Génère des tests unitaires pour du code Python.

        Args:
            code: Le code Python à tester
            test_framework: Le framework de test à utiliser
            parsed_elements: Les éléments parsés du code
            include_mocks: Indique si des mocks doivent être inclus

        Returns:
            str: Le code de test généré
        """
        functions = []
        classes = []

        if parsed_elements:
            for element in parsed_elements:
                if element.get("type") == "function":
                    functions.append(element)
                elif element.get("type") == "class":
                    classes.append(element)
        else:
            lines = code.split("\n")
            for i, line in enumerate(lines):
                line = line.strip()
                if line.startswith("def "):
                    func_name = line.split("def ")[1].split("(")[0].strip()
                    functions.append({
                        "type": "function",
                        "name": func_name,
                        "line": i + 1,
                        "params": line.split("(")[1].split(")")[0] if "(" in line and ")" in line else ""
                    })
                elif line.startswith("class "):
                    class_name = line.split("class ")[1].split(":")[0].strip()
                    classes.append({
                        "type": "class",
                        "name": class_name,
                        "line": i + 1
                    })

        if test_framework == "unittest":
            return self._generate_unittest_tests(code, functions, classes, include_mocks)
        elif test_framework == "pytest":
            return self._generate_pytest_tests(code, functions, classes, include_mocks)
        else:
            return self._generate_unittest_tests(code, functions, classes, include_mocks)

    def _generate_unittest_tests(self, code: str, functions: List[Dict[str, Any]], classes: List[Dict[str, Any]], include_mocks: bool) -> str:
        """Génère des tests unitaires avec le framework unittest."""
        module_name = "module_test"

        test_code = """\"\"\"
Tests unitaires générés automatiquement avec unittest
\"\"\"
import unittest
from unittest.mock import MagicMock, patch
"""

        test_code += f"from {module_name} import "

        elements_to_import = []
        for func in functions:
            elements_to_import.append(func["name"])
        for cls in classes:
            elements_to_import.append(cls["name"])

        if elements_to_import:
            test_code += f", {', '.join(elements_to_import)}\n\n"
        else:
            test_code += "*\n\n"

        if functions:
            test_code += "class TestFunctions(unittest.TestCase):\n"
            test_code += "    \"\"\"Tests pour les fonctions.\"\"\"\n\n"

            for func in functions:
                func_name = func["name"]
                params = func.get("params", "")

                test_code += f"    def test_{func_name}(self):\n"
                test_code += f"        \"\"\"Test pour la fonction {func_name}.\"\"\"\n"

                test_values = []
                for param in params.split(","):
                    param = param.strip()
                    if not param:
                        continue

                    if "str" in param or "name" in param or "text" in param:
                        test_values.append('"test"')
                    elif "int" in param or "num" in param or "count" in param:
                        test_values.append("42")
                    elif "float" in param or "price" in param or "amount" in param:
                        test_values.append("3.14")
                    elif "bool" in param or "flag" in param or "is_" in param:
                        test_values.append("True")
                    elif "list" in param or "array" in param:
                        test_values.append("[1, 2, 3]")
                    elif "dict" in param or "map" in param:
                        test_values.append('{"key": "value"}')
                    else:
                        test_values.append("None")

                if include_mocks and params:
                    test_code += "        # Créer des mocks pour les dépendances\n"
                    test_code += "        mock_dependency = MagicMock()\n\n"

                test_code += "        # Exécuter la fonction à tester\n"
                test_code += f"        result = {func_name}({', '.join(test_values)})\n\n"

                test_code += "        # Vérifier les résultats\n"
                test_code += "        self.assertIsNotNone(result)\n\n"

            test_code += "\n"

        for cls in classes:
            class_name = cls["name"]

            test_code += f"class Test{class_name}(unittest.TestCase):\n"
            test_code += f"    \"\"\"Tests pour la classe {class_name}.\"\"\"\n\n"

            test_code += "    def setUp(self):\n"
            test_code += "        \"\"\"Initialisation des tests.\"\"\"\n"
            test_code += f"        self.instance = {class_name}()\n\n"

            test_code += "    def test_initialization(self):\n"
            test_code += f"        \"\"\"Test de l'initialisation de {class_name}.\"\"\"\n"
            test_code += f"        self.assertIsInstance(self.instance, {class_name})\n\n"

            test_code += "    def test_methods(self):\n"
            test_code += f"        \"\"\"Test des méthodes de {class_name}.\"\"\"\n"
            test_code += "        # Ajouter des tests pour les méthodes spécifiques\n"
            test_code += "        pass\n\n"

        test_code += "if __name__ == '__main__':\n"
        test_code += "    unittest.main()"

        return test_code

    def _generate_pytest_tests(self, code: str, functions: List[Dict[str, Any]], classes: List[Dict[str, Any]], include_mocks: bool) -> str:
        """Génère des tests unitaires avec le framework pytest."""
        module_name = "module_test"

        test_code = """\"\"\"
Tests unitaires générés automatiquement avec pytest
\"\"\"
import pytest
from unittest.mock import MagicMock, patch
"""

        test_code += f"from {module_name} import "

        elements_to_import = []
        for func in functions:
            elements_to_import.append(func["name"])
        for cls in classes:
            elements_to_import.append(cls["name"])

        if elements_to_import:
            test_code += f", {', '.join(elements_to_import)}\n\n"
        else:
            test_code += "*\n\n"

        if classes:
            test_code += "@pytest.fixture\n"
            for cls in classes:
                class_name = cls["name"]
                test_code += f"def {class_name.lower()}_instance():\n"
                test_code += f"    \"\"\"Fixture pour créer une instance de {class_name}.\"\"\"\n"
                test_code += f"    return {class_name}()\n\n"

        for func in functions:
            func_name = func["name"]
            params = func.get("params", "")

            test_code += f"def test_{func_name}():\n"
            test_code += f"    \"\"\"Test pour la fonction {func_name}.\"\"\"\n"

            test_values = []
            for param in params.split(","):
                param = param.strip()
                if not param:
                    continue

                if "str" in param or "name" in param or "text" in param:
                    test_values.append('"test"')
                elif "int" in param or "num" in param or "count" in param:
                    test_values.append("42")
                elif "float" in param or "price" in param or "amount" in param:
                    test_values.append("3.14")
                elif "bool" in param or "flag" in param or "is_" in param:
                    test_values.append("True")
                elif "list" in param or "array" in param:
                    test_values.append("[1, 2, 3]")
                elif "dict" in param or "map" in param:
                    test_values.append('{"key": "value"}')
                else:
                    test_values.append("None")

            if include_mocks and params:
                test_code += "    # Créer des mocks pour les dépendances\n"
                test_code += "    mock_dependency = MagicMock()\n\n"

            test_code += "    # Exécuter la fonction à tester\n"
            test_code += f"    result = {func_name}({', '.join(test_values)})\n\n"

            test_code += "    # Vérifier les résultats\n"
            test_code += "    assert result is not None\n\n"

        for cls in classes:
            class_name = cls["name"]

            test_code += f"def test_{class_name.lower()}_initialization({class_name.lower()}_instance):\n"
            test_code += f"    \"\"\"Test de l'initialisation de {class_name}.\"\"\"\n"
            test_code += f"    assert isinstance({class_name.lower()}_instance, {class_name})\n\n"

            test_code += f"def test_{class_name.lower()}_methods({class_name.lower()}_instance):\n"
            test_code += f"    \"\"\"Test des méthodes de {class_name}.\"\"\"\n"
            test_code += "    # Ajouter des tests pour les méthodes spécifiques\n"
            test_code += "    pass\n\n"

        return test_code

    def _generate_javascript_tests(self, code: str, test_framework: str, parsed_elements: List[Dict[str, Any]], include_mocks: bool) -> str:
        """
        Génère des tests unitaires pour du code JavaScript.

        Args:
            code: Le code JavaScript à tester
            test_framework: Le framework de test à utiliser
            parsed_elements: Les éléments parsés du code
            include_mocks: Indique si des mocks doivent être inclus

        Returns:
            str: Le code de test généré
        """
        functions = []
        classes = []

        if parsed_elements:
            for element in parsed_elements:
                if element.get("type") == "function":
                    functions.append(element)
                elif element.get("type") == "class":
                    classes.append(element)
        else:
            lines = code.split("\n")
            for i, line in enumerate(lines):
                line = line.strip()
                if "function " in line:
                    func_name = line.split("function ")[1].split("(")[0].strip()
                    if func_name:
                        functions.append({
                            "type": "function",
                            "name": func_name,
                            "line": i + 1,
                            "params": line.split("(")[1].split(")")[0] if "(" in line and ")" in line else ""
                        })
                elif "class " in line:
                    class_name = line.split("class ")[1].split(" ")[0].split("{")[0].strip()
                    classes.append({
                        "type": "class",
                        "name": class_name,
                        "line": i + 1
                    })

        if test_framework == "jest":
            return self._generate_jest_tests(code, functions, classes, include_mocks)
        elif test_framework == "mocha":
            return self._generate_mocha_tests(code, functions, classes, include_mocks)
        else:
            return self._generate_jest_tests(code, functions, classes, include_mocks)

    def _generate_typescript_tests(self, code: str, test_framework: str, parsed_elements: List[Dict[str, Any]], include_mocks: bool) -> str:
        """
        Génère des tests unitaires pour du code TypeScript.

        Args:
            code: Le code TypeScript à tester
            test_framework: Le framework de test à utiliser
            parsed_elements: Les éléments parsés du code
            include_mocks: Indique si des mocks doivent être inclus

        Returns:
            str: Le code de test généré
        """
        functions = []
        classes = []
        interfaces = []
        types = []

        if parsed_elements:
            for element in parsed_elements:
                if element.get("type") == "function":
                    functions.append(element)
                elif element.get("type") == "class":
                    classes.append(element)
                elif element.get("type") == "interface":
                    interfaces.append(element)
                elif element.get("type") == "type":
                    types.append(element)
        else:
            lines = code.split("\n")
            for i, line in enumerate(lines):
                line = line.strip()
                if "function " in line:
                    func_name = line.split("function ")[1].split("(")[0].strip()
                    return_type = line.split("):")[1].split("{")[0].strip() if "):" in line else "void"
                    if func_name:
                        functions.append({
                            "type": "function",
                            "name": func_name,
                            "line": i + 1,
                            "params": line.split("(")[1].split(")")[0] if "(" in line and ")" in line else "",
                            "return_type": return_type
                        })
                elif "class " in line:
                    class_def = line.split("class ")[1]
                    class_name = class_def.split(" ")[0].split("{")[0].strip()
                    extends = class_def.split("extends ")[1].split(" ")[0].strip() if "extends " in class_def else None
                    implements = class_def.split("implements ")[1].split(" {")[0].strip() if "implements " in class_def else None
                    classes.append({
                        "type": "class",
                        "name": class_name,
                        "line": i + 1,
                        "extends": extends,
                        "implements": implements
                    })
                elif "interface " in line:
                    interface_def = line.split("interface ")[1]
                    interface_name = interface_def.split(" ")[0].split("{")[0].strip()
                    extends = interface_def.split("extends ")[1].split(" {")[0].strip() if "extends " in interface_def else None
                    interfaces.append({
                        "type": "interface",
                        "name": interface_name,
                        "line": i + 1,
                        "extends": extends
                    })
                elif "type " in line and "=" in line:
                    type_name = line.split("type ")[1].split("=")[0].strip()
                    types.append({
                        "type": "type",
                        "name": type_name,
                        "line": i + 1
                    })

        if test_framework == "jest":
            return self._generate_typescript_jest_tests(code, functions, classes, interfaces, types, include_mocks)
        elif test_framework == "mocha":
            return self._generate_typescript_mocha_tests(code, functions, classes, interfaces, types, include_mocks)
        else:
            return self._generate_typescript_jest_tests(code, functions, classes, interfaces, types, include_mocks)

    def _generate_fallback_tests(self, request: TestGenerationRequest, parser=None) -> TestGenerationResponse:
        """
        Génère des tests en fallback local sans LLM.

        Args:
            request: Requête de génération de tests
            parser: Parser optionnel

        Returns:
            TestGenerationResponse: Tests générés en mode fallback
        """
        framework = request.test_framework or self._get_default_test_framework(request.language)

        tested_elements = self._extract_tested_elements(request.code, request.language, parser)

        if request.language == "python":
            test_code = self._generate_python_tests(
                request.code, framework, tested_elements, request.include_mocks
            )
        elif request.language == "javascript":
            test_code = self._generate_javascript_tests(
                request.code, framework, tested_elements, request.include_mocks
            )
        elif request.language == "typescript":
            test_code = self._generate_typescript_tests(
                request.code, framework, tested_elements, request.include_mocks
            )
        else:
            test_code = self._generate_generic_tests(request.code, request.language, framework)

        estimated_coverage = self._estimate_coverage(tested_elements, request.code, request.coverage_target)

        test_file_path = None
        if request.file_path and request.output_dir:
            test_file_path = self._generate_test_file_path(
                request.file_path, request.output_dir, framework
            )

        return TestGenerationResponse(
            test_code=test_code,
            language=request.language,
            framework=framework,
            test_file_path=test_file_path,
            estimated_coverage=estimated_coverage,
            tested_elements=tested_elements
        )

    def _extract_tested_elements(self, code: str, language: str, parser=None) -> List[Dict[str, str]]:
        """
        Extrait les éléments testables du code.

        Args:
            code: Code source à analyser
            language: Langage du code
            parser: Parser optionnel

        Returns:
            Liste des éléments testables
        """
        elements = []

        if parser and hasattr(parser, 'parse'):
            try:
                parsed = parser.parse(code)
                if 'elements' in parsed:
                    return [{"name": e["name"], "type": e["type"]} for e in parsed['elements']]
            except Exception:
                pass

        lines = code.split('\n')

        if language == "python":
            for i, line in enumerate(lines):
                line = line.strip()
                if line.startswith("def ") and not line.startswith("def _"):
                    func_name = line.split("def ")[1].split("(")[0].strip()
                    elements.append({"name": func_name, "type": "function"})
                elif line.startswith("class "):
                    class_name = line.split("class ")[1].split("(")[0].split(":")[0].strip()
                    elements.append({"name": class_name, "type": "class"})

        elif language in ["javascript", "typescript"]:
            for i, line in enumerate(lines):
                line = line.strip()
                if "function " in line and not line.startswith("//"):
                    func_name = line.split("function ")[1].split("(")[0].strip()
                    if func_name:
                        elements.append({"name": func_name, "type": "function"})
                elif "class " in line and not line.startswith("//"):
                    class_name = line.split("class ")[1].split(" ")[0].split("{")[0].strip()
                    elements.append({"name": class_name, "type": "class"})

        return elements

    def _estimate_coverage(self, tested_elements: List[Dict[str, str]], code: str, target_coverage: float) -> float:
        """
        Estime la couverture de code basée sur les éléments testés.

        Args:
            tested_elements: Éléments qui seront testés
            code: Code source
            target_coverage: Couverture cible

        Returns:
            Estimation de la couverture (0.0-1.0)
        """
        if not tested_elements:
            return 0.0

        lines_of_code = len([line for line in code.split('\n') if line.strip() and not line.strip().startswith('#')])
        element_count = len(tested_elements)

        base_coverage = min(element_count * 0.3, 0.9)

        if target_coverage:
            estimated_coverage = min(base_coverage, target_coverage)
        else:
            estimated_coverage = base_coverage

        return round(estimated_coverage, 2)

    def _generate_test_file_path(self, file_path: str, output_dir: str, framework: str) -> str:
        """
        Génère le chemin du fichier de test.

        Args:
            file_path: Chemin du fichier source
            output_dir: Répertoire de sortie
            framework: Framework de test

        Returns:
            Chemin du fichier de test
        """
        import os

        filename = os.path.basename(file_path)
        name_without_ext = os.path.splitext(filename)[0]
        extension = os.path.splitext(filename)[1]

        if framework in ["jest"]:
            test_filename = f"{name_without_ext}.test{extension}"
        else:
            test_filename = f"test_{name_without_ext}{extension}"

        return os.path.join(output_dir, test_filename)

    def _generate_generic_tests(self, code: str, language: str, framework: str) -> str:
        """
        Génère des tests génériques pour les langages non spécifiquement supportés.

        Args:
            code: Code source
            language: Langage
            framework: Framework de test

        Returns:
            Code de test générique
        """
        return f"""
// Tests générés automatiquement pour {language}
// Framework: {framework}

// TODO: Implémenter les tests spécifiques pour {language}
// Code source à tester:
/*
{code}
*/

// Exemple de structure de test:
function testExample() {{
    // Ajouter les tests appropriés ici
    console.log("Tests à implémenter pour {language}");
}}
"""

    def _generate_jest_tests(self, code: str, functions: List[Dict[str, Any]], classes: List[Dict[str, Any]], include_mocks: bool) -> str:
        """Génère des tests Jest pour JavaScript."""
        test_code = "// Tests générés automatiquement avec Jest\n\n"

        if include_mocks:
            test_code += "// Mocks\njest.mock('./module');\n\n"

        if functions:
            test_code += "// Tests des fonctions\n"
            test_code += "describe('Functions', () => {\n"
            for func in functions:
                func_name = func["name"]
                test_code += f"  test('{func_name} should work correctly', () => {{\n"
                test_code += f"    // TODO: Implémenter le test pour {func_name}\n"
                test_code += f"    expect({func_name}).toBeDefined();\n"
                test_code += "  });\n\n"
            test_code += "});\n\n"

        if classes:
            test_code += "// Tests des classes\n"
            for cls in classes:
                class_name = cls["name"]
                test_code += f"describe('{class_name}', () => {{\n"
                test_code += f"  test('should instantiate correctly', () => {{\n"
                test_code += f"    const instance = new {class_name}();\n"
                test_code += f"    expect(instance).toBeInstanceOf({class_name});\n"
                test_code += "  });\n"
                test_code += "});\n\n"

        return test_code

    def _generate_mocha_tests(self, code: str, functions: List[Dict[str, Any]], classes: List[Dict[str, Any]], include_mocks: bool) -> str:
        """Génère des tests Mocha pour JavaScript."""
        test_code = "// Tests générés automatiquement avec Mocha\n"
        test_code += "const { expect } = require('chai');\n\n"

        if include_mocks:
            test_code += "// Configuration des mocks\nconst sinon = require('sinon');\n\n"

        if functions:
            test_code += "describe('Functions', function() {\n"
            for func in functions:
                func_name = func["name"]
                test_code += f"  describe('{func_name}', function() {{\n"
                test_code += f"    it('should work correctly', function() {{\n"
                test_code += f"      // TODO: Implémenter le test pour {func_name}\n"
                test_code += f"      expect({func_name}).to.exist;\n"
                test_code += "    });\n"
                test_code += "  });\n"
            test_code += "});\n\n"

        if classes:
            for cls in classes:
                class_name = cls["name"]
                test_code += f"describe('{class_name}', function() {{\n"
                test_code += f"  it('should instantiate correctly', function() {{\n"
                test_code += f"    const instance = new {class_name}();\n"
                test_code += f"    expect(instance).to.be.instanceOf({class_name});\n"
                test_code += "  });\n"
                test_code += "});\n\n"

        return test_code

    def _generate_typescript_jest_tests(self, code: str, functions: List[Dict[str, Any]], classes: List[Dict[str, Any]], interfaces: List[Dict[str, Any]], types: List[Dict[str, Any]], include_mocks: bool) -> str:
        """Génère des tests unitaires avec le framework Jest pour TypeScript."""
        module_name = "module"

        test_code = "/**\n * Tests unitaires générés automatiquement avec Jest pour TypeScript\n */\n"

        test_code += "import { expect } from '@jest/globals';\n"

        if functions or classes or interfaces or types:
            test_code += "// Import du module à tester\n"

            elements_to_import = []
            for func in functions:
                elements_to_import.append(func["name"])
            for cls in classes:
                elements_to_import.append(cls["name"])
            for interface in interfaces:
                elements_to_import.append(interface["name"])
            for type in types:
                elements_to_import.append(type["name"])

            if elements_to_import:
                test_code += f"import {{ {', '.join(elements_to_import)} }} from './{module_name}';\n\n"

        if include_mocks:
            test_code += "// Configuration des mocks\n"
            test_code += "jest.mock('./module');\n\n"

        for func in functions:
            func_name = func["name"]

            test_code += f"// Tests pour la fonction {func_name}\n"
            test_code += f"describe('{func_name}', () => {{\n"

            test_code += "  it('should be defined', () => {\n"
            test_code += f"    expect({func_name}).toBeDefined();\n"
            test_code += "  });\n\n"

            test_code += "  it('should return expected result', () => {\n"

            params = func.get("params", "")
            return_type = func.get("return_type", "void")

            param_values = []
            if params:
                param_parts = params.split(",")
                for param in param_parts:
                    param = param.strip()
                    if ":" in param:
                        param_name, param_type = param.split(":", 1)
                        param_name = param_name.strip()
                        param_type = param_type.strip()

                        if "string" in param_type:
                            param_values.append(f"'test{param_name}'")
                        elif "number" in param_type:
                            param_values.append("42")
                        elif "boolean" in param_type:
                            param_values.append("true")
                        elif "[]" in param_type or "Array" in param_type:
                            param_values.append("[]")
                        elif "object" in param_type or "{" in param_type:
                            param_values.append("{}")
                        else:
                            param_values.append("undefined")
                    else:
                        param_values.append("undefined")

            func_call = f"{func_name}({', '.join(param_values)})"

            if return_type and return_type != "void":
                if "string" in return_type:
                    test_code += f"    expect(typeof {func_call}).toBe('string');\n"
                elif "number" in return_type:
                    test_code += f"    expect(typeof {func_call}).toBe('number');\n"
                elif "boolean" in return_type:
                    test_code += f"    expect(typeof {func_call}).toBe('boolean');\n"
                elif "[]" in return_type or "Array" in return_type:
                    test_code += f"    expect(Array.isArray({func_call})).toBe(true);\n"
                elif "Promise" in return_type:
                    test_code += f"    return {func_call}.then(result => {{\n"
                    test_code += "      expect(result).toBeDefined();\n"
                    test_code += "    });\n"
                else:
                    test_code += f"    expect({func_call}).toBeDefined();\n"
            else:
                test_code += f"    {func_call};\n"
                test_code += "    expect(true).toBe(true); // Vérifier que la fonction s'exécute sans erreur\n"

            test_code += "  });\n"

            test_code += "\n  it('should handle error cases', () => {\n"
            test_code += "    // Ajouter des tests pour les cas d'erreur\n"
            test_code += "  });\n"

            test_code += "});\n\n"

        for cls in classes:
            class_name = cls["name"]

            test_code += f"// Tests pour la classe {class_name}\n"
            test_code += f"describe('{class_name}', () => {{\n"
            test_code += f"  let instance: {class_name};\n\n"

            test_code += "  beforeEach(() => {\n"
            test_code += f"    instance = new {class_name}();\n"
            test_code += "  });\n\n"

            test_code += "  it('should initialize correctly', () => {\n"
            test_code += f"    expect(instance).toBeInstanceOf({class_name});\n"
            test_code += "  });\n\n"

            test_code += "  it('should have expected methods', () => {\n"
            test_code += "    // Ajouter des tests pour les méthodes spécifiques\n"
            test_code += "  });\n"
            test_code += "});\n\n"

        for interface in interfaces:
            interface_name = interface["name"]

            test_code += f"// Tests pour l'interface {interface_name}\n"
            test_code += f"describe('{interface_name}', () => {{\n"
            test_code += f"  let mockImplementation: {interface_name};\n\n"

            test_code += "  beforeEach(() => {\n"
            test_code += f"    mockImplementation = {{\n"
            test_code += "      // Implémenter les propriétés requises par l'interface\n"
            test_code += "    }} as " + interface_name + ";\n"
            test_code += "  });\n\n"

            test_code += "  it('should be able to create an implementation', () => {\n"
            test_code += f"    expect(mockImplementation).toBeDefined();\n"
            test_code += "  });\n\n"

            if interface.get("extends"):
                parent_interface = interface.get("extends")
                test_code += f"\n  it('should extend {parent_interface}', () => {{\n"
                test_code += "    // Vérifier que l'implémentation contient les propriétés de l'interface parente\n"
                test_code += f"    const parentProps: Array<keyof {parent_interface}> = [];\n"
                test_code += "    parentProps.forEach(prop => {\n"
                test_code += "      expect(mockImplementation[prop]).toBeDefined();\n"
                test_code += "    });\n"
                test_code += "  });\n"

            test_code += "});\n\n"

        for type in types:
            type_name = type["name"]

            test_code += f"// Tests pour le type {type_name}\n"
            test_code += f"describe('{type_name}', () => {{\n"
            test_code += f"  let instance: {type_name};\n\n"

            test_code += "  beforeEach(() => {\n"
            test_code += f"    instance = {{\n"
            test_code += "      // Initialiser avec des valeurs valides pour ce type\n"
            test_code += "    }} as " + type_name + ";\n"
            test_code += "  });\n\n"

            test_code += "  it('should be a valid type', () => {\n"
            test_code += f"    const typeCheck = (value: {type_name}): boolean => true;\n"
            test_code += "    expect(typeCheck(instance)).toBe(true);\n"
            test_code += "  });\n"

            test_code += "});\n\n"

        return test_code

    def _generate_typescript_mocha_tests(self, code: str, functions: List[Dict[str, Any]], classes: List[Dict[str, Any]], interfaces: List[Dict[str, Any]], types: List[Dict[str, Any]], include_mocks: bool) -> str:
        """Génère des tests unitaires avec le framework Mocha pour TypeScript."""
        module_name = "module"

        test_code = "/**\n * Tests unitaires générés automatiquement avec Mocha pour TypeScript\n */\n"

        test_code += "import { expect } from 'chai';\n"

        if functions or classes or interfaces or types:
            test_code += "// Import du module à tester\n"

            elements_to_import = []
            for func in functions:
                elements_to_import.append(func["name"])
            for cls in classes:
                elements_to_import.append(cls["name"])
            for interface in interfaces:
                elements_to_import.append(interface["name"])
            for type in types:
                elements_to_import.append(type["name"])

            if elements_to_import:
                test_code += f"import {{ {', '.join(elements_to_import)} }} from './{module_name}';\n\n"

        if include_mocks:
            test_code += "// Configuration des mocks\n"
            test_code += "import * as sinon from 'sinon';\n\n"

        for func in functions:
            func_name = func["name"]

            test_code += f"// Tests pour la fonction {func_name}\n"
            test_code += f"describe('{func_name}', () => {{\n"

            test_code += "  it('should be defined', () => {\n"
            test_code += f"    expect({func_name}).to.exist;\n"
            test_code += "  });\n\n"

            test_code += "  it('should return expected result', () => {\n"

            params = func.get("params", "")
            return_type = func.get("return_type", "void")

            param_values = []
            if params:
                param_parts = params.split(",")
                for param in param_parts:
                    param = param.strip()
                    if ":" in param:
                        param_name, param_type = param.split(":", 1)
                        param_name = param_name.strip()
                        param_type = param_type.strip()

                        if "string" in param_type:
                            param_values.append(f"'test{param_name}'")
                        elif "number" in param_type:
                            param_values.append("42")
                        elif "boolean" in param_type:
                            param_values.append("true")
                        elif "[]" in param_type or "Array" in param_type:
                            param_values.append("[]")
                        elif "object" in param_type or "{" in param_type:
                            param_values.append("{}")
                        else:
                            param_values.append("undefined")
                    else:
                        param_values.append("undefined")

            func_call = f"{func_name}({', '.join(param_values)})"

            if return_type and return_type != "void":
                if "string" in return_type:
                    test_code += f"    expect(typeof {func_call}).toBe('string');\n"
                elif "number" in return_type:
                    test_code += f"    expect(typeof {func_call}).toBe('number');\n"
                elif "boolean" in return_type:
                    test_code += f"    expect(typeof {func_call}).toBe('boolean');\n"
                elif "[]" in return_type or "Array" in return_type:
                    test_code += f"    expect(Array.isArray({func_call})).toBe(true);\n"
                elif "Promise" in return_type:
                    test_code += f"    return {func_call}.then(result => {{\n"
                    test_code += "      expect(result).to.exist;\n"
                    test_code += "    });\n"
                else:
                    test_code += f"    expect({func_call}).to.exist;\n"
            else:
                test_code += f"    {func_call};\n"
                test_code += "    expect(true).toBe(true); // Vérifier que la fonction s'exécute sans erreur\n"

            test_code += "  });\n"

            test_code += "\n  it('should handle error cases', () => {\n"
            test_code += "    // Ajouter des tests pour les cas d'erreur\n"
            test_code += "  });\n"

            test_code += "});\n\n"

        for cls in classes:
            class_name = cls["name"]

            test_code += f"// Tests pour la classe {class_name}\n"
            test_code += f"describe('{class_name}', () => {{\n"
            test_code += f"  let instance: {class_name};\n\n"

            test_code += "  beforeEach(() => {\n"
            test_code += f"    instance = new {class_name}();\n"
            test_code += "  });\n\n"

            test_code += "  it('should initialize correctly', () => {\n"
            test_code += f"    expect(instance).toBeInstanceOf({class_name});\n"
            test_code += "  });\n\n"

            test_code += "  it('should have expected methods', () => {\n"
            test_code += "    // Ajouter des tests pour les méthodes spécifiques\n"
            test_code += "  });\n"
            test_code += "});\n\n"

        for interface in interfaces:
            interface_name = interface["name"]

            test_code += f"// Tests pour l'interface {interface_name}\n"
            test_code += f"describe('{interface_name}', () => {{\n"
            test_code += f"  let mockImplementation: {interface_name};\n\n"

            test_code += "  beforeEach(() => {\n"
            test_code += f"    mockImplementation = {{\n"
            test_code += "      // Implémenter les propriétés requises par l'interface\n"
            test_code += "    }} as " + interface_name + ";\n"
            test_code += "  });\n\n"

            test_code += "  it('should be able to create an implementation', () => {\n"
            test_code += f"    expect(mockImplementation).to.exist;\n"
            test_code += "  });\n\n"

            if interface.get("extends"):
                parent_interface = interface.get("extends")
                test_code += f"\n  it('should extend {parent_interface}', () => {{\n"
                test_code += "    // Vérifier que l'implémentation contient les propriétés de l'interface parente\n"
                test_code += f"    const parentProps: Array<keyof {parent_interface}> = [];\n"
                test_code += "    parentProps.forEach(prop => {\n"
                test_code += "      expect(mockImplementation[prop]).to.exist;\n"
                test_code += "    });\n"
                test_code += "  });\n"

            test_code += "});\n\n"

        for type in types:
            type_name = type["name"]

            test_code += f"// Tests pour le type {type_name}\n"
            test_code += f"describe('{type_name}', () => {{\n"
            test_code += f"  let instance: {type_name};\n\n"

            test_code += "  beforeEach(() => {\n"
            test_code += f"    instance = {{\n"
            test_code += "      // Initialiser avec des valeurs valides pour ce type\n"
            test_code += "    }} as " + type_name + ";\n"
            test_code += "  });\n\n"

            test_code += "  it('should be a valid type', () => {\n"
            test_code += f"    const typeCheck = (value: {type_name}): boolean => true;\n"
            test_code += "    expect(typeCheck(instance)).toBe(true);\n"
            test_code += "  });\n"

            test_code += "});\n\n"

        return test_code


_test_generation_tool = TestGenerationTool()

def generate_tests(request: TestGenerationRequest, parser=None, llm_manager=None) -> TestGenerationResponse:
    """
    Fonction de compatibilité pour l'ancien système.

    Args:
        request: Requête de génération de tests
        parser: Parser optionnel
        llm_manager: LLM manager optionnel

    Returns:
        TestGenerationResponse: Tests générés
    """
    return _test_generation_tool.execute(request, parser=parser, llm_manager=llm_manager)
