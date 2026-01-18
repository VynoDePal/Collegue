"""
Ressources pour les bonnes pratiques TypeScript.

Ce module fournit des recommandations et bonnes pratiques pour le développement TypeScript.
"""
from fastmcp import FastMCP
from typing import Dict, Any, List
import json

GENERAL_BEST_PRACTICES = {
    "strict_null_checks": {
        "title": "Activer strictNullChecks",
        "description": "Permet de détecter les erreurs potentielles liées aux valeurs null et undefined",
        "example_bad": """
function getLength(text: string) {
  return text.length;
}

// Erreur potentielle: getLength(null) causera une erreur à l'exécution
""",
        "example_good": """
// Avec strictNullChecks activé
function getLength(text: string | null | undefined) {
  if (text === null || text === undefined) {
    return 0;
  }
  return text.length;
}
"""
    },
    "avoid_any": {
        "title": "Éviter le type any",
        "description": "Le type any contourne la vérification de type, ce qui annule les avantages de TypeScript",
        "example_bad": """
function process(data: any) {
  return data.length; // Aucune garantie que data a une propriété length
}
""",
        "example_good": """
function process(data: string[] | string) {
  return data.length; // TypeScript vérifie que data a une propriété length
}
"""
    },
    "use_interfaces": {
        "title": "Utiliser des interfaces pour les objets",
        "description": "Les interfaces définissent clairement la structure des objets",
        "example_bad": """
function updateUser(user: any) {
  user.name = "Nouveau nom";
}
""",
        "example_good": """
interface User {
  id: number;
  name: string;
  email?: string;
}

function updateUser(user: User) {
  user.name = "Nouveau nom";
}
"""
    },
    "use_type_aliases": {
        "title": "Utiliser des alias de type pour les types complexes",
        "description": "Les alias de type simplifient la réutilisation des types complexes",
        "example_bad": """
function processData(
  data: string | number | boolean | null | undefined
) {
  // Traitement...
}
""",
        "example_good": """
type DataType = string | number | boolean | null | undefined;

function processData(data: DataType) {
  // Traitement...
}
"""
    },
    "readonly_properties": {
        "title": "Utiliser readonly pour les propriétés immuables",
        "description": "Empêche la modification accidentelle des propriétés qui ne devraient pas changer",
        "example_bad": """
interface User {
  id: number;
  name: string;
}

function processUser(user: User) {
  user.id = 456; // Modification accidentelle de l'ID
}
""",
        "example_good": """
interface User {
  readonly id: number;
  name: string;
}

function processUser(user: User) {
  // user.id = 456; // Erreur de compilation
  user.name = "Nouveau nom"; // OK
}
"""
    }
}

FUNCTION_BEST_PRACTICES = {
    "function_return_types": {
        "title": "Spécifier les types de retour des fonctions",
        "description": "Rend le code plus prévisible et facilite la détection d'erreurs",
        "example_bad": """
function add(a: number, b: number) {
  return a + b;
}
""",
        "example_good": """
function add(a: number, b: number): number {
  return a + b;
}
"""
    },
    "function_overloads": {
        "title": "Utiliser les surcharges de fonctions pour les comportements complexes",
        "description": "Permet de définir plusieurs signatures pour une même fonction",
        "example_bad": """
function process(input: string | number) {
  if (typeof input === 'string') {
    return input.toUpperCase();
  } else {
    return input * 2;
  }
}
""",
        "example_good": """
function process(input: string): string;
function process(input: number): number;
function process(input: string | number): string | number {
  if (typeof input === 'string') {
    return input.toUpperCase();
  } else {
    return input * 2;
  }
}
"""
    },
    "optional_parameters": {
        "title": "Utiliser des paramètres optionnels ou des valeurs par défaut",
        "description": "Rend les fonctions plus flexibles",
        "example_bad": """
function greet(name: string, greeting: string) {
  return `${greeting}, ${name}!`;
}
// Doit toujours fournir les deux arguments
""",
        "example_good": """
// Avec paramètre optionnel
function greet(name: string, greeting?: string): string {
  return `${greeting || 'Hello'}, ${name}!`;
}

// Ou avec valeur par défaut
function greetWithDefault(name: string, greeting: string = 'Hello'): string {
  return `${greeting}, ${name}!`;
}
"""
    }
}

