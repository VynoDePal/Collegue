# AGENTS.md — règles de collaboration

Source unique des règles pour tout agent (Codex, Claude Code) qui travaille sur ce dépôt.
`CLAUDE.md` n'ajoute que ce qui est propre à Claude Code. Le plan opérationnel détaillé
(vagues, contrats, checklist de revue) est dans [`docs/consolidation/protocole.md`](docs/consolidation/protocole.md).

Langue : français (échanges, commits, PR, documentation).

## Règles générales du dépôt

- Lancer `pytest` depuis la **racine** du worktree : plusieurs tests utilisent des chemins relatifs au dépôt.
- La CI exécute `ruff check collegue tests` **et** `ruff format --check collegue tests`. Lancer les deux sur
  l'arbre complet, pas sur les seuls fichiers modifiés (un import ajouté dans un `__init__.py` casse l'ordre isort ailleurs).
- Tests `integration` (LLM, GitHub, Sentry, Postgres, K8s réels) : exclus par défaut, jamais lancés sans consigne écrite du manager.
- Environnement de test durable (depuis le 2026-10-08) : les dépendances communes vivent sous
  `~/.codex/collegue-consolidation/20260928/envs/dependencies-20261008` (versions figées dans
  `evidence/w1-env-freeze-20261008.txt`) ; chaque venv de rôle (`envs/<rôle>`) y accède par un `.pth` et possède ses
  propres scripts console. `/tmp` n'héberge plus aucune dépendance. **Aucun paquet `collegue` n'est installé** dans ces
  venv : le code importé est celui du worktree, d'où l'obligation de lancer depuis sa racine. Ne jamais
  `pip install` dans un venv partagé ni dans `dependencies-20261008`.
- Ne jamais afficher ni copier une clé, un token ou le contenu d'un fichier d'authentification.
- Pas de force-push. Mettre une branche à jour par `git merge`, jamais par rebase suivi d'un push forcé.
- Pas de `git stash` nu : la pile est partagée entre worktrees. Utiliser un commit WIP.

## Protocole de consolidation (5 vagues)

Le chantier de consolidation corrige des garanties incomplètes identifiées par l'audit du 2026-09-28
(base `main` = `51ab3fc`). Chaque vague est livrée **et vérifiée** avant la suivante.

### Rôles

| Rôle | Qui | Fait | Ne fait pas |
|---|---|---|---|
| Manager / testeur | Codex | Arbitre, écrit les briefs, relit, teste indépendamment sur un checkout propre du SHA intégré, accepte ou refuse un SHA | Aucun code produit |
| Implémenteur A | Claude Code (session réelle) | Implémente son lot, tests rouge→vert, commits **locaux** | Push, PR, merge, fichiers d'un autre rôle |
| Implémenteur B | Claude Code (session réelle) | Idem, en parallèle de A, sur un périmètre disjoint | Idem |
| Intégrateur C | Claude Code (session réelle) | Fusionne A/B dans la branche de vague, relit les diffs, teste, **seul** à pousser, ouvrir la PR et fusionner | Réécrire un lot en silence, fusionner sans acceptation |

- A, B et C sont trois sessions Claude Code distinctes et simultanées. Aucun sous-agent, session annexe ou modèle de remplacement, ni pour Claude ni pour Codex.
- Une session ne change pas de modèle et reste reprise par le manager pour les corrections ou la vague suivante.
- Le manager ne modifie pas le code produit ; C ne modifie le code de A/B que pour un raccordement mécanique (import, signature, renommage) et le signale.

### Frontière Git et sources de confiance (depuis la vague 1)

Un workspace est écrit par du code non fiable (agent, tests du gate) : son `.git` (config, hooks, `core.fsmonitor`,
filtres, `diff.external`, `HEAD`, index, gitfile ou lien symbolique) ne doit jamais être exécuté ni lu par l'hôte.
Détail et inventaire : `docs/consolidation/w1-isolation.md`.

