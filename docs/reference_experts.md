# Référence des Experts IA — Collègue MCP

> Documentation complète de chaque expert : paramètres, sorties, cas d'usage et exemples.

## Table des matières

1. [code_review](#code_review)
2. [architecture_analysis](#architecture_analysis)
3. [performance_analysis](#performance_analysis)
4. [code_refactoring](#code_refactoring)
5. [test_generation](#test_generation)
6. [code_documentation](#code_documentation)
7. [iac_guardrails_scan](#iac_guardrails_scan)
8. [impact_analysis](#impact_analysis)
9. [repo_consistency_check](#repo_consistency_check)
10. [smart_orchestrator](#smart_orchestrator)
11. [expert_dashboard](#expert_dashboard)
12. [Outils statiques](#outils-statiques)

---

## code_review

### Description

Expert en revue de code qualité. Analyse la lisibilité, la complexité, la sécurité, le respect des principes DRY/SOLID, et la gestion d'erreurs.

### Paramètres

| Paramètre | Type | Requis | Description |
|-----------|------|--------|-------------|
| `code` | string | ✓ | Code source à analyser |
| `language` | string | ✓ | Langage (`python`, `javascript`, `typescript`, `php`) |
| `file_path` | string | | Chemin du fichier (contexte) |
| `analysis_depth` | string | | `fast` (heuristiques) ou `deep` (+ LLM) |
| `severity_threshold` | string | | Sévérité minimale : `info`, `warning`, `error`, `critical` |
| `context` | string | | Contexte additionnel (PR, ticket) |

### Sortie

```json
{
  "quality_score": 0.75,
  "findings": [
    {
      "category": "security",
      "severity": "error",
      "line": 42,
      "title": "Hardcoded credential detected",
      "description": "A password is hardcoded in the source.",
      "suggestion": "Use environment variables or a secrets manager."
    }
  ],
  "category_scores": {
    "naming": 0.9,
    "complexity": 0.6,
    "security": 0.3,
    "dry": 0.8,
    "solid": 0.7,
    "error_handling": 0.6
  },
  "strengths": ["Good function naming", "Clear error messages"],
  "recommendations": ["Extract validation logic into a separate function"]
}
```

### Délégation

- Si `quality_score < 0.5` → déclenche `code_refactoring`
- Après refactoring → re-review automatique

### Exemple

```
"Fais une code review approfondie de ce code Python.
Concentre-toi sur la sécurité et la complexité."
```

---

## architecture_analysis

### Description

Expert en analyse architecturale. Détecte les patterns, dépendances, cycles, couplage/cohésion, et évalue la dette technique.

### Paramètres

| Paramètre | Type | Requis | Description |
|-----------|------|--------|-------------|
| `files` | array | ✓ | `[{"path": "...", "content": "..."}]` |
| `language` | string | | Langage (`python`, `javascript`, etc.) |
| `analysis_depth` | string | | `fast` ou `deep` |
| `focus_areas` | array | | Domaines à analyser : `dependencies`, `patterns`, `coupling`, `cohesion` |

### Sortie

```json
{
  "architecture_score": 0.82,
  "debt_score": 0.15,
  "detected_patterns": ["Repository", "Service Layer", "Singleton"],
  "issues": [
    {
      "category": "coupling",
      "severity": "warning",
      "description": "Module X dépend de 8 autres modules",
      "affected_modules": ["auth", "users", "orders"]
    }
  ],
  "recommendations": ["Introduce a facade pattern to reduce coupling"]
}
```

### Délégation

- Si changements architecturaux détectés → déclenche `impact_analysis`
- Si dette technique élevée → déclenche `code_refactoring`

---

## performance_analysis

### Description

Expert en analyse de performance. Détecte les complexités algorithmiques problématiques, I/O bloquants, concaténations en boucle, et identifie les hotspots.

### Paramètres

| Paramètre | Type | Requis | Description |
|-----------|------|--------|-------------|
| `code` | string | ✓ | Code source à analyser |
| `language` | string | ✓ | Langage (`python`, `javascript`, `typescript`) |
| `file_path` | string | | Chemin du fichier |
| `analysis_depth` | string | | `fast` ou `deep` |

### Sortie

```json
{
  "performance_score": 0.64,
  "issues": [
    {
      "category": "algorithmic_complexity",
      "severity": "error",
      "line": 15,
      "description": "Nested loop creates O(n²) complexity",
      "estimated_complexity": "O(n²)",
      "suggestion": "Use a dictionary for O(1) lookups"
    }
  ],
  "hotspots": [
    {"line": 15, "reason": "Nested iteration", "impact": "high"}
  ],
  "optimizations": ["Use set() for membership testing", "Cache computed values"]
}
```

### Patterns détectés

| Pattern | Description |
|---------|-------------|
| `nested_loops` | Boucles imbriquées O(n²+) |
| `string_concat_loop` | Concaténation de strings en boucle |
| `blocking_io_in_loop` | I/O synchrone dans une boucle |
| `readlines_large_file` | `readlines()` sur gros fichiers |
| `global_import_in_func` | Import dans une fonction (latence) |
| `repeated_regex_compile` | `re.compile()` non mis en cache |

### Délégation

- Si `performance_score < 0.5` → déclenche `code_refactoring` (type: optimize)

---

## code_refactoring

### Description

Expert en restructuration de code. Applique des transformations (renommage, extraction, simplification, optimisation) et valide la syntaxe AST du résultat.

### Paramètres

| Paramètre | Type | Requis | Description |
|-----------|------|--------|-------------|
| `code` | string | ✓ | Code source brut à modifier |
| `language` | string | ✓ | Langage du code |
| `refactoring_type` | string | ✓ | Type : `rename`, `extract`, `simplify`, `optimize`, `clean`, `modernize` |
| `file_path` | string | | Chemin du fichier (contexte) |
| `parameters` | object | | Config spécifique (ex: `{"naming_convention": "snake_case"}`) |

### Types de refactoring

| Type | Description | Exemple |
|------|-------------|---------|
| `rename` | Renommer variables/fonctions | `x` → `user_count` |
| `extract` | Extraire en fonctions | Code dupliqué → fonction réutilisable |
| `simplify` | Simplifier la logique | Ifs imbriqués → early returns |
| `optimize` | Optimiser les performances | Liste → générateur |
| `clean` | Supprimer le mort | Imports et variables inutilisés |
| `modernize` | Syntaxes modernes | `format()` → f-string |

### Sortie

```json
{
  "refactored_code": "def calculate_total(items):\n    return sum(item.price for item in items)",
  "changes_made": ["Replaced loop with generator expression", "Removed unused variable"],
  "metrics_before": {"complexity_score": 8, "lines": 15},
  "metrics_after": {"complexity_score": 3, "lines": 5}
}
```

### Boucle agentique

Le refactoring itère jusqu'à 3 fois :
1. Génère le code refactoré
2. Valide la syntaxe AST
3. Compare les métriques avant/après
4. Si la qualité est insuffisante → re-génère avec feedback

### Délégation (après refactoring)

- → `code_review` (vérifier la qualité)
- → `test_generation` (régénérer les tests)
- → `code_documentation` (mettre à jour la doc)

---

## test_generation

### Description

Expert en génération de tests unitaires. Produit des tests exécutables et validés pour le framework cible.

### Paramètres

| Paramètre | Type | Requis | Description |
|-----------|------|--------|-------------|
| `code` | string | ✓ | Code source à tester |
| `language` | string | ✓ | Langage (`python`, `javascript`, `typescript`, `php`) |
| `test_framework` | string | | Framework : `pytest`, `jest`, `mocha`, `phpunit` |
| `include_mocks` | boolean | | Générer des mocks pour les dépendances externes |
| `coverage_target` | float | | Couverture visée (0.0-1.0, défaut: 0.8) |
| `file_path` | string | | Chemin du fichier source |

### Sortie

```json
{
  "test_code": "import pytest\n\ndef test_calculate_total_empty():\n    assert calculate_total([]) == 0\n...",
  "test_count": 5,
  "coverage_estimate": 0.85,
  "frameworks_used": ["pytest"],
  "notes": "Mocked database dependency for isolation"
}
```

### Boucle agentique

1. Génère les tests
2. Parse et valide la syntaxe
3. Vérifie que les imports sont corrects
4. Si échec → régénère avec feedback spécifique

---

## code_documentation

### Description

Expert en documentation technique. Génère des docstrings, commentaires et documentation de module.

### Paramètres

| Paramètre | Type | Requis | Description |
|-----------|------|--------|-------------|
| `code` | string | ✓ | Code source à documenter |
| `language` | string | ✓ | Langage |
| `doc_style` | string | | Style : `google`, `numpy`, `sphinx`, `jsdoc` |
| `coverage_target` | float | | Couverture visée (0.0-1.0) |
| `include_examples` | boolean | | Inclure des exemples d'utilisation |

### Sortie

```json
{
  "documented_code": "def calculate_total(items: list[Item]) -> float:\n    \"\"\"Calculate the total price of items.\n    ...",
  "documentation_coverage": 1.0,
  "documented_elements": ["calculate_total", "Item", "PriceCalculator"],
  "notes": "Added type hints and usage examples"
}
```

---

## iac_guardrails_scan

### Description

Expert en sécurité Infrastructure as Code. Scanne Terraform, Kubernetes YAML et Dockerfiles pour détecter les vulnérabilités.

### Paramètres

| Paramètre | Type | Requis | Description |
|-----------|------|--------|-------------|
| `files` | array | ✓ | `[{"path": "Dockerfile", "content": "..."}]` |
| `policy_profile` | string | | Profil : `standard`, `strict`, `permissive` |
| `analysis_depth` | string | | `fast` ou `deep` |
| `auto_remediate` | boolean | | Générer des patches de correction |

### Sortie

```json
{
  "security_score": 0.6,
  "compliance_score": 0.75,
  "findings": [
    {
      "rule_id": "DOCKER-001",
      "severity": "high",
      "title": "Container running as root",
      "path": "Dockerfile",
      "line": 1,
      "remediation": "Add USER directive with non-root user"
    }
  ],
  "risk_level": "medium",
  "remediation_patches": ["..."]
}
```

### Règles détectées

| Catégorie | Exemples |
|-----------|----------|
| Privilèges | Root container, capabilities non droppées |
| Réseau | Ports exposés inutilement, bind 0.0.0.0 |
| Secrets | Variables d'env avec passwords, clés hardcodées |
| Images | latest tag, images non-signées |
| Conformité | CIS benchmarks, SOC2 |

### Délégation

- Si `security_score < 0.5` et `auto_remediate=true` → génère des patches

---

## impact_analysis

### Description

Expert en analyse prédictive d'impact. Évalue les risques d'un changement avant implémentation.

### Paramètres

| Paramètre | Type | Requis | Description |
|-----------|------|--------|-------------|
| `change_intent` | string | ✓ | Description du changement prévu |
| `files` | array | ✓ | Fichiers impactés initiaux `[{"path": "...", "content": "..."}]` |
| `diff` | string | | Diff unifié si disponible |
| `entry_points` | array | | Points d'entrée du projet |
| `confidence_mode` | string | | `balanced`, `conservative`, `aggressive` |
| `analysis_depth` | string | | `fast` ou `deep` |

### Sortie

```json
{
  "impacted_files": [
    {"path": "auth/middleware.py", "impact_type": "direct", "confidence": 0.9},
    {"path": "tests/test_auth.py", "impact_type": "indirect", "confidence": 0.7}
  ],
  "risk_notes": [
    {"category": "breaking_change", "description": "API signature change", "severity": "high"}
  ],
  "search_queries": ["usages of AuthMiddleware", "imports from auth.middleware"],
  "test_recommendations": ["test_auth.py", "test_integration.py"],
  "semantic_summary": "Le changement affecte 3 modules et peut casser 2 APIs publiques."
}
```

---

## repo_consistency_check

### Description

Expert en détection d'incohérences : imports inutilisés, variables mortes, code dupliqué, symboles non résolus.

### Paramètres

| Paramètre | Type | Requis | Description |
|-----------|------|--------|-------------|
| `files` | array | ✓ | `[{"path": "...", "content": "..."}]` |
| `language` | string | | `python`, `javascript`, `typescript`, `php`, `auto` |
| `checks` | array | | Checks à lancer : `unused_imports`, `unused_vars`, `dead_code`, `duplication`, `unresolved_symbol` |
| `analysis_depth` | string | | `fast` ou `deep` |
| `auto_chain` | boolean | | Si true, déclenche `code_refactoring` automatiquement |

### Sortie

```json
{
  "issues": [
    {
      "check": "unused_imports",
      "severity": "warning",
      "file": "main.py",
      "line": 3,
      "symbol": "os",
      "message": "Module 'os' imported but never used"
    }
  ],
  "refactoring_score": 0.65,
  "refactoring_priority": "recommended",
  "suggested_actions": ["Remove 3 unused imports", "Delete dead function 'old_handler'"]
}
```

### Délégation

- Si `refactoring_score > 0.6` et `auto_chain=true` → déclenche `code_refactoring`

---

## smart_orchestrator

### Description

Méta-expert qui reçoit une requête complexe, planifie les experts nécessaires, les exécute en séquence, et synthétise la réponse.

### Paramètres

| Paramètre | Type | Requis | Description |
|-----------|------|--------|-------------|
| `task` | string | ✓ | Description de la tâche à accomplir |
| `files` | array | | Fichiers de contexte |
| `constraints` | object | | Contraintes (max_tools, timeout, etc.) |

### Fonctionnement

```
1. Reçoit "Fais un audit complet du module auth"
2. Planifie : [code_review, architecture_analysis, performance_analysis]
3. Exécute chaque expert en séquence
4. Synthétise les résultats en une réponse unifiée
```

### Exemple

```
"Planifie et exécute un audit complet de ce module.
Je veux connaître la qualité, les problèmes de performance,
et les risques architecturaux. Propose un plan d'action."
```

---

## expert_dashboard

### Description

Tableau de bord qui agrège les résultats de tous les experts et fournit une vue synthétique de la santé du projet.

### Paramètres

Aucun paramètre requis — utilise les données de la mémoire et du monitoring.

### Sortie

```json
{
  "project_health": 0.72,
  "expert_statuses": {
    "code_review": {"score": 0.75, "last_run": "2024-01-15"},
    "architecture": {"score": 0.82, "last_run": "2024-01-14"},
    "performance": {"score": 0.64, "last_run": "2024-01-15"},
    "security": {"score": 0.55, "last_run": "2024-01-13"}
  },
  "recommendations": [
    {"priority": "high", "action": "Address 3 security findings in IaC"},
    {"priority": "medium", "action": "Refactor performance hotspots in module X"}
  ],
  "metrics": {
    "total_executions": 45,
    "avg_latency_ms": 2300,
    "total_cost": 0.12,
    "error_rate": 0.02
  }
}
```

---

## Outils statiques

### secret_scan

Détecte les secrets exposés dans le code (clés API, tokens, mots de passe).

| Pattern détecté | Exemple |
|----------------|---------|
| AWS Access Key | `AKIA...` |
| GitHub Token | `ghp_...`, `gho_...` |
| Google API Key | `AIzaSy...` |
| JWT | `eyJ...` |
| Generic password | `password = "..."` |
| Private key | `-----BEGIN RSA PRIVATE KEY-----` |

### dependency_guard

Audit de sécurité des dépendances (supply chain) :
- Détection de typosquatting
- Vulnérabilités connues (CVE)
- Dépendances abandonnées
- Licences incompatibles

---

## Matrice des langages supportés

| Expert | Python | JS | TS | PHP | Java | C# | Go | Rust | Terraform | Docker | K8s |
|--------|--------|----|----|-----|------|----|----|------|-----------|--------|-----|
| code_review | ✓ | ✓ | ✓ | ✓ | | | | | | | |
| architecture | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | | | |
| performance | ✓ | ✓ | ✓ | | | | | | | | |
| refactoring | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | | | |
| test_generation | ✓ | ✓ | ✓ | ✓ | | | | | | | |
| documentation | ✓ | ✓ | ✓ | ✓ | | | | | | | |
| iac_scan | | | | | | | | | ✓ | ✓ | ✓ |
| impact | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | | | |
| consistency | ✓ | ✓ | ✓ | ✓ | | | | | | | |
