# Vague 4 (B) — preuve métier multi-tâches, reprise, rollback et campagne réelle ponctuelle

Complète la preuve du seul endpoint `/nightly` (smoke mono-tâche, `pilot/nightly_e2e.py`, inchangé) par une application de
référence **FastAPI + SQLite + Alembic** construite en **trois tâches dépendantes** par les entrées publiques du produit. Deux
volets : une campagne **déterministe** (sans modèle, sans facture, exécutée par la CI) et l'**invocation réelle ponctuelle**
(préparée, jamais lancée par ce lot). Code : `collegue/pilot/w4_business.py`, `tests/w4_business_{fixture,campaign}.py`,
`.github/workflows/consolidation-e2e.yml`.

## 1. Application de référence

Graine : les huit fichiers de `VynoDePal/collegue-e2e-fixture` (ID 1298596453, commit
`8e3691d8e4f311e00d620c9c2ca2d9edbd8b136a`), **octet pour octet** (`tests/w4_business_fixture.SEED`).

| Tâche | Livrable de référence | Oracle scellé (rouge par assertion avant, vert après) |
| --- | --- | --- |
| 1 — persistance et migration | `alembic.ini`, `migrations/env.py`, révision `0001` (`audits`, `findings`), `app/db.py` (`DATABASE_URL`) | `alembic upgrade head` sur une base **réellement vierge** : code retour 0, tables, `alembic_version == 0001`, colonnes |
| 2 — création puis lecture d'un audit | `POST /audits` (201), `GET /audits/{id}` (200/404), `app/repository.py` | migration de la tâche 1 **intégrée dans la base**, création, relecture identique, 404, lignes persistées, relecture après « redémarrage » |
| 3 — export PDF | `GET /audits/{id}/export.pdf`, `app/export.py`, `docs/export_header.md` | PDF 200 `application/pdf` dont le **texte extrait par un vrai lecteur (`pypdf`)** contient titre, auditeur, chaque constat et « n° <id> » |

Le PDF est écrit sans bibliothèque (une page, police standard, **flux compressé**) : il n'est pas recherchable par octets (testé) ;
seul un lecteur en extrait le texte. Les oracles utilisent `TestClient(raise_server_exceptions=False)` et testent le **code
retour** des sous-processus : un rouge est toujours une `AssertionError`, jamais une erreur de collecte, d'import ou de migration
(`collection_errors == errors == skipped == 0` est asserté dans la preuve de livraison).

Témoins : `WRONG_DATA_STAGE_3` (PDF **valide**, 200, MIME correct, mais les données d'un autre audit ; ses propres tests « 200 + MIME +
signature » passent — seul l'oracle lit le texte) et `BROKEN_NOTICE_HEADER` (changement de **documentation** qui retire la mention
légale « CONFIDENTIEL » : invisible des oracles de données, visible de la sonde de santé).

## 2. Scénario déterministe (commande et étapes)

```
PYTHONPATH=.:tests python tests/w4_business_campaign.py --output rapport.json --human rapport.txt
python -m pytest tests/test_w4_business_campaign.py tests/test_w4_business_fixture.py tests/test_w4_business_report.py \
       tests/test_w4_business_launch.py tests/test_w4_business_workflow.py          # dépendance de test : pypdf
```

