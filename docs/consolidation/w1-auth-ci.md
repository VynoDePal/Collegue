# Vague 1 (B) — OAuth fail-closed, profil réseau local, CI fidèle

Ce document décrit les contrats livrés par le lot B de la consolidation. Il est la
référence pour la relecture et pour les opérateurs.

## 1. OAuth : démarrage fail-closed

**Contrat** : `OAUTH_ENABLED=true` est une exigence. Si l'authentification demandée ne peut
pas être établie, `collegue.app` **ne démarre pas** : l'import lève `OAuthConfigurationError`
(sous-classe de `RuntimeError`) **avant** de construire l'application FastMCP, donc avant de
pouvoir servir quoi que ce soit d'anonyme (un test espionne la construction de `FastMCP`).

| Situation | Avant | Maintenant |
|---|---|---|
| `JWTVerifier` non importable | avertissement, serveur **sans auth** | démarrage refusé |
| constructeur `JWTVerifier` en erreur | erreur journalisée, serveur **sans auth** | démarrage refusé (cause chaînée) |
| `OAUTH_ENABLED=true`, ni `OAUTH_JWKS_URI` ni `OAUTH_PUBLIC_KEY` | avertissement, serveur **sans auth** | démarrage refusé (déjà par la validation de `Settings`, puis par le constructeur) |
| valeurs vides ou blanches (`OAUTH_JWKS_URI`, `OAUTH_PUBLIC_KEY`, `OAUTH_ISSUER`) | clé blanche acceptée | traitées comme absentes → démarrage refusé |
| `OAUTH_ISSUER` absent | refusé par `Settings` | refusé aussi par le constructeur (défense en profondeur) |
| `OAUTH_ALGORITHM` vide/blanc | non contrôlé | démarrage refusé (`Settings` et constructeur) |
| `OAUTH_ALGORITHM`, `OAUTH_REQUIRED_SCOPES` | lus mais **ignorés** | transmis tels quels (après normalisation) à `JWTVerifier(algorithm=…, required_scopes=…)` |
| configuration valide (JWKS ou clé publique) | auth active | inchangé : requêtes sans jeton → `401` + `WWW-Authenticate: Bearer` |
| `OAUTH_ENABLED=false` (défaut) | pas d'auth | inchangé, mais **explicite** : journal « mode local explicite, SANS authentification » |

Implémentation : `collegue/core/server_auth.py` (`build_auth_provider(settings)`), appelé une
seule fois à l'import de `collegue/app.py`.

Avertissement d'exposition : en mode local, si `HOST` ou `COLLEGUE_PUBLISH_HOST` n'est pas
une adresse loopback, un `WARNING` rappelle qu'OAuth doit être activé avant toute exposition
distante. Ce n'est **pas** un refus (décision de politique laissée au manager).

## 2. `entrypoint.sh` (mode HTTP) : le statut du conteneur est fidèle

| Événement | Comportement |
|---|---|
| `fastmcp run` sort avec le code N avant d'être prêt (dont OAuth fail-closed) | le conteneur sort avec **N** ; jamais converti en 0 par le nettoyage ; health server arrêté |
| `fastmcp run` sort avec 0 avant d'être prêt | échec (code 1) |
| MCP vivant mais qui ne répond pas dans `MCP_READY_ATTEMPTS` | échec (code 1), aucune bannière de succès |
| health server qui ne devient pas prêt / sort au démarrage | échec ; son code est propagé ; le MCP n'est pas lancé |
| health server qui meurt en service | échec (son code, ou 1), le MCP est arrêté |
| MCP qui sort seul après avoir été prêt | son code exact est restitué ; health server arrêté |
| SIGTERM / SIGINT (`docker stop`) | arrêt propre, code 0, **aucun** processus fils ne survit |

