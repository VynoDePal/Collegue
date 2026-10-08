# Vague 2 (mission A) — budget durable commun et exécution bornée

## 1. Défauts établis (audit du 28/09)

| Défaut | Cause |
| --- | --- |
| Deux passes BUILD à 0,60 $ sous plafond 1 $ : le contrôleur autorisait encore la suite | le cumul « coder » vivait dans un dict en mémoire recréé à chaque `run_project` ; le merge-bot rappelle `run_project` entre les fusions |
| Deux tentatives IMPROVE à 0,70 $ non débitées | la boucle `improve` ne lisait aucun cumul coder |
| Un redémarrage remettait la dépense à zéro | décisions fondées sur `MetricsCollector` (RAM + `metrics.json` best-effort) |
| Un worker pouvait dépasser budget et échéance pendant UNE tâche | le budget n'était consulté qu'entre deux tâches ; aucune borne côté conteneur |
| Frais de SPEC perdus | `generate_spec` dépense avant que le projet ait un ID |

## 2. Le registre (`collegue/state/budget_ledger.py`)

Trois tables, migration additive `alembic/versions/0011_budget_ledger.py` (`revision="0011"`, `down_revision="0010"`) :

- `budget_scopes` — un compte par projet/cycle (ou par contexte de planification avant que le projet existe). Plafonds, mode `strict`, agrégats `consumed / reserved / unknown` (micro-USD et tokens), `blocked_reason` durable, `last_error`.
- `budget_reservations` — une ligne par réservation, `reservation_id` **unique** et durable ; états `reserved → committed | released | unknown`.
- `budget_events` — journal append-only des règlements ; `event_key` **unique** = idempotence.

**Précision monétaire** : entiers micro-USD (1e-6 $), arrondis **vers le haut** pour toute dépense, **vers le bas** pour les plafonds (`usd_to_micro`, `cap_to_micro`). NaN/inf/négatif/bool refusés.

**Atomicité** : `reserve` est un `UPDATE budget_scopes SET reserved += X WHERE id=? AND (strict=false OR cap IS NULL OR consommé+réservé+inconnu+X <= cap) AND (pas bloqué)` — compare-and-set sur la ligne du scope — dans la **même transaction** que l'insertion de la réservation. Pas de verrou process, pas de lecture-puis-écriture en Python. Vérifié sur **SQLite** (plusieurs connexions réelles) et **PostgreSQL 16** (cluster réel, voir §9). Contraintes `CHECK` (compteurs ≥ 0) = défense en profondeur.

**Idempotence** : rejouer un `reservation_id` rend la réservation existante ; rejouer un `event_key` n'a aucun effet ; un règlement avec une AUTRE clé sur une réservation déjà réglée est refusé (`BudgetLedgerError`) — jamais compté deux fois. Une course de rejeu est tranchée par la contrainte d'unicité.

**Interface publique** (`ProjectStateManager.budget_ledger`, sans état en mémoire — deux instances sur la même base voient le même registre) :

| Appel | Effet |
| --- | --- |
| `scope_for_project(pid, max_cost_usd, max_tokens, strict)` | ouvre/retrouve le scope (plafonds = config courante) ; **import unique** des cumuls historiques |
| `create_planning_scope()` / `bind_project(scope_key, pid)` / `note_failure()` | contexte durable avant la première dépense ; lié au projet dès qu'il existe ; échec conservé |
| `reserve(scope, usd/micro_usd, tokens, kind, role, model, transport, reservation_id, ttl/expires_at)` | réserve AVANT dépense ; lève `BudgetRefused(code)` |
| `commit(reservation_id, usd, tokens, event_key)` | consommation établie ; libère le reliquat |
| `release(reservation_id, reason)` | uniquement si l'absence de consommation est ÉTABLIE |
| `mark_unknown(reservation_id, reason)` | usage inconnu : réservation **conservée**, scope **bloqué** (strict) |
| `resolve_unknown(...)` | l'opérateur règle avec la dépense réelle (`pilot.budget.resolve_unknown_usage`) |
| `recover_expired(scope)` | réservations jamais réglées après leur échéance → `unknown` |
| `snapshot(scope)` | `consumed / reserved / unknown / balance / blocked_reason` |

Codes de refus : `cap_usd`, `cap_tokens`, `blocked_unknown_usage`, `ledger_unavailable`, `unbounded_transport`, `deadline`, `scope_missing`. `BudgetRefused` est un `BaseException` (comme `BudgetExceeded`) : il traverse les `except Exception` des chemins LLM ; le pilote le convertit en arrêt `paused_budget` à la frontière d'une tâche (la tâche repasse `todo`).

## 3. Qui décide, qui affiche