- Toute opération Git **hôte** sur un workspace passe par `collegue.executor.git_boundary` : `TrustedGit` (workspace géré :
  `GIT_DIR` = répertoire de contrôle frère `<workspace>.control`, hors de tout montage, `GIT_WORK_TREE` = workspace,
  environnement reconstruit, options neutralisées en `-c`) ou `HardenedGitRunner` (clone plat créé par l'hôte et jamais
  monté : revert, santé de `main`). Ne jamais relire `<workspace>/.git` : c'est une copie jetable pour l'agent.
- **Source de confiance** : la base de livraison est `trusted_base(workspace)` (HEAD du contrôle) ; `Workspace.base_commit`
  n'est que le SHA du clone initial. `advance_base` la fait avancer (compounding). `repo_source` (checkout de l'opérateur)
  et un clone neuf jamais monté sont de confiance ; un workspace géré ou un répertoire de contrôle ne l'est jamais
  (`require_trusted_checkout`).
- `LocalCommandRunner` est réservé aux fixtures de confiance et aux lectures sur `repo_source` ; il refuse (126) un workspace
  géré. Jamais un défaut de production sur un workspace : `runner=None` passe par la frontière, un workspace non géré lève
  `WorkspaceError` (fail-closed, aucun repli silencieux). Un runner injecté est refusé sur un workspace géré.
- **Tous** les bind mounts passent par `collegue.sandbox.executor.git_control_exposure` : workspace, cache pip, auth
  d'abonnement de `DockerSandbox`, et auth (RW) + script (RO) du sampler de `core/llm/sampling_ctx.py`. Un montage est
  refusé s'il est, contient (à toute profondeur, parcours borné sans suivre de liens) ou se trouve sous un répertoire
  portant `GIT_CONTROL_MARKER`. **Aucune dispense**, pas même pour un workspace géré (un autre contrôle peut y être
  imbriqué). Une erreur n'est jamais une absence : seules `ENOENT`/`ENOTDIR` établies par `lstat` autorisent un chemin « à
  créer » ; `EACCES`, lien pendant, boucle, bornes dépassées ⇒ refus (le démon Docker, plus privilégié, franchirait un
  parent non traversable). N'utiliser ni `os.path.exists`/`isdir`/`lexists` pour décider d'une exposition, ni un second
  constructeur de `docker run -v` hors de cette garde.
- Noms de fichiers venant de l'agent : lus ou écrits sur l'hôte seulement via `collegue.sandbox.paths.workspace_file`
  (ni `..`, ni lien symbolique suivi, ni sortie du workspace). L'audit de dépendances ne s'exécute jamais sur l'hôte ; une
  mesure indisponible est refusée (composite non fini), jamais comptée comme zéro.
- Tout nouveau sous-processus hôte doit être inventorié (le test `test_host_subprocess_usage_is_inventoried` échoue sinon) ;
  un changement qui y touche est revu par C avec la checklist du protocole (§5.1).

### Worktrees et branches

- Un worktree par rôle, hors du checkout utilisateur : `collegue-consol-{a,b,c}` (+ `qa` pour Codex, HEAD détaché).
- Branches : `codex/consolidation-w<N>-{a,b,c}` ; la branche de C est la branche de vague.
- Ne jamais toucher `main` ni le checkout utilisateur (`~/Documents/Collegue`). Ne jamais travailler hors de son worktree.
- Chaque rôle a son venv, sa base d'état et son `COLLEGUE_HOME` (sous le répertoire de la campagne). Ne pas modifier le venv partagé de l'audit.

### Propriété des fichiers

- **Un seul propriétaire par fichier et par vague.** Le brief de la vague donne le partage ; A et B n'ont aucun fichier en commun.
- `AGENTS.md`, `CLAUDE.md` et `docs/consolidation/protocole.md` appartiennent à C.
- Besoin de toucher un fichier d'un autre rôle : l'écrire dans son rapport (fichier, raison, changement voulu) et continuer les parties indépendantes. Ne pas l'éditer.
- Conflit fonctionnel entre lots ou échec indépendant du lot : le rapporter précisément, ne pas le masquer.

### Trois niveaux de tests

