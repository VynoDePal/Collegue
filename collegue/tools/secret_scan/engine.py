"""
Moteur de détection des secrets pour l'outil Secret Scan.

Contient la logique métier pure pour la détection, le masquage
et la gestion des secrets.
"""
import os
import re
import fnmatch
from typing import List, Optional, Tuple
from .models import SecretFinding
from .config import SECRET_PATTERNS, SEVERITY_LEVELS, SECRET_RECOMMENDATIONS, DEFAULT_EXTENSIONS, DEFAULT_EXCLUDES
from ...core.file_security import safe_read_file, FileSecurityError


class SecretDetectionEngine:
    """Moteur de détection des secrets dans le code."""
    
    def __init__(self, logger=None):
        self.logger = logger
        self.patterns = [(name, re.compile(pattern), severity, desc) 
                        for name, pattern, severity, desc in SECRET_PATTERNS]
    
    def mask_secret(self, secret: str, visible_chars: int = 4) -> str:
        """Masque un secret en ne montrant que les premiers et derniers caractères."""
        if len(secret) <= visible_chars * 2:
            return '*' * len(secret)
        return secret[:visible_chars] + '*' * (len(secret) - visible_chars * 2) + secret[-visible_chars:]
    
    def get_recommendation(self, secret_type: str) -> str:
        """Retourne une recommandation pour corriger le secret exposé."""
        for key, rec in SECRET_RECOMMENDATIONS.items():
            if key in secret_type.lower():
                return rec
        return "Supprimez ce secret du code et utilisez une méthode sécurisée (variables d'environnement, vault)."
    
    def should_scan_file(self, filepath: str, include_patterns: List[str], 
                        exclude_patterns: List[str]) -> bool:
        """Détermine si un fichier doit être scanné."""
        filename = os.path.basename(filepath)
        
        # Vérifier les exclusions
        for pattern in exclude_patterns:
            if fnmatch.fnmatch(filepath, pattern) or fnmatch.fnmatch(filename, pattern):
                return False
            for part in filepath.split(os.sep):
                if fnmatch.fnmatch(part, pattern):
                    return False
        
        # Vérifier les inclusions si spécifiées
        if include_patterns:
            for pattern in include_patterns:
                if fnmatch.fnmatch(filepath, pattern) or fnmatch.fnmatch(filename, pattern):
                    return True
            return False
        
        # Vérifier l'extension
        ext = os.path.splitext(filepath)[1].lower()
        if not ext and filename.startswith('.'):
            return True
        return ext in DEFAULT_EXTENSIONS
    
    def scan_content(self, content: str, filepath: Optional[str] = None,
                    severity_threshold: str = "low") -> List[SecretFinding]:
        """Scanne le contenu pour trouver des secrets."""
        findings = []
        threshold_level = SEVERITY_LEVELS.get(severity_threshold, 1)
        lines = content.split('\n')
        
        for name, regex, severity, description in self.patterns:
            if SEVERITY_LEVELS.get(severity, 1) < threshold_level:
                continue
            
            try:
                for match in regex.finditer(content):
                    start = match.start()
                    line_num = content[:start].count('\n') + 1
                    line_start = content.rfind('\n', 0, start) + 1
                    col_num = start - line_start + 1
                    
                    # Récupérer le contenu de la ligne
                    if line_num <= len(lines):
                        line_content = lines[line_num - 1]
                    else:
                        line_content = match.group()
                    
                    matched_text = match.group()
                    masked_match = self.mask_secret(matched_text)
                    masked_line = line_content.replace(matched_text, masked_match)
                    
                    findings.append(SecretFinding(
                        type=name,
                        severity=severity,
                        file=filepath,
                        line=line_num,
                        column=col_num,
                        match=masked_line.strip()[:200],
                        rule=description,
                        recommendation=self.get_recommendation(name)
                    ))
            except re.error as e:
                if self.logger:
                    self.logger.warning(f"Erreur regex pour {name}: {e}")
        
        return findings
    
    def scan_file(self, filepath: str, severity_threshold: str, 
                 max_size: int) -> List[SecretFinding]:
        """Scanne un fichier pour trouver des secrets."""
        try:
            # Utiliser safe_read_file pour éviter les attaques TOCTOU et symlinks
            content = safe_read_file(filepath, max_size)
            return self.scan_content(content, filepath, severity_threshold)
        except FileSecurityError as e:
            # Log les violations de sécurité au niveau warning
            if self.logger:
                self.logger.warning(f"Security violation for {filepath}: {e}")
            return []
        except Exception as e:
            if self.logger:
                self.logger.warning(f"Erreur lecture {filepath}: {e}")
            return []
    
    def scan_directory(self, dirpath: str, include_patterns: List[str],
                      exclude_patterns: List[str], severity_threshold: str,
                      max_size: int) -> Tuple[List[SecretFinding], int]:
        """Scanne un répertoire récursivement."""
        findings = []
        files_scanned = 0
        
        for root, dirs, files in os.walk(dirpath):
            # Filtrer les répertoires exclus
            dirs[:] = [d for d in dirs if d not in DEFAULT_EXCLUDES]
            
            for filename in files:
                filepath = os.path.join(root, filename)
                rel_path = os.path.relpath(filepath, dirpath)
                
                if self.should_scan_file(rel_path, include_patterns, exclude_patterns):
                    file_findings = self.scan_file(filepath, severity_threshold, max_size)
                    for finding in file_findings:
                        finding.file = rel_path
                    findings.extend(file_findings)
                    files_scanned += 1
        
        return findings, files_scanned
    
    def deduplicate_findings(self, findings: List[SecretFinding]) -> List[SecretFinding]:
        """Déduplique les findings basés sur (type, file, line, match)."""
        seen = set()
        unique = []
        for f in findings:
            key = (f.type, f.file, f.line, f.match[:50])
            if key not in seen:
                seen.add(key)
                unique.append(f)
        return unique
