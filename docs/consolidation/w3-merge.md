# Vague 3 (B) — politique de fusion commune et reprise durable

Périmètre : toutes les fusions automatiques du moteur (merge-bot BUILD, drain de fin de run, reprise, Phase 5, revert
distant). Code : `collegue/pilot/merge_policy.py` (validation, lecture seule), `collegue/pilot/merge_cycle.py`
(cycle durable), `collegue/state` (table `task_merges`, migration `0012`), clients `collegue/tools/github_commands`.

## 1. Désactivé par défaut

`BUILD_AUTO_MERGE` vaut `false` dans `Settings`, dans les replis `getattr(settings, "BUILD_AUTO_MERGE", False)` et dans
`.env.example`. Un réglage absent ou nul n'active jamais l'auto-merge. L'activation est un opt-in explicite
(`BUILD_AUTO_MERGE=true`) ; elle ne dispense d'aucune exigence ci-dessous. Phase 5 garde son propre interrupteur
(`AUTO_MERGE_ENABLED`) et ses restrictions de faible risque (taille, allowlist de chemins, statistiques de diff).

## 2. Chemin commun de validation

`merge_policy.verify_merge_candidate` est appelé par **tous** les chemins qui émettent un `PUT /pulls/N/merge` :

| Chemin | Appelant | Phase de preuve | Checks |
| --- | --- | --- | --- |
| Boucle normale + drain de fin BUILD | `merge_cycle.merge_task` | `build` | requis verts |
| Reprise BUILD | `merge_cycle.resume_cycles` (pas de nouveau PUT, voir §4) | — | — |
| Phase 5, promotion | `automerge.auto_merge_promotion` | `improve` | requis ET tous verts |
| Phase 5, reprise d'incident `merge_pending` | `phase5_resume.resume_phase5_incident` | `improve` | requis ET tous verts |
| Revert distant | `remote_revert.publish_and_merge_revert` | sans preuve (revert mécanique) | requis verts + précondition serveur ; contenu prouvé par le tree restauré |

Ordre des contrôles (tout échec, toute lecture impossible ⇒ **pas de fusion**) :

1. PR ouverte, non brouillon, bonne branche de base, bonne branche de tête (une PR préexistante différente est refusée).
2. Preuve de livraison durable du lot A (`load_delivery_proof`), chargée pour la **tête exactement observée**,
   `passed is True`, phase attendue, base et tree complets. Module absent ⇒ refus.
3. La tête distante descend de la base prouvée (`compare`, `behind_by == 0`) et son tree est celui de la preuve.
4. Le sommet distant de la base est la base de la preuve.
5. Checks requis (protections classiques **et** rulesets, `app_id` / `integration_id` respectés) présents et `success`
   sur la tête ; absent / pending / failed / cancelled / skipped / neutral / liste incomplète ⇒ refus. Un check
   non requis en échec terminal bloque aussi.
6. Précondition serveur contre la course sur la base (§3).
7. Relecture finale : la PR et le sommet de base n'ont pas bougé pendant la validation.

Le `PUT` transmet `sha` = tête validée. Après la réponse, `verify_merge_result` relit le commit de fusion : parents
`[base prouvée]` (squash) ou `[base prouvée, tête]` (merge) et tree = tree de la preuve. Un écart ⇒ état
`attention` (Phase 5 : `post_merge_guard_failed`), aucune resynchronisation d'un contenu non prouvé.

## 3. Précondition serveur et garantie de concurrence

L'API de fusion ne prend **pas** de SHA de base : entre notre dernière lecture et l'évaluation serveur, la base peut
avancer. Deux lectures successives ne le démontrent pas. La seule garantie côté serveur est une règle « branche à jour
avant fusion » (`strict`) qui s'applique **à l'acteur du jeton** : GitHub répond alors `405 Head branch is not up to
date with the base branch`. Elle est exigée, sinon `CODE_POLICY` ⇒ refus. Sont acceptées :

- un ruleset **actif** (`enforcement == "active"`) avec `strict_required_status_checks_policy` dont
  `current_user_can_bypass == "never"` ;
- une protection classique `required_status_checks.strict` appliquée à l'acteur : `enforce_admins` activé, ou rôle de
  base sans droit de contournement (`read`, `triage`, `write`, `maintain`).

