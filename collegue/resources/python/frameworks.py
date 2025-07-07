"""
Frameworks Python - Ressources pour les frameworks Python populaires
"""
from pydantic import BaseModel
from typing import Dict, List, Optional, Any

class PythonFrameworkReference(BaseModel):
    """Modèle pour une référence de framework Python."""
    name: str
    description: str
    version: Optional[str] = None
    website: Optional[str] = None
    documentation: Optional[str] = None
    github: Optional[str] = None
    categories: List[str] = []
    features: List[str] = []
    examples: List[Dict[str, str]] = []

# Dictionnaire des frameworks Python populaires
PYTHON_FRAMEWORKS = {
    # Web Frameworks
    "django": {
        "name": "Django",
        "description": "Framework web complet pour le développement rapide et propre",
        "version": "4.2",
        "website": "https://www.djangoproject.com/",
        "documentation": "https://docs.djangoproject.com/",
        "github": "https://github.com/django/django",
        "categories": ["web", "full-stack", "orm"],
        "features": ["ORM", "Admin interface", "Authentication", "Routing", "Templates", "Forms", "Security"],
        "examples": [
            {"title": "Modèle simple", "code": "from django.db import models\n\nclass Article(models.Model):\n    title = models.CharField(max_length=100)\n    content = models.TextField()\n    published = models.DateTimeField(auto_now_add=True)"},
            {"title": "Vue simple", "code": "from django.http import HttpResponse\n\ndef hello(request):\n    return HttpResponse('Hello, world!')"}
        ]
    },
    "flask": {
        "name": "Flask",
        "description": "Micro-framework web léger et flexible",
        "version": "2.3",
        "website": "https://flask.palletsprojects.com/",
        "documentation": "https://flask.palletsprojects.com/en/2.3.x/",
        "github": "https://github.com/pallets/flask",
        "categories": ["web", "micro-framework"],
        "features": ["Routing", "Templates", "RESTful request dispatching", "Sessions", "Testing support"],
        "examples": [
            {"title": "Application simple", "code": "from flask import Flask\n\napp = Flask(__name__)\n\n@app.route('/')\ndef hello():\n    return 'Hello, World!'"},
            {"title": "Route avec paramètre", "code": "from flask import Flask\n\napp = Flask(__name__)\n\n@app.route('/user/<username>')\ndef show_user(username):\n    return f'User: {username}'"}
        ]
    },
    "fastapi": {
        "name": "FastAPI",
        "description": "Framework web moderne, rapide et basé sur les standards Python",
        "version": "0.100.0",
        "website": "https://fastapi.tiangolo.com/",
        "documentation": "https://fastapi.tiangolo.com/",
        "github": "https://github.com/tiangolo/fastapi",
        "categories": ["web", "api", "async"],
        "features": ["Async support", "Type hints", "Auto documentation", "Dependency injection", "Security", "Validation"],
        "examples": [
            {"title": "Application simple", "code": "from fastapi import FastAPI\n\napp = FastAPI()\n\n@app.get('/')\ndef read_root():\n    return {'Hello': 'World'}"},
            {"title": "Modèle Pydantic", "code": "from fastapi import FastAPI\nfrom pydantic import BaseModel\n\nclass Item(BaseModel):\n    name: str\n    price: float\n\napp = FastAPI()\n\n@app.post('/items/')\ndef create_item(item: Item):\n    return item"}
        ]
    },
    
    # Data Science
    "pandas": {
        "name": "Pandas",
        "description": "Bibliothèque d'analyse et de manipulation de données",
        "version": "2.0.0",
        "website": "https://pandas.pydata.org/",
        "documentation": "https://pandas.pydata.org/docs/",
        "github": "https://github.com/pandas-dev/pandas",
        "categories": ["data-science", "analysis", "visualization"],
        "features": ["DataFrame", "Series", "Data alignment", "Missing data handling", "Reshaping", "Merging", "Grouping"],
        "examples": [
            {"title": "Création de DataFrame", "code": "import pandas as pd\n\ndf = pd.DataFrame({'A': [1, 2, 3], 'B': [4, 5, 6]})"},
            {"title": "Lecture de CSV", "code": "import pandas as pd\n\ndf = pd.read_csv('data.csv')"}
        ]
    },
    "numpy": {
        "name": "NumPy",
        "description": "Bibliothèque fondamentale pour le calcul scientifique en Python",
        "version": "1.24.0",
        "website": "https://numpy.org/",
        "documentation": "https://numpy.org/doc/stable/",
        "github": "https://github.com/numpy/numpy",
        "categories": ["data-science", "scientific", "computation"],
        "features": ["N-dimensional arrays", "Broadcasting", "Mathematical functions", "Linear algebra", "Random number generation"],
        "examples": [
            {"title": "Création d'array", "code": "import numpy as np\n\narr = np.array([1, 2, 3])"},
            {"title": "Opérations matricielles", "code": "import numpy as np\n\na = np.array([[1, 2], [3, 4]])\nb = np.array([[5, 6], [7, 8]])\nc = np.dot(a, b)"}
        ]
    },
    
    # Testing
    "pytest": {
        "name": "pytest",
        "description": "Framework de test simple et puissant",
        "version": "7.3.1",
        "website": "https://pytest.org/",
        "documentation": "https://docs.pytest.org/",
        "github": "https://github.com/pytest-dev/pytest",
        "categories": ["testing", "quality-assurance"],
        "features": ["Fixtures", "Parametrization", "Plugins", "Assertions", "Markers"],
        "examples": [
            {"title": "Test simple", "code": "def test_function():\n    assert 1 + 1 == 2"},
            {"title": "Fixture", "code": "import pytest\n\n@pytest.fixture\ndef data():\n    return [1, 2, 3]\n\ndef test_data(data):\n    assert len(data) == 3"}
        ]
    }
}

