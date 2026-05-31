"""
Configuration et constantes pour l'outil Code Review.
"""

REVIEW_STANDARDS = {
    "naming": "Conventions de nommage (variables, fonctions, classes)",
    "complexity": "Complexité cyclomatique et imbrication excessive",
    "security": (
        "Vulnérabilités de sécurité — signale IMPÉRATIVEMENT le cas échéant : "
        "injections SQL/commande (f-strings ou concaténation dans des requêtes), "
        "secrets/clés/mots de passe codés en dur (ex: SECRET_KEY = \"...\"), "
        "hachage de mot de passe faible ou non salé (MD5, SHA1, SHA-256 nu), "
        "comparaison de mots de passe/jetons non à temps constant, "
        "jetons d'authentification falsifiables (base64/JSON non signés), "
        "validation des entrées manquante, contrôle d'autorisation absent"
    ),
    "performance": "Patterns inefficaces (boucles O(n²), allocations inutiles)",
    "dry": "Duplication de code (Don't Repeat Yourself)",
    "solid": "Principes SOLID (Single Responsibility, Open/Closed, etc.)",
    "error_handling": "Gestion des erreurs (try/catch, validation, edge cases)",
    "style": "Style de code et formatage",
}

SEVERITY_LEVELS = ["info", "warning", "error", "critical"]

SEVERITY_WEIGHTS = {
    "info": 0.05,
    "warning": 0.15,
    "error": 0.35,
    "critical": 0.50,
}

NAMING_CONVENTIONS = {
    "python": {
        "variables": "snake_case",
        "functions": "snake_case",
        "classes": "PascalCase",
        "constants": "UPPER_SNAKE_CASE",
    },
    "javascript": {
        "variables": "camelCase",
        "functions": "camelCase",
        "classes": "PascalCase",
        "constants": "UPPER_SNAKE_CASE",
    },
    "typescript": {
        "variables": "camelCase",
        "functions": "camelCase",
        "classes": "PascalCase",
        "constants": "UPPER_SNAKE_CASE",
        "interfaces": "PascalCase (I prefix optional)",
    },
    "php": {
        "variables": "camelCase",
        "functions": "camelCase",
        "classes": "PascalCase",
        "constants": "UPPER_SNAKE_CASE",
    },
}

SECURITY_PATTERNS = {
    "python": [
        r"eval\s*\(",
        r"exec\s*\(",
        r"os\.system\s*\(",
        r"subprocess\.call\s*\(.*shell\s*=\s*True",
        r"(?i)password\s*=\s*['\"]",
        r"(?i)api_key\s*=\s*['\"]",
        r"(?i)secret\s*=\s*['\"]",
    ],
    "javascript": [
        r"eval\s*\(",
        r"innerHTML\s*=",
        r"document\.write\s*\(",
        r"(?i)password\s*[=:]\s*['\"]",
        r"(?i)apiKey\s*[=:]\s*['\"]",
    ],
}

COMPLEXITY_KEYWORDS = {
    "python": ["if ", "elif ", "else:", "for ", "while ", "try:", "except ", "with "],
    "javascript": [
        "if ",
        "else if",
        "else ",
        "for ",
        "while ",
        "try ",
        "catch ",
        "switch ",
        "case ",
    ],
    "php": [
        "if ",
        "elseif ",
        "else ",
        "for ",
        "foreach ",
        "while ",
        "try ",
        "catch ",
        "switch ",
        "case ",
    ],
}
