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
| **Improve** | `collegue/improve/` | Après le MVP, fait cycler les experts pour élever un **objectif de qualité déterministe** (couverture − sécu − lint − complexité) ; ne promeut un diff que s'il progresse **sans régression** (gate fail-closed), s'arrête sur rendements décroissants. Détail : [Amélioration continue (Phase 4)](#amélioration-continue-phase-4). |

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
| **Merge-bot (phase BUILD)** | Pendant la **construction du MVP**, une tâche réussie est **auto-mergée** (squash) puis le clone local est resynchronisé sur `origin/<base>` avant la tâche suivante (`BUILD_AUTO_MERGE=true` par défaut). Sans lui, avec 1 PR en vol + dépendances strictes, le build se figerait `awaiting_merge` (et des bases périmées créeraient des conflits). C'est le merge humain **simulé** pendant la construction autonome. Mettre `BUILD_AUTO_MERGE=false` ramène tout au merge humain. |
| **Merge humain (phase AMÉLIORATION, §6)** | Les PR d'**amélioration** (Phase 4) restent **ouvertes** : elles ne sont **jamais** auto-mergées — relecture/merge par un **humain**. C'est le défaut sûr pour faire évoluer un produit déjà construit. |
| **`dry_run` par défaut** | Un **run** sans `--execute` va jusqu'aux aperçus de PR sans écriture GitHub/état et sans auto-merge. `plan draft` persiste volontairement son brouillon (SPEC/DAG/oracles/cible) pour permettre validation et reprise ; seul `plan sync --execute` touche GitHub. |
| **Budget dur** | `MAX_COST_USD` / `MAX_TOKENS_BUDGET` atteints → **auto-pause** (les appels LLM sont stoppés). `COLLEGUE_RUN_DEADLINE_SECONDS` borne la durée mur. |
| **Gate qualité** | Un diff dont les tests sont rouges, qui retire une exigence, ou qui porte un finding **critical/error** de la revue (sécurité réelle, défaut signalé par le reviewer LLM) **n'ouvre pas / ne merge pas** de PR. Les findings d'heuristiques crues de style/maintenabilité (complexité, duplication, nommage, exception silencieuse) sont **advisory** (informent la PR, ne bloquent pas un code aux tests verts). |
| **Sandbox du gate sans secrets** | Le coder OpenHands et le code projet testé ne partagent plus le même conteneur produit : le sandbox du gate conserve les ressources nécessaires aux installations/tests, mais ne reçoit ni clés LLM, ni variables du coder, ni credentials d'abonnement OpenHands. |
| **Snapshot immuable de livraison** | Avant d'exécuter le code projet, le moteur fige les payloads texte/suppressions et l'empreinte SHA-256 du diff. BUILD et Phase 4 refusent toute dérive pendant les tests/mesures ; `open_pr` pousse exclusivement ce snapshot, jamais le workspace vivant. Une remédiation déterministe de `requirements.txt` déclenche un nouveau snapshot et un second gate complet. |
| **Acceptation §4.7 (opt-in, OFF)** | `GATE_ACCEPTANCE_TESTS=false` par défaut. En opt-in, le rôle **QA** génère un oracle pytest par tâche **avant** l'aperçu et l'approbation. Source, SHA-256, provenance et politique d'activation sont persistés atomiquement et couverts par l'empreinte du plan : omettre ensuite le flag au run ne contourne pas le gate. Celui-ci recharge et rejoue exactement cet oracle dans son sandbox isolé, sans diff ni nouvel appel LLM ; absence, altération, aucun test collecté ou exit non nul = livraison bloquée. |
| **Validation humaine du plan** | Le produit impose trois processus séparés : `plan draft` affiche le contenu, la cible (dépôt + branche) et son SHA-256 ; `plan approve --expected-plan-hash …` scelle exactement ce snapshot sans LLM/GitHub ; `plan sync` recharge uniquement la cible persistée. SPEC, DAG, oracles, deadline et cible sont hashés. Toute mutation entre les étapes invalide le hash. |
| **Snapshot d'écriture cohérent** | En sync réelle, cible, SPEC et tâches sont copiés dans une seule transaction verrouillée. Les appels GitHub consomment exclusivement ce snapshot approuvé : une mutation/réapprobation concurrente ne peut pas envoyer le contenu d'une révision vers la cible d'une autre. Après la première issue matérialisée, une révision différente exige une réconciliation explicite des liaisons avant réapprobation. |
| **SPEC avant issues** | Sur le chemin produit `plan sync --execute`, le DAG est validé intégralement puis GitHub doit confirmer le commit de `SPEC.md` **avant** la première issue. Client absent, erreur API, réponse sans `commit.sha` ou fichier distant divergent = arrêt sans issue. Un SPEC déjà identique est confirmé sans commit vide. |
| **Auto-merge RISK-GATED (politique, opt-in, distinct)** | Politique séparée du merge-bot ci-dessus : merge **fin** par risque (`AUTO_MERGE_ENABLED=false` par défaut ; activée, n'autoriserait **que** du faible risque : allowlist de chemins **non exécutables**, plafond de LOC, **toutes** les vérifs CI vertes ; garde dure code/exécutable/secret/CI insensible à la casse). ⚠️ **Pas encore câblée dans la boucle du pilote** (le câblage exige une passe CI-aware — suivi). |
| **Auto-revert (politique)** | Filet prévu de l'auto-merge risk-gated : si `main` devenait rouge après un auto-merge (santé en sandbox), un revert serait préparé (fail-closed : santé non concluante = rouge ; un revert en échec **escalade**). Inactif tant que cette politique n'est pas câblée. |
| **Outil MCP du pilote** | Exposé en MCP **uniquement** si `PILOT_TOOL_ENABLED=true` **et** `OAUTH_ENABLED=true` (sinon **refus de démarrer**). Allowlist d'appelants (sujets OAuth vérifiés, jamais un en-tête client). Jamais auto-découvert. `dry_run` par défaut. |

---

## Amélioration continue (Phase 4)

Une fois le MVP construit (`--improve`), le moteur ne s'arrête pas : il **fait
progresser la qualité du projet généré** en ouvrant des PR d'amélioration, sous le
budget restant. Le cœur est une **fonction objectif déterministe** — pas un avis de
LLM — pour qu'une promotion soit reproductible et **sans faux-rejet** :

```
composite = w_cov·(couverture/100)
          − w_séc·sécu_pondérée
          − w_lint·violations_lint
          − w_cx·blocs_complexes
          − w_dep·vulns_deps
```

| Poids | Valeur | Note |
|-------|--------|------|
| `w_cov` (couverture) | `1.0` | Domine : la couverture (0–100) normalisée en 0–1. |
| `w_séc` (sécurité) | `0.1` | Pénalité par unité de score sécu **pondéré par sévérité**. |
| `w_lint` | `0.02` | Pénalité faible par violation (un gain de couverture ne doit pas sauter sur un lint marginal). |
| `w_cx` (complexité) | `0.05` | Pénalité par bloc trop complexe (mccabe). |
| `w_dep` (vulns deps) | `0.5` | **Signal opt-in**, off par défaut (terme nul) ; voir plus bas. |

**Mesuré sur le workspace sur disque** (pas sur le diff d'un round) : l'objectif est
donc **symétrique avant/après**, ce qui permet de promouvoir un vrai gain sans le
faux-rejet d'un signal diff-scopé non déterministe (cause racine corrigée en Phase 4).

### Ce qui entre dans l'objectif (déterministe)

| Signal | Source | Détail |
|--------|--------|--------|
| **Couverture de tests** | `pytest --cov` (ligne `TOTAL`) | `tests_passed` (exit 0) sert de **garde dure** au gate. |
| **Sécurité (pondérée)** | `secret_scan` statique (regex, **zéro LLM**) | Sévérités pondérées (critical 10 / high 5 / medium 2 / low 1). Lockfiles générés + dossiers/fichiers de test/fixtures exclus du scan (sinon ~99 % du poids vient de `package-lock.json`). |
| **Lint** | `ruff` (`E,F,W`) | `--isolated --no-cache` : reproductible, indépendant de la config du projet généré. |
| **Complexité** | `ruff` mccabe (`C901`, seuil 10) | Compte de blocs au-delà du seuil. |

Signaux **informatifs** (corps de PR / relecteur humain, **hors composite gaté**) :
la **revue LLM** (`review_score`) et la **couverture de docstrings** (`doc_coverage`,
proxy de maintenabilité). Un signal LLM diff-scopé est non déterministe → jamais dans
la décision de promotion.

Signal **opt-in** : les **vulnérabilités de dépendances** (`pip-audit` sur
`requirements.txt`). Off par défaut (terme composite nul, règle de gate no-op) ;
activé, il est **gaté en tolérance 0**. `pip-audit` n'est pas imposé en dépendance —
absent ⇒ no-op.

### Le gate (fail-closed) — `collegue/improve/gate.py`

Avant **chaque** PR, deux instantanés (avant/après le diff) sont comparés. Toutes ces
règles doivent passer, sinon le diff est **jeté** (rollback avant promotion — aucune
régression n'atteint la branche de base) :

1. **Tests verts** après le diff (garde dure).
2. **Scores composites finis** (un `NaN`/`inf` = mesure corrompue ⇒ rejet).
3. **Mesurabilité stable** : couverture ET lint/complexité mesurés des deux côtés
   (une bascule « mesuré → non mesuré » gonflerait le composite ⇒ rejet).
4. **Anti-régression par signal** : sécu (tolérance 0), lint (slack 0), complexité
   (slack 0), vulns deps (tolérance 0).
5. **Gain réel** : `Δcomposite ≥ min_gain` (défaut `0.01`) — pas de promotion de bruit.

### La boucle — `collegue/improve/loop.py`

Chaque round, sur un **clone neuf** :

1. **Compounding** — réapplique **et commite** les diffs déjà promus, pour que `HEAD`
   reflète l'état cumulé amélioré. Le diff capturé du round ne contient alors que ses
   **nouveaux** changements (pas de double-comptage), et la baseline monte round après round.
2. **Mesure baseline** → **propose une dimension** (piloté par la métrique) parmi
   `coverage`, `security`, `refactoring`, `documentation`, `consistency` (les trois
   dernières en round-robin de polissage).
3. L'**agent code un diff** pour cette dimension.
4. **Auto-fix lint** — `ruff --fix` + `ruff format` sur les `.py` touchés, **avant** la
   mesure : le coder se concentre sur le fond, le lint auto-corrigible est nettoyé (le
   gate étant tolérance 0 sur le lint). Best-effort ; un fix qui casserait un test est
   rattrapé par la mesure `after` (tests rouges ⇒ rejet).
5. **Mesure après** → **gate**. Accepté ⇒ **PR** + métrique persistée. Rejeté ⇒ diff jeté.

**PR stackées** (mode `--execute`) : chaque PR d'amélioration est basée sur la branche
de la **promotion précédente** → des diffs incrémentaux **mergeables dans l'ordre**,
sans conflit cumulatif. Le merge des PR d'amélioration reste **humain** (§6).

**Arrêt** : `plateau` (deux rounds consécutifs sans promotion — rendements décroissants),
`paused_budget` / `deadline_reached` (budget-temps), ou `safety_cap` (garde-fou
anti-boucle, 50 itérations).

> En `dry_run` (défaut), la boucle va jusqu'aux **aperçus** de PR sans aucune écriture
> (utile pour valider l'objectif et les promotions avant de lancer en réel).

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
entrypoint. La planification est volontairement découpée en trois commandes : le
processus qui appelle le LLM ne peut ni approuver son propre résultat, ni changer la
cible GitHub après coup.

```bash
# 1. Génère et persiste le brouillon, puis affiche son SHA-256
python -m collegue.pilot plan draft \
  --name mon-app --problem "..." --owner mon-org --repo mon-app

# 2. Après relecture humaine, approuve exactement le hash affiché
python -m collegue.pilot plan approve \
  --project-id 1 --expected-plan-hash SHA256_AFFICHE

# 3. Prévisualise la synchronisation, puis l'exécute explicitement
python -m collegue.pilot plan sync --project-id 1
python -m collegue.pilot plan sync --project-id 1 --execute
```

Les trois étapes peuvent vivre dans des processus distincts : le SPEC, le DAG, les
oracles, la deadline et la cible normalisée (dépôt + branche de base) sont relus depuis l'état durable. `approve` et `sync`
n'initialisent aucun contexte LLM. Un hash périmé, une cible altérée ou un override
de cible à ces étapes est refusé en fail-closed.

Une fois le projet synchronisé, le build consomme ce plan approuvé :

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
| `--owner` / `--repo` | Cible GitHub des PR. **Requise et obligatoirement identique au draft scellé.** |
| `--base` | Branche de base des PR (défaut `main`), elle aussi identique au draft scellé. |
| `--execute` | Désactive le `dry_run` : écritures réelles. |
| `--max-iterations` | Garde-fou anti-boucle (optionnel). |
| `--improve` | Après le MVP, enchaîne le moteur d'amélioration sous le budget restant. |

L'exécution réelle de bout en bout (Docker + OpenHands + LLM + écritures GitHub)
nécessite `STATE_DATABASE_URL`, un `GITHUB_TOKEN`, Docker, et un provider LLM
configurés (voir [Réglages](#réglages-env)).

**Codage par abonnement (coût API `$0`).** Au lieu d'une clé API, le codeur peut
passer par un **abonnement** ChatGPT/Codex : mettre `CODER_SUBSCRIPTION=true` +
`SANDBOX_SUBSCRIPTION_AUTH_DIR=~/.openhands` (creds OpenHands montées dans le
sandbox). Le reviewer/juge suit le même chemin quand son modèle n'est pas un
modèle Gemini (`LLM_MODEL_REVIEWER=gpt-5.4` → échantillonné dans le sandbox).
Avec `BUILD_AUTO_MERGE=true` (défaut), un seul `--execute` construit **tout le
MVP** (merge-bot enchaîne les tâches) ; `--improve` ajoute ensuite des PR
d'amélioration **laissées ouvertes** pour merge humain.

---

## Réglages (.env)

En plus des réglages du serveur (voir le [README](../README.md)), le moteur autonome
lit :

| Variable | Description | Défaut |
|----------|-------------|--------|
| `STATE_DATABASE_URL` | État durable (Postgres ou SQLite). Requis pour un run réel. | — |
| `LLM_MODEL_CODER` / `_QA` / `_PLANNER` / `_REVIEWER` | Modèle par **rôle** (codeur fort, QA économique, planificateur, revue). Retombe sur `LLM_MODEL` si absent. | `LLM_MODEL` |
| `MAX_COST_USD` | Plafond dur de dépense cumulée (`0` = désactivé) → auto-pause. Le canal **coder** est pris en compte (#495). Planner et QA débitent chaque appel/retry au tarif du rôle/modèle effectif ; sous plafond `$`, un modèle remote sans tarif autoritaire est refusé avant sampling. L'abonnement reste à coût API `$0`, avec ses tokens comptés séparément. | `0` |
| `LLM_PRICE_PROMPT_PER_1M` | Prix de secours du canal coder, $/1M tokens prompt — utilisé quand le runner émet `cost_usd=0` malgré des tokens (#484). | `0` |
| `LLM_PRICE_COMPLETION_PER_1M` | Idem, $/1M tokens completion. `0` = désactivé. | `0` |
| `MAX_TOKENS_BUDGET` | Plafond dur de tokens cumulés (`0` = désactivé). Planner/QA exigent une enveloppe d'usage non nulle quand ce plafond est actif ; réponse sans preuve = planification refusée avant persistance. | `0` |
| `BUDGET_EXHAUSTED_ACTION` | `pause` (refuse les appels LLM) ou `warn` (journalise seulement). | `pause` |
| `COLLEGUE_RUN_DEADLINE_SECONDS` | Durée mur max d'un run (`0` = pas de deadline). | `0` |
| `TASK_MAX_ATTEMPTS` | Tentatives max par tâche — retry avec backoff sur échec transitoire (`1` = pas de retry). | `3` |
| `TASK_RETRY_BACKOFF_SECONDS` | Base du backoff linéaire entre tentatives (plafonné à 90 s). | `15` |
| `DEPS_REQUIRE_MERGED` | Exige le **merge** d'une dépendance avant de débloquer ses dépendants (sinon le démarrage sur PR non mergée est signalé) ; arrêt `awaiting_merge` quand seuls des merges manquent. **Forcé à vrai** quand `BUILD_AUTO_MERGE` est actif. | `false` |
| `BUILD_AUTO_MERGE` | **Merge-bot de la phase build** : auto-merge (squash) de chaque PR de tâche + resync du clone avant la suivante (+ drain de la dernière PR). `false` → tout au merge humain. La phase amélioration n'est **jamais** auto-mergée. | `true` |
| `LLM_CALL_TIMEOUT` | Timeout par appel LLM, secondes (`0` = off). | `0` |
| `CODER_SUBSCRIPTION` | Code via un **abonnement** ChatGPT/Codex (OpenHands `subscription_login`, coût API `$0`) au lieu d'une clé API. Le **reviewer/juge** suit aussi l'abonnement si son modèle n'est pas un modèle Gemini (échantillonné dans le sandbox). | `false` |
| `CODER_SUBSCRIPTION_MODEL` | Modèle codeur via l'abonnement. | `gpt-5.5` |
| `CODER_SUBSCRIPTION_FALLBACK` | Modèle(s) de repli de l'abonnement (CSV). | `gpt-5.4` |
| `SANDBOX_SUBSCRIPTION_AUTH_DIR` | Dossier des creds d'abonnement OpenHands, monté en lecture/écriture dans le sandbox (ex. `~/.openhands`). Requis si `CODER_SUBSCRIPTION`. | — |
| `SANDBOX_NETWORK` | Réseau Docker du sandbox coder (`bridge` / `host`). `host` si le bridge stalle les gros transferts. | `bridge` |
| `SANDBOX_MEMORY` | Plafond mémoire du conteneur coder. | `6g` |
| `SANDBOX_CPUS` | Plafond CPU du conteneur coder. | `2.0` |
| `SANDBOX_TIMEOUT` | Timeout d'une exécution coder en sandbox, secondes. | `2400` |
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

### CI nightly « integration » (#404)

Le workflow [`integration-nightly.yml`](../.github/workflows/integration-nightly.yml)
exécute chaque nuit (03:17 UTC, ou à la demande via *Run workflow*) la suite
`pytest -m integration` que la CI PR exclut :

| Chemin réel exercé | Pré-requis | Fourni par |
|--------------------|------------|------------|
| État durable **PostgreSQL** (checkpoints, reprise) | `STATE_DATABASE_URL` | service container `postgres:16` (aucun secret) |
| **DockerSandbox** réel (isolation FS, persistance, kill au timeout) | Docker + image | runner GitHub + `python:3.12-slim` |
| Appels **LLM réels** (chaînes de délégation…) | clé API | secret `INTEGRATION_LLM_API_KEY` |
| Sentry / GitHub réels | tokens | secrets `INTEGRATION_SENTRY_AUTH_TOKEN`, `INTEGRATION_SENTRY_ORG`, `INTEGRATION_GITHUB_TOKEN` |

**Secrets** (Settings → Secrets and variables → Actions) — tous **optionnels** : un
test dont le pré-requis manque se **skippe** avec sa raison (`-rs`). Garde-fou
anti-vacuité : si *tous* les tests ont été skippés, le job **échoue** (un nightly
vert qui n'a rien exercé serait une fausse assurance). Les coûts LLM sont bornés
par le budget dur (`MAX_COST_USD=2`, `MAX_TOKENS_BUDGET=2M`, deadline 30 min) —
coût attendu par run : ~0 à 2 $ selon les secrets fournis.

En cas d'échec, le workflow ouvre (ou commente) une issue **« CI nightly
integration en échec »** avec le lien du run — la CI PR normale n'est jamais
bloquée par le nightly.

> Étape suivante (suivi #404) : un smoke **end-to-end** réel (plan → `--execute`
> sur un dépôt fixture → PR ouverte puis nettoyée). Il exige une image sandbox
> embarquant OpenHands, que le dépôt ne fournit pas encore.
