# Vague 5 — organisation, contrats et checklist d'intégration (C)

**État exact : les vagues 1 à 4 sont livrées sur `main` ; la validation réelle avec modèles est restée INCOMPLÈTE (campagne W4 refusée au
préflight avant toute émission) ; la vague 5 est autorisée et en cours ; A et B développent en parallèle, rien n'est intégré ni publié ;
aucune campagne réelle n'a été démarrée, aucune clé n'existe.** Ce document est rédigé AVANT les livraisons de A et B : il consigne la
répartition, les contrats du manager (`briefs/w5-common.md`), la préparation de C (CI, image, fixture, workflow) et la checklist
d'intégration. Il ne décrit aucun résultat du travail de A ou B. Règles générales : [`AGENTS.md`](../../AGENTS.md) ; protocole :
[`protocole.md`](protocole.md) ; vague précédente : [`w4-integration.md`](w4-integration.md).

- **Base commune** : `main` = `869ed3c8936b2bfaa72ac9c64746b1420a827825` (PR #612, arbre `2825ca3775d8f5452ce0bb946763a8b8fa0d8500`, parent
  unique `5c1cbf5`, run push `37903659594` : cinq checks verts, 4 278 passés / 38 ignorés / 9 désélectionnés par Python, PostgreSQL réel
  21 / 23 / 5). La campagne W4 `37905627368` s'est arrêtée à `P06-worker-capacity` (`unbounded_transport`) : 0 appel modèle, 0 USD ;
  **R04 (amélioration) et R05 (incident/rollback) n'étaient pas câblés dans le lanceur réel**. La W5 est une nouvelle campagne autorisée, pas
  une relance.
- **Branches** : A `codex/consolidation-w5-a`, B `codex/consolidation-w5-b`, C `codex/consolidation-w5-c` (même base).
- **Objet** : parcours réel plan approuvé → 3 BUILD dépendants → IMPROVE → incident contrôlé → rollback Phase 5 → acquittement/reprise,
  **2 USD / 250 000 tokens / 900 s GLOBAUX**, `gemma-4-31b-it` pour tous les rôles, repli du CODEUR seulement `gemma-4-26b-a4b-it`, strict,
  sans dispense ni mode advisory/abonnement de substitution.
- **Non-objectifs** : lancer ou dispatcher quoi que ce soit avant ordre exact du manager ; toucher `main` ou la graine de la fixture ;
  changer les cinq noms de checks ou les protections de `Collegue` ; relire ou changer un secret.

## 1. Propriété des fichiers

| Qui | Possède |
|---|---|
| **A** | `collegue/core/llm/**`, `collegue/broker/**`, `state/{models,manager,budget_ledger}.py`, migration additive `0013`, `config.py`, `executor/{worker_budget,openhands_sdk_agent,oh_runner,agent}.py`, le relais `executor/oh_broker_relay.py`, `sandbox/executor.py`, `pilot/runtime.py`, `monitoring/pricing.py` si nécessaire, `.env.example`, tests budget/transport/routage et `tests/test_w5_broker*.py`, `docs/consolidation/w5-broker.md` |
| **B** | `pilot/w4_business.py`, `pilot/w5_business*`, `pilot/nightly_e2e.py` (cycle de vie/bootstrap seulement), `tests/w4_business*`, `tests/test_w4_business*`, `tests/test_w5_business*.py`, `docs/consolidation/w5-business.md`, `tests/fixtures/w5-business/**` |
| **C** | `AGENTS.md`, `CLAUDE.md`, `docs/consolidation/{protocole,w5-integration}.md`, **tous** les workflows, Dockerfiles, `pyproject.toml`, `locks/**` et scripts CI, `scripts/w5_*`, `scripts/ci_w5_*`, `tests/test_w5_ci*.py`, `tests/test_w5_integration*.py`, préparation GitHub de la fixture |

