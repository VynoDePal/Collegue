# Vague 1 (B) — OAuth fail-closed, profil réseau local, CI fidèle

Ce document décrit les contrats livrés par le lot B de la consolidation. Il est la
référence pour la relecture et pour les opérateurs.

## 1. OAuth : démarrage fail-closed

**Contrat** : `OAUTH_ENABLED=true` est une exigence. Si l'authentification demandée ne peut
pas être établie, `collegue.app` **ne démarre pas** (l'import lève `OAuthConfigurationError`,
sous-classe de `RuntimeError`). Il n'existe plus de repli silencieux sur `auth=None`.

| Situation | Avant | Maintenant |
|---|---|---|
| `OAUTH_ENABLED=true`, constructeur `JWTVerifier` en erreur | erreur journalisée, serveur **sans auth** | démarrage refusé (`OAuthConfigurationError`, cause chaînée) |
| `OAUTH_ENABLED=true`, `JWTVerifier` non importable | avertissement, serveur **sans auth** | démarrage refusé |
| `OAUTH_ENABLED=true`, ni `OAUTH_JWKS_URI` ni `OAUTH_PUBLIC_KEY` | avertissement, serveur **sans auth** | démarrage refusé (aussi refusé plus tôt par la validation de `Settings`) |
| `OAUTH_ENABLED=true`, configuration valide (JWKS ou clé publique) | auth active | inchangé : requêtes sans jeton → `401` + `WWW-Authenticate: Bearer` |
| `OAUTH_ENABLED=false` (défaut) | pas d'auth | inchangé, mais **explicite** : journal « mode local explicite, SANS authentification » |

Implémentation : `collegue/core/server_auth.py` (`build_auth_provider(settings)`), appelé une
seule fois à l'import de `collegue/app.py`. Aucun effet de bord à l'import du module
lui-même (testable sans démarrer l'application).

Dans Docker, le conteneur sort avec un code non nul (`entrypoint.sh` propage le statut de
`fastmcp run`) ; avec `restart: always`, Compose relance en boucle : c'est voulu, un service
sans authentification ne doit pas répondre.

Avertissement d'exposition : en mode local, si `HOST` ou `COLLEGUE_PUBLISH_HOST` n'est pas
une adresse loopback, un `WARNING` rappelle qu'OAuth doit être activé avant toute exposition
distante. Ce n'est **pas** un refus (décision de politique laissée au manager).

## 2. Profil réseau local

- `docker-compose.yml` publie **tous** les ports hôte (`4121`, `4122`, `4125`, `4123`, `8088`)
  sur `${COLLEGUE_PUBLISH_HOST:-127.0.0.1}`. Vérifié avec `docker compose config` :
  `host_ip: 127.0.0.1` par défaut.
- Les conteneurs écoutent toujours sur `0.0.0.0` **en interne** (`MCP_HOST`, `entrypoint.sh`,
  `--server.address`) : nécessaire au mapping de ports Docker. Le fonctionnement
  intra-conteneur (healthcheck sur `localhost:4122`, réseau Compose) est inchangé.
- `Settings.HOST` vaut désormais `127.0.0.1` par défaut : `python collegue/app.py` n'écoute
  plus sur toutes les interfaces sans choix explicite.

### Exposition distante explicite

1. `COLLEGUE_PUBLISH_HOST=0.0.0.0` (ou l'IP d'une interface précise) dans `.env`.
2. **OAuth obligatoire** : `OAUTH_ENABLED=true`, `OAUTH_ISSUER` et `OAUTH_JWKS_URI` (ou
   `OAUTH_PUBLIC_KEY`). Voir `.env.example`.
3. Un reverse proxy TLS devant le service reste recommandé ; Keycloak en `start-dev` ne doit
   pas être exposé tel quel.

Pour `python collegue/app.py` hors Docker : `HOST=0.0.0.0`, mêmes exigences.

## 3. CI nightly (`integration-nightly.yml`)

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
- Nouveau job « Statut du produit E2E (jamais vert par skip) » : `product-e2e` est un opt-in
  (`vars.INTEGRATION_E2E_ENABLED`). Quand il est ignoré, le résumé et une annotation
  `::warning::` annoncent **NON EXÉCUTÉ — ce nightly ne prouve PAS le cycle produit**.
  `failure`, `cancelled` ou une valeur inconnue font échouer le job.

Le test nightly réel qui attend 2 délégations et en obtient 3 n'a **pas** été modifié : le
défaut corrigé ici est le statut CI, pas l'assertion. Il est désormais rendu visible.

## 4. Smoke Docker (`tests.yml`, job « Docker build »)

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
  (`{"status":"ok"}`) **et** MCP `initialize` sur `:4121/mcp/` (HTTP 200 + résultat) ;
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
jeton répond `401` et la sonde le déclare non prêt : c'est attendu, un smoke OAuth exigerait un
jeton de test.

Les noms des checks requis sont inchangés : `Ruff`, `Pytest (Python 3.11)`,
`Pytest (Python 3.12)`, `Dependency audit`, `Docker build` (garde-fou :
`test_required_pull_request_check_names_are_unchanged`).

## 5. Tests

| Fichier | Couvre |
|---|---|
| `tests/test_app_oauth_fail_closed.py` | démarrage réel (sous-processus) : local, OAuth JWKS/clé publique (401 sans jeton), constructeur en erreur, import absent, matériel de clé manquant |
| `tests/test_server_auth.py` | `build_auth_provider`, loopback, avertissements d'exposition, `HOST`/`COLLEGUE_PUBLISH_HOST` |
| `tests/test_docker_compose_config.py` | publication loopback (statique, interpolation, `docker compose config` réel) |
| `tests/test_ci_nightly_pipeline.py` | exécute l'étape pytest du workflow contre un faux `pytest` (codes 1/2/3/5), bilan JUnit, statut E2E |
| `tests/test_ci_docker_smoke.py` | script de smoke contre un `docker` factice : succès, crash, jamais prêt, MCP indisponible, mort après prêt, `docker run` KO, nettoyage, logs |

## 6. Limites connues

- `OAUTH_REQUIRED_SCOPES` et `OAUTH_ALGORITHM` sont lus par `Settings` mais **non transmis** à
  `JWTVerifier` (`required_scopes`, `algorithm`) : les scopes configurés ne sont pas imposés.
  Comportement inchangé (hors périmètre de cette vague, imposer les scopes peut verrouiller des
  déploiements existants) ; à décider.
- `entrypoint.sh` affiche « All services started successfully! » même si le MCP est déjà mort, et
  ne tue pas le health server dans ce cas (sans conséquence dans un conteneur : le PID 1 sort).
  Le code de sortie, lui, est bien non nul. Fichier hors périmètre B.
- Le smoke n'a pas été exécuté sur une vraie image dans cette vague (pas de build Docker
  local) : la preuve réelle sera le job « Docker build » de la CI distante. Il a été exécuté
  contre le vrai `entrypoint.sh` et le vrai serveur via un shim `docker` (voir le rapport).
- `E2E produit` reste opt-in : tant que `INTEGRATION_E2E_ENABLED` n'est pas `true`, le nightly ne
  prouve pas le cycle produit (explicitement annoncé, mais le run reste vert).
