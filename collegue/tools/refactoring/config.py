"""
Configuration et constantes pour l'outil Refactoring.
"""

# Types de refactoring disponibles
REFACTORING_TYPES = {
    "rename": "Renommer des variables, fonctions ou classes pour améliorer la clarté",
    "extract": "Extraire du code en fonctions ou méthodes réutilisables",
    "simplify": "Simplifier la logique complexe et les conditions imbriquées",
    "optimize": "Optimiser les performances et l'efficacité",
    "clean": "Nettoyer le code mort, imports inutilisés et code superflu",
    "modernize": "Moderniser le code vers les patterns contemporains"
}

# Instructions spécifiques par langage et type de refactoring
REFACTORING_LANGUAGE_INSTRUCTIONS = {
    "python": {
        "rename": "Utilise les conventions PEP 8 (snake_case, PascalCase)",
        "extract": "Crée des fonctions avec type hints et docstrings",
        "simplify": "Utilise comprehensions, walrus operator",
        "optimize": "Utilise set, deque, évite les boucles inutiles",
        "clean": "Supprime imports inutiles, utilise f-strings",
        "modernize": "Utilise dataclasses, type hints, pathlib"
    },
    "javascript": {
        "rename": "Utilise camelCase pour variables/fonctions",
        "extract": "Crée des fonctions avec JSDoc, arrow functions",
        "simplify": "Utilise destructuring, template literals",
        "optimize": "Utilise Map/Set, évite les mutations",
        "clean": "Supprime var, utilise const/let",
        "modernize": "Utilise ES6+, async/await, modules ES6"
    },
    "typescript": {
        "rename": "Utilise camelCase avec types explicites",
        "extract": "Crée des fonctions typées avec génériques",
        "simplify": "Utilise union types, optional chaining",
        "optimize": "Types stricts, évite 'any'",
        "clean": "Supprime types redondants",
        "modernize": "Utilise strict mode, utility types"
    },
    "terraform": {
        "rename": "Utilise snake_case pour les ressources et variables",
        "extract": "Utilise des modules pour le code réutilisable",
        "simplify": "Utilise for_each et dynamic blocks",
        "optimize": "Réduit la duplication, utilise locals",
        "clean": "Supprime les variables inutilisées, formate avec `terraform fmt` style",
        "modernize": "Utilise les versions récentes des providers et syntaxes"
    },
    "hcl": {
        "rename": "Utilise snake_case",
        "extract": "Utilise des modules",
        "simplify": "Utilise des expressions conditionnelles claires",
        "optimize": "Utilise locals pour les valeurs répétées",
        "clean": "Supprime les commentaires obsolètes",
        "modernize": "Utilise la syntaxe HCL2"
    },
    "php": {
        "rename": "Utilise les conventions PSR-12 (camelCase pour méthodes/variables, PascalCase pour classes)",
        "extract": "Crée des méthodes typées avec PHPDoc ou Type Hints PHP 7/8",
        "simplify": "Utilise l'opérateur null coalescing (??), Elvis (?:), et Match expressions (PHP 8)",
        "optimize": "Utilise les fonctions natives PHP, évite les copies de tableaux inutiles",
        "clean": "Supprime les 'use' inutilisés, formate selon PSR-12",
        "modernize": "Utilise Constructor Property Promotion, Union Types, Attributes, Enums (PHP 8.1+)"
    }
}

# Patterns de commentaires par langage
COMMENT_PATTERNS = {
    "python": ["#"],
    "javascript": ["//", "/*"],
    "typescript": ["//", "/*"],
    "java": ["//", "/*"],
    "c#": ["//", "/*"],
    "php": ["//", "/*", "#"]
}

# Indicateurs de complexité
COMPLEXITY_INDICATORS = [
    "if ", "for ", "while ", "switch ", "case ", "try ", "catch ", "except "
]
