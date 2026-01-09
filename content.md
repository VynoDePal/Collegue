# Collègue

@VynoDePal

[Visit Server](https://github.com/VynoDePal/Collegue)

[# mcp](https://mcp.so/tag/mcp) · [# developer-tools](https://mcp.so/tag/developer-tools) · [# code-generation](https://mcp.so/tag/code-generation) · [# refactoring](https://mcp.so/tag/refactoring) · [# documentation](https://mcp.so/tag/documentation) · [# testing](https://mcp.so/tag/testing)

---

## What is Collègue MCP?
Collègue est un serveur Model Context Protocol (MCP) destiné aux développeurs. Il fournit un ensemble d’outils avancés pour analyser, générer, refactorer, documenter le code et créer des tests unitaires. Construit avec FastMCP, il s’intègre facilement à des clients MCP comme Windsurf, Cursor, Claude Desktop et d’autres IDE compatibles.

Points forts:
- Outils de productivité orientés développeurs (analyse, génération, refactoring, doc, tests)
- Gestion centralisée du LLM (config via environnement, MCP headers ou mcp_config côté client)
- Endpoints de santé et métadonnées OAuth prêts pour la production
- Intégration possible avec Keycloak et scopes fins (`mcp.read`, `mcp.write`)

---

## How to use Collègue MCP?

Méthodes recommandées:

1) Démarrage via Docker Compose (incluant nginx et OAuth optionnel)
- Voir `docker-compose.yml` du projet
- Expose par défaut `http://localhost:8088/` via nginx
- Endpoint de santé: `/_health`

2) Démarrage local (Python)
- Python 3.10+
- `pip install -r requirements.txt`
- Lancer: `python -m collegue.app`

3) Configuration côté client (Windsurf/Cursor)
- Déclarez le serveur HTTP Collègue dans votre configuration MCP (exemples ci-dessous)
- Optionnel: passez `X-LLM-Model` et `X-LLM-Api-Key` via headers pour surcharger le modèle/clé à la volée

---

## Key features of Collègue MCP
- Analyse de code multi-langages (Python, JS/TS, Java, C#, Go, Rust, PHP, Ruby)
- Génération de code guidée par description avec contraintes et contexte
- Refactoring (rename, extract, simplify, optimize, clean, modernize) avec métriques
- Génération automatique de documentation (Markdown, RST, HTML, docstring, JSON)
- Génération de tests unitaires (pytest, unittest, Jest, etc.) avec estimation de couverture
- **Exécution de tests** (run_tests) avec résultats structurés et intégration test_generation
- **Détection de secrets** (secret_scan) : 30+ patterns (AWS, GCP, OpenAI, GitHub, etc.)
- **Audit de dépendances** (dependency_guard) : vulnérabilités, typosquatting, blocklist
- Système de prompts amélioré, A/B testing, optimisation par langage
- Intégration LLM flexible (OpenRouter recommandé), surcharge par headers MCP
- Endpoints de santé et découverte OAuth pour intégration SSO

---

## Use cases of Collègue MCP
1) Revue de code assistée: comprendre rapidement un module inconnu
2) Démarrage rapide: générer un squelette de service/API/algorithme
3) Amélioration continue: refactorer pour lisibilité et performance
4) Documentation vivante: produire/mettre à jour la doc et docstrings
5) Qualité logicielle: générer une base de tests pour augmenter la couverture
6) Sécurité: détecter les secrets exposés avant commit
7) Supply chain: auditer les dépendances contre les vulnérabilités et packages malveillants
8) CI/CD: exécuter et valider les tests générés automatiquement

---

## Tools

Collègue expose les outils MCP suivants (via `collegue/tools/`):

- code_explanation
  - Description: Analyse et explique du code multi-langages
  - Paramètres clés: `code`, `language?`, `detail_level?`, `focus_on?`, `session_id?`

- code_generation
  - Description: Génère du code à partir d’une description textuelle
  - Paramètres clés: `description`, `language`, `context?`, `constraints?`, `file_path?`, `session_id?`

- code_documentation
  - Description: Génère automatiquement de la documentation (Markdown, RST, HTML, docstring, JSON)
  - Paramètres clés: `code`, `language`, `doc_style?`, `doc_format?`, `include_examples?`, `focus_on?`, `file_path?`, `session_id?`

