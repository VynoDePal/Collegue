# Collègue MCP

> 🇫🇷 **Version française** | 🇬🇧 [English version](README.en.md)

[![Tests](https://github.com/VynoDePal/Collegue/actions/workflows/tests.yml/badge.svg)](https://github.com/VynoDePal/Collegue/actions/workflows/tests.yml)

Un **collectif d'experts IA spécialisés** sous forme de serveur MCP (Model Context Protocol). Chaque outil est un agent expert dans son domaine — analyse de code, refactoring, tests, sécurité, architecture — et ils travaillent ensemble via un système de délégation automatique, mémoire persistante et monitoring proactif.

---

## 🚀 Démarrage Rapide (Docker)

```bash
git clone https://github.com/VynoDePal/Collegue.git
cd Collegue
cp .env.example .env   # renseigner LLM_API_KEY (Gemini)
docker compose up -d
```

Endpoints :

| URL | Rôle |
|-----|------|
| `http://localhost:4121/mcp/` | Serveur MCP (transport HTTP) |
| `http://localhost:4122/_health` | Healthcheck |

### Configurer votre IDE

#### Claude Code (CLI)

```bash
claude mcp add --transport http collegue http://localhost:4121/mcp/
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

### Mode stdio (container à la volée)

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

> Image à construire localement : `docker build -f docker/collegue/Dockerfile -t collegue-mcp .`

---

## ✨ Les 10 Experts IA

Chaque expert utilise un LLM, itère via une **boucle agentique**, et peut **déléguer** à d'autres experts.

| Expert | Description |
|--------|-------------|
| **Code Review** | Qualité, naming, complexité, sécurité, DRY, SOLID |
| **Architecture Analysis** | Patterns, dépendances, cycles, couplage, dette technique |
| **Performance Analysis** | O(n²), I/O bloquant, concat en boucle, hotspots |
| **Code Refactoring** | Restructure, optimise, valide AST, compare métriques |
| **Test Generation** | Tests unitaires exécutables (pytest, jest, phpunit) |
| **Code Documentation** | Docstrings, documentation technique, couverture |
| **IaC Guardrails Scan** | Sécurité Terraform, Kubernetes, Dockerfile |
| **Impact Analysis** | Analyse prédictive de risques avant changement |
| **Repo Consistency Check** | Imports inutilisés, code mort, duplication |
| **Smart Orchestrator** | Planifie et coordonne plusieurs experts |

### Outils supplémentaires

| Catégorie | Outils |
|-----------|--------|
| **Statiques** | Dependency Guard, Secret Scan, Run Tests |
| **Intégrations** | PostgreSQL, GitHub, Sentry, Kubernetes |

---

## 🤖 Système Multi-Agents

```
┌─────────────────────────────────────────────────────┐
│                 Collègue MCP Server                   │
│                                                     │
│  Code Review ─── Architecture ─── Performance       │
│       │               │               │             │
│  Refactoring ─── Test Gen ─── Documentation         │
│       │               │               │             │
│  IaC Scan ─── Consistency ─── Impact Analysis       │
│                                                     │
│  ┌───────────────────────────────────────────────┐  │
│  │ Délégation · Mémoire · Monitor · Dashboard   │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

| Composant | Rôle |
|-----------|------|
| **Boucle Agentique** | Exécute → valide → corrige → re-exécute jusqu'à convergence |
| **Délégation** | 14 règles automatiques (ex: `code_review` → `refactoring` si score < 0.5) |
| **Mémoire** | Stocke les résultats dans `.collegue/memory/` pour les sessions futures |
| **Moniteur** | Détecte les fichiers modifiés et déclenche les experts pertinents |
| **Dashboard** | Agrège les scores de santé du projet |

---

## 🔑 Configuration

### Variables d'environnement (.env)

Aperçu **par thème** (liste exhaustive et valeurs par défaut dans
**[.env.example](.env.example)**) :

| Variable(s) | Description | Requis |
|-------------|-------------|--------|
| `LLM_API_KEY` | Clé API du provider LLM (Gemini par défaut) | ✓ |
| `LLM_PROVIDER` / `LLM_MODEL` | Provider et modèle LLM par défaut | |
| `LLM_MODEL_*` / `LLM_PROVIDER_*` | Modèle/provider par **rôle** (CODER, QA, PLANNER, REVIEWER) | |
| `LLM_RATE_LIMIT_*` | Limites d'appels LLM par client (minute / jour) | |
| `CACHE_ENABLED` / `CACHE_TTL` | Cache des réponses d'outils | |
| `OAUTH_ENABLED` (+ `OAUTH_*`, Keycloak) | Authentification OAuth (**off** par défaut) | |
| `GITHUB_TOKEN` / `GITHUB_OWNER` / `GITHUB_REPO` | Intégration GitHub (watchdog, PR) | |
| `SENTRY_DSN` / `SENTRY_ENVIRONMENT` | Observabilité Sentry | |
| `STATE_DATABASE_URL` | État durable du moteur autonome (Postgres/SQLite) | |
| `MAX_COST_USD` / `MAX_TOKENS_BUDGET` / `COLLEGUE_RUN_DEADLINE_SECONDS` | Budget dur du run (auto-pause) | |
| `COLLEGUE_HOME` | Racine de persistance (budget, métriques, checkpoints) | |
| `CODER_SUBSCRIPTION` (+ `CODER_SUBSCRIPTION_MODEL`, `SANDBOX_SUBSCRIPTION_AUTH_DIR`) | Codage par **abonnement** ChatGPT/Codex (coût API `$0`) au lieu d'une clé | |
| `BUILD_AUTO_MERGE` | **Merge-bot de la phase build** (auto-merge des PR de tâches ; **on** par défaut). L'amélioration reste à merge humain | |
| `SANDBOX_NETWORK` / `SANDBOX_MEMORY` / `SANDBOX_CPUS` / `SANDBOX_TIMEOUT` | Réseau et ressources du conteneur coder | |
| `AUTO_MERGE_ENABLED` / `AUTO_REVERT_ENABLED` / `PILOT_TOOL_ENABLED` | Capacités autonomes risk-gated (opt-in, **off** par défaut) | |

> Réglages détaillés du moteur autonome (budget, auto-merge/revert, outil MCP du pilote) :
> [docs/moteur_autonome.md](docs/moteur_autonome.md#réglages-env).

---

## 🧭 Moteur de développement autonome

Au-delà des experts **réactifs**, Collègue peut piloter un développement de bout en
bout : **planifier → coder → tester → ouvrir des PR**, sous budget, avec GitHub comme
substrat. Étages : `planner` → `pilote` → `executor` → `improve`, sur un socle d'état
durable (Postgres/SQLite) et de sandbox Docker.

**Sûr par défaut** : `dry_run` (aucune écriture) tant qu'on ne passe pas `--execute` ;
budget dur auto-pausé. En BUILD réel, un **merge-bot** auto-merge chaque tâche pour
construire le MVP (`BUILD_AUTO_MERGE`, on par défaut) ; la phase **amélioration**
laisse ses PR **ouvertes pour merge humain** (§6). L'auto-merge risk-gated,
l'auto-revert et l'outil MCP du pilote restent **désactivés par défaut** et fail-closed.
Le codeur peut tourner via **abonnement** ChatGPT/Codex (coût API `$0`).

```bash
# Aperçu (dry_run) puis exécution réelle
python -m collegue.pilot --project-id 1 --repo-source /chemin/clone --owner org --repo app
python -m collegue.pilot ... --execute            # écritures réelles (PR + état)
python -m collegue.pilot ... --execute --improve  # + cycle d'amélioration
```

`--improve` enchaîne, une fois le MVP **réellement mergé puis resynchronisé sur
`origin/<base>`**, la **boucle d'amélioration continue**
(Phase 4) : un **objectif de qualité déterministe** (couverture − sécu − lint −
complexité, sans avis de LLM) ouvre des PR seulement quand le diff **progresse sans
régression** (gate fail-closed) ; les PR sont **stackées** et s'arrêtent au plateau.
Une dernière PR BUILD non mergée ou un resync git en échec bloque Phase 4 au lieu
de produire un faux succès `completed`.

Architecture, boucle d'amélioration, garde-fous, observabilité/audit, reprise après
crash et réglages : **[docs/moteur_autonome.md](docs/moteur_autonome.md)**.

---

## 🤖 Agent Watchdog (Self-Healing)

Surveille Sentry et génère des PRs GitHub automatiques pour corriger les erreurs.

```
Sentry (erreurs) → Watchdog (analyse) → LLM (fix) → GitHub (PR)
```

Voir [docs/watchdog_deployment.md](docs/watchdog_deployment.md) pour le déploiement.

---

## 📚 Documentation

| Document | Description |
|----------|-------------|
| [Guide Utilisateur](docs/guide_utilisateur.md) | Installation, configuration, premiers pas, bonnes pratiques |
| [Guide d'Intégration](docs/guide_integration.md) | Intégration Claude Desktop, Cursor, Windsurf, CI/CD |
| [Référence des Experts](docs/reference_experts.md) | Paramètres, sorties et cas d'usage de chaque expert |
| [Système Multi-Agents](docs/multi_agent_expert_system.md) | Architecture technique, délégation, mémoire |
| [Moteur de développement autonome](docs/moteur_autonome.md) | Pilote autonome : architecture, **amélioration continue (Phase 4)**, garde-fous, audit, reprise, réglages |
| [Évaluations LLM](docs/llm_evals.md) | Benchmarks qualité des sorties LLM |
| [Rate Limiting](docs/rate_limiting_and_quotas.md) | Quotas et limites |

---

## 🛠️ Développement

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m collegue.app
```

Tests :

```bash
python -m pytest --tb=short -q
ruff check collegue tests
```
