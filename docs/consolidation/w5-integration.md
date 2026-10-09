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
| Scanner de fuite de la clé ET constructeur de l'ensemble publiable | `scripts/w5_leak_scan.py` | `tests/test_w5_ci_leak_scan.py` (22), `tests/test_w5_ci_campaign_workflow.py` (bash de l'étape exécuté réellement) |
| **Preuve composée en conteneur réel** (SDK réel → vrai courtier hôte → faux Google) | `tests/w5_integration_harness.py`, `tests/test_w5_integration_image.py`, étape de `tests.yml` | `tests/test_w5_ci_general.py` (statique) ; **exécution : CI distante après intégration** |
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

**Observé en C47** : le `push` du commit du socle sur `collegue-business/bootstrap-w5` a déclenché le workflow (hors branche par défaut) et le check `Fixture tests` de l'application 15368 a réussi sur ce commit ; chaque PR de sonde vers une base jetable a déclenché le workflow `pull_request` et produit un check de l'application 15368 sur la TÊTE de la PR (job réel, `head_sha` = tête, exécution `path` = workflow approuvé). Les workflows d'une PR créée
par le jeton de campagne (PAT) ne demandent pas d'approbation ; une PR en conflit de fusion ne déclenche rien.

### 4.2 Protection INDÉPENDANTE contre la substitution du workflow

