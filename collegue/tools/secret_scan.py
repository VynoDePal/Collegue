"""
Secret Scan - Outil de détection de secrets dans le code

Cet outil scanne le code source pour détecter les secrets exposés:
- Clés API (AWS, Google, Azure, OpenAI, etc.)
- Tokens d'authentification (JWT, OAuth, Bearer)
- Mots de passe hardcodés
- Clés privées (SSH, PGP, certificats)
- Variables d'environnement sensibles

Problème résolu: L'IA génère souvent du code avec des placeholders qui ressemblent
à de vrais secrets, ou copie des patterns de code public contenant des secrets.
Valeur: Empêche les fuites de secrets avant le commit.
Bénéfice: Évite des incidents de sécurité majeurs (exposition de clés).
"""
import os
import re
from typing import Optional, Dict, Any, List, Type
from pydantic import BaseModel, Field, field_validator
from .base import BaseTool, ToolError, ToolValidationError, ToolExecutionError


class SecretScanRequest(BaseModel):
    """Modèle de requête pour le scan de secrets."""
    target: Optional[str] = Field(
        None, 
        description="Cible du scan: fichier ou dossier (utiliser 'content' pour passer du code directement)"
    )
    content: Optional[str] = Field(
        None,
        description="Contenu du code à scanner (alternative à target pour environnements isolés comme MCP)"
    )
    scan_type: str = Field(
        "auto",
        description="Type de scan: 'file', 'directory', 'content', ou 'auto' (auto-détecté)"
    )
    language: Optional[str] = Field(
        None, 
        description="Langage du code (pour optimiser les patterns)"
    )
    include_patterns: Optional[List[str]] = Field(
        None,
        description="Patterns de fichiers à inclure (ex: ['*.py', '*.ts'])"
    )
    exclude_patterns: Optional[List[str]] = Field(
        None,
        description="Patterns de fichiers à exclure (ex: ['*.min.js', 'node_modules/*'])"
    )
    severity_threshold: Optional[str] = Field(
        "low",
        description="Seuil de sévérité minimum: 'low', 'medium', 'high', 'critical'"
    )
    max_file_size: Optional[int] = Field(
        1024 * 1024,  # 1MB
        description="Taille max des fichiers à scanner (bytes)",
        ge=1024,
        le=10 * 1024 * 1024
    )
    
    @field_validator('scan_type')
    def validate_scan_type(cls, v):
        valid = ['auto', 'file', 'directory', 'content']
        if v not in valid:
            raise ValueError(f"Type de scan '{v}' invalide. Utilisez: {', '.join(valid)}")
        return v
    
    @field_validator('severity_threshold')
    def validate_severity(cls, v):
        valid = ['low', 'medium', 'high', 'critical']
        if v not in valid:
            raise ValueError(f"Sévérité '{v}' invalide. Utilisez: {', '.join(valid)}")
        return v
    
    def model_post_init(self, __context):
        """Valide que target ou content est fourni."""
        if not self.target and not self.content:
            raise ValueError("Vous devez fournir 'target' (chemin) ou 'content' (code à scanner)")


class SecretFinding(BaseModel):
    """Un secret détecté."""
    type: str = Field(..., description="Type de secret (api_key, password, token, etc.)")
    severity: str = Field(..., description="Sévérité: low, medium, high, critical")
    file: Optional[str] = Field(None, description="Fichier contenant le secret")
    line: Optional[int] = Field(None, description="Numéro de ligne")
    column: Optional[int] = Field(None, description="Numéro de colonne")
    match: str = Field(..., description="Extrait du code (masqué partiellement)")
    rule: str = Field(..., description="Règle de détection déclenchée")
    recommendation: str = Field(..., description="Recommandation pour corriger")


class SecretScanResponse(BaseModel):
    """Modèle de réponse pour le scan de secrets."""
    clean: bool = Field(..., description="True si aucun secret trouvé")
    total_findings: int = Field(..., description="Nombre total de secrets détectés")
    critical: int = Field(0, description="Nombre de secrets critiques")
    high: int = Field(0, description="Nombre de secrets haute sévérité")
    medium: int = Field(0, description="Nombre de secrets moyenne sévérité")
    low: int = Field(0, description="Nombre de secrets basse sévérité")
    files_scanned: int = Field(..., description="Nombre de fichiers scannés")
    findings: List[SecretFinding] = Field(
        default_factory=list,
        description="Liste des secrets trouvés (max 100)"
    )
    scan_summary: str = Field(..., description="Résumé du scan")


