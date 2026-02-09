"""
Ressources pour les types et interfaces TypeScript.

Ce module fournit des informations sur les types et interfaces standard de TypeScript.
"""
from fastmcp import FastMCP
from typing import Dict, Any, List
import json

PRIMITIVE_TYPES = {
    "string": {
        "description": "Représente des séquences de caractères",
        "example": "let name: string = 'TypeScript';",
        "notes": "Équivalent à String en JavaScript"
    },
    "number": {
        "description": "Représente des valeurs numériques (entiers et flottants)",
        "example": "let age: number = 25; let price: number = 9.99;",
        "notes": "Équivalent à Number en JavaScript"
    },
    "boolean": {
        "description": "Représente une valeur logique true/false",
        "example": "let isActive: boolean = true;",
        "notes": "Équivalent à Boolean en JavaScript"
    },
    "null": {
        "description": "Représente l'absence intentionnelle de valeur",
        "example": "let empty: null = null;",
        "notes": "Souvent utilisé avec l'union de types"
    },
    "undefined": {
        "description": "Représente une variable non initialisée",
        "example": "let notDefined: undefined = undefined;",
        "notes": "Souvent utilisé avec l'union de types"
    },
    "any": {
        "description": "Type dynamique qui contourne la vérification de type",
        "example": "let variable: any = 4; variable = 'string';",
        "notes": "À éviter quand possible pour maintenir les avantages du typage statique"
    },
    "void": {
        "description": "Absence de type, utilisé pour les fonctions sans valeur de retour",
        "example": "function log(): void { console.log('message'); }",
        "notes": "Différent de undefined, représente l'absence de retour"
    },
    "never": {
        "description": "Représente un type qui ne se produit jamais",
        "example": "function error(): never { throw new Error('message'); }",
        "notes": "Utilisé pour les fonctions qui ne terminent jamais ou qui lancent toujours une exception"
    },
    "unknown": {
        "description": "Type sécurisé pour les valeurs de type inconnu",
        "example": "let value: unknown = getValueFromAPI();",
        "notes": "Plus sûr que any, nécessite une vérification de type avant utilisation"
    }
}

COMPLEX_TYPES = {
    "array": {
        "description": "Collection ordonnée d'éléments du même type",
        "syntax": ["Type[]", "Array<Type>"],
        "example": "let numbers: number[] = [1, 2, 3]; let strings: Array<string> = ['a', 'b'];",
        "notes": "Les deux syntaxes sont équivalentes"
    },
    "tuple": {
        "description": "Tableau avec un nombre fixe d'éléments dont les types sont connus",
        "syntax": "[Type1, Type2, ...]",
        "example": "let person: [string, number] = ['Alice', 30];",
        "notes": "Utile quand on connaît le nombre exact d'éléments et leur type"
    },
    "enum": {
        "description": "Ensemble de constantes nommées",
        "syntax": "enum Name { Value1, Value2, ... }",
        "example": "enum Direction { Up, Down, Left, Right }",
        "notes": "Peut être numérique (par défaut) ou de type string"
    },
    "object": {
        "description": "Représente un type non-primitif",
        "syntax": "{ prop1: Type1, prop2: Type2, ... }",
        "example": "let user: { name: string, age: number } = { name: 'Alice', age: 30 };",
        "notes": "Généralement défini via interfaces ou types"
    },
    "union": {
        "description": "Type qui peut être l'un des types spécifiés",
        "syntax": "Type1 | Type2 | ...",
        "example": "let id: string | number = 101;",
        "notes": "Utile pour les variables qui peuvent avoir différents types"
    },
    "intersection": {
        "description": "Type qui combine plusieurs types",
        "syntax": "Type1 & Type2 & ...",
        "example": "type Employee = Person & { employeeId: number };",
        "notes": "Combine toutes les propriétés des types spécifiés"
    },
    "literal": {
        "description": "Type qui représente une valeur exacte",
        "syntax": "value as const",
        "example": "let direction: 'up' | 'down' = 'up';",
        "notes": "Peut être string, number ou boolean"
    },
    "type alias": {
        "description": "Nom pour un type",
        "syntax": "type Name = Type",
        "example": "type Point = { x: number, y: number };",
        "notes": "Simplifie la réutilisation des types complexes"
    }
}

