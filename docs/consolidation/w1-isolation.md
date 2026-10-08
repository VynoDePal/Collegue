# Vague 1 (mission A) — frontière Git/sandbox

Objectif : aucun code ni aucune configuration Git écrits par l'agent ou par les
tests ne doit s'exécuter sur l'hôte, et la base/livraison d'une tâche doit rester
une autorité que ni l'agent ni les tests ne peuvent falsifier.

Implémentation : `collegue/executor/git_boundary.py` (frontière),
`collegue/executor/workspace.py`, `runner.py`, `revert.py`,
`collegue/sandbox/executor.py`, `collegue/improve/loop.py` (compounding).
Tests : `tests/test_executor_git_boundary.py`, `tests/test_sandbox.py`,
`tests/test_improve_loop.py`.

## Menace

Le workspace est monté en lecture-écriture dans le conteneur où tournent l'agent
(OpenHands) puis les tests du gate. Un `.git` vivant dans ce montage est donc
écrit par du code non fiable. Reproduit par l'audit : `core.fsmonitor` planté par
`implement_issue`, exécuté par le `git add -A` de la capture. Mêmes effets pour :

| Vecteur | Exécuté / falsifié par |
| --- | --- |
| `core.fsmonitor` (relatif ou absolu) | `git add`, `git status`, `git diff` |
| hooks (`post-index-change`, `pre-commit`, `reference-transaction`, `post-checkout`…) | `git add`, `commit`, `reset`, `checkout`, `revert` |
| `core.hooksPath` | idem |
| `include.path` / `includeIf` | toute lecture de config |
| filtres `clean`/`smudge` + `.gitattributes` | `git add`, `reset --hard`, `checkout` |
| `diff.external`, `diff.<x>.textconv` | `git diff` |
| `.git` remplacé par un gitfile / un symlink / `core.worktree` | redirige tout git hôte vers un dépôt ou un arbre choisi par l'attaquant |
| commit / `update-index --assume-unchanged` / `skip-worktree` / `HEAD` réécrit / objets supprimés dans le `.git` du workspace | masque ou fausse le diff (la base « HEAD » est celle de l'attaquant) |
| dépôt git imbriqué (`vendor/x/.git`) | gitlink livré ou interrogé |
| symlinks sortant du workspace | lecture de contenu hôte poussé en PR |

## Méthode : le workspace n'est jamais une source fiable

1. **Contrôle hors montage.** `prepare_workspace` clone `repo_source` sans
   checkout (gabarit vide : aucun hook), **déplace** ce `.git` en
   `<workspace>.control` (frère du workspace, mode 0700, marqueur
   `.collegue-git-control` apparié au chemin réel du workspace) et matérialise le
   workspace sur `collegue/issue-N` depuis ce contrôle. Le contrôle contient la
   config, les refs, l'index et le `HEAD` = **base de livraison**. Il n'est jamais
   monté : `DockerSandbox` refuse tout bind mount qui l'expose, à toute profondeur
   (workspace, cache pip, creds d'abonnement — voir « Garantie des montages »).
2. **Une seule porte.** Toute opération git hôte sur un workspace passe par
   `TrustedGit` : `GIT_DIR` = contrôle, `GIT_WORK_TREE` = workspace, environnement
   **reconstruit de zéro** (HOME vide, ni config système/globale, aucun `GIT_*`
   hérité — `GIT_CONFIG_COUNT`, `GIT_EXTERNAL_DIFF`, `GIT_DIR`… sont ignorés), et
   neutralisation en `-c` des mécanismes exécutants (`HARDENING_CONFIG` :
   `core.fsmonitor=`, `core.hooksPath=/dev/null`, `core.attributesFile`,
   `core.pager`, `core.editor`, `protocol.ext.allow=never`, `gc.auto=0`…), posés
   **après** les `-c` de l'appelant pour qu'un appelant ne puisse pas les lever.
3. **Le `.git` de l'agent est une copie jetable.** `<workspace>/.git` est une
   copie réelle du contrôle (aucun hardlink : contrôlé par test), confort de
   l'agent et compatibilité des sondes/tests. L'hôte ne le lit jamais. Il est
   régénéré après un seed/un commit de base.
4. **Capture** (`capture_diff`) : `git add -A` dans l'index privé du contrôle
   (persistant : un recapture borné `paths=("requirements.txt",)` après les tests
   conserve l'état stagé initial), puis `git diff --cached … HEAD` **de contrôle**
   avec `--binary --full-index --no-renames --no-ext-diff --no-textconv`. La liste
   des fichiers vient de `diff --raw -z` (noms non échappés). Fail-closed :
   gitlink/dépôt imbriqué (mode 160000), nom non UTF-8, diff tronqué (> 32 MiB),
   sortie git en erreur. Les symlinks sont enregistrés comme liens, jamais suivis.
5. **Seed/retry** (`apply_seed_diff`) : `git apply -3` avec le `GIT_DIR` de
   contrôle, patch sur stdin (`git apply` refuse déjà `.git/` et `..`). Échec →
   `reset --hard` + `clean -fdq` de contrôle, `False` (comportement #436 inchangé).
6. **Compounding** (`improve/loop.py::_seed_promoted_diffs`) : seed puis
   `advance_base` = commit **dans le contrôle**. La base fiable courante est
   `trusted_base(workspace)` ; `Workspace.base_commit` reste le SHA au clone.
7. **Revert** : `revert_commit`/`prepare_revert` utilisent par défaut
   `HardenedGitRunner` (même env/neutralisation). Sur un workspace géré il bascule
   sur le contrôle ; sur un clone plat créé par l'hôte (jamais monté) il exige un
   `.git` réel (pas de gitfile/symlink/`commondir`/`config.worktree`) et une config
   dans une liste blanche minimale (`remote.*.url|fetch`, `branch.*`, `user.*`,
   `core` structurel, jamais `include`, transport `ext::`, filtres, drivers…).
   `guard.py` et `remote_revert.py` n'instancient plus de `LocalCommandRunner`
   implicite pour ces clones.
8. **Snapshots** (`capture_delivery_snapshot`/`verify_delivery_snapshot`) ne
   lisent que des fichiers (jamais git) et ne suivent pas les symlinks ; ils sont
   désormais adossés à une capture fiable, et un recapture après tests reste dans
   la frontière.

## Interfaces

Nouvelles : `collegue.executor.git_boundary` (`TrustedGit`, `HardenedGitRunner`,
`default_git_runner`, `hardened_env`, `HARDENING_CONFIG`, `plain_git_dir_problem`,
`create_managed_workspace`, `locate_control`, `control_dir_for`), et dans
`workspace.py` : `managed_repo`, `trusted_base`, `advance_base`. `WorkspaceError`
est défini dans `git_boundary` et ré-exporté par `workspace` (mêmes imports).
`collegue.sandbox.executor.GIT_CONTROL_MARKER`.

Contrats préservés : `Workspace(path, branch, base_commit)` inchangé ;
`prepare_workspace`, `apply_seed_diff`, `run_issue`, `capture_diff`,
`execute_issue`, `revert_commit`, `prepare_revert` gardent leur signature ;
`CodeAgent`/`AgentResult` inchangés ; le protocole `CommandRunner` est inchangé.

Changement de comportement volontaire :

- `runner=None` (production) → frontière. Sur un workspace **non géré** (dossier
  quelconque, `Workspace` construit à la main) `run_issue`/`capture_diff`/
  `apply_seed_diff` **échouent** (`WorkspaceError`) au lieu de retomber sur le
  `LocalCommandRunner` : plus de repli silencieux.
- Un `runner` **injecté** reste admis pour une fixture de confiance non gérée
  (tests) ; sur un workspace **géré** il est refusé (il contournerait la
  frontière), avant même de lancer l'agent.
- `--no-renames` : un renommage rapporte l'ancien ET le nouveau chemin (sinon la
  PR ne supprimait jamais l'ancien fichier).
- Noms de fichiers lus en `-z` (les noms non ASCII n'étaient plus retrouvés).

## Inventaire des usages git / sous-process hôte (suite vague 1)

Classement : **F** = checkout toujours fiable (opérateur, ou clone neuf que l'hôte vient de créer et qui
n'a jamais été monté) ; **W** = workspace ayant pu être monté/exécuté (agent ou tests) → frontière.
Un test statique (`test_host_subprocess_usage_is_inventoried`) échoue si un nouvel usage de sous-process
apparaît hors de cette table.

| Chemin | Cible | Classe | Traitement |
| --- | --- | --- | --- |
| `executor/runner.py` capture, `pipeline.py` recapture, `workspace.apply_seed_diff`, `improve/loop._seed_promoted_diffs` | workspace de tâche | W | `TrustedGit` (contrôle hors montage) |
| `executor/revert.py` (`revert_commit`, `prepare_revert`) | clone de revert créé par l'hôte / workspace géré | F (clone) / W (géré) | `HardenedGitRunner` (bascule sur le contrôle si géré) ; source géré refusée |
| `pilot/guard.py::check_main_health` | clone de santé (créé par l'hôte, puis MONTÉ en RW par `sandbox.run_tests`) | F jusqu'au montage | `HardenedGitRunner` ; clone supprimé en `finally`, jamais réutilisé par git après le montage ; source géré refusée |
| `pilot/guard.py::guard_post_merge` (`rev-parse HEAD`) | `repo_source` | F | `LocalCommandRunner` (checkout opérateur) ; `prepare_revert` reçoit `runner` brut → runner durci |
| `pilot/remote_revert.py::prove_local_revert` | clone de revert (jamais monté) | F | `HardenedGitRunner` par défaut |
| `pilot/remote_revert.py::_verify_synced_repository` | `repo_source` resynchronisé | F | `LocalCommandRunner` : lecture de `HEAD`/tree du checkout opérateur |
| `executor/workspace.resync_repository_base`, `pilot/runtime.py::_resync_repo_source`, `driver.py`, `automerge.py`, `phase5_resume.py` | `repo_source` | F | inchangé (config opérateur : credentials/LFS) + **garde** `require_trusted_checkout` : un workspace géré ou un contrôle renvoie `False` |
| `pilot/runtime.py` | n'exécute aucun git lui-même (délègue au resync ci-dessus) | F | inchangé |
| `pilot/nightly_e2e.py::_clone_base` (`git clone/remote get-url/rev-parse`) | clone public NEUF d'une fixture, dans `mkdtemp`, sans checkout intermédiaire par un agent | F | inchangé : le seul consommateur ensuite est `collegue.pilot --repo-source`, qui re-clone via `prepare_workspace` (donc frontière). Aucune écriture par l'agent/les tests n'a lieu dans ce clone ; son `.git` vient d'un `git clone` (aucun hook copié) |
| `autonomous/proactive_monitor.py::ChangeDetector` | `repo_path` | F | inchangé : `set_repo_path`/`MonitorConfig.repo_path` ne sont appelés par aucun chemin du moteur (seul le tableau de bord lit `get_stats()`) ; l'opérateur désigne son propre dépôt. Ne reçoit jamais un `Workspace` |
| `improve/metrics.py` ruff | fichiers du workspace | W | chemins confinés (`sandbox/paths.workspace_file`) : ni `..`, ni absolu hors workspace, ni lien symbolique |
| `improve/metrics.py` audit de dépendances | `requirements.txt` du workspace | W | **sandbox** de `measure` seulement, jamais l'hôte (voir ci-dessous) |
| `executor/quality_gate.py` (lectures `package.json`/`requirements.txt`/`main.py`, écriture de remédiation) | fichiers du workspace | W | confinés ; l'écriture refuse tout lien symbolique |
| `executor/command.py::LocalCommandRunner` | tout `cwd` | — | refuse (126) un workspace géré et un répertoire de contrôle |

### Boucle d'amélioration : noms de fichiers de l'agent
`autofix_lint` n'accepte plus que des fichiers réguliers **dans** le workspace, sans lien symbolique : traversée
(`../`), chemin absolu extérieur, NUL et liens (externes ou internes) sont ignorés. `_default_doc_coverage` ne lit
plus à travers un lien.

### Audit de dépendances (`dep_vulns_enabled`)
`_default_dep_audit` ne lance plus rien sur l'hôte : `pip-audit -r requirements.txt --no-deps --disable-pip`
est exécuté par le `sandbox` passé à `measure`. Une exigence VCS/URL/locale/non épinglée échoue dans le conteneur
sans être résolue ni construite. **Fail-closed** : sandbox absent, outil absent (127), code ≠ 0/1, timeout, JSON
invalide/tronqué/incomplet ou dépendance ignorée (`skip_reason`) ⇒ `dep_audit_measured=False`, `dep_vulns=-1`,
`composite=-inf` (le gate rejette avant comme après) — jamais « 0 vulnérabilité ». `dep_vulns_fn` injecté reste
prioritaire (une valeur négative ou une exception ⇒ même refus). **Prérequis opérationnel** : l'image sandbox doit
embarquer `pip-audit` ; sinon l'audit activé refuse la mesure (voir limites).

## Garantie des montages (suivi W1)

`git_control_exposure(path)` (`collegue/sandbox/executor.py`) est appliqué à **toutes** les sources de `-v` :
1. le workspace (`DockerSandbox._validate_workspace`) ;
2. `pip_cache_dir` et 3. `subscription_auth_dir` (`DockerSandbox._build_run_argv`) — réglages d'opérateur, donc
   chemins d'entrée ;
4. le montage d'auth RW **et** le script sampler RO de `LocalSamplingContext._sample_subscription`
   (`collegue/core/llm/sampling_ctx.py`), qui construit son propre `docker run` hors `DockerSandbox`.

Le refus est levé avant toute construction d'argv / avant tout appel du runner : aucune commande Docker n'est émise
(`ValueError` côté sandbox, `RuntimeError` côté sampler).

Un chemin est accepté seulement si, après `realpath` (alias, symlinks, `..`, chemin relatif), aucun répertoire
portant le marqueur `.collegue-git-control` n'est :

1. le chemin lui-même, ou **l'un de ses ancêtres** (monter l'intérieur d'un contrôle en exposerait une partie) ;
2. **contenu dans le chemin, à toute profondeur** ≤ 64 (ancêtre lointain du contrôle : `deep/project/workspace.control`).

Propriétés :

- **Aucune dispense, y compris pour un workspace géré.** Être apparié à SON contrôle frère ne prouve pas l'absence
  d'AUTRES contrôles dans l'arbre : `prepare_workspace(dest_root=<ws>/nested)` place `inner.control` sous le montage
  de `outer`. Tout arbre monté est donc parcouru. Conséquence assumée : un arbre piégé par l'agent (marqueur forgé,
  sous-répertoire illisible, énorme) fait REFUSER son propre montage — jamais l'autoriser.
- **Parcours borné et sans lien** : itératif, `scandir(follow_symlinks=False)` — ni boucle, ni lien vers l'hôte
  suivi. Trois bornes (constantes de module) : répertoires parcourus **et file en attente** ≤ 250 000
  (`GIT_CONTROL_SCAN_MAX_DIRS`), entrées itérées — fichiers compris, comptées à mesure — ≤ 1 000 000
  (`GIT_CONTROL_SCAN_MAX_ENTRIES`), profondeur ≤ 64 (`GIT_CONTROL_SCAN_MAX_DEPTH`). Un répertoire géant est donc
  coupé pendant son itération, pas après l'avoir entièrement empilé.
- **Fail-closed** : erreur de lecture (permission, disparition), dépassement de borne, `OSError` ⇒ refus
  (« vérification impossible »). Un arbre énorme est refusé plutôt que parcouru sans limite : l'opérateur doit
  alors monter un répertoire plus petit.
- **Sans état en mémoire** : la décision ne dépend que du marqueur sur disque ; elle vaut donc après reprise du
  processus, pour un contrôle restauré, et pour chaque appel.
- **Compatibilité** : un chemin absent (workspace ou cache à créer) est accepté — seuls ses ancêtres existants
  sont examinés. Les répertoires ordinaires (frères d'un contrôle compris) sont acceptés. Coût mesuré : ≈ 0,06 s
  pour 30 000 répertoires.
- **Sampler d'abonnement** : en plus, le script monté `:ro` doit être un fichier régulier (un répertoire exposerait
  son arbre) et aucun des deux chemins ne peut contenir `:` (injection d'options de `-v`) ; les chemins canoniques
  sont ceux qui sont montés. Le routage fournisseur/modèle n'est pas modifié.
- **Faux positif assumé** : un marqueur planté dans un arbre fait refuser ce montage.

Limite : le test porte sur le disque au moment de la construction de l'argv ; un contrôle créé *après* par un autre
processus entre la vérification et `docker run` n'est pas couvert (course locale, hors modèle de menace : seul l'hôte
écrit des contrôles).

## Limites explicites (non couvert par ce lot)

- `repo_source` (checkout utilisateur : `resync_repository_base`,
  `git rev-parse HEAD` de la garde) reste traité comme dépôt de confiance avec le
  runner local : sa config est celle de l'utilisateur (credentials, LFS…) et ne
  peut pas être passée en liste blanche.
- Git LFS / filtres utilisateur : les workspaces gérés ne chargent aucune config
  globale ; les pointeurs LFS restent des pointeurs.
- Les workspaces créés par une version antérieure (sans `.control`) sont refusés
  (fail-closed) ; ils ne survivent pas à un redémarrage de run (un workspace est
  recréé à chaque tentative).
- Le sandbox reste Docker `--user hôte` avec workspace RW : cette vague ferme les
  effets HÔTE via Git, pas les effets du code non fiable à l'intérieur du
  conteneur ni le cache pip partagé.
- `pip-audit` n'est pas installé dans `docker/sandbox/Dockerfile` (et le sandbox de mesure a le réseau coupé par
  défaut) : tant que l'image ne l'embarque pas et qu'un réseau n'est pas fourni, activer `dep_vulns_enabled` REFUSE
  la mesure (fail-closed) au lieu de passer en silence. Aucun câblage produit n'active ce flag aujourd'hui.
