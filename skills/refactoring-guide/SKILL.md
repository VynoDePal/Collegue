---
name: refactoring-guide
description: Guide de refactoring avec les principes SOLID, DRY, KISS et les patterns de transformation. Utilise cette skill quand l'utilisateur veut refactorer du code, améliorer la qualité, réduire la dette technique, ou appliquer un design pattern.
---

# Refactoring Guide — Workflow Collègue

Tu guides le refactoring de code en suivant une approche méthodique. Utilise `repo_consistency_check` pour diagnostiquer, `code_refactoring` pour transformer, et `test_generation` pour sécuriser.

## Règle d'or

> **Ne jamais refactorer sans tests.** Si le code n'a pas de tests, en générer d'abord avec `test_generation`.

## Workflow de refactoring

### 1. Diagnostic

Appelle `repo_consistency_check` pour identifier les problèmes :

```
repo_consistency_check({
  files: [...],
  checks: ['unused_imports', 'unused_vars', 'dead_code', 'duplication'],
  mode: 'deep'
})
```

**Indicateurs de besoin de refactoring :**
- Score de duplication > 20%
- Fichiers > 300 lignes
- Fonctions > 50 lignes
- Complexité cyclomatique > 10
- Imports inutilisés > 5 par fichier
- Variables non utilisées

### 2. Sécurisation (tests)

Avant toute modification, s'assurer que les tests existent :

```
test_generation({
  code: "...",
  language: "typescript",
  coverage_target: 0.8,
  include_mocks: true
})
```

### 3. Transformation

Appelle `code_refactoring` avec le type approprié :

| Situation | Type | Description |
|-----------|------|-------------|
| Nommage confus | `rename` | Renommer variables, fonctions, classes |
| Fonction trop longue | `extract` | Extraire des sous-fonctions |
| Logique complexe | `simplify` | Simplifier les conditions et boucles |
| Performance faible | `optimize` | Optimiser les algorithmes et requêtes |
| Code mort / imports inutiles | `clean` | Nettoyer le code superflu |
| Patterns obsolètes | `modernize` | Adopter les patterns modernes |

### 4. Vérification

Relancer `repo_consistency_check` pour confirmer l'amélioration.

## Quand refactorer

### Refactorer maintenant
- Avant d'ajouter une nouvelle feature au même fichier
- Quand un bug révèle du code fragile
- Quand les tests sont difficiles à écrire (signe de mauvais design)
- Quand le même code est copié-collé une 3ème fois (Rule of Three)

### Ne PAS refactorer maintenant
- Juste avant un deadline / release
- Du code qui fonctionne et qu'on ne touche pas
- Sans tests existants ou sans temps d'en écrire
- Pour des raisons purement esthétiques

## Techniques de refactoring par smell

### Bloaters (code trop gros)

**Long Method** (>50 lignes)
```
Technique: Extract Method
1. Identifier les blocs logiques dans la fonction
2. Extraire chaque bloc en une fonction nommée
3. Remplacer le bloc par l'appel de fonction
→ code_refactoring type: "extract"
```

**Large Class** (>300 lignes, >10 méthodes)
```
Technique: Extract Class
1. Identifier les groupes de méthodes/attributs liés
2. Créer une nouvelle classe pour chaque groupe
3. Déléguer via composition
→ code_refactoring type: "extract"
```

**Long Parameter List** (>4 paramètres)
```
Technique: Introduce Parameter Object
1. Grouper les paramètres liés en un objet/interface
2. Passer l'objet au lieu des paramètres individuels
→ code_refactoring type: "simplify"
```

### Couplers (couplage excessif)

**Feature Envy** (utilise plus les données d'un autre objet)
```
Technique: Move Method
1. Déplacer la méthode vers la classe dont elle utilise les données
→ code_refactoring type: "extract"
```

**Inappropriate Intimacy** (classes trop liées)
```
Technique: Extract Interface + Dependency Injection
1. Définir une interface pour le contrat
2. Injecter la dépendance au lieu de la créer
→ code_refactoring type: "simplify"
```

### Dispensables (code inutile)

**Dead Code**
```
Technique: Remove Dead Code
1. repo_consistency_check → détecte le code mort
2. Supprimer les fonctions/variables non utilisées
→ code_refactoring type: "clean"
```

**Duplicated Code**
```
Technique: Extract Method / Pull Up Method
1. repo_consistency_check → détecte les duplications
2. Extraire le code commun en fonction partagée
→ code_refactoring type: "extract"
```

### Change Preventers (résistance au changement)

**Shotgun Surgery** (un changement = modifier 10 fichiers)
```
Technique: Move Method + Inline Class
1. impact_analysis → voir les fichiers impactés
2. Consolider la logique dispersée en un module
→ code_refactoring type: "simplify"
```

## Modernisation par langage

### Python : patterns modernes
| Ancien | Moderne |
|--------|---------|
| `os.path.join()` | `pathlib.Path()` |
| `dict.keys()` iteration | `for k, v in dict.items()` |
| `try/except pass` | Gestion d'erreur explicite |
| `class Config: pass` | `@dataclass` ou `BaseModel` |
| `format()` strings | f-strings |
| `type()` checks | `isinstance()` + type hints |
| `threading` pour I/O | `asyncio` |
| manual `__init__` | `@dataclass(frozen=True)` |

### TypeScript / JavaScript : patterns modernes
| Ancien | Moderne |
|--------|---------|
| `var` | `const` / `let` |
| `.then().catch()` | `async/await` |
| `for (let i = 0; ...)` | `.map()` / `.filter()` / `.reduce()` |
| `Object.assign()` | Spread `{...obj}` |
| `require()` | `import` ESM |
| `class + inheritance` | Composition + hooks |
| `any` type | Types stricts + generics |
| Callback hell | Promises + async/await |

Pour des exemples détaillés, consulte [examples.md](examples.md).
