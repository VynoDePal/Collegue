# Vague 4 — organisation, interfaces et checklist d'intégration (C)

**État exact : les vagues 1, 2 et 3 sont livrées sur `main` ; la vague 4 est en cours ; aucune campagne réelle n'a été démarrée.**
Ce document est rédigé **avant** les livraisons de A et B : il consigne la répartition, les interfaces **proposées** (contrat de
départ du manager, à confronter aux rapports `reports/w4-{a,b}-interface.md`), les preuves attendues et la livraison en deux
étapes. Il ne décrit aucun résultat de leur travail et n'annonce aucune garantie non testée. Règles générales :
[`AGENTS.md`](../../AGENTS.md) ; protocole et checklists précédentes : [`protocole.md`](protocole.md) ; vagues précédentes :
[`w2-integration.md`](w2-integration.md), [`w3-integration.md`](w3-integration.md).

- **Base commune** : `main` = `5c1cbf51cec854b2fa73e3951273951e7a7212be` (PR #611, arbre `206d53ac4c926cc23f3338bc395e28b4a281ad05`, parent unique `58355a4`, run push `37860048078` : cinq checks verts, 3852 tests passés / 36 ignorés / 9 désélectionnés aux deux versions de Python, PostgreSQL réel 21/23/5 sans skip).
- **Branches** : A `codex/consolidation-w4-a`, B `codex/consolidation-w4-b`, C `codex/consolidation-w4-c` (même base, développement parallèle).
- **Objet** : A corrige la **résolution cohérente de la destination et de l'authentification par rôle** (fournisseur, modèle, endpoint, authentification, jusqu'aux appels effectivement émis) ; B construit la **preuve métier multi-tâches** (FastAPI + SQLite + Alembic, trois tâches dépendantes, PDF, reprise, amélioration, incident et rollback Phase 5) et prépare l'invocation **unique** de la campagne réelle sans la lancer ; C organise, puis intègre sur instruction.
- **Non-objectifs** : lancer la campagne réelle (interdite pendant l'implémentation ; son lancement unique sera ordonné à C par le manager sur la révision finale), élargir le chantier budgétaire de W2, ajouter un service de proxy facturable, changer les protections GitHub ou les cinq noms de checks, réécrire fonctionnellement un lot.

## 1. Propriété des fichiers

| Qui | Possède |
|---|---|
| **A** | `collegue/core/llm/{roles,client,sampling_ctx,sampling_handler,budget_guard}.py`, `collegue/config.py`, `.env.example`, câblage sampling de `collegue/app.py`, factory de `collegue/pilot/runtime.py`, `collegue/executor/{openhands_agent,openhands_sdk_agent,worker_budget,oh_runner,oh_sampler}.py`, `collegue/sandbox/executor.py` **uniquement** pour un raccord de credential par référence hors argv (toutes les gardes W1 préservées) ; raccords de propagation du rôle : `core/llm/__init__.py`, `tools/base.py`, `tools/agent_loop.py`, `planner/{spec_generator,decomposer,acceptance_tests}.py`, `executor/quality_gate.py` (routage seul : ni oracles, ni veto, ni preuve W3) ; tests existants `test_llm_roles`, `test_llm_client_routing`, `test_sampling_ctx`, `test_openhands_sdk_agent`, `test_pilot_runtime`, `test_oh_runner`, `test_oh_runner_budget`, `test_oh_sampler`, `test_budget_transport`, `test_sandbox` et nouveaux `test_w4_routing*.py` ; `docs/consolidation/w4-routing.md` |
| **B** | `collegue/pilot/nightly_e2e.py`, module de campagne métier dédié si nécessaire, `.github/workflows/integration-nightly.yml`, éventuel workflow ponctuel dédié (par exemple `consolidation-e2e.yml`), fixture métier sous un répertoire dédié, `tests/test_pilot_nightly_e2e.py`, `tests/test_ci_nightly_pipeline.py`, nouveaux `tests/test_w4_business*.py`, helpers `w4_business*` ; `docs/consolidation/w4-e2e.md` |
| **C** | `AGENTS.md`, `CLAUDE.md`, `docs/consolidation/{protocole,w4-integration}.md`, README français/anglais, `.github/workflows/tests.yml`, `pyproject.toml` et `locks/` **seulement sur besoin établi** (ex. lecteur PDF de test), `tests/conftest.py` et `tests/test_executor_git_boundary.py` (raccords d'inventaire communs), raccords mécaniques et tests d'intégration dédiés : **`tests/test_w4_integration_*.py` et `tests/w4_integration_*` uniquement** (`tests/w4_business*` et `tests/test_w4_business*` appartiennent à B, `tests/test_w4_routing*` à A) |

Un fichier a un seul propriétaire. Tout autre fichier est signalé au manager **avant** édition (il peut l'attribuer). Un besoin sur
un fichier d'un autre rôle s'écrit dans `reports/w4-{a,b}-interface.md` ou dans le rapport, il ne s'édite pas. Une modification
fonctionnelle de raccord revient à l'auteur. Les harnais et sondes du manager sont hors dépôt et intouchables.

## 2. Interfaces proposées (contrat de départ du manager)

À vérifier contre les rapports d'interface des auteurs dès leur publication ; toute contradiction est signalée au manager avec fichier,
raison et test attendu.

### 2.1 Routage (A → B, C)
- **Résolution unique de la destination réellement émise** : rôle, fournisseur, modèle canonique, endpoint, méthode d'authentification et **référence** au credential ; la forme Python exacte est choisie par A et publiée tôt. Les secrets ne figurent ni dans les `repr`, ni dans les journaux, ni dans les exceptions de configuration (y compris les erreurs de validation), ni dans les `argv`.
- **Héritage** : la clé et l'endpoint globaux ne sont hérités par un rôle que si leur fournisseur est celui du rôle. Changement de fournisseur sans modèle compatible ou credential adapté : **erreur explicite avant émission**. Un fournisseur local peut accepter volontairement l'absence de clé. L'abonnement doit être **explicitement** sélectionné et cohérent avec le modèle. Toute combinaison non supportée est refusée et documentée, jamais convertie en silence (plus de repli implicite sur Gemini).
- **Atteindre les appels effectifs** : sampling hors ligne, handler serveur FastMCP (deux rôles de même modèle mais de clés/endpoints distincts restent distinguables, y compris en appels concurrents), OpenHands (point de construction du SDK : préfixe de modèle, endpoint, provenance de la clé, kwargs réellement supportés par la version verrouillée ; clé passée par référence hors argv) et consommateurs qui ne passent pas par `accounted_sample` (`tools/base.py`, `tools/agent_loop.py`). Le sampling délégué à un client MCP externe n'est pas contrôlé par le handler serveur : portée à documenter.
- **Frontière budgétaire (W2 préservée)** : la destination réservée est celle **effectivement utilisée**, retries et replis compris ; le registre n'est pas remplacé et les contraintes strictes ne sont pas affaiblies pour faciliter un transport. Un codeur qui accède à un credential d'API dans le workspace ne devient pas sûr parce que son routage est corrigé. Le plafond de tokens strict n'est pas garanti par l'abonnement.
- **Pour B** : une API de résolution/validation **sans émission ni dépense**, réutilisable par le préflight ; le contrôle de capacité de `worker_budget` reste unique et sans verdict affaibli.
- **Témoins manager (avant correction, sur le code de W3)** : routage planner OpenAI/gpt-5.4 émis vers l'endpoint Google avec la clé globale Gemini ; `OHSdkAgent.litellm_model()` renvoie `gemini/gpt-5.4` malgré un fournisseur coder `openai` ; matrice de 19 cas où seuls les deux témoins globaux passent ; appels QA/reviewer concurrents partant tous deux avec la clé et l'endpoint globaux.

### 2.2 Scénario métier (B → C)
- **Fixture** : référence FastAPI + SQLite + Alembic, **trois tâches dépendantes** — (1) persistance et migration ; (2) création puis lecture d'un audit ; (3) export PDF dont le contenu porte les données de cet audit — depuis une base SQLite **réellement vierge**. Dépôt réel dédié `VynoDePal/collegue-e2e-fixture` (identité et seed revérifiés au préflight) ; le dépôt produit n'est jamais la cible d'un agent.
- **Sérialisation** : `STRICT_MAX_INFLIGHT_PRS=1` ; chaque tâche commence après fusion **et** resynchronisation prouvées de la précédente (aucune re-livraison automatique d'une PR périmée ; aucun contournement de la preuve ni réécriture silencieuse de sa base).
- **Entrées publiques** : planification, approbation, exécution, promotion et fusion, avec transport LLM et GitHub **simulés aux seules frontières** ; une preuve verte, une synchronisation réussie ou un budget respecté ne sont jamais substitués à leur logique produit ; une étape simulée ou non exécutée est identifiable. Les ponts Git/REST de W3 sont réutilisés quand ils conviennent.
- **Oracles** scellés hors workspace (W3) : rouge **par assertion** sur la seed (par exemple HTTP 404 comparé au contrat, absence de table ou de fichier constatée avant usage), même empreinte, puis vert. Une erreur de collecte/d'import/de migration, un skip ou une indisponibilité n'est pas une preuve négative.
- **PDF** : analysé par un **vrai lecteur** (texte des données de l'audit), avec témoin négatif (PDF valide mais mauvaises données) ; jamais par recherche d'octets ni par MIME seul. Les fichiers SQLite et PDF générés sont des artefacts d'exécution, pas du contenu à publier par la Contents API.
- **Parcours** : contenu intégré vérifié avant la tâche suivante ; reprise après redémarrage avec le même registre (sans perte ni redépense) ; amélioration sans régression ; incident contrôlé puis rollback Phase 5 d'un changement autorisé par la politique de faible risque, avec restauration du **comportement métier** (données, HTTP, PDF, commit restauré) **et** de l'état durable (acquittement compris). Les politiques W3 restent actives (restrictions de chemins, base strictement protégée).
- **Rapport machine et humain** : états distincts « étape réussie », « non exécutée », « arrêt budget », « échec », « validation incomplète » ; SHA/base, empreintes, résultats métier, compteurs du registre et point d'arrêt conservés.

### 2.3 Campagne réelle (B prépare, C lance sur ordre)
- **Une seule commande, auditable, hors récurrence** (workflow ponctuel dédié ou mécanisme équivalent ; `INTEGRATION_E2E_ENABLED` récurrent non activé ; aucun autre test payant déclenché) sur le dépôt fixture identifié. **Bornes globales : 2 USD, 250 000 tokens, 900 s** ; plafond issu du **registre W2**, jamais d'un compteur remis à zéro entre commandes ; aucune relance payante automatique (les retries internes consomment la même enveloppe) ; une échéance ne laisse aucun conteneur ou appel facturable hors surveillance.
- **Préflight sans dépense** (avant toute planification payante) : identité du dépôt fixture, **compatibilité du transport du worker avec les trois plafonds**, protections applicables au vrai acteur et à la vraie branche (le ruleset 18840666 de la fixture rend la seed immuable mais ne garantit pas, en soi, les checks stricts d'une base éphémère), absence de clé imprimée. Une configuration ou un transport incompatible **bloque avant émission** : c'est une **preuve de préflight** (zéro appel prouvé), **pas** une preuve du parcours avec modèles réels ; le rapport l'indique comme *validation incomplète*.
- Les budgets des sessions Claude Code de développement sont distincts, leurs credentials ne sont jamais réutilisés par la campagne produit.

## 3. Preuves attendues

| Niveau | Attendu |
|---|---|
| Auteur (A, B) | Reproductions **avant/après** (mêmes assertions) sur la base, tests déterministes au niveau transport, SHA et arbre propres, commandes et retours réels, suite complète hors `integration`, Ruff `check` et `format --check`, Python 3.11 et 3.12 pour les nouveaux chemins, aucun appel payant ni Docker/OpenHands lourd local |
| Intégrateur (C), après gel | Fusions `--no-ff` par SHA, tests de raccord par les entrées publiques, suites complètes 3.11/3.12 (JUnit, comptes, skips identifiés), PostgreSQL réel sans skip pour les tests concernés, wheel/migrations si touchés, candidat figé (`reports/w4-c-candidate.json`, `testing=true` jusqu'à la fin) |
| Manager (Codex) | Validation indépendante sur QA propre (matrice de routage, sonde concurrente, harnais du scénario), lecture des journaux CI, décision d'acceptation puis de fusion par ordres distincts |
| CI distante | Les cinq checks requis (`Ruff`, `Pytest (Python 3.11)`, `Pytest (Python 3.12)`, `Dependency audit`, `Docker build`) sur la **tête publiée exacte**, puis sur le run **push** de `main` ; les tests d'image du routage OpenHands ne se prouvent qu'en CI, après intégration |

## 4. Checklist d'intégration (C, après gel et sur instruction)

À exécuter **seulement** sur les SHA figés et l'instruction distincte du manager ; entrées publiques, témoin bénin et motif du refus vérifié ; aucun `AttributeError` ni double incomplet compté comme refus sûr.

### 4.1 Avant la fusion locale
- [ ] SHA de A (resp. B) = tête de sa branche, descend de `5c1cbf5`, arbre annoncé, worktrees propres.
- [ ] Périmètres disjoints : `git diff --name-only 5c1cbf5 <sha>` de A et de B sans intersection ; tout fichier hors périmètre listé est rapporté au manager (en particulier `tests/conftest.py`, `tests/test_executor_git_boundary.py`, `.github/workflows/tests.yml`, `pyproject.toml`, `locks/`, qui restent à C).
- [ ] Pas de dépendance ajoutée sans choix justifié ; sinon §6. Pas de changement des noms de checks ni des protections.
- [ ] Fusion `--no-ff` par SHA, un conflit fonctionnel retourne à l'auteur ; l'inventaire des sous-processus de `test_executor_git_boundary.py` combine les ajouts des deux lots sans en perdre.

### 4.2 Raccord routage (A)
- [ ] Chaque rôle réel (CODER, PLANNER, QA, REVIEWER, DEFAULT) : destination, modèle et provenance du credential **réellement émis** (clients factices complets, réseau interdit), y compris appels concurrents de deux rôles de même modèle.
- [ ] Aucune clé globale n'atteint un autre fournisseur ; fournisseur différent sans credential ⇒ erreur avant émission ; local sans clé accepté ; abonnement explicite et cohérent ; contradictions fournisseur/préfixe/modèle refusées (témoins cohérents conservés).
- [ ] Aucun secret dans `repr`, journaux, exceptions (y compris validation de config) ni `argv` ; les scripts worker copiés dans l'image restent autonomes (tout import ajouté exige un `COPY`).
- [ ] Budget : réservation sur la destination **effective**, retries et replis compris ; aucune régression des tests de W2 ; refus des transports non bornables inchangé.
- [ ] Points d'image à prouver en CI (construction OpenHands, kwargs du SDK verrouillé, smoke) listés pour la publication.

### 4.3 Raccord métier (B)
- [ ] Scénario déterministe de bout en bout par les entrées publiques, trois tâches **sérialisées**, base vierge, contenu intégré vérifié avant chaque tâche suivante, oracles rouge puis vert (même empreinte), PDF lu par un vrai lecteur avec témoin négatif.
- [ ] Reprise après redémarrage avec le même registre ; amélioration sans régression ; incident puis rollback Phase 5 (comportement ET état durable restaurés) ; restrictions de chemins et protection de base de W3 actives.
- [ ] Rapport avec états distincts (réussie / non exécutée / arrêt budget / échec / validation incomplète) ; une étape simulée est identifiable.
- [ ] Préflight de campagne : échec **avant** toute émission sur transport incompatible, identité de la fixture et protections vérifiées, zéro appel prouvé, aucune clé imprimée.
- [ ] Le workflow ponctuel n'active aucune récurrence, ne déclenche aucun autre test payant et ne modifie aucune protection.

### 4.4 CI, dépendances et non-régressions
- [ ] `.github/workflows/tests.yml` : la CI de PR n'exécute que le scénario **déterministe** ; les cinq noms de checks et les trois étapes PostgreSQL (21/23/5, planchers égaux aux comptes exacts) sont conservés ; toute étape ajoutée a son test de câblage.
- [ ] Dépendances : voir §6 ; `python scripts/locks.py check` (et `--recompile` avec `uv 0.11.33`) verts, audit strict sans avis ignoré, wheel vérifié si le paquet change.
- [ ] Non-régressions : W1 (frontière Git, OAuth), W2 (registre de budget, wheel, migrations, locks), W3 (preuve, oracles, politique de fusion, `task_merges`, migration `0012`).
- [ ] Ruff `check` **et** `format --check` sur `collegue tests`, `git diff --check`, suites complètes 3.11 et 3.12 avec verrous, `TMPDIR` court et `--basetemp` court (socket Unix), suites lourdes sérialisées (`heavy.lock`).

## 5. Livraison en deux étapes

1. **Intégration locale** par C sur SHA figés (jamais avant instruction), candidat figé, rapport, puis arrêt ; validation indépendante de Codex.
2. **Publication** (consigne distincte) : push sans force, PR française par fichier de corps, observation des cinq checks et des revues **sur la tête exacte**, rapport, arrêt. **Une revue absente, en quota ou en erreur n'est pas un avis favorable**, un check absent, sauté ou en attente n'est pas un succès ; tout finding pertinent revient au manager avant fusion.
3. **Fusion** par un ordre séparé sur le SHA exact : squash `--match-head-commit`, sans admin, sans auto-merge différé, sans modification de protection, puis lecture de `main` (parent, arbre) et des cinq checks du **run push** de `main`.
4. **Campagne réelle** : après la livraison W4 et la validation de Codex, **un unique lancement** ordonné à C sur la révision finale ; ni avant, ni répété, ni relancé automatiquement.

## 6. Dépendance de vérification PDF (décision prise : `pypdf` dans l'extra de test `dev`)

- **Besoin établi** par les fichiers en cours de B (`tests/w4_business_fixture.py` : `from pypdf import PdfReader` dans le test métier et l'oracle scellé ; `collegue/pilot/w4_business.py` : `pypdf` dans `ORACLE_MODULES` et extraction réelle du texte). FastAPI et Alembic étaient déjà verrouillés.
- **Pin retenu** : `pypdf>=6.19.0,<7.0.0` dans l'extra `dev` de `pyproject.toml`, résolu par `scripts/locks.py generate dev` (uv `0.11.33`, gel `2026-10-08T00:00:00Z`) en **`pypdf==6.19.0`** (artefact `py3-none-any`, 2 empreintes SHA-256 — identiques à celles de la fermeture OpenHands déjà verrouillée). Seul `locks/dev.txt` change (cible modifiée ; les cinq autres verrous et `requirements*.txt` sont identiques) ; **aucun transitif** sous Python ≥ 3.11 (`typing_extensions` seulement avant 3.11). Preuves : `reports/w4-c-pdf-dependency.md`.
- **Ce n'est pas une dépendance runtime** : ni le wheel, ni l'image runtime, ni `locks/runtime.txt` / `locks/audit.txt` ne l'embarquent. Les dépendances de la fixture (FastAPI/SQLite/Alembic/PDF) ne deviennent pas des dépendances de Collègue.
- **Reproductibilité** : installation depuis le verrou haché uniquement (`pip install --require-hashes --no-deps -r locks/dev.txt`, utilisé par les jobs `Pytest` et par le nightly) ; contrainte minimale extraite mécaniquement pour les environnements de validation : `evidence/w4-c-pdf-requirements.txt`. **Aucun téléchargement implicite à chaque test** : un test qui exige le lecteur échoue explicitement s'il manque, sans skip. Les workers copiés dans l'image restent autonomes et n'embarquent pas ce lecteur.
- **Portée d'audit à ne pas arrondir** : le job CI « Dependency audit » audite l'environnement installé (`locks/audit.txt`, runtime + dashboard + `pip-audit`) **et**, depuis l'étape `Audit locked dev dependencies (strict)` (`pip-audit --strict --desc --no-deps --disable-pip -r locks/dev.txt`, aucun avis ignoré), le verrou `locks/dev.txt` pypdf compris. Cette étape évalue les marqueurs du verrou contre l'interpréteur du job (Python 3.12, Linux) : les entrées réservées à Python < 3.12 (`backports-tarfile`, `importlib-metadata`, `zipp`) **ne sont pas** auditées ; ce n'est pas un audit de la fermeture 3.11. `pypdf==6.19.0` a aussi été audité séparément lors de l'ajout. Preuve : `reports/w4-c-ci-preparation.md` (étape non encore exécutée en CI à la date de ce document).
- **Besoin d'image restant, à établir par B avant l'oracle rouge** : `_build_gate_sandbox` sélectionne le même `SANDBOX_IMAGE` que le codeur (credentials retirés) ; le défaut `collegue-sandbox:latest` (`locks/sandbox.txt`) **ne contient pas** `pypdf`, alors que l'image OpenHands le contient (transitif, même version) et que le workflow de campagne de B sélectionne l'image OpenHands. Il ne faut donc supposer aucune image de gate distincte : il faut **prouver l'image effective du run et la présence de ses dépendances d'oracle** (dont `pypdf`) avant l'oracle rouge, sans nouvelle configuration inutile. Le contrôle d'image de C (`scripts/ci_w4_worker_routing.py`) vérifie `pypdf` dans l'image OpenHands construite par la CI, ce qui n'est pas cette preuve ; aucun ajout à l'image n'est fait.

## 7. Limites à citer sans les arrondir

- **Budget (W2)** : la garantie stricte ne porte que sur les transports bornables ; les transports non bornables sont refusés ; un worker API dont les credentials sont accessibles au workspace est refusé en strict ; abonnement compatible avec un plafond USD seul, pas avec un plafond de tokens strict ; attestations de prix/tokenizer déclarées par l'opérateur ; droit de planification de 2 h sans battement de cœur.
- **Preuve et fusion (W3)** : Contents API en **texte seul** (formats non représentables refusés) ; journal de preuves non signé ; un oracle exécuté dans le **même interpréteur** que pytest ne résiste pas à du code arbitraire hostile ; l'API REST n'offre **aucune précondition atomique sur la base**, compensée par une **protection serveur stricte réellement applicable** (sinon refus) ; GitHub App refusé faute d'identité d'acteur prouvée ; **une PR validée sur une ancienne base n'est pas re-livrée automatiquement** (deux PR simultanées, PR empilées).
- **Vague 4** : aucune campagne réelle démarrée ; une validation sans modèle réel (préflight bloqué, scénario déterministe) n'est jamais présentée comme une validation réelle ; les revues externes (Copilot, Codex GitHub) ont été indisponibles par quota sur les PR précédentes et ne sont pas des avis favorables.

## 8. Contradictions et points de vigilance à relever dès les rapports d'interface

Rien n'est encore publié par A ou B. À vérifier à leur publication, et à signaler au manager (fichier, raison, test attendu) :
- le préflight de B appelle-t-il une API publique de A **sans émission** (et non une copie des règles de `worker_budget`) ?
- `collegue/sandbox/executor.py` (A, raccord de credential par référence) préserve-t-il toutes les gardes W1 (montages, `git_control_exposure`) ?
- les champs de config ajoutés par A (`config.py`, `.env.example`) sont-ils tous lus par le harnais de B par la résolution publique, sans copie divergente ?
- le workflow ponctuel de B est-il bien disjoint du nightly (aucun autre test payant) et du workflow `tests.yml` de C ?
- un lecteur PDF est-il réellement nécessaire, et la fixture reste-t-elle hors des dépendances runtime ?
- les tests de B sont-ils compatibles avec `STRICT_MAX_INFLIGHT_PRS=1` et la politique de fusion de W3 sans désactivation ?