- code_refactoring
  - Description: Refactorise le code (rename/extract/simplify/optimize/clean/modernize) et calcule des métriques d’amélioration
  - Paramètres clés: `code`, `language`, `refactoring_type`, `parameters?`, `file_path?`, `session_id?`

- test_generation
  - Description: Génère des tests unitaires et estime la couverture
  - Paramètres clés: `code`, `language`, `test_framework?`, `include_mocks?`, `coverage_target?`, `validate_tests?`, `file_path?`, `output_dir?`, `session_id?`
  - Nouveau: `validate_tests=true` exécute automatiquement les tests générés via run_tests

- run_tests
  - Description: Exécute des tests unitaires et retourne des résultats structurés
  - Paramètres clés: `target?`, `test_content?`, `source_content?`, `language`, `framework?`, `working_dir?`, `timeout?`, `pattern?`, `verbose?`
  - **Important**: Utilisez `test_content` pour passer le code de test directement (recommandé avec MCP)
  - Frameworks: pytest, unittest, jest, mocha, vitest

- secret_scan
  - Description: Détecte les secrets exposés dans le code (clés API, tokens, mots de passe)
  - Paramètres clés: `target?`, `content?`, `files?`, `scan_type?`, `language?`, `severity_threshold?`, `exclude_patterns?`
  - **RECOMMANDÉ pour MCP**: Utilisez `files` (liste de `{path, content}`) pour scanner tout un projet en batch
  - Exemple: `files: [{path: "src/app.ts", content: "..."}, {path: ".env", content: "..."}]`
  - Retourne: résumé détaillé, liste des fichiers affectés, findings avec sévérité
  - 30+ patterns: AWS, GCP, Azure, OpenAI, GitHub, Stripe, JWT, clés privées, etc.

- dependency_guard
  - Description: Audite les dépendances pour vulnérabilités (npm audit / pip-audit) et packages malveillants
  - Paramètres clés: `target?`, `manifest_content?`, `lock_content?`, `manifest_type?`, `language`, `check_vulnerabilities?`, `blocklist?`, `allowlist?`
  - **RECOMMANDÉ pour JS/TS (Local)**: Si l'outil a accès au disque (pas d'isolation Docker), utilisez `target` (chemin du projet).
  - **POUR JS/TS (Docker/Isolé)**: Si `target` échoue (fichier introuvable), vous DEVEZ fournir `manifest_content` ET `lock_content`.
  - **GROS PROJETS**: Si `package-lock.json` est trop volumineux (>1000 lignes), **minifiez-le** avant l'envoi :
    - Commande: `cat package-lock.json | jq 'del(.packages[].resolved, .packages[].integrity, .packages[].engines, .packages[].funding)'`
    - Ou via Node: `node -e 'const fs=require("fs");const l=JSON.parse(fs.readFileSync("package-lock.json"));delete l.packages;console.log(JSON.stringify(l))'` (Attention: npm audit a besoin de la structure `packages` mais sans les métadonnées lourdes).
    - Utilisez le JSON minifié dans `lock_content`.
  - Exemple: `manifest_content: "<package.json>", lock_content: "<minified-lock.json>"`
  - Supporte: requirements.txt, pyproject.toml, package.json

---

## FAQ from Collègue MCP

- Collègue supporte-t-il OAuth/Keycloak?
  - Oui. Collègue peut valider des Bearer tokens via JWKS. Variables principales: `OAUTH_ENABLED=true`, `OAUTH_ISSUER`, `OAUTH_JWKS_URI`, `OAUTH_REQUIRED_SCOPES`.
  - Recommandation Docker (Keycloak): utilisez l’URL interne Docker pour JWKS (`http://keycloak:8080/.../certs`) et l’URL publique pour l’issuer (`http://localhost:4123/...`). Scopes requis: `mcp.read`, `mcp.write`.

- Comment configurer le LLM?
  - Par défaut via `.env` (`LLM_API_KEY`, `LLM_MODEL`). Vous pouvez aussi surcharger via MCP headers `X-LLM-Api-Key` et `X-LLM-Model`, ou via la configuration MCP du client (Windsurf `mcp_config.json`).

