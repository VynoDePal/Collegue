"""
Moteur d'analyse d'impact pour l'outil Impact Analysis.

Contient la logique métier pure : extraction d'identifiants, analyse de fichiers,
détection de risques, génération de recommandations.
"""
import re
import ast
from typing import List, Dict, Any, Optional, Set, Tuple
from .config import (
    IDENTIFIER_PATTERNS, RISK_PATTERNS, RISK_CATEGORIES,
    CONFIDENCE_THRESHOLDS, TEST_FILE_EXTENSIONS, TEST_COMMANDS
)


class ImpactAnalysisEngine:
    """Moteur d'analyse d'impact des changements de code."""
    
    def __init__(self, logger=None):
        self.logger = logger
    
    def extract_identifiers(self, change_intent: str) -> Set[str]:
        """Extrait les identifiants (noms de symboles) de la description du changement."""
        identifiers = set()
        
        for pattern in IDENTIFIER_PATTERNS:
            matches = re.finditer(pattern, change_intent, re.IGNORECASE)
            for match in matches:
                for group in match.groups() or [match.group()]:
                    if group and len(group) > 2:
                        identifiers.add(group)
        
        # Nettoyer les identifiants
        cleaned = set()
        for ident in identifiers:
            ident = ident.strip("'\"`")
            if ident and not ident.lower() in ['la', 'le', 'the', 'a', 'an']:
                cleaned.add(ident)
        
        return cleaned
    
    def analyze_single_file(
        self,
        file,
        identifiers: Set[str],
        confidence_threshold: float,
        entry_points: Optional[List[str]] = None
    ) -> List[Dict[str, str]]:
        """Analyse un fichier pour trouver les impacts potentiels."""
        impacts = []
        content = file.content
        filepath = file.path
        
        # Vérifier si c'est un point d'entrée
        is_entry_point = entry_points and any(
            ep in filepath or filepath.endswith(ep) for ep in entry_points
        )
        
        # Analyse textuelle simple
        for identifier in identifiers:
            if identifier in content:
                occurrences = content.count(identifier)
                
                # Calculer la confiance
                confidence = "high" if occurrences >= 3 else "medium" if occurrences >= 1 else "low"
                
                # Déterminer le type d'impact
                if is_entry_point:
                    impact_type = "direct"
                elif self._is_test_file(filepath):
                    impact_type = "test"
                elif occurrences > 5:
                    impact_type = "direct"
                else:
                    impact_type = "indirect"
                
                impacts.append({
                    "path": filepath,
                    "reason": f"Contient '{identifier}' ({occurrences} occurrence(s))",
                    "confidence": confidence,
                    "impact_type": impact_type,
                    "identifier": identifier
                })
        
        # Analyse syntaxique pour Python
        if filepath.endswith('.py'):
            try:
                tree = ast.parse(content)
                imports = self._extract_python_imports(tree)
                
                for identifier in identifiers:
                    if identifier in imports:
                        impacts.append({
                            "path": filepath,
                            "reason": f"Importe '{identifier}'",
                            "confidence": "high",
                            "impact_type": "direct"
                        })
            except SyntaxError:
                pass
        
        return impacts
    
    def _extract_python_imports(self, tree) -> Set[str]:
        """Extrait les imports d'un AST Python."""
        imports = set()
        
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split('.')[0])
        
        return imports
    
    def _is_test_file(self, filepath: str) -> bool:
        """Détermine si un fichier est un fichier de test."""
        test_patterns = ['_test.', 'test_', '.test.', '.spec.', '_spec.', 'Test.', 'Tests.']
        return any(pattern in filepath for pattern in test_patterns)
    
    def analyze_risks(self, change_intent: str, identifiers: Set[str]) -> List[Dict[str, str]]:
        """Analyse les risques potentiels du changement."""
        risks = []
        intent_lower = change_intent.lower()
        
        for category, patterns in RISK_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, intent_lower, re.IGNORECASE):
                    confidence = "high" if category == "breaking_change" else "medium"
                    severity = "high" if category in ["breaking_change", "security"] else "medium"
                    
                    risks.append({
                        "category": category,
                        "note": RISK_CATEGORIES.get(category, f"Risque identifié: {category}"),
                        "confidence": confidence,
                        "severity": severity
                    })
                    break  # Une seule note par catégorie
        
        return risks
    
    def generate_search_queries(self, identifiers: Set[str], change_intent: str) -> List[Dict[str, str]]:
        """Génère des requêtes de recherche pour compléter l'analyse."""
        queries = []
        
        for identifier in list(identifiers)[:5]:
            queries.append({
                "query": identifier,
                "rationale": f"Rechercher toutes les utilisations de '{identifier}'",
                "search_type": "symbol"
            })
            
            # Recherche regex pour les variations
            if '_' in identifier:
                camel_case = self._to_camel_case(identifier)
                queries.append({
                    "query": camel_case,
                    "rationale": f"Rechercher la version camelCase '{camel_case}'",
                    "search_type": "text"
                })
        
        # Requêtes spécifiques au type de changement
        if "renommer" in change_intent.lower() or "rename" in change_intent.lower():
            for identifier in identifiers:
                queries.append({
                    "query": rf"\b{identifier}\b",
                    "rationale": f"Rechercher '{identifier}' comme mot entier",
                    "search_type": "regex"
                })
        
        return queries
    
    def _to_camel_case(self, snake_str: str) -> str:
        """Convertit snake_case en CamelCase."""
        components = snake_str.split('_')
        return components[0] + ''.join(x.title() for x in components[1:])
    
    def recommend_tests(
        self,
        impacted_files: List[Dict[str, str]],
        language: str,
        framework: Optional[str] = None
    ) -> List[Dict[str, str]]:
        """Recommande les tests à exécuter."""
        recommendations = []
        
        # Trouver les fichiers de test impactés
        test_files = [f for f in impacted_files if self._is_test_file(f["path"])]
        
        # Recommander les tests pour les fichiers sources impactés
        source_files = [f for f in impacted_files if not self._is_test_file(f["path"])]
        
        for source_file in source_files[:5]:
            path = source_file["path"]
            
            # Chercher un fichier de test correspondant
            test_path = self._guess_test_file(path, language)
            if test_path:
                recommendations.append({
                    "command": f"# Vérifier si le fichier de test existe et l'exécuter\n# {test_path}",
                    "rationale": f"Tests pour {path}",
                    "scope": "unit",
                    "priority": "high" if source_file.get("impact_type") == "direct" else "medium"
                })
        
        # Ajouter les tests déjà identifiés comme impactés
        for test_file in test_files[:3]:
            recommendations.append({
                "command": f"# Exécuter: {test_file['path']}",
                "rationale": "Fichier de test directement impacté",
                "scope": "unit",
                "priority": "high"
            })
        
        return recommendations
    
    def _guess_test_file(self, source_path: str, language: str) -> Optional[str]:
        """Devine le chemin du fichier de test correspondant."""
        extensions = TEST_FILE_EXTENSIONS.get(language, [])
        
        for ext in extensions:
            if '_' in ext or ext.startswith('.'):
                # Pattern comme .test.py ou _test.py
                if ext.startswith('.'):
                    return source_path.replace('.py', ext) if language == 'python' else source_path + ext
                else:
                    return source_path.replace('.py', ext) if language == 'python' else source_path + ext
        
        return None
    
    def generate_followup_actions(
        self,
        impacted_files: List[Dict[str, str]],
        risk_notes: List[Dict[str, str]]
    ) -> List[Dict[str, str]]:
        """Génère des actions de suivi recommandées."""
        actions = []
        
        # Actions basées sur les risques
        for risk in risk_notes:
            if risk["category"] == "breaking_change":
                actions.append({
                    "action": "Vérifier la compatibilité ascendante",
                    "rationale": "Changement cassant potentiel détecté"
                })
            elif risk["category"] == "security":
                actions.append({
                    "action": "Effectuer une revue de sécurité",
                    "rationale": "Risque de sécurité identifié"
                })
            elif risk["category"] == "data_migration":
                actions.append({
                    "action": "Préparer un script de migration",
                    "rationale": "Migration de données nécessaire"
                })
        
        # Actions basées sur les fichiers impactés
        direct_impacts = [f for f in impacted_files if f.get("impact_type") == "direct"]
        if len(direct_impacts) > 10:
            actions.append({
                "action": "Envisager un déploiement progressif (canary)",
                "rationale": f"{len(direct_impacts)} fichiers directement impactés"
            })
        
        if not actions:
            actions.append({
                "action": "Exécuter la suite de tests complète",
                "rationale": "Validation standard après changement"
            })
        
        return actions
    
    def build_analysis_summary(
        self,
        change_intent: str,
        impacted_files: List[Dict[str, str]],
        risk_notes: List[Dict[str, str]]
    ) -> str:
        """Construit le résumé de l'analyse."""
        parts = [f"Analyse d'impact pour: {change_intent}"]
        
        parts.append(f"\nFichiers impactés: {len(impacted_files)}")
        
        direct = len([f for f in impacted_files if f.get("impact_type") == "direct"])
        indirect = len([f for f in impacted_files if f.get("impact_type") == "indirect"])
        tests = len([f for f in impacted_files if f.get("impact_type") == "test"])
        
        if direct > 0:
            parts.append(f"  - Directs: {direct}")
        if indirect > 0:
            parts.append(f"  - Indirects: {indirect}")
        if tests > 0:
            parts.append(f"  - Tests: {tests}")
        
        if risk_notes:
            parts.append(f"\nRisques identifiés: {len(risk_notes)}")
            for risk in risk_notes:
                parts.append(f"  - [{risk['severity'].upper()}] {risk['note']}")
        
        return "\n".join(parts)
    
    def filter_by_confidence(
        self,
        items: List[Dict[str, str]],
        confidence_mode: str
    ) -> List[Dict[str, str]]:
        """Filtre les résultats selon le mode de confiance."""
        confidence_priority = {"high": 3, "medium": 2, "low": 1}
        
        if confidence_mode == "conservative":
            return [item for item in items if confidence_priority.get(item.get("confidence"), 0) >= 3]
        elif confidence_mode == "balanced":
            return [item for item in items if confidence_priority.get(item.get("confidence"), 0) >= 2]
        else:  # aggressive
            return items
