# Vague 3 — protocole et checklist d'intégration (C)

**État : intégré localement, non publié.** Le document a été rédigé **avant** les livraisons de A et B ; les §1 à §6
décrivent le protocole et la checklist, qui restent la référence de revue. Intégration : lot A `ee15e0b` et lot B `c4e63c6`
fusionnés `--no-ff` sur la branche de C (résultats chiffrés, SHA et preuves : `reports/w3-c-integration.md`, hors dépôt).
Les §3 (« points de raccord existants ») décrivent l'état de `58355a4`, **avant** les lots : ils ne décrivent plus le
code courant. Les garanties ne sont établies que sur le SHA figé ; les cinq checks distants et les revues restent à
observer. Règles générales : [`AGENTS.md`](../../AGENTS.md) ; protocole et checklists précédentes :
[`protocole.md`](protocole.md) ; vague 2 : [`w2-integration.md`](w2-integration.md) ; lots : [`w3-quality.md`](w3-quality.md)
(A) et [`w3-merge.md`](w3-merge.md) (B).

- **Base commune** : `main` = `58355a44469360c6212689f094d26199c48535ea` (PR #610, arbre `c20f5f8227795ab5e677c332da8299252108f312`, parent unique `9862b39`, cinq checks du push verts).
- **Branches** : A `codex/consolidation-w3-a`, B `codex/consolidation-w3-b`, C `codex/consolidation-w3-c` (même base, développement parallèle).
- **Objet** : unifier la **preuve de livraison BUILD/IMPROVE** (A) et **sécuriser les fusions** (B). Contrat commun fixé par le manager : `briefs/w3-interface.md` (hors dépôt, sous le dossier de campagne).
- **Non-objectifs** : routage fournisseur/modèle par rôle et MVP fixture (vague 4), campagne réelle, tout changement de dépendances (`pyproject.toml` et `locks/` restent inchangés sauf besoin établi remonté au manager), toute modification des protections du dépôt ou des noms des cinq checks requis.

## 1. Propriété des fichiers

| Qui | Possède |
|---|---|
| **A** | `collegue/executor/{delivery_proof (nouveau),pr,pipeline,quality_gate}.py`, `collegue/planner/acceptance_tests.py`, `collegue/improve/{loop,gate,metrics}.py`, nouveaux modules de contrôle sous `executor/` ou `improve/` ; tests `test_executor_pr`, `test_executor_pipeline`, `test_executor_quality_gate`, `test_acceptance_gate`, `test_planner_acceptance_tests`, `test_improve_*`, nouveaux tests de preuve ; `docs/consolidation/w3-quality.md` |
| **B** | `collegue/pilot/{runtime,automerge,guard,remote_revert}.py` et le nouveau module commun de politique de fusion sous `pilot/` ; `collegue/tools/github_commands/{prs,branches,files,__init__}.py` et d'éventuels clients rulesets/checks ; `collegue/state/{models,manager}.py` et la migration additive `0012` sous `collegue/migrations/versions/` ; `collegue/config.py`, `.env.example` ; tests `test_pilot_*` (hors nightly, réservé à la vague 4), `test_phase5_incident_state`, `test_project_state`, `test_github_commit_branch`, `test_budget_pilot`, `test_budget_adequacy` (raccords runtime) et nouveaux tests clients/politique ; `docs/consolidation/w3-merge.md` |
| **C** | `AGENTS.md`, `CLAUDE.md`, `protocole.md`, ce document ; tests de raccord dédiés ; adaptations **mécaniques et explicites** dans les fichiers runtime de B ou executor de A, **seulement après gel des deux lots** |

Un fichier a un seul propriétaire. Un besoin sur un fichier d'un autre rôle s'écrit dans le rapport (fichier, raison, changement voulu), il ne s'édite pas. Une modification fonctionnelle de raccord revient à l'auteur. Un nouveau réglage de gate de A dans `config.py` est spécifié au manager puis appartient à B ou au raccord C, jamais à une édition concurrente.

## 2. Contrat de preuve (fixé par le manager, à vérifier, pas à réécrire)

Les éléments ci-dessous sont le **contrat de départ**. Rien n'est encore livré ni testé ; chaque point devient une garantie seulement quand la preuve sur SHA figé l'établit.

- **Source durable.** A persiste la preuve dans le journal de décisions existant (`ProjectStateManager.record_decision` / `get_decision_journal`), en données immuables ; le journal n'expose pas de mise à jour, une ancienne preuve n'est jamais écrasée. Les données vivent dans l'état contrôlé, hors du workspace. Une contrainte de schéma nouvelle exige l'arbitrage du manager avant modification de fichiers B.
- **Chargement.** `executor/delivery_proof.py` expose `load_delivery_proof(manager, project_id, *, owner, repo, pr_number, head_sha)`. Retour : objet immuable avec attributs en lecture seule `proof_id` (SHA-256 du contenu canonique), `owner`, `repo`, `project_id` (int), `pr_number` (int), `head_sha`, `base_sha`, `tree_sha`, `phase` (`build`/`improve`), `passed` (vrai booléen), `verdicts`, `oracles`. Tout refus (absent, invalide, incohérent) lève `DeliveryProofError(RuntimeError)` ; aucun objet partiel présenté comme valide. B consomme ces attributs, jamais une sérialisation interne ni le corps de PR.
- **Identité.** `base_sha` est la base Git de confiance des contrôles ; `tree_sha` est l'arbre Git **complet** du contenu testé et livrable, modes inclus, et pas un hash du diff. Les oracles portent leurs empreintes et motifs de verdict ; une obligation requise manquante empêche `passed`. Un `passed=true` fourni par un appelant n'est pas une preuve.
- **Liaison au remote.** A vérifie l'objet commit distant (`branches.get_git_commit(owner, repo, sha)` → `sha/tree_sha/parents` ; `prs.get_pr(...)` → SHA de tête/base) avant de lier la preuve à `head_sha`. Dérive, PR existante de contenu différent, fichier omis, suppression ou mode non représenté ⇒ refus. Refuser un format non pris en charge est acceptable ; annoncer une preuve complète en sautant un binaire ou un lien ne l'est pas.
- **Côté B.** Charger la preuve de la tête effectivement observée, vérifier résultat, base et arbre via les objets distants, puis appliquer checks requis et contraintes Phase 5. La reprise conserve `proof_id`, `head_sha`, `base_sha`, `tree_sha`, SHA distant livré et état de synchronisation. L'absence de preuve d'une ancienne PR ne se reconstruit **jamais** depuis son texte : arrêt explicite et revalidation.
- **Publication.** `branches.ensure_commit_branch` ne pousse pas les blobs d'un workspace local (usage actuel : revert vers un arbre déjà présent). Le hash local d'un objet ne le rend pas disponible sur GitHub. La Contents API peut rester limitée au texte si tout format non représentable est refusé **avant** de déclarer la livraison complète. Toute primitive Git Data supplémentaire est spécifiée au manager (signatures exactes confiées à B), puis intégrée par C.

### Concurrence distante : ce qui n'est pas garanti

L'API REST de fusion protège la **tête** avec `sha` ; elle n'offre **aucun** paramètre atomique `expected_base_sha`. Aujourd'hui `PRCommands.merge_pr` (`prs.py`) fait un `GET` de la PR, compare tête/base attendues (`expected_head_sha`, `expected_base_branch`, `expected_base_sha`), puis un `PUT` avec `sha` : la comparaison de **base** est une lecture suivie d'un appel, donc non atomique. Aucun texte, test ni rapport ne doit présenter cette comparaison comme une garantie atomique. La précondition de base doit venir d'une protection stricte réellement applicable côté serveur à la branche **et** à l'acteur de fusion (ruleset actif, branche à jour exigée, aucun bypass applicable) ; sinon l'automatisme est refusé avec la limite expliquée. Les protections du dépôt ne sont jamais modifiées pour rendre un test vert.

## 3. Points de raccord existants (lus sur `58355a4`, avant tout travail de A/B)

Constats factuels, à re-vérifier contre les SHA livrés ; ils décrivent l'état de départ, pas la cible.

| Point | État de départ |
|---|---|
| Activation du merge-bot BUILD | `collegue/pilot/runtime.py` (≈ l. 513) : `bool(getattr(settings_obj, "BUILD_AUTO_MERGE", True)) and not dry_run` — **activé par défaut**, y compris le repli `getattr` |
| Merge-bot | `_try_merge_pr` (≈ l. 319) appelle `prs.merge_pr(owner, repo, number, method="squash")` **sans SHA**, avec relances courtes ; `_merge_in_review_prs` (≈ l. 350) lit les tâches `in_review`, ne contrôle ni tête, ni checks, ni preuve |
| Synchronisation | Après un merge réussi, la tâche passe à `merged` **avant** `_resync_repo_source` ; un échec de resync n'est qu'un `logger.warning`, la tâche reste `merged` et la suivante peut partir d'un clone périmé |
| Client de fusion | `PRCommands.merge_pr` : idempotent (PR déjà fusionnée), compare tête/base par lecture avant le `PUT` (non atomique, cf. § 2), `sha` dans le corps seulement si `expected_head_sha` fourni |
| Phase 5 | `collegue/pilot/automerge.py` : `maybe_auto_merge` exige déjà un SHA de tête connu et transmet `expected_head_sha/base_branch/base_sha` ; `auto_merge_promotion` lit tête et checks (`prs.get_commit_checks`) ; `is_sensitive` (≈ l. 147) bloque `*.lock`, `.env*`, `.github/`, `migrations/`, `alembic/versions/`, extensions/basenames exécutables |
| Classification | Écarts relevés à la fin de la vague 2 : `requirements.txt`, `requirements-lock.txt`, `Dockerfile.openhands`, `docker/sandbox/Dockerfile.openhands` et les six `locks/*.txt` échappent à `is_sensitive` ; la migration empaquetée (`collegue/migrations/versions/…`) et `pyproject.toml` restent protégés (à vérifier) |
| Livraison BUILD | `executor/pipeline.py` : `execute_issue(...)` → `ExecutionOutcome` (`success`, `stage`, `quality_report`, `pr`, `final_status`, `reason`) ; `executor/pr.py` : `capture_delivery_snapshot`, `verify_delivery_snapshot` (dérive du workspace), `open_pr` (idempotent : `find_pr_by_head` ⇒ `skipped`), `PrResult` (`skipped_binaries`, `skipped_symlinks`) ; la preuve n'est portée que par le corps de PR (marqueur de hash de diff) |
| IMPROVE | `improve/loop.py` + `improve/gate.py` (`evaluate`, `GateDecision.accepted`) : décision de promotion par score composite et tolérances par signal ; veto reviewer, oracles et contrats livrés absents du chemin de promotion (constats du manager, préflight `w3-manager-promotion-before.*`) |
| Journal | `ProjectStateManager.record_decision(project_id, summary, rationale)` / `get_decision_journal(project_id, query)` : texte, sans mise à jour |

## 4. Obligations de test

- Rouge sur la base **puis** vert avec les mêmes assertions (auteurs), un **témoin bénin** qui aboutit par l'entrée publique, et le **motif précis** du refus adverse. Un refus dû à un `AttributeError`, un module absent ou un double incomplet n'est pas une démonstration de sécurité.
- Transports factices **complets** (GitHub, agent, gate) ; opérations locales Git/SQLite, persistance et assertions métier **réelles** quand c'est ce qu'on prétend mesurer. Mutations négatives en mémoire ou en fixtures hors dépôt.
- Python 3.11 **et** 3.12 sur les nouveaux chemins ; une analyse syntaxique ne remplace pas l'exécution. PostgreSQL réel sans skip pour tout test de concurrence ou de migration concerné (cluster local sur socket Unix propre au rôle, aucun service partagé).
- Simuler les courses distantes **au niveau des appels réellement émis** : déplacement de tête ou de base au moment du `PUT`, pas seulement avant le dernier `GET`.
- Aucun appel modèle ni écriture sur un dépôt GitHub réel ; aucune image Docker lourde en local (petite image de test existante, sans réseau ni credentials, pour les vrais oracles si pertinent). `TMPDIR` propre au rôle (`/tmp/collegue-consolidation-w3-<rôle>`) ; ne pas supprimer les anciens `/tmp/collegue-exec-*`.

## 5. Checklist d'intégration (C, après gel des deux lots)

À exécuter **seulement** sur les SHA figés communiqués par le manager et sur instruction distincte. Les cas de raccord passent par les **vraies entrées publiques** (`execute_issue`, boucle IMPROVE, merge-bot/`run_project_from_settings`, `auto_merge_promotion`), jamais par des fonctions privées.

### 5.1 Avant la fusion locale
- [ ] Le SHA de A (resp. B) est la tête de sa branche, descend de `58355a4` et son arbre est celui annoncé ; worktrees propres.
- [ ] Périmètres disjoints : `git diff --name-only 58355a4 <sha>` de A et B n'a aucune intersection ; tout fichier hors périmètre listé est rapporté au manager.
- [ ] Aucun changement de `pyproject.toml`, `locks/`, workflows ni noms de checks ; si présent, arrêt et rapport.
- [ ] Fusion `--no-ff` par SHA, un conflit fonctionnel retourne à l'auteur ; si les deux lots ajoutent une migration, la numérotation (`0012`) et les `down_revision` sont cohérents, le fichier est **empaqueté** sous `collegue/migrations/versions/` et sensible pour la Phase 5.

### 5.2 Raccord 1 — preuve durable après redémarrage
- [ ] Une livraison BUILD **et** une IMPROVE persistent leur preuve après leur gate ; une **nouvelle instance** du manager (nouvelle session, même base) la recharge avec `(project_id, owner, repo, pr_number, head_sha)` ; identités `owner/repo/project_id/pr_number/head_sha/base_sha/tree_sha/phase` exactes, `passed` booléen.
- [ ] Refus avec motif distinct pour : preuve absente, autre PR, autre projet, ancien SHA, `tree`/`base` différents, contenu altéré, champ ou obligation manquante, `passed=true` fourni sans preuve, corps de PR seul.
- [ ] Une preuve n'est jamais écrasée : un second enregistrement s'ajoute, la lecture désigne sans ambiguïté la bonne.

### 5.3 Raccord 2 — contenu complet testé et poussé
- [ ] La reconstruction depuis l'**arbre Git distant** de `head_sha` reproduit le contenu testé ; `tree_sha` calculé sur le contenu complet (modes, suppressions, fichiers de base non modifiés).
- [ ] Refus explicites : binaire requis omis, lien omis, suppression ou changement de mode non représenté, fichier de base modifié pendant le gate, module/donnée nécessaire à l'oracle ignoré ou non suivi, PR existante de même branche et de révision différente.
- [ ] Témoin bénin (livraison texte autonome) réussit par la même entrée ; aucun `engine_error` dû à un double incomplet n'est compté comme refus valide.

### 5.4 Raccord 3 — oracles conservés
- [ ] Même oracle (même SHA-256) : rouge par **assertion en phase d'appel** sur la préimage, vert sur le candidat. Ne valent pas : collecte/import/setup en erreur, zéro test, tests sautés ou `xfail` (total ou partiel), arrêt prématuré sans rapport complet, timeout, panne réseau.
- [ ] Contrats livrés : la tâche 2 rejoue le contrat 1 ; une tâche qui le casse est bloquée ; IMPROVE rejoue **tous** les contrats livrés ; les sources scellées restent dans l'état contrôlé, hors workspace, et des tests modifiés par l'agent ne les remplacent pas. Distinction nouveau contrat (rouge exigé) / non-régression (baseline verte).
- [ ] Les protections du lanceur d'oracles subsistent (import de pytest sous `python -I` avant les chemins du projet, fichier oracle aléatoire en tmpfs, `--noconftest`, config et plugins du projet neutralisés) ; faux `pytest.py`/`conftest`/config du workspace toujours sans effet.

### 5.5 Raccord 4 — veto reviewer et couverture
- [ ] IMPROVE applique oracles et veto reviewer comme BUILD ; un finding bloquant empêche la promotion malgré un score élevé.
- [ ] Couverture 90→80 avec lint amélioré : **refusée** ; couverture indisponible avant/après quand elle est requise : **refusée** ; témoin bénin 80→90 promu. Un score élevé ne compense jamais tests/contrats rouges, revue bloquante, couverture en baisse ou mesure indispensable absente.
- [ ] Le scan de secrets est nommé pour ce qu'il est ; aucune promesse d'analyse de sécurité générale dans la documentation ni le corps de PR.

### 5.6 Raccord 5 — politique commune SHA / checks / base
- [ ] `BUILD_AUTO_MERGE` désactivé par défaut **y compris** quand le réglage est absent (repli `getattr`) ; l'activation est explicite.
- [ ] Un seul chemin de validation partagé par la boucle normale, le drain de fin, la reprise et la Phase 5 ; l'appel de fusion émet le **SHA exact** observé.
- [ ] Checks requis découverts depuis les protections classiques **et** les rulesets applicables (avec `app_id`) ; absent, en attente, `failed`/`cancelled`/`skipped`, mauvaise application, découverte inaccessible ou erreur d'API ⇒ pas de fusion ; pagination couverte ; ruleset seul (sans protection classique) pris en compte.
- [ ] Course : tête ou base déplacée **avant et pendant** l'appel ; ruleset désactivé ou en évaluation, bypass applicable à l'acteur, protection classique contournable ⇒ refus ; l'absence de précondition atomique sur la base est documentée comme limite, jamais déguisée.
- [ ] Classification Phase 5 : `requirements*.txt`, `locks/*.txt`, `Dockerfile*` (dont `docker/sandbox/…`), `pyproject.toml` et migrations empaquetées sont **sensibles** même si l'allowlist est élargie ; les restrictions de faible risque restent intactes (preuve sur chemins concrets).

### 5.7 Raccord 6 — reprise après fusion distante suivie d'un échec de synchronisation
- [ ] Merge distant confirmé puis resync faux ou exception : état durable de synchronisation, **aucun** second appel de fusion, **aucune** tâche suivante construite depuis l'ancien checkout, livraison non comptée comme prête.
- [ ] Crash entre le succès distant et l'écriture locale : au redémarrage, réconciliation GitHub ↔ registre sans double action (`proof_id`, `head_sha`, `base_sha`, `tree_sha`, SHA livré, état de sync conservés).
- [ ] Reprise réussie : resync validée, tâche suivante depuis l'arbre livré ; SHA livré de `main` identique à la preuve.
- [ ] Les gardes de budget et d'échéance de la vague 2 restent actives et le registre n'est pas remis à zéro entre passes.

### 5.8 Non-régressions et livraison
- [ ] W1 : frontière Git (`TrustedGit`, `git_control_exposure`), OAuth fail-closed ; W2 : budget (`BUDGET_MODE`, registre), wheel/migrations/locks (tête de migration attendue, fichier empaqueté), `tests/test_ci_postgres_budget.py` et plancher PostgreSQL 21.
- [ ] Ruff `check` **et** `format --check` sur `collegue tests`, `git diff --check`, suites complètes 3.11 et 3.12 avec locks, PostgreSQL réel sans skip pour les tests concernés, analyse des skips (aucun nouveau dans les fichiers de la vague).
- [ ] Candidat figé (`reports/w3-c-candidate.json`, `testing=true` jusqu'à la fin), rapport, puis publication **seulement** sur instruction : cinq checks requis sur la tête exacte (`Ruff`, `Pytest (Python 3.11)`, `Pytest (Python 3.12)`, `Dependency audit`, `Docker build`), revues lues, finding pertinent transmis au manager avant toute fusion, fusion sur instruction distincte avec correspondance de tête, vérification de `main` (parent, arbre, cinq checks du push).

## 6. Critères de revue (refus par défaut)

Refuser et renvoyer à l'auteur si : une garantie est annoncée sans preuve sur SHA figé ; un test adverse repose sur un mock incomplet ; le témoin bénin manque ; la preuve se lit dans le corps de PR ou un paramètre d'appelant ; une base déplacée n'est détectée que par une lecture antérieure à l'appel ; un check manquant, sauté ou inaccessible est traité comme vert ; un format non représentable est publié sans refus ; une protection, un nom de check ou un seuil est modifié ; une dépendance est ajoutée sans arbitrage.

## 7. Raccords réalisés et limites

- **Tests de raccord (propriété C)** : `tests/w3_remote_bridge.py` (un `FakeGitHubServer` de B dont commits, branches et commits de fusion
  sont les objets d'un VRAI dépôt Git bare de `FakeRemote` de A, derrière les clients de production `BranchCommands`,
  `FileCommands`, `PRCommands` détournés au seul niveau de `_request_json`), `tests/w3_publication.py` (clients complets pour les
  tests historiques), `tests/test_w3_integration_build.py`, `tests/test_w3_integration_improve.py`. Aucune preuve, aucun
  `verify_fn`, aucune politique de fusion injectés ; la resynchronisation et `verify_local_sync` lisent le commit de fusion réel.
- **Mécanique uniquement** : exports publics `TaskMerge` / `TaskMergeConflictError` (`collegue/state/__init__.py`) ; deux étapes
  CI PostgreSQL dédiées (état de fusion : 23 tests ; preuves de livraison : 5 tests) en plus de celle du registre de budget (21),
  planchers égaux aux comptes exacts ; défaut de `BUILD_AUTO_MERGE` dans README/docs ; clients complets dans les anciens tests de
  pilote/runtime/e2e/budget (les anciens doubles de publication ne suffisent plus, sans drapeau de contournement).
- **Limites** : deux PR simultanées validées sur la même base ne sont pas toutes deux fusionnables (la seconde reste ouverte avec
  une base périmée) ; une PR empilée d'IMPROVE n'est validable vers `main` qu'après sa parente ; Contents API texte seul ; journal
  non signé ; jeton d'application GitHub refusé ; oracle non résistant à du code malveillant dans le même interpréteur.
