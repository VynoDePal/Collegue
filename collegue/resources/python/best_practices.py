"""
Best Practices Python - Ressources pour les bonnes pratiques en Python
"""
from pydantic import BaseModel
from typing import Dict, List, Optional, Any

class PythonBestPractice(BaseModel):
    """Modèle pour une bonne pratique Python."""
    title: str
    description: str
    category: str
    examples: Dict[str, Dict[str, str]] = {}  # "good" et "bad" exemples
    references: List[str] = []

# Dictionnaire des bonnes pratiques Python
PYTHON_BEST_PRACTICES = {
    "pep8": {
        "title": "Suivre PEP 8",
        "description": "PEP 8 est le guide de style pour le code Python. Il fournit des conventions pour écrire du code Python lisible.",
        "category": "style",
        "examples": {
            "good": {
                "title": "Bon style",
                "code": "def calculate_total(items):\n    \"\"\"Calcule le total des prix des items.\"\"\"\n    return sum(item.price for item in items)"
            },
            "bad": {
                "title": "Mauvais style",
                "code": "def calculateTotal( items ):\n    return sum( item.price for item in items )"
            }
        },
        "references": ["https://peps.python.org/pep-0008/"]
    },
    "docstrings": {
        "title": "Utiliser des docstrings",
        "description": "Documenter les modules, classes et fonctions avec des docstrings pour améliorer la maintenabilité.",
        "category": "documentation",
        "examples": {
            "good": {
                "title": "Bonne documentation",
                "code": "def calculate_area(radius):\n    \"\"\"Calcule l'aire d'un cercle.\n    \n    Args:\n        radius (float): Le rayon du cercle\n        \n    Returns:\n        float: L'aire du cercle\n    \"\"\"\n    import math\n    return math.pi * radius ** 2"
            },
            "bad": {
                "title": "Mauvaise documentation",
                "code": "def calculate_area(radius):\n    # calcule l'aire\n    import math\n    return math.pi * radius ** 2"
            }
        },
        "references": ["https://peps.python.org/pep-0257/"]
    },
    "type_hints": {
        "title": "Utiliser les annotations de type",
        "description": "Les annotations de type améliorent la lisibilité et permettent la vérification statique du code.",
        "category": "typing",
        "examples": {
            "good": {
                "title": "Avec annotations de type",
                "code": "def greeting(name: str) -> str:\n    return f'Hello, {name}!'"
            },
            "bad": {
                "title": "Sans annotations de type",
                "code": "def greeting(name):\n    return f'Hello, {name}!'"
            }
        },
        "references": ["https://peps.python.org/pep-0484/"]
    },
    "exceptions": {
        "title": "Gérer correctement les exceptions",
        "description": "Attraper des exceptions spécifiques et fournir un contexte utile.",
        "category": "error_handling",
        "examples": {
            "good": {
                "title": "Bonne gestion d'exceptions",
                "code": "try:\n    with open('file.txt', 'r') as file:\n        content = file.read()\nexcept FileNotFoundError:\n    print(\"Le fichier n'existe pas\")\nexcept PermissionError:\n    print(\"Pas d'autorisation pour lire le fichier\")"
            },
            "bad": {
                "title": "Mauvaise gestion d'exceptions",
                "code": "try:\n    with open('file.txt', 'r') as file:\n        content = file.read()\nexcept Exception as e:\n    print(e)"
            }
        },
        "references": ["https://docs.python.org/3/tutorial/errors.html"]
    },
    "context_managers": {
        "title": "Utiliser les gestionnaires de contexte",
        "description": "Les gestionnaires de contexte (with) garantissent une bonne gestion des ressources.",
        "category": "resource_management",
        "examples": {
            "good": {
                "title": "Avec gestionnaire de contexte",
                "code": "with open('file.txt', 'r') as file:\n    content = file.read()"
            },
            "bad": {
                "title": "Sans gestionnaire de contexte",
                "code": "file = open('file.txt', 'r')\ncontent = file.read()\nfile.close()"
            }
        },
        "references": ["https://docs.python.org/3/reference/datamodel.html#context-managers"]
    },
    "list_comprehensions": {
        "title": "Utiliser les compréhensions de liste",
        "description": "Les compréhensions de liste sont plus concises et souvent plus lisibles que les boucles traditionnelles.",
        "category": "idioms",
        "examples": {
            "good": {
                "title": "Avec compréhension de liste",
                "code": "squares = [x**2 for x in range(10)]"
            },
            "bad": {
                "title": "Sans compréhension de liste",
                "code": "squares = []\nfor x in range(10):\n    squares.append(x**2)"
            }
        },
        "references": ["https://docs.python.org/3/tutorial/datastructures.html#list-comprehensions"]
    }
}

def get_best_practice(practice_id: str) -> Optional[PythonBestPractice]:
    """Récupère les informations d'une bonne pratique Python."""
    if practice_id in PYTHON_BEST_PRACTICES:
        return PythonBestPractice(**PYTHON_BEST_PRACTICES[practice_id])
    return None

def get_all_best_practices() -> List[str]:
    """Récupère la liste de toutes les bonnes pratiques disponibles."""
    return list(PYTHON_BEST_PRACTICES.keys())

def get_best_practices_by_category(category: str) -> List[str]:
    """Récupère la liste des bonnes pratiques d'une catégorie spécifique."""
    return [id for id, data in PYTHON_BEST_PRACTICES.items() 
            if data.get("category") == category]

def register_best_practices(app, app_state):
    """Enregistre les ressources des bonnes pratiques Python."""
    
    @app.get("/resources/python/best-practices")
    async def list_python_best_practices():
        """Liste toutes les bonnes pratiques Python disponibles."""
        return {"practices": get_all_best_practices()}
    
    @app.get("/resources/python/best-practices/category/{category}")
    async def list_best_practices_by_category(category: str):
        """Liste les bonnes pratiques d'une catégorie spécifique."""
        return {"practices": get_best_practices_by_category(category)}
    
    @app.get("/resources/python/best-practices/{practice_id}")
    async def get_best_practice_info(practice_id: str):
        """Récupère les informations d'une bonne pratique spécifique."""
        practice = get_best_practice(practice_id)
        if practice:
            return practice.model_dump()
        return {"error": f"Bonne pratique {practice_id} non trouvée"}
    
    # Enregistrement dans le gestionnaire de ressources
    if "resource_manager" in app_state:
        app_state["resource_manager"].register_resource(
            "python_best_practices",
            {
                "description": "Bonnes pratiques Python",
                "practices": get_all_best_practices(),
                "get_practice": get_best_practice,
                "get_by_category": get_best_practices_by_category
            }
        )
