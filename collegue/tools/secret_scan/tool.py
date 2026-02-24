"""
Secret Scan - Outil de détection de secrets dans le code.

Cet outil scanne le code source pour détecter les secrets exposés:
- Clés API (AWS, Google, Azure, OpenAI, etc.)
- Tokens d'authentification (JWT, OAuth, Bearer)
- Mots de passe hardcodés
- Clés privées (SSH, PGP, certificats)
- Variables d'environnement sensibles

Refactorisé: Le fichier original faisait 561 lignes, maintenant ~180 lignes.
La logique métier a été déplacée dans engine.py, les modèles dans models.py.
"""
import os
from typing import List, Dict, Any, Optional
from ..base import BaseTool, ToolValidationError
from ...core.shared import aggregate_severities
from .models import SecretScanRequest, SecretScanResponse, SecretFinding
from .engine import SecretDetectionEngine


class SecretScanTool(BaseTool):
    """
    Outil de détection de secrets dans le code.
    
    Détecte 30+ types de secrets: AWS, GCP, Azure, OpenAI, GitHub, tokens JWT,
    clés privées, connection strings, mots de passe hardcodés, etc.
    """
    
    tool_name = "secret_scan"
    tool_description = "Scanne le code pour détecter les secrets exposés (clés API, tokens, mots de passe)"
    tags = {"security", "analysis"}
    request_model = SecretScanRequest
    response_model = SecretScanResponse
    supported_languages = [
        "python", "typescript", "javascript", "java", "go", "rust", "ruby", "php",
        "json", "yaml", "yml", "toml", "xml", "html", "css", "scss",
        "markdown", "md", "txt", "text", "env", "config", "ini", "properties",
        "dockerfile", "docker", "shell", "bash", "sh", "zsh", "powershell",
        "sql", "graphql", "terraform", "tf", "hcl", "any"
    ]
    long_running = False
    
    def __init__(self, config=None, app_state=None):
        super().__init__(config, app_state)
        self._engine = SecretDetectionEngine(logger=self.logger)
    
    def validate_language(self, language: str) -> bool:
        """Accepte n'importe quel langage pour le scan de secrets."""
        return True
    
    def get_usage_description(self) -> str:
        return (
            "Outil de détection de secrets qui scanne le code source pour trouver "
            "les clés API, tokens, mots de passe et autres informations sensibles. "
            "Supporte 30+ types de secrets (AWS, GCP, Azure, OpenAI, GitHub, etc.)."
        )
    
    def get_examples(self) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Scanner un fichier Python",
                "request": {
                    "target": "config.py",
                    "scan_type": "file"
                }
            },
            {
                "title": "Scanner un répertoire entier",
                "request": {
                    "target": "src/",
                    "scan_type": "directory",
                    "exclude_patterns": ["*.min.js", "node_modules/*"]
                }
            },
            {
                "title": "Scanner du code directement",
                "request": {
                    "target": "api_key = 'sk-1234567890abcdef'",
                    "scan_type": "content"
                }
            },
            {
                "title": "Scanner avec seuil de sévérité",
                "request": {
                    "target": ".",
                    "scan_type": "directory",
                    "severity_threshold": "high"
                }
            }
        ]
    
    def get_capabilities(self) -> List[str]:
        return [
            "Détection de 30+ types de secrets (AWS, GCP, Azure, OpenAI, etc.)",
            "Scan de fichiers, répertoires ou code en mémoire",
            "Classification par sévérité (low, medium, high, critical)",
            "Masquage automatique des secrets dans les rapports",
            "Exclusion configurable de patterns de fichiers",
            "Recommandations de correction pour chaque finding"
        ]
    
    def _build_summary(self, findings: List[SecretFinding], files_scanned: int,
                      files_with_secrets: List[str], severity_counts: dict) -> str:
        """Construit le résumé textuel du scan."""
        if not findings:
            return f"✅ Aucun secret détecté dans {files_scanned} fichier(s) scanné(s)."
        
        files_affected = len(files_with_secrets)
        summary_parts = [
            f"⚠️ {len(findings)} secret(s) détecté(s) dans {files_affected} fichier(s) sur {files_scanned} scanné(s).",
            f"Sévérité: Critique({severity_counts['critical']}), Haute({severity_counts['high']}), "
            f"Moyenne({severity_counts['medium']}), Basse({severity_counts['low']})."
        ]
        
        if files_with_secrets:
            summary_parts.append(f"Fichiers affectés: {', '.join(files_with_secrets[:10])}")
            if len(files_with_secrets) > 10:
                summary_parts.append(f"... et {len(files_with_secrets) - 10} autres fichiers.")
        
        return " ".join(summary_parts)
    
    def _execute_core_logic(self, request: SecretScanRequest, **kwargs) -> SecretScanResponse:
        """Exécute le scan de secrets."""
        findings = []
        files_scanned = 0
        files_with_secrets = []
        
        # Préparer les patterns
        include_patterns = request.include_patterns or []
        from .config import DEFAULT_EXCLUDES
        exclude_patterns = list(DEFAULT_EXCLUDES) + (request.exclude_patterns or [])
        
        # Scan batch de fichiers (mode MCP)
        if request.files:
            self.logger.info(f"Scan batch de {len(request.files)} fichier(s)")
            for file_item in request.files:
                if not self._engine.should_scan_file(file_item.path, include_patterns, exclude_patterns):
                    self.logger.debug(f"Fichier ignoré (pattern): {file_item.path}")
                    continue
                
                if len(file_item.content) > request.max_file_size:
                    self.logger.debug(f"Fichier ignoré (trop grand): {file_item.path}")
                    continue
                
                file_findings = self._engine.scan_content(
                    file_item.content, file_item.path, request.severity_threshold
                )
                
                if file_findings:
                    files_with_secrets.append(file_item.path)
                    for finding in file_findings:
                        finding.file = file_item.path
                    findings.extend(file_findings)
                
                files_scanned += 1
        
        # Scan de contenu direct
        elif request.content:
            findings = self._engine.scan_content(request.content, "[content]", request.severity_threshold)
            files_scanned = 1
            if findings:
                files_with_secrets.append("[content]")
        
        # Scan de fichier ou répertoire
        elif request.target:
            scan_type = request.scan_type
            if scan_type == 'auto':
                if os.path.isfile(request.target):
                    scan_type = 'file'
                elif os.path.isdir(request.target):
                    scan_type = 'directory'
                else:
                    scan_type = 'content'
            
            if scan_type == 'file':
                if not os.path.isfile(request.target):
                    raise ToolValidationError(
                        f"Fichier '{request.target}' inexistant. Utilisez 'content' pour passer du code directement."
                    )
                findings = self._engine.scan_file(request.target, request.severity_threshold, request.max_file_size)
                files_scanned = 1
            
            elif scan_type == 'directory':
                if not os.path.isdir(request.target):
                    raise ToolValidationError(
                        f"Répertoire '{request.target}' inexistant. Utilisez 'content' pour passer du code directement."
                    )
                findings, files_scanned = self._engine.scan_directory(
                    request.target, include_patterns, exclude_patterns,
                    request.severity_threshold, request.max_file_size
                )
            
            elif scan_type == 'content':
                findings = self._engine.scan_content(request.target, None, request.severity_threshold)
                files_scanned = 1
            
            # Collecter les fichiers avec secrets
            for f in findings:
                if f.file and f.file not in files_with_secrets:
                    files_with_secrets.append(f.file)
        
        # Calculer les statistiques
        severity_counts = aggregate_severities(findings)
        
        # Construire le résumé
        summary = self._build_summary(findings, files_scanned, files_with_secrets, severity_counts)
        
        return SecretScanResponse(
            clean=len(findings) == 0,
            total_findings=len(findings),
            critical=severity_counts['critical'],
            high=severity_counts['high'],
            medium=severity_counts['medium'],
            low=severity_counts['low'],
            files_scanned=files_scanned,
            files_with_secrets=files_with_secrets,
            findings=findings[:100],
            scan_summary=summary
        )
