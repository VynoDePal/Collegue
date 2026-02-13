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
from ..core.shared import run_async_from_sync
from ._run_tests import RunTestsTool, RunTestsRequest
from .utils import test_generators_adapter as _test_templates


class TestGenerationRequest(BaseModel):
    __test__ = False
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
        if not v or not v.strip():
            raise ValueError("Le langage ne peut pas être vide")
        return v.strip().lower()

    @field_validator('coverage_target')
    def validate_coverage_target(cls, v):
        if v is not None and (v < 0.0 or v > 1.0):
            raise ValueError("La cible de couverture doit être entre 0.0 et 1.0")
        return v


class TestValidationResult(BaseModel):
    __test__ = False
    validated: bool = Field(..., description="True si les tests ont été validés")
    success: bool = Field(..., description="True si tous les tests passent")
    total: int = Field(0, description="Nombre total de tests")
    passed: int = Field(0, description="Nombre de tests passés")
    failed: int = Field(0, description="Nombre de tests échoués")
    errors: int = Field(0, description="Nombre d'erreurs")
    error_message: Optional[str] = Field(None, description="Message d'erreur si la validation a échoué")
    duration: float = Field(0.0, description="Durée d'exécution en secondes")


class TestGenerationResponse(BaseModel):
    __test__ = False
    test_code: str = Field(..., description="Code de test généré")
    language: str = Field(..., description="Langage du code de test")
    framework: str = Field(..., description="Framework de test utilisé")
    test_file_path: Optional[str] = Field(None, description="Chemin du fichier de test généré")
    estimated_coverage: float = Field(..., description="Estimation de la couverture de code")
    tested_elements: List[Dict[str, str]] = Field(..., description="Éléments testés (fonctions, classes, etc.)")
    validation_result: Optional[TestValidationResult] = Field(None, description="Résultat de validation si validate_tests=True")


