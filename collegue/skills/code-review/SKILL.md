---
name: code-review
description: Guide structuré de code review utilisant repo_consistency_check, impact_analysis et code_refactoring. Utilise cette skill quand l'utilisateur demande une revue de code, un review de PR, ou veut améliorer la qualité d'un codebase.
---

# Code Review — Workflow Collègue

Tu réalises un code review structuré en 5 dimensions. Pour chaque dimension, utilise les outils MCP Collègue appropriés.

## Étape 1 : Analyse de cohérence automatique

Appelle `repo_consistency_check` sur les fichiers modifiés.

**Paramètres :**
- `files` : liste des fichiers modifiés `{path, content}`
- `checks` : tous (`['unused_imports', 'unused_vars', 'dead_code', 'duplication', 'signature_mismatch', 'unresolved_symbol']`)
- `mode` : `'deep'` pour un review complet

**Attention particulière :**
- **Imports inutilisés** → le développeur a peut-être oublié de nettoyer après refactoring
- **Variables non utilisées** → possible code incomplet ou copié-collé
- **Code mort** → fonctions jamais appelées, branches mortes
- **Duplication** → opportunité d'extraction de fonctions utilitaires
- **Symboles non résolus** → imports manquants, fautes de frappe

## Étape 2 : Analyse d'impact

Appelle `impact_analysis` pour comprendre la portée du changement.

**Paramètres :**
- `change_intent` : description du changement (extraire du titre de PR ou du commit message)
- `files` : tous les fichiers du contexte (pas seulement les modifiés)
- `analysis_depth` : `'deep'`

**Questions à poser :**
- Quels fichiers non modifiés pourraient être affectés ?
- Y a-t-il des breaking changes potentiels ?
- Les tests existants couvrent-ils les fichiers impactés ?
- Y a-t-il des risques de régression ?

## Étape 3 : Review des 5 dimensions

Pour chaque dimension, évalue le code sur une échelle de 1-5 :

### 3.1 Correctness (Exactitude)
- Le code fait-il ce qu'il est censé faire ?
- Les edge cases sont-ils gérés ?
- Les erreurs sont-elles correctement propagées ?
- Les types sont-ils corrects (TypeScript strict, Python type hints) ?

### 3.2 Security (Sécurité)
- Y a-t-il des inputs non validés ?
- Les données sensibles sont-elles protégées ?
- Les requêtes SQL sont-elles paramétrées ?
- Les permissions sont-elles vérifiées ?
- Appeler `secret_scan` si des fichiers de config sont modifiés

### 3.3 Performance
- Y a-t-il des boucles O(n²) évitables ?
- Les requêtes DB sont-elles optimisées (N+1, SELECT *) ?
- Le caching est-il utilisé quand approprié ?
- Les opérations I/O sont-elles asynchrones quand possible ?

### 3.4 Maintainability (Maintenabilité)
- Le code est-il lisible et bien nommé ?
- Les fonctions font-elles une seule chose (SRP) ?
- Les abstractions sont-elles au bon niveau ?
- La documentation est-elle suffisante ?

### 3.5 Testing
- Les nouveaux chemins de code sont-ils testés ?
- Les edge cases ont-ils des tests ?
- Les mocks sont-ils appropriés (pas trop, pas trop peu) ?
- Appeler `test_generation` si la couverture semble insuffisante

## Étape 4 : Propositions de refactoring

Si des problèmes de maintenabilité sont détectés, appelle `code_refactoring` :

**Types de refactoring par situation :**
| Problème détecté | Type de refactoring |
|-----------------|-------------------|
| Fonction trop longue (>50 lignes) | `extract` |
| Nommage confus | `rename` |
| Logique trop complexe (cyclomatic > 10) | `simplify` |
| Boucles inefficaces | `optimize` |
| Code legacy / patterns obsolètes | `modernize` |
| Imports inutilisés, code mort | `clean` |

## Étape 5 : Rapport de review

Structure ton feedback ainsi :

```markdown
# Code Review — [PR/fichier]

## Score global : X/5

| Dimension | Score | Détail |
|-----------|-------|--------|
| Correctness | X/5 | ... |
| Security | X/5 | ... |
| Performance | X/5 | ... |
| Maintainability | X/5 | ... |
| Testing | X/5 | ... |

## Blockers (à corriger avant merge)
- [ ] [problème critique]

## Suggestions (améliorations recommandées)
- [ ] [suggestion]

## Nits (cosmétique, non-bloquant)
- [ ] [détail mineur]

## Points positifs
- [ce qui est bien fait]
```

## Patterns et anti-patterns

Pour le catalogue complet des design patterns et anti-patterns, consulte [patterns.md](patterns.md).
