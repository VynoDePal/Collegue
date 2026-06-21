# Guide d'Intégration — Collègue MCP dans un Projet

> Comment intégrer Collègue MCP dans un projet existant ou nouveau, et en tirer le maximum.

## Table des matières

1. [Prérequis : lancer le serveur Docker](#prérequis--lancer-le-serveur-docker)
2. [Intégration avec Claude Desktop](#intégration-avec-claude-desktop)
3. [Intégration avec Cursor](#intégration-avec-cursor)
4. [Intégration avec Windsurf](#intégration-avec-windsurf)
5. [Intégration avec Claude Code (CLI)](#intégration-avec-claude-code-cli)
6. [Intégration dans un nouveau projet](#intégration-dans-un-nouveau-projet)
7. [Intégration dans un projet existant](#intégration-dans-un-projet-existant)
8. [Intégration CI/CD](#intégration-cicd)
9. [Cas d'usage avancés](#cas-dusage-avancés)

---

## Prérequis : lancer le serveur Docker

Toutes les intégrations ci-dessous supposent que le serveur Collègue tourne en local via Docker :

```bash
git clone https://github.com/VynoDePal/Collegue.git
cd Collegue
cp .env.example .env   # renseigner LLM_API_KEY (Gemini)
docker compose --profile local up -d
```

Le serveur est accessible sur `http://localhost:4121/mcp/` uniquement quand le profil
`local` ou `dev` est activé. Ce profil publie le port MCP directement sur l'hôte pour
les IDE locaux ; il ne doit pas être utilisé en production.

> **Mode stdio** (alternative) : vous pouvez aussi laisser votre IDE spawner un container à la volée, voir le README pour la config `docker run`.

### Note de sécurité production

`OAUTH_ENABLED=false` est uniquement acceptable pour un environnement local/dev non
exposé. En production, gardez le port MCP `4121` non publié directement par Docker
Compose et passez par un reverse proxy ou une couche d'authentification. Utilisez un
fichier d'environnement de production (voir `.env.production.example`) avec au
minimum :

```dotenv
OAUTH_ENABLED=true
OAUTH_JWKS_URI=https://auth.example.com/realms/collegue/protocol/openid-connect/certs
OAUTH_ISSUER=https://auth.example.com/realms/collegue
OAUTH_AUDIENCE=collegue-mcp
```

Démarrage production type :

```bash
docker compose --env-file .env.production up -d
```

N'ajoutez `--profile local` ou `--profile dev` que sur votre poste de développement.

---

## Intégration avec Claude Desktop

### Configuration

Fichier : `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) ou `%APPDATA%\Claude\claude_desktop_config.json` (Windows)

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

> **Note** : Claude Desktop ne supporte pas directement le transport HTTP. Le bridge `mcp-remote` convertit stdio→HTTP.

### Vérification

1. Redémarrer Claude Desktop
2. Cliquer sur l'icône MCP (🔌) dans le champ de saisie
3. Vérifier que "collegue" apparaît avec ses outils

### Exemples d'utilisation

```
Toi: Fais une revue de code de ce fichier Python :
[coller le code]

Claude: [utilise code_review] Le score de qualité est de 0.72/1.0...
```

---

## Intégration avec Cursor

### Configuration

Fichier : `.cursor/mcp.json` à la racine du projet ou `~/.cursor/mcp.json` globalement.

```json
{
  "mcpServers": {
    "collegue": {
      "serverUrl": "http://localhost:4121/mcp/"
    }
  }
}
```

### Activation

1. Ouvrir les Settings de Cursor (Ctrl+Shift+P → "MCP")
2. Vérifier que le serveur "collegue" est listé et actif
3. Dans le chat Cursor, les outils MCP seront disponibles automatiquement

### Workflow typique

```
1. Sélectionner du code dans l'éditeur
2. Ouvrir le chat Cursor (Ctrl+L)
3. "Fais une code review de ce code" → Cursor utilise code_review
4. "Génère des tests unitaires" → Cursor utilise test_generation
5. Cursor peut appliquer les changements directement dans le fichier
```

---

## Intégration avec Windsurf

### Configuration

Fichier : `~/.codeium/windsurf/mcp_config.json`

```json
{
  "mcpServers": {
    "collegue": {
      "serverUrl": "http://localhost:4121/mcp/"
    }
  }
}
```

### Utilisation

Windsurf (Cascade) utilise automatiquement les outils MCP quand c'est pertinent :

- "Analyse ce code pour les problèmes de performance" → `performance_analysis`
- "Refactorise cette fonction pour être plus lisible" → `code_refactoring`
- "Vérifie la sécurité de mon Dockerfile" → `iac_guardrails_scan`

---

## Intégration avec Claude Code (CLI)

### Configuration

```bash
claude mcp add --transport http collegue http://localhost:4121/mcp/
```

### Utilisation

```bash
# Conversation avec accès aux outils
claude chat

# Ou commande directe
claude "Fais une code review de src/main.py"
```

---

## Intégration dans un nouveau projet

### Étape 1 : Initialiser le projet

```bash
mkdir mon-projet && cd mon-projet
git init
```

### Étape 2 : Configurer votre IDE

Créer `.cursor/mcp.json` (ou équivalent) :

```json
{
  "mcpServers": {
    "collegue": {
      "serverUrl": "http://localhost:4121/mcp/"
    }
  }
}
```

### Étape 3 : Premier audit

```
"Analyse l'architecture de ces fichiers : [liste des fichiers]"
"Vérifie les incohérences dans le code"
"Donne-moi un dashboard de santé du projet"
```

### Étape 4 : Intégrer dans le workflow

```
Avant chaque commit :
  → "Fais une code review de mes changements"
  → "Vérifie que le Dockerfile est sécurisé"

Avant chaque PR :
  → "Analyse l'impact du renommage de X en Y"
  → "Génère les tests manquants pour ces fonctions"

À chaque sprint :
  → "Audit complet du module X"
  → "Dashboard de santé global"
```

---

## Intégration dans un projet existant

### Audit initial

Commencez par un diagnostic complet :

```
1. "Fais un audit architecture de [fichiers principaux]"
   → Identifie les patterns, la dette technique, les dépendances

2. "Analyse la performance de [module critique]"
   → Détecte les hotspots, O(n²), I/O bloquant

3. "Vérifie la cohérence du code dans [répertoire]"
   → Trouve les imports inutilisés, code mort, duplication

4. "Scanne la sécurité IaC de [Dockerfile/terraform]"
   → Identifie les vulnérabilités de configuration
```

### Plan de remédiation

Utilisez l'orchestrateur pour planifier :

```
"Planifie un audit complet du module auth : qualité, sécurité,
performance, et propose un plan de refactoring priorisé"
```

L'orchestrateur :
1. Appelle `code_review` → findings de qualité
2. Appelle `performance_analysis` → hotspots
3. Appelle `architecture_analysis` → dette technique
4. Synthétise un plan priorisé

### Adoption progressive

| Semaine | Action |
|---------|--------|
| 1 | Configurer MCP, faire un audit initial |
| 2 | Intégrer `code_review` dans le workflow de PR |
| 3 | Ajouter `test_generation` pour les modules critiques |
| 4 | Activer `iac_guardrails_scan` pour l'infra |
| 5+ | Utiliser l'orchestrateur pour les audits réguliers |

---

## Intégration CI/CD

### GitHub Actions — Analyse de qualité

```yaml
name: Collegue Analysis
on:
  pull_request:
    types: [opened, synchronize]

jobs:
  analysis:
    runs-on: ubuntu-latest
    services:
      collegue:
        image: collegue-mcp
        env:
          LLM_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          MCP_TRANSPORT: http
        ports:
          - 4121:4121
    steps:
      - uses: actions/checkout@v4

      - name: Wait for Collegue server
        run: |
          for i in $(seq 1 30); do
            curl -sf http://localhost:4121/mcp/ && break || sleep 2
          done
```

### Pre-commit hook

```yaml
# .pre-commit-config.yaml
repos:
  - repo: local
    hooks:
      - id: collegue-review
        name: Collegue Quick Review
        entry: python scripts/pre_commit_review.py
        language: python
        types: [python]
```

---

## Cas d'usage avancés

### 1. Audit de sécurité d'une PR

```
"Analyse l'impact de ces changements sur la sécurité :
- Fichiers modifiés : auth.py, middleware.py
- Intent : ajouter un nouveau middleware d'authentification
- Diff : [coller le diff]"
```

Collègue va :
1. `impact_analysis` → identifier les fichiers impactés
2. `iac_guardrails_scan` → vérifier la config
3. `code_review` → chercher les failles de sécurité

### 2. Migration de framework

```
"Je veux migrer de Flask vers FastAPI. Analyse ces fichiers
et propose un plan de migration avec l'impact sur chaque module."
```

L'orchestrateur planifie :
1. `architecture_analysis` → comprendre les dépendances
2. `impact_analysis` → mesurer l'impact
3. `code_refactoring` → proposer le nouveau code
4. `test_generation` → régénérer les tests

### 3. Onboarding d'un nouveau développeur

```
"Explique l'architecture de ce projet, ses patterns principaux,
et les conventions de code utilisées."
```

→ `architecture_analysis` + `code_documentation` fournissent un guide complet.

### 4. Revue de code automatisée

Configurez votre CI pour appeler Collègue sur chaque PR et poster un commentaire avec :
- Score de qualité
- Findings critiques
- Suggestions d'amélioration
- Score de performance

---

## Bonnes pratiques

### Sécurité

- **Ne commitez jamais** `LLM_API_KEY` dans le repo
- Utilisez des variables d'environnement ou des secrets CI
- Le token GitHub doit avoir les permissions minimales nécessaires

### Performance

- Utilisez le mode `fast` en CI (pas de coût LLM, feedback en < 1s)
- Réservez le mode `deep` pour les revues manuelles importantes
- Limitez la taille des fichiers envoyés (< 500 lignes pour un feedback optimal)

### Organisation d'équipe

- Partagez la même config MCP via `.cursor/mcp.json` dans le repo
- Documentez les workflows Collègue dans votre CONTRIBUTING.md
- Utilisez le dashboard pour suivre l'évolution de la qualité

### Coûts

| Usage | Estimation |
|-------|-----------|
| Mode fast uniquement | Gratuit (0 appels LLM) |
| 10 analyses deep/jour | ~$0.05/jour (Gemini) |
| 50 analyses deep/jour | ~$0.25/jour (Gemini) |
| CI sur chaque PR (fast) | Gratuit |

> Les coûts sont basés sur Gemini avec le modèle `gemma-4-26b-a4b-it`. D'autres modèles peuvent avoir des coûts différents.