INTERFACES = {
    "basic": {
        "description": "Définit la structure d'un objet",
        "syntax": "interface Name { prop1: Type1; prop2: Type2; }",
        "example": "interface User { id: number; name: string; }",
        "notes": "Utilisé pour définir des contrats dans le code"
    },
    "optional properties": {
        "description": "Propriétés qui peuvent être omises",
        "syntax": "interface Name { prop?: Type; }",
        "example": "interface User { id: number; email?: string; }",
        "notes": "Le ? indique que la propriété est optionnelle"
    },
    "readonly properties": {
        "description": "Propriétés qui ne peuvent pas être modifiées après initialisation",
        "syntax": "interface Name { readonly prop: Type; }",
        "example": "interface User { readonly id: number; name: string; }",
        "notes": "readonly empêche la modification après création"
    },
    "extending interfaces": {
        "description": "Interface qui hérite d'une autre interface",
        "syntax": "interface Child extends Parent { }",
        "example": "interface Employee extends Person { employeeId: number; }",
        "notes": "Permet la réutilisation et l'extension des interfaces existantes"
    },
    "implementing interfaces": {
        "description": "Classes qui implémentent une interface",
        "syntax": "class Name implements Interface { }",
        "example": "class User implements IUser { id: number; name: string; }",
        "notes": "Garantit que la classe respecte le contrat défini par l'interface"
    },
    "function interfaces": {
        "description": "Interfaces pour définir des signatures de fonctions",
        "syntax": "interface Name { (param: Type): ReturnType; }",
        "example": "interface SearchFunc { (source: string, subString: string): boolean; }",
        "notes": "Utile pour typer des fonctions ou callbacks"
    },
    "indexable interfaces": {
        "description": "Interfaces pour les objets avec index",
        "syntax": "interface Name { [key: KeyType]: ValueType; }",
        "example": "interface StringArray { [index: number]: string; }",
        "notes": "Permet de définir des types pour les objets ou tableaux indexables"
    }
}

GENERICS = {
    "basic": {
        "description": "Types paramétrés qui permettent la réutilisation",
        "syntax": "function name<T>(param: T): T { }",
        "example": "function identity<T>(arg: T): T { return arg; }",
        "notes": "Permet de créer des composants réutilisables avec différents types"
    },
    "generic interfaces": {
        "description": "Interfaces avec paramètres de type",
        "syntax": "interface Name<T> { prop: T; }",
        "example": "interface Box<T> { value: T; }",
        "notes": "Permet de créer des interfaces réutilisables avec différents types"
    },
    "generic classes": {
        "description": "Classes avec paramètres de type",
        "syntax": "class Name<T> { prop: T; }",
        "example": "class Container<T> { private item: T; constructor(item: T) { this.item = item; } }",
        "notes": "Permet de créer des classes réutilisables avec différents types"
    },
    "generic constraints": {
        "description": "Restrictions sur les paramètres de type",
        "syntax": "<T extends Constraint>",
        "example": "function getProperty<T, K extends keyof T>(obj: T, key: K): T[K] { return obj[key]; }",
        "notes": "Limite les types qui peuvent être utilisés comme paramètres génériques"
    },
    "default type parameters": {
        "description": "Valeurs par défaut pour les paramètres de type",
        "syntax": "<T = DefaultType>",
        "example": "interface Config<T = string> { value: T; }",
        "notes": "Permet de spécifier un type par défaut si aucun n'est fourni"
    }
}

