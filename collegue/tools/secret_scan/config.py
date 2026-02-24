"""
Configuration et patterns pour l'outil Secret Scan.

Contient tous les patterns regex pour la détection des secrets,
les extensions de fichiers à scanner, et les recommandations.
"""

# Patterns de secrets (nom, regex, sévérité, description)
SECRET_PATTERNS = [
    # Cloud Providers
    ("aws_access_key", r"(?:A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}", "critical", "Clé d'accès AWS"),
    ("aws_secret_key", r"(?i)aws[_\-]?secret[_\-]?(?:access[_\-]?)?key['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})", "critical", "Clé secrète AWS"),
    
    # Google Cloud
    ("gcp_api_key", r"AIza[0-9A-Za-z\-_]{35}", "high", "Clé API Google Cloud"),
    ("gcp_service_account", r'"type":\s*"service_account"', "high", "Compte de service GCP"),
    
    # Azure
    ("azure_storage_key", r"(?i)(?:DefaultEndpointsProtocol|AccountKey)\s*=\s*[A-Za-z0-9+/=]{86,}", "critical", "Clé de stockage Azure"),
    
    # AI/ML APIs
    ("openai_api_key", r"sk-[A-Za-z0-9]{48}", "critical", "Clé API OpenAI"),
    ("anthropic_api_key", r"sk-ant-[A-Za-z0-9\-]{93}", "critical", "Clé API Anthropic"),
    ("gemini_api_key", r"AIzaSy[A-Za-z0-9_-]{33}", "critical", "Clé API Google Gemini"),
    
    # Git Providers
    ("github_token", r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}", "critical", "Token GitHub"),
    ("github_oauth", r"gho_[A-Za-z0-9]{36}", "critical", "Token OAuth GitHub"),
    ("gitlab_token", r"glpat-[A-Za-z0-9\-]{20,}", "critical", "Token GitLab"),
    
    # Databases
    ("postgres_uri", r"postgres(?:ql)?://[^:]+:[^@]+@[^/]+/\w+", "high", "URI PostgreSQL avec credentials"),
    ("mysql_uri", r"mysql://[^:]+:[^@]+@[^/]+/\w+", "high", "URI MySQL avec credentials"),
    ("mongodb_uri", r"mongodb(?:\+srv)?://[^:]+:[^@]+@", "high", "URI MongoDB avec credentials"),
    ("redis_uri", r"redis://:[^@]+@", "high", "URI Redis avec password"),
    
    # Auth Tokens
    ("jwt_token", r"eyJ[A-Za-z0-9_-]*\.eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*", "medium", "Token JWT"),
    ("bearer_token", r"(?i)bearer\s+[A-Za-z0-9\-_\.]{20,}", "medium", "Token Bearer"),
    
    # Private Keys
    ("private_key_rsa", r"-----BEGIN (?:RSA )?PRIVATE KEY-----", "critical", "Clé privée RSA"),
    ("private_key_openssh", r"-----BEGIN OPENSSH PRIVATE KEY-----", "critical", "Clé privée OpenSSH"),
    ("private_key_ec", r"-----BEGIN EC PRIVATE KEY-----", "critical", "Clé privée EC"),
    ("private_key_pgp", r"-----BEGIN PGP PRIVATE KEY BLOCK-----", "critical", "Clé privée PGP"),
    
    # Payment APIs
    ("stripe_secret_key", r"sk_live_[0-9a-zA-Z]{24,}", "critical", "Clé secrète Stripe"),
    ("stripe_publishable", r"pk_live_[0-9a-zA-Z]{24,}", "medium", "Clé publique Stripe (live)"),
    
    # Communication APIs
    ("slack_token", r"xox[baprs]-[0-9]{10,13}-[0-9]{10,13}[a-zA-Z0-9-]*", "high", "Token Slack"),
    ("slack_webhook", r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+", "high", "Webhook Slack"),
    ("sendgrid_api_key", r"SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}", "high", "Clé API SendGrid"),
    ("twilio_api_key", r"SK[0-9a-fA-F]{32}", "high", "Clé API Twilio"),
    
    # Package Managers
    ("npm_token", r"(?i)npm[_\-]?token['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9\-]{36})", "high", "Token NPM"),
    
    # Passwords
    ("password_assignment", r"(?i)(?:password|passwd|pwd|secret|token)['\"]?\s*[:=]\s*['\"]([^'\"]{8,})['\"]", "medium", "Mot de passe hardcodé"),
    ("password_in_url", r"://[^:]+:([^@]{8,})@", "high", "Mot de passe dans URL"),
    
    # Environment Variables
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
    '.md', '.txt', '.rst',
}

# Patterns de fichiers/répertoires à exclure par défaut
DEFAULT_EXCLUDES = {
    'node_modules', '.git', '__pycache__', '.venv', 'venv', 'env',
    '.idea', '.vscode', 'dist', 'build', 'target', 'bin', 'obj',
    '*.min.js', '*.min.css', '*.map', '*.lock',
    '.pytest_cache', '.mypy_cache', '.tox', 'coverage',
}

# Niveaux de sévérité pour le filtrage
SEVERITY_LEVELS = {'low': 1, 'medium': 2, 'high': 3, 'critical': 4}

# Recommandations par type de secret
SECRET_RECOMMENDATIONS = {
    "aws": "Utilisez AWS Secrets Manager ou des variables d'environnement. Révoquez cette clé immédiatement.",
    "gcp": "Utilisez Google Secret Manager. Révoquez cette clé dans la console GCP.",
    "azure": "Utilisez Azure Key Vault. Régénérez cette clé dans le portail Azure.",
    "openai": "Stockez la clé dans une variable d'environnement. Régénérez la clé sur platform.openai.com.",
    "anthropic": "Utilisez une variable d'environnement. Régénérez la clé sur console.anthropic.com.",
    "gemini": "Stockez la clé dans .env. Régénérez sur aistudio.google.com/apikey.",
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
