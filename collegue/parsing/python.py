import ast
import re
from typing import List, Dict, Any, Optional, Tuple
from .base import (
    Import, Declaration, ParseResult, BaseParser,
    ImportType, DeclarationType
)
class PythonParser(BaseParser):
    def __init__(self, content: str, filename: Optional[str] = None):
        super().__init__(content, filename)
        self._tree: Optional[ast.AST] = None
        self._ast_valid = True
        self._cache_imports: Optional[List[Import]] = None
        self._cache_declarations: Optional[Dict[str, Declaration]] = None
        self._cache_identifiers: Optional[List[Tuple[int, str]]] = None
        self._parse_ast()
    def _parse_ast(self) -> None:
        try:
            self._tree = ast.parse(self.content)
            self._ast_valid = True
        except SyntaxError as e:
            self._tree = None
            self._ast_valid = False
    def find_imports(self) -> List[Import]:
        if self._cache_imports is not None:
            return self._cache_imports
        imports = []
        if not self._tree:
            return self._find_imports_regex()
        for node in ast.walk(self._tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(Import(
                        source=alias.name,
                        names=[(alias.name, alias.asname)],
                        line=node.lineno if hasattr(node, 'lineno') else 0,
                        import_type=ImportType.IMPORT,
                        is_relative=False
                    ))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ''
                names = [(alias.name, alias.asname) for alias in node.names]
                imports.append(Import(
                    source=module,
                    names=names,
                    line=node.lineno if hasattr(node, 'lineno') else 0,
                    import_type=ImportType.FROM_IMPORT,
                    is_relative=node.level > 0 if hasattr(node, 'level') else False
                ))
        self._cache_imports = imports
        return imports
    def _find_imports_regex(self) -> List[Import]:
        imports = []
        import_pattern = r'^import\s+([\w.]+)(?:\s+as\s+(\w+))?'
        for match in re.finditer(import_pattern, self.content, re.MULTILINE):
            name = match.group(1)
            alias = match.group(2)
            line = self.content[:match.start()].count('\n') + 1
            imports.append(Import(
                source=name,
                names=[(name, alias)],
                line=line,
                import_type=ImportType.IMPORT,
                is_relative=False
            ))
        from_import_pattern = r'^from\s+(\.{1,3}[\w.]*|[\w.]+)\s+import\s+([^\n]+)'
        for match in re.finditer(from_import_pattern, self.content, re.MULTILINE):
            module = match.group(1) or ''
            names_str = match.group(2)
            line = self.content[:match.start()].count('\n') + 1
            names = []
            for name_part in names_str.split(','):
                name_part = name_part.strip()
                if ' as ' in name_part:
                    name, alias = name_part.split(' as ', 1)
                    names.append((name.strip(), alias.strip()))
                else:
                    names.append((name_part, None))
            imports.append(Import(
                source=module,
                names=names,
                line=line,
                import_type=ImportType.FROM_IMPORT,
                is_relative=module.startswith('.')
            ))
        return imports
    def find_declarations(self) -> Dict[str, Declaration]:
        if self._cache_declarations is not None:
            return self._cache_declarations
        declarations = {}
        if not self._tree:
            return self._find_declarations_regex()
        for node in ast.walk(self._tree):
            if isinstance(node, ast.FunctionDef):
                signature = self._get_function_signature(node)
                declarations[node.name] = Declaration(
                    name=node.name,
                    decl_type=DeclarationType.FUNCTION,
                    line=node.lineno if hasattr(node, 'lineno') else 0,
                    kind='function',
                    signature=signature
                )
            elif isinstance(node, ast.ClassDef):
                declarations[node.name] = Declaration(
                    name=node.name,
                    decl_type=DeclarationType.CLASS,
                    line=node.lineno if hasattr(node, 'lineno') else 0,
                    kind='class'
                )
            elif isinstance(node, ast.AsyncFunctionDef):
                signature = self._get_function_signature(node)
                declarations[node.name] = Declaration(
                    name=node.name,
                    decl_type=DeclarationType.FUNCTION,
                    line=node.lineno if hasattr(node, 'lineno') else 0,
                    kind='async function',
                    signature=signature
                )
        declarations.update(self._find_variables_ast())
        self._cache_declarations = declarations
        return declarations
    def _find_variables_ast(self) -> Dict[str, Declaration]:
        variables = {}
        if not self._tree:
            return variables
        for node in ast.walk(self._tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        if target.id not in variables:
                            variables[target.id] = Declaration(
                                name=target.id,
                                decl_type=DeclarationType.VARIABLE,
                                line=node.lineno if hasattr(node, 'lineno') else 0,
                                kind='variable'
                            )
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name):
                    if node.target.id not in variables:
                        variables[node.target.id] = Declaration(
                            name=node.target.id,
                            decl_type=DeclarationType.VARIABLE,
                            line=node.lineno if hasattr(node, 'lineno') else 0,
                            kind='annotated variable'
                        )
        return variables
    def _find_declarations_regex(self) -> Dict[str, Declaration]:
        declarations = {}
        func_pattern = r'^def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\('
        for match in re.finditer(func_pattern, self.content, re.MULTILINE):
            name = match.group(1)
            line = self.content[:match.start()].count('\n') + 1
            declarations[name] = Declaration(
                name=name,
                decl_type=DeclarationType.FUNCTION,
                line=line,
                kind='function'
            )
        class_pattern = r'^class\s+([a-zA-Z_][a-zA-Z0-9_]*)'
        for match in re.finditer(class_pattern, self.content, re.MULTILINE):
            name = match.group(1)
            line = self.content[:match.start()].count('\n') + 1
            declarations[name] = Declaration(
                name=name,
                decl_type=DeclarationType.CLASS,
                line=line,
                kind='class'
            )
        return declarations
    def _get_function_signature(self, node) -> str:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return ""
        name = node.name
        args = []
        for arg in node.args.args:
            arg_str = arg.arg
            if arg.annotation and isinstance(arg.annotation, ast.Name):
                arg_str += f": {arg.annotation.id}"
            args.append(arg_str)
        if node.args.vararg:
            args.append(f"*{node.args.vararg.arg}")
        if node.args.kwarg:
            args.append(f"**{node.args.kwarg.arg}")
        return_type = ""
        if node.returns and isinstance(node.returns, ast.Name):
            return_type = f" -> {node.returns.id}"
        async_prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
        return f"{async_prefix}def {name}({', '.join(args)}){return_type}"
    def find_identifiers(self) -> List[Tuple[int, str]]:
        if self._cache_identifiers is not None:
            return self._cache_identifiers
        identifiers = []
        if not self._tree:
            return self._find_identifiers_regex()
        declared_names = set(self.find_declarations().keys())
        declared_names.update(imp.source.split('.')[-1] for imp in self.find_imports())
        for node in ast.walk(self._tree):
            if isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Load) and node.id not in declared_names:
                    line = node.lineno if hasattr(node, 'lineno') else 0
                    identifiers.append((line, node.id))
        self._cache_identifiers = identifiers
        return identifiers
    def _find_identifiers_regex(self) -> List[Tuple[int, str]]:
        identifiers = []
        name_pattern = r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b'
        lines = self.content.split('\n')
        for i, line in enumerate(lines, 1):
            for match in re.finditer(name_pattern, line):
                name = match.group(1)
                if name not in {'if', 'else', 'elif', 'for', 'while', 'return', 'def', 'class', 'import', 'from', 'as', 'try', 'except', 'finally', 'with', 'lambda', 'and', 'or', 'not', 'in', 'is', 'True', 'False', 'None'}:
                    identifiers.append((i, name))
        return identifiers
    def _detect_language(self) -> str:
        return 'python'
    def parse(self) -> ParseResult:
        return ParseResult(
            language='python',
            imports=self.find_imports(),
            declarations=self.find_declarations(),
            identifiers=self.find_identifiers(),
            ast_valid=self._ast_valid,
            errors=[] if self._ast_valid else ["SyntaxError dans le code Python"],
            raw=self.content,
        )