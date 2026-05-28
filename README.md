# Collègue MCP

> 🇫🇷 **Version française** | 🇬🇧 [English version](README.en.md)

[![Tests](https://github.com/VynoDePal/Collegue/actions/workflows/tests.yml/badge.svg)](https://github.com/VynoDePal/Collegue/actions/workflows/tests.yml)

Un **collectif d'experts IA spécialisés** sous forme de serveur MCP (Model Context Protocol). Chaque outil est un agent expert dans son domaine — analyse de code, refactoring, tests, sécurité, architecture — et ils travaillent ensemble via un système de délégation automatique, mémoire persistante et monitoring proactif.

## 🚀 Utilisation Rapide (Client NPM)

Configurez votre IDE pour utiliser Collègue via le wrapper NPM officiel. Cela connecte votre IDE au serveur public par défaut.

### Windsurf / Cursor / Claude Desktop / Antigravity

Ajoutez ceci à votre configuration `mcpServers` (souvent dans `~/.codeium/windsurf/mcp_config.json` ou équivalent) :

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

---

## ✨ Fonctionnalités (Outils MCP)

### 🧠 Experts IA (Agents Intelligents)

Chaque expert utilise un LLM en backend, itère sur sa sortie via une **boucle agentique** (validation → correction → re-exécution), et peut **déléguer automatiquement** à d'autres experts.

*   **🔍 Code Review** : Revue qualité complète — naming, complexité, sécurité, DRY, SOLID, error handling. Score de qualité + findings détaillés.
*   **🏗️ Architecture Analysis** : Analyse des patterns, dépendances, cycles, couplage/cohésion. Détection de dette technique.
*   **⚡ Performance Analysis** : Détection de complexité O(n²), I/O bloquant, concat en boucle, hotspots. Score de performance.
*   **♻️ Code Refactoring** : Restructure et optimise le code existant. Valide la syntaxe AST et compare les métriques avant/après.
*   **🧪 Test Generation** : Génère des tests unitaires exécutables. Parse et valide les tests générés avant de les retourner.
*   **📚 Code Documentation** : Génère docstrings et documentation technique avec couverture mesurée.
*   **🛡️ IaC Guardrails Scan** : Sécurisation IaC (Terraform, Kubernetes, Dockerfile) avec scoring de conformité.
*   **🎯 Impact Analysis** : Analyse prédictive des risques d'un changement de code. Insights sémantiques en mode deep.
*   **🔍 Repo Consistency Check** : Détection d'incohérences, code mort, hallucinations. Délégation automatique vers refactoring.
*   **🎛️ Smart Orchestrator** : Reçoit une requête complexe, planifie les experts nécessaires, exécute, synthétise la réponse.

### 🔧 Outils Statiques

*   **📦 Dependency Guard** : Audit de sécurité des dépendances (Supply Chain) — typosquatting, vulnérabilités.
*   **🔐 Secret Scan** : Détection proactive de secrets, clés API et tokens exposés.
*   **🧪 Run Tests** : Exécution de tests unitaires (Python/JS/TS) avec rapports structurés.

### 🔌 Outils d'Intégration

*   **🐘 PostgreSQL Database** : Inspection de schéma, requêtes SQL (lecture seule), statistiques de tables.
*   **🐙 GitHub Operations** : Gestion des repos, PRs, issues, branches, workflows CI/CD.
*   **🚨 Sentry Monitor** : Récupération des erreurs, stacktraces, statistiques de projet.
*   **☸️ Kubernetes Operations** : Inspection des pods, logs, déploiements, services, événements.

---

## 🤖 Système Multi-Agents / Collectif d'Experts

Collègue n'est pas un simple serveur d'outils — c'est un **collectif d'experts IA** qui collaborent automatiquement.

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Collègue MCP Server                       │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
│  │  Code    │  │  Archi   │  │  Perf    │  │ Refactor │   │
│  │  Review  │──│ Analysis │──│ Analysis │──│  Expert  │   │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘   │
│       │              │              │              │         │
│  ┌────▼─────┐  ┌────▼─────┐  ┌────▼─────┐  ┌────▼─────┐   │
│  │  Test    │  │   Doc    │  │   IaC    │  │ Impact  │   │
│  │   Gen    │  │  Expert  │  │  Scan    │  │ Analysis│   │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘   │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  ExpertDelegation · ProjectMemory · ProactiveMonitor│   │
│  │  ExpertDashboard · SmartOrchestrator                │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### Composants clés

| Composant | Rôle |
|-----------|------|
| **Boucle Agentique** | Chaque expert itère sur sa sortie (exécute → valide → corrige → re-exécute) jusqu'à convergence |
| **Délégation Inter-Experts** | 14 règles automatiques : un expert peut déclencher d'autres experts (ex: `code_review` → `refactoring` si score < 0.5) |
| **Mémoire Persistante** | Les experts stockent leurs résultats dans `.collegue/memory/` et rappellent le contexte aux sessions suivantes |
| **Moniteur Proactif** | Détecte les fichiers modifiés et décide quels experts déclencher (`.py` → code_review, `Dockerfile` → iac_scan) |
| **Tableau de Bord** | Agrège les scores qualité/architecture/performance/sécurité avec recommandations priorisées |

### Exemple de chaîne d'experts

```
repo_consistency_check (score=0.8)
  → code_refactoring (restructure le code)
    → code_review (vérifie la qualité)
    → code_documentation (met à jour la doc)
    → test_generation (génère les tests)
```

> 📖 **Guide complet** : [docs/multi_agent_expert_system.md](docs/multi_agent_expert_system.md)

### 📚 Documentation complète

| Document | Description |
|----------|-------------|
| [Guide Utilisateur](docs/guide_utilisateur.md) | Installation, configuration, premiers pas, bonnes pratiques |
| [Guide d'Intégration](docs/guide_integration.md) | Intégration dans Claude Desktop, Cursor, Windsurf, CI/CD |
| [Référence des Experts](docs/reference_experts.md) | Paramètres, sorties et cas d'usage de chaque expert |
| [Système Multi-Agents](docs/multi_agent_expert_system.md) | Architecture technique, délégation, mémoire |

---

## 🧪 Qualité des sorties LLM

Au-delà des tests unitaires + stress (robustesse), une suite d'**évaluations golden** mesure la *correction* des tools qui appellent un LLM — par exemple : les tests générés par `test_generation` sont-ils réellement exécutables et corrects ? Voir [docs/llm_evals.md](docs/llm_evals.md).

Matrice **5 modèles × 3 paths × 13 cas Python** (fonctions pures à decorators complexes en passant par async context managers) — **195 appels LLM, 0 crash**. Trois paths comparés : MCP Collègue, prompt "compétent" (utilisateur qui connaît pytest), prompt naïf.

| Modèle | **MCP** | Competent | Raw | Δ MCP−Competent | Δ MCP−Raw |
|---|---|---|---|---|---|
| `gemini-2.5-flash` | **0.833** | 0.656 | 0.867 | **+0.177** | −0.034 |
| `gemini-3-flash-preview` | 0.918 | **0.959** | 0.615 | **−0.041** | +0.303 |
| `gemini-3.1-pro-preview` | **0.917** | 0.911 | 0.538 | +0.006 | +0.379 |
| `gemma-4-26b-a4b-it` | **0.982** | 0.903 | 0.972 | +0.079 | +0.010 |
| `gemma-4-31b-it` | 0.864 | **0.977** | 0.943 | **−0.113** | −0.079 |

**Lecture honnête** :

- Face à un utilisateur naïf, MCP délivre un **gros lift sur Gemini 3.x** (+0.30 à +0.38) ; marginal ou négatif ailleurs.
- Face à un utilisateur qui écrit un prompt soigné, MCP **ne gagne que sur `gemini-2.5-flash`** (+0.177) ; sur 3 modèles sur 5 le prompt compétent égale ou bat le MCP.
- `gemma-4-26b-a4b-it` est le meilleur baseline du corpus (0.982 en MCP, 0.972 en raw) — candidat prod sérieux.
- La valeur réelle du MCP = **"éviter à l'utilisateur d'écrire un prompt soigné"** + gain net sur `gemini-2.5-flash`. Pas le game-changer universel qu'une mesure à 8 cas suggérait.

Détails complets dans [docs/llm_evals.md](docs/llm_evals.md) · 13 cas × 5 modèles × 3 paths = 195 scores reproductibles.

```bash
# Matrice complète
python -m tests.evals.runner \
  --tool test_generation --tool test_generation_raw --tool test_generation_competent \
  --model gemini-2.5-flash --model gemini-3-flash-preview \
  --model gemini-3.1-pro-preview --model gemma-4-26b-a4b-it --model gemma-4-31b-it

# Run simple (1 modèle, MCP seulement)
python -m tests.evals.runner --tool test_generation
```

---

## 🐳 Auto-hébergement (Docker)

Deux modes sont supportés selon l'envie : **serveur long-running** (`docker compose`) accédé en HTTP, ou **container à la volée** (`docker run -i --rm`) que le client MCP spawne/tue à chaque session en stdio. Choisissez un mode ci-dessous.

### Mode A — Serveur long-running (docker compose, transport HTTP)

Cette variante couvre l'hébergement local du serveur Collègue dans un container Docker qui tourne en permanence, puis la configuration de votre IDE pour s'y connecter en HTTP (streamable transport, port `4121`).

#### 1. Lancer le serveur

```bash
git clone https://github.com/VynoDePal/Collegue.git
cd Collegue
cp .env.example .env   # au minimum, renseigner LLM_API_KEY (Gemini)
docker compose up -d
```

Endpoints exposés une fois le container démarré :

| Endpoint | Rôle |
|---|---|
| `http://localhost:4121/mcp/` | Serveur MCP (transport streamable HTTP) |
| `http://localhost:4122/_health` | Healthcheck (retourne `{"status":"ok"}`) |

Vérification :

```bash
curl -s http://localhost:4122/_health
```

#### 2. Configurer le client MCP

Pointez votre IDE sur `http://localhost:4121/mcp/` au lieu du wrapper NPM. Trois variantes selon le client.

##### Claude Code (CLI Anthropic)

```bash
claude mcp add --transport http collegue-local http://localhost:4121/mcp/
```

Ou en JSON dans `~/.claude/settings.json` :

```json
{
  "mcpServers": {
    "collegue-local": {
      "transport": "http",
      "url": "http://localhost:4121/mcp/"
    }
  }
}
```

##### Windsurf / Cursor / Antigravity

Dans `~/.codeium/windsurf/mcp_config.json` (ou équivalent selon le client) :

```json
{
  "mcpServers": {
    "collegue-local": {
      "serverUrl": "http://localhost:4121/mcp/"
    }
  }
}
```

##### Claude Desktop

Claude Desktop ne parle pas le transport HTTP nativement — il faut un pont stdio→HTTP. Exemple avec `mcp-remote` (`~/Library/Application Support/Claude/claude_desktop_config.json` sur macOS, `%APPDATA%\Claude\claude_desktop_config.json` sur Windows) :

```json
{
  "mcpServers": {
    "collegue-local": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:4121/mcp/"]
    }
  }
}
```

#### 3. Activer les outils d'intégration

Les outils qui appellent des APIs externes (PostgreSQL, GitHub, Sentry, Kubernetes) lisent leurs credentials **depuis l'environnement du container**, pas depuis la config MCP client. Ajoutez-les à votre `.env` avant `docker compose up` :

```env
# .env
LLM_API_KEY=AIzaSy...                                           # Gemini (requis)
POSTGRES_URL=postgresql://user:password@host:5432/database      # postgres_db
GITHUB_TOKEN=ghp_xxxxxxxxxxxx                                   # github_ops (scopes: repo, read:org)
SENTRY_AUTH_TOKEN=sntrys_xxxxxxxxxxxx                           # sentry_monitor
SENTRY_ORG=my-organization                                      # sentry_monitor
```

Reprise du container après modification du `.env` :

```bash
docker compose up -d --force-recreate
```

#### 4. Dépannage

| Symptôme | Cause probable | Action |
|---|---|---|
| Le client MCP ne voit aucun outil | `url` finit sans slash (`/mcp` au lieu de `/mcp/`) | Corriger la config, redémarrer le client |
| `curl /mcp/` → `404` | Le container n'a pas fini son démarrage | Attendre ~10s, relancer ; sinon `docker compose logs -f collegue-app` |
| Outils d'intégration absents des réponses | `.env` chargé avant la correction → variables non visibles | `docker compose up -d --force-recreate` |
| Rate limit LLM atteint rapidement | Quota Gemini Free Tier (20 req/jour) | Passer en tier payant ou ajuster `LLM_RATE_LIMIT_PER_DAY` |

### Mode B — Container à la volée (docker run, transport stdio)

Cette variante est pratique si vous préférez que votre IDE spawne un container Collègue frais à chaque session MCP (pas de serveur qui tourne en arrière-plan). Le client parle au container par stdin/stdout — aucun port exposé, pas de `docker compose`.

#### 1. Construire l'image localement

```bash
git clone https://github.com/VynoDePal/Collegue.git
cd Collegue
docker build -f docker/collegue/Dockerfile -t collegue-mcp .
```

> L'image n'est pas (encore) publiée sur Docker Hub, il faut donc la construire soi-même. Elle pèse ~450 Mo.

#### 2. Configurer le client MCP

Ajoutez ceci à votre config `mcpServers` (même fichier que les exemples ci-dessus). Les credentials des outils d'intégration passent en `-e` directement dans `args`, plus besoin de `.env` puisqu'aucun fichier n'est monté dans le container.

```json
{
  "mcpServers": {
    "collegue": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "-e", "MCP_TRANSPORT=stdio",
        "-e", "LLM_API_KEY=votre_clé_gemini_ici",
        "-e", "GITHUB_TOKEN=ghp_xxxxxxxxxxxx",
        "-e", "SENTRY_AUTH_TOKEN=sntrys_xxxxxxxxxxxx",
        "-e", "SENTRY_ORG=my-organization",
        "collegue-mcp"
      ]
    }
  }
}
```

**Flags importants :**
- `-i` : laisse stdin ouvert (obligatoire pour le transport stdio)
- `--rm` : supprime le container à la fin de la session
- `MCP_TRANSPORT=stdio` : bascule `entrypoint.sh` en mode stdio (sans ça, le container démarre le serveur HTTP sur 4121 et le client stdio bloque)

#### 3. Accès à Kubernetes / PostgreSQL local depuis le container

Le container ne voit par défaut ni votre `~/.kube/config` ni un service PostgreSQL tournant sur votre machine hôte. Si vous en avez besoin, ajoutez un bind-mount ou `--network=host` :

```json
"args": [
  "run", "-i", "--rm",
  "-e", "MCP_TRANSPORT=stdio",
  "-e", "LLM_API_KEY=...",
  "-e", "KUBECONFIG=/root/.kube/config",
  "-v", "${HOME}/.kube/config:/root/.kube/config:ro",
  "--network=host",
  "collegue-mcp"
]
```

> `--network=host` ne fonctionne que sur Linux. Sur macOS/Windows, utilisez `host.docker.internal` dans vos URLs (ex: `POSTGRES_URL=postgresql://user:pwd@host.docker.internal:5432/db`).

#### 4. Comparaison des deux modes

| Critère | Mode A (compose + HTTP) | Mode B (docker run + stdio) |
|---|---|---|
| Démarrage | Une fois, `docker compose up -d` | À chaque session du client MCP |
| Latence du 1er appel | ~50 ms (serveur déjà chaud) | ~2-3 s (cold start du container) |
| Credentials | `.env` dans le repo cloné | Variables `-e` dans la config MCP |
| Ports exposés | `4121`, `4122` | Aucun |
| Adapté au travail hors-ligne | Oui | Oui |
| Watchdog autonome | Disponible | Indisponible (pas de boucle long-running) |

---

## 🛠️ Développement Local (Python)

Pour contribuer au développement du serveur backend :

```bash
# Installation
python -m venv .venv
source .venv/bin/activate  # ou .venv\Scripts\activate sur Windows
pip install -r requirements.txt

# Lancement du serveur
python -m collegue.app
```

---

## 🔑 Configuration des Intégrations

Les outils d'intégration se configurent via le bloc `env` de la configuration MCP :

```json
{
  "mcpServers": {
    "collegue": {
      "command": "npx",
      "args": ["-y", "@collegue/mcp@latest"],
      "env": {
        "POSTGRES_URL": "postgresql://user:password@host:5432/database",
        "GITHUB_TOKEN": "ghp_xxxxxxxxxxxx",
        "SENTRY_AUTH_TOKEN": "sntrys_xxxxxxxxxxxx",
        "SENTRY_ORG": "my-organization"
      }
    }
  }
}
```

### Variables disponibles

| Variable | Description | Outil |
|----------|-------------|-------|
| `POSTGRES_URL` | URI PostgreSQL (ou `DATABASE_URL`) | postgres_db |
| `GITHUB_TOKEN` | Token GitHub (permissions: repo, read:org) | github_ops |
| `SENTRY_AUTH_TOKEN` | Token d'authentification Sentry | sentry_monitor |
| `SENTRY_ORG` | Slug de l'organisation Sentry | sentry_monitor |
| `SENTRY_URL` | URL Sentry self-hosted (optionnel) | sentry_monitor |
| `KUBECONFIG` | Chemin vers kubeconfig (optionnel) | kubernetes_ops |

---

## 🤖 Agent Autonome Watchdog (Self-Healing)

Le **Watchdog** est un agent autonome de **Self-Healing** qui surveille automatiquement vos erreurs Sentry et génère des correctifs via Pull Requests GitHub.

### ⚡ Fonctionnement

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Sentry    │ ──► │  Watchdog   │ ──► │     LLM     │ ──► │   GitHub    │
│  (erreurs)  │     │  (analyse)  │     │   (fix)     │     │    (PR)     │
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
```

1.  **Surveillance** : Scan périodique des erreurs non résolues dans Sentry
2.  **Context Pack** : Extraction intelligente du code source via GitHub (AST-based)
3.  **Analyse LLM** : Génération de patchs minimaux avec recherche web
4.  **Fuzzy Matching** : Application robuste des correctifs (stratégie Aider/RooCode)
5.  **Pull Request** : Création automatique d'une PR avec explication détaillée

### 🛡️ Sécurités intégrées

*   **Validation syntaxique** : Le code Python est vérifié via `ast.parse()` avant commit
*   **Protection anti-destruction** : Les patchs réduisant le fichier de >50% sont rejetés
*   **Fuzzy matching** : Tolérance aux variations d'indentation (score minimum: 0.6)
*   **Atomicité** : Tous les patchs d'un fix doivent réussir ou aucun n'est appliqué
*   **Déduplication** : Chaque issue n'est traitée qu'une seule fois par cycle

### 👥 Mode Multi-Utilisateurs

Le Watchdog supporte automatiquement plusieurs utilisateurs via le **Config Registry** :

*   Les configurations sont enregistrées lors de chaque requête MCP
*   Le watchdog itère sur toutes les configurations actives (< 24h)
*   Les configurations inactives (> 48h) sont automatiquement nettoyées
