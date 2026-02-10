"""
Repo Consistency Check - Outil de détection d'incohérences dans le code

Cet outil détecte les incohérences typiques générées par l'IA:
- Code mort (fonctions/classes jamais appelées)
- Variables inutilisées
- Imports non utilisés
- Duplication de code
- Mismatch paramètres/retours
- Symboles non résolus

Problème résolu: L'IA génère souvent des "hallucinations silencieuses" (code qui compile
mais contient des incohérences).
Valeur: Transforme ces hallucinations en diagnostics actionnables.
Bénéfice: Meilleure fiabilité des patches IA, réduction de dette technique.
"""
import re
import ast
import asyncio
import hashlib
import json
from typing import Optional, Dict, Any, List, Type, Set, Tuple
from collections import defaultdict
from pydantic import BaseModel, Field, field_validator
from .base import BaseTool, ToolError, ToolValidationError, ToolExecutionError
from .shared import FileInput, aggregate_severities, detect_language_from_extension, parse_llm_json_response, run_async_from_sync, validate_fast_deep
from .analyzers.python import PythonAnalyzer
from .analyzers.javascript import JavaScriptAnalyzer


class ConsistencyCheckRequest(BaseModel):
    """Modèle de requête pour la vérification de cohérence."""
    files: List[FileInput] = Field(
        ...,
        description="Liste des fichiers à analyser [{path, content, language?}, ...]",
        min_length=1
    )
    language: str = Field(
        "auto",
        description="Langage principal: 'python', 'typescript', 'javascript', 'auto'"
    )
    checks: Optional[List[str]] = Field(
        None,
        description="Checks à exécuter: 'unused_imports', 'unused_vars', 'dead_code', 'duplication', 'unresolved_symbol'. Tous par défaut."
    )
    diff: Optional[str] = Field(
        None,
        description="Diff unifié optionnel pour focaliser l'analyse sur les changements"
    )
    mode: str = Field(
        "fast",
        description="Mode: 'fast' (heuristiques rapides) ou 'deep' (analyse plus complète)"
    )
    analysis_depth: str = Field(
        "fast",
        description="Profondeur IA: 'fast' (heuristiques seules) ou 'deep' (enrichissement LLM avec scoring)"
    )
    auto_chain: bool = Field(
        False,
        description="Si True et score refactoring > seuil, déclenche automatiquement code_refactoring"
    )
    refactoring_threshold: float = Field(
        0.7,
        description="Seuil de score (0.0-1.0) pour déclencher auto_chain",
        ge=0.0,
        le=1.0
    )
    min_confidence: int = Field(
        60,
        description="Confiance minimum (0-100) pour reporter un issue",
        ge=0,
        le=100
    )

    @field_validator('mode')
    def validate_mode(cls, v):
        return validate_fast_deep(v)

    @field_validator('analysis_depth')
    def validate_analysis_depth(cls, v):
        return validate_fast_deep(v)

    @field_validator('checks')
    def validate_checks(cls, v):
        if v is None:
            return v
        valid = ['unused_imports', 'unused_vars', 'dead_code', 'duplication',
                 'unresolved_symbol']
        for check in v:
            if check not in valid:
                raise ValueError(f"Check '{check}' invalide. Utilisez: {', '.join(valid)}")
        return v

class ConsistencyIssue(BaseModel):
    kind: str = Field(..., description="Type: unused_import, unused_var, dead_code, duplication, unresolved_symbol")
    severity: str = Field(..., description="Sévérité: info, low, medium, high")
    path: str = Field(..., description="Chemin du fichier")
    line: Optional[int] = Field(None, description="Numéro de ligne")
    column: Optional[int] = Field(None, description="Numéro de colonne")
    message: str = Field(..., description="Description du problème")
    confidence: int = Field(..., description="Confiance 0-100")
    suggested_fix: Optional[str] = Field(None, description="Suggestion de correction")
    engine: str = Field("embedded-rules", description="Moteur utilisé")
class LLMInsight(BaseModel):
    category: str = Field(..., description="Catégorie: pattern, architecture, debt, suggestion")
    insight: str = Field(..., description="L'insight détaillé")
    confidence: str = Field("medium", description="Confiance: low, medium, high")
    affected_files: List[str] = Field(default_factory=list, description="Fichiers concernés")

