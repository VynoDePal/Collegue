"""
Standard Library JavaScript - Ressources pour les fonctionnalités standard de JavaScript
"""
from pydantic import BaseModel
from typing import Dict, List, Optional, Any
import json
import os

class JavaScriptAPIReference(BaseModel):
    """Modèle pour une référence d'API JavaScript."""
    name: str
    description: str
    type: str
    syntax: Optional[str] = None
    parameters: List[Dict[str, str]] = []
    return_value: Optional[Dict[str, str]] = None
    examples: List[Dict[str, str]] = []
    browser_compatibility: Dict[str, bool] = {}
    mdn_url: Optional[str] = None

JS_STANDARD_APIS = {

    "array": {
        "name": "Array",
        "description": "Objet global utilisé pour la construction de tableaux",
        "type": "object",
        "mdn_url": "https://developer.mozilla.org/fr/docs/Web/JavaScript/Reference/Global_Objects/Array",
        "examples": [
            {"title": "Création d'un tableau", "code": "const fruits = ['pomme', 'banane', 'orange'];"},
            {"title": "Méthodes de tableau", "code": "const fruits = ['pomme', 'banane'];\nfruits.push('orange');\nconst first = fruits.shift();"}
        ],
        "browser_compatibility": {"chrome": True, "firefox": True, "safari": True, "edge": True}
    },
    "array_map": {
        "name": "Array.prototype.map()",
        "description": "Crée un nouveau tableau avec les résultats de l'appel d'une fonction sur chaque élément du tableau",
        "type": "method",
        "syntax": "arr.map(callback(currentValue[, index[, array]]) { ... }[, thisArg])",
        "parameters": [
            {"name": "callback", "description": "Fonction appelée pour chaque élément du tableau"},
            {"name": "thisArg", "description": "Valeur à utiliser comme 'this' lors de l'exécution du callback"}
        ],
        "return_value": {"type": "Array", "description": "Un nouveau tableau avec les résultats"},
        "examples": [
            {"title": "Doubler les nombres", "code": "const numbers = [1, 2, 3, 4];\nconst doubled = numbers.map(num => num * 2);"}
        ],
        "mdn_url": "https://developer.mozilla.org/fr/docs/Web/JavaScript/Reference/Global_Objects/Array/map",
        "browser_compatibility": {"chrome": True, "firefox": True, "safari": True, "edge": True}
    },
    "string": {
        "name": "String",
        "description": "Objet global représentant une séquence de caractères",
        "type": "object",
        "mdn_url": "https://developer.mozilla.org/fr/docs/Web/JavaScript/Reference/Global_Objects/String",
        "examples": [
            {"title": "Création d'une chaîne", "code": "const str = 'Hello, world!';"},
            {"title": "Méthodes de chaîne", "code": "const str = 'Hello, world!';\nconst sub = str.substring(0, 5);\nconst upper = str.toUpperCase();"}
        ],
        "browser_compatibility": {"chrome": True, "firefox": True, "safari": True, "edge": True}
    },
    "promise": {
        "name": "Promise",
        "description": "Objet représentant l'achèvement ou l'échec d'une opération asynchrone",
        "type": "object",
        "syntax": "new Promise((resolve, reject) => { ... })",
        "mdn_url": "https://developer.mozilla.org/fr/docs/Web/JavaScript/Reference/Global_Objects/Promise",
        "examples": [
            {"title": "Création d'une promesse", "code": "const promise = new Promise((resolve, reject) => {\n  setTimeout(() => {\n    resolve('Success!');\n  }, 1000);\n});"},
            {"title": "Utilisation de then/catch", "code": "fetch('https://api.example.com/data')\n  .then(response => response.json())\n  .then(data => console.log(data))\n  .catch(error => console.error(error));"}
        ],
        "browser_compatibility": {"chrome": True, "firefox": True, "safari": True, "edge": True}
    },
    "json": {
        "name": "JSON",
        "description": "Format de données textuelles dérivé de la notation des objets JavaScript",
        "type": "object",
        "mdn_url": "https://developer.mozilla.org/fr/docs/Web/JavaScript/Reference/Global_Objects/JSON",
        "examples": [
            {"title": "Conversion en JSON", "code": "const obj = { name: 'John', age: 30 };\nconst json = JSON.stringify(obj);"},
            {"title": "Analyse JSON", "code": "const json = '{\"name\":\"John\",\"age\":30}';\nconst obj = JSON.parse(json);"}
        ],
        "browser_compatibility": {"chrome": True, "firefox": True, "safari": True, "edge": True}
    },


    "document": {
        "name": "Document",
        "description": "Interface représentant une page web chargée dans le navigateur",
        "type": "object",
        "mdn_url": "https://developer.mozilla.org/fr/docs/Web/API/Document",
        "examples": [
            {"title": "Sélection d'éléments", "code": "const element = document.getElementById('myId');\nconst elements = document.querySelectorAll('.myClass');"},
            {"title": "Création d'éléments", "code": "const div = document.createElement('div');\ndiv.textContent = 'Hello';\ndocument.body.appendChild(div);"}
        ],
        "browser_compatibility": {"chrome": True, "firefox": True, "safari": True, "edge": True}
    },


    "async_await": {
        "name": "async/await",
        "description": "Syntaxe permettant d'écrire du code asynchrone de manière synchrone",
        "type": "function",
        "syntax": "async function name([param[, param[, ...param]]]) { statements }",
        "mdn_url": "https://developer.mozilla.org/fr/docs/Web/JavaScript/Reference/Statements/async_function",
        "examples": [
            {"title": "Fonction async simple", "code": "async function fetchData() {\n  const response = await fetch('https://api.example.com/data');\n  const data = await response.json();\n  return data;\n}"},
            {"title": "Gestion des erreurs", "code": "async function fetchData() {\n  try {\n    const response = await fetch('https://api.example.com/data');\n    const data = await response.json();\n    return data;\n  } catch (error) {\n    console.error('Error:', error);\n  }\n}"}
        ],
        "browser_compatibility": {"chrome": True, "firefox": True, "safari": True, "edge": True}
    },
    "destructuring": {
        "name": "Destructuring assignment",
        "description": "Expression JavaScript qui permet d'extraire des données d'un tableau ou d'un objet",
        "type": "syntax",
        "mdn_url": "https://developer.mozilla.org/fr/docs/Web/JavaScript/Reference/Operators/Destructuring_assignment",
        "examples": [
            {"title": "Destructuration d'objet", "code": "const person = { name: 'John', age: 30 };\nconst { name, age } = person;"},
            {"title": "Destructuration de tableau", "code": "const colors = ['red', 'green', 'blue'];\nconst [first, second] = colors;"}
        ],
        "browser_compatibility": {"chrome": True, "firefox": True, "safari": True, "edge": True}
    }
}