1. **Auteur (A/B)** — chaque défaut significatif est d'abord reproduit par un test **rouge**, puis le **même test** est vert après correction. Tests pertinents + Ruff (check et format, arbre complet) avant chaque commit.
2. **Intégration (C)** — suite complète, Ruff complet, installation et Docker pertinents, sur la branche de vague après fusion de A/B. Revue des diffs et recherche des usages oubliés (voir la checklist du protocole).
3. **Acceptation indépendante (Codex)** — tests propres du manager sur un checkout propre du **SHA intégré**, puis CI distante.

Interdits : assouplir un test pour cacher une régression, `xfail`/`skip` opportuniste, réduire un seuil. Une preuve manquante rend la validation **incomplète**, jamais réussie par défaut. Un test qui ne peut pas échouer n'est pas une preuve.

### Preuves et rapports

- Rapports : `~/.codex/collegue-consolidation/20260928/reports/w<N>-<rôle>.md` ; preuves : `.../evidence/w<N>-<rôle>-*`. Chaque rôle n'écrit que ses fichiers préfixés.
- Un rapport donne : commits (SHA), fichiers modifiés, commandes exécutées avec code de retour et résultat, interfaces changées, limites et points non couverts.

### Livraison et merge (C uniquement)

1. C fusionne les commits de A/B dans la branche de vague, résout les raccordements mécaniques, retourne les conflits fonctionnels aux auteurs.
2. C lance le niveau 2, puis donne au manager le SHA exact et les résultats. Codex teste (niveau 3).
3. C pousse la branche de vague et ouvre la PR vers `main` **seulement après consigne du manager**.
4. **Livraison en deux étapes.**
   - *Étape 1 — C publie et observe, sans fusionner.* Après l'ouverture de la PR, C observe les 5 checks requis — `Ruff`, `Pytest (Python 3.11)`, `Pytest (Python 3.12)`, `Dependency audit`, `Docker build` — **et** les revues (automatiques ou humaines) sur la tête. Il rapporte au manager : URL, SHA de tête et de base, résultat de chaque check, état de chaque revue, texte intégral de chaque finding (fichier, ligne) avec son évaluation.
   - *Étape 2 — Codex ordonne.* Après cet examen, Codex donne l'instruction de fusion finale **sur cette tête exacte**. C fusionne alors seulement (squash, tête attendue contrôlée) et vérifie `main` ensuite. Sans instruction pour ce SHA, C ne fusionne pas, même si tout est vert.