| Étape | Ce qui est prouvé (entrée publique) |
| --- | --- |
| D01 plan | `plan_project_from_settings` : SPEC + décomposition + oracles QA, transport simulé **réservé/réglé dans le registre W2** par la vraie `guarded_call` ; 3 tâches dépendantes ; `acceptance_tests_required` ; empreintes = sources scellées |
| D02 approve | `approve_project_plan_from_settings` sur le hash relu |
| D03 tâche 1 | `run_project_from_settings` (nouveau gestionnaire = redémarrage) : agent, gate réel (tests du projet exécutés), oracles rejoués, preuve de livraison relue, fusion W3 au SHA exact, resync réelle ; **une seule fusion** |
| D04 resync interrompue | tâche 2 fusionnée à distance, `origin` rendue injoignable : arrêt `merge_sync_pending`, cycle `merged_unsynced`, tâche NON comptée prête, **aucune tâche suivante** |
| D05 redémarrage | reprise de la synchronisation sans seconde fusion (un seul `PUT` pour la PR 102), puis tâche 3 depuis une base qui contient les tâches 1 ET 2 ; contrats livrés rejoués |
| D06 métier | base vierge → migration → audit créé/relu/relu après redémarrage → PDF lu par `pypdf` |
| D07 amélioration | `run_improvement` + hooks Phase 5 de production : amélioration RÉELLE (lint, `ruff format`), mesure réelle (`measure` + couverture), preuve IMPROVE (3 contrats livrés rejoués) ; la politique de faible risque **refuse** la fusion automatique (`app/db.py` sensible) |
| D08 merge opérateur | **SIMULÉ** (merge humain hors moteur) + resync ; identifié comme tel dans le rapport |
| D09 sans régression | tests verts, couverture non inférieure, lint 28 → 8, composite en hausse, comportement métier inchangé |
| D10 incident + rollback | amélioration de **documentation** autorisée (faible risque) qui retire la mention légale : fusion Phase 5 au SHA exact, **santé rouge RÉELLE** (la sonde métier observe le PDF sans mention), revert distant (PR + fusion sous le contrôle des checks requis), resync, santé verte ; tree restauré **identique**, commit de revert distinct, comportement restauré, incident `recovered` |
| D11 acquittement | `acknowledge_phase5_incident` ; le hook de reprise rend « rien à reprendre » |
| D12 registre | enveloppe 2 USD / 250000 tokens / 900 s portée par le registre ; rien de réservé ni d'inconnu ; la tâche 2 n'est comptée qu'UNE fois malgré l'interruption |