CLASS_BEST_PRACTICES = {
    "access_modifiers": {
        "title": "Utiliser les modificateurs d'accès",
        "description": "Contrôle l'accès aux propriétés et méthodes des classes",
        "example_bad": """
class User {
  id = 0;
  name = '';
  
  updateProfile() {
    // Code...
  }
}
""",
        "example_good": """
class User {
  private id: number;
  public name: string;
  
  constructor(id: number, name: string) {
    this.id = id;
    this.name = name;
  }
  
  public updateProfile(): void {
    // Code...
  }
  
  private validateData(): boolean {
    // Code interne...
    return true;
  }
}
"""
    },
    "parameter_properties": {
        "title": "Utiliser les propriétés de paramètres dans les constructeurs",
        "description": "Simplifie la définition et l'initialisation des propriétés de classe",
        "example_bad": """
class User {
  private id: number;
  public name: string;
  
  constructor(id: number, name: string) {
    this.id = id;
    this.name = name;
  }
}
""",
        "example_good": """
class User {
  constructor(
    private id: number,
    public name: string
  ) {}
}
"""
    },
    "implement_interfaces": {
        "title": "Implémenter des interfaces pour les classes",
        "description": "Garantit que les classes respectent un contrat défini",
        "example_bad": """
class UserService {
  getUsers() { return []; }
  getUserById(id: number) { return { id, name: 'User' }; }
}
""",
        "example_good": """
interface IUserService {
  getUsers(): User[];
  getUserById(id: number): User | undefined;
}

class UserService implements IUserService {
  getUsers(): User[] { 
    return []; 
  }
  
  getUserById(id: number): User | undefined { 
    return { id, name: 'User' }; 
  }
}
"""
    }
}

GENERICS_BEST_PRACTICES = {
    "use_generics": {
        "title": "Utiliser des génériques pour les fonctions et classes réutilisables",
        "description": "Permet de créer des composants qui fonctionnent avec différents types",
        "example_bad": """
function first(arr: any[]): any {
  return arr[0];
}
""",
        "example_good": """
function first<T>(arr: T[]): T | undefined {
  return arr[0];
}

// Usage:
const num = first<number>([1, 2, 3]); // Type: number | undefined
const str = first(['a', 'b', 'c']); // Type inféré: string | undefined
"""
    },
    "constrain_generics": {
        "title": "Contraindre les génériques quand nécessaire",
        "description": "Limite les types qui peuvent être utilisés comme paramètres génériques",
        "example_bad": """
function getProperty<T>(obj: T, key: string): any {
  return obj[key]; // Erreur: obj[key] n'est pas sûr
}
""",
        "example_good": """
function getProperty<T, K extends keyof T>(obj: T, key: K): T[K] {
  return obj[key]; // Sûr: key est garanti d'être une clé de obj
}

// Usage:
const user = { id: 1, name: 'Alice' };
const name = getProperty(user, 'name'); // Type: string
// const invalid = getProperty(user, 'age'); // Erreur de compilation
"""
    }
}

