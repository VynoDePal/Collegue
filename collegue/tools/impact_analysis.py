"""
Impact Analysis - Outil d'analyse d'impact des changements de code

Cet outil analyse l'impact potentiel d'un changement de code avant son implémentation:
- Identifie les fichiers impactés par un changement
- Détecte les risques (breaking changes, sécurité, migration)
- Génère des requêtes de recherche pour l'IDE
- Recommande les tests à exécuter

Problème résolu: L'IA génère souvent des changements sans anticiper leurs impacts.
Valeur: Guide la stratégie avant de coder, réduit les itérations.
Bénéfice: Moins de breaking changes, meilleure couverture de tests.
"""
import re
import ast
from typing import Optional, Dict, Any, List, Type, Set, Tuple
from pydantic import BaseModel, Field, field_validator
from .base import BaseTool, ToolError, ToolValidationError, ToolExecutionError


class FileInput(BaseModel):
    """Un fichier avec son chemin et contenu."""
    path: str = Field(..., description="Chemin relatif du fichier")
    content: str = Field(..., description="Contenu du fichier")
    language: Optional[str] = Field(None, description="Langage (auto-détecté si absent)")


class ImpactAnalysisRequest(BaseModel):
    """Modèle de requête pour l'analyse d'impact."""
    change_intent: str = Field(
        ...,
        description="Description du changement prévu (ex: 'renommer UserService en AuthService', 'modifier l'API /users')"
    )
    files: List[FileInput] = Field(
        ...,
        description="Liste des fichiers à analyser [{path, content, language?}, ...]",
        min_length=1
    )
    diff: Optional[str] = Field(
        None,
        description="Diff unifié optionnel du changement proposé"
    )
    entry_points: Optional[List[str]] = Field(
        None,
        description="Points d'entrée importants (ex: 'main.py', 'api/router.ts')"
    )
    assumptions: Optional[List[str]] = Field(
        None,
        description="Contraintes ou hypothèses du projet"
    )
    confidence_mode: str = Field(
        "balanced",
        description="Mode de confiance: 'conservative' (moins de faux positifs), 'balanced', 'aggressive' (plus exhaustif)"
    )
    
    @field_validator('confidence_mode')
    def validate_confidence_mode(cls, v):
        valid = ['conservative', 'balanced', 'aggressive']
        if v not in valid:
            raise ValueError(f"Mode '{v}' invalide. Utilisez: {', '.join(valid)}")
        return v


class ImpactedFile(BaseModel):
    """Un fichier potentiellement impacté."""
    path: str = Field(..., description="Chemin du fichier")
    reason: str = Field(..., description="Raison de l'impact")
    confidence: str = Field(..., description="Niveau de confiance: high, medium, low")
    impact_type: str = Field("direct", description="Type: direct, indirect, test")


class RiskNote(BaseModel):
    """Une note de risque identifiée."""
    category: str = Field(..., description="Catégorie: breaking_change, security, data_migration, performance, compat")
    note: str = Field(..., description="Description du risque")
    confidence: str = Field(..., description="Niveau de confiance")
    severity: str = Field("medium", description="Sévérité: low, medium, high, critical")


class SearchQuery(BaseModel):
    """Une requête de recherche pour l'IDE."""
    query: str = Field(..., description="Pattern de recherche")
    rationale: str = Field(..., description="Pourquoi cette recherche")
    search_type: str = Field("text", description="Type: text, regex, symbol")


class TestRecommendation(BaseModel):
    """Une recommandation de test."""
    command: str = Field(..., description="Commande à exécuter")
    rationale: str = Field(..., description="Pourquoi ce test")
    scope: str = Field("unit", description="Scope: unit, integration, e2e")
    priority: str = Field("medium", description="Priorité: low, medium, high")


class FollowupAction(BaseModel):
    """Une action de suivi recommandée."""
    action: str = Field(..., description="Action à effectuer")
    rationale: str = Field(..., description="Pourquoi cette action")


