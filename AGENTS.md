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

## Protocole de consolidation (4 vagues)

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

### Vague 3 (preuve de livraison commune ; fusions sûres) — en préparation

Base `58355a4` (vague 2 close). Protocole, répartition, contrat de preuve et checklist d'intégration : `docs/consolidation/w3-integration.md`. Cet état est **une préparation** : aucune des garanties ci-dessous n'est livrée tant qu'une preuve sur SHA figé ne l'établit.
- A possède la preuve de livraison (`executor/delivery_proof.py`, `pr.py`, `pipeline.py`, `quality_gate.py`, `planner/acceptance_tests.py`, `improve/{loop,gate,metrics}.py`, oracles et contrats) ; B possède la politique de fusion et la reprise (`pilot/{runtime,automerge,guard,remote_revert}.py`, clients GitHub, `state/{models,manager}.py` et migration `0012`, `config.py`, `.env.example`) ; C possède les documents généraux et les raccords mécaniques, **après gel** des deux lots. Aucun fichier commun implicite.
- **Preuve de livraison.** La source d'autorité est une preuve **durable** (journal de décisions de l'état contrôlé, hors du workspace), rechargeable par `load_delivery_proof(manager, project_id, *, owner, repo, pr_number, head_sha)` depuis une nouvelle instance du manager. Le corps de PR et un `passed=true` fourni par un appelant ne sont pas une preuve ; l'absence de preuve d'une ancienne PR ne se reconstruit pas depuis son texte (arrêt et revalidation). La preuve lie base de confiance, arbre Git **complet** (modes inclus) et oracles à leurs empreintes ; un format non représentable est refusé avant que la livraison soit déclarée complète.
- **Fusion.** `BUILD_AUTO_MERGE` désactivé par défaut (y compris le repli de lecture du réglage), activation explicite, mêmes préconditions SHA/checks/base pour tous les chemins de fusion. L'API REST de fusion protège la **tête** (`sha`) mais n'offre **pas** de paramètre atomique `expected_base_sha` : deux lectures successives ne prouvent rien ; la précondition de base doit venir d'une protection stricte réellement applicable à la branche et à l'acteur, sinon l'automatisme est refusé. Une fusion distante confirmée suivie d'un échec de synchronisation est un état durable à reprendre, sans seconde fusion ni tâche suivante sur un checkout périmé.
- Tests de raccord par les **vraies entrées publiques**, avec témoin bénin et motif du refus vérifié ; un mock incomplet n'est pas une preuve de sécurité. Python 3.11 et 3.12, PostgreSQL réel sans skip pour les tests concernés. Les cinq noms de checks et les protections restent inchangés.
- Pas de nouvelle dépendance : `pyproject.toml` et `locks/` sont inchangés sauf besoin établi remonté au manager. `TMPDIR` propre au rôle sous `/tmp/collegue-consolidation-w3-<rôle>`.

### Budget et effets externes

- Aucun appel LLM/API réel, aucun build Docker lourd ni téléchargement volumineux sans consigne. Ressources limitées (~6,5 GiB de RAM, ~4 GiB de disque) : builds sérialisés par `flock ~/.codex/collegue-consolidation/20260928/heavy.lock`, preuve de build de préférence par la CI distante, aucun nettoyage global, ni suppression d'images ou de caches de l'utilisateur.
- **Campagne réelle finale : une seule**, plafonnée à **2 USD au total, 250 000 tokens, 900 s**, sans relance payante automatique, sur un dépôt fixture dédié. Ce budget est distinct des quotas Claude Code.
- Aucune campagne réelle avant la fin de la vague 4 et la validation du manager.