TYPE_UTILITIES = {
    "Partial<T>": {
        "description": "Rend toutes les propriétés de T optionnelles",
        "example": "type PartialUser = Partial<User>;",
        "notes": "Utile pour les mises à jour partielles d'objets"
    },
    "Required<T>": {
        "description": "Rend toutes les propriétés de T obligatoires",
        "example": "type RequiredUser = Required<User>;",
        "notes": "Inverse de Partial"
    },
    "Readonly<T>": {
        "description": "Rend toutes les propriétés de T en lecture seule",
        "example": "type ReadonlyUser = Readonly<User>;",
        "notes": "Empêche la modification des propriétés"
    },
    "Record<K, T>": {
        "description": "Crée un type avec des propriétés de type K et des valeurs de type T",
        "example": "type UserRoles = Record<string, Role>;",
        "notes": "Utile pour créer des dictionnaires ou des mappages"
    },
    "Pick<T, K>": {
        "description": "Crée un type en sélectionnant un ensemble de propriétés K de T",
        "example": "type UserBasics = Pick<User, 'id' | 'name'>;",
        "notes": "Permet de créer un sous-ensemble d'un type existant"
    },
    "Omit<T, K>": {
        "description": "Crée un type en omettant un ensemble de propriétés K de T",
        "example": "type UserWithoutPassword = Omit<User, 'password'>;",
        "notes": "Inverse de Pick"
    },
    "Exclude<T, U>": {
        "description": "Exclut de T les types assignables à U",
        "example": "type NumberOnly = Exclude<string | number | boolean, string | boolean>;",
        "notes": "Utile pour filtrer des types d'union"
    },
    "Extract<T, U>": {
        "description": "Extrait de T les types assignables à U",
        "example": "type StringOrBoolean = Extract<string | number | boolean, string | boolean>;",
        "notes": "Inverse de Exclude"
    },
    "NonNullable<T>": {
        "description": "Exclut null et undefined de T",
        "example": "type NonNullableString = NonNullable<string | null | undefined>;",
        "notes": "Utile pour garantir des valeurs non nulles"
    },
    "ReturnType<T>": {
        "description": "Extrait le type de retour d'une fonction",
        "example": "type Result = ReturnType<typeof myFunction>;",
        "notes": "Permet d'obtenir le type de retour d'une fonction"
    },
    "Parameters<T>": {
        "description": "Extrait les types des paramètres d'une fonction",
        "example": "type Params = Parameters<typeof myFunction>;",
        "notes": "Permet d'obtenir les types des paramètres d'une fonction"
    }
}

def register(app: FastMCP, app_state: dict):
    """
    Enregistre les ressources de types TypeScript dans l'application FastMCP.

    Args:
        app: L'application FastMCP
        app_state: L'état de l'application
    """
    @app.resource("collegue://typescript/types")
    def typescript_types() -> str:
        """Fournit des informations sur les types primitifs et complexes de TypeScript."""
        return json.dumps({
            "primitive_types": PRIMITIVE_TYPES,
            "complex_types": COMPLEX_TYPES
        })

    @app.resource("collegue://typescript/interfaces")
    def typescript_interfaces() -> str:
        """Fournit des informations sur les interfaces TypeScript."""
        return json.dumps(INTERFACES)

    @app.resource("collegue://typescript/generics")
    def typescript_generics() -> str:
        """Fournit des informations sur les génériques TypeScript."""
        return json.dumps(GENERICS)

    @app.resource("collegue://typescript/type_utilities")
    def typescript_type_utilities() -> str:
        """Fournit des informations sur les utilitaires de types TypeScript."""
        return json.dumps(TYPE_UTILITIES)

    @app.resource("collegue://typescript/type_examples/{type_name}")
    def typescript_type_examples(type_name: str) -> str:
        """
        Fournit des exemples d'utilisation pour un type TypeScript spécifique.

        Args:
            type_name: Nom du type TypeScript (ex: 'string', 'array', 'interface', etc.)
        """

        if type_name.lower() in PRIMITIVE_TYPES:
            return json.dumps(PRIMITIVE_TYPES[type_name.lower()])


        if type_name.lower() in COMPLEX_TYPES:
            return json.dumps(COMPLEX_TYPES[type_name.lower()])


        if type_name.lower() in INTERFACES:
            return json.dumps(INTERFACES[type_name.lower()])


        if type_name.lower() in GENERICS:
            return json.dumps(GENERICS[type_name.lower()])


        utility_name = f"{type_name}<T>" if not type_name.endswith(">") else type_name
        if utility_name in TYPE_UTILITIES:
            return json.dumps(TYPE_UTILITIES[utility_name])

        return json.dumps({"error": f"Type '{type_name}' not found"})