Un fichier a un seul propriétaire. Un besoin sur le fichier d'un autre rôle s'écrit dans `reports/w5-{a,b,c}-interfaces.md`, il ne s'édite pas.
Aucun patch de comportement de A ou B par C en silence ; un raccord mécanique est annoncé.

## 2. Contrats entre lots (rappel du brief commun)

- **Transport** : `LLM_TRANSPORT = direct` (défaut historique) ou `budget_broker`. La destination reste `gemini`/Google, jamais reclassée
  OpenAI parce que le relais parle Chat Completions. Tous les rôles passent par le courtier ; aucun credential dans le sandbox du codeur.
- **Courtier (A)** : session liée durablement (projet/scope, rôle, liste blanche, allocation, échéance) ; `countTokens` du
  `generateContentRequest` complet → réservation → `generateContent` du même objet ; un seul candidat ; sortie bornée ; outils et sortie
  structurée couverts ; flux, médias et URL arbitraires refusés ; faux fournisseur injectable **dans les tests seulement**. Comptabilité :
  allocation parent conservée, sous-registre enfant durable, consolidation unique depuis l'autorité du courtier (jamais depuis la sortie du
  worker) ; toute inconnue bloque le parent ; échéance persistée dès l'ouverture réelle, sans remise à zéro.
