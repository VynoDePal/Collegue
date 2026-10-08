---
name: collegue-toolkit
description: Guide complet des 12 outils MCP Collègue. Utilise cette skill pour savoir quel outil appeler selon la tâche (analyse de code, sécurité, DevOps, debugging). Invoque automatiquement quand l'utilisateur demande de l'aide sur un projet.
---

# Collègue MCP Toolkit — Guide des outils

Tu as accès aux 12 outils du MCP **Collègue** via le protocole MCP. Ce guide t'explique **quand** et **comment** les utiliser pour chaque type de tâche.

## Outils par catégorie

### Analyse de code

| Outil | Quand l'utiliser |
|-------|-----------------|
| `repo_consistency_check` | Détecter imports inutilisés, variables mortes, code dupliqué, symboles non résolus |
| `impact_analysis` | Avant un changement : évaluer les fichiers impactés, risques, tests à lancer |
| `code_refactoring` | Refactoriser du code : rename, extract, simplify, optimize, clean, modernize |
| `code_documentation` | Générer de la documentation (Markdown, RST, HTML, docstring) |
| `test_generation` | Générer des tests unitaires avec mocks optionnels |

### Sécurité

| Outil | Quand l'utiliser |
|-------|-----------------|
| `secret_scan` | Scanner du code pour détecter secrets exposés (clés API, tokens, mots de passe, entropie) |
| `dependency_guard` | Valider les dépendances : existence, versions, vulnérabilités CVE, typosquatting |
| `iac_guardrails_scan` | Scanner Terraform/K8s/Dockerfile pour configurations dangereuses |

### DevOps & Infrastructure

| Outil | Quand l'utiliser |
|-------|-----------------|
| `kubernetes_ops` | Inspecter un cluster K8s : pods, logs, déploiements, services, événements |
| `github_ops` | Interagir avec GitHub : repos, PRs, issues, branches, search code |
| `postgres_db` | Inspecter une base PostgreSQL : schéma, tables, requêtes SELECT |

### Monitoring

| Outil | Quand l'utiliser |
|-------|-----------------|
| `sentry_monitor` | Récupérer erreurs, stacktraces, statistiques depuis Sentry |

## Workflows recommandés

### Audit de sécurité complet
1. `secret_scan` — scanner tous les fichiers pour secrets exposés
2. `dependency_guard` — vérifier les vulnérabilités des dépendances
3. `iac_guardrails_scan` — valider l'infrastructure as code
4. Consolider les résultats en un rapport unifié avec scoring de risque

### Code review approfondi
1. `repo_consistency_check` — détecter les problèmes de cohérence
2. `impact_analysis` — évaluer l'impact du changement
3. `code_refactoring` (si nécessaire) — proposer des améliorations
4. `test_generation` — vérifier la couverture de tests

### Onboarding sur un projet
1. `github_ops` (list_repos, repo_branches) — comprendre la structure
2. `postgres_db` (list_tables, describe_table) — comprendre le schéma de données
3. `repo_consistency_check` — identifier la dette technique
4. `code_documentation` — générer la documentation manquante

### Debugging production
1. `sentry_monitor` (list_issues, issue_events) — identifier les erreurs récentes
2. `kubernetes_ops` (pod_logs, list_events) — vérifier les logs et événements
3. `impact_analysis` — trouver la cause probable dans le code
4. `github_ops` (repo_commits) — identifier le commit responsable

### Préparation de déploiement
1. `dependency_guard` — vérifier qu'aucune vulnérabilité n'est présente
2. `secret_scan` — confirmer qu'aucun secret n'est exposé
3. `iac_guardrails_scan` — valider la configuration d'infrastructure
4. `test_generation` — s'assurer que les tests couvrent les changements

## Combinaisons à éviter

- Ne pas appeler `code_refactoring` sans `repo_consistency_check` d'abord — il faut connaître les problèmes avant de refactoriser
- Ne pas appeler `impact_analysis` sans fournir le `change_intent` — l'outil a besoin de savoir quel changement est prévu
- Ne pas appeler `kubernetes_ops` sans vérifier que le cluster est accessible

## Paramètres clés

### `repo_consistency_check`
- `files` : liste de `{path, content}` — obligatoire
- `checks` : `['unused_imports', 'unused_vars', 'dead_code', 'duplication', 'signature_mismatch', 'unresolved_symbol']`
- `language` : `'python'`, `'typescript'`, `'javascript'`, `'auto'`
- `mode` : `'fast'` (heuristiques) ou `'deep'` (analyse complète)

### `secret_scan`
- `files` : liste de `{path, content}` — recommandé pour scan batch
- `content` : contenu d'un seul fichier
- `severity_threshold` : `'low'`, `'medium'`, `'high'`, `'critical'`

### `dependency_guard`
- `content` : contenu du fichier de dépendances (package-lock.json, requirements.txt, pyproject.toml)
- `language` : `'python'` ou `'typescript'`/`'javascript'`
- `check_vulnerabilities` : `true` pour scanner les CVE via OSV

### `impact_analysis`
- `change_intent` : description du changement en langage naturel — obligatoire
- `files` : liste de `{path, content}` — obligatoire
- `analysis_depth` : `'fast'` (~10ms) ou `'deep'` (+2-3s avec IA)

### `iac_guardrails_scan`
- `files` : liste de `{path, content}` — obligatoire
- `policy_profile` : `'baseline'` (recommandé) ou `'strict'`
- `analysis_depth` : `'fast'` ou `'deep'` (scoring LLM)
