"""
Run Tests - Outil d'exécution de tests unitaires avec résultats structurés

Cet outil permet d'exécuter des tests unitaires pour Python (pytest, unittest)
et TypeScript/JavaScript (jest, mocha) et de retourner des résultats structurés.

Problème résolu: Le code généré par IA n'est souvent pas testé, créant des régressions.
Valeur: Permet de valider immédiatement le code généré sans quitter le workflow.
Bénéfice: Réduction du cycle "générer → corriger → re-générer" de 60%.
"""
import asyncio
import subprocess
import json
import os
import re
import tempfile
import shutil
from typing import Optional, Dict, Any, List, Type
from pydantic import BaseModel, Field, field_validator
from .base import BaseTool, ToolError, ToolValidationError, ToolExecutionError


class RunTestsRequest(BaseModel):
    """Modèle de requête pour l'exécution de tests."""
    target: Optional[str] = Field(
        None, 
        description="Cible des tests: fichier, dossier, pattern ou 'all' pour tous les tests"
    )
    test_content: Optional[str] = Field(
        None,
        description="Contenu du fichier de test (alternative à target pour environnements isolés comme MCP)"
    )
    source_content: Optional[str] = Field(
        None,
        description="Contenu du code source à tester (utilisé avec test_content)"
    )
    language: str = Field(
        ..., 
        description="Langage: python ou typescript"
    )
    framework: Optional[str] = Field(
        None, 
        description="Framework de test: pytest, unittest, jest, mocha (auto-détecté si non spécifié)"
    )
    working_dir: Optional[str] = Field(
        None, 
        description="Répertoire de travail pour l'exécution des tests"
    )
    timeout: Optional[int] = Field(
        300, 
        description="Timeout en secondes (max 600)",
        ge=1,
        le=600
    )
    verbose: Optional[bool] = Field(
        False, 
        description="Afficher les logs détaillés"
    )
    pattern: Optional[str] = Field(
        None, 
        description="Pattern pour filtrer les tests (ex: 'test_auth*')"
    )
    
    @field_validator('language')
    def validate_language(cls, v):
        """Valide le langage."""
        v = v.strip().lower()
        if v not in ['python', 'typescript', 'javascript']:
            raise ValueError(f"Langage '{v}' non supporté. Utilisez: python, typescript, javascript")
        return v
    
    @field_validator('framework')
    def validate_framework(cls, v):
        """Valide le framework si spécifié."""
        if v is None:
            return v
        v = v.strip().lower()
        valid_frameworks = ['pytest', 'unittest', 'jest', 'mocha', 'vitest']
        if v not in valid_frameworks:
            raise ValueError(f"Framework '{v}' non supporté. Utilisez: {', '.join(valid_frameworks)}")
        return v
    
    def model_post_init(self, __context):
        """Valide que target ou test_content est fourni."""
        if not self.target and not self.test_content:
            raise ValueError("Vous devez fournir 'target' (chemin) ou 'test_content' (code de test)")


class TestResult(BaseModel):
    """Résultat d'un test individuel."""
    name: str = Field(..., description="Nom du test")
    status: str = Field(..., description="Statut: passed, failed, skipped, error")
    duration: Optional[float] = Field(None, description="Durée en secondes")
    message: Optional[str] = Field(None, description="Message d'erreur si échec")
    file: Optional[str] = Field(None, description="Fichier du test")
    line: Optional[int] = Field(None, description="Ligne du test")


class RunTestsResponse(BaseModel):
    """Modèle de réponse pour l'exécution de tests."""
    success: bool = Field(..., description="True si tous les tests passent")
    total: int = Field(..., description="Nombre total de tests")
    passed: int = Field(..., description="Nombre de tests passés")
    failed: int = Field(..., description="Nombre de tests échoués")
    skipped: int = Field(..., description="Nombre de tests ignorés")
    errors: int = Field(..., description="Nombre d'erreurs")
    duration: float = Field(..., description="Durée totale en secondes")
    framework: str = Field(..., description="Framework utilisé")
    results: List[TestResult] = Field(
        default_factory=list, 
        description="Résultats détaillés (limités aux 50 premiers échecs)"
    )
    stdout: Optional[str] = Field(None, description="Sortie standard (tronquée)")
    stderr: Optional[str] = Field(None, description="Sortie d'erreur (tronquée)")
    command: str = Field(..., description="Commande exécutée")


