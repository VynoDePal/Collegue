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
       tests/test_w4_business_launch.py tests/test_w4_business_cli.py tests/test_w4_business_verifier_isolation.py \
       tests/test_w4_business_branch_identity.py tests/test_w4_business_contract_handoff.py \
       tests/test_w4_business_workflow.py                                            # dépendance de test : pypdf
```

| Étape | Ce qui est prouvé (entrée publique) |
| --- | --- |
| D01 plan | `plan_project_from_settings` : SPEC + décomposition + oracles QA, transport simulé **réservé/réglé dans le registre W2** par la vraie `guarded_call` ; 3 tâches dépendantes ; `acceptance_tests_required` ; empreintes = sources scellées |
| D02 approve | `approve_project_plan_from_settings` sur le hash relu |
| D03 tâche 1 | `run_project_from_settings` (nouveau gestionnaire = redémarrage) : agent, gate réel (tests du projet exécutés), oracles rejoués, preuve de livraison relue, fusion W3 au SHA exact, resync réelle ; **une seule fusion** |
| D04 resync interrompue | tâche 2 fusionnée à distance, `origin` rendue injoignable : arrêt `merge_sync_pending`, cycle `merged_unsynced`, tâche NON comptée prête, **aucune tâche suivante** |
| D05 redémarrage | reprise de la synchronisation sans seconde fusion (un seul `PUT` pour la PR 102), puis tâche 3 depuis une base qui contient les tâches 1 ET 2 ; contrats livrés rejoués |
| D06 métier | base vierge → migration → audit créé/relu/relu après redémarrage → PDF lu par `pypdf` |
| D07 amélioration | **`run_project_from_settings(improve=True)`** (handoff BUILD → IMPROVE : statut de cycle `improving`, empreinte approuvée inchangée, aucune ré-approbation) : amélioration RÉELLE (lint, `ruff format`), mesure réelle (`measure` + couverture), preuve IMPROVE (3 contrats livrés rejoués), publiée sur `collegue/improve-r1-<empreinte>` ; les têtes BUILD `collegue/issue-1..3` **conservées** (le dépôt réel a `delete_branch_on_merge=false`) restent intactes ; la politique de faible risque **refuse** la fusion automatique (code `.py`) |
| D08 merge opérateur | **SIMULÉ** (merge humain hors moteur) + resync ; identifié comme tel dans le rapport |
| D09 sans régression | tests verts, couverture non inférieure, lint 28 → 8, composite en hausse, comportement métier inchangé |
| D10 incident + rollback | amélioration de **documentation** autorisée (faible risque) : elle retire des identifiants d'exemple de `docs/deploiement.md` (**gain MESURÉ par le vrai scan de secrets**, avant/après consignés, aucun ajustement) et supprime la mention légale de `docs/export_header.md` : fusion Phase 5 au SHA exact, **santé rouge RÉELLE** (la sonde métier observe le PDF sans mention), revert distant (PR + fusion sous le contrôle des checks requis), resync, santé verte ; tree restauré **identique**, commit de revert distinct, comportement restauré, incident `recovered` |
| D11 acquittement | `acknowledge_phase5_incident` ; le hook de reprise rend « rien à reprendre » |
| D12 registre | enveloppe 2 USD / 250000 tokens / 900 s portée par le registre ; rien de réservé ni d'inconnu ; la tâche 2 n'est comptée qu'UNE fois malgré l'interruption |

Variantes : `run_negative_witnesses` (le PDF aux mauvaises données n'est jamais livré, `main` intact, registre cohérent) et
`run_budget_stop_campaign` (plafond de tokens atteint : worker de la tâche 3 refusé AVANT lancement, deux redémarrages sans
dépense, point d'arrêt `B03`).

### Frontières simulées (et seulement elles)

Transport de sampling (réponses scriptées, règlement réel), agent codeur (écrit les fichiers de référence, usage rapporté réglé par
`settle_worker`), GitHub (vrai dépôt Git derrière les vrais clients, pont W3 étendu : `POST git/commits`, suppression de branche,
suppression de la branche de tête après fusion, **désactivée par défaut** comme sur le dépôt fixture réel) et revue
(`FakeReviewer`). La mesure est le VRAI `measure` partout (le gain fictif `ClaimedGain` a été supprimé). Les oracles, le gate, la preuve, la politique de fusion, la resynchronisation, la garde de santé, le
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
| P03 secrets | étapes `static`/`full` : une clé de modèle présente (échec). Étape `launch` (celle de `run`) : la clé du transport choisi est LÉGITIME ; seuls ses noms sont consignés, les valeurs sont masquées (clés par rôle comprises) |
| P04 identité | dépôt, ID, branche par défaut, sentinelle, tête de la graine (lectures seules) |
| P05 routes | destination EFFECTIVE de planificateur, QA, relecteur et codeur par l'API publique `validate_role_routes` du lot A (importée à l'appel ; absente = validation incomplète) ; credential exigé seulement à l'étape `launch` |
| P06 capacité | le worker RÉELLEMENT sélectionné (`OHSdkAgent` bâti sur un sandbox sentinelle qui refuse toute exécution, réglages effectifs) est jugé par `allocate_worker` sur un registre jetable strict aux plafonds de la campagne ; la matrice générale reste informative et ne décide jamais |
| P07 protections | `discover_server_policy` (W3) sur `collegue-business/<run>` : checks requis, règle « à jour » applicable à l'acteur, pas de bypass |
| P08 oracles | l'image que le gate exécute réellement (`SANDBOX_IMAGE`, comme le codeur) sans la pile (fastapi, httpx, sqlalchemy, alembic, **pypdf**) : un import absent n'est pas un rouge valide ; `docker run --pull never`, aucun téléchargement |

**Étapes.** `preflight --stage static` (sans clé, sans image : P08 facultative et non jouée), `preflight --stage full` (sans clé, image
incluse), `run` = étape `launch` : validation effective juste avant l'émission (la clé est présente, jamais affichée) ; `run --stage static`
est **refusé** (l'image du gate ne peut pas être évitée).

Résultat attendu AUJOURD'HUI : P06 refuse pour les deux configurations de worker réalistes — `OHSdkAgent` avec clé facturable
(barrière in-runner non effective), `OHSdkAgent` en abonnement (plafond de tokens non garanti) ; P07 refuse sur le dépôt réel
(ruleset 18840666 : branche par défaut seulement, aucun check requis). Un refus W2 légitime reste une validation réelle incomplète,
**zéro appel émis**. La campagne réelle se termine donc `incomplete_validation` **avant toute émission** ; ce n'est pas une preuve
du parcours avec modèles réels.

### Portée annoncée et point d'arrêt documenté

Le rapport réel déclare TOUTES les preuves de sa portée : BUILD (R01), métier (R02), registre (R03), **amélioration (R04)** et
**incident / rollback (R05)**. L'invocation réelle ne câble que R01–R03 ; R04 se termine `incomplete_validation` avec le point
d'arrêt exact et R05 reste `not_executed` : le verdict ne peut pas être `validated` tant que ces preuves ne sont pas jouées (un BUILD
réussi n'est pas la validation finale ; la preuve déterministe est dans `tests/w4_business_campaign.py`). Aucun proxy, aucune garde
budgétaire levée. Après un arrêt budget / échéance / erreur, l'identité du projet (consignée dès sa création dans
`facts.launch`) sert à lire le MÊME registre ; une lecture impossible garde l'arrêt d'origine et déclare la preuve manquante.

### Vérificateur métier (code généré = non fiable)

`verify_business_checkout` ne s'exécute **jamais sur l'hôte par défaut** : conteneur Docker durci (`--network none`, racine en lecture
seule, `--cap-drop ALL`, `--pull never`, UID de l'appelant, nommé), environnement par liste blanche (aucun secret), montages validés par
la garde commune W1 `git_control_exposure` (contrôle Git direct, imbriqué ou ancêtre, `:`, racine, lien pendant, erreur de stat),
durée supervisée par `timeout(1)`, processus principal du conteneur et HORS du code livré (TERM puis KILL après 3 s : codes 124 et 137, une
expiration n'est déduite que si la durée a effectivement atteint la limite), relève de l'hôte et `docker kill` par NOM sur toute interruption,
échéance globale partagée (aucune phase après expiration). Docker/image indisponible ou montage refusé =
`incomplete`. `trusted_local_runner` est réservé aux fixtures de confiance et doit être demandé explicitement.

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
* Constats F1–F3 (IMPROVE après BUILD, collision de branches, `.coverage`) : corrigés (statut de cycle vs approbation du contenu,
  identité de branche `collegue/improve-r<round>-<empreinte(base_sha, arbres)>`, artefacts de mesure ignorés par la fixture) ;
  historique dans `reports/w4-b-interface.md`.
* **Reprise d'un projet `improving` (résolu à l'intégration)** : l'appel INITIAL de `run_project_from_settings` charge le plan avec la
  sémantique de contenu approuvé (`allow_cycle_status=True`, `require_approval=not dry_run` conservé) ; la seconde passe de D10 traverse
  donc l'entrée publique (`improvement_pass`) sans ré-approbation, avec la même vraie mesure enregistrée. La synchronisation GitHub
  (`sync_project_plan_from_settings`) garde la garde P4 stricte ; un plan modifié, révoqué ou en brouillon reste refusé
  (`test_w4_business_contract_handoff`, sans `xfail`).
* **Limite de la campagne réelle** : l'invocation réelle ne câble que planification, approbation, synchronisation, BUILD des trois
  tâches, vérification métier et lecture du registre. L'amélioration (R04) et l'incident avec rollback (R05) restent déclarés requis,
  non joués par ce lancement (`incomplete_validation` avec le point d'arrêt exact) : un BUILD réussi n'est pas la validation finale.
* **Constat produit** : tout fichier de code (`.py`) interdit l'auto-merge Phase 5 même dans l'allowlist ; une amélioration qui ajoute
  des tests (gain de couverture) n'est donc jamais auto-fusionnée. L'incident nominal utilise un gain mesuré par le scan de secrets
  sur de la documentation.
* L'écriture d'un PDF « maison » de référence est un choix de test (aucune dépendance de rendu) ; une vraie bibliothèque produirait
  un PDF différent mais lu par le même lecteur.
