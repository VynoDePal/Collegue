# Paire JSON W5 — workflow et contrat de raccord (propriété C)

Statut : **intégré localement (A `e598f3bef03453771964df15d2491eca5381347b`, B `5407208a9fb578b30f347c33b8c98dc03380f187`, workflow C `24af0072ad8fc33a6e0b6e0366ab061f5e3ebf33`) ; la PR #617 (tête `ffb48b5`) est ouverte et son finding de reprise est corrigé localement par A46 `01319977f37291d0f046a16e1e8bc3927109cf15` / B41 `b408fd4792df7dadb64255444f4357a58c9dedeb` (nouvelle tête à publier sur ordre) ; paire non lancée.** La paire est autorisée *en principe* par l'utilisateur (13 octobre 2026) ; elle ne qualifie pas le parcours métier ; le manager ordonne séparément la publication, la fusion sur un SHA précis, la préparation temporaire de l'environnement et du secret, puis le dispatch. Ce document décrit le workflow `.github/workflows/w5-json-pair.yml` et son raccord au CLI `scripts/w5_json_pair.py` (lot A : `collegue/broker/json_pair.py`, `docs/consolidation/w5-json-pair.md`, `tests/test_w5_json_pair.py`). Les tests adverses du cœur sont ceux du lot B (`w5-json-pair-tests.md`, `tests/test_w5_json_pair_adversarial.py`, `tests/w5_json_pair_support.py`, 97/97 à B39). Contrat commun : `briefs/w5-json-pair-contract.md` et arbitrage `briefs/w5-json-pair-cli-ruling.md` (sous la racine d'orchestration, hors dépôt).

## Ce que la paire est — et n'est pas

Deux générations au plus sur `gemma-4-31b-it`, même prompt JSON du canari, **sans** puis **avec** `responseMimeType` (empreintes normalisées `2edf3771…ecc2` puis `98d8c344…a300`), enveloppe unique **2 USD / 250 000 jetons / 900 s**, une tentative par variante. Elle isole le seul MIME, ce que la comparaison historique texte/JSON ne faisait pas (le prompt et l'instruction système différaient aussi). Elle ne qualifie ni les outils, ni le 26B, ni BUILD/IMPROVE/incident/rollback/reprise, et **ne change pas le verdict de la qualification réelle, qui reste incomplète** : `business_validation` vaut toujours `not_executed`. Une paire réussie ne prouve pas la cause du HTTP 500 du 12 octobre (perdu : son corps n'a pas été conservé) ni un support permanent du mode JSON ; une paire échouée ne prouve pas un non-support.

## État de la qualification à la date du document

* Campagne `w5-gemma-20261012-538fff52` (run `38273892997`) : **close, incomplète**. Texte 31B OK (86 jetons réglés) ; JSON 31B → HTTP 500 / RPC `INTERNAL`, 270 jetons conservés `unknown`, scope bloqué ; aucune suite, aucun retry ni repli. Secret, environnement et variable supprimés après le run. Elle ne se relance jamais.
* Diagnostic hors ligne du canari JSON (A40/A41/A42, B35/B36, contre-revue C86/C87) : **terminé** ; aucun défaut local démontré, cause indéterminée.
* Paire : **autorisée en principe, non lancée.** Aucun registre, secret ni identifiant n'existe pour elle.

## Étapes du workflow (un seul job, aucune image Docker, aucun Docker local)