Refusés : ruleset désactivé ou en `evaluate`, contournement possible ou indéterminé (champ absent), protection classique
non appliquée aux administrateurs avec un acteur `admin`, **rôle personnalisé ou inconnu** (la doc GitHub : les
restrictions ne s'appliquent pas aux rôles personnalisés avec « bypass branch protections » sans « Do not allow
bypassing »), merge queue (l'API de fusion directe est inadaptée), aucun check requis, aucune protection stricte.
Le moteur **ne modifie jamais** les protections.

Réponses GitHub nécessaires (toutes en lecture) : `GET /user` (un jeton d'application répond 403 ⇒ acteur non établi ⇒
refus), `GET /repos/{o}/{r}/collaborators/{login}/permission`, `GET .../branches/{b}/protection` (404 = absence ou
invisibilité, jamais une preuve), `GET .../rules/branches/{b}` (paginé, tronqué ⇒ erreur), `GET .../rulesets/{id}`
(`enforcement`, `current_user_can_bypass`), `GET .../compare/{base}...{head}`, `GET .../git/commits/{sha}`,
`GET .../commits/{sha}/check-runs` (avec `app.id`) et `.../statuses`.

Détection a posteriori si la précondition était trompée (serveur qui n'applique pas la règle, bypass non déclaré) :
contrôle du commit de fusion ci-dessus. Elle **détecte** un contenu non testé ; elle ne l'empêche pas (c'est le rôle de
la règle serveur).

## 4. Cycle durable (`task_merges`)

```
(rien) ─begin→ merge_pending ─fusion confirmée + commit conforme→ merged_unsynced ─resync vérifiée→ synced (+ tâche merged)
                  ├─ PR non fusionnée / tête changée → abandoned
                  └─ contenu ou base incohérents      → attention (humain, `acknowledge_task_merge`)
```

- L'intention (PR, tête, base, tree, `proof_id`, méthode) est écrite **avant** le `PUT` (write-ahead, CAS par révision).
- Un crash entre le succès distant et l'écriture locale est réconcilié en relisant GitHub (`reconcile_task_merge`) : PR
  fusionnée avec la même tête et commit conforme ⇒ `merged_unsynced` ; autre tête ou commit non conforme ⇒ `attention` ;
  PR toujours ouverte ou fermée non fusionnée ⇒ `abandoned` ; GitHub illisible ⇒ reste `merge_pending` (jamais de PUT).
  Une réponse perdue (504) est réconciliée de la même façon dans le processus.
- Resync échoué (ou non prouvé : `HEAD == merge_sha` avec le tree prouvé, ou `merge_sha` ancêtre de `HEAD`) ⇒ l'état reste
  `merged_unsynced`. **Pas de second merge**, tâche non comptée prête, tâche suivante non lancée.
- Barrière runtime : au démarrage et après chaque passe, tout cycle `merge_pending` / `merged_unsynced` / `attention`
  arrête le run (`merge_reconcile_pending`, `merge_sync_pending`, `merge_attention`) avant `run_project`, avant le drain
  final et avant le handoff Phase 4. Elle s'applique même si `BUILD_AUTO_MERGE` a été retiré depuis (on ne fait alors que
  réconcilier / resynchroniser, jamais fusionner).
- Les gardes budget / deadline de la vague 2 restent consultées à chaque tour d'attente (`continue_fn`), sans reset.

## 4 bis. Fusion survenue HORS moteur (opérateur, autre outil)

Avec `BUILD_AUTO_MERGE=false` (défaut), les PR sont fusionnées à la main : le clone opérateur (`repo_source`) est périmé et
aucun cycle du merge-bot n'existe. Le driver ne marque plus jamais une tâche `merged` sur la seule foi de GitHub, et le
blocage survit au redémarrage :

1. **Persister d'abord.** `driver.reconcile_in_review_tasks` écrit, AVANT toute tentative de synchronisation,
   `manager.begin_external_task_merge(task_id, owner, repo, base_branch, pr_number, merge_sha)` : une ligne `task_merges`
   `origin='external'`, `state='merged_unsynced'`, `merge_sha` = commit de fusion distant. Tête/base/tree/`proof_id`/méthode
   restent **NULL** (contraintes `ck_task_merges_origin_anchors`) : aucune preuve de livraison n'est inventée. Si la
   persistance échoue (ou si GitHub ne donne pas de `merge_commit_sha`), la tâche reste bloquée : aucune synchronisation n'est
   tentée sans barrière durable.
