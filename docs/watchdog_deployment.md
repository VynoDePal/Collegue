# Déploiement du Watchdog Autonome

Le Watchdog est un service autonome qui surveille Sentry et crée des Pull Requests sur GitHub pour corriger les erreurs.

## Pré-requis

### Variables d'environnement
Ajoutez ces variables à votre fichier `.env` ou à votre configuration de déploiement (Coolify, Portainer, etc.) :

```bash
# Sentry
SENTRY_ORG=votre-organisation-sentry
SENTRY_AUTH_TOKEN=votre-token-sentry  # Scopes: project:read, issue:read, event:read, org:read

# GitHub
GITHUB_TOKEN=votre-pat-github      # Scopes: repo (pour créer branches/PRs)
# GITHUB_OWNER=...                 # Optionnel: force le propriétaire du repo (sinon auto-détecté)
```

## Déploiement

Le service est intégré dans `docker-compose.yml` sous le nom `collegue-watchdog`.

### Lancement
```bash
docker-compose up -d collegue-watchdog
```
Ou pour tout relancer :
```bash
docker-compose up -d --build
```

## Vérification

Pour voir si le watchdog tourne et détecte les projets :
```bash
docker-compose logs -f collegue-watchdog
```

Vous devriez voir des logs comme :
```
INFO - 🔍 Démarrage du cycle de Self-Healing Multi-Projets...
INFO - 📡 Récupération des données pour l'org: votre-org
INFO - ✅ X projets et Y dépôts liés trouvés.
```

---

## Runbook E2E (issue #208)

La suite [tests/test_watchdog_e2e.py](../tests/test_watchdog_e2e.py) couvre le cycle complet (Sentry → ContextPack → LLM → fuzzy match → AST validation → garde anti-destruction → PR GitHub). Elle tourne en deux modes.

### Mode mock (défaut, tourne en CI)

Tous les clients externes sont mockés au niveau `MagicMock.side_effect` pour Sentry/GitHub et `patch(generate_text)` pour le LLM. Aucun token requis.

```bash
PYTHONPATH=. pytest tests/test_watchdog_e2e.py -v
```

Résultat attendu : 9 passed, 1 deselected (le test `test_live_watchdog_cycle_against_sandbox` est marqué `@pytest.mark.integration` et skippé par défaut via `addopts = -m 'not integration'` dans [pyproject.toml](../pyproject.toml)).

Scénarios couverts :
1. **Trivial fix** (AttributeError sur attribut manquant, match exact) → PR créée
2. **Fuzzy match** (indentation dérivée, score difflib ≥ 0.6) → PR créée avec le bon contenu
3. **Refus > 50%** (LLM tente d'effacer le fichier) → ni `create_branch`, ni `update_file`, ni `create_pr` appelés
4. **UserConfigRegistry blacklist** (7 placeholders refusés : `your-org`, `my-organization`, `test-org`, etc.)
5. **Multi-utilisateurs** (2 configs enregistrées résolvent leurs propres tokens sans fallback env)

Fixtures :
- [tests/fixtures/watchdog/sample_sentry_issue.json](../tests/fixtures/watchdog/sample_sentry_issue.json) — payload Sentry anonymisé (AttributeError)
- [tests/fixtures/watchdog/sample_source.py](../tests/fixtures/watchdog/sample_source.py) — module Python fautif

### Mode live (`pytest -m integration`, manuel)

Pour valider le cycle contre un vrai Sentry sandbox + un vrai repo GitHub jetable + un vrai quota LLM, configurer :

| Variable | Valeur |
|---|---|
| `SENTRY_AUTH_TOKEN` | Token Sentry avec scopes `project:read`, `issue:read`, `event:read` — idéalement sur un compte Sentry Developer (gratuit) avec un projet de test |
| `SENTRY_ORG` | Slug de l'organisation sandbox (éviter `test-org` — blacklist `PLACEHOLDER_ORGS`) |
| `GITHUB_TOKEN` | PAT avec scope `repo` sur un **repo dédié et jetable** (le Watchdog y pushera des branches `fix/sentry-*`) |
| `GITHUB_OWNER` + `GITHUB_REPO` | Propriétaire et nom du repo jetable |
| `LLM_API_KEY` | Clé Gemini avec quota suffisant (~5-10 req/test : web search + analyse + éventuelles retries) |

Puis :

```bash
pytest -m integration tests/test_watchdog_e2e.py -v
```

> ⚠️ Le test `test_live_watchdog_cycle_against_sandbox` est actuellement un placeholder qui skippe proprement si les variables manquent. Le runner live complet est une prochaine itération (voir commentaire dans le test).

### Mécanique de déduplication

Le Watchdog maintient `_processed_issues` en mémoire process pour éviter de boucler sur la même issue. La fixture `_reset_processed_issues` de la suite E2E vide ce set entre chaque test — **en production, un redémarrage du container efface aussi la mémoire**, ce qui peut provoquer une re-tentative sur une issue déjà traitée. Non bloquant (la PR existe déjà, GitHub refuse la création de branche dupliquée), mais à garder en tête lors d'un crash-loop.
