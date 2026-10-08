# Vague 3 — preuve commune BUILD/IMPROVE, contrats conservés (lot A)

Ce document décrit ce que le lot A garantit après la vague 3, comment le vérifier et ce qu'il ne garantit pas. Les
modules : `collegue/executor/{delivery_proof,oracle,contracts,pr,pipeline,quality_gate}.py`,
`collegue/improve/{gate,metrics,promotion,loop}.py`, `collegue/planner/acceptance_tests.py`.

## 1. Principe

Avant la vague 3, une livraison était un `passed=True` en mémoire et un hash de diff dans le corps de la PR ; IMPROVE
contournait les oracles et le veto de revue, et le score composite pouvait racheter une baisse de couverture. Désormais :

1. **Le contenu testé est figé** (arbre Git complet) avant le premier contrôle, et c'est cet arbre — et rien d'autre —
   qui est publié ; l'arbre distant est relu et comparé.
2. **Les contraintes bloquantes sont communes** à BUILD et IMPROVE et ne se compensent pas : un score élevé ne rachète
   ni des tests rouges, ni un finding bloquant, ni une baisse de couverture, ni une mesure indispensable absente, ni un
   contrat livré cassé.
3. **Le résultat est une preuve immuable**, dérivée (jamais déclarée), liée à la tête distante vérifiée, persistée dans
   l'état durable et relisible depuis une nouvelle instance du manager (`load_delivery_proof`).

## 2. Contenu testé = contenu publié

`seal_tested_content(workspace)` travaille dans l'index PRIVÉ de contrôle (frontière Git de la vague 1, jamais le `.git`
du workspace) :

| Étape | Effet |
|---|---|
| `git add -A` | l'index de contrôle représente exactement ce que Git publierait (`.gitignore` respecté) |
| `git clean -fdx` | **purge** de tout ce qui n'est pas dans cet arbre : fichiers ignorés ou résiduels. Un module « ignoré mais nécessaire » fait échouer les tests au lieu de les faire réussir. Les sorties régénérables (caches, `node_modules`) sont reconstruites par le gate lui-même |
| `write-tree` | `tree_sha` : arbre COMPLET (modes, suppressions, fichiers de base inclus) ; `content_sha256` = empreinte du manifeste `ls-tree` |

Après les contrôles, `verify_tested_content` relit l'index (`update-index --really-refresh` puis `diff-files`) : tout
fichier SUIVI (base comprise, pas seulement le diff) dont le contenu, le mode ou le type a changé pendant le gate invalide la
preuve (`DeliveryDriftError`). Les nouvelles sorties du gate (non suivies) sont sans effet. La remédiation déterministe de
`requirements.txt` est la seule mutation autorisée : le contenu est re-scellé **sur ce seul chemin**
(`seal_tested_content(only_paths=…)`), jamais avec un `add -A` qui embarquerait les artefacts du gate.

**Formats non représentables** (refus explicite AVANT toute écriture distante, `DeliveryRefusedError`) : binaire non UTF-8,
lien symbolique, fichier exécutable/lien/sous-module NEUF ou dont le mode change. La Contents API écrit un fichier neuf en
`100644` et conserve le mode d'un fichier existant : omettre un tel chemin ferait annoncer une livraison complète qui n'en
est pas une. Le refus s'applique aussi au `dry_run` (l'aperçu reflète l'issue réelle).

## 3. Vérification de la publication distante (`open_pr`)