TOOLING_BEST_PRACTICES = {
    "strict_mode": {
        "title": "Activer le mode strict",
        "description": "Active toutes les vérifications de type strictes",
        "example": """
// tsconfig.json
{
  "compilerOptions": {
    "strict": true
  }
}
"""
    },
    "eslint": {
        "title": "Utiliser ESLint avec TypeScript",
        "description": "Ajoute des règles de linting spécifiques à TypeScript",
        "example": """
// .eslintrc.js
module.exports = {
  parser: '@typescript-eslint/parser',
  plugins: ['@typescript-eslint'],
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended'
  ],
  rules: {
    '@typescript-eslint/explicit-function-return-type': 'error',
    '@typescript-eslint/no-explicit-any': 'error'
  }
};
"""
    },
    "prettier": {
        "title": "Utiliser Prettier pour le formatage",
        "description": "Assure un style de code cohérent",
        "example": """
// .prettierrc
{
  "semi": true,
  "singleQuote": true,
  "tabWidth": 2,
  "trailingComma": "es5"
}
"""
    },
    "path_aliases": {
        "title": "Configurer des alias de chemins",
        "description": "Simplifie les imports dans les projets complexes",
        "example": """
// tsconfig.json
{
  "compilerOptions": {
    "baseUrl": ".",
    "paths": {
      "@core/*": ["src/core/*"],
      "@components/*": ["src/components/*"],
      "@utils/*": ["src/utils/*"]
    }
  }
}

// Usage:
import { Button } from '@components/Button';
import { formatDate } from '@utils/date';
"""
    }
}

def register(app: FastMCP, app_state: dict):
    """
    Enregistre les ressources de bonnes pratiques TypeScript dans l'application FastMCP.
    
    Args:
        app: L'application FastMCP
        app_state: L'état de l'application
    """
    @app.resource("collegue://typescript/best_practices/general")
    def typescript_general_best_practices() -> str:
        """Fournit des bonnes pratiques générales pour TypeScript."""
        return json.dumps(GENERAL_BEST_PRACTICES)
    
    @app.resource("collegue://typescript/best_practices/functions")
    def typescript_function_best_practices() -> str:
        """Fournit des bonnes pratiques pour les fonctions TypeScript."""
        return json.dumps(FUNCTION_BEST_PRACTICES)
    
    @app.resource("collegue://typescript/best_practices/classes")
    def typescript_class_best_practices() -> str:
        """Fournit des bonnes pratiques pour les classes TypeScript."""
        return json.dumps(CLASS_BEST_PRACTICES)
    
    @app.resource("collegue://typescript/best_practices/generics")
    def typescript_generics_best_practices() -> str:
        """Fournit des bonnes pratiques pour les génériques TypeScript."""
        return json.dumps(GENERICS_BEST_PRACTICES)
    
    @app.resource("collegue://typescript/best_practices/tooling")
    def typescript_tooling_best_practices() -> str:
        """Fournit des bonnes pratiques pour la configuration et les outils TypeScript."""
        return json.dumps(TOOLING_BEST_PRACTICES)
    
    @app.resource("collegue://typescript/best_practices/{category}/{practice_id}")
    def typescript_best_practice_example(category: str, practice_id: str = None) -> str:
        """
        Fournit un exemple de bonne pratique TypeScript spécifique.
        
        Args:
            category: Catégorie de bonnes pratiques (ex: 'general', 'function', 'class', 'generics', 'tooling')
            practice_id: Identifiant de la bonne pratique
        """
        if not category:
            return json.dumps({
                "error": "Category is required",
                "available_categories": ["general", "function", "class", "generics", "tooling"]
            })
        
        # Sélectionner la catégorie
        if category.lower() == "general":
            practices = GENERAL_BEST_PRACTICES
        elif category.lower() == "function":
            practices = FUNCTION_BEST_PRACTICES
        elif category.lower() == "class":
            practices = CLASS_BEST_PRACTICES
        elif category.lower() == "generics":
            practices = GENERICS_BEST_PRACTICES
        elif category.lower() == "tooling":
            practices = TOOLING_BEST_PRACTICES
        else:
            return json.dumps({
                "error": f"Category '{category}' not found",
                "available_categories": ["general", "function", "class", "generics", "tooling"]
            })
        
        # Si practice_id est fourni, retourner cette pratique spécifique
        if practice_id:
            if practice_id in practices:
                return json.dumps(practices[practice_id])
            else:
                return json.dumps({
                    "error": f"Practice '{practice_id}' not found in category '{category}'",
                    "available_practices": list(practices.keys())
                })
        
        # Sinon, retourner toutes les pratiques de la catégorie
        return json.dumps(practices)
