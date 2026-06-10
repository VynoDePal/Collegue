# Moteur de développement autonome

Au-delà du collectif d'experts **réactifs** (appelés depuis un IDE), Collègue
embarque un **moteur de développement autonome** : à partir d'une problématique et
d'un budget-temps, il planifie, code, teste et ouvre des Pull Requests, en
utilisant **GitHub comme substrat de pilotage**. Le développeur garde la main :
**aucun merge dans `main` sans approbation humaine** (sauf opt-in explicite, voir
[Garde-fous de sûreté](#garde-fous-de-sûreté)).

Ce guide décrit l'architecture, les garde-fous, l'observabilité, la reprise après
crash, et comment lancer un run.

> **Posture par défaut : sûr.** Tout est en `dry_run` (aperçu, aucune écriture) tant
> qu'on ne passe pas `--execute`. Les capacités dangereuses (auto-merge, auto-revert,
> outil MCP du pilote) sont **désactivées par défaut** et **fail-closed**.

---

## Vue d'ensemble

Le moteur enchaîne quatre étages, posés sur un socle d'état durable et d'exécution
isolée :

```
problématique + budget
        │
        ▼
┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  planner     │──▶│  pilote      │──▶│  executor    │──▶│  improve     │
│ SPEC → tâches│   │ ordonnance + │   │ 1 issue →    │   │ cycle qualité│
│ (DAG GitHub) │   │ budget-temps │   │ diff → PR    │   │ gaté/rollback│
└──────────────┘   └──────┬───────┘   └──────┬───────┘   └──────────────┘
                          │                  │
                   ┌──────▼──────────────────▼──────────────────┐
                   │ state (Postgres/SQLite) · sandbox Docker   │
                   │ budget dur · checkpoints · journal d'audit │
                   └────────────────────────────────────────────┘
```

| Étage | Module | Rôle |
|-------|--------|------|
| **Planner** | `collegue/planner/` | Problématique → `SPEC.md` → graphe de tâches (DAG) → labels/milestones/board GitHub. Gate de **validation humaine** du plan (anti-TOCTOU par empreinte SHA-256). |
| **Pilote** | `collegue/pilot/` | Ordonnance les tâches prêtes (DFS anti-cycle) sous un **contrôleur budget-temps**, chaîne l'exécuteur, checkpoint, et bascule en mode amélioration quand le MVP est construit. |
| **Executor** | `collegue/executor/` | Exécute **une** issue de bout en bout : workspace git → agent codeur (OpenHands, en sandbox) → tests + revue experte (gate fail-closed) → ouverture de PR. |
| **Improve** | `collegue/improve/` | Après le MVP, fait cycler les experts pour élever un **score de qualité** ; ne promeut un changement que s'il progresse **sans régression** (gating par métrique), s'arrête sur rendements décroissants. |

Socle commun :

| Brique | Module | Rôle |
|--------|--------|------|
| **État durable** | `collegue/state/` | Projets, tâches, décisions, métriques, **checkpoints** (SQLAlchemy + Alembic ; Postgres ou SQLite). |
| **Sandbox** | `collegue/sandbox/` | Exécution **isolée** (Docker) du code non fiable (agent, tests). |
| **Budget-temps** | `collegue/pilot/budget.py` | Plafond `$`/tokens (dur, auto-pause) + deadline mur ; arrête proactivement la boucle. |
| **Audit du run** | `collegue/pilot/audit.py` | Journal append-only des actions + **ledger de coût par run** + export auditable. |

---

## Garde-fous de sûreté

Le risque n°1 d'un moteur autonome est de **dégrader le projet** ou de **brûler le
budget**. Les garde-fous sont conçus **fail-closed** (au moindre doute, on s'arrête
ou on refuse) :

| Garde-fou | Comportement |
|-----------|--------------|
| **Merge humain (§6)** | C'est le **défaut**. Une tâche réussie passe `in_review` (PR ouverte), **jamais** `done`/`merged` automatiquement. Un humain merge. |
| **`dry_run` par défaut** | Sans `--execute`, le pipeline va jusqu'aux **aperçus** de PR sans **aucune** écriture (ni GitHub ni état). |
| **Budget dur** | `MAX_COST_USD` / `MAX_TOKENS_BUDGET` atteints → **auto-pause** (les appels LLM sont stoppés). `COLLEGUE_RUN_DEADLINE_SECONDS` borne la durée mur. |
| **Gate qualité** | Un diff dont les tests sont rouges, ou qui n'améliore pas le score, **n'ouvre pas de PR** (il est jeté). |
| **Auto-merge opt-in** | `AUTO_MERGE_ENABLED=false` par défaut. Activé, il ne fusionne **que** du faible risque (allowlist de chemins **non exécutables**, plafond de LOC, **toutes** les vérifs CI vertes). Tout code/exécutable/secret/CI est bloqué par une garde dure insensible à la casse. |
| **Auto-revert** | Filet de l'auto-merge : après un auto-merge, si `main` devient rouge (tests en sandbox), un revert est préparé. Fail-closed : santé non concluante = traitée comme rouge. Un revert qui échoue **escalade** vers un humain (ne passe jamais pour un succès). |
| **Outil MCP du pilote** | Exposé en MCP **uniquement** si `PILOT_TOOL_ENABLED=true` **et** `OAUTH_ENABLED=true` (sinon **refus de démarrer**). Allowlist d'appelants (sujets OAuth vérifiés, jamais un en-tête client). Jamais auto-découvert. `dry_run` par défaut. |

---

## Observabilité et audit

Le run autonome est **traçable et auditable** (distinct de l'observabilité du serveur
MCP, qui trace ses propres appels d'outils) :

- **Journal d'audit du run** (`RunAuditLog`) : chaque action clé est tracée :
  `task_started`, `gate_decision`, `pr_opened`, `automerge_decision`, `auto_revert`,
  `auto_revert_failed`, `budget_event`, `checkpoint_saved`, `run_stop`.
- **Ledger de coût par run** : coût USD + tokens agrégés par run/projet.
- **Export auditable** : `export_run_audit(...)` produit un JSON déterministe.
- **Dashboard** : l'onglet **« Run autonome »** du dashboard Streamlit (port `4125`)
  affiche, par projet, la timeline d'audit, le coût/tokens, les décisions
  auto-merge/auto-revert, le statut et l'itération du dernier checkpoint. Un échec de
  revert (`auto_revert_failed`) y est signalé comme **intervention humaine requise**.

---

## Reprise après crash

Un run de plusieurs jours peut **reprendre après un crash sans perte d'état** :

- **Checkpoints** : la progression est checkpointée par itération (table `checkpoints`).
- **Reprise des tâches** : au redémarrage, l'état des tâches en base fait foi ; une
  tâche restée `in_progress` (run interrompu) est repassée `todo` et re-tentée.
- **Deadline absolue** : le `started_at` du run est **persisté** ; à la reprise, le
  contrôleur budget-temps est reconstruit depuis ce départ d'origine, et la deadline
  mur reste **fixe** au lieu de glisser à chaque redémarrage.

> Pour que le plafond `$`/tokens survive aussi aux redémarrages, `COLLEGUE_HOME` doit
> pointer vers un chemin **absolu et stable**, identique d'un redémarrage à l'autre
> (le cumul est persisté dans `$COLLEGUE_HOME/monitoring/`). Le chemin est résolu en
> absolu dès l'import (#406) — un `chdir` du process en cours de run ne déplace donc
> plus la persistance — mais si la variable est laissée relative (défaut `.collegue`),
> deux redémarrages depuis des répertoires différents liront deux cumuls distincts :
> le pilote **avertit** au lancement quand un budget dur est configuré avec un
> `COLLEGUE_HOME` non absolu.

---

## Lancer un run

Le pilote s'invoque **explicitement** (jamais auto-démarré par le serveur MCP) via son
entrypoint. Le projet doit déjà exister dans l'état durable (planifié via le planner).

```bash
# Aperçu (dry_run) : aucune écriture GitHub ni état
python -m collegue.pilot \
  --project-id 1 --repo-source /chemin/vers/clone \
  --owner mon-org --repo mon-app

# Exécution réelle (branches/commits/PR + transitions d'état)
python -m collegue.pilot ... --execute

# Enchaîner le moteur d'amélioration (Phase 4) une fois le MVP construit
python -m collegue.pilot ... --execute --improve
```

| Flag | Effet |
|------|-------|
| `--project-id` | Id du projet (état durable). **Requis.** |
| `--repo-source` | Dépôt git source (chemin / clone). **Requis.** |
| `--owner` / `--repo` | Cible GitHub des PR. **Requis.** |
| `--base` | Branche de base des PR (défaut `main`). |
| `--execute` | Désactive le `dry_run` : écritures réelles. |
| `--max-iterations` | Garde-fou anti-boucle (optionnel). |
| `--improve` | Après le MVP, enchaîne le moteur d'amélioration sous le budget restant. |

L'exécution réelle de bout en bout (Docker + OpenHands + LLM + écritures GitHub)
nécessite `STATE_DATABASE_URL`, un `GITHUB_TOKEN`, Docker, et un provider LLM
configurés (voir [Réglages](#réglages-env)).

---

## Réglages (.env)

En plus des réglages du serveur (voir le [README](../README.md)), le moteur autonome
lit :

| Variable | Description | Défaut |
|----------|-------------|--------|
| `STATE_DATABASE_URL` | État durable (Postgres ou SQLite). Requis pour un run réel. | — |
| `LLM_MODEL_CODER` / `_QA` / `_PLANNER` / `_REVIEWER` | Modèle par **rôle** (codeur fort, QA économique, planificateur, revue). Retombe sur `LLM_MODEL` si absent. | `LLM_MODEL` |
| `MAX_COST_USD` | Plafond dur de dépense cumulée (`0` = désactivé). | `0` |
| `MAX_TOKENS_BUDGET` | Plafond dur de tokens cumulés (`0` = désactivé). | `0` |
| `BUDGET_EXHAUSTED_ACTION` | `pause` (refuse les appels LLM) ou `warn` (journalise seulement). | `pause` |
| `COLLEGUE_RUN_DEADLINE_SECONDS` | Durée mur max d'un run (`0` = pas de deadline). | `0` |
| `LLM_CALL_TIMEOUT` | Timeout par appel LLM, secondes (`0` = off). | `0` |
| `AUTO_MERGE_ENABLED` | Active l'auto-merge progressif (opt-in). | `false` |
| `AUTO_MERGE_MAX_LOC` | Plafond de lignes nettes pour l'auto-merge. | `50` |
| `AUTO_MERGE_PATH_ALLOWLIST` | Motifs de chemins **faible risque** (CSV ; `**` = sous-dossiers). | `docs/**,**/*.md,**/*.rst` |
| `AUTO_MERGE_METHOD` | Méthode de merge : `squash` / `merge` / `rebase`. | `squash` |
| `AUTO_REVERT_ENABLED` | Filet auto-revert (n'a d'effet que si l'auto-merge est actif). | `true` |
| `AUTO_REVERT_HEALTH_COMMAND` | Commande de santé exécutée en sandbox sur `main`. | `pytest -q` |
| `PILOT_TOOL_ENABLED` | Expose le pilote en outil MCP (strict : exige `OAUTH_ENABLED`). | `false` |
| `PILOT_TOOL_ALLOWED_SUBJECTS` | Allowlist de sujets OAuth autorisés (CSV ; vide = personne). | — |

---

## Tests

- La logique du moteur est **testable sans réseau / Docker / LLM** (mocks + injection
  de dépendances) et tourne en CI.
- Les chemins réels (Docker, OpenHands, API GitHub, Postgres, LLM) sont derrière le
  marqueur pytest `integration`, exclu de la CI par défaut :

```bash
python -m pytest -m "not integration" -q   # CI (par défaut)
python -m pytest -m integration -q          # chemins réels (credentials requis)
```