- **Décision** : `BudgetTimeController.should_continue()` lit le registre (après `recover_expired`) dès qu'un scope est attaché (`attach_project_budget`, fait par `run_project` / `run_improvement` pour tout run RÉEL). Le `MetricsCollector` et l'accumulateur coder ne décident plus.
- **Affichage** : `RunAuditLog.cost`, `run_cost_summary` et la métrique `run_cost_usd` sont des **projections du registre** (pas de cumul local → pas de double comptage). Le `MetricsCollector` reste une source de statistiques d'experts.
- **Cumul commun** : planification, BUILD (toutes les passes du merge-bot), IMPROVE, sampling de tous les rôles, retries, replis, reprises — un seul scope par projet, retrouvé après redémarrage.
- **dry-run** : aucune écriture au registre.

## 4. Transports : garantis et non garantis

| Transport | Contrôle avant émission | Garantie stricte |
| --- | --- | --- |
| Sampling HTTP OpenAI-compatible hors serveur (`LocalSamplingContext._create`), planner, QA, reviewer, juge, adéquation | une **réservation par tentative** (retries compris) ; le SDK ne retente jamais en interne (`max_retries=0`), la boucle est la nôtre | **Oui**, pour les appels émis par notre code |
| Handler de sampling du serveur MCP (`UsageTrackingSamplingHandler`), **avec** un scope lié | idem | Oui |
| Même handler **sans** scope lié (outils MCP sans projet) | garde historique `enforce_budget` (collector) | **Non** — hors registre, signalé |
| Sampler d'abonnement (`_sample_subscription`, Docker) | réservation de tokens avant le `docker run` ; conteneur nommé + auto-limité (`timeout`) + `docker kill` par nom au timeout hôte | Oui en tokens ; 0 $ (non facturé). Tout échec = usage inconnu |
| Worker OpenHands SDK (`OHSdkAgent` + `oh_runner`) | allocation réservée avant lancement ; **le runner contrôle chaque appel** (retries, replis de modèle) contre l'allocation et l'échéance ; conteneur auto-limité ; marqueurs `armed`/`final` | Oui **pour les appels du framework d'agent** ; voir §5 |
| Worker OpenHands historique (`OpenHandsAgent`, CLI `openhands.core.main`) | aucun | **Refusé en strict sous plafond** (`unbounded_transport`) ; accepté en `advisory` |
| Modèle sans tarif autoritaire sous plafond USD | — | **Refusé** (`unbounded_transport`) ; borné en tokens s'il n'y a pas de plafond USD |
| Double de test / agent non déclaré | réservation + règlement après coup | Aucune garantie intra-passe (documenté) |

## 5. Périmètre exact de la garantie stricte

La garantie porte sur **les appels que notre code émet** : chaque tentative est réservée avant l'émission, aucun appel n'est émis si le solde, le blocage ou l'échéance l'interdit, et une configuration non bornable est refusée plutôt que prétendue.