Avec `pull_request`, le fichier appliqué est celui du commit de fusion : une PR peut le modifier, donc un check de même nom et de la même application ne prouve pas son contenu.
Couches, de la plus forte à la plus faible :
1. **Serveur** (CODEOWNERS `/.github/` et `/ci/` + `require_code_owner_review`) : **OBSERVÉ INEFFICACE** (sonde distante C47, 2026-10-09) : avec 0 approbation requise, une PR de l'auteur-propriétaire qui modifie le
   workflow, le CODEOWNERS ou le verrou approuvé, check VERT, a été **fusionnée** dans sa base jetable (PR #10, #11, #12 ; `mergeable` accepté, règle `pull_request` « pass » dans les rule-suites). Cette couche ne protège donc
   rien tant que le ruleset n'est pas modifié ; elle n'a pas été modifiée (ni affaiblie ni renforcée) en C47. Un renforcement (p. ex. une approbation requise) bloquerait aussi les fusions légitimes de la campagne (un seul compte) :
   décision du manager.
2. **Fusionneur de confiance** (propriété de B : `collegue/pilot/merge_policy.py`, décision manager `w5-manager-fixture-check-design.md`) : avant toute fusion dans `collegue-business/*`, compare l'arbre
   Git RÉEL de la tête à celui de la base de confiance pour **`.github/` ET `ci/`** (deux entrées racine : SHA de sous-arbre, qui couvre tout le contenu, ajout / modification / suppression /
   renommage compris ; lecture impossible ou arbre tronqué = refus) — `ci/` porte le verrou approuvé : le laisser hors garde laisserait remplacer la pile « approuvée » sans que CODEOWNERS ne soit
   le seul rempart. Variante de référence dans `scripts/w5_fixture_bootstrap.py` : `protected_tree_violations` (par fichier, SHA de blob) et `check_provenance` (le check est un job réel de
   l'exécution du workflow approuvé, tête et chemin concordants ; un check publié par l'API des checks n'est pas un job). La sonde évalue ce garde sur chaque tête (`guard.refuses`).
   **Check attendu** : pour un événement `pull_request`, le check-run `Fixture tests` (application 15368) est rattaché à la TÊTE de la PR (`commits/<tête>/check-runs`), alors que le workflow
   s'exécute sur le commit de FUSION `refs/pull/N/merge` (base + tête) ; comme les chemins protégés de la tête sont identiques à ceux de la base, ceux du commit de fusion le sont aussi. Ce
   rattachement n'a PAS été observé : la sonde (scénario `green`, `check_provenance`) le constate ou le contredit avant toute confiance.
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
- **Preuve composée en conteneur réel (préparée, NON exécutée)** — `tests/test_w5_integration_image.py` + `tests/w5_integration_harness.py`, marqueur `w5_image`, étape « Prove the real SDK through the
  real broker service » du job « Docker build » : image finale `collegue-sandbox-broker`, **vrai** `DockerSandbox` (`--network none`, un seul montage), **vrai** `BrokerRuntime` / `BrokerService` /
  `BrokerSocketServer` et registre SQLite sur l'hôte, **faux fournisseur Google derrière le service** (le SDK ne parle jamais à un double qui fabrique un succès). Sept tests : (1) le vrai
  `oh_runner` / SDK exécute un outil `terminal` puis `finish` ; `countTokens` et `generateContent` portent le même objet ; usage = REGISTRE (15 × n), jamais le stdout ; (2) code hostile appelant le
  socket (modèle, session, route, URL absolue, champs `base_url` / `api_key` / `scope`, flux, outil hébergé non autorisés) ; le fournisseur ne voit que les deux appels ordinaires ; aucun accès
  Internet, une seule interface, aucune clé, aucun socket Docker, montages = socket + workspace ; (3–6) **échéance persistée commune** : un worker qui dort ou calcule sans nouvel appel (3 comportements) est
  tué à l'échéance (ouverte AVANT le run), conteneur ABSENT ensuite, aucune session restante ; une requête en vol coupée reste réservée comme inconnue (scope bloqué, une seule émission) ; (7) aucun
  worker ni génération après l'échéance. Exclus de la suite générale (`addopts`), sélectionnés et exécutés SANS saut dans le job (`ci_require_junit.py`, plancher 7 = nombre exact vérifié par un test).
  **Statut honnête** : aucun Docker ni SDK ici ; seuls (3–7) ont été rejoués contre le code de A (`756a4e6`) avec un docker de remplacement qui exécute la commande sur l'hôte (cinq tests verts :
  logique du harnais et comportement hôte de A, PAS la mort réelle du conteneur ni l'image) ; (2) n'a vérifié que les codes de refus et le décompte de l'amont (les assertions d'isolation échouent
  logiquement hors conteneur) ; (1) n'a jamais tourné. Interfaces de A dont elle dépend : `INTERFACE_CONTRACT` du harnais (échec franc si elles changent). Un défaut comportemental révélé va à A.

## 6. Campagne future (à NE PAS lancer sans ordre exact du manager)

Workflow `consolidation-e2e.yml` : `workflow_dispatch` seul, confirmation exacte, identifiant neuf (consommé au lancement par B ; aucun rerun), environnement GitHub `w5-gemma-campaign` propre à la
campagne, clé temporaire `W5_GOOGLE_API_KEY` référencée **une seule fois**, dans l'étape de campagne, sous le nom **`LLM_API_KEY`** (le seul que lisent les réglages et `BrokerRuntime.from_settings`) ;
préflight statique et complet sans clé ; enveloppe `2 / 250000 / 900` au niveau du job, `BROKER_GLOBAL_DEADLINE_SECONDS=900`, `BROKER_RUN_DIR=/tmp/cbk` ; transport `budget_broker`,
`SANDBOX_NETWORK=none`, image `collegue-sandbox-broker:ci` ; aucune variable de prix ; nettoyage `always()`. **Publication sûre** : seul l'ensemble construit par le scanner de confiance (`scripts/w5_leak_scan.py`, valeur lue dans l'environnement, jamais en argv) est déposé. Il lit **tous** les fichiers en octets
(binaires, SQLite) ET contrôle les **noms** ; un fichier n'entre dans `$W5_PUBLISH` qu'après avoir été lu, vérifié et copié dans le même passage (renommage atomique : une commande interrompue ne
laisse aucun fichier partiel) ; contenu ou nom contaminé, parcours ou lecture en erreur, lien ou objet irrégulier = exclu et verdict rouge ; les chemins du rapport sont **expurgés** (jamais la clé,
même dans un nom). Code 2, crash du scanner, échec de quarantaine ou commande interrompue : l'ensemble publiable ne contient que des fichiers déjà vérifiés (vide si le scanner n'a rien
copié). Les sources vivantes (rapport, registre, espaces de travail) ne sont JAMAIS déposées ; ce que le nettoyage écrit après le scan ne l'est donc pas non plus. Campagne non lancée
(`steps.campaign.outcome == 'skipped'` : l'étape qui reçoit la clé n'a pas tourné, donc elle n'a jamais existé) : même construction avec `--assume-no-key`. Un diagnostic minimal sûr
(issues des étapes et identifiants validés, jamais dérivé d'un fichier) et le rapport du scanner (noms expurgés) sont toujours déposés. Le manager injecte puis retire la clé.
**Pas de campagne tant que l'image broker n'a pas été construite, auditée et prouvée par la CI distante.**

## 7. Checklist d'intégration (C, sur SHA figés de A et B et ordre du manager)

1. SHA de A/B = têtes actuelles ; périmètres disjoints ; fusion par SHA sans fast-forward ; relire les diffs, chercher les usages oubliés.
2. **Relais** présent ⇒ retirer le marqueur `A_NOT_INTEGRATED` de `tests/test_w5_ci_general.py` (il casse par construction) ; confronter l'interface réelle (`start`, `MAX_CLIENT_BYTES`, variables) à
   `scripts/ci_w5_broker_transport.py`.
3. **Plancher PostgreSQL** du courtier = nombre exact collecté dans `tests/test_w5_broker_postgres.py`.
4. **Fichiers de B** : `tests/test_w4_business_workflow.py` (retirer l'`xfail` de l'enveloppe, liste des détenteurs de clé sur `LLM_API_KEY`, chemins déposés) ; validation du socle (`approved_files` contient
   `requirements.txt`, que B refuse aujourd'hui car fichier de graine : accepter UNIQUEMENT `modified_seed_files = ["requirements.txt"]` avec comparaison des deux hachages `modified_seed_hashes`, refuser tout autre
   changement de graine, `app/main.py`, tests de graine, workflow altéré) ; `_ADDED_ALLOWED` doit admettre `.github/CODEOWNERS` et `ci/requirements-approved.lock` (refusés aujourd'hui : ni `.yml`, ni `docs/*.md`, ni `requirements*.txt`) ;
   `_validate_check_producer` / `check_producer` (déclencheur `pull_request_target`, état de C44) est remplacé par `check_workflow` du manifeste (`pull_request` + `push`, job `Fixture tests`, aucun `pull_request_target`) ;
   ruleset : `require_code_owner_review` vrai, `do_not_enforce_on_create` faux ; **garde de fusion sur `.github/` ET `ci/`** (aujourd'hui `CONTROLS_DIRECTORY = ".github"` seulement), § 4.2.
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
