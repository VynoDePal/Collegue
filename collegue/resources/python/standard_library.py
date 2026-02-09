"""
Standard Library Python - Ressources pour la bibliothèque standard Python
"""
from pydantic import BaseModel
from typing import Dict, List, Optional, Any
import json
import os

class PythonModuleReference(BaseModel):
    """Modèle pour une référence de module Python."""
    name: str
    description: str
    version: Optional[str] = None
    functions: List[Dict[str, Any]] = []
    classes: List[Dict[str, Any]] = []
    constants: List[Dict[str, Any]] = []
    examples: List[Dict[str, str]] = []
    url: Optional[str] = None

STDLIB_MODULES = {

    "os": {
        "name": "os",
        "description": "Interface portable avec le système d'exploitation",
        "url": "https://docs.python.org/3/library/os.html",
        "examples": [
            {"title": "Liste des fichiers", "code": "import os\nfiles = os.listdir('.')"},
            {"title": "Chemin absolu", "code": "import os\npath = os.path.abspath('file.txt')"}
        ]
    },
    "sys": {
        "name": "sys",
        "description": "Configuration spécifique au système et paramètres d'interpréteur",
        "url": "https://docs.python.org/3/library/sys.html",
        "examples": [
            {"title": "Arguments de ligne de commande", "code": "import sys\nargs = sys.argv"},
            {"title": "Sortie standard", "code": "import sys\nsys.stdout.write('Hello')"}
        ]
    },
    "datetime": {
        "name": "datetime",
        "description": "Types de base pour la manipulation de dates et heures",
        "url": "https://docs.python.org/3/library/datetime.html",
        "examples": [
            {"title": "Date actuelle", "code": "from datetime import datetime\nnow = datetime.now()"},
            {"title": "Formatage de date", "code": "from datetime import datetime\nformatted = datetime.now().strftime('%Y-%m-%d')"}
        ]
    },
    "json": {
        "name": "json",
        "description": "Encodage et décodage JSON",
        "url": "https://docs.python.org/3/library/json.html",
        "examples": [
            {"title": "Sérialisation", "code": "import json\ndata = json.dumps({'key': 'value'})"},
            {"title": "Désérialisation", "code": "import json\nobj = json.loads('{\"key\": \"value\"}')"}
        ]
    },
    "re": {
        "name": "re",
        "description": "Opérations sur les expressions régulières",
        "url": "https://docs.python.org/3/library/re.html",
        "examples": [
            {"title": "Recherche de motif", "code": "import re\nmatch = re.search(r'\\d+', 'abc123')"},
            {"title": "Remplacement", "code": "import re\nresult = re.sub(r'\\d+', 'X', 'abc123')"}
        ]
    },

    "collections": {
        "name": "collections",
        "description": "Types de conteneurs spécialisés",
        "url": "https://docs.python.org/3/library/collections.html",
        "examples": [
            {"title": "Counter", "code": "from collections import Counter\nc = Counter(['a', 'b', 'a'])"},
            {"title": "defaultdict", "code": "from collections import defaultdict\nd = defaultdict(list)"}
        ]
    },

    "threading": {
        "name": "threading",
        "description": "Exécution de code en parallèle via des threads",
        "url": "https://docs.python.org/3/library/threading.html",
        "examples": [
            {"title": "Création de thread", "code": "import threading\nt = threading.Thread(target=lambda: print('Hello'))"},
            {"title": "Synchronisation", "code": "import threading\nlock = threading.Lock()\nwith lock:\n    print('Thread-safe')"}
        ]
    },
    "asyncio": {
        "name": "asyncio",
        "description": "Programmation asynchrone avec async/await",
        "url": "https://docs.python.org/3/library/asyncio.html",
        "examples": [
            {"title": "Coroutine simple", "code": "import asyncio\nasync def main():\n    await asyncio.sleep(1)\n    return 'done'"},
            {"title": "Exécution de tâches", "code": "import asyncio\nasyncio.run(main())"}
        ]
    }
}

def get_module_reference(module_name: str) -> Optional[PythonModuleReference]:
    """Récupère les informations d'un module de la bibliothèque standard."""
    if module_name in STDLIB_MODULES:
        return PythonModuleReference(**STDLIB_MODULES[module_name])
    return None

def get_all_modules() -> List[str]:
    """Récupère la liste de tous les modules disponibles."""
    return list(STDLIB_MODULES.keys())

def register_stdlib(app, app_state):
    """Enregistre les ressources de la bibliothèque standard Python."""

    @app.resource("collegue://python/stdlib/index")
    def get_stdlib_index() -> str:
        """Liste tous les modules de la bibliothèque standard Python disponibles."""
        return json.dumps(get_all_modules())

    @app.resource("collegue://python/stdlib/{module_name}")
    def get_stdlib_module_resource(module_name: str) -> str:
        """Récupère les informations d'un module spécifique de la bibliothèque standard."""
        module = get_module_reference(module_name)
        if module:
            return module.model_dump_json()
        return json.dumps({"error": f"Module {module_name} non trouvé"})