Elle **ne protège pas** d'un programme malveillant du workspace qui disposerait de la clé facturable (variable d'environnement du conteneur coder) et d'un accès réseau libre au fournisseur : un `curl` lancé par l'agent n'est pas un appel du framework. Fermer cette voie exige d'empêcher la dépense **hors du canal réservé** (clé détenue par l'hôte derrière un proxy de comptage, réseau du conteneur restreint à ce proxy) : ce n'est PAS fait ici — c'est un raccordement réseau/Docker (B) et une décision d'architecture. Le mode `advisory` ne revendique aucune garantie.

## 6. Worker : allocation, échéance, décès du client

- `allocate_worker` : réserve `BUDGET_WORKER_SHARE` (80 %) du solde (USD et tokens), plafonné par `BUDGET_WORKER_MAX_*`, plancher d'allocation utile `BUDGET_WORKER_MIN_USD/TOKENS` (sinon pause budget : sans plancher les allocations décroissent géométriquement sans jamais atteindre zéro). L'échéance est la plus proche entre l'échéance du run et le timeout sandbox.
- Le runner reçoit `--budget-usd --budget-tokens --deadline-epoch --price-in --price-out [--no-billing]`.
- **Échéance pendant la tâche** : le runner refuse tout appel passé l'échéance ; le conteneur est enveloppé par `timeout --signal=TERM --kill-after=15 N` : **il s'arrête seul si le client `docker` ou le process hôte meurt**. Le runner traite `SIGTERM` en sortie propre pour vider l'usage. Au dépassement hôte, `docker kill` par nom.
- **Règlement** : marqueur `final` → usage complet → `commit` + reliquat libéré. Armé sans `final` (conteneur tué), ou coût indéterminé → `mark_unknown` (réservation conservée, suite stricte bloquée). Runner mort **avant** d'armer, sans aucun usage (crash d'import #498) → zéro **établi**, pas de blocage. Worker jamais lancé (Docker absent, montage refusé) → `release`.

## 7. Planification

`plan_project_from_settings` crée le scope de planification **avant** `generate_spec`, le lie au projet après `persist_spec`, et conserve l'échec (`last_error`) avec sa dépense. Décomposition et QA §4.7 utilisent le même scope ; BUILD le retrouve par `project_id`. Les fichiers `planner/*` n'ont pas eu besoin de modification : `accounted_sample` est lui-même sensible au registre.

## 8. Migration 0011 et import unique

Additive. Les métriques `run_cost_usd` / `run_tokens` sont des **snapshots cumulatifs ordonnés par `id`** : seule la **dernière** valeur par projet est importée (les sommer compterait N fois). Les unicités (`scope_key`, `project_id`, `reservation_id`, `event_key`) rendent l'import exactement-une-fois, y compris par `create_all` (SQLite/tests, import lazy à la 1ʳᵉ ouverture du scope) et après `downgrade` + `upgrade`. Testé sur SQLite et PostgreSQL.

## 9. Tests et commandes

- Registre : `tests/test_budget_ledger.py` (SQLite, concurrence 2–12 connexions).
- **PostgreSQL réel** : `tests/test_budget_ledger_postgres.py` — lance un cluster jetable (`initdb` + `pg_ctl`, socket unix, utilisateur courant, aucun secret) ou utilise `COLLEGUE_TEST_POSTGRES_URL` (service PostgreSQL de la CI). Concurrence multi-connexions, rejeu, blocage, contraintes, migration + import. Ne se saute pas : sans PostgreSQL ni URL il **échoue** avec la marche à suivre ; initdb refuse de tourner en root → fournir l'URL.
  `pytest tests/test_budget_ledger_postgres.py`
- Transports : `tests/test_budget_transport.py` ; garde du runner : `tests/test_oh_runner_budget.py` ; sandbox auto-limité : `tests/test_sandbox.py` ; acceptation pilote (BUILD ×2, BUILD→IMPROVE, redémarrage, crash, échéance, transport non bornable, planification) : `tests/test_budget_pilot.py` ; défauts de l'audit sur API préexistantes : `tests/test_budget_baseline_behavior.py` (rouges sur la base, verts ici).

## 10. Hypothèses de tarification

1. Prix = grille autoritaire de `monitoring/pricing.py` (standard, ≤ 200k) ou `LLM_PRICE_*_PER_1M`. **Aucun prix de repli** : un modèle distant inconnu n'est pas bornable en USD.
2. Providers locaux et Gemma 4 gratuit : 0 $ **autoritaire** ; abonnement (`billable=false`) : 0 $, mais des tokens sont comptés.
3. Estimation d'un appel : prompt ≈ 1 token pour 2 caractères + 32 (réel ≈ 3–4) + `max_tokens` de sortie. Sur-réserver ne coûte que de la marge près du plafond ; le réel remplace l'estimation au règlement.
4. Cache, audio, grands prompts non distingués : le tarif standard est supposé être une borne haute.
5. Une réponse HTTP d'erreur (4xx/5xx) ou un échec de **connexion** avant l'envoi n'est pas facturé → `release`. Un timeout, une lecture interrompue, une annulation ou une exception inconnue → **usage inconnu**.

## 11. Opérateur

`pilot.budget.budget_status(manager, pid)` donne le scope, le motif de blocage durable et les réservations inconnues ; `resolve_unknown_usage(manager, pid, reservation_id, usd=…, tokens=…)` règle avec la dépense réelle (relevé fournisseur) et débloque le strict. Aucun sous-commande CLI n'est ajoutée (`pilot/__main__.py` n'est pas du lot).

## 12. Configuration

`BUDGET_MODE=strict|advisory` (défaut strict), `BUDGET_WORKER_SHARE`, `BUDGET_WORKER_MAX_USD/TOKENS`, `BUDGET_WORKER_MIN_USD/TOKENS`, et les existants `MAX_COST_USD`, `MAX_TOKENS_BUDGET`, `BUDGET_EXHAUSTED_ACTION` (`warn` ⇒ non strict). Le blocage par usage inconnu ne s'applique que si un plafond est configuré (sans plafond il n'y a rien à protéger ; l'inconnu reste tracé).

## 13. Raccordements pour B / C

- `alembic/versions/0011_budget_ledger.py` : à déplacer mécaniquement dans le package avec les révisions existantes (B ne le modifie pas).
- `collegue/executor/oh_runner.py` est copié seul dans l'image : **il n'importe que la stdlib** (aucun nouveau module à embarquer) ; le nouveau marqueur de sortie est `[collegue-budget]`.
- CI : `tests/test_budget_ledger_postgres.py` tourne dans le job `pytest` existant (les images `ubuntu-latest` fournissent PostgreSQL sous `/usr/lib/postgresql/*/bin`) ; pour être explicite, B peut ajouter un `services: postgres` et exporter `COLLEGUE_TEST_POSTGRES_URL`. **Ne pas utiliser le workflow nightly** (appels LLM réels).
- Dockerfile sandbox : `coreutils timeout` est requis dans l'image (présent dans `python:3.12-slim`).

## 14. Limites

Voir `reports/w2-a.md`.