2. **Synchroniser et prouver.** Une seule resynchronisation du clone de confiance (`sync_base_fn`, défaut
   `resync_repository_base`, qui refuse un workspace géré), puis `verify_local_sync(repo, merge_sha, None)` (`HEAD == sha` ou
   ancêtre). Succès : `synced` + tâche `merged` dans UNE transaction (`transition_task_merge(..., complete_task=True)`). Échec :
   le cycle reste `merged_unsynced`, `last_error` mémorise la raison, la tâche reste `in_review`.
3. **Relire avant tout travail.** À chaque `run_project` réel, `driver.resume_external_merges` relit les cycles
   `origin='external'` `merged_unsynced` **sans GitHub** et reprend depuis le SHA enregistré (sync réelle + vérification
   réelle) AVANT toute tâche et avant la découverte des PR ; la barrière du runtime (`merge_cycle.resume_cycles`, même
   `sync_base_fn` si injectée) fait de même avant `run_project`. Découverte GitHub en panne, PR introuvable ou crash entre la
   persistance et la sync : sans effet sur la barrière. Une erreur de lecture de l'état se propage (le run échoue).
4. **Arrêt.** Tant qu'un tel cycle est inachevé : `repo_sync_failed` (driver, `pending_reviews`) ou `merge_sync_pending`
   (runtime) AVANT toute tâche, dépendante ou non ; ni drain final ni handoff Phase 4. Le moteur n'émet jamais de `PUT` sur ce
   chemin : reprise sans nouvelle fusion. Un cycle `origin='engine'` reste du ressort exclusif du merge-bot/runtime.

Les gardes budget/deadline ne sont pas touchées (la reprise ne dépense rien et précède la boucle).

Procédure opérateur : un cycle `attention` n'est levé que par `acknowledge_task_merge` après inspection ; la ligne est alors
supprimée, la tâche reste `in_review` et la passe suivante revalide la PR (preuve, base, checks) ou, si elle a été fusionnée
hors moteur, la réconcilie comme ci-dessus, **après** resynchronisation prouvée du clone.

## 5. Classification sensible

`pilot.automerge.is_sensitive` refuse, même si l'allowlist est élargie : manifestes et locks de dépendances
(`requirements*.txt`, `constraints*.txt`, `locks/**`, `pyproject.toml`, `package*.json`, `go.mod/sum`, `Gemfile*`,
`Cargo.*`, `composer.*`…), Dockerfiles et compose (`Dockerfile*`, `*.dockerfile`, `docker-compose*`, `compose.y*ml`,
`.dockerignore`) et le segment `migrations` (donc `collegue/migrations/versions/**`, `alembic.ini`). Chemins concrets
testés : les 13 chemins de `w3-manager-sensitive-paths-before.json`.

## 6. Migration `0012_task_merges`

Additive : crée `task_merges` (PK `task_id` → `tasks` cascade, `project_id`, `state`, `revision`, `origin` (`engine`|`external`),
ancres (NULL seulement pour `external`), `merge_sha`, `last_error`, contraintes CHECK) et `ix_task_merges_project_id`. Aucune table ni colonne existante modifiée ; une base
migrée jusqu'à 0011 reste compatible (table vide = aucun cycle). Downgrade : retire la seule table.
Aucun cycle n'existe pour les tâches `in_review` antérieures : leur PR est traitée comme n'importe quelle PR sans
cycle, donc **sans preuve de livraison elle est refusée** (jamais reconstruite depuis le texte de la PR) et reste pour
merge humain.

## 7. Tests

`tests/test_pilot_merge_policy.py`, `test_pilot_merge_cycle.py` (faux serveur REST `tests/github_fake_server.py` branché
sur les vrais clients ; courses simulées au niveau des appels émis), `test_task_merge_state.py` +
`test_task_merge_postgres.py` (même contrat `task_merge_contract.py` sur SQLite et PostgreSQL réel, CAS concurrent ;
**vraie migration Alembic** 0011 → 0012 → 0011 → head sur base vierge avec données préexistantes et version relue,
SQLite et PostgreSQL ; le test PostgreSQL « schéma ORM » porte explicitement sur `create_all`, pas sur la migration),
`test_github_merge_clients.py`, `test_pilot_local_sync.py` (vrai dépôt git), `test_pilot_external_merge_resume.py`
(runtime public, vrais dépôts Git, fusion hors moteur : témoins synchronisé / périmé, resync `False` / exception, fusion
absente du clone, reprise, workspace géré refusé), `test_pilot_driver.py`, `test_pilot_automerge.py`,
`test_pilot_phase5_resume.py`, `test_pilot_remote_revert.py`, `test_pilot_runtime.py`.
