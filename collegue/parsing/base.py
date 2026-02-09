from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple, Set
from enum import Enum
class ImportType(Enum):
    IMPORT = "import"
    FROM_IMPORT = "from_import"
    NAMESPACE = "namespace"
    NAMED = "named"
    DEFAULT = "default"
    SIDE_EFFECT = "side_effect"
    REQUIRE = "require"
    DYNAMIC = "dynamic"
class DeclarationType(Enum):
    VARIABLE = "variable"
    FUNCTION = "function"
    CLASS = "class"
    INTERFACE = "interface"
    TYPE = "type"
    ENUM = "enum"
@dataclass
class Import:
    source: str
    names: List[Tuple[str, Optional[str]]] = field(default_factory=list)
    line: int = 0
    column: int = 0
    import_type: ImportType = ImportType.IMPORT
    is_relative: bool = False
    def __post_init__(self):
        if not self.is_relative:
            self.is_relative = self.source.startswith(('./', '../'))
@dataclass
class Declaration:
    name: str
    decl_type: DeclarationType
    line: int = 0
    column: int = 0
    kind: str = ""
    signature: str = ""
@dataclass
class ParseResult:
    language: str = ""
    imports: List[Import] = field(default_factory=list)
    declarations: Dict[str, Declaration] = field(default_factory=dict)
    identifiers: List[Tuple[int, str]] = field(default_factory=list)
    ast_valid: bool = True
    errors: List[str] = field(default_factory=list)
    raw: str = ""
class BaseParser(ABC):
    def __init__(self, content: str, filename: Optional[str] = None):
        self.content = content
        self.filename = filename or ""
        self.lines = content.split('\n')
    @abstractmethod
    def find_imports(self) -> List[Import]:
        pass
    @abstractmethod
    def find_declarations(self) -> Dict[str, Declaration]:
        pass
    @abstractmethod
    def find_identifiers(self) -> List[Tuple[int, str]]:
        pass
    def parse(self) -> ParseResult:
        return ParseResult(
            language=self._detect_language(),
            imports=self.find_imports(),
            declarations=self.find_declarations(),
            identifiers=self.find_identifiers(),
            raw=self.content,
        )
    @abstractmethod
    def _detect_language(self) -> str:
        pass
    def _get_line_at_position(self, pos: int) -> int:
        return self.content[:pos].count('\n') + 1
    def _get_column_at_position(self, pos: int) -> int:
        line_start = self.content.rfind('\n', 0, pos) + 1
        return pos - line_start + 1