- **Isolation du codeur** : `--network none` ; seul un répertoire ne contenant que `broker.sock` est monté en lecture seule ; le relais embarqué
  `/opt/oh_broker_relay.py` (stdlib, boucle locale → socket Unix, plafond de volume, aucune destination fournie par la requête) est la
  seule voie. La clé ne vit que dans le service du courtier (processus de confiance de l'étape de campagne).
- **Fixture (C → B)** : manifeste `collegue-fixture-bootstrap/1` — `repository`, `repository_id`, `seed_sha`, `bootstrap_sha`,
  `approved_files` (chemin → sha256 de ce que le socle **ajoute** à la graine), `required_check = "Fixture tests"`, `check_app_id = 15368`,
  `ruleset_id`, `branch_pattern = refs/heads/collegue-business/*`. B le valide PAR L'API ; il n'est jamais une preuve à lui seul.
  Champ additionnel `check_producer` (voir § 4.2). Le socle n'ajoute **aucune** implémentation des trois tâches.
- **Délai** : les 900 s commencent à l'ouverture réelle Google, avant les canaris des deux Gemma ; aucune nouvelle génération après l'échéance ;
  collecte et nettoyage sans génération peuvent finir ensuite.

## 3. Ce que C a préparé (local, rien de distant)

| Élément | Fichier | Preuve locale |
|---|---|---|
| Plan de fixture hors ligne, application/nettoyage/contre-épreuves sur ordre | `scripts/w5_fixture_bootstrap.py` | `tests/test_w5_ci_fixture.py` (SHA du socle identique à `git`, idempotence, collisions, nettoyage ciblé, contre-épreuves simulées) |
| Workflow de campagne W5 (évolution de `consolidation-e2e.yml`) | `.github/workflows/consolidation-e2e.yml` | `tests/test_w5_ci_campaign_workflow.py` |
| CI générale : étape PostgreSQL du courtier, preuve du transport dans l'image sans réseau | `.github/workflows/tests.yml`, `scripts/ci_w5_broker_transport.py` | `tests/test_w5_ci_general.py`, `tests/test_w5_ci_transport_proof.py` |
| COPY du relais de A | `docker/sandbox/Dockerfile.openhands` | `tests/test_w5_ci_general.py` (xfail strict tant que A n'est pas intégré) |
| Plan soumis à la revue du manager | `evidence/w5-c-fixture-plan/` (payloads, diff, ordre des appels, contre-épreuves) | `SHA256SUMS` |

## 4. Fixture : conception et points d'attaque

Dépôt `VynoDePal/collegue-e2e-fixture` (id 1298596453), `main` = graine `8e3691d8…` (arbre `c8bffa32…`, ruleset 18840666 intact), PR #4 et #7
étrangères. Le socle est un **commit déterministe** (parent unique = graine) publié sur `collegue-business/bootstrap-w5` ; il ajoute
`.github/workflows/fixture-tests.yml`, `docs/runbook-ops.md` (support R04) et `docs/deploiement.md` (support R05) — octet pour octet les
documents de B — et **ne modifie aucun fichier de la graine** (`docs/export_header.md` est créé par les BUILD, pas par le socle).

### 4.1 Ruleset des bases éphémères

Actif sur `refs/heads/collegue-business/*`, sans bypass : PR obligatoire, check `Fixture tests` **lié à l'application GitHub Actions
(`integration_id` 15368)**, base à jour (`strict_required_status_checks_policy`). `do_not_enforce_on_create` est vrai : sans cela, créer une
base éphémère depuis le socle exigerait un check déjà passé sur un commit qui n'en a pas. Pas de règle `deletion` : le nettoyage de la
campagne supprime ses propres branches. Ni `main` ni son ruleset ne sont concernés.

### 4.2 Pourquoi le check n'est pas celui d'un job

Le workflow est déclenché par `pull_request_target` : GitHub exécute le fichier **de la branche de base**, jamais celui de la tête de la PR (une PR
qui réécrit le workflow ne change pas le contrôle qui la juge ; la garde refuse en plus toute modification de `.github/`). Mais le check
automatique d'un job `pull_request_target` est normalement rattaché à la **base** (`GITHUB_SHA`), pas à la tête (à observer, § 4.4) : la politique de fusion W3 exige un succès sur la tête
exacte. Le job (`Fixture runner`) n'est donc PAS le check requis : sa dernière étape de confiance publie `Fixture tests` par l'API Checks
sur `github.event.pull_request.head.sha`, avec le jeton de l'application Actions (une PAT ne peut pas créer de check-run). La conclusion est
`success` seulement si la garde, le téléchargement des roues ET les tests ont réussi. **Rien de cela n'est tenu pour acquis avant la
contre-épreuve distante** (§ 4.4) ; le manifeste porte `check_producer` (workflow, déclencheur, job, nom publié, application) pour que B
puisse le valider sans confondre job et check.

### 4.3 Exécution du code candidat

Le code du candidat (y compris `requirements.txt`) ne s'exécute jamais sur l'hôte du runner : les roues sont téléchargées par un conteneur sans
privilège (utilisateur 65534, capacités retirées, `no-new-privileges`, système de fichiers en lecture seule, aucun secret, aucun socket
Docker), puis l'installation hors ligne et `pytest` tournent dans un second conteneur **sans réseau**. Le jeton du workflow n'est vu que par la
garde et l'étape de publication, jamais par un conteneur candidat. **Limites** : l'image `python:3.12-slim` est référencée par son étiquette
(à figer par condensat après le premier tirage observé) ; le téléchargement des roues a accès à Internet (sans secret) ; une dépendance
malveillante n'est exécutée que dans le conteneur sans réseau mais peut y fausser le verdict des tests — c'est le rôle des oracles de B, pas
de ce check, d'établir la qualité.

### 4.4 Contre-épreuves distantes (`probe`, sur ordre seulement)

Quatre PR jetables vers des bases de sonde (liste exacte : `evidence/w5-c-fixture-plan/probe-plan.md`) : `green` (le workflow SE DÉCLENCHE et
publie un check de l'application 15368 sur la tête exacte), `red-test` (vrai échec `pytest` → check rouge, PR non fusionnable ; tentative de faux
check vert avec le jeton de campagne → refus de l'API ou check d'une autre application qui ne compte pas), `forged-workflow` (workflow altéré
par la tête → check rouge), `missing-check` (base issue de la graine, sans workflow → aucun check, PR bloquée). Un check absent n'est jamais un
succès. **Incertitudes que la sonde tranche** : les workflows `pull_request_target` partent-ils depuis une base qui n'est pas la branche par
défaut ? le jeton de campagne déclenche-t-il et peut-il publier ? les Actions sont-elles activées ? Si `green` échoue, aucune alternative
conforme (check lié à l'application Actions, `main` immuable) n'est connue : la décision revient au manager.

### 4.5 Ordre des mutations (aucune n'est faite dans cette passe)

`inspect` (lecture) → revue du manager de `evidence/w5-c-fixture-plan/` → `apply --order-token APPLIQUER-W5-FIXTURE-<12 hex>` (arbre, commit,
branche du socle, puis ruleset ; refus de toute collision ou ressource non possédée avant la première écriture) → `verify` → `probe` →
manifeste complété par le `ruleset_id` renvoyé → variable d'environnement GitHub `W5_BOOTSTRAP_MANIFEST_JSON` de l'environnement de campagne.
Retour arrière : `cleanup` ne supprime que la branche du socle et le ruleset **de cette campagne** (identités exactes vérifiées).

## 5. CI générale (cinq checks inchangés : Ruff, Pytest 3.11, Pytest 3.12, Dependency audit, Docker build)

- **Aucun modèle réel** : aucun secret ni clé dans `tests.yml` ; le faux courtier et le faux fournisseur n'existent que dans les tests.
- **PostgreSQL obligatoire** : étape `PostgreSQL broker state` (service `postgres:16`, `test -f`, `test -n`, collecte égale à l'exécution, JUnit
  exigé par `scripts/ci_require_junit.py`). Plancher provisoire 2 ; **à fixer au nombre exact collecté à l'intégration de A**, puis jamais abaissé.
- **Image** : le relais est copié à côté du runner ; `scripts/ci_w5_broker_transport.py` s'exécute dans l'image construite, `--network none`, sans
  secret ni montage (script par l'entrée standard) : le VRAI SDK OpenHands appelle le relais, qui parle à un FAUX courtier sur socket Unix ;
  isolation (seule la boucle locale, aucune connexion externe, aucune variable ressemblant à une clé), relais non proxy, plafond de volume.
  Le script n'a PAS pu être exécuté localement (SDK non installé, par consigne) : sa logique est éprouvée avec un relais de remplacement et un
  faux SDK (`tests/test_w5_ci_transport_proof.py`), sa preuve réelle est celle de la CI distante.
- **À ajouter à l'intégration (dépend de l'interface finale de A)** : une preuve hôte `tests/test_w5_integration_*.py` — vrai courtier et vrai
  serveur de socket de A, faux fournisseur Google, conteneur `--network none` avec montage du répertoire du socket — vérifiant la clé absente
  de l'environnement, des arguments, des montages et des journaux du conteneur, et le décompte des appels reçus par le faux fournisseur.

## 6. Campagne future (à NE PAS lancer sans ordre exact du manager)

Workflow `consolidation-e2e.yml` : `workflow_dispatch` seul, confirmation exacte, identifiant neuf (consommé au lancement par B ; aucun rerun),
environnement GitHub `w5-gemma-campaign` propre à la campagne, clé temporaire `W5_GOOGLE_API_KEY` référencée **une seule fois**, dans l'étape de
campagne, sous le nom `GOOGLE_API_KEY` ; préflight statique et complet sans clé ; enveloppe `2 / 250000 / 900` au niveau du job ; transport
`budget_broker`, `SANDBOX_NETWORK=none` ; aucune variable de prix ; nettoyage `always()` ; rapport ET registre durable déposés `always()` ; un
fichier de sortie contenant la clé est supprimé et la campagne invalidée. Le manager injecte puis retire la clé (même en cas de refus).

## 7. Checklist d'intégration (C, sur SHA figés de A et B et ordre du manager)

1. SHA de A/B = têtes actuelles ; périmètres disjoints ; fusion par SHA sans fast-forward ; relire les diffs, chercher les usages oubliés.
2. **Relais** : `collegue/executor/oh_broker_relay.py` présent ⇒ retirer le marqueur `A_NOT_INTEGRATED` de `tests/test_w5_ci_general.py` (il casse
   par construction) ; confronter l'interface réelle (`start(socket_path, port)`, `MAX_CLIENT_BYTES`, variables) à `scripts/ci_w5_broker_transport.py`.
3. **Plancher PostgreSQL** du courtier = nombre exact de tests collectés dans `tests/test_w5_broker_postgres.py`.
4. **Tests de B à aligner par B** (fichiers de B) : `tests/test_w4_business_workflow.py` — retirer le `xfail` de l'enveloppe, adapter la liste des étapes
   détentrices de la clé à `GOOGLE_API_KEY` ; `_workflow_jobs` de `w5_business.py` n'accepte que `pull_request` et le nom de job comme check : il doit
   reconnaître le `check_producer` du manifeste (§ 4.2).
5. **Manifeste** : recoupement octet pour octet des deux documents avec `tests/fixtures/w5-business/docs/` (test `test_the_example_documents_are_byte_identical…`,
   sauté tant que B n'est pas intégré), puis régénération du plan (les SHA changent si le moindre fichier change) et nouvelle soumission au manager.
6. Suites complètes Python 3.11 et 3.12, Ruff complet, `python scripts/locks.py check`, PostgreSQL réel sans skip, image et preuves distantes sur le SHA publié.
7. Observer les cinq checks et les revues (une revue absente est dite absente) ; rapporter ; fusionner seulement sur l'ordre exact pour cette tête.

## 8. Besoins et décisions ouverts (à trancher par le manager avant l'intégration)

1. **Audit strict du verrou de l'image** : `pip-audit --strict --desc --no-deps --disable-pip -r locks/sandbox-openhands.txt` échoue (33 avis, 6
   paquets, preuve `evidence/w5-c-audit-sandbox-openhands-lock.txt`) : `litellm` 1.83.0 (14 avis, corrigés ≥ 1.83.7–1.83.14), `anyio` 4.9.0
   (corrigé 4.14.2), `python-socketio` 5.14.0 (corrigé 5.16.2), `pytest` 8.4.2 (PYSEC corrigé 9.0.3 — impossible sous `pytest<9` imposé par
   `pytest-asyncio==0.23.6`), `python-jose` 3.5.0 et `ecdsa` 0.19.2 (sans version corrigée). Ce verrou n'est pas audité par la CI actuelle ; l'y
   ajouter la rendrait rouge et aucun avis ne doit être ignoré en silence. Régénérer le verrou exige `uv 0.11.33` et la résolution de la fermeture
   OpenHands (écartée localement par consigne) ; **décision** : qui corrige, par quelles contraintes, et le risque pour le SDK 1.7.0.
2. **Pile hors ligne du gate** : `fastapi`, `uvicorn`, `sqlalchemy`, `alembic`, `pypdf`, `reportlab`, `httpx` sont dans le verrou (`tests/test_w5_ci_general.py`),
   mais transitivement et pas déclarés dans `pyproject.toml` ; et 3 des 4 versions épinglées par la graine immuable (`fastapi==0.116.1`, `uvicorn==0.35.0`,
   `pytest==8.4.1`) diffèrent du verrou (0.141.1, 0.54.0, 8.4.2) : sans réseau, le `pip install -r requirements.txt` du gate échoue (xfail strict
   consignant l'écart). **Décision** : épingler ces versions dans le groupe `sandbox-openhands` (si la résolution avec `openhands-ai` le permet) ou autoriser un
   réseau restreint au gate.
3. **Réseau du gate** : `_build_gate_sandbox` (propriété de A) réutilise `SANDBOX_NETWORK` du codeur, qui doit être `none` avec le courtier. **Besoin A** : un
   réglage distinct pour le gate, ou la décision précédente rend le réseau inutile.
4. **Jeton de la fixture** : `probe` exige le droit d'écrire contenu et pull requests sur la fixture et le déclenchement de workflows ; à confirmer avant `apply`.
5. **Condensat de l'image `python:3.12-slim`** du workflow de la fixture : à figer (changement du plan donc du SHA du socle).