Variantes : `run_negative_witnesses` (le PDF aux mauvaises données n'est jamais livré, `main` intact, registre cohérent) et
`run_budget_stop_campaign` (plafond de tokens atteint : worker de la tâche 3 refusé AVANT lancement, deux redémarrages sans
dépense, point d'arrêt `B03`).

### Frontières simulées (et seulement elles)

Transport de sampling (réponses scriptées, règlement réel), agent codeur (écrit les fichiers de référence, usage rapporté réglé par
`settle_worker`), GitHub (vrai dépôt Git derrière les vrais clients, pont W3 étendu : `POST git/commits`, suppression de branche,
suppression de la branche de tête après fusion), revue (`FakeReviewer`), et **la mesure du tour d'incident** (`ClaimedGain`
annonce 3 violations de lint de moins : une modification de documentation n'en produit aucune ; la régression, la santé rouge et le
rollback, eux, sont réels). Les oracles, le gate, la preuve, la politique de fusion, la resynchronisation, la garde de santé, le
revert distant et le registre sont ceux de production. Aucune preuve verte, aucun `verify_fn`, aucun `proof_loader` n'est injecté.

## 3. Rapport machine et humain

`CampaignReport` : cinq états d'étape — `succeeded`, `not_executed`, `budget_stop`, `failed`, `incomplete_validation` — déclarées
d'avance (une étape non jouée reste `not_executed`, jamais lue comme réussie). Verdict `validated` seulement si **toutes** les
étapes requises ont réussi ; sinon `failed` (1) > `budget_stop` (4) > `incomplete_validation` (3). Le rapport conserve : SHA et
arbres, empreintes d'oracles, preuves (rouge/vert), résultats métier, compteurs du registre à chaque étape et le **point d'arrêt**.
Les secrets connus sont masqués à la sérialisation. Schéma `w4-business-report/1`.

## 4. Campagne réelle ponctuelle

Enveloppe GLOBALE : **2 USD, 250000 tokens, 900 s**, portée par le registre durable W2 propre à la campagne
(`STATE_DATABASE_URL` sous `COLLEGUE_HOME`, créé vierge par le workflow ; le scope est rouvert par `scope_for_project` et le
préflight vérifie que ses plafonds ne dépassent pas l'enveloppe). Aucun retry payant de campagne : une exécution du produit,
`TASK_MAX_ATTEMPTS=1`, tentative de workflow > 1 refusée ; les retries internes des transports consomment la même enveloppe.
L'échéance est absolue (`started_at` persisté) et les commandes sont lancées sous `bounded_command_runner` (groupe de processus tué
à l'échéance) ; la vérification du livrable s'exécute dans un conteneur **nommé**, durci, sans réseau ni secret, tué par son nom au
délai (`run_in_named_container`).

### Préflight (aucune clé de modèle, aucun appel facturable)

| Contrôle | Refus (→ `incomplete_validation`, code 3) |
| --- | --- |
| P01 contexte | autre déclencheur que `workflow_dispatch`, tentative ≠ 1, confirmation inexacte, `INTEGRATION_E2E_ENABLED` actif |
| P02 environnement | tout écart avec l'enveloppe (plafond, mode strict, `TASK_MAX_ATTEMPTS`, `STRICT_MAX_INFLIGHT_PRS`, registre non absolu…) |
| P03 secrets | une clé de modèle présente dans l'étape de préflight (échec) |
| P04 identité | dépôt, ID, branche par défaut, sentinelle, tête de la graine (lectures seules) |
| P05 capacité | **aucun** transport de worker ne tient simultanément USD + tokens + échéance en strict (règles de `allocate_worker`) |
| P06 protections | `discover_server_policy` (W3) sur `collegue-business/<run>` : checks requis, règle « à jour » applicable à l'acteur, pas de bypass |
| P07 oracles | image du sandbox sans la pile (fastapi, httpx, sqlalchemy, alembic, **pypdf**) : un import absent n'est pas un rouge valide |

Résultat attendu AUJOURD'HUI (prouvé, `evidence/w4-b-*`) : P05 refuse — `OHSdkAgent` avec clé facturable (barrière in-runner non
effective), `OHSdkAgent` en abonnement (plafond de tokens non garanti), `OpenHandsAgent` (aucun contrôle) — et P06 refuse sur le
dépôt réel (ruleset 18840666 : branche par défaut seulement, aucun check requis). La campagne réelle se termine donc
`incomplete_validation` **avant toute émission** ; ce n'est pas une preuve du parcours avec modèles réels.

### Politique de fusion de la base éphémère

Le BUILD fusionne chaque tâche dans `collegue-business/<run>` avec `BUILD_AUTO_MERGE=true`, `STRICT_MAX_INFLIGHT_PRS=1`, sous les
contrôles W3 inchangés : preuve de livraison de la tête exacte, checks requis verts, sommet de base == base prouvée, précondition
serveur « à jour » applicable au vrai acteur, resynchronisation prouvée avant la tâche suivante. Rien n'est désactivé ni
contourné. Prérequis (propriétaire du dépôt fixture, hors périmètre de B) : un ruleset sur `collegue-business/**` avec règle de
checks requis stricte non contournable pour l'acteur, des checks produits par une application identifiable sur chaque PR, et un
jeton utilisateur. Sans cela P06 refuse. Aucune protection n'est modifiée par ce lot.

### Invocation (non lancée)

`workflow_dispatch` du workflow `consolidation-e2e.yml` avec `confirm = LANCER-UNE-FOIS-2USD-250000TOKENS-900S` et un
`campaign_id` (`^[a-z0-9][a-z0-9-]{2,39}$`). Étapes : registre vierge migré → préflight statique (jeton de lecture seulement) →
image du sandbox → préflight complet → campagne (seule étape qui reçoit la clé du fournisseur) → nettoyage idempotent
(`always()`) → rapport (artefact, sans secret). Le workflow du nightly n'est pas modifié (`INTEGRATION_E2E_ENABLED` reste
désactivé) et n'est pas utilisé.

## 5. Garanties et limites

* La campagne déterministe prouve la logique produit (planification, preuves, oracles, fusions, reprise, Phase 5, registre) sur un
  dépôt Git réel derrière une API GitHub simulée ; elle ne prouve ni la qualité d'un modèle réel, ni les protections d'un GitHub
  réel, ni le sandbox Docker/OpenHands (non utilisé localement).
* `launch_campaign` / `verify_in_container` ne sont exercés que par des doubles (ordre, nettoyage, arrêts, durcissement) : leur
  déroulé contre le GitHub réel n'est pas prouvé.
* Constats F1–F3 (IMPROVE après BUILD, collision de branches, `.coverage`) : `reports/w4-b-interface.md`.
* L'écriture d'un PDF « maison » de référence est un choix de test (aucune dépendance de rendu) ; une vraie bibliothèque produirait
  un PDF différent mais lu par le même lecteur.