Mode réel : **aucune dérogation** (il n'existe pas de drapeau de contournement). Exigé : un `ProofDraft` qui PASSE, un
`manager` et un `project_id`.

1. `verify_remote_base` — le tip de la branche de base a l'arbre de la base testée (`base_tree_sha`). Base déplacée ⇒ refus sans écriture.
2. PR existante — sa tête (`find_pr_by_head` puis `get_pr`) doit avoir l'arbre testé et descendre de la base ; sinon refus.
   Une PR de même nom de branche mais d'une autre révision n'est jamais présentée comme la livraison. Une reprise
   identique réutilise la preuve déjà enregistrée (`persist_or_reuse_delivery_proof`) ; un contenu différent pour la même
   tête est refusé (jamais d'écrasement).
3. Publication (`ensure_branch`, `update_file`, `delete_file`), puis `get_branch_sha` + `verify_remote_head` : l'arbre de
   l'objet commit publié est **exactement** l'arbre testé et la chaîne de commits est linéaire jusqu'à la base vérifiée.
4. `create_pr` puis `get_pr` : la tête et la base observées par la PR sont celles qui ont été vérifiées (tête/base qui
   bougent PENDANT la publication ⇒ refus).
5. `seal_proof` puis `persist_or_reuse_delivery_proof` (journal de décisions) ; le corps de la PR ne porte que des marqueurs de
   traçabilité humaine (`collegue-tree-sha`), jamais l'autorité.

Limite : une PR créée avant la détection d'une dérive (étape 4) n'est pas refermée par le moteur ; aucune preuve n'est
persistée, B la refuse (`no_proof`).

## 4. La preuve

`DeliveryProof` (dataclass figée) : `proof_id` (SHA-256 du contenu canonique), `owner`, `repo`, `project_id`, `pr_number`,
`head_sha`, `base_sha`, `base_tree_sha`, `tree_sha`, `phase` (`build`/`improve`), `passed`, `verdicts`, `oracles`,
`contracts_required`, `content_sha256`, `delivered_paths`, `ignored_inputs_removed`, `created_at`.

`passed` est **dérivé** (`derive_passed`) : tous les verdicts requis passent ET toutes les obligations de phase sont
présentes (BUILD : `content_integrity`, `tests`, `review` ; IMPROVE : + `coverage`, `secret_scan`) ET, si des contrats sont
exigés, `contracts` est présent avec des oracles tous verts. `load_delivery_proof(manager, project_id, *, owner, repo,
pr_number, head_sha)` relit, recalcule `proof_id`, vérifie `passed` contre les verdicts et refuse tout écart
(`DeliveryProofError`) : absence, identités différentes, schéma inconnu, champ manquant/typé autrement, conflit de deux preuves
distinctes pour la même tête, enregistrement altéré.

## 5. Oracles : verdict = rapport complet, jamais « exit 0 »

Le lanceur conserve ses protections (`python -I`, `pytest` importé avant l'ajout des chemins du projet, fichier d'oracle
aléatoire créé dans le tmpfs, `--noconftest`, `-c /dev/null`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, env neutralisé) et ajoute un
plugin qui émet un rapport nonce-lié. L'hôte (`oracle.judge_oracle_run`) juge par oracle :

| Observé | Statut |
|---|---|
| ≥ 1 test exécuté en phase `call`, tous passés, rien d'ignoré | `green` |
| échecs, **tous** des `AssertionError` levées en phase `call` d'un test exécuté | `red-assertion` |
| collecte/import/setup/teardown en erreur, zéro test, skip, xfail/xpass, exception autre qu'une assertion, session interrompue, délai, sortie du process sans rapport complet, événements incomplets, rapports multiples, code de sortie contradictoire | `invalid` (motif précis) |

Contrôles de la sortie avant tout jugement (`collect_oracle_report`) : un seul rapport par nonce (plusieurs ⇒ sortie non
fiable, jamais « le dernier gagne »), le code de sortie RÉEL du process doit être celui que le rapport déclare, une sortie
tronquée par le sandbox (la sortie est bornée à 10 Mio, tête conservée ; `--show-capture=no` borne le bruit) ou un délai ne
prouvent rien, et chaque test collecté doit avoir un cycle de vie complet (`setup`/`call`/`teardown`) — un nombre de tests
collectés différent du nombre de tests avec événements (session interrompue, rapport borné) est invalide.

**Preuve négative** (contrat COURANT uniquement) : le MÊME oracle (même SHA-256) exécuté sur la préimage (clone neuf à la
base testée, jamais le workspace de l'agent) doit être `red-assertion`, puis `green` sur le candidat. Les contrats déjà
livrés ne sont pas exigés rouges : seulement verts sur le candidat. Les sources viennent de l'ÉTAT durable après
`require_approved` et vérification de la provenance (SHA-256 de la source, empreintes SPEC/critères/contrat/prompt) ; les
tests écrits dans le workspace ne remplacent jamais un contrat.

**Prompt QA v2** : pour qu'un oracle puisse échouer par assertion avant l'implémentation, le prompt système exige de
localiser le code à produire dans le test (`importlib.util.find_spec`, `Path.cwd()`) et d'affirmer sa présence par un `assert`
à message, jamais d'importer en tête de module. Les oracles scellés avec le prompt v1 restent vérifiables (registre
`LEGACY_SYSTEM_PROMPTS`) ; s'ils ne discriminent pas la préimage, la preuve négative les refuse avec un motif explicite et
le plan doit être régénéré.

**Portée** : le nonce associe un rapport à un run, ce n'est pas une signature. Du code malveillant exécuté DANS le même
interpréteur que pytest peut forger le statut de sortie ET le rapport (il peut lire `/proc/self/cmdline`). L'isolation d'un
oracle contre le code qu'il teste n'est pas fournie par cette architecture et n'est pas annoncée.

## 6. Contrats : l'état prévaut

* BUILD : contrat courant (preuve négative) + tous les contrats livrés (`done`/`merged`).
* IMPROVE : tous les contrats livrés. Un seul contrat cassé, absent ou invérifiable refuse la promotion.
* **Exigence** : `Project.acceptance_tests_required` dans l'état, ou une tâche livrée qui porte un oracle, rend les contrats
  obligatoires pour la preuve **quel que soit** le booléen du rapport du gate ou la présence d'un checker. Un défaut de
  câblage (checker absent, `include_delivered=False`, `GATE_ACCEPTANCE_TESTS=false` face à un projet qui les exige) est
  refusé : `contracts` est requis, avec les identifiants de tâches attendus d'après l'état.
* Un projet sans exigence et sans oracle livré n'a rien à rejouer (le BUILD historique « oracles désactivés » n'est pas en
  soi un défaut).

## 7. IMPROVE : contraintes communes, sans dérogation

`run_improvement` n'expose aucun paramètre de dérogation. Pour chaque round, avant PR :

| Verdict | Condition |
|---|---|
| `content_integrity` | binaire/lien/mode refusés, arbre scellé, aucune dérive pendant la mesure ni le rejeu des contrats |
| `tests` | la commande de test du projet est verte sur le candidat |
| `review` | un reviewer a rendu un verdict sur le diff ET il n'est pas bloquant ; une panne ou l'absence de reviewer n'est pas une revue propre |
| `coverage` | mesurée avant ET après, jamais en baisse (aucune tolérance) |
| `secret_scan` | **scan statique de secrets par expressions régulières**, hors tests/fixtures/lockfiles : ne s'aggrave pas. Ce n'est PAS un audit de sécurité |
| `contracts` | tous les contrats livrés restent verts (cf. §6) |
| `gate` | gain réel ≥ `min_gain`, lint/complexité/vulns sans régression (tolérances existantes) |

Le score composite reste l'objectif du gain, pas une monnaie d'échange : 20 violations de lint corrigées (+0,4) ne rachètent
pas 10 points de couverture. La publication suit le même chemin que le BUILD (§3) avec une preuve `phase="improve"` ; en
mode empilé, la base de la PR N+1 est la tête vérifiée de la PR N.

## 8. Sonde de smoke

La sonde (`serve.py`) démarre le serveur du projet dans **son propre groupe de processus** (`start_new_session`) et, dans
tous les cas (succès, échec, délai, SIGTERM), tue ce groupe et lui seul (`killpg`). Elle ne laisse plus d'enfant après
`proc.terminate()`, y compris quand le shell sort alors que ses enfants vivent.

## 9. Limites assumées

* Contents API : texte UTF-8 uniquement ; les modes `100755`/`120000`/`160000` neufs sont refusés, pas contournés.
* Le journal de décisions n'est pas signé (décision manager) : les invariants internes (identités, `proof_id`, `passed`
  dérivé, idempotence) sont vérifiés à la relecture, pas l'intégrité cryptographique de la base.
* Les artefacts écrits dans le workspace par la mesure de baseline d'IMPROVE (avant l'agent) restent dans le diff capturé ;
  ils sont publiés et testés ensemble (pas de divergence), mais polluent la PR si le projet ne les ignore pas.
* Les doubles de transport historiques incomplets (sans `get_branch_sha`, `get_git_commit`, `get_pr`) ne peuvent plus
  produire de livraison réelle : `tests/github_fakes.py` fournit un distant fidèle adossé à un vrai dépôt Git.

## 10. Vérifier

```
python -m pytest -p no:cacheprovider tests/test_delivery_proof.py tests/test_oracle_judge.py tests/test_contracts.py \
  tests/test_executor_delivery.py tests/test_improve_promotion.py tests/test_executor_pr.py tests/test_executor_pipeline.py \
  tests/test_executor_quality_gate.py tests/test_improve_gate.py tests/test_improve_loop.py tests/test_improve_metrics.py
python -m ruff check --no-cache collegue tests && python -m ruff format --no-cache --check collegue tests
```
