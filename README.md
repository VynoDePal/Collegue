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

> **Réseau : loopback par défaut.** `docker compose up` publie **tous** les ports sur `127.0.0.1`
> (MCP `4121`, health `4122`, Keycloak `4123`, dashboard `4125`, nginx `8088`) : rien n'est joignable
> depuis le réseau. Le serveur écoute sur `0.0.0.0` **à l'intérieur** du conteneur ; seule la
> publication côté hôte est restreinte. Trois variables indépendantes (dans `.env`) changent l'adresse
> de publication :
>
> | Variable | Ports concernés |
> |----------|-----------------|
> | `COLLEGUE_PUBLISH_HOST` | MCP `4121`, health `4122`, nginx `8088` |
> | `COLLEGUE_DASHBOARD_PUBLISH_HOST` | dashboard Streamlit `4125` (**aucune authentification**) |
> | `COLLEGUE_KEYCLOAK_PUBLISH_HOST` | Keycloak `4123` (`start-dev`, à ne pas exposer tel quel) |
>
> **Exposition distante** (par ex. `COLLEGUE_PUBLISH_HOST=0.0.0.0`) : choix explicite, à n'utiliser
> qu'avec `OAUTH_ENABLED=true`. Ce n'est **pas** imposé : sans OAuth, le serveur journalise seulement un
> avertissement et reste joignable sans authentification. Exposer le MCP n'ouvre ni le dashboard ni
> Keycloak. Un reverse proxy TLS reste recommandé.
>
> **OAuth fail-closed.** Avec `OAUTH_ENABLED=true`, si l'authentification ne peut pas être établie
> (`JWTVerifier` absent, constructeur en erreur, ni `OAUTH_JWKS_URI` ni `OAUTH_PUBLIC_KEY`,
> `OAUTH_ISSUER` ou `OAUTH_ALGORITHM` vides), le serveur **refuse de démarrer** et le conteneur sort
> en code non nul (Compose le relance selon `restart: always` : lire ses logs). Le mode sans
> authentification n'existe que via `OAUTH_ENABLED=false` (défaut), explicite et journalisé.
> `OAUTH_ALGORITHM` et `OAUTH_REQUIRED_SCOPES` sont **effectivement appliqués** à chaque jeton : un
> jeton sans les scopes configurés est refusé (vérifier vos jetons avant de mettre à jour).
> Hors Docker, `HOST` vaut désormais `127.0.0.1` par défaut.

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
| `OAUTH_ENABLED` (+ `OAUTH_*`, Keycloak) | Authentification OAuth (**off** par défaut ; **fail-closed** si activée : voir « Réseau » plus haut) | |
| `COLLEGUE_PUBLISH_HOST` / `COLLEGUE_DASHBOARD_PUBLISH_HOST` / `COLLEGUE_KEYCLOAK_PUBLISH_HOST` | Adresse de publication des ports Docker (`127.0.0.1` par défaut) | |
| `GITHUB_TOKEN` / `GITHUB_OWNER` / `GITHUB_REPO` | Intégration GitHub (watchdog, PR) | |
| `SENTRY_DSN` / `SENTRY_ENVIRONMENT` | Observabilité Sentry | |
| `STATE_DATABASE_URL` | État durable du moteur autonome (Postgres/SQLite) | |
| `MAX_COST_USD` / `MAX_TOKENS_BUDGET` / `COLLEGUE_RUN_DEADLINE_SECONDS` | Plafonds du run, appliqués par le **registre de budget durable** (planification, BUILD, IMPROVE, reprises) ; pause à l'atteinte | |
| `BUDGET_MODE` | `strict` (défaut : réservation **avant** chaque appel émis par le framework, usage inconnu = blocage durable) ou `advisory` (enregistre sans bloquer, **aucune garantie**). La garantie porte sur les appels que le framework émet, pas sur un programme du workspace qui disposerait d'une clé : voir [w2-budget](docs/consolidation/w2-budget.md) (transports acceptés et refusés) | |
| `COLLEGUE_HOME` | Racine de persistance (métriques, checkpoints, **prompts modifiables** ; l'ancien état de prompts d'une installation précédente est repris au premier démarrage, voir [w2-installation](docs/consolidation/w2-installation.md)) | |
| `CODER_SUBSCRIPTION` (+ `CODER_SUBSCRIPTION_MODEL`, `SANDBOX_SUBSCRIPTION_AUTH_DIR`) | Codage par **abonnement** ChatGPT/Codex (coût API `$0`) au lieu d'une clé | |
| `BUILD_AUTO_MERGE` | **Merge-bot de la phase build** (auto-merge des PR de tâches ; **off** par défaut, activation explicite). Ne fusionne que sur preuve de livraison durable, checks requis réussis, base inchangée et protection stricte réellement applicable ([w3-merge](docs/consolidation/w3-merge.md)). Distinct de Phase 5 | `false` |
| `GATE_ACCEPTANCE_TESTS` | Oracles pytest générés au plan-time par le rôle QA, scellés avec le plan puis rejoués sans LLM (**off** par défaut) | |
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

