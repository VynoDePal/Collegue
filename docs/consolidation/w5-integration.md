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
  `approved_files` (chemin → sha256 de ce que le socle **ajoute** à la graine, plus `requirements.txt` modifié), `required_check = "Fixture tests"`, `check_app_id = 15368`,
  `ruleset_id`, `branch_pattern = refs/heads/collegue-business/*`. B le valide PAR L'API ; il n'est jamais une preuve à lui seul.
  Champs additionnels : `added_files`, `modified_seed_files`, `modified_seed_hashes`, `protected_prefixes`, `code_owner`, `check_workflow` (§ 4). Le socle n'ajoute **aucune** implémentation des trois tâches.
- **Délai** : les 900 s commencent à l'ouverture réelle Google, avant les canaris des deux Gemma ; aucune nouvelle génération après l'échéance ;
  collecte et nettoyage sans génération peuvent finir ensuite.

## 3. Ce que C a préparé (local, rien de distant)

| Élément | Fichier | Preuve locale |
|---|---|---|
| Plan de fixture hors ligne, application/nettoyage/contre-épreuves sur ordre | `scripts/w5_fixture_bootstrap.py` | `tests/test_w5_ci_fixture.py` (SHA identique à `git`, idempotence, collisions, attente du check du socle, sonde simulée, garde bash exécutée réellement) |
| Workflow de la fixture « Fixture tests » (dans le socle) | `scripts/w5_fixture_bootstrap.py` (`TRUSTED_WORKFLOW`) | exécution locale des commandes d'installation/test sans Docker : `evidence/w5-c-fixture-workflow-local-run.log` |
| Pile approuvée : groupes `fixture-stack` / `sandbox-broker`, verrous hachés **audités en strict** | `pyproject.toml`, `scripts/locks.py`, `locks/fixture-stack.txt`, `locks/sandbox-broker.txt` | `scripts/locks.py check` (8 verrous), `pip-audit --strict` : aucun avis (`evidence/w5-c-audit-{sandbox-broker,fixture-stack}-lock.txt`) |
| Image du transport broker (SDK/outils seuls, pile métier approuvée, relais) | `docker/sandbox/Dockerfile.broker`, `scripts/patch_openhands_gemma4_terminal.py --mode sdk-only` | tests statiques et modes du patch ; **construction et preuves d'image : CI distante** |
| Workflow de campagne W5 (évolution de `consolidation-e2e.yml`) | `.github/workflows/consolidation-e2e.yml` | `tests/test_w5_ci_campaign_workflow.py` (dont le script bash de l'étape de campagne exécuté réellement) |
| Scanner de fuite de la clé | `scripts/w5_leak_scan.py` | `tests/test_w5_ci_leak_scan.py` |
| CI générale : audits stricts, étape PostgreSQL du courtier, preuves de l'image broker sans réseau | `.github/workflows/tests.yml`, `scripts/ci_w5_broker_transport.py` | `tests/test_w5_ci_general.py`, `tests/test_w5_ci_transport_proof.py` |
| Plan soumis à la revue du manager | `evidence/w5-c-fixture-plan/` (payloads, diff, ordre des appels, contre-épreuves) | `SHA256SUMS` |

## 4. Fixture : conception et points d'attaque

Dépôt `VynoDePal/collegue-e2e-fixture` (id 1298596453), `main` = graine `8e3691d8…` (arbre `c8bffa32…`, ruleset 18840666 intact, **aucun workflow, branche par
défaut immuable**), PR #4 et #7 étrangères. Le socle est un **commit déterministe** (parent unique = graine) publié sur `collegue-business/bootstrap-w5`. Il ajoute
`.github/workflows/fixture-tests.yml`, `.github/CODEOWNERS`, `ci/requirements-approved.lock`, `docs/runbook-ops.md` et `docs/deploiement.md` (documents de B octet
pour octet), et **modifie un seul fichier de la graine, `requirements.txt`** (versions exactes de la pile approuvée ; décision du manager
`w5-manager-bootstrap-dependency-decision.md`). Le manifeste distingue `added_files` et `modified_seed_files = ["requirements.txt"]` et porte les deux hachages
(`modified_seed_hashes`) ; `approved_files` les couvre tous. `docs/export_header.md` et toute implémentation des trois tâches sont produits par les BUILD, jamais par le socle.

### 4.1 Déclenchement (correction du constat du manager)

`pull_request_target` s'exécute dans le contexte de la **branche par défaut** (documentation GitHub relue le 2026-10-09, `evidence/w5-c-github-events-doc.md`) : avec une graine
immuable sans workflow il ne peut jamais partir. Le workflow du socle utilise donc :
* `pull_request` (branches `collegue-business/**`) : exécuté dans le contexte du commit de fusion de la PR, sans exiger de workflow sur la branche par défaut ; la PR vers une base
  éphémère (issue du socle, qui porte le fichier) le déclenche ; le check est le **job** `Fixture tests`, rattaché à la tête de la PR ;
* `push` sur `collegue-business/bootstrap-w5` : produit le check sur le commit du socle lui-même (« Les workflows de `push` s'exécutent aussi hors de la branche par défaut »).

**Ce déclenchement n'est pas démontré tant que la contre-épreuve distante n'a pas tourné** (scénario `green`) : le plan l'organise, il ne le prouve pas. Les workflows d'une PR créée
par le jeton de campagne (PAT) ne demandent pas d'approbation ; une PR en conflit de fusion ne déclenche rien.

### 4.2 Protection INDÉPENDANTE contre la substitution du workflow

Avec `pull_request`, le fichier appliqué est celui du commit de fusion : une PR peut le modifier, donc un check de même nom et de la même application ne prouve pas son contenu.
Couches, de la plus forte à la plus faible :
1. **Serveur** : le CODEOWNERS du socle attribue `/.github/` et `/ci/` au propriétaire du dépôt ; le ruleset exige `require_code_owner_review`. L'auteur d'une PR ne peut pas
   approuver la sienne : aucune PR qui modifie le workflow, le CODEOWNERS ou le verrou approuvé ne peut fusionner, quel que soit son check. **Comportement réel avec 0 approbation
   requise : établi seulement par la sonde** (scénarios `workflow-touch`, `codeowners-touch`, `lock-touch` : check VERT, fusion refusée) ; s'il est inefficace, c'est le résultat à rapporter.
2. **Fusionneur de confiance** (raccord proposé, fichiers de B) : avant toute fusion dans `collegue-business/*`, `protected_tree_violations` sur l'arbre Git réel de la tête (chemins
   protégés identiques au socle, aucun lien, aucun sous-module) **et** `check_provenance` (le check est un job réel de l'exécution du workflow approuvé, tête et chemin concordants ; un
   check publié par l'API des checks n'est pas un job). Ces fonctions sont dans `scripts/w5_fixture_bootstrap.py` (lectures seules, testées).
3. Le workflow lui-même (garde des liens symboliques, pile approuvée, conteneurs) est utile mais **n'est pas** une protection contre sa propre substitution.

**Limite résiduelle** : une PR qui modifie le workflow peut, pendant son propre run, demander des permissions d'écriture au jeton de ce run (PR du même dépôt). Elle ne peut pas
fusionner (couche 1), mais pourrait tenter de publier un faux check sur une AUTRE tête ; la couche 2 (provenance) la détecte, la couche 1 ne le voit pas.

### 4.3 Exécution du code candidat et dépendances

Le code du candidat ne s'exécute jamais sur l'hôte du runner. (1) Un conteneur sans privilège ni secret télécharge les roues du **verrou approuvé du socle**
(`ci/requirements-approved.lock`, haché, `--require-hashes --only-binary=:all: --no-deps`) — jamais un fichier de dépendances dicté par le candidat ; (2) un second conteneur
**sans réseau** (utilisateur 65534, capacités retirées, système de fichiers en lecture seule) installe ces roues, vérifie que `requirements.txt` est satisfait par elles — toute autre
dépendance est REFUSÉE explicitement, rien n'est téléchargé — puis lance `pytest`. Une garde refuse tout lien symbolique et tout fichier de dépendances irrégulier ; seuls des
**répertoires** sont montés (jamais un fichier du candidat : Docker résoudrait un lien `requirements.txt -> /var/run/docker.sock` côté hôte). Aucun jeton, secret ni socket Docker dans un
conteneur. L'image est `python:3.12-slim` figée par condensat. **Limites** : le téléchargement des roues accède à Internet (sans secret, hachages vérifiés) ; un candidat qui commite
une roue locale est exécuté dans le conteneur sans réseau comme tout son code ; la preuve du confinement Docker lui-même est celle de la CI de la fixture (non exécutée ici).

### 4.4 Création des bases et ruleset

Ruleset actif sur `refs/heads/collegue-business/*`, sans bypass : PR obligatoire avec approbation du propriétaire des chemins du contrôle, check `Fixture tests` lié à l'application GitHub
Actions (`integration_id` 15368), base à jour, **`do_not_enforce_on_create` faux** : créer une branche sous ce motif exige un commit qui a déjà passé le check. Le socle le passe de lui-même
(workflow `push`) : `apply` crée la branche, **attend ce check réussi**, puis crée le ruleset (absent, rouge ou jamais terminé = arrêt, aucun ruleset). Les bases de campagne se créent donc
depuis le commit du socle **sans bypass ni faux check** ; un commit sans check (la graine) ne peut pas devenir une base. Pas de règle `deletion` : le nettoyage supprime ses bases.

### 4.5 Contre-épreuves distantes (`probe`, sur ordre seulement)

Huit scénarios (`evidence/w5-c-fixture-plan/probe-plan.md`), fusions RÉELLES dans des bases jetables : `green` (déclenchement, check = job réel de l'application 15368, provenance, fusion
acceptée), `red-test` (check rouge, fusion refusée, faux check refusé ou sans effet), `workflow-touch` / `codeowners-touch` / `lock-touch` (check vert, fusion refusée : isole la protection
du propriétaire), `unapproved-dependency` (check rouge explicite, rien téléchargé), `symlink` (lien mode 120000 par l'API Git Data : garde rouge), `seed-base` (création d'une base depuis la
graine refusée). Un check absent n'est jamais un succès. **Incertitudes que la sonde tranche** : déclenchement sur une PR vers une base non par défaut, code owner à 0 approbation, règle de
création, droits du jeton, Actions activées.

### 4.6 Ordre des mutations (aucune n'est faite dans cette passe)

`inspect` → revue du manager de `evidence/w5-c-fixture-plan/` → `apply --order-token APPLIQUER-W5-FIXTURE-<12 hex>` (arbre, commit, branche ; attente du check ; ruleset) → `verify` → `probe` → manifeste
complété par le `ruleset_id` → variable d'environnement GitHub `W5_BOOTSTRAP_MANIFEST_JSON`. Retour arrière : `cleanup` ne supprime que la branche du socle et le ruleset **de cette campagne**.

## 5. Image broker, dépendances et CI générale (cinq checks inchangés : Ruff, Pytest 3.11, Pytest 3.12, Dependency audit, Docker build)

- **Image dédiée** `docker/sandbox/Dockerfile.broker` : le runner n'importe que `openhands.sdk`, `openhands.tools.preset.default` (`cli_mode=True`) et `openhands.sdk.llm.auth.openai` ; l'image
  installe `openhands-sdk`/`openhands-tools` 1.19.1 et `lmnr` 0.7.52 **sans** l'application `openhands-ai`, ni `python-jose`/`ecdsa`/`passlib`, ni Node/Chromium (gate frontend désactivé). La pile
  métier (FastAPI, uvicorn, httpx, SQLAlchemy, Alembic, pypdf, reportlab, python-multipart, pytest 9.x, pytest-asyncio 1.x) est **déclarée** dans `pyproject.toml` (groupes `fixture-stack`,
  `sandbox-broker`) aux versions exactes du socle de la fixture : un `pip install -r requirements.txt` du gate est satisfait sans réseau. Le patch de compatibilité Gemma s'applique en mode
  explicite `sdk-only` (versions exactes du SDK et des outils, `openhands-ai` absent, hachage exact de la source de `TerminalAction`, gardes inchangées). L'image « direct » historique reste
  construite et vérifiée telle quelle.
- **Audit strict** : `pip-audit --strict --desc --no-deps --disable-pip -r locks/sandbox-broker.txt` et `… locks/fixture-stack.txt` : **aucun avis connu** (étapes de CI ajoutées ; entrées
  `sys_platform` d'autres systèmes non évaluées sur le runner Linux). **Non vérifié à l'exécution** : la compatibilité du SDK 1.19.1 avec `litellm` 1.104.1 (le verrou legacy portait 1.83.0) et
  `pytest-asyncio` 1.x avec les tests de B ; la CI d'image (SDK réel) et le vérificateur métier de B dans cette image le tranchent.
- **Ancien verrou `locks/sandbox-openhands.txt`** : audit strict **toujours rouge** (33 avis, 6 paquets, `evidence/w5-c-audit-sandbox-openhands-lock.txt`) ; **non réparé**, non audité par la CI,
  non utilisé par la campagne W5. Sa réparation (ou son retrait) est une décision séparée.
- **Aucun modèle réel** dans la CI générale ; faux courtier et faux fournisseur dans les tests seulement.
- **PostgreSQL obligatoire** : étape `PostgreSQL broker state` (service `postgres:16`, `test -f`, `test -n`, collecte égale à l'exécution, JUnit exigé). Plancher 76 = tests collectés sur `d5800a8` de
  A ; à recaler sur le nombre exact à l'intégration.
- **Preuves d'image (broker)** : `scripts/ci_w5_broker_transport.py` s'exécute dans l'image construite, `--network none`, sans secret ni montage : isolation, **l'image est son verrou** (chaque entrée du
  verrou embarqué installée à la version exacte, distributions legacy absentes), vrai SDK → relais → faux courtier sur socket Unix, relais non proxy, plafond de volume ; le routage du worker et le
  vérificateur métier de B tournent aussi dans cette image. Aucun de ces scripts n'a pu être exécuté localement avec le vrai SDK (par consigne) : leur logique est éprouvée avec un relais de
  remplacement et un faux SDK, leur preuve réelle est celle de la CI distante.
- **À ajouter à l'intégration** : preuve hôte `tests/test_w5_integration_*.py` — vrai courtier et serveur de socket de A, faux fournisseur Google, conteneur `--network none` avec le répertoire du
  socket — vérifiant la clé absente de l'environnement, des arguments, des montages et des journaux du conteneur, et le décompte des appels reçus par le faux fournisseur.

## 6. Campagne future (à NE PAS lancer sans ordre exact du manager)

Workflow `consolidation-e2e.yml` : `workflow_dispatch` seul, confirmation exacte, identifiant neuf (consommé au lancement par B ; aucun rerun), environnement GitHub `w5-gemma-campaign` propre à la
campagne, clé temporaire `W5_GOOGLE_API_KEY` référencée **une seule fois**, dans l'étape de campagne, sous le nom **`LLM_API_KEY`** (le seul que lisent les réglages et `BrokerRuntime.from_settings`) ;
préflight statique et complet sans clé ; enveloppe `2 / 250000 / 900` au niveau du job, `BROKER_GLOBAL_DEADLINE_SECONDS=900`, `BROKER_RUN_DIR=/tmp/cbk` ; transport `budget_broker`,
`SANDBOX_NETWORK=none`, image `collegue-sandbox-broker:ci` ; aucune variable de prix ; nettoyage `always()`. **Scanner de fuite** (`scripts/w5_leak_scan.py`, valeur lue dans l'environnement, tous
fichiers en octets, formes brute/URL/JSON/base64) exécuté dans l'étape de campagne sur le rapport et le registre : un fichier contaminé, un lien symbolique ou un fichier illisible sont mis en
quarantaine (jamais déposés), la campagne est rouge, preuves saines et nettoyage conservés ; seuls le rapport et le registre `*.sqlite3*` sont déposés. Le manager injecte puis retire la clé.
**Pas de campagne tant que l'image broker n'a pas été construite, auditée et prouvée par la CI distante.**

## 7. Checklist d'intégration (C, sur SHA figés de A et B et ordre du manager)

1. SHA de A/B = têtes actuelles ; périmètres disjoints ; fusion par SHA sans fast-forward ; relire les diffs, chercher les usages oubliés.
2. **Relais** présent ⇒ retirer le marqueur `A_NOT_INTEGRATED` de `tests/test_w5_ci_general.py` (il casse par construction) ; confronter l'interface réelle (`start`, `MAX_CLIENT_BYTES`, variables) à
   `scripts/ci_w5_broker_transport.py`.
3. **Plancher PostgreSQL** du courtier = nombre exact collecté dans `tests/test_w5_broker_postgres.py`.
4. **Fichiers de B** : `tests/test_w4_business_workflow.py` (retirer l'`xfail` de l'enveloppe, liste des détenteurs de clé sur `LLM_API_KEY`, chemins déposés) ; validation du socle (`approved_files` contient
   `requirements.txt`, que B refuse aujourd'hui car fichier de graine : accepter UNIQUEMENT `modified_seed_files = ["requirements.txt"]` avec comparaison des deux hachages `modified_seed_hashes`, refuser tout autre
   changement de graine, `app/main.py`, tests de graine, workflow altéré) ; `.github/CODEOWNERS` et `ci/requirements-approved.lock` dans les fichiers approuvés ; ruleset : `require_code_owner_review`
   vrai, `do_not_enforce_on_create` faux ; **raccord de fusion** (couche 2 du § 4.2).
5. **Manifeste** : recoupement des documents de B (test sauté tant que B n'est pas intégré), puis régénération du plan et nouvelle soumission au manager (le SHA du socle change au moindre octet).
6. Suites complètes 3.11 et 3.12, Ruff complet, `python scripts/locks.py check` (et `--recompile` en CI), PostgreSQL réel sans skip, image broker et preuves distantes sur le SHA publié.
7. Observer les cinq checks et les revues (une revue absente est dite absente) ; rapporter ; fusionner seulement sur l'ordre exact pour cette tête.

## 8. Décisions et besoins ouverts

1. **Sonde distante** (ordre du manager) : tranche déclenchement, code owner à 0 approbation, règle de création, droits du jeton (contenu, pull requests, Actions), Actions activées.
2. **Jeton de la fixture** : écriture du contenu, des PR et des objets Git ; à confirmer avant `apply`.
3. **Compatibilité du SDK 1.19.1 avec la pile résolue** (litellm 1.104.1, pytest-asyncio 1.x, pytest 9.x) : à établir par la CI d'image ; si un blocage apparaît, choisir des contraintes (résolution locale
   possible avec `uv 0.11.33` isolé, sans installer la fermeture).
4. **Ancien verrou `sandbox-openhands`** : rouge à l'audit, non réparé (voir § 5).
5. **Réseau du gate** : l'image broker satisfait le `requirements.txt` du socle sans réseau ; une dépendance supplémentaire demandée par le codeur est refusée (jamais ignorée). Le réglage de réseau
   séparé du gate reste un besoin de A seulement si un réseau restreint est un jour souhaité.
