"""
PHP Analyzer for Repo Consistency Check.

Détecte les imports (use) et variables inutilisés dans le code PHP.
"""
import re
from typing import List, Dict, Any
from .base import BaseAnalyzer, AnalyzerError
from ...core.shared import ConsistencyIssue

class PHPAnalyzer(BaseAnalyzer):
    def analyze_unused_imports(self, code: str, filepath: str) -> List[ConsistencyIssue]:
        issues = []
        
        # Regex pour capturer les 'use' (imports)
        # Format: use Namespace\Class; ou use Namespace\Class as Alias;
        import_pattern = r"^use\s+([a-zA-Z0-9_\\]+)(?:\s+as\s+([a-zA-Z0-9_]+))?\s*;"
        
        lines = code.split('\n')
        defined_imports = []
        
        for i, line in enumerate(lines):
            line = line.strip()
            # Ignorer les use dans les classes (Traits) ou closures
            if not line.startswith('use '):
                continue
                
            match = re.search(import_pattern, line)
            if match:
                full_class = match.group(1)
                alias = match.group(2)
                
                # Le nom à chercher dans le code est l'alias s'il existe, sinon le dernier segment du namespace
                name_to_check = alias if alias else full_class.split('\\')[-1]
                
                defined_imports.append({
                    'name': name_to_check,
                    'full_name': full_class,
                    'line': i + 1,
                    'statement': line
                })
        
        # Analyser le reste du code pour trouver les usages
        content_without_imports = '\n'.join([l for l in lines if not l.strip().startswith('use ')])
        
        for imp in defined_imports:
            # Recherche simple du nom de la classe/alias
            # Attention aux faux positifs (commentaires, chaînes), mais acceptable pour une analyse statique légère
            # On cherche le mot entier précédé par non-alphanumérique ou début de ligne
            # et suivi par non-alphanumérique ou fin de ligne
            pattern = r'(?:^|[^a-zA-Z0-9_])' + re.escape(imp['name']) + r'(?:[^a-zA-Z0-9_]|$)'
            
            if not re.search(pattern, content_without_imports):
                issues.append(ConsistencyIssue(
                    kind="unused_imports",
                    severity="low",
                    path=filepath,
                    line=imp['line'],
                    message=f"Import inutilisé: {imp['name']}",
                    confidence=90,
                    suggested_fix=f"Supprimer la ligne: {imp['statement']}",
                    engine="php-regex-analyzer"
                ))
                
        return issues

    def analyze_unused_vars(self, code: str, filepath: str) -> List[ConsistencyIssue]:
        issues = []
        lines = code.split('\n')
        
        # Analyse simple portée par fonction pour éviter trop de faux positifs
        # On détecte les définitions de variables: $var = ...
        # Et on vérifie si $var est utilisé ensuite
        
        # Pattern d'assignation: $variable = ...
        assign_pattern = r"(\$[a-zA-Z0-9_]+)\s*="
        
        # Pattern d'usage: $variable (sans être suivi de =)
        
        variables = {} # {name: {line: int, used: bool}}
        
        for i, line in enumerate(lines):
            line = line.strip()
            if line.startswith('//') or line.startswith('#') or line.startswith('*'):
                continue
                
            # Détection des usages d'abord (pour gérer $a = $a + 1)
            for var_name in list(variables.keys()):
                # Si la variable apparaît dans la ligne et ce n'est pas (uniquement) une assignation de cette variable
                # Cas simple: on marque comme utilisé si on trouve la string
                if var_name in line:
                    # Vérifier si c'est une assignation de cette variable
                    is_assignment = re.search(re.escape(var_name) + r"\s*=", line)
                    
                    # Si c'est une assignation, ça compte comme usage seulement si c'est $a .= ... ou $a = $a ...
                    # Pour simplifier: si le nom apparaît 2 fois ou si pas d'assignation
                    if not is_assignment or line.count(var_name) > 1:
                        variables[var_name]['used'] = True
            
            # Détection des déclarations
            match = re.search(assign_pattern, line)
            if match:
                var_name = match.group(1)
                # On ne suit que les variables locales (pas $this->...)
                if var_name != '$this' and not var_name.startswith('$_'):
                    if var_name not in variables:
                        variables[var_name] = {'line': i + 1, 'used': False}
        
        for name, data in variables.items():
            if not data['used']:
                issues.append(ConsistencyIssue(
                    kind="unused_vars",
                    severity="medium",
                    path=filepath,
                    line=data['line'],
                    message=f"Variable locale inutilisée: {name}",
                    confidence=70, # Regex moins fiable que AST
                    suggested_fix=f"Supprimer la variable ou l'utiliser",
                    engine="php-regex-analyzer"
                ))
                
        return issues

    def analyze_dead_code(self, code: str, filepath: str, all_code: str = "") -> List[ConsistencyIssue]:
        # Analyse basique des méthodes privées non utilisées
        issues = []
        
        # Pattern: private function name(...)
        private_method_pattern = r"private\s+function\s+([a-zA-Z0-9_]+)\s*\("
        
        methods = []
        for i, line in enumerate(code.split('\n')):
            match = re.search(private_method_pattern, line)
            if match:
                methods.append({'name': match.group(1), 'line': i + 1})
        
        for method in methods:
            # Recherche de ->name( ou ::name(
            call_pattern = r"(?:->|::)" + re.escape(method['name']) + r"\s*\("
            
            # On cherche dans le fichier courant (car méthode privée)
            # On exclut la définition elle-même (approximatif)
            matches = re.findall(call_pattern, code)
            
            # Si < 1 appel (c'est-à-dire 0, car la regex ne matche pas la définition 'private function ...')
            if len(matches) == 0:
                 issues.append(ConsistencyIssue(
                    kind="dead_code",
                    severity="medium",
                    path=filepath,
                    line=method['line'],
                    message=f"Méthode privée non utilisée: {method['name']}",
                    confidence=80,
                    suggested_fix=f"Supprimer la méthode si elle n'est pas utilisée via call_user_func",
                    engine="php-regex-analyzer"
                ))
                
        return issues
