# Collègue MCP

Un assistant de développement intelligent et serveur MCP (Model Context Protocol) fournissant des outils d'analyse, de refactoring, de documentation, d'analyse de risques, sécurité et bien plus.

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

### 🔧 Outils d'Analyse de Code

*   **🛡️ IaC Guardrails Scan** : Sécurisation de l'Infrastructure as Code (Terraform, Kubernetes, Dockerfile) contre les mauvaises configurations et privilèges excessifs.
*   **🎯 Impact Analysis** : Analyse prédictive des risques et impacts d'un changement de code avant son application.
*   **🔍 Repo Consistency Check** : Détection d'incohérences subtiles, code mort et hallucinations silencieuses dans le codebase.
*   **📦 Dependency Guard** : Audit de sécurité des dépendances (Supply Chain) pour éviter typosquatting et paquets malveillants/vulnérables.
*   **🔐 Secret Scan** : Détection proactive de secrets, clés API et tokens exposés dans le code.
*   **🧪 Run Tests** : Exécution de tests unitaires (Python/JS/TS) avec rapports structurés intégrés au contexte.
*   **♻️ Refactoring** : Outils automatisés pour nettoyer, optimiser et restructurer le code existant.
*   **📚 Documentation** : Génération automatique de documentation technique et docstrings.
*   **⚡ Test Generation** : Création intelligente de tests unitaires validés par exécution.

### 🔌 Outils d'Intégration (NEW)

*   **🐘 PostgreSQL Database** : Inspection de schéma, requêtes SQL (lecture seule), statistiques de tables, clés étrangères et index.
*   **🐙 GitHub Operations** : Gestion des repos, PRs, issues, branches, workflows CI/CD et recherche de code.
*   **🚨 Sentry Monitor** : Récupération des erreurs, stacktraces, statistiques de projet et releases pour prioriser le debugging.
*   **☸️ Kubernetes Operations** : Inspection des pods, logs, déploiements, services, événements et ressources du cluster.

---

## 🐳 Auto-hébergement (Docker)

Cette section couvre l'hébergement local du serveur Collègue dans un container Docker, puis la configuration de votre IDE pour s'y connecter en HTTP (streamable transport, port `4121`).

### 1. Lancer le serveur

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

### 2. Configurer le client MCP

Pointez votre IDE sur `http://localhost:4121/mcp/` au lieu du wrapper NPM. Trois modes selon le client.

#### Claude Code (CLI Anthropic)

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

#### Windsurf / Cursor / Antigravity

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

#### Claude Desktop

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

### 3. Activer les outils d'intégration

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

### 4. Dépannage

| Symptôme | Cause probable | Action |
|---|---|---|
| Le client MCP ne voit aucun outil | `url` finit sans slash (`/mcp` au lieu de `/mcp/`) | Corriger la config, redémarrer le client |
| `curl /mcp/` → `404` | Le container n'a pas fini son démarrage | Attendre ~10s, relancer ; sinon `docker compose logs -f collegue-app` |
| Outils d'intégration absents des réponses | `.env` chargé avant la correction → variables non visibles | `docker compose up -d --force-recreate` |
| Rate limit LLM atteint rapidement | Quota Gemini Free Tier (20 req/jour) | Passer en tier payant ou ajuster `LLM_RATE_LIMIT_PER_DAY` |

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
