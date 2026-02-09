import re
from typing import List, Dict, Any, Optional, Tuple
from .base import (
    Import, Declaration, ParseResult, BaseParser,
    ImportType, DeclarationType
)
class JSParser(BaseParser):
    RESERVED_KEYWORDS = {
        'break', 'case', 'catch', 'class', 'const', 'continue', 'debugger', 'default',
        'delete', 'do', 'else', 'export', 'extends', 'finally', 'for', 'function',
        'if', 'import', 'in', 'instanceof', 'new', 'return', 'super', 'switch',
        'this', 'throw', 'try', 'typeof', 'var', 'void', 'while', 'with', 'yield',
        'let', 'static', 'enum', 'await', 'implements', 'package', 'protected',
        'interface', 'private', 'public', 'abstract', 'readonly', 'as', 'from',
        'type', 'namespace', 'declare', 'module', 'get', 'set', 'async',
    }
    BUILTIN_TYPES = {
        'string', 'number', 'boolean', 'symbol', 'bigint', 'undefined', 'null',
        'object', 'any', 'unknown', 'never', 'void', 'Array', 'Record', 'Partial',
        'Required', 'Readonly', 'Pick', 'Omit', 'Exclude', 'Extract', 'NonNullable',
        'Parameters', 'ReturnType', 'InstanceType', 'ThisParameterType', 'OmitThisParameter',
        'ThisType', 'Uppercase', 'Lowercase', 'Capitalize', 'Uncapitalize',
        'Promise', 'Map', 'Set', 'WeakMap', 'WeakSet', 'Date', 'RegExp', 'Error',
        'Function', 'String', 'Number', 'Boolean', 'Object', 'Array', 'console',
        'window', 'document', 'process', 'Buffer', 'Math', 'JSON',
    }
    def __init__(self, content: str, filename: Optional[str] = None):
        super().__init__(content, filename)
        self.tokens = self._tokenize()
    def _tokenize(self) -> List[Tuple[str, int, int, str]]:
        tokens = []
        line = 1
        col = 1
        i = 0
        content = self.content
        while i < len(content):
            char = content[i]
            if char in ' \t':
                col += 1
                i += 1
                continue
            if char == '\n':
                line += 1
                col = 1
                i += 1
                continue
            if char == '/':
                if i + 1 < len(content) and content[i + 1] == '/':
                    while i < len(content) and content[i] != '\n':
                        i += 1
                    continue
                if i + 1 < len(content) and content[i + 1] == '*':
                    i += 2
                    while i < len(content) - 1 and not (content[i] == '*' and content[i + 1] == '/'):
                        if content[i] == '\n':
                            line += 1
                            col = 1
                        else:
                            col += 1
                        i += 1
                    i += 2
                    continue
                is_regex = True
                if tokens:
                    prev_type = tokens[-1][0]
                    if prev_type in ('IDENT', 'NUMBER', ')', ']'):
                        is_regex = False
                if is_regex and i + 1 < len(content) and content[i + 1] not in ('=', ' ', '\n', '\t'):
                    start_line, start_col = line, col
                    value = char
                    i += 1
                    col += 1
                    while i < len(content) and content[i] != '/':
                        if content[i] == '\\':
                            value += content[i:i+2]
                            i += 2
                            col += 2
                        elif content[i] == '\n':
                            break
                        else:
                            value += content[i]
                            i += 1
                            col += 1
                    if i < len(content) and content[i] == '/':
                        value += content[i]
                        i += 1
                        col += 1
                        while i < len(content) and content[i].isalpha():
                            value += content[i]
                            i += 1
                            col += 1
                        tokens.append(('STRING', start_line, start_col, value))
                        continue
                start_line, start_col = line, col
                value = '/'
                i += 1
                col += 1
                if i < len(content) and content[i] == '=':
                    value += '='
                    i += 1
                    col += 1
                tokens.append(('OP', start_line, start_col, value))
                continue
            if char in '"\'':
                quote = char
                start_line, start_col = line, col
                value = char
                i += 1
                col += 1
                while i < len(content) and content[i] != quote:
                    if content[i] == '\\':
                        value += content[i:i+2]
                        i += 2
                        col += 2
                    elif content[i] == '\n':
                        value += content[i]
                        line += 1
                        col = 1
                        i += 1
                    else:
                        value += content[i]
                        i += 1
                        col += 1
                if i < len(content):
                    value += content[i]
                    i += 1
                    col += 1
                tokens.append(('STRING', start_line, start_col, value))
                continue
            if char == '`':
                start_line, start_col = line, col
                value = char
                i += 1
                col += 1
                depth = 0
                while i < len(content):
                    c = content[i]
                    if c == '\\':
                        value += content[i:i+2]
                        i += 2
                        col += 2
                    elif c == '$' and i + 1 < len(content) and content[i + 1] == '{':
                        value += '${'
                        i += 2
                        col += 2
                        depth += 1
                    elif c == '{' and depth > 0:
                        value += c
                        i += 1
                        col += 1
                        depth += 1
                    elif c == '}' and depth > 0:
                        value += c
                        i += 1
                        col += 1
                        depth -= 1
                    elif c == '`' and depth == 0:
                        value += c
                        i += 1
                        col += 1
                        break
                    elif c == '\n':
                        value += c
                        line += 1
                        col = 1
                        i += 1
                    else:
                        value += c
                        i += 1
                        col += 1
                tokens.append(('STRING', start_line, start_col, value))
                continue
            if char.isalpha() or char == '_' or char == '$':
                start_line, start_col = line, col
                value = ''
                while i < len(content) and (content[i].isalnum() or content[i] in '_$'):
                    value += content[i]
                    i += 1
                    col += 1
                if value in self.RESERVED_KEYWORDS:
                    tokens.append(('KEYWORD', start_line, start_col, value))
                else:
                    tokens.append(('IDENT', start_line, start_col, value))
                continue
            start_line, start_col = line, col
            if char in '{}[](),;:':
                tokens.append((char, start_line, start_col, char))
                i += 1
                col += 1
            elif char in '+-*/%=!<>&|^~':
                value = char
                i += 1
                col += 1
                if i < len(content) and content[i] in '=+-':
                    value += content[i]
                    i += 1
                    col += 1
                tokens.append(('OP', start_line, start_col, value))
            else:
                i += 1
                col += 1
        return tokens
    def find_imports(self) -> List[Import]:
        imports = []
        i = 0
        while i < len(self.tokens):
            token = self.tokens[i]
            if token[0] == 'KEYWORD' and token[3] == 'import':
                line = token[1]
                if i + 1 < len(self.tokens):
                    next_tok = self.tokens[i + 1]
                    if next_tok[0] == 'OP' and next_tok[3] == '*':
                        if i + 3 < len(self.tokens) and self.tokens[i + 2][0] == 'KEYWORD' and self.tokens[i + 3][0] == 'IDENT':
                            alias = self.tokens[i + 3][3]
                            j = i + 4
                            while j < len(self.tokens) and not (self.tokens[j][0] == 'KEYWORD' and self.tokens[j][3] == 'from'):
                                j += 1
                            if j + 1 < len(self.tokens) and self.tokens[j + 1][0] == 'STRING':
                                source = self.tokens[j + 1][3].strip('"\'')
                                imports.append(Import(
                                    source=source,
                                    names=[('*', alias)],
                                    line=line,
                                    import_type=ImportType.NAMESPACE,
                                    is_relative=source.startswith(('./', '../'))
                                ))
                    elif next_tok[0] == '{':
                        names = []
                        j = i + 2
                        while j < len(self.tokens) and self.tokens[j][0] != '}':
                            if self.tokens[j][0] == 'IDENT':
                                name = self.tokens[j][3]
                                alias = None
                                if j + 1 < len(self.tokens) and self.tokens[j + 1][0] == 'KEYWORD' and self.tokens[j + 1][3] == 'as':
                                    if j + 2 < len(self.tokens) and self.tokens[j + 2][0] == 'IDENT':
                                        alias = self.tokens[j + 2][3]
                                        j += 2
                                names.append((name, alias))
                            j += 1
                        j += 1
                        while j < len(self.tokens) and not (self.tokens[j][0] == 'KEYWORD' and self.tokens[j][3] == 'from'):
                            j += 1
                        if j + 1 < len(self.tokens) and self.tokens[j + 1][0] == 'STRING':
                            source = self.tokens[j + 1][3].strip('"\'')
                            imports.append(Import(
                                source=source,
                                names=names,
                                line=line,
                                import_type=ImportType.NAMED,
                                is_relative=source.startswith(('./', '../'))
                            ))
                    elif next_tok[0] == 'IDENT':
                        default_name = next_tok[3]
                        j = i + 2
                        named_names = []
                        if j < len(self.tokens) and self.tokens[j][0] == ',':
                            j += 1
                            if j < len(self.tokens) and self.tokens[j][0] == '{':
                                j += 1
                                while j < len(self.tokens) and self.tokens[j][0] != '}':
                                    if self.tokens[j][0] == 'IDENT':
                                        n = self.tokens[j][3]
                                        a = None
                                        if j + 1 < len(self.tokens) and self.tokens[j + 1][0] == 'KEYWORD' and self.tokens[j + 1][3] == 'as':
                                            if j + 2 < len(self.tokens) and self.tokens[j + 2][0] == 'IDENT':
                                                a = self.tokens[j + 2][3]
                                                j += 2
                                        named_names.append((n, a))
                                    j += 1
                                j += 1
                        while j < len(self.tokens) and not (self.tokens[j][0] == 'KEYWORD' and self.tokens[j][3] == 'from'):
                            j += 1
                        if j + 1 < len(self.tokens) and self.tokens[j + 1][0] == 'STRING':
                            source = self.tokens[j + 1][3].strip('"\'')
                            all_names = [(default_name, None)] + named_names
                            imports.append(Import(
                                source=source,
                                names=all_names,
                                line=line,
                                import_type=ImportType.DEFAULT if not named_names else ImportType.NAMED,
                                is_relative=source.startswith(('./', '../'))
                            ))
                    elif next_tok[0] == 'STRING':
                        source = next_tok[3].strip('"\'')
                        imports.append(Import(
                            source=source,
                            names=[],
                            line=line,
                            import_type=ImportType.SIDE_EFFECT,
                            is_relative=source.startswith(('./', '../'))
                        ))
            if token[0] == 'IDENT' and token[3] == 'require':
                if i + 1 < len(self.tokens) and self.tokens[i + 1][0] == '(':
                    if i + 2 < len(self.tokens) and self.tokens[i + 2][0] == 'STRING':
                        source = self.tokens[i + 2][3].strip('"\'')
                        imports.append(Import(
                            source=source,
                            names=[],
                            line=token[1],
                            import_type=ImportType.REQUIRE,
                            is_relative=source.startswith(('./', '../'))
                        ))
            i += 1
        require_pattern = r'require\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)'
        for match in re.finditer(require_pattern, self.content):
            source = match.group(1)
            if not any(imp.source == source and imp.import_type == ImportType.REQUIRE for imp in imports):
                line = self.content[:match.start()].count('\n') + 1
                imports.append(Import(
                    source=source,
                    names=[],
                    line=line,
                    import_type=ImportType.REQUIRE,
                    is_relative=source.startswith(('./', '../'))
                ))
        return imports
    def find_declarations(self) -> Dict[str, Declaration]:
        declarations = {}
        i = 0
        while i < len(self.tokens):
            token = self.tokens[i]
            if token[0] == 'KEYWORD' and token[3] in ('const', 'let', 'var'):
                line = token[1]
                kind = token[3]
                if i + 1 < len(self.tokens):
                    next_tok = self.tokens[i + 1]
                    if next_tok[0] == '{':
                        j = i + 2
                        while j < len(self.tokens) and self.tokens[j][0] != '}':
                            if self.tokens[j][0] == 'IDENT':
                                name = self.tokens[j][3]
                                if name not in self.RESERVED_KEYWORDS and name not in self.BUILTIN_TYPES:
                                    declarations[name] = Declaration(
                                        name=name,
                                        decl_type=DeclarationType.VARIABLE,
                                        line=line,
                                        kind=kind
                                    )
                            j += 1
                    elif next_tok[0] == '[':
                        j = i + 2
                        while j < len(self.tokens) and self.tokens[j][0] != ']':
                            if self.tokens[j][0] == 'IDENT':
                                name = self.tokens[j][3]
                                if name not in self.RESERVED_KEYWORDS and name not in self.BUILTIN_TYPES:
                                    declarations[name] = Declaration(
                                        name=name,
                                        decl_type=DeclarationType.VARIABLE,
                                        line=line,
                                        kind=kind
                                    )
                            j += 1
                    elif next_tok[0] == 'IDENT':
                        name = next_tok[3]
                        if name not in self.RESERVED_KEYWORDS and name not in self.BUILTIN_TYPES:
                            declarations[name] = Declaration(
                                name=name,
                                decl_type=DeclarationType.VARIABLE,
                                line=line,
                                kind=kind
                            )
            elif token[0] == 'KEYWORD' and token[3] == 'function':
                line = token[1]
                if i + 1 < len(self.tokens) and self.tokens[i + 1][0] == 'IDENT':
                    name = self.tokens[i + 1][3]
                    declarations[name] = Declaration(
                        name=name,
                        decl_type=DeclarationType.FUNCTION,
                        line=line,
                        kind='function'
                    )
            elif token[0] == 'KEYWORD' and token[3] == 'class':
                line = token[1]
                if i + 1 < len(self.tokens) and self.tokens[i + 1][0] == 'IDENT':
                    name = self.tokens[i + 1][3]
                    declarations[name] = Declaration(
                        name=name,
                        decl_type=DeclarationType.CLASS,
                        line=line,
                        kind='class'
                    )
            elif token[0] == 'KEYWORD' and token[3] == 'interface':
                line = token[1]
                if i + 1 < len(self.tokens) and self.tokens[i + 1][0] == 'IDENT':
                    name = self.tokens[i + 1][3]
                    declarations[name] = Declaration(
                        name=name,
                        decl_type=DeclarationType.INTERFACE,
                        line=line,
                        kind='interface'
                    )
            elif token[0] == 'KEYWORD' and token[3] == 'type':
                line = token[1]
                if i + 1 < len(self.tokens) and self.tokens[i + 1][0] == 'IDENT':
                    if i + 2 < len(self.tokens) and self.tokens[i + 2][0] == '=':
                        name = self.tokens[i + 1][3]
                        declarations[name] = Declaration(
                            name=name,
                            decl_type=DeclarationType.TYPE,
                            line=line,
                            kind='type'
                        )
            elif token[0] == 'KEYWORD' and token[3] == 'enum':
                line = token[1]
                if i + 1 < len(self.tokens) and self.tokens[i + 1][0] == 'IDENT':
                    name = self.tokens[i + 1][3]
                    declarations[name] = Declaration(
                        name=name,
                        decl_type=DeclarationType.ENUM,
                        line=line,
                        kind='enum'
                    )
            i += 1
        return declarations
    def find_identifiers(self) -> List[Tuple[int, str]]:
        identifiers = []
        i = 0
        in_import = False
        while i < len(self.tokens):
            token = self.tokens[i]
            if token[0] == 'KEYWORD' and token[3] == 'import':
                in_import = True
                i += 1
                continue
            if in_import and (token[0] == 'STRING' or token[0] == ';'):
                in_import = False
                i += 1
                continue
            if token[0] == 'IDENT' and not in_import:
                name = token[3]
                if name not in self.RESERVED_KEYWORDS and name not in self.BUILTIN_TYPES:
                    prev_token = self.tokens[i - 1] if i > 0 else None
                    is_declaration = False
                    if prev_token and prev_token[0] == 'KEYWORD' and prev_token[3] in ('const', 'let', 'var', 'function', 'class', 'interface', 'type', 'enum'):
                        is_declaration = True
                    if prev_token and prev_token[0] == 'KEYWORD' and prev_token[3] in ('as', 'from'):
                        is_declaration = True
                    if prev_token and prev_token[0] == '.':
                        is_declaration = True
                    if not is_declaration:
                        identifiers.append((token[1], name))
            i += 1
        return identifiers
    def _detect_language(self) -> str:
        if self.filename:
            if self.filename.endswith('.ts') or self.filename.endswith('.tsx'):
                return 'typescript'
            if self.filename.endswith('.js') or self.filename.endswith('.jsx'):
                return 'javascript'
        ts_indicators = ['interface ', 'type ', 'enum ', 'namespace ', 'declare ', ': ', '<>']
        for indicator in ts_indicators:
            if indicator in self.content:
                return 'typescript'
        return 'javascript'