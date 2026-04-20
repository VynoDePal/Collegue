"""Stress payloads for code_refactoring."""
TOOL_NAME = "code_refactoring"

_minified = "def a():return sum([x*x for x in range(100)])" + ";" * 20_000
_bad_syntax = "def oops(\n    return 1\n"
_bidi_null = "def f():\n    x = '\u202e\x00'\n    return x"

PAYLOADS = [
    {"description": "refactoring_type inconnu (delete_everything)",
     "arguments": {"code": "def f(): pass", "language": "python", "refactoring_type": "delete_everything"}},
    {"description": "code vide + refactoring_type=rename",
     "arguments": {"code": "", "language": "python", "refactoring_type": "rename"}},
    {"description": "parameters non-dict (liste)",
     "arguments": {"code": "def f(): pass", "language": "python", "refactoring_type": "rename",
                   "parameters": ["not", "a", "dict"]}},
    {"description": "parameters string",
     "arguments": {"code": "def f(): pass", "language": "python", "refactoring_type": "rename",
                   "parameters": "oops"}},
    {"description": "Code avec syntaxe invalide",
     "arguments": {"code": _bad_syntax, "language": "python", "refactoring_type": "clean"}},
    {"description": "Code minifie 100KB sur une ligne",
     "arguments": {"code": _minified, "language": "python", "refactoring_type": "simplify"}},
    {"description": "refactoring=extract sans function_name",
     "arguments": {"code": "def f(): x=1+2; y=3+4; return x+y", "language": "python",
                   "refactoring_type": "extract"}},
    {"description": "Caracteres null et BiDi",
     "arguments": {"code": _bidi_null, "language": "python", "refactoring_type": "clean"}},
    {"description": "language vide",
     "arguments": {"code": "def f(): pass", "language": "", "refactoring_type": "clean"}},
    {"description": "language=cobol (non supporte)",
     "arguments": {"code": "MOVE 1 TO X.", "language": "cobol", "refactoring_type": "modernize"}},
]