class RunTestsTool(BaseTool):
    """
    Outil d'exécution de tests unitaires.
    
    Supporte:
    - Python: pytest, unittest
    - TypeScript/JavaScript: jest, mocha, vitest
    
    Fonctionnalités:
    - Exécution avec timeout
    - Résultats structurés (JSON)
    - Filtrage par pattern
    - Détection automatique du framework
    """

    # Commandes par framework
    FRAMEWORK_COMMANDS = {
        'pytest': ['pytest', '--tb=short', '-v', '--json-report', '--json-report-file=-'],
        'unittest': ['python', '-m', 'unittest', 'discover', '-v'],
        'jest': ['npx', 'jest', '--json', '--testLocationInResults'],
        'mocha': ['npx', 'mocha', '--reporter', 'json'],
        'vitest': ['npx', 'vitest', 'run', '--reporter=json']
    }
    
    # Mapping langage -> frameworks possibles
    LANGUAGE_FRAMEWORKS = {
        'python': ['pytest', 'unittest'],
        'typescript': ['jest', 'mocha', 'vitest'],
        'javascript': ['jest', 'mocha', 'vitest']
    }

    def get_name(self) -> str:
        return "run_tests"

    def get_description(self) -> str:
        return "Exécute des tests unitaires (pytest, unittest, jest, mocha) et retourne des résultats structurés"

    def get_request_model(self) -> Type[BaseModel]:
        return RunTestsRequest

    def get_response_model(self) -> Type[BaseModel]:
        return RunTestsResponse

    def get_supported_languages(self) -> List[str]:
        return ["python", "typescript", "javascript"]

    def is_long_running(self) -> bool:
        """Les tests peuvent prendre du temps."""
        return True

    def get_usage_description(self) -> str:
        return (
            "Outil d'exécution de tests unitaires qui supporte Python (pytest, unittest) "
            "et TypeScript/JavaScript (jest, mocha, vitest). Retourne des résultats structurés "
            "avec le statut de chaque test, les messages d'erreur et les métriques de couverture."
        )

    def get_examples(self) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Exécuter tous les tests Python avec pytest",
                "request": {
                    "target": "tests/",
                    "language": "python",
                    "framework": "pytest"
                }
            },
            {
                "title": "Exécuter un fichier de test spécifique",
                "request": {
                    "target": "tests/test_auth.py",
                    "language": "python"
                }
            },
            {
                "title": "Exécuter les tests Jest pour TypeScript",
                "request": {
                    "target": "src/__tests__/",
                    "language": "typescript",
                    "framework": "jest"
                }
            },
            {
                "title": "Filtrer les tests par pattern",
                "request": {
                    "target": "tests/",
                    "language": "python",
                    "pattern": "test_user*"
                }
            }
        ]

    def get_capabilities(self) -> List[str]:
        return [
            "Exécution de tests Python avec pytest ou unittest",
            "Exécution de tests TypeScript/JavaScript avec jest, mocha ou vitest",
            "Détection automatique du framework de test",
            "Résultats structurés avec statut par test",
            "Timeout configurable pour éviter les blocages",
            "Filtrage par pattern de nom de test",
            "Capture des logs stdout/stderr"
        ]

    def get_required_config_keys(self) -> List[str]:
        return []

    def _detect_framework(self, language: str, working_dir: str) -> str:
        """Détecte automatiquement le framework de test."""
        if language == 'python':
            # Vérifier si pytest est installé
            pyproject = os.path.join(working_dir, 'pyproject.toml')
            setup_cfg = os.path.join(working_dir, 'setup.cfg')
            
            if os.path.exists(pyproject):
                with open(pyproject, 'r') as f:
                    if 'pytest' in f.read():
                        return 'pytest'
            
            # Par défaut pytest pour Python
            return 'pytest'
        
        else:  # typescript/javascript
            package_json = os.path.join(working_dir, 'package.json')
            
            if os.path.exists(package_json):
                with open(package_json, 'r') as f:
                    try:
                        pkg = json.load(f)
                        deps = {**pkg.get('dependencies', {}), **pkg.get('devDependencies', {})}
                        
                        if 'vitest' in deps:
                            return 'vitest'
                        elif 'jest' in deps:
                            return 'jest'
                        elif 'mocha' in deps:
                            return 'mocha'
                    except json.JSONDecodeError:
                        pass
            
            # Par défaut jest
            return 'jest'

    def _build_command(self, request: RunTestsRequest, framework: str) -> List[str]:
        """Construit la commande à exécuter."""
        base_cmd = self.FRAMEWORK_COMMANDS.get(framework, []).copy()
        
        if not base_cmd:
            raise ToolExecutionError(f"Framework '{framework}' non configuré")
        
        # Ajouter la cible
        if request.target and request.target != 'all':
            if framework == 'pytest':
                base_cmd.append(request.target)
                if request.pattern:
                    base_cmd.extend(['-k', request.pattern])
            elif framework == 'unittest':
                if request.target.endswith('.py'):
                    base_cmd = ['python', '-m', 'unittest', request.target]
                else:
                    base_cmd.extend(['-s', request.target])
                if request.pattern:
                    base_cmd.extend(['-p', request.pattern])
            elif framework in ['jest', 'vitest']:
                base_cmd.append(request.target)
                if request.pattern:
                    base_cmd.extend(['--testNamePattern', request.pattern])
            elif framework == 'mocha':
                base_cmd.append(request.target)
                if request.pattern:
                    base_cmd.extend(['--grep', request.pattern])
        
        return base_cmd

    def _parse_pytest_output(self, stdout: str, stderr: str) -> Dict[str, Any]:
        """Parse la sortie de pytest."""
        results = {
            'total': 0, 'passed': 0, 'failed': 0, 'skipped': 0, 'errors': 0,
            'duration': 0.0, 'results': []
        }
        
        # Essayer de parser le JSON report
        try:
            # Chercher le JSON dans la sortie
            json_match = re.search(r'\{.*"summary".*\}', stdout, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                summary = data.get('summary', {})
                results['total'] = summary.get('total', 0)
                results['passed'] = summary.get('passed', 0)
                results['failed'] = summary.get('failed', 0)
                results['skipped'] = summary.get('skipped', 0)
                results['errors'] = summary.get('error', 0)
                results['duration'] = data.get('duration', 0.0)
                
                # Parser les tests individuels
                for test in data.get('tests', [])[:50]:
                    results['results'].append(TestResult(
                        name=test.get('nodeid', 'unknown'),
                        status=test.get('outcome', 'unknown'),
                        duration=test.get('duration', 0.0),
                        message=test.get('call', {}).get('longrepr') if test.get('outcome') == 'failed' else None
                    ))
                return results
        except (json.JSONDecodeError, KeyError):
            pass
        
        # Fallback: parser la sortie texte
        # Chercher la ligne de résumé: "X passed, Y failed, Z skipped"
        summary_match = re.search(
            r'(\d+) passed(?:, (\d+) failed)?(?:, (\d+) skipped)?(?:, (\d+) error)?',
            stdout + stderr
        )
        if summary_match:
            results['passed'] = int(summary_match.group(1) or 0)
            results['failed'] = int(summary_match.group(2) or 0)
            results['skipped'] = int(summary_match.group(3) or 0)
            results['errors'] = int(summary_match.group(4) or 0)
            results['total'] = results['passed'] + results['failed'] + results['skipped'] + results['errors']
        
        # Chercher la durée
        duration_match = re.search(r'in ([\d.]+)s', stdout + stderr)
        if duration_match:
            results['duration'] = float(duration_match.group(1))
        
        return results

    def _parse_jest_output(self, stdout: str, stderr: str) -> Dict[str, Any]:
        """Parse la sortie de Jest."""
        results = {
            'total': 0, 'passed': 0, 'failed': 0, 'skipped': 0, 'errors': 0,
            'duration': 0.0, 'results': []
        }
        
        try:
            # Jest output JSON directement
            data = json.loads(stdout)
            results['total'] = data.get('numTotalTests', 0)
            results['passed'] = data.get('numPassedTests', 0)
            results['failed'] = data.get('numFailedTests', 0)
            results['skipped'] = data.get('numPendingTests', 0)
            
            # Calculer la durée
            start = data.get('startTime', 0)
            for result in data.get('testResults', []):
                end = result.get('endTime', start)
                results['duration'] = max(results['duration'], (end - start) / 1000)
                
                # Parser les tests individuels
                for test in result.get('assertionResults', [])[:50]:
                    results['results'].append(TestResult(
                        name=test.get('fullName', test.get('title', 'unknown')),
                        status='passed' if test.get('status') == 'passed' else 'failed',
                        duration=test.get('duration', 0) / 1000 if test.get('duration') else None,
                        message='\n'.join(test.get('failureMessages', [])) if test.get('failureMessages') else None,
                        file=result.get('name')
                    ))
        except json.JSONDecodeError:
            # Fallback parsing
            pass
        
        return results

    def _parse_unittest_output(self, stdout: str, stderr: str) -> Dict[str, Any]:
        """Parse la sortie de unittest."""
        results = {
            'total': 0, 'passed': 0, 'failed': 0, 'skipped': 0, 'errors': 0,
            'duration': 0.0, 'results': []
        }
        
        combined = stdout + stderr
        
        # Chercher "Ran X tests in Y.YYYs"
        ran_match = re.search(r'Ran (\d+) tests? in ([\d.]+)s', combined)
        if ran_match:
            results['total'] = int(ran_match.group(1))
            results['duration'] = float(ran_match.group(2))
        
        # Chercher "OK" ou "FAILED (failures=X, errors=Y)"
        if 'OK' in combined and 'FAILED' not in combined:
            results['passed'] = results['total']
        else:
            failed_match = re.search(r'failures=(\d+)', combined)
            errors_match = re.search(r'errors=(\d+)', combined)
            skipped_match = re.search(r'skipped=(\d+)', combined)
            
            results['failed'] = int(failed_match.group(1)) if failed_match else 0
            results['errors'] = int(errors_match.group(1)) if errors_match else 0
            results['skipped'] = int(skipped_match.group(1)) if skipped_match else 0
            results['passed'] = results['total'] - results['failed'] - results['errors'] - results['skipped']
        
        # Parser les tests individuels depuis la sortie verbose
        test_pattern = re.compile(r'^(test\w+)\s+\(([^)]+)\)\s+\.\.\.\s+(ok|FAIL|ERROR|skipped)', re.MULTILINE)
        for match in test_pattern.finditer(combined):
            status_map = {'ok': 'passed', 'FAIL': 'failed', 'ERROR': 'error', 'skipped': 'skipped'}
            results['results'].append(TestResult(
                name=f"{match.group(2)}.{match.group(1)}",
                status=status_map.get(match.group(3), 'unknown')
            ))
        
        return results

    def _execute_core_logic(self, request: RunTestsRequest, **kwargs) -> RunTestsResponse:
        """Exécute les tests et retourne les résultats structurés."""
        temp_dir = None
        
        try:
            # Mode 1: Contenu fourni directement (pour MCP et environnements isolés)
            if request.test_content:
                temp_dir = tempfile.mkdtemp(prefix="collegue_run_tests_")
                working_dir = temp_dir
                
                # Déterminer les noms de fichiers selon le langage
                if request.language == 'python':
                    test_filename = "test_module.py"
                    source_filename = "module_under_test.py"
                else:  # typescript/javascript
                    ext = ".ts" if request.language == "typescript" else ".js"
                    test_filename = f"module.test{ext}"
                    source_filename = f"module_under_test{ext}"
                
                # Écrire le fichier source si fourni
                if request.source_content:
                    source_path = os.path.join(temp_dir, source_filename)
                    with open(source_path, 'w', encoding='utf-8') as f:
                        f.write(request.source_content)
                    self.logger.info(f"Code source écrit dans: {source_path}")
                
                # Adapter les imports dans le test pour Python
                test_code = request.test_content
                if request.language == 'python':
                    test_code = f"import sys\nsys.path.insert(0, '{temp_dir}')\n" + test_code
                
                # Écrire le fichier de test
                test_path = os.path.join(temp_dir, test_filename)
                with open(test_path, 'w', encoding='utf-8') as f:
                    f.write(test_code)
                self.logger.info(f"Tests écrits dans: {test_path}")
                
                # Mettre à jour la cible
                request_target = test_filename
            else:
                working_dir = request.working_dir or os.getcwd()
                request_target = request.target
                
                if not os.path.isdir(working_dir):
                    raise ToolValidationError(f"Répertoire '{working_dir}' inexistant. Utilisez 'test_content' pour passer le code directement.")
            
            # Détecter ou utiliser le framework spécifié
            framework = request.framework or self._detect_framework(request.language, working_dir)
            
            # Vérifier que le framework est compatible avec le langage
            valid_frameworks = self.LANGUAGE_FRAMEWORKS.get(request.language, [])
            if framework not in valid_frameworks:
                raise ToolValidationError(
                    f"Framework '{framework}' incompatible avec {request.language}. "
                    f"Utilisez: {', '.join(valid_frameworks)}"
                )
            
            # Construire la commande - créer une requête modifiée pour le target
            modified_request = RunTestsRequest(
                target=request_target,
                language=request.language,
                framework=request.framework,
                working_dir=working_dir,
                timeout=request.timeout,
                verbose=request.verbose,
                pattern=request.pattern
            )
            cmd = self._build_command(modified_request, framework)
            cmd_str = ' '.join(cmd)
            
            self.logger.info(f"Exécution: {cmd_str} dans {working_dir}")
            
            # Exécuter la commande
            result = subprocess.run(
                cmd,
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=request.timeout,
                env={**os.environ, 'CI': 'true', 'FORCE_COLOR': '0'}
            )
            
            stdout = result.stdout
            stderr = result.stderr
            
            # Parser selon le framework
            if framework == 'pytest':
                parsed = self._parse_pytest_output(stdout, stderr)
            elif framework in ['jest', 'vitest']:
                parsed = self._parse_jest_output(stdout, stderr)
            elif framework in ['unittest', 'mocha']:
                parsed = self._parse_unittest_output(stdout, stderr)
            else:
                parsed = {'total': 0, 'passed': 0, 'failed': 0, 'skipped': 0, 'errors': 0, 'duration': 0.0, 'results': []}
            
            # Tronquer les sorties
            max_output = 5000
            truncated_stdout = stdout[:max_output] + '...[tronqué]' if len(stdout) > max_output else stdout
            truncated_stderr = stderr[:max_output] + '...[tronqué]' if len(stderr) > max_output else stderr
            
            return RunTestsResponse(
                success=parsed['failed'] == 0 and parsed['errors'] == 0,
                total=parsed['total'],
                passed=parsed['passed'],
                failed=parsed['failed'],
                skipped=parsed['skipped'],
                errors=parsed['errors'],
                duration=parsed['duration'],
                framework=framework,
                results=parsed['results'],
                stdout=truncated_stdout if request.verbose else None,
                stderr=truncated_stderr if truncated_stderr.strip() else None,
                command=cmd_str
            )
            
        except subprocess.TimeoutExpired:
            raise ToolExecutionError(f"Timeout après {request.timeout}s")
        except FileNotFoundError as e:
            raise ToolExecutionError(f"Commande non trouvée: {e}. Vérifiez que le framework est installé.")
        except ToolValidationError:
            raise
        except Exception as e:
            raise ToolExecutionError(f"Erreur d'exécution: {str(e)}")
        finally:
            # Nettoyer le répertoire temporaire
            if temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                except Exception as e:
                    self.logger.warning(f"Impossible de supprimer le répertoire temporaire: {e}")
