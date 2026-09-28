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
   monté : `DockerSandbox._validate_workspace` refuse tout montage qui contient le
   marqueur (montage du parent `collegue-exec-*` ou du contrôle lui-même).
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

## Limites explicites (non couvert par ce lot)

- `improve/metrics.py` (hors périmètre A, NON corrigé) : **vérifié** —
  `autofix_lint` passe des chemins issus de `files_changed` à `ruff --fix` après un
  simple `os.path.isfile` : un symlink `x.py` → fichier hôte est suivi et le fichier
  HORS workspace est réécrit (reproduction :
  `evidence/w1-a-finding-autofix-symlink.txt`). **À vérifier** (non exécuté ici, il
  faudrait du réseau) : `_default_dep_audit` lance `pip-audit -r requirements.txt` sur
  l'hôte, dont la résolution pip peut construire des sdists ou suivre des URL VCS
  choisies par l'agent. Correctif proposé dans le rapport A.
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