| # | Étape | Secret présent | Rôle |
|---|---|---|---|
| 1 | Contexte de lancement réel | aucun | exporte les chemins éphémères (toujours), puis compare — sans les fabriquer — événement `workflow_dispatch`, référence `refs/heads/main`, `GITHUB_SHA == expected_sha`, `GITHUB_RUN_ATTEMPT == 1`, regex de l'identifiant et du SHA |
| 2 | Pointe de `main` inchangée | `github.token` éphémère, lecture seule | `gh api repos/…/commits/main --jq .sha` doit être `expected_sha` ; toute erreur de lecture est un échec |
| 3-5 | checkout, Python 3.12, dépendances verrouillées | aucun | comme les autres workflows W5 (`locks/dev.txt`, aucune dépendance nouvelle) |
| 6 | `preflight` | **aucun** | contexte réel lu de l'environnement, corps et empreintes, enveloppe, nightly inactif ; refuse si une clé de modèle ou un jeton est présent |
| 7 | `claim` | **jeton de la fixture seulement** | création exclusive et atomique de `collegue-business-claims/<id>` AVANT que la clé existe ; aucune erreur GitHub lue comme une absence ; jamais supprimée |
| 8 | migrations | aucun | `python -m collegue.migrations upgrade` sur le SQLite vierge de la campagne (convention ci-dessous) |
| 9 | **Paire réelle** | **clé seulement** (`LLM_API_KEY` ← `W5_JSON_PAIR_GOOGLE_API_KEY`) | `run` borné, puis `finalize` dans un processus **sans** clé, puis le scanner de confiance avec la clé |
| 10 | Rapports sans clé | aucun | seulement si la paire a été `skipped` : `finalize` puis `--assume-no-key` (la clé n'a jamais existé) |
| 11 | Diagnostic minimal sûr | aucun | statuts d'étapes (valeurs énumérées par GitHub) et identifiants validés ; jamais dérivé d'un fichier |
| 12-14 | dépôts | aucun | `w5-json-pair-report`, `w5-json-pair-registry`, `w5-json-pair-diagnostic`, uniquement depuis l'ensemble scanné |

Déclenchement : `workflow_dispatch` seulement, avec `confirm` (`LANCER-UNE-PAIRE-JSON-2GENERATIONS-2USD-250000TOKENS-900S`), `campaign_id` (neuf, aucun défaut) et `expected_sha` (40 hexadécimaux). Permissions `contents: read`. Concurrence : groupe `w5-json-pair`, jamais annulée en cours. Environnement GitHub `w5-json-pair`, distinct du nightly et des anciennes campagnes ; **le manager le crée, y place le secret, et les supprime à la clôture** (le workflow n'a aucun droit d'administration). Si l'environnement n'existait pas, GitHub le créerait vide au premier dispatch : la clé serait absente et `run` refuserait avant tout appel.

### Pourquoi `finalize` précède le scan et tourne sans clé

Le scanner n'approuve que les octets qu'il lit. Si `finalize` réécrivait un rapport après le scan, ces octets ne seraient pas approuvés. L'étape qui reçoit la clé orchestre donc : `run` (timeout dur `timeout --signal=TERM --kill-after=30 960`, l'échéance de 900 s étant le contrôle principal et portée par le CLI), puis `env -u LLM_API_KEY … finalize` (aucune clé dans ce processus), puis `scripts/w5_leak_scan.py --env LLM_API_KEY` sur `report/` et `home/*.sqlite3*` seulement, qui construit seul l'ensemble publiable. Aucun fichier modifié après le scan ne rejoint l'ensemble. Un désaccord entre le code de `run` et celui de `finalize`, un code inattendu (124, 137, 143, plantage) ou un `finalize` en erreur donnent **3 (incomplet)** ; une fuite ou une analyse impossible donnent **1** et le fichier contaminé n'est pas déposé. Si le délai de l'étape (25 min) ou du job (35 min) coupe le scan, rien n'est déposé depuis les sources : seul le diagnostic de statuts l'est, et la comparaison est incomplète.

## Interface du CLI (arbitrée, intégrée)

`python scripts/w5_json_pair.py {preflight|claim|run|finalize} --root R --campaign-id ID`, sous `R = $RUNNER_TEMP/w5-json-pair`. Besoins de C : `evidence/w5-c88-cli-needs.md` (hors dépôt) ; arbitrage : le contrat de C prévaut. Le CLI importe `CONFIRMATION` du cœur (la phrase n'existe qu'une fois) : les tests de C vérifient la valeur réellement exposée, pas une chaîne du source. Sorties : `proof/preflight.json`, `proof/claim.json` (identité, référence créée, `run_id`, `run_attempt`, SHA, dépôt : une simple preuve `created=true` ne suffit pas), `home/<id>.sqlite3` (registre et journal, même fichier), `report/json-pair.{json,txt}`, `report/provenance.json`. Codes : `0` observations obtenues, `1` échec observé, `3` incomplet ou refus, `2` usage invalide. Aucun argument ne permet de fabriquer le contexte GitHub ni de choisir le modèle, l'URL ou le budget. Le contexte réel est relu de l'environnement à chaque étape pertinente et comparé à la preuve. Seuls `--expected-sha` et `--confirm` sont des affirmations du déclencheur, et elles sont contrôlées.

