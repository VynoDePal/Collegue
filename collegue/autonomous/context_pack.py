"""
Context Pack - Système d'extraction de contexte code pour l'agent autonome.

Ce module fournit les outils pour extraire le contexte pertinent
depuis une stacktrace Sentry et le fichier source correspondant,
afin de permettre au LLM de générer des patchs minimaux et précis.

Sources:
- Python traceback module: https://docs.python.org/3/library/traceback.html
- Sentry API stacktrace format: https://docs.sentry.io/api/events/
- AST-based code chunking: Best practice for LLM code context
"""
import ast
import base64
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("context_pack")


@dataclass
class StackFrame:
    """Représente un frame de stacktrace."""
    filename: str
    lineno: int
    function: str
    context_line: Optional[str] = None
    pre_context: List[str] = field(default_factory=list)
    post_context: List[str] = field(default_factory=list)
    abs_path: Optional[str] = None
    module: Optional[str] = None
    in_app: bool = True  # True si c'est du code de l'application (pas stdlib/site-packages)


@dataclass
class FileContext:
    """Contexte d'un fichier source."""
    filepath: str
    full_content: str
    relevant_chunk: str
    chunk_start_line: int
    chunk_end_line: int
    error_line: Optional[int] = None
    function_name: Optional[str] = None
    class_name: Optional[str] = None
    imports_section: Optional[str] = None  # Premiers imports du fichier


@dataclass
class ContextPack:
    """Pack de contexte complet pour l'analyse LLM."""
    # Fichier principal où l'erreur s'est produite
    primary_file: Optional[FileContext] = None
    
    # Fichiers liés (imports, appelants)
    related_files: List[FileContext] = field(default_factory=list)
    
    # Informations sur l'erreur
    error_title: str = ""
    error_message: str = ""
    error_type: str = ""
    stacktrace_summary: str = ""
    
    # Frames extraits de la stacktrace
    frames: List[StackFrame] = field(default_factory=list)
    
    # Frame coupable (celui où l'erreur s'est produite dans le code du projet)
    culprit_frame: Optional[StackFrame] = None
    
    # Contraintes pour le LLM
    constraints: List[str] = field(default_factory=lambda: [
        "Ne crée PAS de nouveaux fichiers sauf si explicitement demandé",
        "Ne remplace PAS tout le fichier - génère des patchs SEARCH/REPLACE minimaux",
        "Conserve TOUT le code existant qui fonctionne",
        "Utilise le format de patch spécifié"
    ])
    
    def to_prompt_context(self) -> str:
        """Génère le contexte formaté pour le prompt LLM."""
        parts = []
        
        # Erreur
        parts.append(f"## ERREUR: {self.error_title}")
        if self.error_type:
            parts.append(f"Type: {self.error_type}")
        if self.error_message:
            parts.append(f"Message: {self.error_message}")
        parts.append("")
        
        # Fichier principal avec le code
        if self.primary_file:
            pf = self.primary_file
            parts.append(f"## FICHIER COUPABLE: {pf.filepath}")
            if pf.function_name:
                parts.append(f"Fonction: {pf.function_name}")
            if pf.class_name:
                parts.append(f"Classe: {pf.class_name}")
            if pf.error_line:
                parts.append(f"Ligne de l'erreur: {pf.error_line}")
            parts.append(f"Lignes affichées: {pf.chunk_start_line}-{pf.chunk_end_line}")
            parts.append("")
            
            # Code avec numéros de ligne
            parts.append("```python")
            lines = pf.relevant_chunk.split('\n')
            for i, line in enumerate(lines, start=pf.chunk_start_line):
                marker = " >>> " if i == pf.error_line else "     "
                parts.append(f"{i:4d}{marker}{line}")
            parts.append("```")
            parts.append("")
        
        # Stacktrace résumé
        if self.stacktrace_summary:
            parts.append("## STACKTRACE (résumé)")
            parts.append(self.stacktrace_summary)
            parts.append("")
        
        # Contraintes
        parts.append("## CONTRAINTES IMPORTANTES")
        for constraint in self.constraints:
            parts.append(f"- {constraint}")
        
        return "\n".join(parts)


