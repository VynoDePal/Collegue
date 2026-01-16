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

Si vous souhaitez héberger votre propre instance du serveur Collègue (backend Python) :

1.  **Cloner le dépôt**
    ```bash
    git clone https://github.com/VynoDePal/Collegue.git
    cd Collegue
    ```

2.  **Configuration**
    Copiez le fichier d'exemple et configurez vos clés API (OpenRouter, etc.) :
    ```bash
    cp .env.example .env
    ```

3.  **Lancement**
    ```bash
    docker compose up -d
    ```
    Le serveur sera accessible sur le port configuré (par défaut `4121`).

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