Verdict (arbitrage § 4) : `observed` exige les deux variantes exécutées et la seconde réponse = objet JSON valide à usage connu, sans blocage ; un témoin 200 non JSON ou vide à usage connu continue (`json_valid=false`) ; une seconde réponse 200 non JSON ou vide est `failed / json_output_invalid` (code 1) même si l'usage est réglé ; une ambiguïté, un incident interne ou un refus de reprise sont `incomplete` ; tout refus HTTP explicite arrête la paire.

### Convention du registre

Le workflow applique les migrations standard sur le SQLite vide de la campagne, après `claim` et avant `run` ; le CLI considère un registre migré, sans tentative, sans scope ni dépense, comme neuf, et refuse un registre d'une autre campagne, avec une dépense ou un démarrage antérieur. Le cœur peut aussi initialiser un schéma neuf lui-même ; l'étape de migration n'en est pas une dépendance dure, mais elle fait valider la migration standard en CI. La protection contre un nouveau dispatch réutilisant l'identité repose sur la **référence GitHub persistante**, pas sur le SQLite du runner (jetable).

### Variable du nightly et environnement du CLI

`INTEGRATION_E2E_ENABLED` est lue **réellement** par le workflow (`${{ vars.INTEGRATION_E2E_ENABLED }}`) et jamais remplacée par une constante, qui masquerait un nightly activé. Le CLI n'accepte que la valeur exacte `false` ; une variable **absente, vide ou ambiguë** (`False`, `0`, `unknown`…) est refusée (`nightly_unestablished`), une valeur active (`1/true/yes/on`) aussi (`nightly_active`). La variable existe à `false` sur le dépôt (lecture manager du 13 octobre). Les noms de variables d'un runner qui ressemblent à des secrets (`ACTIONS_RUNTIME_TOKEN`, `ACTIONS_ID_TOKEN_REQUEST_*`) sont admis ; tout autre nom portant `API_KEY`, `SECRET`, `PASSWORD`, `PRIVATE_KEY`, `TOKEN` ou `PAT` est refusé par chaque étape qui n'en est pas propriétaire (jeton de la fixture : `claim` seul ; `LLM_API_KEY` : `run` seul ; `GH_TOKEN` n'existe que dans l'étape de lecture de `main`, avant tout CLI).

### Ce que le registre contient

Le registre budgétaire conserve la **sortie du modèle** : le journal et le rapport de la paire ne contiennent ni clé ni texte libre (tests A/B), mais une clé reflétée par le modèle dans une réponse se retrouve dans les octets du registre. Le scanner de confiance le détecte alors (verdict rouge, code 1, registre déplacé en quarantaine et jamais déposé, rapport sain déposé). On ne peut donc pas annoncer une absence globale du secret dans le registre : c'est le scan qui en juge, à chaque exécution.

### Reprise d'un journal fermé (A46, B41)

`_conduct` ferme toujours le journal de la paire, même quand une **écriture** du service a échoué après l'émission (verrou SQLite, disque) : la tentative reste `emitting` et la réserve ouverte. L'ancien `finalize` prenait « journal fermé » pour « registre réparé ». Désormais `finalize` répare le registre d'après l'état durable des tentatives (`emitting` d'un propriétaire disparu → inconnu bloquant jamais rejoué, `prepared` → libéré sans consommation inventée, règlement établi mais non appliqué → appliqué une seule fois), sans clé, sans réseau, sans jamais rouvrir ni réémettre ; un conducteur vivant n'est pas touché ; un journal corrompu ou étranger est refusé avant toute réparation ; deux variantes réglées avec une réserve encore ouverte ne sont plus `observed` (`incomplete/budget_unknown`). Dans le workflow, `finalize` s'exécute **avant** le scan : le registre déposé est le registre réparé. Tests de C : `test_finalize_repairs_a_closed_journal_…`, `test_finalize_completes_a_durable_settlement_…` et `test_the_pair_step_publishes_the_registry_repaired_…` dans `tests/test_w5_json_pair_workflow_cli.py` (pannes d'écriture injectées dans le seul processus de `run`, `finalize` réel ensuite), rouges sur le code de `ffb48b5` et verts avec A46.

