"""
Configuration et constantes pour l'outil Code Review.
"""

REVIEW_STANDARDS = {
    "naming": "Conventions de nommage (variables, fonctions, classes)",
    "complexity": "Complexité cyclomatique et imbrication excessive",
    "security": "Vulnérabilités de sécurité (injection, exposition, hardcoded secrets)",
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
        r"password\s*=\s*['\"]",
        r"api_key\s*=\s*['\"]",
        r"secret\s*=\s*['\"]",
    ],
    "javascript": [
        r"eval\s*\(",
        r"innerHTML\s*=",
        r"document\.write\s*\(",
        r"password\s*[=:]\s*['\"]",
        r"apiKey\s*[=:]\s*['\"]",
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
