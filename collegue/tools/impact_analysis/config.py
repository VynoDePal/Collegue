"""
Configuration et constantes pour l'outil Impact Analysis.
"""

# Patterns pour identifier les entités dans la description du changement
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
    r"[A-Z][a-z]+(?:[A-Z][a-z]+)+",
    r"[a-z]+(?:_[a-z]+)+",
]

# Catégories de risques
RISK_CATEGORIES = {
    "breaking_change": "Changement cassant l'API ou la compatibilité",
    "security": "Risque de sécurité potentiel",
    "data_migration": "Nécessite une migration de données",
    "performance": "Impact sur les performances",
    "compat": "Problème de compatibilité"
}

# Patterns de risques par catégorie
RISK_PATTERNS = {
    "breaking_change": [
        r"renommer|rename|supprimer|delete|remove",
        r"changer.*signature|modify.*signature",
        r"déprécier|deprecate",
    ],
    "security": [
        r"auth|login|password|token|secret|key",
        r"permission|role|access|security",
        r"sql|injection|xss",
    ],
    "data_migration": [
        r"database|schema|migration|column|field",
        r"modifier.*table|alter.*table",
    ],
    "performance": [
        r"loop|boucle|recursive|récursif",
        r"n\+1|query.*loop",
        r"sync|synchronous|bloquant|blocking",
    ],
}

# Seuils de confiance par mode
CONFIDENCE_THRESHOLDS = {
    "conservative": 0.8,
    "balanced": 0.6,
    "aggressive": 0.4
}

# Extensions de fichiers de test par langage
TEST_FILE_EXTENSIONS = {
    "python": ["_test.py", "test_", "_spec.py"],
    "javascript": [".test.js", ".spec.js", "_test.js"],
    "typescript": [".test.ts", ".spec.ts", "_test.ts"],
    "java": ["Test.java", "IT.java", "Tests.java"],
    "c#": ["Tests.cs", "Test.cs"],
    "php": ["Test.php", "_test.php"],
    "go": ["_test.go"],
    "rust": ["_test.rs"],
}

# Commandes de test par langage/framework
TEST_COMMANDS = {
    "python": {
        "pytest": "pytest {path} -v",
        "unittest": "python -m unittest {path}",
    },
    "javascript": {
        "jest": "jest {path}",
        "mocha": "mocha {path}",
    },
    "typescript": {
        "jest": "jest {path}",
    },
    "php": {
        "phpunit": "./vendor/bin/phpunit {path}",
        "pest": "./vendor/bin/pest {path}",
    },
}