def get_api_reference(api_name: str) -> Optional[JavaScriptAPIReference]:
    """Récupère les informations d'une API JavaScript standard."""
    if api_name in JS_STANDARD_APIS:
        return JavaScriptAPIReference(**JS_STANDARD_APIS[api_name])
    return None

def get_all_apis() -> List[str]:
    """Récupère la liste de toutes les APIs disponibles."""
    return list(JS_STANDARD_APIS.keys())

def get_apis_by_type(api_type: str) -> List[str]:
    """Récupère la liste des APIs d'un type spécifique."""
    return [name for name, data in JS_STANDARD_APIS.items()
            if data.get("type") == api_type]

def register_stdlib(app, app_state):
    """Enregistre les ressources de la bibliothèque standard JavaScript."""

    @app.resource("collegue://javascript/stdlib/index")
    def get_js_stdlib_index() -> str:
        """Liste toutes les APIs JavaScript disponibles."""
        return json.dumps(get_all_apis())

    @app.resource("collegue://javascript/stdlib/type/{api_type}")
    def get_js_apis_by_type_resource(api_type: str) -> str:
        """Liste les APIs d'un type spécifique."""
        return json.dumps(get_apis_by_type(api_type))

    @app.resource("collegue://javascript/stdlib/{api_name}")
    def get_js_api_resource(api_name: str) -> str:
        """Récupère les informations d'une API spécifique."""
        api = get_api_reference(api_name)
        if api:
            return api.model_dump_json()
        return json.dumps({"error": f"API {api_name} non trouvée"})
