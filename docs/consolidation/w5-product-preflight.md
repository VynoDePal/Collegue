# Préflight PRODUIT distant, sans modèle (C69)

**But.** Exécuter sur GitHub Actions, avec le jeton de lecture de la fixture (`INTEGRATION_FIXTURE_GITHUB_TOKEN`, `Administration:read` ajouté) et **sans aucune clé
de modèle**, les VRAIES entrées publiques du produit — `python -m collegue.pilot.w4_business preflight --stage static` puis `--stage full` — pour établir
que les contrôles **P01 à P12** (dont P07 protections W3, P09 modèles, P10 courtier, P11 socle V2, P12 identifiant jamais consommé, et P08 pile des oracles dans
l'image du courtier réellement construite) passent contre l'API réelle, AVANT toute décision de campagne. Ce n'est **pas** une campagne : zéro appel modèle,
zéro mutation, aucun identifiant consommé. Une exécution validée ne prouve **ni BUILD, ni R04/R05, ni la campagne** ; une nouvelle campagne exige un ordre distinct
(identifiant neuf, enveloppe 2 USD / 250 000 tokens / 900 s) et la remise en place de son environnement et de son secret, aujourd'hui supprimés.

## Fichiers (propriété C, aucun code produit)

| Fichier | Rôle |
|---|---|
| `.github/workflows/w5-product-preflight.yml` | workflow autonome `workflow_dispatch`, un job, 30 min max, `contents: read`, aucun environnement GitHub |
| `scripts/w5_product_preflight.py` | harnais : `stage` (lance le préflight du produit puis le scanner de fuite) et `provenance` (enregistrement typé) |
| `tests/test_w5_ci_product_preflight.py` | contrat du workflow + harnais + deux tests qui exécutent le VRAI scanner et la VRAIE entrée du produit hors ligne |
| `docs/consolidation/w5-product-preflight.md` | ce document |

Aucune modification de `collegue/*`, des workflows de campagne ou nightly, de `locks/`, des Dockerfiles ni des secrets.

## L'obstacle P01 et son traitement

`check_launch_context` exige `GITHUB_EVENT_NAME == "workflow_dispatch"`, `GITHUB_RUN_ATTEMPT == "1"`, `W4_BUSINESS_CONFIRM == LANCER-UNE-FOIS-2USD-250000TOKENS-900S` et
la récurrence nightly inactive ; sur tout autre contexte il rend `incomplete_validation` et **arrête toutes les étapes suivantes** (`CampaignReport.run` : étape
échouée ⇒ `halted`). Conséquences :

* un workflow déclenché par `push`/`pull_request` ne peut pas jouer P02 à P12 honnêtement : P01 le refuserait (c'est le garde qui fonctionne) ; fabriquer
  `GITHUB_EVENT_NAME` ou `GITHUB_RUN_ATTEMPT` pour passer serait exactement ce qui est interdit ;
* il faut donc un **vrai `workflow_dispatch`**, première tentative, avec la phrase saisie par l'humain : fait ici, sans rien fabriquer. Une **relance Actions**
  (tentative 2) est refusée par P01 : un nouveau préflight = un nouveau dispatch ;
* la phrase d'enveloppe est celle que le produit exige du déclenchement ; ce workflow ne lance rien (aucune clé, aucune commande `run`, aucun environnement). Le choisir
  autrement demanderait un changement de P01 chez B — non nécessaire.

### Enregistrement GitHub (mécanisme)

Un `workflow_dispatch` n'est déclenchable que si le fichier est sur la **branche par défaut**. Chemin certain : une **petite PR vers `main`** contenant ce workflow, le
harnais, le test et ce document (aucun fichier produit), fusionnée sur ordre du manager, puis `gh workflow run w5-product-preflight.yml --ref main -f confirm=<phrase>`
sur ordre exact. Chemin facultatif non éprouvé (à tester par le manager, sans risque car sans clé) : pousser la branche puis tenter le dispatch avec `--ref <branche>` ;
si GitHub répond 404 (« workflow not found on the default branch »), revenir à la PR. Dans les deux cas l'événement est un vrai dispatch. Le produit testé est celui de
`GITHUB_SHA` ; `provenance.json` consigne ce SHA et les arbres `collegue/`, `locks/`, `docker/sandbox/` à comparer avec `main` = `734c7f5`.

## Déroulement du job (un seul job, une étape qui échoue arrête les suivantes sauf dépôt des preuves)

1. Chemins éphémères : `COLLEGUE_HOME` neuf, `STATE_DATABASE_URL` SQLite du run, identifiant **`w5-preflight-<run_id>`** (jamais revendiqué : P12 vérifie seulement
   qu'aucune branche de revendication n'existe), `W5_BOOTSTRAP_MANIFEST` = **`tests/fixtures/w5-business/manifest.v2.json` suivi** (aucune variable de dépôt) ; il est relu
   puis VÉRIFIÉ par l'API (P11, `validate_bootstrap_manifest`), jamais cru.
2. `checkout` sans identifiants persistés, Python 3.12, dépendances `locks/dev.txt` (hachées), commande de santé via `scripts/w5_workflow_env.py`, migrations du registre local.
3. **Préflight statique** (jeton, aucune clé de modèle) : P01 à P07, P09 à P12 ; P08 facultative et non jouée à ce stade.
4. **Image du sandbox du courtier** (`docker build -f docker/sandbox/Dockerfile.broker`), seulement si 3. a réussi.
5. **Préflight complet** (jeton) : tout ce qui précède plus P08 (`docker run --network none … ` pile des oracles et lecteur PDF réel).
6. **Provenance** (aucun secret, toujours jouée) puis dépôt de deux artefacts : `w5-product-preflight-report` (uniquement l'ensemble scanné `publish/`) et
   `w5-product-preflight-diagnostic` (provenance, codes, rapports du scanner).

Les réglages d'environnement du job sont **identiques** à ceux du job de campagne (un test compare valeur par valeur ; seul l'identifiant de campagne diffère) : P02 vérifie
l'enveloppe réelle.

## Garanties et barrières (testées)

* **Pas de modèle.** Aucun nom de clé (`LLM_API_KEY*`, `GEMINI`, `GOOGLE`, `OPENAI`, `ANTHROPIC`, `W5_GOOGLE*`) dans le workflow ; aucun environnement GitHub ; le harnais REFUSE
  (code 2, avant toute commande) si une telle variable existe ; le produit la refuse aussi en P03. Rapport : `llm_calls_emitted` et `billable_actions_emitted` relus et exigés à 0.
* **Pas de mutation.** La seule action du produit construite est `preflight` (liste fermée, testée : ni `run`, `cleanup`, `capacity`, ni claim/activate) ; les contrôles du produit
  sont des lectures. Aucune commande `gh`, `curl`, `docker run/login`, `git push` dans le workflow.
* **Jeton confiné.** `secrets.INTEGRATION_FIXTURE_GITHUB_TOKEN` n'apparaît que dans l'`env` des deux étapes de préflight (sans action tierce) ; jamais au niveau du job. Les
  sorties du produit et du scanner sont relues puis **expurgées** de toutes les formes usuelles du jeton avant d'atteindre le journal (erreurs et URLs mal formées comprises) ;
  aucun débogage HTTP, aucune copie d'environnement général. Le scanner de confiance (`scripts/w5_leak_scan.py`, inchangé) cherche le jeton dans le contenu et les noms de tout
  ce qui est publié ; un fichier contaminé n'est jamais déposé et rend l'étape rouge.
* **Propagation des échecs.** Le code du produit est conservé (1 échec, 3 validation incomplète) ; le scanner ne peut que l'aggraver ; un 0 sans rapport machine est rouge.
  Les rapports partiels restent déposés : un préflight arrêté en P07 laisse P09 à P12 `not_executed`, jamais « succès ».
* **Provenance typée** (`diag/provenance.json`, schéma `w5-product-preflight-provenance/1`) : SHA testé et du harnais, arbres/blobs, empreinte du manifeste, nom et id de l'image,
  commandes et codes RÉELS, étapes réellement jouées (ids et états), émissions, contexte de lancement. Seuls des champs typés sont recopiés (jamais détail, message ni preuve libre).
  Verdict `validated` (0) seulement si : dispatch, tentative 1, SHA, manifeste, id d'image valide, **les deux étapes** validées (code 0, rapport sain `validated`, étapes requises réussies,
  zéro émission, scanner `clean`) ; `failed` (1) si un rapport est rouge ou incohérent ; sinon `incomplete_validation` (3), avec `partial: true` si le complet n'a pas été joué.

## Preuve locale (C69) et limites

* Hors ligne seulement : tests du contrat et du harnais ; **répétition** des contrôles hors ligne avec l'environnement exact du workflow (`evidence/w5-c69-rehearsal.json`) : P01 à P06,
  P09 et P10 réussissent, la forme du manifeste V2 est acceptée ; P07, P11, P12 et P08 ne se jouent qu'à distance (GitHub réel, Docker). C'est une **preuve partielle**, pas une exécution du produit
  contre GitHub.
* Jamais exécuté à distance : le premier dispatch pourra révéler une divergence (autorisations Actions, image, lecture de P11/P12). Les artefacts et la provenance sont faits pour le diagnostiquer
  sans rouvrir de secret. Aucune écriture n'est possible avec ce workflow.
