"""
Moteur d'analyse et de génération de tests.

Contient la logique métier pure : analyse de code, extraction d'éléments,
gestion des templates, génération de fallback.
"""
import re
import ast
from typing import List, Dict, Any, Optional, Tuple
from .config import TEST_FRAMEWORKS, DEFAULT_FRAMEWORKS, IMPORT_TEMPLATES, TEST_TEMPLATES, LANGUAGE_TEST_INSTRUCTIONS


class TestGenerationEngine:
    """Moteur d'analyse et de génération de tests."""
    
    def __init__(self, logger=None):
        self.logger = logger
    
    def detect_framework(self, language: str, requested_framework: Optional[str] = None) -> str:
        """Détecte le framework de test approprié."""
        lang = language.lower()
        
        if requested_framework:
            frameworks = TEST_FRAMEWORKS.get(lang, [])
            if requested_framework.lower() in [f.lower() for f in frameworks]:
                return requested_framework.lower()
        
        return DEFAULT_FRAMEWORKS.get(lang, "pytest")
    
    def get_supported_frameworks(self, language: str) -> List[str]:
        """Retourne les frameworks supportés pour un langage."""
        return TEST_FRAMEWORKS.get(language.lower(), [])
    
    def extract_code_elements(self, code: str, language: str) -> List[Dict[str, Any]]:
        """Extrait les éléments de code à tester."""
        elements = []
        
        if language.lower() == "python":
            elements = self._extract_python_elements(code)
        elif language.lower() in ["javascript", "typescript"]:
            elements = self._extract_js_elements(code)
        elif language.lower() == "java":
            elements = self._extract_java_elements(code)
        elif language.lower() == "c#":
            elements = self._extract_csharp_elements(code)
        elif language.lower() == "php":
            elements = self._extract_php_elements(code)
        
        return elements
    
    def _extract_python_elements(self, code: str) -> List[Dict[str, Any]]:
        """Extrait les fonctions et classes Python."""
        elements = []
        
        try:
            tree = ast.parse(code)
            
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    # Ignorer les méthodes privées
                    if node.name.startswith('_'):
                        continue
                    
                    params = [arg.arg for arg in node.args.args if arg.arg != 'self']
                    
                    elements.append({
                        'type': 'function',
                        'name': node.name,
                        'params': params,
                        'line_number': node.lineno,
                        'complexity': self._estimate_complexity(node)
                    })
                
                elif isinstance(node, ast.ClassDef):
                    methods = []
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and not item.name.startswith('_'):
                            methods.append(item.name)
                    
                    elements.append({
                        'type': 'class',
                        'name': node.name,
                        'methods': methods,
                        'line_number': node.lineno
                    })
        
        except SyntaxError as e:
            if self.logger:
                self.logger.warning(f"Erreur syntaxique Python: {e}")
        
        return elements
    
    def _extract_js_elements(self, code: str) -> List[Dict[str, Any]]:
        """Extrait les fonctions et classes JavaScript/TypeScript."""
        elements = []
        
        # Pattern pour les fonctions
        func_patterns = [
            r'(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)',
            r'(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>',
            r'(?:export\s+)?(?:async\s+)?function\s*\(([^)]*)\)\s*\{[^}]*\}',
        ]
        
        for pattern in func_patterns:
            matches = re.finditer(pattern, code, re.MULTILINE | re.DOTALL)
            for match in matches:
                name = match.group(1) if len(match.groups()) > 0 else 'anonymous'
                params_str = match.group(2) if len(match.groups()) > 1 else ''
                params = [p.strip() for p in params_str.split(',') if p.strip()]
                
                elements.append({
                    'type': 'function',
                    'name': name,
                    'params': params,
                    'line_number': code[:match.start()].count('\n') + 1
                })
        
        # Pattern pour les classes
        class_pattern = r'(?:export\s+)?class\s+(\w+)(?:\s+extends\s+\w+)?\s*\{'
        for match in re.finditer(class_pattern, code):
            elements.append({
                'type': 'class',
                'name': match.group(1),
                'line_number': code[:match.start()].count('\n') + 1
            })
        
        return elements
    
    def _extract_java_elements(self, code: str) -> List[Dict[str, Any]]:
        """Extrait les méthodes et classes Java."""
        elements = []
        
        # Pattern pour les classes
        class_pattern = r'(?:public\s+)?class\s+(\w+)'
        for match in re.finditer(class_pattern, code):
            elements.append({
                'type': 'class',
                'name': match.group(1),
                'line_number': code[:match.start()].count('\n') + 1
            })
        
        # Pattern pour les méthodes publiques
        method_pattern = r'(?:public\s+)(?:static\s+)?(?:\w+[<>\[\]]*\s+)?(\w+)\s*\(([^)]*)\)\s*\{'
        for match in re.finditer(method_pattern, code):
            name = match.group(1)
            if name in ['if', 'for', 'while', 'switch']:
                continue
            
            elements.append({
                'type': 'method',
                'name': name,
                'line_number': code[:match.start()].count('\n') + 1
            })
        
        return elements
    
    def _extract_csharp_elements(self, code: str) -> List[Dict[str, Any]]:
        """Extrait les méthodes et classes C#."""
        elements = []
        
        # Pattern pour les classes
        class_pattern = r'(?:public\s+)?class\s+(\w+)'
        for match in re.finditer(class_pattern, code):
            elements.append({
                'type': 'class',
                'name': match.group(1),
                'line_number': code[:match.start()].count('\n') + 1
            })
        
        # Pattern pour les méthodes publiques
        method_pattern = r'(?:public\s+)(?:static\s+)?(?:\w+[<>\[\]]*\s+)?(\w+)\s*\(([^)]*)\)\s*\{'
        for match in re.finditer(method_pattern, code):
            name = match.group(1)
            if name in ['if', 'for', 'while', 'switch']:
                continue
            
            elements.append({
                'type': 'method',
                'name': name,
                'line_number': code[:match.start()].count('\n') + 1
            })
        
        return elements
    
    def _extract_php_elements(self, code: str) -> List[Dict[str, Any]]:
        """Extrait les méthodes et classes PHP."""
        elements = []
        
        # Pattern pour les classes
        class_pattern = r'(?:abstract\s+)?class\s+(\w+)'
        for match in re.finditer(class_pattern, code):
            elements.append({
                'type': 'class',
                'name': match.group(1),
                'line_number': code[:match.start()].count('\n') + 1
            })
        
        # Pattern pour les méthodes/fonctions publiques
        method_pattern = r'(?:public\s+)(?:static\s+)?function\s+(\w+)\s*\(([^)]*)\)'
        for match in re.finditer(method_pattern, code):
            elements.append({
                'type': 'method',
                'name': match.group(1),
                'params': match.group(2),
                'line_number': code[:match.start()].count('\n') + 1
            })
        
        return elements
    
    def _estimate_complexity(self, node) -> str:
        """Estime la complexité cyclomatique d'une fonction Python."""
        complexity = 1
        
        for child in ast.walk(node):
            if isinstance(child, (ast.If, ast.While, ast.For, ast.ExceptHandler)):
                complexity += 1
            elif isinstance(child, ast.BoolOp):
                complexity += len(child.values) - 1
        
        if complexity <= 3:
            return "low"
        elif complexity <= 6:
            return "medium"
        return "high"
    
    def generate_import_statement(self, language: str, framework: str, class_name: str, module_name: str) -> str:
        """Génère l'instruction d'import pour le test."""
        lang_imports = IMPORT_TEMPLATES.get(language, {})
        template = lang_imports.get(framework, "")
        
        if template:
            return template.format(class_name=class_name, module=module_name)
        return ""
    
    def generate_test_file_path(self, original_path: Optional[str], language: str, framework: str) -> str:
        """Génère le chemin du fichier de test."""
        if not original_path:
            return f"test_generated.{self._get_extension(language)}"
        
        import pathlib
        path = pathlib.Path(original_path)
        
        # Convention de nommage par langage
        if language == "python":
            return str(path.parent / f"test_{path.stem}.py")
        elif language in ["javascript", "typescript"]:
            return str(path.parent / f"{path.stem}.test{path.suffix}")
        elif language == "php":
            return str(path.parent / f"{path.stem}Test.php")
        elif language == "java":
            return str(path.parent / f"{path.stem}Test.java")
        elif language == "c#":
            return str(path.parent / f"{path.stem}Tests.cs")
        
        return f"test_{path.name}"
    
    def _get_extension(self, language: str) -> str:
        """Retourne l'extension de fichier par langage."""
        extensions = {
            "python": "py",
            "javascript": "js",
            "typescript": "ts",
            "java": "java",
            "c#": "cs",
            "php": "php"
        }
        return extensions.get(language.lower(), "txt")
    
    def estimate_coverage(self, elements: List[Dict[str, Any]], test_count: int) -> float:
        """Estime la couverture de code basée sur les éléments et tests."""
        if not elements:
            return 0.0
        
        # Estimation simple : un test par élément = 80% de couverture
        coverage = min(1.0, (test_count / len(elements)) * 0.8)
        return round(coverage, 2)
    
    def get_test_instructions(self, language: str) -> str:
        """Retourne les instructions de génération de tests pour un langage."""
        return LANGUAGE_TEST_INSTRUCTIONS.get(language.lower(), "")
    
    def build_prompt(self, code: str, language: str, framework: str, 
                    include_mocks: bool, coverage_target: float, 
                    elements: List[Dict[str, Any]]) -> str:
        """Construit le prompt pour le LLM."""
        prompt_parts = [
            f"Génère des tests unitaires pour le code {language} suivant.",
            f"",
            f"Framework de test: {framework}",
            f"Cible de couverture: {coverage_target:.0%}",
            f"Inclure des mocks: {'oui' if include_mocks else 'non'}",
            f"",
            f"```{language}",
            code,
            f"```",
            f"",
        ]
        
        if elements:
            prompt_parts.append("Éléments à tester:")
            for element in elements[:10]:
                if element['type'] == 'function':
                    prompt_parts.append(f"- Fonction: {element['name']}({', '.join(element.get('params', []))})")
                elif element['type'] in ['class', 'Class']:
                    methods = element.get('methods', [])
                    if methods:
                        prompt_parts.append(f"- Classe: {element['name']} (méthodes: {', '.join(methods[:5])})")
                    else:
                        prompt_parts.append(f"- Classe: {element['name']}")
                else:
                    prompt_parts.append(f"- {element['type']}: {element['name']}")
            prompt_parts.append("")
        
        # Ajouter les instructions spécifiques au langage
        instructions = self.get_test_instructions(language)
        if instructions:
            prompt_parts.append("Instructions:")
            prompt_parts.append(instructions)
        
        return "\n".join(prompt_parts)
    
    def generate_fallback_tests(self, code: str, language: str, framework: str,
                               elements: List[Dict[str, Any]]) -> Tuple[str, int]:
        """Génère des tests basiques en fallback si le LLM n'est pas disponible."""
        if not elements:
            return f"# Tests générés automatiquement pour {language}\n# Aucun élément trouvé à tester", 0
        
        test_lines = [
            f"# Tests générés automatiquement pour {language}",
            f"# Framework: {framework}",
            ""
        ]
        
        # Ajouter un import basique
        if language == "python":
            test_lines.append("import pytest")
            test_lines.append("")
        
        test_count = 0
        
        for element in elements:
            if element['type'] == 'function':
                test_lines.append(f"def test_{element['name']}():")
                test_lines.append(f"    # TODO: Implémenter le test pour {element['name']}")
                test_lines.append("    pass")
                test_lines.append("")
                test_count += 1
            
            elif element['type'] == 'class':
                if language == "python":
                    test_lines.append(f"class Test{element['name']}:")
                    for method in element.get('methods', [])[:3]:
                        test_lines.append(f"    def test_{method}(self):")
                        test_lines.append(f"        # TODO: Implémenter le test pour {method}")
                        test_lines.append("        pass")
                        test_lines.append("")
                        test_count += 1
                else:
                    test_lines.append(f"# Tests pour la classe {element['name']}")
                    test_lines.append("")
        
        test_lines.append("")
        test_lines.append("# NOTE: Ces tests sont des placeholders.")
        test_lines.append("# Utilisez un LLM pour générer des tests complets et fonctionnels.")
        
        return "\n".join(test_lines), test_count