- Quels endpoints utiles côté HTTP?
  - Santé: `/_health`
  - Découverte OAuth (authorization server): `/.well-known/oauth-authorization-server`
  - Découverte OAuth (protected resource MCP): `/.well-known/oauth-protected-resource`

- Quels clients MCP sont compatibles?
  - Windsurf (recommandé), Cursor, Claude Desktop et autres agents MCP avec transport HTTP.

---

## Server Config

Option A — HTTP direct (recommandé):

```json
{ "mcpServers": { "collegue": { "serverUrl": "http://localhost:8088/mcp/", "headers": { "Accept": "application/json, text/event-stream", "Content-Type": "application/json", "X-LLM-Model": "openai/gpt-4o-mini", "X-LLM-Api-Key": "sk-or-v1-xxx" }, "transport": "http" } } }
```

Notes:
- `serverUrl` peut être `http://localhost:8088/mcp/` lorsque Collègue est derrière nginx (Docker Compose par défaut). Si vous lancez Collègue localement sans proxy, utilisez l’URL/port exposés par `FastMCP`.
- Les headers `X-LLM-Model` et `X-LLM-Api-Key` sont optionnels; ils permettent de surcharger le modèle/la clé pour la session courante.

Option B — Configuration Cursor (streamable-http):

```json
{ "mcpServers": { "collegue": { "url": "http://localhost:8088/mcp/", "headers": { "Accept": "application/json, text/event-stream", "Content-Type": "application/json" }, "transport": "streamable-http" } } }
```

Option C — OAuth (Keycloak) variables d’environnement côté serveur:

```json
{ "OAUTH_ENABLED": "true", "OAUTH_ISSUER": "http://localhost:4123/realms/master", "OAUTH_AUTH_SERVER_PUBLIC": "http://localhost:4123/realms/master", "OAUTH_JWKS_URI": "http://keycloak:8080/realms/master/protocol/openid-connect/certs", "OAUTH_REQUIRED_SCOPES": ["mcp.read", "mcp.write"], "PROVISION_CLIENT_ID": "windsurf-client" }
```

Remarques OAuth:
- Utilisez l’URL interne Docker pour `OAUTH_JWKS_URI` et l’URL publique pour `OAUTH_ISSUER`.
- Les clients pourront découvrir automatiquement l’issuer via `/.well-known/oauth-protected-resource`.

---

## Examples

- Explication de code (Python):
```json
{ "tool": "code_explanation", "request": { "code": "def fibonacci(n):\n    if n <= 1: return n\n    return fibonacci(n-1) + fibonacci(n-2)", "language": "python", "detail_level": "detailed" } }
```

- Génération de code (TypeScript):
```json
{ "tool": "code_generation", "request": { "description": "Contrôleur REST pour gérer les utilisateurs (CRUD)", "language": "typescript", "constraints": ["types stricts", "gestion d'erreurs"] } }
```

- Refactoring (simplify):
```json
{ "tool": "code_refactoring", "request": { "code": "if (user.age > 18) { if (user.hasLicense) { if (user.hasInsurance) { return true } } } return false", "language": "javascript", "refactoring_type": "simplify" } }
```

- Documentation (Markdown):
```json
{ "tool": "code_documentation", "request": { "code": "class Calculator {\n  add(x, y) { return x + y }\n}", "language": "javascript", "doc_format": "markdown", "doc_style": "standard", "include_examples": true } }
```

- Génération de tests (pytest):
```json
{ "tool": "test_generation", "request": { "code": "class Calculator:\n    def add(self, a, b):\n        return a + b", "language": "python", "test_framework": "pytest", "coverage_target": 0.9 } }
```

---

Build notes:
- Implémentation principale: `collegue/app.py` (FastMCP) et enregistrement dynamique des outils via `collegue/tools/__init__.py`
- Outils: `code_explanation`, `code_generation`, `code_documentation`, `code_refactoring`, `test_generation`, `run_tests`, `secret_scan`, `dependency_guard`
- Santé: `/_health` (via wrapper HTTP de compatibilité)
- OAuth: `.well-known` endpoints exposés lorsque `OAUTH_ENABLED=true`