**Sûr par défaut** : un run reste en `dry_run` (aucune écriture) tant qu'on ne passe pas `--execute` ;
`plan draft` persiste seulement son brouillon durable ; l'opérateur approuve ensuite
le hash affiché, et seul `plan sync --execute` touche GitHub ;
budget durable en mode `strict` (pause à l'atteinte d'un plafond ou d'un usage inconnu ; les transports non bornables sont refusés plutôt que prétendus). En BUILD réel, un **merge-bot** (opt-in : `BUILD_AUTO_MERGE`, **off** par défaut) peut auto-merger chaque tâche pour
construire le MVP, sur preuve de livraison validée ; la phase **amélioration**
laisse ses PR **ouvertes pour merge humain** (§6) par défaut. L'auto-merge
risk-gated Phase 5 est réellement câblé mais reste opt-in : CI complète, SHA stable,
resync et santé de `main` sont obligatoires. Si cette santé régresse et que
`AUTO_REVERT_ENABLED` est actif, un commit qui restaure exactement l'arbre précédent
est publié, validé par CI, fusionné sous gardes SHA puis contrôlé une dernière fois.
Une transaction durable écrite avant le merge permet de reprendre après un crash
sans dupliquer le merge ou le revert. Le rollback utilise un lease CAS entre
workers et reste en état `recovered` jusqu'à acquittement humain (`phase5 show/ack`),
ce qui empêche de reproposer en boucle le même changement. L'ensemble reste
**désactivé par défaut** et fail-closed.
Le codeur peut tourner via **abonnement** ChatGPT/Codex (coût API `$0`).

```bash
# Phase 1 : trois gestes séparés — le processus LLM ne s'auto-approuve jamais
python -m collegue.pilot plan draft --name app --problem "..." --owner org --repo app --base main
python -m collegue.pilot plan approve --project-id 1 --expected-plan-hash SHA256_AFFICHE
python -m collegue.pilot plan sync --project-id 1 --execute

# Build : aperçu (dry_run) puis exécution réelle
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
pip install --require-hashes --no-deps -r locks/dev.txt   # dépendances verrouillées et hachées (outils de test inclus)
python -m collegue.migrations upgrade                      # schéma de l'état durable (STATE_DATABASE_URL)
python -m collegue.app
```

`pyproject.toml` est la **source unique** des dépendances ; les fichiers `locks/*.txt` en sont générés
(`python scripts/locks.py`, voir [w2-installation](docs/consolidation/w2-installation.md)). Le paquet embarque ses
ressources (skills, graines de prompts, migrations) : il s'exécute aussi installé depuis un wheel, hors du dépôt.

Tests :

```bash
python -m pytest --tb=short -q
ruff check collegue tests
```
