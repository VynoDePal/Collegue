"""
Code Parser - Analyse syntaxique du code pour différents langages
"""
import ast
import re
from typing import Dict, List, Any, Optional, Tuple

class CodeParser:
    """
    Analyse le code source pour construire une représentation structurée.
    Cette classe est responsable de l'analyse syntaxique du code dans différents langages.
    """
    
    def __init__(self):
        self.supported_languages = ["python", "javascript", "typescript"]
    
    def parse(self, code: str, language: str = None) -> Dict[str, Any]:
        """
        Analyse un extrait de code et retourne sa structure.
        
        Args:
            code (str): Le code source à analyser
            language (str, optional): Le langage du code. Si None, tente de le détecter.
            
        Returns:
            dict: La représentation structurée du code
        """
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
    
    def _detect_language(self, code: str) -> str:
        """
        Tente de détecter le langage du code.
        
        Args:
            code (str): Le code source
            
        Returns:
            str: Le langage détecté ou "unknown"
        """
        # Implémentation améliorée de détection basée sur des heuristiques
        python_score = 0
        js_score = 0
        ts_score = 0
        
        # Motifs Python
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
        
        # Motifs JavaScript
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
        
        # Motifs TypeScript (en plus des motifs JavaScript)
        if "interface " in code:
            ts_score += 3
        if "type " in code and "=" in code and "<" in code and ">" in code:
            ts_score += 3
        if ": " in code and ";" in code:  # Annotations de type
            ts_score += 2
        if "<" in code and ">" in code and "extends" in code:  # Generics
            ts_score += 2
        if "implements " in code:
            ts_score += 2
        if "namespace " in code:
            ts_score += 2
        if "enum " in code:
            ts_score += 2
        
        # Ajout des scores JavaScript à TypeScript car TypeScript est un surensemble de JavaScript
        ts_score += js_score // 2  # On ajoute seulement la moitié pour éviter de trop favoriser TypeScript
        
        if python_score > js_score and python_score > ts_score:
            return "python"
        elif ts_score > js_score and ts_score > python_score:
            return "typescript"
        elif js_score > python_score and js_score >= ts_score:  # Changé pour >= au lieu de >
            return "javascript"
        
        if ".py" in code.lower():
            return "python"
        elif ".ts" in code.lower() or ".tsx" in code.lower():
            return "typescript"
        elif ".js" in code.lower() or ".jsx" in code.lower():
            return "javascript"
            
        return "unknown"
    
    def _parse_python(self, code: str) -> Dict[str, Any]:
        """
        Analyse du code Python en utilisant l'AST.
        
        Args:
            code (str): Le code source Python
            
        Returns:
            dict: La représentation structurée du code
        """
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
            # Fallback à l'analyse basique si le code contient des erreurs de syntaxe
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
        """Extrait les imports Python avec une méthode basique."""
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
        """Extrait les fonctions Python avec une méthode basique."""
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
        """Extrait les classes Python avec une méthode basique."""
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
            
    def _parse_javascript(self, code: str) -> Dict[str, Any]:
        """
        Analyse du code JavaScript en utilisant des expressions régulières.
        
        Args:
            code (str): Le code source JavaScript
            
        Returns:
            dict: La représentation structurée du code
        """
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
        """Extrait les imports JavaScript."""
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
        """Extrait les fonctions JavaScript avec une méthode améliorée."""
        functions = []
        lines = code.split("\n")
        
        # Regex pour les différents types de fonctions JavaScript
        function_patterns = [
            # Fonction standard: function name() {}
            r"function\s+([a-zA-Z_$][a-zA-Z0-9_$]*)\s*\(([^)]*)\)",
            # Méthode de classe: methodName() {}
            r"([a-zA-Z_$][a-zA-Z0-9_$]*)\s*\(([^)]*)\)\s*{",
            # Fonction fléchée: const name = () => {}
            r"(const|let|var)\s+([a-zA-Z_$][a-zA-Z0-9_$]*)\s*=\s*\(([^)]*)\)\s*=>"
        ]
        
        for i, line in enumerate(lines):
            line = line.strip()
            
            # Fonction standard
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
            
            # Méthode de classe
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
            
            # Fonction fléchée
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
        """Extrait les classes JavaScript avec une méthode améliorée."""
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
        """Extrait les variables JavaScript."""
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
        """
        Analyse du code TypeScript en utilisant des expressions régulières.
        
        Args:
            code (str): Le code source TypeScript
            
        Returns:
            dict: La représentation structurée du code
        """
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
        """Extrait les imports TypeScript (similaires à JavaScript)."""
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
        """Extrait les fonctions TypeScript avec support des annotations de type."""
        functions = []
        lines = code.split("\n")
        
        # Regex pour les différents types de fonctions TypeScript
        function_patterns = [
            # Fonction standard: function name(param: type): returnType {}
            r"function\s+([a-zA-Z_$][a-zA-Z0-9_$]*)\s*(?:<[^>]*>)?\s*\(([^)]*)\)(?:\s*:\s*([a-zA-Z_$][a-zA-Z0-9_$<>\.]*))?\s*{?",
            # Méthode de classe: methodName(param: type): returnType {}
            r"(?:public|private|protected)?\s*(?:static)?\s*([a-zA-Z_$][a-zA-Z0-9_$]*)\s*(?:<[^>]*>)?\s*\(([^)]*)\)(?:\s*:\s*([a-zA-Z_$][a-zA-Z0-9_$<>\.]*))?\s*{?",
            # Fonction fléchée: const name = (param: type): returnType => {}
            r"(const|let|var)\s+([a-zA-Z_$][a-zA-Z0-9_$]*)\s*=\s*(?:<[^>]*>)?\s*\(([^)]*)\)(?:\s*:\s*([a-zA-Z_$][a-zA-Z0-9_$<>\.]*))?\s*=>"
        ]
        
        for i, line in enumerate(lines):
            line = line.strip()
            
            # Fonction standard
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
            
            # Méthode de classe
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
            
            # Fonction fléchée
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
        """Extrait les classes TypeScript avec support des interfaces et generics."""
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
        """Extrait les interfaces TypeScript."""
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
        """Extrait les définitions de types TypeScript."""
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
        """Extrait les variables TypeScript avec support des annotations de type."""
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
        """
        Extrait les imports Python à partir de l'AST.
        
        Args:
            tree (ast.AST): L'arbre syntaxique abstrait du code Python
            
        Returns:
            List[Dict[str, Any]]: Liste des imports avec leurs attributs
        """
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
                        "type": "from_import",  # Changé de "import_from" à "from_import"
                        "module": module,
                        "name": name.name,
                        "alias": name.asname,
                        "line": node.lineno
                    })
        
        return imports

    def _extract_python_functions_ast(self, tree: ast.AST, code: str) -> List[Dict[str, Any]]:
        """
        Extrait les fonctions Python à partir de l'AST.
        
        Args:
            tree (ast.AST): L'arbre syntaxique abstrait du code Python
            code (str): Le code source Python
            
        Returns:
            List[Dict[str, Any]]: Liste des fonctions avec leurs attributs
        """
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
                    "is_method": False  # Sera mis à jour plus tard pour les méthodes de classe
                })
        
        return functions
    
    def _extract_python_classes_ast(self, tree: ast.AST, code: str) -> List[Dict[str, Any]]:
        """
        Extrait les classes Python à partir de l'AST.
        
        Args:
            tree (ast.AST): L'arbre syntaxique abstrait du code Python
            code (str): Le code source Python
            
        Returns:
            List[Dict[str, Any]]: Liste des classes avec leurs attributs
        """
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
                
                # Extraire la docstring
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
        """
        Extrait les variables Python à partir de l'AST.
        
        Args:
            tree (ast.AST): L'arbre syntaxique abstrait du code Python
            code (str): Le code source Python
            
        Returns:
            List[Dict[str, Any]]: Liste des variables avec leurs attributs
        """
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
                        
                        # Extraire la valeur si c'est une constante
                        if isinstance(node.value, ast.Constant):
                            var_info["value"] = node.value.value
                        
                        variables.append(var_info)
        
        return variables