class SuggestedAction(BaseModel):
    tool_name: str = Field(..., description="Nom du tool à appeler (ex: code_refactoring)")
    action_type: str = Field(..., description="Type: refactor, cleanup, restructure")
    rationale: str = Field(..., description="Pourquoi cette action")
    priority: str = Field("medium", description="Priorité: low, medium, high, critical")
    params: Dict[str, Any] = Field(default_factory=dict, description="Paramètres pour le tool")
    score: float = Field(0.0, description="Score de pertinence (0.0-1.0)", ge=0.0, le=1.0)


class ConsistencyCheckResponse(BaseModel):
    valid: bool = Field(..., description="True si aucun problème trouvé")
    summary: Dict[str, int] = Field(
        ...,
        description="Résumé par sévérité {total, high, medium, low, info}"
    )
    issues: List[ConsistencyIssue] = Field(
        default_factory=list,
        description="Liste des problèmes détectés"
    )
    files_analyzed: int = Field(..., description="Nombre de fichiers analysés")
    checks_performed: List[str] = Field(..., description="Checks exécutés")
    analysis_summary: str = Field(..., description="Résumé de l'analyse")

    analysis_depth_used: str = Field("fast", description="Profondeur d'analyse utilisée")
    llm_insights: Optional[List[LLMInsight]] = Field(
        None,
        description="Insights IA (mode deep): patterns, architecture, dette technique"
    )

    refactoring_score: float = Field(
        0.0,
        description="Score de refactoring recommandé (0.0-1.0)",
        ge=0.0,
        le=1.0
    )
    refactoring_priority: str = Field(
        "none",
        description="Priorité: none, suggested, recommended, critical"
    )
    suggested_actions: List[SuggestedAction] = Field(
        default_factory=list,
        description="Actions suggérées (tools à appeler)"
    )

    auto_refactoring_triggered: bool = Field(
        False,
        description="True si le refactoring automatique a été déclenché"
    )
    auto_refactoring_result: Optional[Dict[str, Any]] = Field(
        None,
        description="Résultat du refactoring automatique (si déclenché)"
    )