- « Prêt » = le **health server ET le MCP** répondent. Le MCP est prêt quand une requête
  `initialize` **complète** (`POST /mcp/`, `Accept: application/json, text/event-stream`) reçoit
  un **2xx** ou le refus d'authentification attendu d'un endpoint protégé (**401/403**).
  Sont rejetés : `000` (rien à l'écoute), `404` (mauvais chemin), `405`/`406` (mauvais contrat),
  `400`, `408`, `429` et `5xx`. La bannière « All services started successfully! » n'apparaît
  qu'à ce moment.
- Réglages : `COLLEGUE_APP_DIR` (`/app`), `READY_POLL_INTERVAL` (1 s), `HEALTH_READY_ATTEMPTS`
  (30), `MCP_READY_ATTEMPTS` (120).
- Sous-commande `./entrypoint.sh mcp-ready` : ne démarre rien, code 0 si le MCP répond selon le
  critère ci-dessus, 1 sinon. Elle est utilisée par le healthcheck Compose.
- Le mode `stdio` est inchangé (`exec fastmcp run … --transport stdio`).

## 3. Profil réseau local

Toutes les publications hôte du Compose (**4121, 4122, 4123, 4125, 8088**) sont liées au
loopback par défaut. Trois variables **indépendantes** (défaut `127.0.0.1`) évitent qu'ouvrir
le MCP ouvre aussi ce qui n'a pas d'authentification :

| Variable | Ports | Remarque |
|---|---|---|
| `COLLEGUE_PUBLISH_HOST` | 4121 (MCP), 4122 (health, well-known OAuth), 8088 (nginx : proxifie seulement `/mcp/`, `/_health`, `/.well-known/`) | surface MCP |
| `COLLEGUE_DASHBOARD_PUBLISH_HOST` | 4125 | dashboard Streamlit, **aucune authentification** |
| `COLLEGUE_KEYCLOAK_PUBLISH_HOST` | 4123 | Keycloak en `start-dev`, à ne pas exposer tel quel |

Les conteneurs écoutent toujours sur `0.0.0.0` **en interne** (`MCP_HOST`, `entrypoint.sh`,
`--server.address`) : le réseau Compose et le mapping de ports fonctionnent comme avant.
`Settings.HOST` vaut `127.0.0.1` par défaut pour `python collegue/app.py`.

Le healthcheck de `collegue-app` exige le health server **et** `entrypoint.sh mcp-ready` : le
health server seul ne rend plus le conteneur « sain » (`nginx` et le dashboard attendent donc
le MCP via `depends_on: service_healthy`).

### Demander une exposition distante du MCP, avec OAuth

Dans `.env` :

```
COLLEGUE_PUBLISH_HOST=0.0.0.0          # ou l'IP d'une interface précise
OAUTH_ENABLED=true
OAUTH_ISSUER=https://idp.example.com/realms/collegue
OAUTH_JWKS_URI=https://idp.example.com/realms/collegue/protocol/openid-connect/certs
OAUTH_AUDIENCE=collegue                # facultatif
```

Puis vérifier :

1. `docker compose config` → `host_ip: 0.0.0.0` pour 4121/4122/8088 seulement (dashboard et
   Keycloak restent sur `127.0.0.1` tant que leur propre variable n'est pas posée) ;
2. `docker compose up -d` → le conteneur reste sain ; si OAuth est inutilisable il **sort en
   code non nul** (voir `docker compose logs collegue-app`, `OAuthConfigurationError`) ;
3. une requête MCP sans jeton doit répondre `401`.

`/_health` et `/.well-known/oauth-protected-resource` (port 4122, aussi via nginx) restent non
authentifiés par conception (statut et découverte OAuth). Un reverse proxy TLS reste recommandé.
Pour `python collegue/app.py` hors Docker : `HOST=0.0.0.0`, mêmes exigences.

## 4. CI nightly (`integration-nightly.yml`)

**Défaut corrigé** : `pytest … | tee` sous `bash -e` sans `pipefail` renvoyait le statut de
`tee` (0). Le run 34577743360 était `success` avec `1 failed, 7 passed, 7 skipped`.

- L'étape « Run integration suite » lit `PIPESTATUS` : le code de sortie de pytest est
  propagé **exactement** (1, 2, 3, 5…), indépendamment du shell du runner ; stderr est fusionné
  dans `pytest-integration.log`. Un échec d'écriture du journal fait aussi échouer l'étape.
- « Bilan » (`scripts/ci_integration_bilan.py junit`) lit le **JUnit XML** (jamais le wording
  de pytest) et échoue si : rapport absent/illisible/vide/avec DTD, test en échec ou en
  erreur, **aucun test exécuté** (tout skippé), ou étape pytest non `success`. Les tests
  skippés sont listés avec leur raison dans le résumé du run et signalés comme *non exécutés*.
- Rapport et journal sont téléversés `if: always()` (même en échec).
- Job « Statut du produit E2E (jamais vert par skip) » : `product-e2e` est un opt-in
  (`vars.INTEGRATION_E2E_ENABLED`). Quand il est ignoré, le résumé et une annotation
  `::warning::` annoncent **NON EXÉCUTÉ — ce nightly ne prouve PAS le cycle produit**.
  `failure`, `cancelled` ou une valeur inconnue font échouer le job.

Le test nightly réel qui attend 2 délégations et en obtient 3 n'a **pas** été modifié : le
défaut corrigé ici est le statut CI, pas l'assertion. Il est désormais rendu visible.

## 5. Smoke Docker (`tests.yml`, job « Docker build »)

**Défaut corrigé** : `docker run … &` + `sleep 5` + `docker logs || true` + `docker stop || true`
était vert quoi qu'il arrive. Le log du run 29215033828 (main `51ab3fc`) s'arrête sur
« Validation du modèle LLM 'test-model' (provider=gemini) en cours… » : le MCP n'avait pas fini
de démarrer, et avec `provider=gemini` + fausse clé l'application tente un appel réseau
de validation.

`scripts/ci_docker_smoke.sh IMAGE` :

- lance le conteneur **sans réseau** (`--network none`) avec `LLM_PROVIDER=anthropic` et une
  clé factice (ce provider ne valide rien à distance au démarrage ; garde-fou :
  `test_anthropic_startup_validation_makes_no_network_call`) → aucun appel LLM/API possible ;
- sonde via `docker exec … curl` sur le loopback du conteneur : santé `:4122/_health`
  (`{"status":"ok"}`), MCP `initialize` sur `:4121/mcp/` (HTTP 200 + résultat) **et** la
  commande exacte du healthcheck Compose (un test garantit qu'elle est identique à celle de
  `docker-compose.yml`) ;
- attente bornée (`SMOKE_TIMEOUT_SECONDS`, 120 s ; `SMOKE_POLL_INTERVAL_SECONDS`, 2 s) ;
- échoue si le conteneur **sort** (même avec le code 0), si la santé est invalide, si le délai
  est dépassé, ou si le conteneur meurt juste après être devenu prêt ;
- pas de `--rm` : les logs d'un conteneur crashé survivent ; ils sont toujours écrits dans
  `smoke-logs/collegue-smoke.log`, affichés (200 dernières lignes) et téléversés
  (`docker-smoke-logs`, `if: always()`) ;
- nettoyage (`docker stop`, `docker rm -f`) dans un `trap EXIT` qui **restitue le statut
  initial** ; un échec de nettoyage n'est qu'un avertissement.

| Code | Sens |
|---|---|
| 0 | prêt |
| 2 | usage / variable invalide |
| 10 | `docker run` en échec |
| 11 | conteneur sorti avant d'être prêt |
| 12 | pas prêt dans le délai |
| 13 | mort juste après être devenu prêt |

Le smoke vérifie le profil **local** (OAuth désactivé). Avec OAuth activé, `initialize` sans
jeton répond `401` : c'est « prêt » pour l'entrypoint et le healthcheck, mais pas pour la sonde
stricte du smoke (HTTP 200) ; un smoke OAuth exigerait un jeton de test.

Les noms des checks requis sont inchangés : `Ruff`, `Pytest (Python 3.11)`,
`Pytest (Python 3.12)`, `Dependency audit`, `Docker build` (garde-fou :
`test_required_pull_request_check_names_are_unchanged`).

## 6. Tests

| Fichier | Couvre |
|---|---|
| `tests/test_app_oauth_fail_closed.py` | démarrage réel (sous-processus) : local, OAuth JWKS/clé publique (401 sans jeton), constructeur en erreur, import absent, clé absente/vide/blanche ; jamais de `FastMCP` construit sur un refus |
| `tests/test_server_auth.py` | `build_auth_provider`, valeurs blanches, issuer, algorithme, scopes exacts, loopback, avertissements d'exposition |
| `tests/test_entrypoint_lifecycle.py` | `entrypoint.sh` avec faux `fastmcp`/health/`curl` : codes exacts, jamais « prêt » à tort, timeouts, mort du health server, SIGTERM, aucun processus survivant, `mcp-ready`, stdio |
| `tests/test_docker_compose_config.py` | cinq publications en loopback, variables séparées, `docker compose config` réel, healthcheck MCP |
| `tests/test_ci_nightly_pipeline.py` | étape pytest du workflow contre un faux `pytest` (codes 1/2/3/5), bilan JUnit, statut E2E |
| `tests/test_ci_docker_smoke.py` | script de smoke contre un `docker` factice : succès, crash, jamais prêt, MCP/healthcheck indisponible, mort après prêt, `docker run` KO, nettoyage, logs |

## 7. Limites connues

- Les scopes requis sont désormais **imposés** : un déploiement dont les jetons n'ont pas les
  scopes de `OAUTH_REQUIRED_SCOPES` verra ses requêtes refusées. Vérifier la configuration
  Keycloak avant mise à jour.
- Le smoke n'a pas été exécuté sur une vraie image dans cette vague (pas de build Docker
  local) : la preuve réelle sera le job « Docker build » de la CI distante. Il a été exécuté
  contre le vrai `entrypoint.sh` et le vrai serveur via un shim `docker` (voir le rapport).
- `E2E produit` reste opt-in : tant que `INTEGRATION_E2E_ENABLED` n'est pas `true`, le nightly ne
  prouve pas le cycle produit (explicitement annoncé, mais le run reste vert).
- `tests/test_entrypoint.py::TestHealthServer` lance un health server sur le port 4122 de l'hôte :
  des exécutions parallèles de la suite complète peuvent se marcher dessus.
