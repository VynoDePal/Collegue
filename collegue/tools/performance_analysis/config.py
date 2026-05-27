"""
Configuration et constantes pour l'outil Performance Analysis.
"""

ANALYSIS_CATEGORIES = {
    "cpu": "Utilisation CPU (boucles inefficaces, calculs redondants)",
    "memory": "Utilisation mémoire (fuites, allocations inutiles, copies)",
    "io": "Opérations I/O (fichiers, réseau, base de données)",
    "network": "Appels réseau (requêtes série vs parallèle)",
    "algorithmic": "Complexité algorithmique (Big-O)",
    "parallelism": "Opportunités de parallélisation",
}

# Patterns inefficaces par langage
INEFFICIENT_PATTERNS = {
    "python": {
        "nested_loops": {
            "pattern": r"for\s+\w+\s+in\s+.*:\s*\n\s+for\s+\w+\s+in",
            "description": "Boucle imbriquée (potentiel O(n²))",
            "category": "algorithmic",
            "severity": "warning",
        },
        "list_in_loop": {
            "pattern": r"for\s+\w+\s+in\s+[^\n]+:\n(?:[ \t]+[^\n]*\n){0,3}[ \t]+\w+\.append\(",
            "description": "Construction de liste par append dans une boucle (préférer list comprehension)",
            "category": "cpu",
            "severity": "info",
        },
        "string_concat_loop": {
            "pattern": r"for\s+\w+\s+in\s+[^\n]+:\n(?:[ \t]+[^\n]*\n){0,3}[ \t]+\w+\s*\+=\s*['\"]",
            "description": "Concaténation de chaînes dans une boucle (préférer join())",
            "category": "memory",
            "severity": "warning",
        },
        "global_import_in_func": {
            "pattern": r"def\s+\w+\([^\n]*\)[^\n]*:\n(?:[ \t]+[^\n]*\n){0,3}[ \t]+import\s+",
            "description": "Import à l'intérieur d'une fonction (coût à chaque appel)",
            "category": "cpu",
            "severity": "info",
        },
        "blocking_io": {
            "pattern": r"(?:open|requests\.get|requests\.post|urllib)",
            "description": "I/O potentiellement bloquant",
            "category": "io",
            "severity": "info",
        },
        "catch_all": {
            "pattern": r"except\s*:",
            "description": "Except sans type spécifique (masque les erreurs de performance)",
            "category": "cpu",
            "severity": "info",
        },
    },
    "javascript": {
        "nested_loops": {
            "pattern": r"for\s*\(.*\)\s*\{[\s\S]*?for\s*\(",
            "description": "Boucle imbriquée (potentiel O(n²))",
            "category": "algorithmic",
            "severity": "warning",
        },
        "dom_in_loop": {
            "pattern": r"for\s*\(.*\)\s*\{[\s\S]*?document\.",
            "description": "Accès DOM dans une boucle (batch les modifications)",
            "category": "cpu",
            "severity": "error",
        },
        "sync_xhr": {
            "pattern": r"XMLHttpRequest.*false\s*\)",
            "description": "XHR synchrone (bloque le thread principal)",
            "category": "io",
            "severity": "critical",
        },
        "no_debounce": {
            "pattern": r"addEventListener\s*\(\s*['\"](?:scroll|resize|mousemove)['\"]",
            "description": "Event listener haute fréquence sans debounce/throttle",
            "category": "cpu",
            "severity": "warning",
        },
    },
}

COMPLEXITY_LEVELS = {
    "O(1)": "Temps constant — optimal",
    "O(log n)": "Logarithmique — très bon",
    "O(n)": "Linéaire — acceptable",
    "O(n log n)": "Quasi-linéaire — acceptable pour le tri",
    "O(n²)": "Quadratique — à surveiller",
    "O(n³)": "Cubique — problématique",
    "O(2^n)": "Exponentiel — critique",
}
