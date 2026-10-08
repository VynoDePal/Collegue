# Catalogue de patterns et anti-patterns

## Design Patterns — Quand les recommander

### Creational Patterns

| Pattern | Quand l'utiliser | Anti-pattern correspondant |
|---------|-----------------|--------------------------|
| **Factory Method** | Création d'objets avec logique de sélection | `if/else` ou `switch` géant pour instancier |
| **Builder** | Objet avec beaucoup de paramètres optionnels | Constructeur avec 10+ paramètres |
| **Singleton** | Ressource partagée unique (DB pool, config) | Variables globales mutables |
| **Dependency Injection** | Découplage composants / testabilité | Instanciation directe dans le constructeur |

### Structural Patterns

| Pattern | Quand l'utiliser | Anti-pattern correspondant |
|---------|-----------------|--------------------------|
| **Adapter** | Intégrer une API tierce incompatible | Couplage direct à l'API externe partout |
| **Facade** | Simplifier un sous-système complexe | Appels directs à 10 services différents |
| **Decorator** | Ajouter des comportements (logging, cache, auth) | Héritage profond pour ajouter des features |
| **Composite** | Structure arborescente (menus, fichiers) | Traitement différent nœuds/feuilles |

### Behavioral Patterns

| Pattern | Quand l'utiliser | Anti-pattern correspondant |
|---------|-----------------|--------------------------|
| **Strategy** | Algorithmes interchangeables | `if/else` pour choisir un algorithme |
| **Observer** | Notification de changements d'état | Polling ou couplage direct entre composants |
| **Command** | Opérations undo/redo, file d'attente | Actions directes sans historique |
| **State** | Objet avec comportement variant selon l'état | `if state == X` partout dans le code |

## Anti-patterns courants — Détection et correction

### God Object / God Class
**Symptômes :** Classe avec 500+ lignes, 20+ méthodes, responsabilités multiples
**Détection :** `repo_consistency_check` → fichier avec beaucoup de déclarations
**Correction :** Extraire en classes spécialisées (SRP), utiliser `code_refactoring` type `extract`

### Spaghetti Code
**Symptômes :** Flux de contrôle illisible, goto implicites, callbacks imbriqués
**Détection :** Complexité cyclomatique élevée, indentation profonde (>4 niveaux)
**Correction :** Refactorer en fonctions pures, utiliser async/await, `code_refactoring` type `simplify`

### Copy-Paste Programming
**Symptômes :** Blocs de code dupliqués avec variations mineures
**Détection :** `repo_consistency_check` check `duplication`
**Correction :** Extraire en fonctions/méthodes partagées, `code_refactoring` type `extract`

### Magic Numbers / Strings
**Symptômes :** Valeurs littérales sans explication (`if status == 3`, `timeout = 86400`)
**Détection :** Recherche de littéraux numériques dans les conditions
**Correction :** Extraire en constantes nommées (`STATUS_APPROVED = 3`, `TIMEOUT_24H = 86400`)

### Feature Envy
**Symptômes :** Méthode qui utilise plus de données d'une autre classe que de la sienne
**Détection :** Beaucoup d'appels `other_obj.field` dans une méthode
**Correction :** Déplacer la méthode vers la classe dont elle utilise les données

### Shotgun Surgery
**Symptômes :** Un changement nécessite de modifier 10+ fichiers
**Détection :** `impact_analysis` montre beaucoup de fichiers impactés pour un petit changement
**Correction :** Consolider la logique dispersée, appliquer SRP

### Primitive Obsession
**Symptômes :** Utiliser des strings/numbers pour tout (email comme string, money comme float)
**Détection :** Paramètres de fonction tous `str`/`int` sans validation
**Correction :** Créer des Value Objects (`Email`, `Money`, `PhoneNumber`)

### Long Parameter List
**Symptômes :** Fonction avec 5+ paramètres
**Détection :** Signatures de fonctions longues
**Correction :** Grouper en objets (Builder pattern, dataclass/interface)

## Principes SOLID — Checklist de review

### S — Single Responsibility Principle
- [ ] Chaque classe/module a une seule raison de changer
- [ ] Les fonctions font une seule chose
- [ ] Le nom de la classe/fonction décrit précisément ce qu'elle fait

### O — Open/Closed Principle
- [ ] Le code est extensible sans modification (plugins, strategies)
- [ ] Les nouveaux comportements s'ajoutent via composition, pas modification
- [ ] Les abstractions (interfaces) sont stables

### L — Liskov Substitution Principle
- [ ] Les sous-classes respectent le contrat de la classe parente
- [ ] Pas de `isinstance()` / `typeof` pour traiter différemment les sous-types
- [ ] Les préconditions ne sont pas renforcées dans les sous-classes

### I — Interface Segregation Principle
- [ ] Les interfaces sont petites et focalisées
- [ ] Les clients n'implémentent pas de méthodes qu'ils n'utilisent pas
- [ ] Préférer plusieurs petites interfaces à une grosse

### D — Dependency Inversion Principle
- [ ] Les modules de haut niveau ne dépendent pas des modules de bas niveau
- [ ] Les deux dépendent d'abstractions
- [ ] L'injection de dépendances est utilisée pour le découplage

## Patterns spécifiques par langage

### TypeScript / JavaScript
- Préférer `const` sur `let`, jamais `var`
- Utiliser le narrowing TypeScript plutôt que les casts `as`
- Préférer les unions discriminées aux enums
- Utiliser `readonly` pour les propriétés immutables
- Éviter `any`, utiliser `unknown` si le type est inconnu

### Python
- Utiliser les dataclasses ou Pydantic pour les DTOs
- Préférer les context managers (`with`) pour les ressources
- Utiliser les type hints partout (mypy strict)
- Préférer les compréhensions aux boucles + append
- Utiliser `pathlib.Path` au lieu de `os.path`

### React
- Préférer les composants fonctionnels avec hooks
- Extraire la logique dans des custom hooks (`useXxx`)
- Utiliser `React.memo()` seulement après profiling
- Éviter les re-renders inutiles (dépendances useEffect)
- Préférer la composition à l'héritage de composants