class LLMTestGenerationResult(BaseModel):
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
    __test__ = False
    tool_name = "test_generation"
    tool_description = "Génère automatiquement des tests unitaires pour du code source"
    tags = {"generation", "testing"}
    request_model = TestGenerationRequest
    response_model = TestGenerationResponse
    supported_languages = ["python", "javascript", "typescript", "java", "c#"]
    long_running = True

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
        return []

    def _validate_generated_tests(
        self, 
        test_code: str, 
        language: str, 
        framework: str,
        source_code: str,
        working_dir: Optional[str] = None
    ) -> TestValidationResult:
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
            elif language == "php":
                source_ext = ".php"
                test_prefix = ""
                source_filename = "ModuleUnderTest.php"
                test_filename = "ModuleUnderTestTest.php"
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
        ctx = kwargs.get('ctx')
        parser = kwargs.get('parser')

        self.validate_language(request.language)

        if ctx:
            try:
                framework = request.test_framework or self._get_default_test_framework(request.language)
                prompt = self._build_test_generation_prompt(request)
                system_prompt = f"""Tu es un expert en tests unitaires {request.language} avec {framework}.
Génère des tests complets, bien structurés et couvrant les cas limites."""

                # Use ctx.sample() via run_async_from_sync for sync context
                result = run_async_from_sync(ctx.sample(
                    messages=prompt,
                    system_prompt=system_prompt,
                    temperature=0.5,
                    max_tokens=2500
                ))

                generated_tests = result.text

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
                self.logger.warning(f"Erreur avec ctx.sample(), utilisation du fallback: {e}")
                return self._generate_fallback_tests(request, parser)
        else:
            return self._generate_fallback_tests(request, parser)

    async def _execute_core_logic_async(self, request: TestGenerationRequest, **kwargs) -> TestGenerationResponse:
        ctx = kwargs.get('ctx')
        parser = kwargs.get('parser')
        use_structured_output = kwargs.get('use_structured_output', True)
        
        if not ctx:
            return self._generate_fallback_tests(request, parser)
        
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
                    # Use ctx.sample() directly with result_type for structured output
                    llm_result = await ctx.sample(
                        messages=prompt,
                        system_prompt=system_prompt,
                        result_type=LLMTestGenerationResult,
                        temperature=0.5,
                        max_tokens=2500
                    )
                    
                    if isinstance(llm_result.result, LLMTestGenerationResult):
                        result_data = llm_result.result
                        generated_tests = result_data.test_code
                        tested_elements = []
                        for func in result_data.tested_functions:
                            tested_elements.append({"type": "function", "name": func})
                        for cls in result_data.tested_classes:
                            tested_elements.append({"type": "class", "name": cls})
                        
                        if ctx:
                            await ctx.info(f"Structured output: {result_data.test_count} tests générés")
                        
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
                            estimated_coverage=result_data.coverage_estimate,
                            tested_elements=tested_elements,
                            validation_result=validation_result
                        )
                except Exception as e:
                    self.logger.warning(f"Structured output a échoué, fallback vers texte brut: {e}")
            
            # Use ctx.sample() directly for text generation
            result = await ctx.sample(
                messages=prompt,
                system_prompt=system_prompt + "\nRéponds UNIQUEMENT avec le code de test, sans explications.",
                temperature=0.5,
                max_tokens=2500
            )
            generated_tests = result.text
            
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
        framework_map = {
            "python": "pytest",
            "javascript": "jest",
            "typescript": "jest",
            "java": "junit",
            "c#": "nunit"
        }
        return framework_map.get(language.lower(), "generic")

    def _get_available_test_frameworks(self, language: str) -> List[str]:
        frameworks_map = {
            "python": ["unittest", "pytest"],
            "javascript": ["jest", "mocha", "jasmine"],
            "typescript": ["jest", "mocha", "jasmine"],
            "java": ["junit", "testng"],
            "c#": ["nunit", "xunit", "mstest"]
        }
        return frameworks_map.get(language.lower(), ["generic"])

    def _get_test_framework_instructions(self, language: str, framework: str) -> str:
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

    def _generate_unittest_tests(self, code, functions, classes, include_mocks):
        return _test_templates.generate_unittest_tests(code, functions, classes, include_mocks)

    def _generate_pytest_tests(self, code, functions, classes, include_mocks):
        return _test_templates.generate_pytest_tests(code, functions, classes, include_mocks)

    def _generate_javascript_tests(self, code: str, test_framework: str, parsed_elements: List[Dict[str, Any]], include_mocks: bool) -> str:
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
                if "function " in line and not line.startswith("//"):
                    func_name = line.split("function ")[1].split("(")[0].strip()
                    if func_name:
                        functions.append({
                            "type": "function",
                            "name": func_name,
                            "line": i + 1,
                            "params": line.split("(")[1].split(")")[0] if "(" in line and ")" in line else ""
                        })
                elif "class " in line and not line.startswith("//"):
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

    def _generate_php_tests(self, code: str, test_framework: str, parsed_elements: List[Dict[str, Any]], include_mocks: bool) -> str:
        functions = []
        classes = []
        
        if parsed_elements:
            for element in parsed_elements:
                if element.get("type") == "function" or element.get("type") == "method":
                    functions.append(element)
                elif element.get("type") == "class":
                    classes.append(element)
        else:
            # Fallback regex simple
            lines = code.split("\n")
            for i, line in enumerate(lines):
                line = line.strip()
                if "function " in line:
                    parts = line.split("function ")[1].split("(")
                    if parts[0].strip():
                        functions.append({
                            "type": "function",
                            "name": parts[0].strip(),
                            "line": i + 1
                        })
                elif line.startswith("class "):
                    parts = line.split("class ")[1].split(" ")
                    if parts[0].strip():
                        classes.append({
                            "type": "class",
                            "name": parts[0].strip(),
                            "line": i + 1
                        })
                        
        if test_framework == "pest":
            return _test_templates.generate_pest_tests(code, functions, classes, include_mocks)
        else:
            return _test_templates.generate_phpunit_tests(code, functions, classes, include_mocks)

    def _generate_typescript_tests(self, code: str, test_framework: str, parsed_elements: List[Dict[str, Any]], include_mocks: bool) -> str:
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
        import os

        filename = os.path.basename(file_path)
        name_without_ext = os.path.splitext(filename)[0]
        extension = os.path.splitext(filename)[1]

        if framework in ["jest"]:
            test_filename = f"{name_without_ext}.test{extension}"
        else:
            test_filename = f"test_{name_without_ext}{extension}"

        return os.path.join(output_dir, test_filename)

    def _generate_generic_tests(self, code, language, framework):
        return _test_templates.generate_generic_tests(code, language, framework)

    def _generate_jest_tests(self, code, functions, classes, include_mocks):
        return _test_templates.generate_jest_tests(code, functions, classes, include_mocks)

    def _generate_mocha_tests(self, code, functions, classes, include_mocks):
        return _test_templates.generate_mocha_tests(code, functions, classes, include_mocks)

    def _generate_typescript_jest_tests(self, code, functions, classes, interfaces, types, include_mocks):
        return _test_templates.generate_typescript_jest_tests(code, functions, classes, interfaces, types, include_mocks)

    def _generate_typescript_mocha_tests(self, code, functions, classes, interfaces, types, include_mocks):
        return _test_templates.generate_typescript_mocha_tests(code, functions, classes, interfaces, types, include_mocks)


_test_generation_tool = TestGenerationTool()

def generate_tests(request: TestGenerationRequest, parser=None) -> TestGenerationResponse:
    return _test_generation_tool.execute(request, parser=parser)