5. **Portée de l'acceptation.** L'acceptation du manager porte sur la révision **et** sur les éléments connus au moment où elle est donnée. Tout nouveau finding de revue distante pertinent pour les critères de la vague est transmis au manager **avant** la fusion, pour arbitrage explicite (corriger maintenant, ou accepter une limite justifiée). C ne le reporte pas de sa propre initiative à la vague suivante et ne se contente pas de l'ajouter aux limites de la PR. Le manager tranche dans le périmètre déjà autorisé : l'utilisateur n'est pas sollicité pour cela.
6. **Une preuve absente n'est jamais une preuve réussie.** Check manquant, en attente, ignoré ou annulé ; revue non faite, en cours ou en échec (quota, indisponibilité) ; test non exécuté : cela se rapporte tel quel et ne se présente jamais comme un succès ni comme une absence de finding.
7. Tout changement de SHA (commit, merge de `main`, correction) invalide l'acceptation et les preuves concernées : les refaire sur le nouveau SHA.
8. Pas de bypass : ni force-push, ni contournement du ruleset, ni admin merge, ni auto-merge différé sur une tête mouvante, ni reconfiguration des protections, ni modification des noms de checks requis.
9. Après la fusion, vérifier `main` (SHA, arbre identique à l'arbre accepté, 5 checks du push) avant de démarrer la vague suivante. Une vague n'est **clôturée** qu'une fois `main` vérifié et aucun finding pertinent resté ouvert ; une vague livrée avec un finding ouvert est « livrée partiellement, non clôturée ».

### Vague 2 (budget durable ; distribution installable)

Base `9862b39` (vague 1 close). Protocole, raccordements, checklist et état intégré : `docs/consolidation/w2-integration.md`.
- A possède le registre de budget (`state/budget_ledger.py`, `core/llm/budget_guard.py`), `state/{models,manager}.py`, la migration `0011` et le runtime/worker de dépense ; B possède le packaging, les locks, les ressources, les migrations **existantes**, les workflows et les Dockerfiles. `0011` vit dans `collegue/migrations/versions/` (déplacée mécaniquement par C, mêmes identifiants `0011`/`0010`). C ne réécrit aucun comportement de A ou B.
- **Budget strict ou advisory.** `BUDGET_MODE=strict` (défaut) : chaque appel émis par le framework est réservé **avant** l'émission, un usage inconnu bloque durablement, un transport non bornable est **refusé**. `advisory` enregistre sans bloquer et **ne garantit rien**. La garantie ne couvre pas un programme du workspace qui disposerait d'une clé ; un abonnement n'est accepté que pour un plafond USD (pas de plafond de tokens strict) ; un fournisseur, un modèle ou un endpoint non reconnu est refusé sans prix ou attestation de l'opérateur. Ne jamais écrire « plafond garanti » sans cette portée (`docs/consolidation/w2-budget.md`).
- **Cycle.** Une planification relancée avec la même identité reprend le même solde ; un nouveau cycle est explicite (`--cycle-id`). Un historique cumulatif décroissant ou invalide est déclaré ambigu et bloque la suite stricte jusqu'à résolution.
- **Prompts.** L'état modifiable vit sous `$COLLEGUE_HOME/prompts` ; l'ancien état d'une installation précédente est repris sans jamais être modifié (voir `docs/consolidation/w2-installation.md`).
- **PostgreSQL.** Une garantie de concurrence se prouve sur un **service réel**, jamais sur un mock ni un test sauté. Le job `Pytest` requis (3.11 et 3.12) lance `tests/test_budget_ledger_postgres.py` contre un service `postgres:16` et `scripts/ci_require_junit.py` exige que tout ce qui est collecté ait tourné (plancher 21, 0 skip, 0 échec). Les mesures SQLite et PostgreSQL se rapportent séparément. Ne jamais déclencher le nightly existant pour cela (il appelle des modèles).
- Les scripts worker copiés dans l'image sandbox (`oh_runner.py`, `oh_sampler.py`) n'importent pas `collegue` : tout import ajouté exige un `COPY` correspondant.
- Dépendances : `pyproject.toml` est la source unique, `locks/*.txt` en sont générés (`python scripts/locks.py`, vérifié en CI). Toute dépendance produit nouvelle passe par ce mécanisme ; le bootstrap `uv` épinglé mais non haché du job d'audit est la seule exception acceptée. Version d'`uv` : exactement `UV_VERSION` de `scripts/locks.py` (`python scripts/locks.py uv-version`), identique dans le job d'audit et dans `docker/sandbox/Dockerfile.openhands` ; `generate` et `check --recompile` refusent toute autre version, et `uv` reste dans l'environnement audité par `pip-audit --strict` (ne jamais l'en retirer ni ignorer un avis). Pour la changer : `docs/consolidation/w2-installation.md` § « uv de résolution ».
- Disque limité (racine ≈ 1,6 Go, `/tmp` ≈ 6,8 Go) : caches (`UV_CACHE_DIR`, `PIP_CACHE_DIR`) et artefacts propres sous `/tmp/<rôle>-…`, dépendances partagées en lecture seule, aucun build Docker lourd local (la CI fait foi pour l'image).

### Vague 3 (preuve de livraison commune ; fusions sûres) — livrée

Base `58355a4` (vague 2 close). Protocole, répartition, contrat et checklist : `docs/consolidation/w3-integration.md` ; documents de lot : `docs/consolidation/w3-quality.md` (preuve, oracles, IMPROVE) et `docs/consolidation/w3-merge.md` (politique de fusion, état durable). **Livrée** : PR #611 fusionnée en squash, `main` = `5c1cbf51cec854b2fa73e3951273951e7a7212be` (arbre `206d53ac…`, parent unique `58355a4`), cinq checks du push verts (run `37860048078`). Les revues externes ont été indisponibles par quota : aucun avis favorable n'en est déduit.
- A possède la preuve de livraison (`executor/{delivery_proof,contracts,oracle,pr,pipeline,quality_gate}.py`, `planner/acceptance_tests.py`, `improve/*`) ; B possède la politique de fusion et la reprise (`pilot/{merge_policy,merge_cycle,runtime,driver,automerge,guard,remote_revert,phase5_resume}.py`, clients GitHub, `state/{models,manager}.py`, migration `0012`, `config.py`) ; C possède les documents généraux, `tests/w3_*`, `tests/test_w3_integration_*.py` et les raccords mécaniques.
- **Preuve de livraison.** Source d'autorité : une preuve **durable** (journal de décisions de l'état contrôlé, hors du workspace), relue par `load_delivery_proof(manager, project_id, *, owner, repo, pr_number, head_sha)` depuis une nouvelle instance du manager. Le corps de PR et un `passed=true` d'appelant ne sont pas une preuve ; l'absence de preuve d'une ancienne PR n'est jamais reconstruite depuis son texte. La preuve lie base de confiance, arbre Git **complet** et oracles (mêmes SHA-256) ; elle est liée à la tête distante vérifiée. Formats non représentables (binaire, lien, mode exécutable) refusés avant toute écriture distante ; résidus ignorés ou dépôts imbriqués purgés du contenu testé. **Limites** : Contents API texte seul ; journal non signé ; un oracle ne résiste pas à du code malveillant exécuté dans le même interpréteur que pytest (le nonce associe un rapport à un run, il ne signe rien).
- **Contrats et IMPROVE.** `Project.acceptance_tests_required` de l'état prévaut sur le réglage `GATE_ACCEPTANCE_TESTS` ; BUILD rejoue le contrat courant (rouge par assertion sur la préimage, puis vert) et les contrats livrés ; IMPROVE rejoue tous les contrats livrés. Couverture en baisse, mesure requise absente, revue bloquante ou absente, tests rouges bloquent malgré un meilleur score ; aucune dérogation côté appelant.
- **Fusion.** `BUILD_AUTO_MERGE` désactivé par défaut (y compris le repli de lecture), activation explicite ; un seul chemin de validation (preuve, SHA de tête exact, base, checks requis des protections classiques **et** rulesets, `app_id`) pour la boucle, le drain, la reprise et la Phase 5. L'API REST de fusion protège la **tête** (`sha`) mais n'offre **pas** de précondition atomique sur la base : la garantie repose sur une règle serveur « à jour avant fusion » réellement applicable à l'acteur, sinon la fusion est refusée (rôle personnalisé ou inconnu présumé contournant ; jeton d'application GitHub non pris en charge). État durable `task_merges` (migration `0012`, origine `engine`/`external`) : une fusion distante confirmée dont la resynchronisation échoue bloque **toute** tâche (indépendante comprise) jusqu'à resynchronisation prouvée, sans seconde fusion. Chemins sensibles (locks, `requirements*`, Dockerfiles, `pyproject.toml`, migrations) jamais auto-fusionnés. **Limite observée** : deux PR simultanées validées sur la même base ne sont pas toutes deux fusionnables ; après la première fusion, la seconde (base périmée) reste ouverte, à refaire.
- Tests de raccord par les **vraies entrées publiques**, sans `verify_fn`, `proof_loader` ni preuve injectés, sur un Git distant réel derrière les vrais clients GitHub (`tests/w3_remote_bridge.py`). Les tests historiques de publication reçoivent des clients complets (`tests/w3_publication.py`) ; aucun fixture autouse ni drapeau de contournement. Python 3.11 et 3.12, PostgreSQL réel sans skip pour les trois fichiers de preuve (étapes CI dédiées, planchers = comptes exacts).
- Pas de nouvelle dépendance : `pyproject.toml` et `locks/` inchangés. `TMPDIR` propre au rôle, chemins courts (`--basetemp`) pour les sockets Unix.

### Vague 4 (routage par rôle ; preuve métier multi-tâches) — livrée, validation réelle incomplète

Base `5c1cbf5` (vague 3 livrée). Organisation, interfaces, preuves et checklist : `docs/consolidation/w4-integration.md` ; documents de lot : `w4-routing.md` (A) et `w4-e2e.md` (B). **Livrée** : PR #612 fusionnée, `main` = `869ed3c8936b2bfaa72ac9c64746b1420a827825` (arbre `2825ca37…`, parent unique `5c1cbf5`), cinq checks du push verts (run `37903659594`). **La validation réelle avec modèles est INCOMPLÈTE** : la campagne finale (run `37905627368`, `w4-business-final-20261009`, 2 USD / 250 000 tokens / 900 s) a été refusée au préflight `P06-worker-capacity` (`unbounded_transport`) avant toute émission — 0 appel modèle, 0 USD ; son unique lancement est consommé. Les étapes métier avec modèle réel (BUILD, amélioration R04, incident/rollback R05) restent **sans preuve** ; R04 et R05 n'étaient pas câblés dans le lanceur réel. La consolidation globale n'est pas terminée.
- A a livré la résolution cohérente fournisseur/modèle/endpoint/authentification par rôle jusqu'aux appels émis (`core/llm/*`, adaptateurs OpenHands, `worker_budget`, factory de `pilot/runtime.py`) ; B le scénario métier déterministe (FastAPI + SQLite + Alembic, trois tâches dépendantes sérialisées, PDF lu par un vrai lecteur, reprise, amélioration, incident et rollback Phase 5 déterministes) et le préflight de campagne ; C les documents, `tests.yml`, le lecteur PDF de test dans l'extra `dev` seulement, et les raccords.
- **Routage.** Une résolution unique de la destination réellement émise ; une clé ou un endpoint global ne s'hérite que pour le même fournisseur ; fournisseur sans credential adapté ⇒ erreur avant émission ; abonnement sélectionné explicitement ; aucune combinaison non supportée convertie en silence ; aucun secret dans `repr`, journaux, exceptions ni `argv`. La réservation de budget porte sur la destination effective, retries et replis compris.
- **Limites à citer sans les arrondir** — W2 : plafond strict seulement pour les transports bornables (non bornables refusés), abonnement sans plafond de tokens strict ; W3 : Contents API texte seul, journal non signé, oracle dans le même interpréteur, base distante exigeant une protection stricte applicable (pas de précondition REST atomique), pas de re-livraison automatique d'une PR validée sur une ancienne base, GitHub App refusé ; W4 : audit des verrous limité aux marqueurs Python 3.12/Linux, témoins Docker avec relève hôte allongée, revues externes (Copilot, Codex GitHub) indisponibles par quota — aucun avis favorable n'en est déduit.

### Vague 5 (qualification Gemma 4 : courtier budgétaire, R04/R05 réels) — autorisée, en cours

Base `869ed3c` (vague 4 livrée). Répartition, contrats, préparation de C, checklist d'intégration et décisions ouvertes : `docs/consolidation/w5-integration.md` ; documents de lot attendus : `w5-broker.md` (A) et `w5-business.md` (B). **État exact : W1 à W4 livrées ; W5 en cours — A32 (`ab90dd5`, descend de A31 `1ea690a`, A30 `e0f8b8b`, A29 `f467978` ; sans le commit de supervision `6e61b44`, refusé) et B27 (`68d50f7`, descend de B24 `4953a43`) sont intégrés LOCALEMENT dans la branche de C (candidat non publié, validation indépendante puis ordre de publication distinct à venir) ; zéro appel modèle, aucune campagne réelle démarrée, aucun secret créé.** L'autorisation est celle du plan « Qualification complète de Collègue avec Gemma 4 » : c'est une **nouvelle** campagne, pas une relance de celle de W4.
- A possède le **courtier budgétaire** (`collegue/broker/**`, `LLM_TRANSPORT=budget_broker`, état durable `0013`, relais embarqué, `sandbox/executor.py`, `pilot/runtime.py`) ; B le câblage métier réel (R04 amélioration livrée, R05 incident déterministe explicite avec vrais contrôles, nettoyage unique après toutes les phases, validation du socle de la fixture par l'API, identité de campagne consommée) ; C les workflows, Dockerfiles, `pyproject.toml`, `locks/`, scripts CI et la préparation GitHub de la fixture. Ne pas intégrer A ou B avant leurs SHA figés et l'instruction du manager.
- **Transport.** `direct` (historique) ou `budget_broker` ; la destination reste Google/Gemini. Le codeur tourne `--network none` et ne parle qu'à un relais loopback qui recopie vers un socket Unix du courtier ; **la clé n'existe que dans le service du courtier**, jamais dans le codeur, le gate, les tests ni le nightly. Toute inconnue bloque ; une estimation de secours est interdite (incompatibilité `countTokens`/borne ⇒ refus explicite).
- **Campagne** : `gemma-4-31b-it` pour tous les rôles, repli du codeur seulement `gemma-4-26b-a4b-it`, **2 USD / 250 000 tokens / 900 s globaux** (canaris des deux modèles compris, échéance persistée dès l'ouverture réelle Google), identifiant neuf consommé au lancement (aucun rerun), clé dans un secret d'environnement GitHub temporaire propre à la campagne injecté puis retiré par le manager. **Aucun dispatch ni lancement sans ordre exact du manager.**
- **Fixture** : `main` (graine `8e3691d8…`, ruleset 18840666, sans workflow, branche par défaut immuable) n'est jamais touché. Le socle V1 (`10750c7b…`, branche `collegue-business/bootstrap-w5`, conservé comme historique) et le ruleset 24793056 ont été APPLIQUÉS (C47) ; **le socle V2 (`ad56c0fa03066b1f6efb3cf6a5aa26cb496372ca`, arbre `6e6bea13…`, branche `collegue-business/bootstrap-w5-v2`) est APPLIQUÉ et éprouvé (C49) et sert à la campagne** ; V1 :  : workflow `pull_request` + `push` (jamais `pull_request_target`, qui s'exécute sur la branche par défaut), verrou haché de la pile approuvée, `requirements.txt` aligné (seule modification de la graine) ; ruleset sur `refs/heads/collegue-business/*` (PR obligatoire, check `Fixture tests` lié à l'application GitHub Actions, base à jour, création seulement depuis un commit qui a passé le check, aucun bypass). **Le CODEOWNERS à 0 approbation est INEFFICACE** (observé : PR #10-#12 qui modifient workflow, CODEOWNERS et verrou ont fusionné) : ce n'est qu'un signal de revue. La sécurité du chemin Collègue = contrôle de `.github/` ET `ci/` avant publication (B23, `executor/pr.py`) et avant fusion (B), check authentifié par check-run → job → exécution, protections GitHub réelles ; une fusion manuelle hors produit n'est pas couverte. V2 = mêmes fichiers, commentaires corrigés, ruleset identique ; toute nouvelle mutation distante exige l'ordre du manager. La variable de dépôt `W5_BOOTSTRAP_MANIFEST_JSON` n'est pas encore configurée ; environnement et secret Google absents.
- **Image** : le transport broker a son image dédiée (`docker/sandbox/Dockerfile.broker`, verrou `locks/sandbox-broker.txt` audité en strict, SDK/outils OpenHands seuls, pile métier approuvée, patch Gemma en mode `sdk-only`) ; l'image « direct » historique et son verrou `sandbox-openhands` (audit rouge, non réparé) restent inchangés. La clé n'existe que sous le nom `LLM_API_KEY` dans l'étape de campagne, et un scanner de confiance (`scripts/w5_leak_scan.py`) empêche tout dépôt d'un fichier qui la contient.
- Docker/OpenHands lourds exclusivement en CI distante ; ne pas installer la fermeture du SDK en local ; scratch sous `/dev/shm/w5-<rôle>`, `TMPDIR` court.

### Budget et effets externes

- Aucun appel LLM/API réel, aucun build Docker lourd ni téléchargement volumineux sans consigne. Ressources limitées (~6,5 GiB de RAM, ~4 GiB de disque) : builds sérialisés par `flock ~/.codex/collegue-consolidation/20260928/heavy.lock`, preuve de build de préférence par la CI distante, aucun nettoyage global, ni suppression d'images ou de caches de l'utilisateur.
- **Campagne réelle finale : une seule**, plafonnée à **2 USD au total, 250 000 tokens, 900 s**, sans relance payante automatique, sur un dépôt fixture dédié. Ce budget est distinct des quotas Claude Code.
- Aucune campagne réelle avant la livraison de la vague 5, le contrôle de `main` et l'ordre exact du manager.
