"""
Best Practices JavaScript - Ressources pour les bonnes pratiques en JavaScript
"""
from pydantic import BaseModel
from typing import Dict, List, Optional, Any

import json

class JavaScriptBestPractice(BaseModel):
    """Modèle pour une bonne pratique JavaScript."""
    title: str
    description: str
    category: str
    examples: Dict[str, Dict[str, str]] = {}  # "good" et "bad" exemples
    references: List[str] = []

JS_BEST_PRACTICES = {
    "use_strict": {
        "title": "Utiliser 'use strict'",
        "description": "Le mode strict permet d'éviter certaines erreurs silencieuses et améliore les performances.",
        "category": "syntax",
        "examples": {
            "good": {
                "title": "Avec mode strict",
                "code": "'use strict';\n\nfunction doSomething() {\n  // code sécurisé\n  let x = 10;\n  return x;\n}"
            },
            "bad": {
                "title": "Sans mode strict",
                "code": "function doSomething() {\n  // peut causer des erreurs silencieuses\n  x = 10;  // variable globale non déclarée\n  return x;\n}"
            }
        },
        "references": ["https://developer.mozilla.org/fr/docs/Web/JavaScript/Reference/Strict_mode"]
    },
    "const_let": {
        "title": "Préférer const et let à var",
        "description": "const et let ont une portée de bloc et sont plus prévisibles que var.",
        "category": "variables",
        "examples": {
            "good": {
                "title": "Utilisation de const et let",
                "code": "const PI = 3.14;  // valeur constante\nlet count = 0;  // variable qui peut changer\ncount += 1;"
            },
            "bad": {
                "title": "Utilisation de var",
                "code": "var PI = 3.14;  // peut être réassigné accidentellement\nvar count = 0;\ncount += 1;"
            }
        },
        "references": ["https://developer.mozilla.org/fr/docs/Web/JavaScript/Reference/Statements/const", "https://developer.mozilla.org/fr/docs/Web/JavaScript/Reference/Statements/let"]
    },
    "arrow_functions": {
        "title": "Utiliser les fonctions fléchées",
        "description": "Les fonctions fléchées sont plus concises et ne redéfinissent pas this.",
        "category": "functions",
        "examples": {
            "good": {
                "title": "Fonction fléchée",
                "code": "const add = (a, b) => a + b;\n\n// Préserve le contexte this\nclass Counter {\n  constructor() {\n    this.count = 0;\n  }\n  start() {\n    setInterval(() => this.count++, 1000);\n  }\n}"
            },
            "bad": {
                "title": "Fonction traditionnelle",
                "code": "const add = function(a, b) {\n  return a + b;\n};\n\n// Perd le contexte this\nclass Counter {\n  constructor() {\n    this.count = 0;\n  }\n  start() {\n    const self = this;  // workaround nécessaire\n    setInterval(function() {\n      self.count++;\n    }, 1000);\n  }\n}"
            }
        },
        "references": ["https://developer.mozilla.org/fr/docs/Web/JavaScript/Reference/Functions/Arrow_functions"]
    },
    "destructuring": {
        "title": "Utiliser la déstructuration",
        "description": "La déstructuration permet d'extraire des données de tableaux ou d'objets de manière concise.",
        "category": "syntax",
        "examples": {
            "good": {
                "title": "Avec déstructuration",
                "code": "const person = { name: 'John', age: 30 };\nconst { name, age } = person;\n\nfunction printCoords({ x, y }) {\n  console.log(`X: ${x}, Y: ${y}`);\n}"
            },
            "bad": {
                "title": "Sans déstructuration",
                "code": "const person = { name: 'John', age: 30 };\nconst name = person.name;\nconst age = person.age;\n\nfunction printCoords(coords) {\n  console.log(`X: ${coords.x}, Y: ${coords.y}`);\n}"
            }
        },
        "references": ["https://developer.mozilla.org/fr/docs/Web/JavaScript/Reference/Operators/Destructuring_assignment"]
    },
    "promises": {
        "title": "Utiliser les Promesses ou async/await",
        "description": "Les Promesses et async/await permettent de gérer le code asynchrone de manière plus lisible.",
        "category": "async",
        "examples": {
            "good": {
                "title": "Avec async/await",
                "code": "async function fetchData() {\n  try {\n    const response = await fetch('https://api.example.com/data');\n    const data = await response.json();\n    return data;\n  } catch (error) {\n    console.error('Error:', error);\n  }\n}"
            },
            "bad": {
                "title": "Callback hell",
                "code": "function fetchData(callback) {\n  fetch('https://api.example.com/data')\n    .then(function(response) {\n      response.json()\n        .then(function(data) {\n          callback(null, data);\n        })\n        .catch(function(error) {\n          callback(error);\n        });\n    })\n    .catch(function(error) {\n      callback(error);\n    });\n}"
            }
        },
        "references": ["https://developer.mozilla.org/fr/docs/Web/JavaScript/Reference/Global_Objects/Promise", "https://developer.mozilla.org/fr/docs/Web/JavaScript/Reference/Statements/async_function"]
    },
    "modules": {
        "title": "Utiliser les modules ES",
        "description": "Les modules ES permettent une meilleure organisation du code et une gestion des dépendances plus claire.",
        "category": "organization",
        "examples": {
            "good": {
                "title": "Avec modules ES",
                "code": "// math.js\nexport function add(a, b) {\n  return a + b;\n}\n\n// main.js\nimport { add } from './math.js';\nconsole.log(add(2, 3));"
            },
            "bad": {
                "title": "Sans modules",
                "code": "// math.js\nfunction add(a, b) {\n  return a + b;\n}\nwindow.add = add;\n\n// main.js\nconsole.log(add(2, 3));"
            }
        },
        "references": ["https://developer.mozilla.org/fr/docs/Web/JavaScript/Guide/Modules"]
    }
}

def get_best_practice(practice_id: str) -> Optional[JavaScriptBestPractice]:
    """Récupère les informations d'une bonne pratique JavaScript."""
    if practice_id in JS_BEST_PRACTICES:
        return JavaScriptBestPractice(**JS_BEST_PRACTICES[practice_id])
    return None

def get_all_best_practices() -> List[str]:
    """Récupère la liste de toutes les bonnes pratiques disponibles."""
    return list(JS_BEST_PRACTICES.keys())

def get_best_practices_by_category(category: str) -> List[str]:
    """Récupère la liste des bonnes pratiques d'une catégorie spécifique."""
    return [id for id, data in JS_BEST_PRACTICES.items() 
            if data.get("category") == category]

def register_best_practices(app, app_state):
    """Enregistre les ressources des bonnes pratiques JavaScript."""
    
    @app.resource("collegue://javascript/best-practices/index")
    def get_js_best_practices_index() -> str:
        """Liste toutes les bonnes pratiques JavaScript disponibles."""
        return json.dumps(get_all_best_practices())
    
    @app.resource("collegue://javascript/best-practices/category/{category}")
    def get_js_best_practices_by_category_resource(category: str) -> str:
        """Liste les bonnes pratiques d'une catégorie spécifique."""
        return json.dumps(get_best_practices_by_category(category))
    
    @app.resource("collegue://javascript/best-practices/{practice_id}")
    def get_js_best_practice_resource(practice_id: str) -> str:
        """Récupère les informations d'une bonne pratique spécifique."""
        practice = get_best_practice(practice_id)
        if practice:
            return practice.model_dump_json()
        return json.dumps({"error": f"Bonne pratique {practice_id} non trouvée"})
