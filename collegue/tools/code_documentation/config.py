"""
Configuration et constantes pour l'outil Code Documentation.
"""

# Instructions de style de documentation
STYLE_INSTRUCTIONS = {
    "standard": "Génère une documentation claire et concise avec descriptions, paramètres et valeurs de retour",
    "detailed": "Génère une documentation très détaillée avec exemples, cas d'usage et notes techniques",
    "minimal": "Génère une documentation minimale avec seulement les informations essentielles",
    "api": "Génère une documentation de style API avec format standardisé pour chaque fonction/classe",
    "tutorial": "Génère une documentation de style tutoriel avec explications pédagogiques"
}

# Instructions de format de sortie
FORMAT_INSTRUCTIONS = {
    "markdown": "Utilise le format Markdown avec en-têtes appropriés",
    "rst": "Utilise le format reStructuredText",
    "html": "Génère du HTML bien formaté",
    "docstring": "Génère des docstrings dans le style du langage",
    "json": "Retourne la documentation structurée en JSON"
}

# Instructions spécifiques par langage
LANGUAGE_INSTRUCTIONS = {
    "python": "Utilise les conventions PEP 257 pour les docstrings, inclus les types avec les paramètres",
    "javascript": "Utilise JSDoc format avec @param, @returns, @example",
    "typescript": "Inclus les types TypeScript dans la documentation, utilise @param avec types",
    "java": "Utilise Javadoc format avec @param, @return, @throws",
    "c#": "Utilise XML documentation format avec <summary>, <param>, <returns>",
    "go": "Utilise les conventions Go avec commentaires au-dessus des déclarations",
    "rust": "Utilise les doc comments avec /// et inclus les exemples avec ```",
    "php": "Utilise le format PHPDoc (PSR-5/PSR-19) avec @param, @return, @throws et types explicites"
}

# Descriptions des styles
STYLE_DESCRIPTIONS = {
    "standard": "Documentation complète avec descriptions, paramètres, retours et exemples basiques",
    "detailed": "Documentation très détaillée avec explications approfondies, cas d'usage et exemples avancés",
    "minimal": "Documentation concise avec informations essentielles seulement",
    "api": "Documentation technique orientée API avec signatures, types et codes d'erreur",
    "tutorial": "Documentation pédagogique avec explications pas-à-pas et exemples pratiques"
}

# Descriptions des formats
FORMAT_DESCRIPTIONS = {
    "markdown": "Format Markdown (.md) idéal pour GitHub, wikis et documentation web",
    "rst": "reStructuredText (.rst) utilisé par Sphinx et la documentation Python",
    "html": "HTML complet avec CSS pour documentation web interactive",
    "docstring": "Docstrings insérées directement dans le code source",
    "json": "Format JSON structuré pour intégration avec d'autres outils"
}
