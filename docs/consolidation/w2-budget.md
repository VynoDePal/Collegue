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

Quatre tables, migration additive `alembic/versions/0011_budget_ledger.py` (`revision="0011"`, `down_revision="0010"`) :

- `budget_scopes` — un compte par projet/cycle (ou par contexte de planification avant que le projet existe). Plafonds, mode `strict`, agrégats `consumed / reserved / unknown` (micro-USD et tokens), `blocked_reason` durable, `last_error`.
- `budget_reservations` — une ligne par réservation, `reservation_id` **unique** et durable ; états `reserved → committed | released | unknown`.
- `budget_events` — journal append-only des règlements ; `event_key` **unique** = idempotence.
- `budget_blocks` — causes de blocage **indépendantes** de l'usage d'un appel (`bound_violation`, `ambiguous_history`, `manual`) : `block_key` unique, ouverte jusqu'à une résolution explicite.

`budget_scopes` porte aussi `claim_token` / `claim_expires_at` : le droit exclusif d'un cycle de planification (compare-and-set).

**Précision monétaire** : entiers micro-USD (1e-6 $), arrondis **vers le haut** pour toute dépense, **vers le bas** pour les plafonds (`usd_to_micro`, `cap_to_micro`). NaN/inf/négatif/bool refusés.

**Atomicité** : `reserve` est un `UPDATE budget_scopes SET reserved += X WHERE id=? AND (strict=false OR cap IS NULL OR consommé+réservé+inconnu+X <= cap) AND (pas bloqué)` — compare-and-set sur la ligne du scope — dans la **même transaction** que l'insertion de la réservation. Pas de verrou process, pas de lecture-puis-écriture en Python. Vérifié sur **SQLite** (plusieurs connexions réelles) et **PostgreSQL 16** (cluster réel, voir §9). Contraintes `CHECK` (compteurs ≥ 0) = défense en profondeur.

**Idempotence** : rejouer un `reservation_id` rend la réservation existante ; rejouer un `event_key` n'a aucun effet ; un règlement avec une AUTRE clé sur une réservation déjà réglée est refusé (`BudgetLedgerError`) — jamais compté deux fois. Une course de rejeu est tranchée par la contrainte d'unicité.

**Interface publique** (`ProjectStateManager.budget_ledger`, sans état en mémoire — deux instances sur la même base voient le même registre) :

| Appel | Effet |
| --- | --- |
| `scope_for_project(pid, max_cost_usd, max_tokens, strict)` | ouvre/retrouve le scope (plafonds = config courante) ; **import unique** des cumuls historiques |
| `open_planning_cycle(cycle_key, …)` / `release_planning_claim()` / `ProjectStateManager.create_project_in_cycle()` | cycle de planification à identité durable (§7) : scope retrouvé, droit exclusif, projet créé **et** lié en une transaction |
| `create_planning_scope()` / `bind_project(scope_key, pid)` / `note_failure()` | contexte durable sans identité de cycle ; lien a posteriori ; échec conservé |
| `block(scope, reason, event_key, kind)` / `open_blocks()` / `resolve_block(scope, block_key, event_key, reason, usd, tokens)` | causes de blocage indépendantes et leur résolution explicite |
| `reserve(scope, usd/micro_usd, tokens, kind, role, model, transport, reservation_id, ttl/expires_at)` | réserve AVANT dépense ; lève `BudgetRefused(code)` |
| `commit(reservation_id, usd, tokens, event_key)` | consommation établie ; libère le reliquat |
| `release(reservation_id, reason)` | uniquement si l'absence de consommation est ÉTABLIE |
| `mark_unknown(reservation_id, reason)` | usage inconnu : réservation **conservée**, scope **bloqué** (strict) |
| `resolve_unknown(...)` | l'opérateur règle avec la dépense réelle (`pilot.budget.resolve_unknown_usage`) — **ne lève pas** une cause indépendante (`budget_blocks`) encore ouverte |
| `recover_expired(scope)` | réservations jamais réglées après leur échéance → `unknown` |
| `snapshot(scope)` | `consumed / reserved / unknown / balance / blocked_reason` |