class SecretScanTool(BaseTool):
    """
    Outil de détection de secrets dans le code.
    
    Détecte:
    - Clés API (AWS, GCP, Azure, OpenAI, Stripe, etc.)
    - Tokens (JWT, OAuth, Bearer, GitHub, GitLab)
    - Mots de passe hardcodés
    - Clés privées (RSA, SSH, PGP)
    - Connection strings (bases de données)
    - Variables d'environnement sensibles exposées
    
    Basé sur des patterns regex éprouvés (similaires à gitleaks, trufflehog).
    """

    # Patterns de détection de secrets
    # Format: (nom, pattern regex, sévérité, description)
    SECRET_PATTERNS = [
        # AWS
        ("aws_access_key", r"(?:A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}", "critical", "Clé d'accès AWS"),
        ("aws_secret_key", r"(?i)aws[_\-]?secret[_\-]?(?:access[_\-]?)?key['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})", "critical", "Clé secrète AWS"),
        
        # Google Cloud
        ("gcp_api_key", r"AIza[0-9A-Za-z\-_]{35}", "high", "Clé API Google Cloud"),
        ("gcp_service_account", r"\"type\":\s*\"service_account\"", "high", "Compte de service GCP"),
        
        # Azure
        ("azure_storage_key", r"(?i)(?:DefaultEndpointsProtocol|AccountKey)\s*=\s*[A-Za-z0-9+/=]{86,}", "critical", "Clé de stockage Azure"),
        
        # OpenAI / LLM
        ("openai_api_key", r"sk-[A-Za-z0-9]{48}", "critical", "Clé API OpenAI"),
        ("anthropic_api_key", r"sk-ant-[A-Za-z0-9\-]{93}", "critical", "Clé API Anthropic"),
        ("openrouter_api_key", r"sk-or-v1-[A-Za-z0-9]{64}", "critical", "Clé API OpenRouter"),
        
        # GitHub / GitLab
        ("github_token", r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}", "critical", "Token GitHub"),
        ("github_oauth", r"gho_[A-Za-z0-9]{36}", "critical", "Token OAuth GitHub"),
        ("gitlab_token", r"glpat-[A-Za-z0-9\-]{20,}", "critical", "Token GitLab"),
        
        # Bases de données
        ("postgres_uri", r"postgres(?:ql)?://[^:]+:[^@]+@[^/]+/\w+", "high", "URI PostgreSQL avec credentials"),
        ("mysql_uri", r"mysql://[^:]+:[^@]+@[^/]+/\w+", "high", "URI MySQL avec credentials"),
        ("mongodb_uri", r"mongodb(?:\+srv)?://[^:]+:[^@]+@", "high", "URI MongoDB avec credentials"),
        ("redis_uri", r"redis://:[^@]+@", "high", "URI Redis avec password"),
        
        # JWT et tokens
        ("jwt_token", r"eyJ[A-Za-z0-9_-]*\.eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*", "medium", "Token JWT"),
        ("bearer_token", r"(?i)bearer\s+[A-Za-z0-9\-_\.]{20,}", "medium", "Token Bearer"),
        
        # Clés privées
        ("private_key_rsa", r"-----BEGIN (?:RSA )?PRIVATE KEY-----", "critical", "Clé privée RSA"),
        ("private_key_openssh", r"-----BEGIN OPENSSH PRIVATE KEY-----", "critical", "Clé privée OpenSSH"),
        ("private_key_ec", r"-----BEGIN EC PRIVATE KEY-----", "critical", "Clé privée EC"),
        ("private_key_pgp", r"-----BEGIN PGP PRIVATE KEY BLOCK-----", "critical", "Clé privée PGP"),
        
        # Stripe
        ("stripe_secret_key", r"sk_live_[0-9a-zA-Z]{24,}", "critical", "Clé secrète Stripe"),
        ("stripe_publishable", r"pk_live_[0-9a-zA-Z]{24,}", "medium", "Clé publique Stripe (live)"),
        
        # Slack
        ("slack_token", r"xox[baprs]-[0-9]{10,13}-[0-9]{10,13}[a-zA-Z0-9-]*", "high", "Token Slack"),
        ("slack_webhook", r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+", "high", "Webhook Slack"),
        
        # SendGrid / Twilio
        ("sendgrid_api_key", r"SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}", "high", "Clé API SendGrid"),
        ("twilio_api_key", r"SK[0-9a-fA-F]{32}", "high", "Clé API Twilio"),
        
        # NPM
        ("npm_token", r"(?i)npm[_\-]?token['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9\-]{36})", "high", "Token NPM"),
        
        # Mots de passe génériques
        ("password_assignment", r"(?i)(?:password|passwd|pwd|secret|token)['\"]?\s*[:=]\s*['\"]([^'\"]{8,})['\"]", "medium", "Mot de passe hardcodé"),
        ("password_in_url", r"://[^:]+:([^@]{8,})@", "high", "Mot de passe dans URL"),
        
        # Variables d'environnement exposées
        ("env_secret", r"(?i)(?:export\s+)?(?:API_KEY|SECRET_KEY|AUTH_TOKEN|DATABASE_PASSWORD|DB_PASSWORD)['\"]?\s*=\s*['\"]?([A-Za-z0-9\-_/+=]{16,})", "medium", "Secret dans variable d'environnement"),
    ]
    
    # Extensions de fichiers à scanner par défaut
    DEFAULT_EXTENSIONS = {
        '.py', '.js', '.ts', '.jsx', '.tsx', '.json', '.yaml', '.yml',
        '.env', '.env.local', '.env.development', '.env.production',
        '.config', '.cfg', '.ini', '.conf', '.properties',
        '.sh', '.bash', '.zsh', '.fish',
        '.xml', '.html', '.htm',
        '.java', '.kt', '.scala', '.go', '.rs', '.rb', '.php',
        '.cs', '.vb', '.fs',
        '.sql', '.prisma',
        '.toml', '.lock',
        '.md', '.txt', '.rst',  # Documentation peut contenir des exemples
    }
    
    # Fichiers/dossiers à exclure par défaut
    DEFAULT_EXCLUDES = {
        'node_modules', '.git', '__pycache__', '.venv', 'venv', 'env',
        '.idea', '.vscode', 'dist', 'build', 'target', 'bin', 'obj',
        '*.min.js', '*.min.css', '*.map', '*.lock',
        '.pytest_cache', '.mypy_cache', '.tox', 'coverage',
    }
    
    SEVERITY_LEVELS = {'low': 1, 'medium': 2, 'high': 3, 'critical': 4}

    def get_name(self) -> str:
        return "secret_scan"

    def get_description(self) -> str:
        return "Scanne le code pour détecter les secrets exposés (clés API, tokens, mots de passe)"

    def get_request_model(self) -> Type[BaseModel]:
        return SecretScanRequest

    def get_response_model(self) -> Type[BaseModel]:
        return SecretScanResponse

    def get_supported_languages(self) -> List[str]:
        return ["python", "typescript", "javascript", "java", "go", "rust", "ruby", "php"]

    def is_long_running(self) -> bool:
        return False  # Le scan est généralement rapide

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

    def get_required_config_keys(self) -> List[str]:
        return []

    def _mask_secret(self, secret: str, visible_chars: int = 4) -> str:
        """Masque un secret en ne montrant que les premiers et derniers caractères."""
        if len(secret) <= visible_chars * 2:
            return '*' * len(secret)
        return secret[:visible_chars] + '*' * (len(secret) - visible_chars * 2) + secret[-visible_chars:]

    def _get_recommendation(self, secret_type: str) -> str:
        """Retourne une recommandation pour corriger le secret exposé."""
        recommendations = {
            "aws": "Utilisez AWS Secrets Manager ou des variables d'environnement. Révoquez cette clé immédiatement.",
            "gcp": "Utilisez Google Secret Manager. Révoquez cette clé dans la console GCP.",
            "azure": "Utilisez Azure Key Vault. Régénérez cette clé dans le portail Azure.",
            "openai": "Stockez la clé dans une variable d'environnement. Régénérez la clé sur platform.openai.com.",
            "anthropic": "Utilisez une variable d'environnement. Régénérez la clé sur console.anthropic.com.",
            "openrouter": "Stockez la clé dans .env. Régénérez sur openrouter.ai/keys.",
            "github": "Révoquez ce token sur github.com/settings/tokens. Utilisez GITHUB_TOKEN dans CI/CD.",
            "gitlab": "Révoquez ce token. Utilisez des variables CI/CD GitLab.",
            "postgres": "Utilisez des variables d'environnement pour les credentials de BDD.",
            "mysql": "Utilisez des variables d'environnement pour les credentials de BDD.",
            "mongodb": "Utilisez des variables d'environnement. Configurez l'authentification MongoDB.",
            "redis": "Configurez Redis avec ACL et utilisez des variables d'environnement.",
            "jwt": "Les JWT doivent être générés dynamiquement, pas hardcodés.",
            "bearer": "Les tokens doivent être récupérés dynamiquement, pas hardcodés.",
            "private_key": "Ne jamais committer de clé privée. Utilisez un gestionnaire de secrets.",
            "stripe": "Utilisez des variables d'environnement. Régénérez la clé sur dashboard.stripe.com.",
            "slack": "Révoquez ce token sur api.slack.com. Utilisez OAuth pour les apps.",
            "sendgrid": "Régénérez la clé sur app.sendgrid.com. Utilisez des variables d'environnement.",
            "twilio": "Régénérez la clé sur twilio.com/console. Stockez dans des variables d'environnement.",
            "npm": "Révoquez ce token. Utilisez npm login ou NPM_TOKEN en CI/CD.",
            "password": "Ne jamais hardcoder de mots de passe. Utilisez des variables d'environnement ou un vault.",
            "env": "Ne jamais committer de fichiers .env contenant des secrets.",
        }
        
        for key, rec in recommendations.items():
            if key in secret_type.lower():
                return rec
        
        return "Supprimez ce secret du code et utilisez une méthode sécurisée (variables d'environnement, vault)."

    def _should_scan_file(self, filepath: str, include_patterns: List[str], exclude_patterns: List[str]) -> bool:
        """Détermine si un fichier doit être scanné."""
        import fnmatch
        
        filename = os.path.basename(filepath)
        
        # Vérifier les exclusions
        for pattern in exclude_patterns:
            if fnmatch.fnmatch(filepath, pattern) or fnmatch.fnmatch(filename, pattern):
                return False
            # Vérifier si un dossier parent est exclu
            for part in filepath.split(os.sep):
                if fnmatch.fnmatch(part, pattern):
                    return False
        
        # Vérifier les inclusions
        if include_patterns:
            for pattern in include_patterns:
                if fnmatch.fnmatch(filepath, pattern) or fnmatch.fnmatch(filename, pattern):
                    return True
            return False
        
        # Par défaut, scanner les extensions connues
        ext = os.path.splitext(filepath)[1].lower()
        # Fichiers sans extension comme .env, .gitignore
        if not ext and filename.startswith('.'):
            return True
        return ext in self.DEFAULT_EXTENSIONS

    def _scan_content(self, content: str, filepath: Optional[str] = None, severity_threshold: str = "low") -> List[SecretFinding]:
        """Scanne le contenu pour trouver des secrets."""
        findings = []
        threshold_level = self.SEVERITY_LEVELS.get(severity_threshold, 1)
        
        lines = content.split('\n')
        
        for name, pattern, severity, description in self.SECRET_PATTERNS:
            if self.SEVERITY_LEVELS.get(severity, 1) < threshold_level:
                continue
            
            try:
                regex = re.compile(pattern)
                for match in regex.finditer(content):
                    # Trouver la ligne et la colonne
                    start = match.start()
                    line_num = content[:start].count('\n') + 1
                    line_start = content.rfind('\n', 0, start) + 1
                    col_num = start - line_start + 1
                    
                    # Extraire le contexte (la ligne complète)
                    if line_num <= len(lines):
                        line_content = lines[line_num - 1]
                    else:
                        line_content = match.group()
                    
                    # Masquer le secret
                    matched_text = match.group()
                    masked_match = self._mask_secret(matched_text)
                    
                    # Remplacer le secret dans la ligne par la version masquée
                    masked_line = line_content.replace(matched_text, masked_match)
                    
                    findings.append(SecretFinding(
                        type=name,
                        severity=severity,
                        file=filepath,
                        line=line_num,
                        column=col_num,
                        match=masked_line.strip()[:200],  # Limiter la longueur
                        rule=description,
                        recommendation=self._get_recommendation(name)
                    ))
            except re.error as e:
                self.logger.warning(f"Erreur regex pour {name}: {e}")
        
        return findings

    def _scan_file(self, filepath: str, severity_threshold: str, max_size: int) -> List[SecretFinding]:
        """Scanne un fichier pour trouver des secrets."""
        try:
            # Vérifier la taille
            if os.path.getsize(filepath) > max_size:
                self.logger.debug(f"Fichier ignoré (trop grand): {filepath}")
                return []
            
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            
            return self._scan_content(content, filepath, severity_threshold)
        except Exception as e:
            self.logger.warning(f"Erreur lecture {filepath}: {e}")
            return []

    def _scan_directory(self, dirpath: str, include_patterns: List[str], exclude_patterns: List[str], 
                       severity_threshold: str, max_size: int) -> tuple:
        """Scanne un répertoire récursivement."""
        findings = []
        files_scanned = 0
        
        for root, dirs, files in os.walk(dirpath):
            # Filtrer les dossiers exclus
            dirs[:] = [d for d in dirs if d not in self.DEFAULT_EXCLUDES]
            
            for filename in files:
                filepath = os.path.join(root, filename)
                rel_path = os.path.relpath(filepath, dirpath)
                
                if self._should_scan_file(rel_path, include_patterns, exclude_patterns):
                    file_findings = self._scan_file(filepath, severity_threshold, max_size)
                    # Utiliser le chemin relatif dans les findings
                    for finding in file_findings:
                        finding.file = rel_path
                    findings.extend(file_findings)
                    files_scanned += 1
        
        return findings, files_scanned

    def _execute_core_logic(self, request: SecretScanRequest, **kwargs) -> SecretScanResponse:
        """Exécute le scan de secrets."""
        findings = []
        files_scanned = 0
        
        # Préparer les patterns
        include_patterns = request.include_patterns or []
        exclude_patterns = list(self.DEFAULT_EXCLUDES) + (request.exclude_patterns or [])
        
        # Mode 1: Contenu fourni directement (pour MCP et environnements isolés)
        if request.content:
            findings = self._scan_content(request.content, "[content]", request.severity_threshold)
            files_scanned = 1
        
        # Mode 2: Chemin de fichier/dossier
        elif request.target:
            # Déterminer le type de scan
            scan_type = request.scan_type
            if scan_type == 'auto':
                if os.path.isfile(request.target):
                    scan_type = 'file'
                elif os.path.isdir(request.target):
                    scan_type = 'directory'
                else:
                    # Fallback: traiter comme contenu si le chemin n'existe pas
                    scan_type = 'content'
            
            # Exécuter le scan
            if scan_type == 'file':
                if not os.path.isfile(request.target):
                    raise ToolValidationError(f"Fichier '{request.target}' inexistant. Utilisez 'content' pour passer du code directement.")
                findings = self._scan_file(request.target, request.severity_threshold, request.max_file_size)
                files_scanned = 1
                
            elif scan_type == 'directory':
                if not os.path.isdir(request.target):
                    raise ToolValidationError(f"Répertoire '{request.target}' inexistant. Utilisez 'content' pour passer du code directement.")
                findings, files_scanned = self._scan_directory(
                    request.target, include_patterns, exclude_patterns,
                    request.severity_threshold, request.max_file_size
                )
                
            elif scan_type == 'content':
                # Traiter target comme du contenu
                findings = self._scan_content(request.target, None, request.severity_threshold)
                files_scanned = 1
        
        # Compter par sévérité
        severity_counts = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0}
        for finding in findings:
            severity_counts[finding.severity] = severity_counts.get(finding.severity, 0) + 1
        
        # Limiter le nombre de findings retournés
        limited_findings = findings[:100]
        
        # Construire le résumé
        if not findings:
            summary = f"✅ Aucun secret détecté dans {files_scanned} fichier(s) scanné(s)."
        else:
            summary = (
                f"⚠️ {len(findings)} secret(s) détecté(s) dans {files_scanned} fichier(s) scanné(s). "
                f"Critique: {severity_counts['critical']}, Haute: {severity_counts['high']}, "
                f"Moyenne: {severity_counts['medium']}, Basse: {severity_counts['low']}."
            )
        
        return SecretScanResponse(
            clean=len(findings) == 0,
            total_findings=len(findings),
            critical=severity_counts['critical'],
            high=severity_counts['high'],
            medium=severity_counts['medium'],
            low=severity_counts['low'],
            files_scanned=files_scanned,
            findings=limited_findings,
            scan_summary=summary
        )