class RepoConsistencyCheckTool(BaseTool):
    tool_name = "repo_consistency_check"
    tool_description = "Détecte les incohérences dans le code: imports/variables inutilisés, code mort, duplication"
    request_model = ConsistencyCheckRequest
    response_model = ConsistencyCheckResponse
    supported_languages = ["python", "typescript", "javascript", "auto"]
    long_running = False

    ALL_CHECKS = ['unused_imports', 'unused_vars', 'dead_code', 'duplication',
                  'unresolved_symbol']

    SEVERITY_MAP = {
        'unused_import': 'low',
        'unused_var': 'medium',
        'dead_code': 'medium',
        'duplication': 'low',
        'unresolved_symbol': 'high',
    }

    def get_usage_description(self) -> str:
        return (
            "Analyse le code pour détecter les incohérences typiques générées par l'IA: "
            "imports non utilisés, variables mortes, code dupliqué, symboles non résolus."
        )

    def get_examples(self) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Vérifier un fichier Python",
                "request": {
                    "files": [{"path": "utils.py", "content": "import os\nimport sys\nprint('hello')"}],
                    "language": "python"
                }
            },
            {
                "title": "Mode deep avec checks spécifiques",
                "request": {
                    "files": [{"path": "app.ts", "content": "..."}],
                    "language": "typescript",
                    "mode": "deep",
                    "checks": ["unused_imports", "dead_code"]
                }
            }
        ]

    def __init__(self, config=None, app_state=None):
        super().__init__(config, app_state)
        self._python_analyzer = PythonAnalyzer(logger=self.logger)
        self._js_analyzer = JavaScriptAnalyzer(logger=self.logger)

    def get_capabilities(self) -> List[str]:
        return [
            "Détection d'imports non utilisés (Python, JS/TS)",
            "Détection de variables inutilisées",
            "Détection de code mort (fonctions non appelées)",
            "Détection de duplication de code",
            "Détection de mismatch signature/usage",
            "Support multi-fichiers avec analyse croisée"
        ]

    def _detect_language(self, filepath: str) -> str:
        return detect_language_from_extension(filepath)


    def _analyze_duplication(self, files: List[FileInput], min_lines: int = 5) -> List[ConsistencyIssue]:
        issues = []


        def normalize_line(line: str) -> str:

            line = line.strip()
            line = re.sub(r'//.*$', '', line)
            line = re.sub(r'#.*$', '', line)
            return line

        def get_blocks(content: str, block_size: int = 5) -> Dict[str, Tuple[int, str]]:
            lines = content.split('\n')
            blocks = {}
            for i in range(len(lines) - block_size + 1):
                block_lines = [normalize_line(l) for l in lines[i:i+block_size]]

                if all(len(l) < 3 for l in block_lines):
                    continue
                block_hash = hashlib.md5('\n'.join(block_lines).encode()).hexdigest()
                if block_hash not in blocks:
                    blocks[block_hash] = (i + 1, '\n'.join(lines[i:i+block_size]))
            return blocks

        file_blocks = {}
        for file in files:
            file_blocks[file.path] = get_blocks(file.content, min_lines)

        seen_duplicates = set()
        for path1, blocks1 in file_blocks.items():
            for path2, blocks2 in file_blocks.items():
                if path1 >= path2:
                    continue

                common = set(blocks1.keys()) & set(blocks2.keys())
                for block_hash in common:
                    if block_hash in seen_duplicates:
                        continue
                    seen_duplicates.add(block_hash)

                    line1, code = blocks1[block_hash]
                    line2, _ = blocks2[block_hash]

                    issues.append(ConsistencyIssue(
                        kind="duplication",
                        severity="low",
                        path=path1,
                        line=line1,
                        message=f"Bloc de code dupliqué dans {path2}:{line2}",
                        confidence=80,
                        suggested_fix="Extraire dans une fonction/module partagé",
                        engine="hash-comparator"
                    ))

        return issues

    def _analyze_unresolved_symbols(self, files: List[FileInput]) -> List[ConsistencyIssue]:
        issues = []

        defined_symbols = set()

        python_builtins = {
            'print', 'len', 'range', 'str', 'int', 'float', 'bool', 'list', 'dict', 'set',
            'tuple', 'type', 'isinstance', 'hasattr', 'getattr', 'setattr', 'open', 'input',
            'sum', 'min', 'max', 'abs', 'round', 'sorted', 'reversed', 'enumerate', 'zip',
            'map', 'filter', 'any', 'all', 'None', 'True', 'False', 'Exception', 'ValueError',
            'TypeError', 'KeyError', 'IndexError', 'AttributeError', 'super', 'property',
            'staticmethod', 'classmethod', 'self', 'cls', '__name__', '__file__',
        }


        js_globals = {
            'console', 'window', 'document', 'fetch', 'Promise', 'Array', 'Object', 'String',
            'Number', 'Boolean', 'JSON', 'Math', 'Date', 'Error', 'undefined', 'null',
            'setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'require', 'module',
            'exports', 'process', 'Buffer', '__dirname', '__filename', 'global', 'this',
        }

        defined_symbols.update(python_builtins)
        defined_symbols.update(js_globals)

        for file in files:
            lang = file.language or self._detect_language(file.path)

            if lang == 'python':
                try:
                    tree = ast.parse(file.content)
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            defined_symbols.add(node.name)
                        elif isinstance(node, ast.ClassDef):
                            defined_symbols.add(node.name)
                        elif isinstance(node, ast.Import):
                            for alias in node.names:
                                defined_symbols.add(alias.asname or alias.name.split('.')[0])
                        elif isinstance(node, ast.ImportFrom):
                            for alias in node.names:
                                defined_symbols.add(alias.asname or alias.name)
                        elif isinstance(node, ast.Assign):
                            for target in node.targets:
                                if isinstance(target, ast.Name):
                                    defined_symbols.add(target.id)
                except SyntaxError:
                    pass

            elif lang in ('typescript', 'javascript'):

                patterns = [
                    r"(?:function|class)\s+(\w+)",
                    r"(?:const|let|var)\s+(\w+)",
                    r"import\s+(?:\{[^}]*\}|\*\s+as\s+)?(\w+)",
                    r"export\s+(?:default\s+)?(?:function|class|const|let|var)\s+(\w+)",
                ]
                for pattern in patterns:
                    matches = re.findall(pattern, file.content)
                    defined_symbols.update(matches)

        for file in files:
            lang = file.language or self._detect_language(file.path)

            if lang == 'python':
                try:
                    tree = ast.parse(file.content)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                            if node.id not in defined_symbols:
                                issues.append(ConsistencyIssue(
                                    kind="unresolved_symbol",
                                    severity="high",
                                    path=file.path,
                                    line=node.lineno,
                                    column=node.col_offset,
                                    message=f"Symbole '{node.id}' non résolu dans le scope fourni",
                                    confidence=60,
                                    suggested_fix=f"Vérifier l'import de '{node.id}' ou sa définition",
                                    engine="scope-analyzer"
                                ))
                except SyntaxError:
                    pass

        return issues

    def _calculate_refactoring_score(self, issues: List[ConsistencyIssue]) -> Tuple[float, str]:
        if not issues:
            return 0.0, "none"

        weights = {'high': 0.4, 'medium': 0.25, 'low': 0.1, 'info': 0.05}
        total_weight = sum(weights.get(i.severity, 0.1) for i in issues)

        score = min(1.0, total_weight / 4.0)

        if score >= 0.8:
            priority = "critical"
        elif score >= 0.6:
            priority = "recommended"
        elif score >= 0.3:
            priority = "suggested"
        else:
            priority = "none"

        return score, priority

    def _generate_suggested_actions(
        self,
        issues: List[ConsistencyIssue],
        files: List[FileInput],
        score: float
    ) -> List[SuggestedAction]:
        actions = []


        issue_types = {}
        for issue in issues:
            issue_types.setdefault(issue.kind, []).append(issue)


        if len(issues) >= 5:

            file_issues = {}
            for issue in issues:
                file_issues.setdefault(issue.path, []).append(issue)

            worst_file = max(file_issues.items(), key=lambda x: len(x[1]))
            file_content = next((f.content for f in files if f.path == worst_file[0]), "")

            actions.append(SuggestedAction(
                tool_name="code_refactoring",
                action_type="cleanup",
                rationale=f"Fichier '{worst_file[0]}' a {len(worst_file[1])} problèmes de cohérence",
                priority="high" if len(worst_file[1]) >= 5 else "medium",
                params={
                    "code": file_content[:5000],
                    "language": self._detect_language(worst_file[0]),
                    "refactoring_type": "clean",
                    "file_path": worst_file[0]
                },
                score=score
            ))

        if 'dead_code' in issue_types and len(issue_types['dead_code']) >= 2:
            actions.append(SuggestedAction(
                tool_name="code_refactoring",
                action_type="cleanup",
                rationale=f"{len(issue_types['dead_code'])} fonctions/classes mortes détectées",
                priority="medium",
                params={"refactoring_type": "clean"},
                score=min(1.0, len(issue_types['dead_code']) * 0.2)
            ))

        if 'duplication' in issue_types:
            actions.append(SuggestedAction(
                tool_name="code_refactoring",
                action_type="restructure",
                rationale=f"{len(issue_types['duplication'])} bloc(s) de code dupliqué(s)",
                priority="medium",
                params={"refactoring_type": "extract"},
                score=min(1.0, len(issue_types['duplication']) * 0.25)
            ))

        return actions[:5]

    async def _deep_analysis_with_llm(
        self,
        request: ConsistencyCheckRequest,
        issues: List[ConsistencyIssue],
        llm_manager=None
    ) -> Tuple[Optional[List[LLMInsight]], float, str]:
        try:
            manager = llm_manager or self.llm_manager
            if not manager:
                self.logger.warning("LLM manager non disponible pour analyse deep")
                score, priority = self._calculate_refactoring_score(issues)
                return None, score, priority


            files_summary = []
            for f in request.files[:5]:
                preview = f.content[:400] + "..." if len(f.content) > 400 else f.content
                files_summary.append(f"### {f.path}\n```\n{preview}\n```")

            issues_summary = []
            for issue in issues[:15]:
                issues_summary.append(
                    f"- [{issue.severity.upper()}] {issue.kind} @ {issue.path}:{issue.line or '?'}: {issue.message}"
                )

            prompt = f"""Analyse les incohérences détectées dans ce code et fournis des insights.

## Fichiers analysés
{chr(10).join(files_summary)}

## Issues détectées ({len(issues)} total)
{chr(10).join(issues_summary) if issues_summary else "Aucune issue détectée"}

---

Fournis une analyse enrichie au format JSON strict:
{
  "refactoring_score": 0.0-1.0,
  "insights": [
    {
      "category": "pattern|architecture|debt|suggestion",
      "insight": "Description détaillée",
      "confidence": "low|medium|high",
      "affected_files": ["file1.py", "file2.ts"]
    }
  ]
}

Catégories d'insights:
- **pattern**: Anti-patterns détectés (god class, spaghetti code, etc.)
- **architecture**: Problèmes structurels (couplage, cohésion, responsabilités)
- **debt**: Dette technique (complexité, maintenabilité)
- **suggestion**: Recommandations d'amélioration

Le `refactoring_score` doit refléter l'urgence d'un refactoring:
- 0.0-0.3: Code acceptable, améliorations optionnelles
- 0.3-0.6: Refactoring suggéré pour améliorer la maintenabilité
- 0.6-0.8: Refactoring recommandé, risques de bugs
- 0.8-1.0: Refactoring critique, dette technique élevée

Réponds UNIQUEMENT avec le JSON, sans markdown ni explication."""

            response = await manager.async_generate(prompt)

            if not response:
                score, priority = self._calculate_refactoring_score(issues)
                return None, score, priority

            try:
                data = parse_llm_json_response(response)

                llm_score = float(data.get("refactoring_score", 0.0))
                llm_score = max(0.0, min(1.0, llm_score))

                heuristic_score, _ = self._calculate_refactoring_score(issues)
                final_score = (llm_score * 0.6) + (heuristic_score * 0.4)

                if final_score >= 0.8:
                    priority = "critical"
                elif final_score >= 0.6:
                    priority = "recommended"
                elif final_score >= 0.3:
                    priority = "suggested"
                else:
                    priority = "none"

                insights = []
                for item in data.get("insights", [])[:10]:
                    if isinstance(item, dict) and "insight" in item:
                        insights.append(LLMInsight(
                            category=item.get("category", "suggestion"),
                            insight=item["insight"],
                            confidence=item.get("confidence", "medium"),
                            affected_files=item.get("affected_files", [])
                        ))

                self.logger.info(f"Analyse deep: {len(insights)} insights, score={final_score:.2f}")
                return insights, final_score, priority

            except json.JSONDecodeError as e:
                self.logger.warning(f"Erreur parsing réponse LLM: {e}")
                score, priority = self._calculate_refactoring_score(issues)
                return None, score, priority

        except Exception as e:
            self.logger.error(f"Erreur analyse deep: {e}")
            score, priority = self._calculate_refactoring_score(issues)
            return None, score, priority

    async def _execute_auto_chain_refactoring(
        self,
        request: ConsistencyCheckRequest,
        issues: List[ConsistencyIssue],
        suggested_actions: List[SuggestedAction],
        llm_manager=None,
        ctx=None
    ) -> Optional[Dict[str, Any]]:
        try:

            from .refactoring import RefactoringTool, RefactoringRequest

            if not suggested_actions:
                return None

            best_action = max(suggested_actions, key=lambda a: a.score)

            if best_action.tool_name != "code_refactoring":
                return None

            params = best_action.params
            if not params.get("code"):

                file_with_issues = next(
                    (f for f in request.files
                     if any(i.path == f.path for i in issues)),
                    request.files[0] if request.files else None
                )
                if not file_with_issues:
                    return None
                params["code"] = file_with_issues.content[:5000]
                params["language"] = file_with_issues.language or self._detect_language(file_with_issues.path)
                params["file_path"] = file_with_issues.path

            refactoring_request = RefactoringRequest(
                code=params.get("code", ""),
                language=params.get("language", "python"),
                refactoring_type=params.get("refactoring_type", "clean"),
                file_path=params.get("file_path"),
                parameters={"context": "auto-triggered from repo_consistency_check"}
            )

            refactoring_tool = RefactoringTool(app_state=self.app_state)
            result = refactoring_tool.execute(
                refactoring_request,
                llm_manager=llm_manager,
                ctx=ctx
            )

            self.logger.info(f"Auto-refactoring exécuté sur {params.get('file_path', 'fichier')}")

            return {
                "file_path": params.get("file_path"),
                "refactoring_type": params.get("refactoring_type"),
                "original_code_preview": params.get("code", "")[:200] + "...",
                "refactored_code_preview": result.refactored_code[:200] + "..." if result.refactored_code else None,
                "changes_count": len(result.changes),
                "explanation": result.explanation
            }

        except Exception as e:
            self.logger.error(f"Erreur auto-chain refactoring: {e}")
            return None

    def _execute_core_logic(self, request: ConsistencyCheckRequest, **kwargs) -> ConsistencyCheckResponse:
        self.logger.info(f"Vérification de cohérence sur {len(request.files)} fichier(s)")


        checks = request.checks or self.ALL_CHECKS

        all_issues = []
        all_contents = '\n'.join(f.content for f in request.files)

        for file in request.files:
            lang = file.language or (request.language if request.language != 'auto' else self._detect_language(file.path))

            if lang == 'python':
                if 'unused_imports' in checks:
                    all_issues.extend(self._python_analyzer.analyze_unused_imports(file.content, file.path))
                if 'unused_vars' in checks:
                    all_issues.extend(self._python_analyzer.analyze_unused_vars(file.content, file.path))
                if 'dead_code' in checks:
                    all_issues.extend(self._python_analyzer.analyze_dead_code(file.content, file.path, all_contents))

            elif lang in ('typescript', 'javascript'):
                if 'unused_imports' in checks:
                    all_issues.extend(self._js_analyzer.analyze_unused_imports(file.content, file.path))
                if 'unused_vars' in checks:
                    all_issues.extend(self._js_analyzer.analyze_unused_vars(file.content, file.path))

        if 'duplication' in checks and len(request.files) > 1:
            all_issues.extend(self._analyze_duplication(request.files))

        if 'unresolved_symbol' in checks and request.mode == 'deep':
            all_issues.extend(self._analyze_unresolved_symbols(request.files))

        all_issues = [i for i in all_issues if i.confidence >= request.min_confidence]

        severity_counts = aggregate_severities(all_issues, default_levels=['high', 'medium', 'low', 'info'])

        summary = {
            'total': len(all_issues),
            'high': severity_counts['high'],
            'medium': severity_counts['medium'],
            'low': severity_counts['low'],
            'info': severity_counts['info'],
        }

        llm_manager = kwargs.get('llm_manager') or self.llm_manager
        ctx = kwargs.get('ctx')

        llm_insights = None
        analysis_depth_used = "fast"
        refactoring_score = 0.0
        refactoring_priority = "none"

        if request.analysis_depth == "deep":
            self.logger.info("Mode deep: enrichissement IA en cours...")
            analysis_depth_used = "deep"

            try:
                coro = self._deep_analysis_with_llm(request, all_issues, llm_manager)
                llm_insights, refactoring_score, refactoring_priority = run_async_from_sync(coro, timeout=30)
            except Exception as e:
                self.logger.warning(f"Fallback mode fast suite à erreur deep: {e}")
                refactoring_score, refactoring_priority = self._calculate_refactoring_score(all_issues)
        else:

            refactoring_score, refactoring_priority = self._calculate_refactoring_score(all_issues)

        suggested_actions = self._generate_suggested_actions(all_issues, request.files, refactoring_score)

        auto_refactoring_triggered = False
        auto_refactoring_result = None

        if request.auto_chain and refactoring_score >= request.refactoring_threshold and suggested_actions:
            self.logger.info(f"Auto-chain: score {refactoring_score:.2f} >= seuil {request.refactoring_threshold}")
            try:
                coro = self._execute_auto_chain_refactoring(
                    request, all_issues, suggested_actions, llm_manager, ctx
                )
                auto_refactoring_result = run_async_from_sync(coro, timeout=60)

                if auto_refactoring_result:
                    auto_refactoring_triggered = True
            except Exception as e:
                self.logger.warning(f"Erreur auto-chain: {e}")

        if not all_issues:
            analysis_summary = f"✅ Aucune incohérence détectée dans {len(request.files)} fichier(s)."
        else:
            analysis_summary = (
                f"⚠️ {len(all_issues)} incohérence(s) détectée(s) dans {len(request.files)} fichier(s). "
                f"Haute({severity_counts['high']}), Moyenne({severity_counts['medium']}), "
                f"Basse({severity_counts['low']}), Info({severity_counts['info']})."
            )

        if analysis_depth_used == "deep":
            analysis_summary += f" 🤖 Score refactoring: {refactoring_score:.0%} ({refactoring_priority})."
            if llm_insights:
                analysis_summary += f" {len(llm_insights)} insight(s) IA."

        if auto_refactoring_triggered:
            analysis_summary += " 🔧 Refactoring auto-déclenché."

        return ConsistencyCheckResponse(
            valid=len(all_issues) == 0,
            summary=summary,
            issues=all_issues[:100],
            files_analyzed=len(request.files),
            checks_performed=checks,
            analysis_summary=analysis_summary,
            analysis_depth_used=analysis_depth_used,
            llm_insights=llm_insights,
            refactoring_score=refactoring_score,
            refactoring_priority=refactoring_priority,
            suggested_actions=suggested_actions,
            auto_refactoring_triggered=auto_refactoring_triggered,
            auto_refactoring_result=auto_refactoring_result
        )