class ContextPackBuilder:
    """
    Construit un ContextPack à partir d'une issue Sentry et du contenu GitHub.
    
    Usage:
        builder = ContextPackBuilder(github_tool, repo_owner, repo_name, github_token)
        context_pack = await builder.build(sentry_event, issue_title)
    """
    
    # Préfixes de fichiers à considérer comme "in_app" (code du projet)
    PROJECT_PREFIXES = ["collegue/", "src/", "app/", "lib/"]
    
    # Patterns à exclure (stdlib, site-packages, etc.)
    EXCLUDE_PATTERNS = [
        "site-packages",
        "/usr/lib/python",
        "/usr/local/lib/python",
        "<frozen",
        "<string>",
        "importlib",
        "asyncio/",
        "concurrent/",
        "threading",
        "multiprocessing",
    ]
    
    def __init__(
        self,
        github_tool,
        repo_owner: str,
        repo_name: str,
        github_token: str,
        project_prefixes: Optional[List[str]] = None
    ):
        self.github = github_tool
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.github_token = github_token
        if project_prefixes:
            self.PROJECT_PREFIXES = project_prefixes
    
    def extract_frames_from_event(self, event: Any) -> List[StackFrame]:
        """
        Extrait les frames de stacktrace depuis un événement Sentry.
        
        L'événement Sentry peut avoir plusieurs formats:
        1. event.stacktrace (string brut)
        2. event.exception.values[0].stacktrace.frames (structuré)
        3. event.entries avec type='exception' (API web)
        """
        frames = []
        
        # Format 1: stacktrace comme attribut string
        if hasattr(event, 'stacktrace') and isinstance(event.stacktrace, str):
            frames = self._parse_stacktrace_string(event.stacktrace)
        
        # Format 2: données structurées (modèle Pydantic ou dict)
        if hasattr(event, 'raw_data') and event.raw_data:
            raw = event.raw_data
            frames = self._extract_frames_from_raw(raw)
        
        # Format 3: directement dans l'event
        if not frames and hasattr(event, 'entries'):
            for entry in event.entries or []:
                if entry.get('type') == 'exception':
                    values = entry.get('data', {}).get('values', [])
                    for val in values:
                        st = val.get('stacktrace', {})
                        for f in st.get('frames', []):
                            frames.append(self._dict_to_frame(f))
        
        return frames
    
    def _extract_frames_from_raw(self, raw: Dict) -> List[StackFrame]:
        """Extrait les frames depuis les données brutes JSON de Sentry."""
        frames = []
        
        # Essayer exception.values[].stacktrace.frames
        exception = raw.get('exception', {})
        values = exception.get('values', [])
        for val in values:
            st = val.get('stacktrace', {})
            for f in st.get('frames', []):
                frames.append(self._dict_to_frame(f))
        
        # Essayer entries[].data.values[].stacktrace.frames
        if not frames:
            for entry in raw.get('entries', []):
                if entry.get('type') == 'exception':
                    for val in entry.get('data', {}).get('values', []):
                        st = val.get('stacktrace', {})
                        for f in st.get('frames', []):
                            frames.append(self._dict_to_frame(f))
        
        return frames
    
    def _dict_to_frame(self, f: Dict) -> StackFrame:
        """Convertit un dict Sentry en StackFrame."""
        return StackFrame(
            filename=f.get('filename', f.get('absPath', '')),
            lineno=f.get('lineNo', f.get('lineno', 0)),
            function=f.get('function', '<unknown>'),
            context_line=f.get('context_line') or f.get('contextLine'),
            pre_context=f.get('pre_context') or f.get('preContext') or [],
            post_context=f.get('post_context') or f.get('postContext') or [],
            abs_path=f.get('absPath') or f.get('abs_path'),
            module=f.get('module'),
            in_app=f.get('inApp', f.get('in_app', True))
        )
    
    def _parse_stacktrace_string(self, stacktrace: str) -> List[StackFrame]:
        """
        Parse une stacktrace Python au format texte.
        
        Format attendu:
        File "path/to/file.py", line 123, in function_name
            code_line_here
        """
        frames = []
        pattern = r'File "([^"]+)", line (\d+), in (\w+)'
        
        lines = stacktrace.split('\n')
        i = 0
        while i < len(lines):
            match = re.search(pattern, lines[i])
            if match:
                filename = match.group(1)
                lineno = int(match.group(2))
                function = match.group(3)
                
                # La ligne suivante est généralement le code
                context_line = None
                if i + 1 < len(lines) and lines[i + 1].strip():
                    context_line = lines[i + 1].strip()
                
                frames.append(StackFrame(
                    filename=filename,
                    lineno=lineno,
                    function=function,
                    context_line=context_line,
                    in_app=self._is_project_file(filename)
                ))
            i += 1
        
        return frames
    
    def _is_project_file(self, filepath: str) -> bool:
        """Détermine si un fichier appartient au projet (pas stdlib/site-packages)."""
        # Exclure les patterns connus
        for pattern in self.EXCLUDE_PATTERNS:
            if pattern in filepath:
                return False
        
        # Vérifier les préfixes du projet
        for prefix in self.PROJECT_PREFIXES:
            if prefix in filepath or filepath.startswith(prefix):
                return True
        
        # Si le path ne contient pas de markers stdlib, considérer comme projet
        if not filepath.startswith('/usr') and not filepath.startswith('/lib'):
            return True
        
        return False
    
    def filter_project_frames(self, frames: List[StackFrame]) -> List[StackFrame]:
        """Filtre pour ne garder que les frames du projet."""
        return [f for f in frames if f.in_app and self._is_project_file(f.filename)]
    
    def get_culprit_frame(self, frames: List[StackFrame]) -> Optional[StackFrame]:
        """
        Identifie le frame "coupable" - là où l'erreur s'est vraiment produite.
        
        C'est généralement le dernier frame dans le code du projet
        (les frames sont ordonnés du plus ancien au plus récent).
        """
        project_frames = self.filter_project_frames(frames)
        if project_frames:
            return project_frames[-1]  # Le plus récent
        return frames[-1] if frames else None
    
    def _normalize_filepath(self, filepath: str) -> str:
        """Normalise le chemin de fichier pour correspondre à la structure du repo."""
        # Enlever les préfixes absolus courants
        prefixes_to_strip = ['/app/', '/home/', '/var/', '/srv/']
        for prefix in prefixes_to_strip:
            if filepath.startswith(prefix):
                # Trouver le premier segment qui match PROJECT_PREFIXES
                parts = filepath.split('/')
                for i, part in enumerate(parts):
                    for proj_prefix in self.PROJECT_PREFIXES:
                        if part == proj_prefix.rstrip('/'):
                            return '/'.join(parts[i:])
        
        # Si le chemin commence par un prefix de projet, le garder tel quel
        for prefix in self.PROJECT_PREFIXES:
            if filepath.startswith(prefix) or prefix.rstrip('/') in filepath:
                idx = filepath.find(prefix.rstrip('/'))
                if idx >= 0:
                    return filepath[idx:]
        
        return filepath
    
    async def fetch_file_content(self, filepath: str) -> Optional[str]:
        """Récupère le contenu d'un fichier depuis GitHub."""
        from collegue.tools.github_ops import GitHubRequest
        
        normalized_path = self._normalize_filepath(filepath)
        
        try:
            response = self.github._execute_core_logic(GitHubRequest(
                command="get_file",
                owner=self.repo_owner,
                repo=self.repo_name,
                path=normalized_path,
                token=self.github_token
            ))
            
            if response and response.content:
                # Le contenu est encodé en base64
                try:
                    return base64.b64decode(response.content).decode('utf-8')
                except Exception:
                    # Peut-être déjà décodé
                    return response.content
        except Exception as e:
            logger.warning(f"Impossible de récupérer {normalized_path} depuis GitHub: {e}")
        
        return None
    
    def extract_code_chunk(
        self,
        content: str,
        error_line: int,
        context_lines: int = 50
    ) -> Tuple[str, int, int, Optional[str], Optional[str]]:
        """
        Extrait un chunk de code pertinent autour de la ligne d'erreur.
        
        Utilise l'AST Python pour trouver la fonction/classe contenant la ligne,
        avec fallback sur un contexte de N lignes avant/après.
        
        Returns:
            (chunk, start_line, end_line, function_name, class_name)
        """
        lines = content.split('\n')
        total_lines = len(lines)
        
        function_name = None
        class_name = None
        start_line = 1
        end_line = total_lines
        
        # Essayer d'utiliser AST pour trouver la fonction/classe
        try:
            tree = ast.parse(content)
            
            # Trouver la fonction contenant la ligne
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if hasattr(node, 'lineno') and hasattr(node, 'end_lineno'):
                        if node.lineno <= error_line <= node.end_lineno:
                            function_name = node.name
                            # Étendre un peu le contexte (5 lignes avant/après la fonction)
                            start_line = max(1, node.lineno - 5)
                            end_line = min(total_lines, node.end_lineno + 5)
                            
                            # Chercher la classe parente
                            for parent in ast.walk(tree):
                                if isinstance(parent, ast.ClassDef):
                                    if hasattr(parent, 'lineno') and hasattr(parent, 'end_lineno'):
                                        if parent.lineno < node.lineno <= parent.end_lineno:
                                            class_name = parent.name
                            break
            
            # Si pas de fonction trouvée, chercher une classe
            if not function_name:
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef):
                        if hasattr(node, 'lineno') and hasattr(node, 'end_lineno'):
                            if node.lineno <= error_line <= node.end_lineno:
                                class_name = node.name
                                start_line = max(1, node.lineno - 5)
                                end_line = min(total_lines, node.end_lineno + 5)
                                break
        
        except SyntaxError:
            logger.warning("Erreur de syntaxe dans le fichier, utilisation du fallback")
        except Exception as e:
            logger.warning(f"Erreur AST: {e}, utilisation du fallback")
        
        # Fallback: contexte simple autour de la ligne
        if start_line == 1 and end_line == total_lines and total_lines > context_lines * 2:
            start_line = max(1, error_line - context_lines)
            end_line = min(total_lines, error_line + context_lines)
        
        # Extraire le chunk (indices 0-based)
        chunk_lines = lines[start_line - 1:end_line]
        chunk = '\n'.join(chunk_lines)
        
        return chunk, start_line, end_line, function_name, class_name
    
    def extract_imports_section(self, content: str, max_lines: int = 30) -> str:
        """Extrait la section des imports du fichier."""
        lines = content.split('\n')
        import_lines = []
        
        for line in lines[:max_lines]:
            stripped = line.strip()
            if stripped.startswith('import ') or stripped.startswith('from '):
                import_lines.append(line)
            elif stripped.startswith('#') or stripped == '' or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            elif import_lines:  # On a déjà des imports et on rencontre autre chose
                break
        
        return '\n'.join(import_lines)
    
    def build_stacktrace_summary(self, frames: List[StackFrame], max_frames: int = 5) -> str:
        """Construit un résumé lisible de la stacktrace."""
        if not frames:
            return "Aucune stacktrace disponible"
        
        project_frames = self.filter_project_frames(frames)
        frames_to_show = project_frames[-max_frames:] if project_frames else frames[-max_frames:]
        
        lines = []
        for f in frames_to_show:
            line = f"  {f.filename}:{f.lineno} in {f.function}()"
            if f.context_line:
                line += f"\n    → {f.context_line.strip()}"
            lines.append(line)
        
        return '\n'.join(lines)
    
    async def build(
        self,
        sentry_event: Any,
        issue_title: str,
        error_message: str = "",
        error_type: str = ""
    ) -> ContextPack:
        """
        Construit un ContextPack complet à partir d'un événement Sentry.
        
        Args:
            sentry_event: Événement Sentry (objet ou dict)
            issue_title: Titre de l'issue Sentry
            error_message: Message d'erreur détaillé (optionnel)
            error_type: Type d'exception (optionnel)
        
        Returns:
            ContextPack prêt à être utilisé dans le prompt LLM
        """
        pack = ContextPack(
            error_title=issue_title,
            error_message=error_message,
            error_type=error_type
        )
        
        # 1. Extraire les frames de la stacktrace
        frames = self.extract_frames_from_event(sentry_event)
        pack.frames = frames
        
        if not frames:
            logger.warning("Aucun frame extrait de l'événement Sentry")
            # Essayer de parser le stacktrace string si disponible
            if hasattr(sentry_event, 'stacktrace') and sentry_event.stacktrace:
                pack.stacktrace_summary = sentry_event.stacktrace[:2000]
            return pack
        
        # 2. Identifier le frame coupable
        culprit = self.get_culprit_frame(frames)
        pack.culprit_frame = culprit
        
        # 3. Construire le résumé de stacktrace
        pack.stacktrace_summary = self.build_stacktrace_summary(frames)
        
        if not culprit:
            logger.warning("Aucun frame coupable identifié")
            return pack
        
        # 4. Récupérer le contenu du fichier depuis GitHub
        file_content = await self.fetch_file_content(culprit.filename)
        
        if not file_content:
            logger.warning(f"Impossible de récupérer le fichier {culprit.filename}")
            return pack
        
        # 5. Extraire le chunk de code pertinent
        chunk, start, end, func_name, class_name = self.extract_code_chunk(
            file_content,
            culprit.lineno
        )
        
        # 6. Extraire les imports
        imports = self.extract_imports_section(file_content)
        
        # 7. Construire le FileContext principal
        pack.primary_file = FileContext(
            filepath=self._normalize_filepath(culprit.filename),
            full_content=file_content,
            relevant_chunk=chunk,
            chunk_start_line=start,
            chunk_end_line=end,
            error_line=culprit.lineno,
            function_name=func_name or culprit.function,
            class_name=class_name,
            imports_section=imports
        )
        
        logger.info(
            f"ContextPack construit: {pack.primary_file.filepath} "
            f"lignes {start}-{end}, fonction: {func_name or culprit.function}"
        )
        
        return pack
