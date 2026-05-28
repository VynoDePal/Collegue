# Guide Utilisateur — Collègue MCP

> Guide complet pour installer, configurer et utiliser Collègue MCP dans vos projets de développement.

## Table des matières

1. [Introduction](#introduction)
2. [Installation](#installation)
3. [Configuration](#configuration)
4. [Premiers pas](#premiers-pas)
5. [Utilisation quotidienne](#utilisation-quotidienne)
6. [Bonnes pratiques](#bonnes-pratiques)
7. [Dépannage](#dépannage)

---

## Introduction

Collègue MCP est un **collectif d'experts IA spécialisés** qui fonctionne comme un serveur MCP (Model Context Protocol). Il s'intègre dans votre IDE pour fournir :

- **Analyse de code** : qualité, architecture, performance, sécurité
- **Génération** : refactoring, tests unitaires, documentation
- **Planification** : analyse d'impact, orchestration multi-étapes
- **Monitoring** : détection proactive de problèmes, métriques

Chaque expert utilise un LLM en backend et peut **déléguer automatiquement** à d'autres experts quand il détecte des problèmes hors de son domaine.

---

## Installation

### Option 1 : Via NPM (recommandé — aucune installation locale)

```json
{
  "mcpServers": {
    "collegue": {
      "command": "npx",
      "args": ["-y", "@collegue/mcp@latest"]
    }
  }
}
```

### Option 2 : Docker Compose (serveur persistant)

```bash
git clone https://github.com/VynoDePal/Collegue.git
cd Collegue
cp .env.example .env
# Renseigner LLM_API_KEY dans .env
docker compose up -d
```

Puis configurer le client pour `http://localhost:4121/mcp/`.

### Option 3 : Docker Run (à la volée)

```json
{
  "mcpServers": {
    "collegue": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "MCP_TRANSPORT=stdio",
        "-e", "LLM_API_KEY=votre_clé_gemini",
        "collegue-mcp"
      ]
    }
  }
}
```

### Option 4 : Développement local (Python)

```bash
git clone https://github.com/VynoDePal/Collegue.git
cd Collegue
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m collegue.app
```

---

## Configuration

### Variable d'environnement requise

| Variable | Description | Exemple |
|----------|-------------|---------|
| `LLM_API_KEY` | Clé API Google Gemini | `AIzaSy...` |

### Variables optionnelles (intégrations)

| Variable | Description | Outil activé |
|----------|-------------|--------------|
| `POSTGRES_URL` | URI PostgreSQL | `postgres_db` |
| `GITHUB_TOKEN` | Token GitHub (scopes: repo, read:org) | `github_ops` |
| `SENTRY_AUTH_TOKEN` | Token Sentry | `sentry_monitor` |
| `SENTRY_ORG` | Organisation Sentry | `sentry_monitor` |
| `KUBECONFIG` | Chemin kubeconfig | `kubernetes_ops` |

### Configuration par client MCP

#### Claude Code (CLI)

```bash
claude mcp add --transport http collegue-local http://localhost:4121/mcp/
```

#### Claude Desktop

```json
{
  "mcpServers": {
    "collegue": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:4121/mcp/"]
    }
  }
}
```

#### Windsurf / Cursor / Antigravity

```json
{
  "mcpServers": {
    "collegue": {
      "serverUrl": "http://localhost:4121/mcp/"
    }
  }
}
```

---

## Premiers pas

### 1. Vérifier la connexion

Une fois configuré, demandez à votre assistant IA :

> "Utilise l'outil `expert_dashboard` pour me donner un résumé du projet"

Si la connexion fonctionne, vous recevrez un dashboard avec les scores de qualité.

### 2. Votre première analyse

Fournissez un fichier de code à analyser :

> "Fais une code review de ce fichier : [collez le code ou le chemin]"

L'expert `code_review` analysera le code et retournera :
- Un score de qualité (0.0 à 1.0)
- Des findings détaillés (catégorie, sévérité, suggestions)
- Les points forts du code
- Des recommandations d'amélioration

### 3. Explorer les experts disponibles

| Expert | Commande type |
|--------|---------------|
| Code Review | "Analyse la qualité de ce code" |
| Architecture | "Analyse l'architecture de ces fichiers" |
| Performance | "Détecte les problèmes de performance" |
| Refactoring | "Refactorise ce code pour le simplifier" |
| Test Generation | "Génère des tests unitaires pour cette fonction" |
| Documentation | "Documente cette classe" |
| IaC Scan | "Analyse la sécurité de ce Dockerfile" |
| Impact Analysis | "Quel serait l'impact de renommer cette classe ?" |
| Consistency Check | "Vérifie les incohérences dans ce code" |
| Orchestrator | "Fais un audit complet de ce module" |

---

## Utilisation quotidienne

### Workflow recommandé

```
1. Avant de coder → impact_analysis (planification)
2. Pendant le dev → code_review + performance_analysis
3. Après le code  → test_generation + code_documentation
4. Avant le merge → repo_consistency_check + iac_guardrails_scan
5. Vue globale   → expert_dashboard
```

### Mode Fast vs Deep

Chaque expert supporte deux modes :

| Mode | Description | Quand l'utiliser |
|------|-------------|-----------------|
| `fast` | Heuristiques statiques uniquement (regex, AST) | Feedback rapide, CI, gros volumes |
| `deep` | Heuristiques + analyse LLM approfondie | Analyse détaillée, revues importantes |

Spécifier le mode :
```
"analysis_depth": "deep"
```

### Délégation automatique

Les experts se délèguent mutuellement quand c'est pertinent :

| Déclencheur | Condition | Expert déclenché |
|-------------|-----------|-----------------|
| `code_review` | quality_score < 0.5 | `code_refactoring` |
| `repo_consistency_check` | refactoring_score > 0.6 | `code_refactoring` |
| `code_refactoring` | après refactoring | `code_review` + `test_generation` + `code_documentation` |
| `performance_analysis` | score < 0.5 | `code_refactoring` (optimize) |
| `iac_guardrails_scan` | security_score < 0.5 | auto-remediation |
| `architecture_analysis` | changements détectés | `impact_analysis` |

### Mémoire persistante

Les experts mémorisent leurs résultats dans `.collegue/memory/`. Au prochain appel, ils rappellent le contexte :

- Patterns détectés précédemment
- Issues corrigées
- Scores historiques
- Recommandations passées

Cela leur permet de :
- Ne pas re-signaler des issues déjà corrigées
- Suivre l'évolution de la qualité dans le temps
- Fournir des recommandations contextuelles

### Moniteur proactif

Le `ProactiveMonitor` détecte les fichiers modifiés et suggère quels experts déclencher :

| Extension modifiée | Experts suggérés |
|-------------------|------------------|
| `.py`, `.js`, `.ts` | code_review, performance_analysis |
| `Dockerfile`, `*.tf` | iac_guardrails_scan |
| `requirements.txt`, `package.json` | architecture_analysis |
| `*.test.*`, `*_test.*` | (aucun — c'est déjà des tests) |

---

## Bonnes pratiques

### 1. Fournir du contexte

Plus vous donnez de contexte, meilleurs sont les résultats :

```
✅ "Refactorise cette fonction pour extraire la logique de validation. 
    Le projet utilise FastAPI et Pydantic."

❌ "Refactorise ça."
```

### 2. Utiliser le bon expert

| Besoin | Expert à utiliser |
|--------|------------------|
| "Mon code est-il propre ?" | `code_review` |
| "Est-ce que c'est lent ?" | `performance_analysis` |
| "Quel impact si je change X ?" | `impact_analysis` |
| "Génère des tests" | `test_generation` |
| "Audit global du projet" | `smart_orchestrator` |

### 3. Profiter de la délégation

Plutôt que d'appeler manuellement 5 experts, utilisez le `smart_orchestrator` avec une requête complexe :

> "Fais un audit complet de ce module : qualité, performance, sécurité, et propose des refactorings."

L'orchestrateur planifiera et exécutera les experts pertinents automatiquement.

### 4. Exploiter le mode deep

Pour les revues importantes (PRs critiques, nouveau module) :

> "Fais une code review approfondie (analysis_depth: deep) de ce fichier"

Le mode deep ajoute des insights sémantiques que les heuristiques seules ne détectent pas.

### 5. Consulter le dashboard régulièrement

> "Montre-moi le dashboard expert du projet"

Le dashboard agrège les scores de tous les experts et montre :
- Score global de santé du projet
- Scores par domaine (qualité, architecture, performance, sécurité)
- Recommandations priorisées
- Activité de délégation récente

---

## Dépannage

### Problèmes courants

| Symptôme | Cause probable | Solution |
|----------|---------------|----------|
| "Tool not found" | Serveur MCP non connecté | Vérifier la config mcpServers, redémarrer l'IDE |
| Timeout sur analyse deep | Quota LLM atteint ou fichier trop gros | Passer en mode `fast`, ou découper le fichier |
| Scores tous à 0 | Pas de `LLM_API_KEY` configuré | Ajouter la clé API Gemini |
| "Non supporté" sur un langage | Langage non reconnu | Vérifier `supported_languages` de chaque expert |
| Mémoire non persistée | Permission d'écriture sur `.collegue/` | Vérifier les droits du répertoire projet |
| Délégation ne se déclenche pas | Seuils non atteints | Les seuils sont fixes (ex: quality < 0.5) — normal si le code est bon |

### Langages supportés

| Expert | Langages |
|--------|----------|
| code_review | Python, JavaScript, TypeScript, PHP |
| architecture_analysis | Python, JavaScript, TypeScript, Java, C#, PHP, Go, Rust |
| performance_analysis | Python, JavaScript, TypeScript |
| code_refactoring | Python, JavaScript, TypeScript, Java, C#, PHP, Go, Rust |
| test_generation | Python, JavaScript, TypeScript, PHP |
| code_documentation | Python, JavaScript, TypeScript, PHP |
| iac_guardrails_scan | Terraform, Kubernetes YAML, Dockerfile |
| impact_analysis | Python, JavaScript, TypeScript, Java, C#, PHP, Go, Rust |
| repo_consistency_check | Python, JavaScript, TypeScript, PHP |

### Logs et debugging

En mode Docker Compose :
```bash
docker compose logs -f collegue-app
```

En mode développement local :
```bash
python -m collegue.app  # Les logs s'affichent dans stdout
```

### Limites connues

- **Rate limiting LLM** : Gemini Free Tier = 20 requêtes/jour. En production, utilisez un tier payant.
- **Taille des fichiers** : Les fichiers > 500 lignes sont analysés mais le mode deep peut être lent.
- **Langages limités** : Les heuristiques statiques ne couvrent que Python/JS/TS/PHP. Le mode deep (LLM) peut analyser n'importe quel langage mais avec moins de précision.
- **Mémoire locale** : La mémoire est stockée dans `.collegue/memory/` au niveau du projet. Pas de synchronisation entre machines.