def get_framework_reference(framework_name: str) -> Optional[PythonFrameworkReference]:
    """Récupère les informations d'un framework Python."""
    if framework_name.lower() in PYTHON_FRAMEWORKS:
        return PythonFrameworkReference(**PYTHON_FRAMEWORKS[framework_name.lower()])
    return None

def get_all_frameworks() -> List[str]:
    """Récupère la liste de tous les frameworks disponibles."""
    return list(PYTHON_FRAMEWORKS.keys())

def get_frameworks_by_category(category: str) -> List[str]:
    """Récupère la liste des frameworks d'une catégorie spécifique."""
    return [name for name, data in PYTHON_FRAMEWORKS.items() 
            if category in data.get("categories", [])]

def register_frameworks(app, app_state):
    """Enregistre les ressources des frameworks Python."""
    
    @app.get("/resources/python/frameworks")
    async def list_python_frameworks():
        """Liste tous les frameworks Python disponibles."""
        return {"frameworks": get_all_frameworks()}
    
    @app.get("/resources/python/frameworks/category/{category}")
    async def list_frameworks_by_category(category: str):
        """Liste les frameworks d'une catégorie spécifique."""
        return {"frameworks": get_frameworks_by_category(category)}
    
    @app.get("/resources/python/frameworks/{framework_name}")
    async def get_framework_info(framework_name: str):
        """Récupère les informations d'un framework spécifique."""
        framework = get_framework_reference(framework_name)
        if framework:
            return framework.model_dump()
        return {"error": f"Framework {framework_name} non trouvé"}
    
    # Enregistrement dans le gestionnaire de ressources
    if "resource_manager" in app_state:
        app_state["resource_manager"].register_resource(
            "python_frameworks",
            {
                "description": "Frameworks Python populaires",
                "frameworks": get_all_frameworks(),
                "get_framework": get_framework_reference,
                "get_by_category": get_frameworks_by_category
            }
        )
