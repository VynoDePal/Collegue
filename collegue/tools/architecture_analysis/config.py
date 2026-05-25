"""
Configuration et constantes pour l'outil Architecture Analysis.
"""

ANALYSIS_TYPES = {
    "dependencies": "Analyse des dépendances entre modules",
    "coupling": "Évaluation du couplage entre composants",
    "cohesion": "Évaluation de la cohésion des modules",
    "patterns": "Détection des patterns architecturaux",
    "debt": "Identification de la dette technique",
    "metrics": "Métriques architecturales (LOC, complexité, profondeur)",
    "circular_deps": "Détection des dépendances circulaires",
}

ARCHITECTURAL_PATTERNS = [
    "MVC (Model-View-Controller)",
    "Layered Architecture",
    "Clean Architecture",
    "Hexagonal / Ports & Adapters",
    "Microservices",
    "Event-Driven",
    "Repository Pattern",
    "Service Layer",
    "Factory Pattern",
    "Singleton",
    "Observer",
    "Strategy",
]

DEBT_INDICATORS = {
    "god_class": {
        "description": "Classe avec trop de responsabilités",
        "threshold_lines": 300,
        "threshold_methods": 15,
        "severity": "error",
    },
    "circular_dependency": {
        "description": "Dépendance circulaire entre modules",
        "severity": "critical",
    },
    "deep_inheritance": {
        "description": "Hiérarchie d'héritage trop profonde",
        "threshold_depth": 4,
        "severity": "warning",
    },
    "high_fan_out": {
        "description": "Module avec trop de dépendances sortantes",
        "threshold": 10,
        "severity": "warning",
    },
    "missing_abstraction": {
        "description": "Code procédural sans abstraction",
        "severity": "info",
    },
}

IMPORT_PATTERNS = {
    "python": r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))",
    "javascript": r'(?:import\s+.*\s+from\s+[\'"](.+?)[\'"]|require\s*\(\s*[\'"](.+?)[\'"]\s*\))',
    "typescript": r'(?:import\s+.*\s+from\s+[\'"](.+?)[\'"]|require\s*\(\s*[\'"](.+?)[\'"]\s*\))',
    "php": r"(?:use\s+([\w\\\\]+)|require(?:_once)?\s+['\"](.+?)['\"])",
}
