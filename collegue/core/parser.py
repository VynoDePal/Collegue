"""
Code Parser - Analyse syntaxique du code pour différents langages
"""
import ast
import re
from typing import Dict, List, Any, Optional, Tuple

class CodeParser:
    def __init__(self):
        self.supported_languages = ["python", "javascript", "typescript", "php"]

    def parse(self, code: str, language: str = None) -> Dict[str, Any]:
        if language is None:
            language = self._detect_language(code)

        if language not in self.supported_languages:
            return {"error": f"Langage non supporté: {language}"}

        if language == "python":
            return self._parse_python(code)
        elif language == "javascript":
            return self._parse_javascript(code)
        elif language == "typescript":
            return self._parse_typescript(code)
        elif language == "php":
            return self._parse_php(code)

    def _detect_language(self, code: str) -> str:
        python_score = 0
        js_score = 0
        ts_score = 0
        php_score = 0

        # PHP detection
        if "<?php" in code:
            php_score += 10
        if "$" in code and ";" in code:
            php_score += 2
        if "namespace " in code and ";" in code:
            php_score += 3
        if "use " in code and "\\" in code and ";" in code:
            php_score += 3
        if "public function " in code or "private function " in code:
            php_score += 3
        if "->" in code or "::" in code:
            php_score += 2
        
        # ... existing python detection ...
        if "def " in code:
            python_score += 2
        if "class " in code and ":" in code:
            python_score += 2
        if "import " in code or "from " in code and " import " in code:
            python_score += 2
        if ":" in code and "#" in code:
            python_score += 1
        if "self." in code:
            python_score += 1

        # ... existing js detection ...
        if "function " in code:
            js_score += 2
        if "const " in code or "let " in code or "var " in code:
            js_score += 2
        if "=> {" in code:
            js_score += 2
        if "{" in code and "}" in code:
            js_score += 1
        if ";" in code:
            js_score += 1
        if "export " in code or "import " in code and " from " in code:
            js_score += 2
        if "console.log" in code:
            js_score += 1
        if "document." in code:
            js_score += 1

        # ... existing ts detection ...
        if "interface " in code:
            ts_score += 3
        if "type " in code and "=" in code and "<" in code and ">" in code:
            ts_score += 3
        if ": " in code and ";" in code:
            ts_score += 2
        if "<" in code and ">" in code and "extends" in code:
            ts_score += 2
        if "implements " in code:
            ts_score += 2
        if "namespace " in code:
            ts_score += 2
        if "enum " in code:
            ts_score += 2

        ts_score += js_score // 2

        scores = {
            "python": python_score,
            "javascript": js_score,
            "typescript": ts_score,
            "php": php_score
        }
        
        max_lang = max(scores, key=scores.get)
        if scores[max_lang] > 0:
            return max_lang

        if ".py" in code.lower():
            return "python"
        elif ".ts" in code.lower() or ".tsx" in code.lower():
            return "typescript"
        elif ".js" in code.lower() or ".jsx" in code.lower():
            return "javascript"
        elif ".php" in code.lower():
            return "php"

        return "unknown"

    def _parse_python(self, code: str) -> Dict[str, Any]:
        try:
            tree = ast.parse(code)

            imports = self._extract_python_imports_ast(tree)
            functions = self._extract_python_functions_ast(tree, code)
            classes = self._extract_python_classes_ast(tree, code)
            variables = self._extract_python_variables_ast(tree, code)

            return {
                "language": "python",
                "imports": imports,
                "functions": functions,
                "classes": classes,
                "variables": variables,
                "raw": code,
                "ast_valid": True
            }
        except SyntaxError:

            return {
                "language": "python",
                "imports": self._extract_python_imports(code),
                "functions": self._extract_python_functions(code),
                "classes": self._extract_python_classes(code),
                "variables": [],
                "raw": code,
                "ast_valid": False,
                "error": "Erreur de syntaxe dans le code Python"
            }

    def _extract_python_imports(self, code: str) -> List[Dict[str, Any]]:
        imports = []
        for i, line in enumerate(code.split("\n")):
            line = line.strip()
            if line.startswith("import "):
                imports.append({
                    "type": "import",
                    "name": line[7:].strip(),
                    "line": i + 1
                })
            elif line.startswith("from ") and " import " in line:
                parts = line.split(" import ")
                imports.append({
                    "type": "from_import",
                    "module": parts[0][5:].strip(),
                    "name": parts[1].strip(),
                    "line": i + 1
                })
        return imports

    def _extract_python_functions(self, code: str) -> List[Dict[str, Any]]:
        functions = []
        lines = code.split("\n")
        for i, line in enumerate(lines):
            line = line.strip()
            if line.startswith("def "):
                name = line.split("def ")[1].split("(")[0].strip()
                functions.append({
                    "name": name,
                    "line": i + 1,
                    "signature": line
                })
        return functions

    def _extract_python_classes(self, code: str) -> List[Dict[str, Any]]:
        classes = []
        lines = code.split("\n")
        for i, line in enumerate(lines):
            line = line.strip()
            if line.startswith("class "):
                name = line.split("class ")[1].split("(")[0].split(":")[0].strip()
                classes.append({
                    "name": name,
                    "line": i + 1,
                    "signature": line
                })
        return classes

    def _parse_php(self, code: str) -> Dict[str, Any]:
        try:
            imports = self._extract_php_imports(code)
            functions = self._extract_php_functions(code)
            classes = self._extract_php_classes(code)
            variables = self._extract_php_variables(code)

            return {
                "language": "php",
                "imports": imports,
                "functions": functions,
                "classes": classes,
                "variables": variables,
                "raw": code,
                "syntax_valid": True
            }
        except Exception as e:
            return {
                "language": "php",
                "imports": [],
                "functions": [],
                "classes": [],
                "variables": [],
                "raw": code,
                "syntax_valid": False,
                "error": f"Erreur lors de l'analyse du code PHP: {str(e)}"
            }

    def _extract_php_imports(self, code: str) -> List[Dict[str, Any]]:
        imports = []
        lines = code.split("\n")
        
        # Pattern pour les imports PHP: use Namespace\Class; ou use Namespace\Class as Alias;
        import_pattern = r"^use\s+([a-zA-Z0-9_\\]+)(?:\s+as\s+([a-zA-Z0-9_]+))?\s*;"
        
        for i, line in enumerate(lines):
            line = line.strip()
            match = re.search(import_pattern, line)
            if match:
                name = match.group(1)
                alias = match.group(2)
                imports.append({
                    "type": "use",
                    "name": name,
                    "alias": alias,
                    "line": i + 1,
                    "statement": line
                })
        return imports

    def _extract_php_functions(self, code: str) -> List[Dict[str, Any]]:
        functions = []
        lines = code.split("\n")
        
        # Pattern pour les fonctions: function name(...) {
        # Pattern pour les méthodes: visibility function name(...) {
        function_pattern = r"(?:(public|protected|private|static)\s+)*function\s+([a-zA-Z0-9_]+)\s*\(([^)]*)\)(?:\s*:\s*([a-zA-Z0-9_\\\?]+))?\s*\{?"
        
        for i, line in enumerate(lines):
            line = line.strip()
            match = re.search(function_pattern, line)
            if match:
                visibility = match.group(1)
                name = match.group(2)
                params = match.group(3).strip()
                return_type = match.group(4)
                
                func_info = {
                    "name": name,
                    "line": i + 1,
                    "signature": line,
                    "params": params,
                    "return_type": return_type if return_type else "mixed"
                }
                
                if visibility:
                    func_info["type"] = "method"
                    func_info["visibility"] = visibility
                else:
                    func_info["type"] = "function"
                    
                functions.append(func_info)
        return functions

    def _extract_php_classes(self, code: str) -> List[Dict[str, Any]]:
        classes = []
        lines = code.split("\n")
        
        # Pattern: class Name extends Parent implements Interface {
        class_pattern = r"(?:abstract\s+|final\s+)?class\s+([a-zA-Z0-9_]+)(?:\s+extends\s+([a-zA-Z0-9_\\]+))?(?:\s+implements\s+([a-zA-Z0-9_\\,\s]+))?\s*\{?"
        interface_pattern = r"interface\s+([a-zA-Z0-9_]+)(?:\s+extends\s+([a-zA-Z0-9_\\,\s]+))?\s*\{?"
        trait_pattern = r"trait\s+([a-zA-Z0-9_]+)\s*\{?"
        
        for i, line in enumerate(lines):
            line = line.strip()
            
            # Class detection
            match = re.search(class_pattern, line)
            if match:
                name = match.group(1)
                parent = match.group(2)
                interfaces = match.group(3)
                
                class_info = {
                    "type": "class",
                    "name": name,
                    "line": i + 1,
                    "signature": line
                }
                if parent:
                    class_info["extends"] = parent
                if interfaces:
                    class_info["implements"] = [i.strip() for i in interfaces.split(",")]
                
                classes.append(class_info)
                continue
                
            # Interface detection
            match = re.search(interface_pattern, line)
            if match:
                name = match.group(1)
                parents = match.group(2)
                
                interface_info = {
                    "type": "interface",
                    "name": name,
                    "line": i + 1,
                    "signature": line
                }
                if parents:
                    interface_info["extends"] = [p.strip() for p in parents.split(",")]
                
                classes.append(interface_info)
                continue
                
            # Trait detection
            match = re.search(trait_pattern, line)
            if match:
                name = match.group(1)
                classes.append({
                    "type": "trait",
                    "name": name,
                    "line": i + 1,
                    "signature": line
                })
                
        return classes

    def _extract_php_variables(self, code: str) -> List[Dict[str, Any]]:
        variables = []
        lines = code.split("\n")
        
        # Pattern: $name = value;
        # Exclut les variables dans les paramètres de fonction ou les boucles si pas d'assignation directe
        var_pattern = r"(\$[a-zA-Z0-9_]+)\s*=\s*(.+?);"
        
        for i, line in enumerate(lines):
            line = line.strip()
            # Ignorer les lignes de commentaires
            if line.startswith("//") or line.startswith("#") or line.startswith("*"):
                continue
                
            match = re.search(var_pattern, line)
            if match:
                name = match.group(1)
                value = match.group(2).strip()
                
                variables.append({
                    "name": name,
                    "line": i + 1,
                    "value": value
                })
                
        return variables

    def _parse_javascript(self, code: str) -> Dict[str, Any]:
        try:
            imports = self._extract_js_imports(code)
            functions = self._extract_js_functions(code)
            classes = self._extract_js_classes(code)
            variables = self._extract_js_variables(code)

            return {
                "language": "javascript",
                "imports": imports,
                "functions": functions,
                "classes": classes,
                "variables": variables,
                "raw": code,
                "syntax_valid": True
            }
        except Exception as e:
            return {
                "language": "javascript",
                "imports": [],
                "functions": [],
                "classes": [],
                "variables": [],
                "raw": code,
                "syntax_valid": False,
                "error": f"Erreur lors de l'analyse du code JavaScript: {str(e)}"
            }

    def _extract_js_imports(self, code: str) -> List[Dict[str, Any]]:
        imports = []
        for i, line in enumerate(code.split("\n")):
            line = line.strip()
            if line.startswith("import "):
                imports.append({
                    "type": "es6_import",
                    "statement": line,
                    "line": i + 1
                })
            elif "require(" in line:
                imports.append({
                    "type": "commonjs_require",
                    "statement": line,
                    "line": i + 1
                })
        return imports

    def _extract_js_functions(self, code: str) -> List[Dict[str, Any]]:
        functions = []
        lines = code.split("\n")

        function_patterns = [

            r"function\s+([a-zA-Z_$][a-zA-Z0-9_$]*)\s*\(([^)]*)\)",

            r"([a-zA-Z_$][a-zA-Z0-9_$]*)\s*\(([^)]*)\)\s*{",

            r"(const|let|var)\s+([a-zA-Z_$][a-zA-Z0-9_$]*)\s*=\s*\(([^)]*)\)\s*=>"
        ]

        for i, line in enumerate(lines):
            line = line.strip()

            match = re.search(function_patterns[0], line)
            if match:
                name = match.group(1)
                params = match.group(2).strip()
                functions.append({
                    "name": name,
                    "line": i + 1,
                    "signature": line,
                    "type": "function_declaration",
                    "params": params
                })
                continue

            match = re.search(function_patterns[1], line)
            if match and not line.startswith("function") and not line.startswith("if") and not line.startswith("while"):
                name = match.group(1)
                params = match.group(2).strip()
                functions.append({
                    "name": name,
                    "line": i + 1,
                    "signature": line,
                    "type": "method",
                    "params": params
                })
                continue

            match = re.search(function_patterns[2], line)
            if match:
                declaration_type = match.group(1)
                name = match.group(2)
                params = match.group(3).strip()
                functions.append({
                    "name": name,
                    "line": i + 1,
                    "signature": line,
                    "type": "arrow_function",
                    "declaration_type": declaration_type,
                    "params": params
                })

        return functions

    def _extract_js_classes(self, code: str) -> List[Dict[str, Any]]:
        classes = []
        lines = code.split("\n")

        class_pattern = r"class\s+([a-zA-Z_$][a-zA-Z0-9_$]*)\s*(?:extends\s+([a-zA-Z_$][a-zA-Z0-9_$]*))?(?:\s*{)?"

        for i, line in enumerate(lines):
            line = line.strip()
            match = re.search(class_pattern, line)
            if match:
                name = match.group(1)
                parent = match.group(2)
                classes.append({
                    "name": name,
                    "line": i + 1,
                    "signature": line,
                    "extends": parent
                })

        return classes

    def _extract_js_variables(self, code: str) -> List[Dict[str, Any]]:
        variables = []
        lines = code.split("\n")

        var_pattern = r"(const|let|var)\s+([a-zA-Z_$][a-zA-Z0-9_$]*)\s*=\s*(.+?)(?:;|$)"

        for i, line in enumerate(lines):
            line = line.strip()
            match = re.search(var_pattern, line)
            if match:
                declaration_type = match.group(1)
                name = match.group(2)
                value = match.group(3).strip()
                variables.append({
                    "name": name,
                    "line": i + 1,
                    "declaration_type": declaration_type,
                    "value": value
                })

        return variables

    def _parse_typescript(self, code: str) -> Dict[str, Any]:
        try:
            imports = self._extract_ts_imports(code)
            functions = self._extract_ts_functions(code)
            classes = self._extract_ts_classes(code)
            interfaces = self._extract_ts_interfaces(code)
            types = self._extract_ts_types(code)
            variables = self._extract_ts_variables(code)

            return {
                "language": "typescript",
                "imports": imports,
                "functions": functions,
                "classes": classes,
                "interfaces": interfaces,
                "types": types,
                "variables": variables,
                "raw": code,
                "syntax_valid": True
            }
        except Exception as e:
            return {
                "language": "typescript",
                "imports": [],
                "functions": [],
                "classes": [],
                "interfaces": [],
                "types": [],
                "variables": [],
                "raw": code,
                "syntax_valid": False,
                "error": f"Erreur lors de l'analyse du code TypeScript: {str(e)}"
            }

    def _extract_ts_imports(self, code: str) -> List[Dict[str, Any]]:
        imports = []
        for i, line in enumerate(code.split("\n")):
            line = line.strip()
            if line.startswith("import "):
                imports.append({
                    "type": "es6_import",
                    "statement": line,
                    "line": i + 1
                })
            elif "require(" in line:
                imports.append({
                    "type": "commonjs_require",
                    "statement": line,
                    "line": i + 1
                })
        return imports

    def _extract_ts_functions(self, code: str) -> List[Dict[str, Any]]:
        functions = []
        lines = code.split("\n")


        function_patterns = [

            r"function\s+([a-zA-Z_$][a-zA-Z0-9_$]*)\s*(?:<[^>]*>)?\s*\(([^)]*)\)(?:\s*:\s*([a-zA-Z_$][a-zA-Z0-9_$<>\.]*))?\s*{?",

            r"(?:public|private|protected)?\s*(?:static)?\s*([a-zA-Z_$][a-zA-Z0-9_$]*)\s*(?:<[^>]*>)?\s*\(([^)]*)\)(?:\s*:\s*([a-zA-Z_$][a-zA-Z0-9_$<>\.]*))?\s*{?",

            r"(const|let|var)\s+([a-zA-Z_$][a-zA-Z0-9_$]*)\s*=\s*(?:<[^>]*>)?\s*\(([^)]*)\)(?:\s*:\s*([a-zA-Z_$][a-zA-Z0-9_$<>\.]*))?\s*=>"
        ]

        for i, line in enumerate(lines):
            line = line.strip()

            match = re.search(function_patterns[0], line)
            if match:
                name = match.group(1)
                params = match.group(2).strip()
                return_type = match.group(3) if match.group(3) else "any"
                functions.append({
                    "name": name,
                    "line": i + 1,
                    "signature": line,
                    "type": "function_declaration",
                    "params": params,
                    "return_type": return_type
                })
                continue

            match = re.search(function_patterns[1], line)
            if match and not line.startswith("function") and not line.startswith("if") and not line.startswith("while"):
                name = match.group(1)
                params = match.group(2).strip()
                return_type = match.group(3) if match.group(3) else "any"
                functions.append({
                    "name": name,
                    "line": i + 1,
                    "signature": line,
                    "type": "method",
                    "params": params,
                    "return_type": return_type
                })
                continue

            match = re.search(function_patterns[2], line)
            if match:
                declaration_type = match.group(1)
                name = match.group(2)
                params = match.group(3).strip()
                return_type = match.group(4) if match.group(4) else "any"
                functions.append({
                    "name": name,
                    "line": i + 1,
                    "signature": line,
                    "type": "arrow_function",
                    "declaration_type": declaration_type,
                    "params": params,
                    "return_type": return_type
                })

        return functions

    def _extract_ts_classes(self, code: str) -> List[Dict[str, Any]]:
        classes = []
        lines = code.split("\n")

        class_pattern = r"(?:export\s+)?class\s+([a-zA-Z_$][a-zA-Z0-9_$]*)(?:<([^>]*)>)?(?:\s+extends\s+([a-zA-Z_$][a-zA-Z0-9_$<>\.]*))?\s*(?:implements\s+([a-zA-Z_$][a-zA-Z0-9_$<>\.,\s]*))?(?:\s*{)?"

        for i, line in enumerate(lines):
            line = line.strip()
            match = re.search(class_pattern, line)
            if match:
                name = match.group(1)
                generics = match.group(2)
                parent = match.group(3)
                implements = match.group(4)

                class_info = {
                    "name": name,
                    "line": i + 1,
                    "signature": line,
                }

                if generics:
                    class_info["generics"] = generics.strip()
                if parent:
                    class_info["extends"] = parent.strip()
                if implements:
                    class_info["implements"] = [impl.strip() for impl in implements.split(",")]

                classes.append(class_info)

        return classes

    def _extract_ts_interfaces(self, code: str) -> List[Dict[str, Any]]:
        interfaces = []
        lines = code.split("\n")

        interface_pattern = r"(?:export\s+)?interface\s+([a-zA-Z_$][a-zA-Z0-9_$]*)(?:<([^>]*)>)?(?:\s+extends\s+([a-zA-Z_$][a-zA-Z0-9_$<>\.,\s]*))?(?:\s*{)?"

        for i, line in enumerate(lines):
            line = line.strip()
            match = re.search(interface_pattern, line)
            if match:
                name = match.group(1)
                generics = match.group(2)
                extends = match.group(3)

                interface_info = {
                    "name": name,
                    "line": i + 1,
                    "signature": line,
                }

                if generics:
                    interface_info["generics"] = generics.strip()
                if extends:
                    interface_info["extends"] = [ext.strip() for ext in extends.split(",")]

                interfaces.append(interface_info)

        return interfaces

    def _extract_ts_types(self, code: str) -> List[Dict[str, Any]]:
        types = []
        lines = code.split("\n")

        type_pattern = r"(?:export\s+)?type\s+([a-zA-Z_$][a-zA-Z0-9_$]*)(?:<([^>]*)>)?\s*=\s*(.+?)(?:;|$)"

        for i, line in enumerate(lines):
            line = line.strip()
            match = re.search(type_pattern, line)
            if match:
                name = match.group(1)
                generics = match.group(2)
                definition = match.group(3)

                type_info = {
                    "name": name,
                    "line": i + 1,
                    "signature": line,
                    "definition": definition.strip()
                }

                if generics:
                    type_info["generics"] = generics.strip()

                types.append(type_info)

        return types

    def _extract_ts_variables(self, code: str) -> List[Dict[str, Any]]:
        variables = []
        lines = code.split("\n")

        var_pattern = r"(const|let|var)\s+([a-zA-Z_$][a-zA-Z0-9_$]*)(?::\s*([a-zA-Z_$][a-zA-Z0-9_$<>\[\]\.]*))?\s*=\s*(.+?)(?:;|$)"

        for i, line in enumerate(lines):
            line = line.strip()
            match = re.search(var_pattern, line)
            if match:
                declaration_type = match.group(1)
                name = match.group(2)
                var_type = match.group(3)
                value = match.group(4).strip()

                var_info = {
                    "name": name,
                    "line": i + 1,
                    "declaration_type": declaration_type,
                    "value": value
                }

                if var_type:
                    var_info["type"] = var_type

                variables.append(var_info)

        return variables

    def _extract_python_imports_ast(self, tree: ast.AST) -> List[Dict[str, Any]]:
        imports = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for name in node.names:
                    imports.append({
                        "type": "import",
                        "name": name.name,
                        "alias": name.asname,
                        "line": node.lineno
                    })
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for name in node.names:
                    imports.append({
                        "type": "from_import",
                        "module": module,
                        "name": name.name,
                        "alias": name.asname,
                        "line": node.lineno
                    })

        return imports

    def _extract_python_functions_ast(self, tree: ast.AST, code: str) -> List[Dict[str, Any]]:
        functions = []

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                params = []
                defaults = [None] * (len(node.args.args) - len(node.args.defaults)) + list(node.args.defaults)

                for arg, default in zip(node.args.args, defaults):
                    param_info = {
                        "name": arg.arg,
                        "type": None,
                        "default": None
                    }

                    if arg.annotation:
                        if isinstance(arg.annotation, ast.Name):
                            param_info["type"] = arg.annotation.id
                        elif isinstance(arg.annotation, ast.Attribute):
                            param_info["type"] = f"{arg.annotation.value.id}.{arg.annotation.attr}"

                    if default:
                        if isinstance(default, ast.Constant):
                            param_info["default"] = default.value
                        elif isinstance(default, ast.Name):
                            param_info["default"] = default.id

                    params.append(param_info)

                return_type = None
                if node.returns:
                    if isinstance(node.returns, ast.Name):
                        return_type = node.returns.id
                    elif isinstance(node.returns, ast.Attribute):
                        return_type = f"{node.returns.value.id}.{node.returns.attr}"

                docstring = ast.get_docstring(node)

                function_body = ""
                if len(node.body) > 0:
                    start_line = node.body[0].lineno
                    end_line = node.body[-1].lineno
                    function_body_lines = code.split('\n')[start_line-1:end_line]
                    function_body = '\n'.join(function_body_lines)

                functions.append({
                    "name": node.name,
                    "params": params,
                    "return_type": return_type,
                    "docstring": docstring,
                    "line": node.lineno,
                    "body": function_body,
                    "is_method": False
                })

        return functions

    def _extract_python_classes_ast(self, tree: ast.AST, code: str) -> List[Dict[str, Any]]:
        classes = []

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = []
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        bases.append(base.id)
                    elif isinstance(base, ast.Attribute):
                        bases.append(f"{base.value.id}.{base.attr}")

                methods = []
                for child in node.body:
                    if isinstance(child, ast.FunctionDef):
                        params = []
                        defaults = [None] * (len(child.args.args) - len(child.args.defaults)) + list(child.args.defaults)

                        for i, (arg, default) in enumerate(zip(child.args.args, defaults)):
                            if i == 0 and arg.arg == 'self':
                                continue

                            param_info = {
                                "name": arg.arg,
                                "type": None,
                                "default": None
                            }

                            if arg.annotation:
                                if isinstance(arg.annotation, ast.Name):
                                    param_info["type"] = arg.annotation.id
                                elif isinstance(arg.annotation, ast.Attribute):
                                    param_info["type"] = f"{arg.annotation.value.id}.{arg.annotation.attr}"

                            if default:
                                if isinstance(default, ast.Constant):
                                    param_info["default"] = default.value
                                elif isinstance(default, ast.Name):
                                    param_info["default"] = default.id

                            params.append(param_info)

                        return_type = None
                        if child.returns:
                            if isinstance(child.returns, ast.Name):
                                return_type = child.returns.id
                            elif isinstance(child.returns, ast.Attribute):
                                return_type = f"{child.returns.value.id}.{child.returns.attr}"

                        docstring = ast.get_docstring(child)

                        methods.append({
                            "name": child.name,
                            "params": params,
                            "return_type": return_type,
                            "docstring": docstring,
                            "line": child.lineno,
                            "is_method": True
                        })

                attributes = []
                for child in node.body:
                    if isinstance(child, ast.Assign):
                        for target in child.targets:
                            if isinstance(target, ast.Name):
                                attributes.append({
                                    "name": target.id,
                                    "line": child.lineno
                                })

                docstring = ast.get_docstring(node)

                classes.append({
                    "name": node.name,
                    "bases": bases,
                    "methods": methods,
                    "attributes": attributes,
                    "docstring": docstring,
                    "line": node.lineno
                })

        return classes

    def _extract_python_variables_ast(self, tree: ast.AST, code: str) -> List[Dict[str, Any]]:
        variables = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        var_info = {
                            "name": target.id,
                            "line": node.lineno,
                            "value": None
                        }


                        if isinstance(node.value, ast.Constant):
                            var_info["value"] = node.value.value

                        variables.append(var_info)

        return variables