**Valeurs et identités.** Un plafond (`MAX_COST_USD`, `MAX_TOKENS_BUDGET`), un montant, des tokens ou des micro-USD invalides (NaN, inf, bool, illisible, négatif, fraction de token) sont **refusés** (`ValueError`) : seul `None`/`0` signifie « pas de plafond », et une valeur invalide ne le désactive jamais. Un `reservation_id` ou un `event_key` rejoué avec un sens contradictoire (autre scope, autre montant, autre type, autre modèle ; commit rejoué avec un autre montant ; libération sous la clé d'un commit) lève `BudgetIdentityError`, sur le chemin normal **et** sur la course `IntegrityError` (SQLite et PostgreSQL, plusieurs connexions) ; une contrainte CHECK/FK violée n'est jamais prise pour un rejeu. Même règle pour les causes de blocage (`block` : scope, type, motif) et leurs résolutions (`resolve_block` : scope, cause, montants, justification) : un rejeu strictement identique reste idempotent, une clé réutilisée avec un autre sens est refusée.

**Causes de blocage.** `blocked_reason` est un agrégat : il reste posé tant qu'une réservation est `unknown` OU qu'une cause de `budget_blocks` est ouverte, et affiche la plus ancienne cause RESTANTE. Régler l'usage d'un appel ne résout donc pas une borne de transport démentie ni un historique ambigu ; chacune se résout explicitement (`resolve_block`, justification obligatoire). Sur PostgreSQL la levée verrouille le scope en `FOR NO KEY UPDATE` (un `FOR UPDATE` entrerait en deadlock avec les verrous de clé étrangère des insertions d'événements — vu et corrigé par un test concurrent réel).

Codes de refus : `cap_usd`, `cap_tokens`, `blocked_unknown_usage`, `ledger_unavailable`, `unbounded_transport`, `deadline`, `scope_missing`. `BudgetRefused` est un `BaseException` (comme `BudgetExceeded`) : il traverse les `except Exception` des chemins LLM ; le pilote le convertit en arrêt `paused_budget` à la frontière d'une tâche (la tâche repasse `todo`).

## 3. Qui décide, qui affiche

- **Décision** : `BudgetTimeController.should_continue()` lit le registre (après `recover_expired`) dès qu'un scope est attaché (`attach_project_budget`, fait par `run_project` / `run_improvement` pour tout run RÉEL). Le `MetricsCollector` et l'accumulateur coder ne décident plus.
- **Affichage** : `RunAuditLog.cost`, `run_cost_summary` et la métrique `run_cost_usd` sont des **projections du registre** (pas de cumul local → pas de double comptage). Le `MetricsCollector` reste une source de statistiques d'experts.
- **Cumul commun** : planification, BUILD (toutes les passes du merge-bot), IMPROVE, sampling de tous les rôles, retries, replis, reprises — un seul scope par projet, retrouvé après redémarrage.
- **dry-run** : aucune écriture au registre.

## 4. Transports : acceptés et refusés en mode strict sous plafond

« Strict sous plafond » = `BUDGET_MODE=strict` et au moins un plafond (`MAX_COST_USD`, `MAX_TOKENS_BUDGET`). La garantie est évaluée **par dimension plafonnée**.

| Transport | Contrôle avant émission | Statut en strict |
| --- | --- | --- |
| HTTP OpenAI-compatible du cycle (`LocalSamplingContext._create` : planner, décomposition, QA §4.7, reviewer, juge, **juge d'adéquation**) | une **réservation par tentative** ; borne haute de la requête sérialisée (§10) ; `max_tokens` obligatoire et transmis ; SDK `max_retries=0`, la boucle est la nôtre | **Accepté** pour une **identité exacte de modèle** reconnue (ou attestée) sur une **destination réelle** reconnue (hôte du client : Gemini hébergé ou API OpenAI), texte seul |
| Même transport, modèle non reconnu / destination inconnue (passerelle, `base_url` personnalisé, provider local) | — | **REFUSÉ** (`unbounded_transport`) tant que l'opérateur n'atteste pas l'identité EXACTE (`BUDGET_ATTESTED_BYTE_TOKENIZER_MODELS`). Un tarif explicite n'atteste pas un tokenizer |
| Handler de sampling du serveur MCP, **avec** un scope lié | idem ; il **injecte** `max_tokens` (4096) s'il manque ; destination = `base_url` du client | Accepté sous les mêmes conditions |
| Même handler **sans** scope lié, experts MCP réactifs (`tools/`), `autonomous/watchdog`, `core/tool_llm_manager` | garde historique `enforce_budget` (collector) ou aucune | **Hors cycle projet, hors garantie**, signalé (inventaire testé : aucun appel fournisseur direct dans `pilot`, `planner`, `executor`, `improve`, `sandbox`, `state`) |
| Sampler d'abonnement (`_sample_subscription`, Docker) | réservation avant le `docker run` ; conteneur nommé + auto-limité + `docker kill` par nom ; `max_output_tokens` demandé, `num_retries=0` | **Plafond USD seul** : accepté (0 $ établi par l'absence autoritaire de facturation). **Plafond de tokens strict : REFUSÉ** — le backend peut ignorer le plafond de sortie (non vérifiable hors ligne), aucune borne effective. `advisory` : disponible |
| Worker OpenHands SDK (`OHSdkAgent`, `"in-runner"`) avec **clé facturable** | — | **REFUSÉ** : une commande du workspace a la même clé et un réseau libre, pas de barrière effective. `advisory` : disponible, sans garantie |
| Worker OpenHands SDK avec **abonnement** | allocation réservée ; le runner contrôle chaque appel du framework (§6) | **Plafond USD seul** : accepté (0 $ établi). **Plafond de tokens strict : REFUSÉ** (mêmes raisons : sortie non garantie en amont, creds montés accessibles aux commandes du workspace). `advisory` : disponible |
| Worker historique (`OpenHandsAgent`, `"none"`) | aucun | **REFUSÉ** ; accepté en `advisory` |
| Agent **sans** attribut `budget_enforcement` | — | **REFUSÉ** : aucune garantie par défaut |
| Double déterministe qui ne dépense rien | réservation + règlement | Accepté s'il déclare **explicitement** `budget_enforcement = "test-double"` — jamais sur un agent réel ; un *manager* sans registre doit le déclarer aussi (§7) |
| Modèle sans tarif autoritaire sous plafond USD (variante attestée sans prix, identité inconnue, destination non hébergée) | — | **REFUSÉ** ; borné en tokens s'il n'y a pas de plafond USD |
| Provider déclaré local mais client sur un endpoint hébergé facturé | — | Facturé au **tarif cloud** (jamais 0), réservation et règlement à la même autorité |
| Destination **inconnue** (passerelle, hôte tiers, URL absente) même avec un provider déclaré local | — | **REFUSÉE** sous plafond USD strict tant qu'aucun prix n'est établi (`LLM_PRICE_*`) : ni tarif de grille ni gratuité présumée. Seuls sont gratuits le **loopback** (`localhost`, `127.0.0.0/8`, `::1`) avec un provider local, ou un hôte **attesté** auto-hébergé (`BUDGET_ATTESTED_FREE_HOSTS`, égalité exacte). Une attestation de tokenizer n'établit pas un tarif |
| Identité d'un fournisseur envoyée à l'endpoint d'un autre (`gpt-5.4` sur l'endpoint Google, modèle Gemini sur l'API OpenAI) | — | **REFUSÉE** sans prix établi : le tarif de la grille est lié à la famille qui sert le modèle |
| Prompt borné > 200 000 tokens sur un modèle de la grille | — | **REFUSÉ** : la grille est un tarif standard ≤ 200k, aucun palier supérieur n'est établi |
| Modalité non textuelle (image, audio, fichier, vidéo), `bytes`, sortie non bornée | — | **REFUSÉ** avant émission |

## 5. Périmètre exact de la garantie stricte

La garantie porte sur **les appels que notre code émet** : chaque tentative est réservée avant l'émission, aucun appel n'est émis si le solde, le blocage ou l'échéance l'interdit, et une configuration non bornable est refusée plutôt que prétendue. Elle s'évalue **dimension par dimension** :

- **dollars** : borne tarifaire établie (grille ≤ 200k ou prix configuré pour un modèle hors grille) + borne de tokens justifiée ; ou 0 $ établi par une absence AUTORITAIRE de facturation (abonnement, provider local, modèle gratuit de la grille) ;
- **tokens** : exige une borne de prompt justifiée ET une sortie réellement plafonnée ET, pour un worker, une barrière non contournable. Un 0 $ établi ne vaut JAMAIS garantie de tokens.

Elle **ne protège pas** d'un programme du workspace qui disposerait de la clé ou des credentials et d'un accès réseau libre : un `curl` lancé par l'agent n'est pas un appel du framework. Arbitrage retenu, sans nouvelle infrastructure obligatoire : clé facturable accessible au workspace ⇒ worker **refusé** en strict ; abonnement ⇒ accepté pour un plafond USD seul, **refusé** sous un plafond de tokens. Une vraie barrière (clé détenue par l'hôte derrière un proxy de comptage, réseau du conteneur restreint à ce proxy) reste une décision d'architecture / un raccordement Docker : non faite ici. Le mode `advisory` conserve toutes ces possibilités, nommé et sans garantie.

## 6. Worker : allocation, échéance, décès du client

- `allocate_worker` : réserve `BUDGET_WORKER_SHARE` (80 %) du solde (USD et tokens), plafonné par `BUDGET_WORKER_MAX_*`, plancher d'allocation utile `BUDGET_WORKER_MIN_USD/TOKENS` (sinon pause budget : sans plancher les allocations décroissent géométriquement sans jamais atteindre zéro). L'échéance est la plus proche entre l'échéance du run et le timeout sandbox.
- Le runner reçoit `--budget-usd --budget-tokens --deadline-epoch --prices '{modèle:[in,out]}' [--strict] [--byte-bounded-models …] [--no-billing]`. La table de tarifs couvre **tout** la chaîne (principal + replis, `OHSdkAgent.model_chain()`) : un repli est tarifé à son propre prix, un modèle sans tarif sous plafond USD ou d'identité inconnue (hors liste exacte / attestation) est **écarté** (le suivant est tenté) ; le principal non bornable fait refuser le lancement (code 3).
- Capacité de l'agent (`worker_budget` en-tête) : absente ⇒ refus ; `none` ⇒ refus ; `test-double` ⇒ accepté ; `in-runner` ⇒ refusé avec une clé facturable, et avec l'abonnement refusé sous un plafond de **tokens** strict (plafond USD seul : accepté). Les réglages `BUDGET_WORKER_*` et la durée sont validés strictement (NaN/inf/bool/négatif/illisible ⇒ refus `unbounded_transport`, jamais une valeur corrigée).
- **Échéance pendant la tâche** : le runner refuse tout appel passé l'échéance, et un **chien de garde** (thread) l'applique PENDANT un appel en vol : marqueur `unknown`, arrêt immédiat (code 5), usage inconnu ; le conteneur est enveloppé par `timeout --signal=TERM --kill-after=15 N` : **il s'arrête seul si le client `docker` ou le process hôte meurt**. Le runner traite `SIGTERM` en sortie propre pour vider l'usage. Au dépassement hôte, `docker kill` par nom.
- **Échecs** : seuls un rejet HTTP prouvé (400/401/403/404/405/413/415/422/429, retry seulement 429) et un échec de connexion avant l'envoi sont « sans consommation ». Tout autre échec (5xx, 408, 409, timeout, annulation) ⇒ marqueur `unknown`, **ni retry ni repli**, arrêt du worker. Une réponse sans usage compté par le SDK ou une borne démentie ⇒ `unknown` aussi.
- **Règlement** : marqueur `unknown` ⇒ usage inconnu **même si `final` est présent** (`final` prouve que les deltas ont été vidés, pas que les compteurs ont tout vu). Marqueur `final` seul → usage complet → `commit` + reliquat libéré. Armé sans `final` (conteneur tué), timeout hôte, ou coût indéterminé → `mark_unknown` (réservation conservée, suite stricte bloquée). Runner mort **avant** d'armer, sans aucun usage (crash d'import #498) → zéro **établi**, pas de blocage. Worker jamais lancé (Docker absent, montage refusé) → `release`.

## 7. Planification : identité de cycle, reprise, création atomique

`plan_project_from_settings(…, cycle_id=None)` donne au cycle une **identité durable** (`planning_cycle_key`) :

- `cycle_id` explicite (CLI : `plan draft --cycle-id ID`) → `planning:cycle:<ID>` : la consigne peut changer entre deux reprises sans renouveler l'enveloppe ;
- sinon, identité dérivée de `(owner, repo, name, problem)` → `planning:auto:<sha256>` : relancer la MÊME commande après un échec **reprend le même solde** ; des projets distincts ont des enveloppes distinctes (aucun budget global unique).

**Une nouvelle enveloppe est un nouveau cycle explicite** (un autre `cycle_id`) ; le changement de plafond de la configuration, lui, s'applique au cycle existant (`_open` met les plafonds à jour).

Déroulement et reprise (le cycle porte un **droit exclusif** — jeton + échéance 2 h en compare-and-set ; un second appelant reçoit `PlanningCycleError(busy)` **sans rien émettre** ; un droit échu d'un process mort est repris) :

1. SPEC générée (dépense réservée au scope du cycle, encore sans projet) ;
2. **le projet est créé ET lié au scope dans UNE transaction** (`ProjectStateManager.create_project_in_cycle`, via `persist_spec(…, cycle=…)`) : un arrêt avant le commit ne laisse ni projet sans budget ni scope lié à un projet fantôme, un droit perdu ou échu annule la création. La dépense de SPEC reste au scope ; la reprise ne régénère pas un projet — elle régénère la SPEC (payée de nouveau, DANS la même enveloppe) ;
3. décomposition puis tests d'acceptation §4.7. Une reprise après SPEC persistée (**projet sans tâches**, ou tâches sans tests d'acceptation alors qu'ils sont exigés) **réutilise la SPEC et le projet** et reprend à l'étape inachevée : ni second projet, ni SPEC régénérée ;
4. un draft complet (tâches, et tests d'acceptation si exigés) est un cycle **abouti** : `PlanningCycleError(project_id=…)` — relire/approuver ce draft, ou ouvrir un autre `cycle_id`.

La suite (BUILD, IMPROVE) retrouve le même scope par `project_id`. **Procédure de reprise réelle** : relancer la même commande (`plan draft …`, éventuellement avec le même `--cycle-id`) ; si un process est mort en tenant le cycle, attendre l'échéance du droit (2 h) ou, après vérification qu'aucun process ne planifie, laisser le droit expirer.

**Registre absent (F5).** Un run réel (planification, BUILD, IMPROVE) en mode strict avec un manager qui n'expose pas de registre est **refusé** (`ledger_unavailable`) — jamais de repli silencieux sur un compteur historique. Exceptions explicites : `BUDGET_MODE=advisory` (nommé, sans garantie) ou un manager qui déclare `budget_enforcement = "test-double"`.

## 8. Migration 0011 et import unique

Additive. Les métriques `run_cost_usd` / `run_tokens` sont des **snapshots cumulatifs ordonnés par `id`** : on retient le **dernier cumul** (jamais la somme des cumuls). Quand la série est croissante, ce dernier cumul est aussi son maximum. Une série **décroissante** ou contenant une valeur **invalide** (NaN, inf, négative) est **ambiguë** : elle ne prouve pas la dépense passée. L'import retient alors la **borne au maximum des valeurs valides OBSERVÉES** et ouvre une cause durable `ambiguous_history` (une par métrique) qui **bloque le strict** jusqu'à `resolve_block` — l'opérateur peut y ajouter la dépense établie (relevé fournisseur). Cette borne n'est **pas** une borne haute de toute la dépense passée (elle ne couvre que ce que les snapshots ont montré) et n'est jamais présentée comme la dépense totale établie ; l'ambiguïté n'est jamais « résolue » par l'import. Sans plafond configuré la cause est enregistrée et devient bloquante dès qu'un plafond apparaît. La migration (SQL) et l'import paresseux (`create_all`) ont la même sémantique, vérifiée sur SQLite et PostgreSQL (huit scénarios : croissant, décroissant, décroissant sur une seule métrique, valeur invalide, négative, uniquement invalide, zéro, sans historique), y compris le rejeu après `downgrade` + `upgrade`. Les unicités (`scope_key`, `project_id`, `reservation_id`, `event_key`, `block_key`) rendent l'import exactement-une-fois.

## 9. Tests et commandes

- Registre : `tests/test_budget_ledger.py` (SQLite, concurrence 2–12 connexions).
- **PostgreSQL réel** : `tests/test_budget_ledger_postgres.py` — lance un cluster jetable (`initdb` + `pg_ctl`, socket unix, utilisateur courant, aucun secret) ou utilise `COLLEGUE_TEST_POSTGRES_URL` (service PostgreSQL de la CI). Concurrence multi-connexions, rejeu, blocage, contraintes, migration + import. Ne se saute pas : sans PostgreSQL ni URL il **échoue** avec la marche à suivre ; initdb refuse de tourner en root → fournir l'URL.
  `pytest tests/test_budget_ledger_postgres.py`
- Cycle de planification (identité, reprise, création atomique, concurrence, F5) : `tests/test_budget_planning_cycle.py` ; juge d'adéquation (factory réelle) + inventaire des appels directs : `tests/test_budget_adequacy.py`.
- Transports : `tests/test_budget_transport.py` ; garde du runner : `tests/test_oh_runner_budget.py` ; sandbox auto-limité : `tests/test_sandbox.py` ; acceptation pilote (BUILD ×2, BUILD→IMPROVE, redémarrage, crash, échéance, transport non bornable, planification) : `tests/test_budget_pilot.py` ; défauts de l'audit sur API préexistantes : `tests/test_budget_baseline_behavior.py` (rouges sur la base, verts ici).

## 10. Hypothèses de tarification et de bornes

1. **Autorité tarifaire = destination réelle du transport**, la même pour la réservation et le règlement (`pricing_family`) : famille déduite de l'hôte du client (`api.openai.com` → openai, `generativelanguage.googleapis.com` → gemini), sinon du routage de la config (`LLM_PROVIDER` hébergé, ou `llm_base_url` pour un provider local). `LLM_PROVIDER=lmstudio` avec un client qui parle à l'API OpenAI est facturé au **tarif cloud**, jamais à 0. **Gratuit établi** = provider local déclaré ET hôte de loopback, ou hôte attesté (`BUDGET_ATTESTED_FREE_HOSTS`) ; un hôte inconnu n'est ni facturé à un tarif de grille ni gratuit (le label de provider seul ne prouve rien), sans résolution DNS ni réseau. Les endpoints auto-hébergés hors loopback relèvent de l'attestation explicite, de `advisory`, ou d'un prix configuré ; le routage complet par rôle reste W4.
2. **Tarif de grille par identité exacte ET famille** (`monitoring/pricing.strict_grid_price`) : clé exacte ou instantané daté, servie par la famille qui SERT ce modèle (`grid_family` : gemini/gemma → gemini, gpt/o-series → openai) ; `gpt-5.4` envoyé à l'endpoint Gemini n'a pas le tarif OpenAI. Un zéro (Gemma gratuit) n'est valable que pour sa famille. Un préfixe de nom (`gpt-5.4-expensive-variant`) n'hérite **jamais** du tarif de `gpt-5.4` (la correspondance par préfixe du dashboard reste, nommée estimation, hors garantie). Hors grille : `LLM_PRICE_*_PER_1M` (l'opérateur en répond), sinon **aucun tarif ⇒ refus sous plafond USD**. Une attestation de tokenizer n'est PAS une attestation de facturation. Au-delà de 200 000 tokens de prompt un modèle de la grille est refusé (tarif standard ≤ 200k, aucun palier supérieur) ; les `LLM_PRICE_*` ne remplacent pas la grille d'un modèle qu'elle connaît. Chaque modèle (principal, repli — pour un worker, selon le fournisseur réel de la chaîne —, modèle changé) est tarifé à **son** prix. Gemma 4 gratuit sur son endpoint, abonnement (`billable=false`) : 0 $, avec tokens comptés (sans garantie de plafond, §4) ; un transport effectivement gratuit n'exige de borne de tokens que sous un plafond de tokens.
3. **Borne du prompt = octets UTF-8 de la requête SÉRIALISÉE** (messages, système, outils, schémas, avec guillemets, virgules, deux-points et accolades ; séparateurs les plus larges) + cadrage fixe (16/message, 64/outil, 64/requête). Justification : un tokenizer à repli octet (BPE byte-level, SentencePiece à byte-fallback) ne produit jamais plus de tokens que d'octets, et l'échappement JSON ne fait que rallonger le texte réel. Ce n'est PAS une moyenne « chars/N ».
4. **Quand cette hypothèse est admise** : pour une **identité exacte** listée dans `HOSTED_KNOWN_MODELS` (Gemini/Gemma sur l'endpoint hébergé Google, GPT/o-series sur l'API OpenAI ; instantané daté `-AAAA-MM-JJ`/`-AAAAMMJJ` admis) servie par une **destination réellement utilisée par le client** (hôte `generativelanguage.googleapis.com` ou `api.openai.com`) ; ou pour une identité **exactement** attestée par l'opérateur. Un préfixe de nom (`gpt-…`, `gemini…`) n'est pas une preuve, un tarif explicite non plus, ni un `LLM_PROVIDER` sans rapport avec le client. La liste est une hypothèse documentée sur les tokenizers, **non vérifiée contre l'API** (aucun appel réel autorisé) ; le routage complet par rôle est W4.
5. **Sortie** : `max_tokens` doit être un entier > 0 réellement transmis (HTTP : vérifié par test ; handler MCP : injecté ; runner : `max_output_tokens` du LLM obligatoire). Hypothèse : les tokens de raisonnement comptent dans ce plafond. Les compteurs réels sont comparés à la borne **après** l'appel : si le fournisseur la dément, la consommation réelle est engagée en entier et le scope est **bloqué** par une cause `bound_violation` (détection, pas prévention).
6. Cache, audio : non distingués ; le tarif standard est supposé être une borne haute (audio : refusé).
7. Une réponse HTTP d'erreur **ne prouve pas** l'absence de facturation. Release uniquement pour un rejet prouvé (400/401/403/404/405/413/415/422/429) ou un échec de connexion avant l'envoi ; le reste ⇒ **usage inconnu**, réservation conservée, **aucun retry** en strict (HTTP `guarded_call` comme worker `BudgetGuard`). `advisory` garde le comportement historique du SDK.
8. **Délai par appel et usage (Python 3.11 / 3.12).** `sample_with_timeout` applique `LLM_CALL_TIMEOUT` avec `asyncio.timeout` dans la tâche de l'appelant, pas `asyncio.wait_for` : sous 3.11 `wait_for` exécute la coroutine dans une tâche au contexte copié, l'usage écrit dans la ContextVar de `sampling_usage` y restait enfermé et `accounted_sample` croyait l'usage absent (`UsageAccountingError` sur un cycle sain). La vérification d'usage reste active (une absence réelle n'est jamais lue comme zéro) et le délai annule toujours l'appel ; capture imbriquée, appels parallèles, agrégation de plusieurs émissions, erreur ou annulation externe après réception de l'usage : testés sous les deux versions (`tests/test_llm_timeout_usage.py`). Le délai de `guarded_call` (échéance du run) garde `wait_for` : il n'écrit aucune ContextVar d'usage dans la coroutine enveloppée.
9. **Échéance** : appliquée aussi PENDANT un appel en vol (`asyncio.wait_for` sur le temps restant, annulation, usage inconnu, `BudgetRefused(deadline)` ⇒ arrêt `deadline_reached`) et avant un backoff qui la franchirait.

## 11. Opérateur

`pilot.budget.budget_status(manager, pid)` donne le scope, le motif de blocage durable, les réservations inconnues et les **causes de blocage ouvertes** (`blocks`) ; `resolve_unknown_usage(manager, pid, reservation_id, usd=…, tokens=…)` règle un appel avec la dépense réelle ; `resolve_budget_block(manager, pid, block_key, note=…, usd=…, tokens=…)` résout EXPLICITEMENT une cause indépendante (justification obligatoire ; `usd`/`tokens` = dépense passée établie, utile pour un historique ambigu). Le scope ne se débloque que lorsqu'il ne reste aucune cause. Seul `plan draft --cycle-id` est exposé en CLI (`pilot/__main__.py`) ; la résolution des blocages reste une API.

## 12. Configuration

`BUDGET_MODE=strict|advisory` (défaut strict), `BUDGET_WORKER_SHARE`, `BUDGET_WORKER_MAX_USD/TOKENS`, `BUDGET_WORKER_MIN_USD/TOKENS`, `BUDGET_ATTESTED_FREE_HOSTS` (CSV de noms d'hôtes exacts attestés auto-hébergés et non facturés ; le loopback est déjà réputé local avec un provider local), `BUDGET_ATTESTED_BYTE_TOKENIZER_MODELS` (CSV d'identités **exactes** de modèles dont l'opérateur atteste « tokens ≤ octets UTF-8 » ; pas de préfixe), et les existants `MAX_COST_USD`, `MAX_TOKENS_BUDGET`, `BUDGET_EXHAUSTED_ACTION` (`warn` ⇒ non strict). Le blocage par usage inconnu ne s'applique que si un plafond est configuré (sans plafond il n'y a rien à protéger ; l'inconnu reste tracé).

## 13. Raccordements pour B / C

- `alembic/versions/0011_budget_ledger.py` (crée aussi `budget_blocks` et les colonnes `claim_*`, avec le même import legacy que `create_all`) : à déplacer mécaniquement dans le package avec les révisions existantes (B ne le modifie pas).
- `collegue/executor/oh_runner.py` est copié seul dans l'image : **il n'importe que la stdlib** (aucun nouveau module à embarquer) ; les marqueurs de sortie sont `[collegue-budget] armed|final|unknown|deadline` (son registre d'identités de modèles est une copie de `HOSTED_KNOWN_MODELS`, vérifiée égale par un test).
- CI : `tests/test_budget_ledger_postgres.py` tourne dans le job `pytest` existant (les images `ubuntu-latest` fournissent PostgreSQL sous `/usr/lib/postgresql/*/bin`) ; pour être explicite, B peut ajouter un `services: postgres` et exporter `COLLEGUE_TEST_POSTGRES_URL`. **Ne pas utiliser le workflow nightly** (appels LLM réels).
- Dockerfile sandbox : `coreutils timeout` est requis dans l'image (présent dans `python:3.12-slim`).

## 14. Limites

Voir `reports/w2-a.md`, `reports/w2-a-manager-review.md` et `reports/w2-a-atomicity-review.md`. En résumé :

- tokenizers et plafond de sortie : hypothèses documentées, **non vérifiées contre l'API** (aucun appel réel) ; une borne démentie est détectée **après** coup (consommation engagée, scope bloqué) ;
- worker : pas de barrière non contournable contre une commande du workspace (clé ou credentials montés) ⇒ refus en strict pour une clé facturable ou un plafond de tokens ;
- historique legacy ambigu : borne des dépenses OBSERVÉES, jamais la dépense totale établie ; blocage jusqu'à résolution explicite ;
- droit exclusif d'un cycle : échéance fixe de 2 h, sans battement de cœur ; un process vivant plus long perdrait son droit (la création du projet serait alors refusée, jamais dupliquée) ;
- le routage complet des endpoints par rôle reste W4 ; seuls les destinations hébergées reconnues sont admises sans attestation.