### Verdict d'une borne démentie

Une réponse peut être un JSON valide et néanmoins interdite pour budget ou borne : `bound_violation` à usage connu est `failed` (le scope est bloqué, la seconde variante n'est pas lancée) ; `json_valid` est un fait de parsing, pas la validation de la livraison.

## Ce que le workflow ne fait pas

Pas de Docker ni d'image, pas d'OpenHands, pas d'exécution métier ; ni `w4_business`, ni `w5_fixture_bootstrap`, ni nettoyage (la revendication reste après la fin) ; aucun appel `countTokens` ou `generateContent` hors du CLI et du service budgétaire ; aucune clé ni jeton dans les arguments, journaux ou rapports ; aucun rerun (`run_attempt != 1` refusé, identité revendiquée, groupe de concurrence unique) ; aucune écriture GitHub hors la référence de revendication ; aucun élargissement à d'autres modèles.

## Tests (tous hors ligne, aucun fournisseur, aucun GitHub, aucun `pip install`)

* `tests/test_w5_json_pair_workflow.py` : structure du YAML (déclencheur unique, entrées, `if`, permissions, concurrence, environnement propre, absence de Docker/métier/nettoyage, ordre des étapes, clé et jeton chacun dans une seule étape, préflight sans secret, `finalize` sous `env -u`, scanner sans `--assume-no-key` après la clé) et exécution des scripts d'étape avec des doublures du CLI (matrice de codes, timeouts, désaccords, fuites, scanner qui plante) ; valeur de `CONFIRMATION` réellement exposée par le CLI.
* `tests/test_w5_json_pair_workflow_cli.py` + `tests/w5_json_pair_cli_harness.py` : le **workflow joué de bout en bout** — `${{ }}` résolues, `if` évalués, chaque `run:` dans `bash -eo pipefail`, `$GITHUB_ENV` rechargé — avec le **vrai CLI**, le vrai `GoogleUpstream` (réseau remplacé par `httpx.MockTransport`, faux Google de B), les vraies migrations, le vrai `timeout`/`env -u` et le **vrai scanner**. Seuls `gh` (pointe de `main`) et deux fabriques de services externes (dépôt GitHub de la fixture, couche réseau) sont des doublures. L'étape d'installation des dépendances est exclue du banc (elle télécharge) ; son shim refuse `pip`. Scénarios : nominal (4 requêtes, 4 sous-commandes, `finalize` sans clé, jeton seulement à `claim`, clé seulement à `run`, ensemble publié exact), erreur ambiguë (inconnu 270 conservé, code 3), 4xx (code 1, aucun renvoi), sortie native non JSON (code 1), témoin non JSON (la seconde variante continue), clé reflétée dans une réponse ou dans le registre (scan rouge, valeur jamais publiée ni affichée), nightly absent/faux/actif, revendication existante ou en erreur, `main` qui avance, tentative 2, reprise sans réémission, préflight sur registre démarré.
* Aucun contrôle de la paire n'est ignoré : `test_every_file_of_the_pair_exists_so_no_check_can_pass_by_skipping` échoue si un fichier disparaît, et les tests de A/B n'ont plus de `skip` actif une fois le workflow présent.
* `tests/test_w5_ci_campaign_workflow.py` exempte ce workflow de l'interdiction de toute clé `GOOGLE_API_KEY` et vérifie que les autres workflows ignorent la clé et l'environnement de la paire.

## À valider après intégration (non fait ici)

La suite complète, Ruff, le wheel installé hors checkout et le CLI relié au faux fournisseur sont rapportés dans `reports/w5-c90-json-pair.md` (hors dépôt). Restent à faire sur GitHub, après publication ordonnée : la construction Docker et les cinq checks, qui restent requis (Ruff, Pytest 3.11, Pytest 3.12, Dependency audit, Docker build). Publication, fusion sur la tête exacte et dispatch sont trois ordres distincts du manager.
