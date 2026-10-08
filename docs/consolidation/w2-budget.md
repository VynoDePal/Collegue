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

**Valeurs et identités.** Un plafond (`MAX_COST_USD`, `MAX_TOKENS_BUDGET`), un montant, des tokens ou des micro-USD invalides (NaN, inf, bool, illisible, négatif, fraction de token) sont **refusés** (`ValueError`) : seul `None`/`0` signifie « pas de plafond », et une valeur invalide ne le désactive jamais. Un `reservation_id` ou un `event_key` rejoué avec un sens contradictoire (autre scope, autre montant, autre type, autre modèle ; commit rejoué avec un autre montant ; libération sous la clé d'un commit) lève `BudgetIdentityError`, sur le chemin normal **et** sur la course `IntegrityError` (SQLite et PostgreSQL, plusieurs connexions) ; une contrainte CHECK/FK violée n'est jamais prise pour un rejeu.

Codes de refus : `cap_usd`, `cap_tokens`, `blocked_unknown_usage`, `ledger_unavailable`, `unbounded_transport`, `deadline`, `scope_missing`. `BudgetRefused` est un `BaseException` (comme `BudgetExceeded`) : il traverse les `except Exception` des chemins LLM ; le pilote le convertit en arrêt `paused_budget` à la frontière d'une tâche (la tâche repasse `todo`).

## 3. Qui décide, qui affiche

- **Décision** : `BudgetTimeController.should_continue()` lit le registre (après `recover_expired`) dès qu'un scope est attaché (`attach_project_budget`, fait par `run_project` / `run_improvement` pour tout run RÉEL). Le `MetricsCollector` et l'accumulateur coder ne décident plus.
- **Affichage** : `RunAuditLog.cost`, `run_cost_summary` et la métrique `run_cost_usd` sont des **projections du registre** (pas de cumul local → pas de double comptage). Le `MetricsCollector` reste une source de statistiques d'experts.
- **Cumul commun** : planification, BUILD (toutes les passes du merge-bot), IMPROVE, sampling de tous les rôles, retries, replis, reprises — un seul scope par projet, retrouvé après redémarrage.
- **dry-run** : aucune écriture au registre.

## 4. Transports : acceptés et refusés en mode strict sous plafond

| Transport | Contrôle avant émission | Statut en strict |
| --- | --- | --- |
| Sampling HTTP OpenAI-compatible hors serveur (`LocalSamplingContext._create`), planner, QA, reviewer, juge, adéquation | une **réservation par tentative** ; borne haute du payload complet (§10) ; `max_tokens` obligatoire et transmis ; SDK `max_retries=0`, la boucle est la nôtre | **Accepté** (familles de tokenizer connues ou attestées, texte seul) |
| Handler de sampling du serveur MCP, **avec** un scope lié | idem ; le handler **injecte** `max_tokens` (4096) s'il manque, pour que la sortie soit réellement bornée | Accepté |
| Même handler **sans** scope lié (outils MCP sans projet) | garde historique `enforce_budget` (collector) | **Hors registre**, signalé, aucune garantie |
| Sampler d'abonnement (`_sample_subscription`, Docker) | réservation de tokens avant le `docker run` ; conteneur nommé + auto-limité + `docker kill` par nom ; payload `strict` ⇒ `max_output_tokens` borné et `num_retries=0` | Accepté en **tokens** (0 $) ; l'honneur du plafond de sortie par le backend abonnement n'est pas vérifiable hors ligne → détecté a posteriori (§10) |
| Worker OpenHands SDK (`OHSdkAgent`, `budget_enforcement="in-runner"`) **avec abonnement** (0 $/token) | allocation réservée ; le runner contrôle chaque appel du framework (§6) | **Accepté**, plafond de tokens ; limite résiduelle §5 |
| Worker OpenHands SDK **avec clé facturable** | — | **REFUSÉ** (`unbounded_transport`) : une commande du workspace a la même clé et un réseau libre, `in-runner` n'est pas une barrière effective. Disponible en `advisory` (aucune garantie annoncée) |
| Worker historique (`OpenHandsAgent`, `budget_enforcement="none"`) | aucun | **REFUSÉ** ; accepté en `advisory` |
| Agent **sans** attribut `budget_enforcement` | — | **REFUSÉ** : aucune garantie par défaut |
| Double déterministe qui ne dépense rien (`FakeCodeAgent`, doubles de test) | réservation + règlement | Accepté s'il déclare **explicitement** `budget_enforcement = "test-double"` — ne jamais le déclarer sur un agent réel |
| Modèle sans tarif autoritaire sous plafond USD | — | **REFUSÉ** ; borné en tokens s'il n'y a pas de plafond USD |
| Famille de tokenizer inconnue (hors `gpt-`/`o1`/`o3`/`o4`/`gemini`/`gemma`/`claude`) | — | **REFUSÉ** tant que l'opérateur n'atteste pas `BUDGET_ATTESTED_BYTE_TOKENIZER_MODELS` |
| Modalité non textuelle (image, audio, fichier, vidéo), `bytes`, sortie non bornée | — | **REFUSÉ** avant émission |

## 5. Périmètre exact de la garantie stricte

La garantie porte sur **les appels que notre code émet** : chaque tentative est réservée avant l'émission, aucun appel n'est émis si le solde, le blocage ou l'échéance l'interdit, et une configuration non bornable est refusée plutôt que prétendue.

Elle **ne protège pas** d'un programme du workspace qui disposerait de la clé et d'un accès réseau libre au fournisseur : un `curl` lancé par l'agent n'est pas un appel du framework. Arbitrage retenu, sans nouvelle infrastructure obligatoire :

- clé **facturable** accessible au workspace ⇒ le mode strict sous plafond **refuse** le worker (pas de garantie en dollars prétendue) ; `advisory` reste possible, honnêtement documenté comme sans garantie ;
- clé d'**abonnement** (0 $/token) ⇒ pas d'exposition en dollars ; le plafond de tokens borne les appels du framework, pas une commande du workspace qui utiliserait les creds montés (limite résiduelle assumée).

Une vraie barrière (clé détenue par l'hôte derrière un proxy de comptage, réseau du conteneur restreint à ce proxy) reste une décision d'architecture / un raccordement Docker (B) : non faite ici.

## 6. Worker : allocation, échéance, décès du client

- `allocate_worker` : réserve `BUDGET_WORKER_SHARE` (80 %) du solde (USD et tokens), plafonné par `BUDGET_WORKER_MAX_*`, plancher d'allocation utile `BUDGET_WORKER_MIN_USD/TOKENS` (sinon pause budget : sans plancher les allocations décroissent géométriquement sans jamais atteindre zéro). L'échéance est la plus proche entre l'échéance du run et le timeout sandbox.
- Le runner reçoit `--budget-usd --budget-tokens --deadline-epoch --prices '{modèle:[in,out]}' [--strict] [--byte-bounded-models …] [--no-billing]`. La table de tarifs couvre **tout** la chaîne (principal + replis, `OHSdkAgent.model_chain()`) : un repli est tarifé à son propre prix, un modèle sans tarif sous plafond USD ou de famille de tokenizer inconnue est **écarté** (le suivant est tenté) ; le principal non bornable fait refuser le lancement (code 3).
- Capacité de l'agent (`worker_budget` en-tête) : absente ⇒ refus ; `none` ⇒ refus ; `test-double` ⇒ accepté ; `in-runner` ⇒ accepté seulement sans exposition en dollars. Les réglages `BUDGET_WORKER_*` et la durée sont validés strictement (NaN/inf/bool/négatif/illisible ⇒ refus `unbounded_transport`, jamais une valeur corrigée).
- **Échéance pendant la tâche** : le runner refuse tout appel passé l'échéance, et un **chien de garde** (thread) l'applique PENDANT un appel en vol : marqueur `unknown`, arrêt immédiat (code 5), usage inconnu ; le conteneur est enveloppé par `timeout --signal=TERM --kill-after=15 N` : **il s'arrête seul si le client `docker` ou le process hôte meurt**. Le runner traite `SIGTERM` en sortie propre pour vider l'usage. Au dépassement hôte, `docker kill` par nom.
- **Échecs** : seuls un rejet HTTP prouvé (400/401/403/404/405/413/415/422/429, retry seulement 429) et un échec de connexion avant l'envoi sont « sans consommation ». Tout autre échec (5xx, 408, 409, timeout, annulation) ⇒ marqueur `unknown`, **ni retry ni repli**, arrêt du worker. Une réponse sans usage compté par le SDK ou une borne démentie ⇒ `unknown` aussi.
- **Règlement** : marqueur `unknown` ⇒ usage inconnu **même si `final` est présent** (`final` prouve que les deltas ont été vidés, pas que les compteurs ont tout vu). Marqueur `final` seul → usage complet → `commit` + reliquat libéré. Armé sans `final` (conteneur tué), timeout hôte, ou coût indéterminé → `mark_unknown` (réservation conservée, suite stricte bloquée). Runner mort **avant** d'armer, sans aucun usage (crash d'import #498) → zéro **établi**, pas de blocage. Worker jamais lancé (Docker absent, montage refusé) → `release`.

## 7. Planification

`plan_project_from_settings` crée le scope de planification **avant** `generate_spec`, le lie au projet après `persist_spec`, et conserve l'échec (`last_error`) avec sa dépense. Décomposition et QA §4.7 utilisent le même scope ; BUILD le retrouve par `project_id`. Les fichiers `planner/*` n'ont pas eu besoin de modification : `accounted_sample` est lui-même sensible au registre.

## 8. Migration 0011 et import unique

Additive. Les métriques `run_cost_usd` / `run_tokens` sont des **snapshots cumulatifs ordonnés par `id`** : seule la **dernière** valeur par projet est importée (les sommer compterait N fois). Les unicités (`scope_key`, `project_id`, `reservation_id`, `event_key`) rendent l'import exactement-une-fois, y compris par `create_all` (SQLite/tests, import lazy à la 1ʳᵉ ouverture du scope) et après `downgrade` + `upgrade`. Testé sur SQLite et PostgreSQL.

## 9. Tests et commandes

- Registre : `tests/test_budget_ledger.py` (SQLite, concurrence 2–12 connexions).
- **PostgreSQL réel** : `tests/test_budget_ledger_postgres.py` — lance un cluster jetable (`initdb` + `pg_ctl`, socket unix, utilisateur courant, aucun secret) ou utilise `COLLEGUE_TEST_POSTGRES_URL` (service PostgreSQL de la CI). Concurrence multi-connexions, rejeu, blocage, contraintes, migration + import. Ne se saute pas : sans PostgreSQL ni URL il **échoue** avec la marche à suivre ; initdb refuse de tourner en root → fournir l'URL.
  `pytest tests/test_budget_ledger_postgres.py`
- Transports : `tests/test_budget_transport.py` ; garde du runner : `tests/test_oh_runner_budget.py` ; sandbox auto-limité : `tests/test_sandbox.py` ; acceptation pilote (BUILD ×2, BUILD→IMPROVE, redémarrage, crash, échéance, transport non bornable, planification) : `tests/test_budget_pilot.py` ; défauts de l'audit sur API préexistantes : `tests/test_budget_baseline_behavior.py` (rouges sur la base, verts ici).

## 10. Hypothèses de tarification et de bornes

1. Prix = grille autoritaire de `monitoring/pricing.py` (standard, ≤ 200k) ou `LLM_PRICE_*_PER_1M`. **Aucun prix de repli** : un modèle distant inconnu n'est pas bornable en USD. Chaque modèle (principal, repli, modèle changé) est tarifé à **son** prix.
2. Providers locaux et Gemma 4 gratuit : 0 $ **autoritaire** ; abonnement (`billable=false`) : 0 $, mais des tokens sont comptés.
3. **Borne du prompt = octets UTF-8 du payload COMPLET transmis** (messages, système, outils, schémas, clés) + cadrage fixe (16/message, 64/outil, 64/requête). Justification : un tokenizer à repli octet (BPE byte-level : tiktoken/GPT ; SentencePiece à byte-fallback : Gemini/Gemma, Claude) ne produit jamais plus de tokens que d'octets. Ce n'est PAS une moyenne « chars/N » (l'ancien `chars/2 + 32` sous-estimait, p. ex. les emojis). Hypothèse : valable pour les familles listées ou **attestées par l'opérateur** ; sinon refus en strict.
4. **Sortie** : `max_tokens` doit être un entier > 0 réellement transmis (HTTP : vérifié par test ; handler MCP : injecté ; runner : `max_output_tokens` du LLM obligatoire). Hypothèse : les tokens de raisonnement comptent dans ce plafond (familles prises en charge). Les compteurs réels sont comparés à la borne **après** l'appel : si le fournisseur la dément, la consommation réelle est engagée en entier et le scope est **bloqué** (la borne était une hypothèse, la réalité l'a démentie).
5. Cache, audio, grands prompts non distingués : le tarif standard est supposé être une borne haute.
6. Une réponse HTTP d'erreur **ne prouve pas** l'absence de facturation. Release uniquement pour un rejet prouvé (400/401/403/404/405/413/415/422/429) ou un échec de connexion avant l'envoi ; 5xx, 408, 409, timeout, lecture interrompue, annulation, exception inconnue ⇒ **usage inconnu**, réservation conservée, **aucun retry** en strict (HTTP `guarded_call` comme worker `BudgetGuard`). Le mode `advisory` garde le comportement historique du SDK.
7. **Échéance** : appliquée aussi PENDANT un appel en vol (`asyncio.wait_for` sur le temps restant, annulation, usage inconnu, `BudgetRefused(deadline)` ⇒ arrêt `deadline_reached`) et avant un backoff qui la franchirait.

## 11. Opérateur

`pilot.budget.budget_status(manager, pid)` donne le scope, le motif de blocage durable et les réservations inconnues ; `resolve_unknown_usage(manager, pid, reservation_id, usd=…, tokens=…)` règle avec la dépense réelle (relevé fournisseur) et débloque le strict. Aucun sous-commande CLI n'est ajoutée (`pilot/__main__.py` n'est pas du lot).

## 12. Configuration

`BUDGET_MODE=strict|advisory` (défaut strict), `BUDGET_WORKER_SHARE`, `BUDGET_WORKER_MAX_USD/TOKENS`, `BUDGET_WORKER_MIN_USD/TOKENS`, `BUDGET_ATTESTED_BYTE_TOKENIZER_MODELS` (CSV de préfixes de modèles dont l'opérateur atteste « tokens ≤ octets UTF-8 »), et les existants `MAX_COST_USD`, `MAX_TOKENS_BUDGET`, `BUDGET_EXHAUSTED_ACTION` (`warn` ⇒ non strict). Le blocage par usage inconnu ne s'applique que si un plafond est configuré (sans plafond il n'y a rien à protéger ; l'inconnu reste tracé).

## 13. Raccordements pour B / C

- `alembic/versions/0011_budget_ledger.py` : à déplacer mécaniquement dans le package avec les révisions existantes (B ne le modifie pas).
- `collegue/executor/oh_runner.py` est copié seul dans l'image : **il n'importe que la stdlib** (aucun nouveau module à embarquer) ; le nouveau marqueur de sortie est `[collegue-budget]`.
- CI : `tests/test_budget_ledger_postgres.py` tourne dans le job `pytest` existant (les images `ubuntu-latest` fournissent PostgreSQL sous `/usr/lib/postgresql/*/bin`) ; pour être explicite, B peut ajouter un `services: postgres` et exporter `COLLEGUE_TEST_POSTGRES_URL`. **Ne pas utiliser le workflow nightly** (appels LLM réels).
- Dockerfile sandbox : `coreutils timeout` est requis dans l'image (présent dans `python:3.12-slim`).

## 14. Limites

Voir `reports/w2-a.md`.
