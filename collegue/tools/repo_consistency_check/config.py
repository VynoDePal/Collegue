"""
Configuration pour l'outil Repo Consistency Check.
"""

# Liste des checks disponibles
ALL_CHECKS = [
    'unused_imports',
    'unused_vars', 
    'dead_code',
    'duplication',
    'unresolved_symbol'
]

# Mapping des types d'issues vers leur sévérité
SEVERITY_MAP = {
    'unused_import': 'low',
    'unused_var': 'medium',
    'dead_code': 'medium',
    'duplication': 'low',
    'unresolved_symbol': 'high',
}

# Builtins par langage pour l'analyse de symboles
BUILTINS = {
    'python': {
        'print', 'len', 'range', 'str', 'int', 'float', 'bool', 'list', 'dict', 'set',
        'tuple', 'type', 'isinstance', 'hasattr', 'getattr', 'setattr', 'open', 'input',
        'sum', 'min', 'max', 'abs', 'round', 'sorted', 'reversed', 'enumerate', 'zip',
        'map', 'filter', 'any', 'all', 'None', 'True', 'False', 'Exception', 'ValueError',
        'TypeError', 'KeyError', 'IndexError', 'AttributeError', 'super', 'property',
        'staticmethod', 'classmethod', 'self', 'cls', '__name__', '__file__',
    },
    'javascript': {
        'console', 'window', 'document', 'fetch', 'Promise', 'Array', 'Object', 'String',
        'Number', 'Boolean', 'JSON', 'Math', 'Date', 'Error', 'undefined', 'null',
        'setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'require', 'module',
        'exports', 'process', 'Buffer', '__dirname', '__filename', 'global', 'this',
    },
    'php': {
        'echo', 'print', 'isset', 'empty', 'count', 'strlen', 'array', 'null', 'true', 'false',
        '$_GET', '$_POST', '$_SESSION', '$_SERVER', '$_FILES', '$_COOKIE', '$_ENV', '$this',
        'Exception', 'stdClass', 'DateTime', 'json_encode', 'json_decode', 'var_dump', 'die', 'exit'
    }
}

# Poids pour le calcul du score de refactoring
REFACTORING_WEIGHTS = {
    'high': 0.4,
    'medium': 0.25,
    'low': 0.1,
    'info': 0.05
}

# Seuils pour la priorité de refactoring
REFACTORING_THRESHOLDS = {
    'critical': 0.8,
    'recommended': 0.6,
    'suggested': 0.3
}

# Taille minimale des blocs pour la détection de duplication
DUPLICATION_MIN_LINES = 5