class ImpactAnalysisResponse(BaseModel):
    """Modèle de réponse pour l'analyse d'impact."""
    change_summary: str = Field(..., description="Résumé du changement analysé")
    impacted_files: List[ImpactedFile] = Field(
        default_factory=list,
        description="Fichiers impactés identifiés"
    )
    risk_notes: List[RiskNote] = Field(
        default_factory=list,
        description="Risques identifiés"
    )
    search_queries: List[SearchQuery] = Field(
        default_factory=list,
        description="Requêtes de recherche pour compléter l'analyse"
    )
    tests_to_run: List[TestRecommendation] = Field(
        default_factory=list,
        description="Tests recommandés"
    )
    followups: List[FollowupAction] = Field(
        default_factory=list,
        description="Actions de suivi"
    )
    analysis_summary: str = Field(..., description="Résumé de l'analyse")


class ImpactAnalysisTool(BaseTool):
    """
    Outil d'analyse d'impact des changements de code.
    
    Analyse un changement décrit en langage naturel et:
    - Identifie les fichiers potentiellement impactés
    - Détecte les risques (breaking changes, sécurité, etc.)
    - Génère des requêtes de recherche pour l'IDE
    - Recommande les tests à exécuter
    
    Fonctionne sur le contenu des fichiers fournis (compatible MCP isolé).
    """

    # Patterns pour extraire les identifiants du change_intent
    IDENTIFIER_PATTERNS = [
        r"renommer\s+['\"`]?(\w+)['\"`]?\s+(?:en|vers|to)\s+['\"`]?(\w+)['\"`]?",
        r"rename\s+['\"`]?(\w+)['\"`]?\s+(?:to|as)\s+['\"`]?(\w+)['\"`]?",
        r"modifier\s+(?:l[ea']?\s*)?['\"`]?(\w+)['\"`]?",
        r"modify\s+['\"`]?(\w+)['\"`]?",
        r"supprimer\s+(?:l[ea']?\s*)?['\"`]?(\w+)['\"`]?",
        r"delete\s+['\"`]?(\w+)['\"`]?",
        r"ajouter\s+(?:un[e]?\s*)?['\"`]?(\w+)['\"`]?",
        r"add\s+['\"`]?(\w+)['\"`]?",
        r"changer\s+(?:l[ea']?\s*)?['\"`]?(\w+)['\"`]?",
        r"change\s+['\"`]?(\w+)['\"`]?",
        r"/api/[\w/]+",
        r"[A-Z][a-z]+(?:[A-Z][a-z]+)+",  # CamelCase
        r"[a-z]+(?:_[a-z]+)+",  # snake_case
    ]
    
    # Patterns d'API REST
    API_PATTERNS = [
        r"(?:GET|POST|PUT|DELETE|PATCH)\s+(/[\w/{}:-]+)",
        r"@(?:app|router|api)\.(?:get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)['\"]",
        r"fetch\s*\(\s*['\"`]([^'\"`]+)['\"`]",
        r"axios\.(?:get|post|put|delete|patch)\s*\(\s*['\"`]([^'\"`]+)['\"`]",
    ]
    
    # Patterns de risque
    RISK_PATTERNS = {
        'breaking_change': [
            (r"def\s+(\w+)\s*\([^)]*\)\s*:", "Modification de signature de fonction"),
            (r"class\s+(\w+)\s*(?:\([^)]*\))?:", "Modification de classe"),
            (r"export\s+(?:default\s+)?(?:function|class|const)\s+(\w+)", "Export modifié"),
        ],
        'security': [
            (r"(?:password|secret|token|key|api_key)\s*=", "Variable sensible"),
            (r"(?:eval|exec)\s*\(", "Exécution dynamique"),
            (r"(?:innerHTML|dangerouslySetInnerHTML)", "Injection HTML potentielle"),
        ],
        'data_migration': [
            (r"(?:CREATE|ALTER|DROP)\s+(?:TABLE|INDEX|DATABASE)", "Modification de schéma"),
            (r"\.migrate\s*\(", "Migration de données"),
            (r"(?:model|schema)\.(?:add|remove|change)_field", "Modification de modèle"),
        ],
        'performance': [
            (r"for\s+\w+\s+in\s+.*for\s+\w+\s+in", "Boucle imbriquée"),
            (r"\.all\(\)", "Chargement complet en mémoire"),
            (r"SELECT\s+\*", "SELECT sans limite"),
        ],
    }

    def get_name(self) -> str:
        return "impact_analysis"

    def get_description(self) -> str:
        return "Analyse l'impact d'un changement de code: fichiers impactés, risques, tests à lancer"

    def get_request_model(self) -> Type[BaseModel]:
        return ImpactAnalysisRequest

    def get_response_model(self) -> Type[BaseModel]:
        return ImpactAnalysisResponse

    def get_supported_languages(self) -> List[str]:
        return ["python", "typescript", "javascript", "java", "go", "rust", "ruby", "any"]

    def is_long_running(self) -> bool:
        return False

    def get_usage_description(self) -> str:
        return (
            "Analyse l'impact potentiel d'un changement de code avant son implémentation. "
            "Identifie les fichiers impactés, détecte les risques, et recommande les tests à exécuter."
        )

    def get_examples(self) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Analyser un renommage",
                "request": {
                    "change_intent": "Renommer UserService en AuthService",
                    "files": [{"path": "services/user.py", "content": "class UserService:..."}]
                }
            },
            {
                "title": "Analyser une modification d'API",
                "request": {
                    "change_intent": "Modifier l'API /api/users pour ajouter pagination",
                    "files": [{"path": "routes/users.ts", "content": "router.get('/users'..."}],
                    "entry_points": ["routes/index.ts"]
                }
            }
        ]

    def get_capabilities(self) -> List[str]:
        return [
            "Identification des fichiers impactés par un changement",
            "Détection de risques (breaking changes, sécurité, migration)",
            "Génération de requêtes de recherche pour l'IDE",
            "Recommandation de tests à exécuter",
            "Support multi-langages (Python, TypeScript, JavaScript, etc.)"
        ]

    def _detect_language(self, filepath: str) -> str:
        """Détecte le langage à partir de l'extension."""
        ext_map = {
            '.py': 'python',
            '.ts': 'typescript', '.tsx': 'typescript',
            '.js': 'javascript', '.jsx': 'javascript', '.mjs': 'javascript',
            '.java': 'java',
            '.go': 'go',
            '.rs': 'rust',
            '.rb': 'ruby',
            '.php': 'php',
            '.cs': 'csharp',
        }
        ext = '.' + filepath.split('.')[-1] if '.' in filepath else ''
        return ext_map.get(ext.lower(), 'unknown')

    def _extract_identifiers_from_intent(self, intent: str) -> Set[str]:
        """Extrait les identifiants mentionnés dans l'intention de changement."""
        identifiers = set()
        
        for pattern in self.IDENTIFIER_PATTERNS:
            matches = re.findall(pattern, intent, re.IGNORECASE)
            for match in matches:
                if isinstance(match, tuple):
                    identifiers.update(m for m in match if m and len(m) > 2)
                elif match and len(match) > 2:
                    identifiers.add(match)
        
        # Extraire aussi les mots CamelCase ou snake_case explicites
        words = re.findall(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+|[a-z]+(?:_[a-z]+)+)\b', intent)
        identifiers.update(w for w in words if len(w) > 3)
        
        return identifiers

    def _extract_api_endpoints(self, intent: str, files: List[FileInput]) -> Set[str]:
        """Extrait les endpoints API mentionnés."""
        endpoints = set()
        
        # Depuis l'intent
        for pattern in self.API_PATTERNS:
            matches = re.findall(pattern, intent, re.IGNORECASE)
            endpoints.update(matches)
        
        # Depuis les fichiers
        for file in files:
            for pattern in self.API_PATTERNS:
                matches = re.findall(pattern, file.content)
                endpoints.update(matches)
        
        return endpoints

    def _analyze_python_imports(self, content: str, filepath: str) -> List[Tuple[str, str]]:
        """Analyse les imports Python."""
        imports = []
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append((alias.name, filepath))
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ''
                    for alias in node.names:
                        imports.append((f"{module}.{alias.name}", filepath))
        except SyntaxError:
            # Fallback regex
            import_matches = re.findall(r'^(?:from\s+([\w.]+)\s+)?import\s+([\w,\s]+)', content, re.MULTILINE)
            for match in import_matches:
                module, names = match
                for name in names.split(','):
                    name = name.strip().split(' as ')[0].strip()
                    if module:
                        imports.append((f"{module}.{name}", filepath))
                    else:
                        imports.append((name, filepath))
        return imports

    def _analyze_js_imports(self, content: str, filepath: str) -> List[Tuple[str, str]]:
        """Analyse les imports JavaScript/TypeScript."""
        imports = []
        patterns = [
            r"import\s+(?:{[^}]+}|\*\s+as\s+\w+|\w+)\s+from\s+['\"]([^'\"]+)['\"]",
            r"require\s*\(\s*['\"]([^'\"]+)['\"]",
            r"import\s*\(\s*['\"]([^'\"]+)['\"]",
        ]
        for pattern in patterns:
            matches = re.findall(pattern, content)
            imports.extend((m, filepath) for m in matches)
        return imports

    def _find_usages(self, identifier: str, files: List[FileInput]) -> List[ImpactedFile]:
        """Trouve les usages d'un identifiant dans les fichiers."""
        impacted = []
        
        # Patterns de recherche pour l'identifiant
        patterns = [
            rf'\b{re.escape(identifier)}\b',
            rf'\b{re.escape(identifier.lower())}\b',
            rf'\b{re.escape(identifier.replace("_", "-"))}\b',
        ]
        
        for file in files:
            for pattern in patterns:
                if re.search(pattern, file.content, re.IGNORECASE):
                    # Compter les occurrences
                    count = len(re.findall(pattern, file.content, re.IGNORECASE))
                    impacted.append(ImpactedFile(
                        path=file.path,
                        reason=f"Contient {count} référence(s) à '{identifier}'",
                        confidence="high" if count > 1 else "medium",
                        impact_type="direct"
                    ))
                    break
        
        return impacted

    def _analyze_risks(self, intent: str, files: List[FileInput], diff: Optional[str]) -> List[RiskNote]:
        """Analyse les risques potentiels."""
        risks = []
        
        # Analyser le diff si disponible
        content_to_analyze = diff or intent
        for file in files:
            content_to_analyze += "\n" + file.content
        
        for category, patterns in self.RISK_PATTERNS.items():
            for pattern, description in patterns:
                if re.search(pattern, content_to_analyze, re.IGNORECASE):
                    severity = "high" if category in ["security", "breaking_change"] else "medium"
                    risks.append(RiskNote(
                        category=category,
                        note=description,
                        confidence="medium",
                        severity=severity
                    ))
        
        # Risques spécifiques basés sur l'intent
        intent_lower = intent.lower()
        
        if any(kw in intent_lower for kw in ["supprimer", "delete", "remove"]):
            risks.append(RiskNote(
                category="breaking_change",
                note="La suppression peut casser les dépendances existantes",
                confidence="high",
                severity="high"
            ))
        
        if any(kw in intent_lower for kw in ["renommer", "rename"]):
            risks.append(RiskNote(
                category="breaking_change",
                note="Le renommage nécessite une mise à jour de toutes les références",
                confidence="high",
                severity="medium"
            ))
        
        if any(kw in intent_lower for kw in ["api", "endpoint", "route"]):
            risks.append(RiskNote(
                category="compat",
                note="Modification d'API: vérifier la rétrocompatibilité et la documentation",
                confidence="medium",
                severity="medium"
            ))
        
        return risks

    def _generate_search_queries(self, identifiers: Set[str], endpoints: Set[str], 
                                  files: List[FileInput]) -> List[SearchQuery]:
        """Génère des requêtes de recherche pour compléter l'analyse."""
        queries = []
        
        for identifier in list(identifiers)[:10]:
            queries.append(SearchQuery(
                query=identifier,
                rationale=f"Trouver toutes les références à '{identifier}'",
                search_type="symbol"
            ))
        
        for endpoint in list(endpoints)[:5]:
            queries.append(SearchQuery(
                query=endpoint,
                rationale=f"Trouver les appels à l'endpoint '{endpoint}'",
                search_type="text"
            ))
        
        # Patterns génériques utiles
        if any(f.path.endswith(('.py',)) for f in files):
            queries.append(SearchQuery(
                query=r"from\s+\.\w+\s+import",
                rationale="Trouver les imports relatifs (peuvent être impactés)",
                search_type="regex"
            ))
        
        return queries

    def _recommend_tests(self, identifiers: Set[str], files: List[FileInput], 
                         risks: List[RiskNote]) -> List[TestRecommendation]:
        """Recommande les tests à exécuter."""
        tests = []
        
        # Déterminer le framework de test probable
        has_python = any(f.path.endswith('.py') for f in files)
        has_js = any(f.path.endswith(('.js', '.ts', '.jsx', '.tsx')) for f in files)
        
        if has_python:
            tests.append(TestRecommendation(
                command="pytest --tb=short -v",
                rationale="Exécuter tous les tests unitaires Python",
                scope="unit",
                priority="high"
            ))
            
            for identifier in list(identifiers)[:3]:
                tests.append(TestRecommendation(
                    command=f"pytest -k '{identifier}' -v",
                    rationale=f"Tests spécifiques liés à '{identifier}'",
                    scope="unit",
                    priority="medium"
                ))
        
        if has_js:
            tests.append(TestRecommendation(
                command="npm test -- --passWithNoTests",
                rationale="Exécuter les tests JavaScript/TypeScript",
                scope="unit",
                priority="high"
            ))
            
            tests.append(TestRecommendation(
                command="npx jest --findRelatedTests <changed-files>",
                rationale="Tests liés aux fichiers modifiés",
                scope="unit",
                priority="medium"
            ))
        
        # Tests basés sur les risques
        if any(r.category == "breaking_change" for r in risks):
            tests.append(TestRecommendation(
                command="# Exécuter tests d'intégration complets",
                rationale="Breaking change détecté: tests d'intégration recommandés",
                scope="integration",
                priority="high"
            ))
        
        if any(r.category == "security" for r in risks):
            tests.append(TestRecommendation(
                command="# Exécuter tests de sécurité",
                rationale="Risque sécurité détecté: audit recommandé",
                scope="integration",
                priority="high"
            ))
        
        return tests

    def _generate_followups(self, risks: List[RiskNote], impacted_count: int) -> List[FollowupAction]:
        """Génère les actions de suivi recommandées."""
        followups = []
        
        if impacted_count > 5:
            followups.append(FollowupAction(
                action="Considérer un refactoring incrémental au lieu d'un changement massif",
                rationale=f"{impacted_count} fichiers impactés: risque élevé de régression"
            ))
        
        if any(r.category == "breaking_change" for r in risks):
            followups.append(FollowupAction(
                action="Documenter le breaking change dans le CHANGELOG",
                rationale="Informer les utilisateurs du changement"
            ))
            followups.append(FollowupAction(
                action="Vérifier les dépendances externes qui utilisent ce code",
                rationale="D'autres projets peuvent être impactés"
            ))
        
        if any(r.category == "data_migration" for r in risks):
            followups.append(FollowupAction(
                action="Préparer un script de migration de données",
                rationale="Changement de schéma détecté"
            ))
            followups.append(FollowupAction(
                action="Tester la migration sur un environnement de staging",
                rationale="Éviter la perte de données en production"
            ))
        
        if any(r.category == "security" for r in risks):
            followups.append(FollowupAction(
                action="Faire une revue de sécurité du changement",
                rationale="Risque sécurité identifié"
            ))
        
        followups.append(FollowupAction(
            action="Mettre à jour la documentation si nécessaire",
            rationale="Garder la documentation synchronisée avec le code"
        ))
        
        return followups

    def _execute_core_logic(self, request: ImpactAnalysisRequest, **kwargs) -> ImpactAnalysisResponse:
        """Exécute l'analyse d'impact."""
        self.logger.info(f"Analyse d'impact: {request.change_intent[:50]}...")
        
        # Extraire les identifiants et endpoints
        identifiers = self._extract_identifiers_from_intent(request.change_intent)
        endpoints = self._extract_api_endpoints(request.change_intent, request.files)
        
        self.logger.debug(f"Identifiants extraits: {identifiers}")
        self.logger.debug(f"Endpoints extraits: {endpoints}")
        
        # Analyser les imports de chaque fichier
        all_imports = []
        for file in request.files:
            lang = file.language or self._detect_language(file.path)
            if lang == 'python':
                all_imports.extend(self._analyze_python_imports(file.content, file.path))
            elif lang in ('typescript', 'javascript'):
                all_imports.extend(self._analyze_js_imports(file.content, file.path))
        
        # Trouver les fichiers impactés
        impacted_files = []
        seen_paths = set()
        
        for identifier in identifiers:
            for impact in self._find_usages(identifier, request.files):
                if impact.path not in seen_paths:
                    impacted_files.append(impact)
                    seen_paths.add(impact.path)
        
        # Ajuster la confiance selon le mode
        if request.confidence_mode == 'conservative':
            impacted_files = [f for f in impacted_files if f.confidence == 'high']
        elif request.confidence_mode == 'aggressive':
            # Ajouter des fichiers avec imports potentiellement liés
            for module, filepath in all_imports:
                for identifier in identifiers:
                    if identifier.lower() in module.lower() and filepath not in seen_paths:
                        impacted_files.append(ImpactedFile(
                            path=filepath,
                            reason=f"Import potentiellement lié: {module}",
                            confidence="low",
                            impact_type="indirect"
                        ))
                        seen_paths.add(filepath)
        
        # Analyser les risques
        risks = self._analyze_risks(request.change_intent, request.files, request.diff)
        
        # Générer les requêtes de recherche
        search_queries = self._generate_search_queries(identifiers, endpoints, request.files)
        
        # Recommander les tests
        tests = self._recommend_tests(identifiers, request.files, risks)
        
        # Générer les followups
        followups = self._generate_followups(risks, len(impacted_files))
        
        # Construire le résumé
        risk_summary = ""
        if risks:
            critical_risks = [r for r in risks if r.severity in ('high', 'critical')]
            if critical_risks:
                risk_summary = f" ⚠️ {len(critical_risks)} risque(s) important(s) détecté(s)."
        
        analysis_summary = (
            f"Analyse de '{request.change_intent[:50]}...': "
            f"{len(impacted_files)} fichier(s) potentiellement impacté(s), "
            f"{len(risks)} risque(s) identifié(s), "
            f"{len(tests)} test(s) recommandé(s).{risk_summary}"
        )
        
        return ImpactAnalysisResponse(
            change_summary=request.change_intent,
            impacted_files=impacted_files[:50],  # Limiter
            risk_notes=risks,
            search_queries=search_queries[:20],
            tests_to_run=tests[:15],
            followups=followups[:10],
            analysis_summary=analysis_summary
        )
