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
* la phrase d'enveloppe est celle que le produit exige du déclenchement en P01. **Portée explicite : elle n'est acceptée que comme paramètre P01 de CE workflow**, qui n'a ni clé de
  modèle, ni environnement GitHub, ni commande `run`/`cleanup`/claim/activate : elle ne lance et ne consomme rien. Elle n'autorise jamais le workflow de campagne, ne le remplace
  pas et ne vaut pas ordre de campagne (qui exige un identifiant neuf, un environnement et un secret à recréer, sur ordre distinct). Aucun changement de P01 (propriété B) ;

### Publication et déclenchement manuel dédié (requis)

Un `workflow_dispatch` n'est déclenchable que si le fichier est sur la **branche par défaut**. Séquence retenue (chacune sur ordre exact du manager) :

1. **Petite PR autonome vers `main`** contenant exactement ces fichiers (workflow, harnais, test, ce document) : aucun fichier produit, aucun workflow de campagne ou nightly modifié. Les **cinq
   checks de livraison** de la PR (Ruff, audit des dépendances, Pytest 3.11, Pytest 3.12, build Docker) restent exigés ; revues absentes ou check manquant ne sont pas un succès.
2. Fusion sur ordre ; les cinq checks du push sur `main` sont observés à leur tour.
3. **Déclenchement manuel dédié** : `gh workflow run w5-product-preflight.yml --ref main -f confirm=<phrase P01>` — jamais le workflow de campagne, jamais d'essai opportuniste. Un seul dispatch
   par préflight ; une relance Actions donne la tentative 2 et P01 la refuse.

Tester d'abord le dispatch depuis la branche (`--ref <branche>`) est une hypothèse GitHub non éprouvée et n'est pas retenue. Le produit testé est celui de `GITHUB_SHA` ; `provenance.json` consigne ce SHA, les arbres
`collegue/`, `locks/`, `docker/sandbox/` et les blobs du harnais (à comparer avec `main`) : **une identité de contenu absente interdit le verdict complet**.

## Inventaire des sous-processus hôte du harnais (AGENTS.md, frontière Git)

Le harnais `scripts/w5_product_preflight.py` est un script CI hors du paquet `collegue/` ; il est néanmoins inventorié et couvert par le test statique
`test_host_subprocess_usage_is_inventoried` (`tests/test_executor_git_boundary.py`, périmètre étendu à ce seul script par `_INVENTORIED_CI_SCRIPTS`, sans balayer les autres scripts).
**Source de confiance commune** : le checkout GitHub Actions du SHA testé (`persist-credentials: false`), qui est le code sous test lui-même ; aucun workspace d'agent ni de tests n'y est
monté ou exécuté (classe F de `w1-isolation.md`). `cwd` est explicite (`ROOT` = racine du dépôt du script) et les chemins de sortie sont résolus en absolu AVANT le lancement, pour ne
dépendre pas du répertoire de l'appelant (le produit refuse déjà un `.env` présent dans son répertoire de lancement : le checkout n'en contient pas).

| # | Usage | Argv (liste fixe, aucun shell) | cwd | Pourquoi c'est de confiance |
|---|---|---|---|---|
| 1 | préflight du produit | `[sys.executable, -m, collegue.pilot.w4_business, preflight, --stage static\|full, --campaign-id ID, --output F, --human F]` | `ROOT` | c'est le produit testé, action `preflight` seule (liste fermée testée), identifiant validé par motif, fichiers sous la racine éphémère du runner |
| 2 | scanner de fuite | `[sys.executable, ROOT/scripts/w5_leak_scan.py, --env GITHUB_TOKEN, --quarantine D, --stage D, --report F, --source report=D]` | `ROOT` | script de confiance du même checkout ; le jeton est lu de l'environnement par le scanner, jamais passé en argv (testé) |
| 3 | identité git | `["git", "rev-parse", "HEAD:<chemin>"]` via `git_value`, chemins issus des constantes `PRODUCT_TREES` / `HARNESS_FILES`, délai 30 s | `ROOT` (défaut explicite) | lecture seule des arbres/blobs du checkout ; aucune écriture, aucun hook, sortie validée comme SHA |

Garde-fous testés (`tests/test_w5_ci_product_preflight.py`) : analyse AST du harnais (seul `subprocess.run`, `cwd` explicite sur chaque lancement, aucun `shell=True`, `Popen`, `os.system`,
`check_output`…), `cwd` identique quel que soit le répertoire de l'appelant, argv absolu et sans jeton. Un nouvel usage non listé fait échouer le test d'inventaire.

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
  sorties brutes du produit et du scanner ne sont **jamais** affichées (journal minimal typé, voir plus bas : erreurs et URLs mal formées ne peuvent donc rien révéler) ; tout texte court
  affiché est expurgé AVANT toute troncature ; aucun débogage HTTP, aucune copie d'environnement général. Le scanner de confiance (`scripts/w5_leak_scan.py`, inchangé) cherche le jeton dans le contenu et les noms de tout
  ce qui est publié ; un fichier contaminé n'est jamais déposé et rend l'étape rouge.
* **Propagation des échecs.** Le code du produit est conservé (1 échec, 3 validation incomplète) ; le scanner ne peut que l'aggraver ; un 0 sans rapport machine est rouge.
  Les rapports partiels restent déposés : un préflight arrêté en P07 laisse P09 à P12 `not_executed`, jamais « succès ».
* **Provenance typée** (`diag/provenance.json`, schéma `w5-product-preflight-provenance/1`) : SHA testé et du harnais, arbres/blobs, empreinte du manifeste, nom et id de l'image,
  commandes et codes RÉELS, étapes réellement jouées (ids et états), émissions, contexte de lancement. Seuls des champs typés sont recopiés (jamais détail, message ni preuve libre ; un
  identifiant de contrôle inconnu n'est jamais recopié). Verdict `validated` (0) seulement si : dispatch, tentative 1, SHA, manifeste, id d'image valide, **arbres produit ET blobs du harnais
  tous prouvés (SHA git valides)**, et **les deux étapes** validées : code 0, rapport sain `validated`, **liste de contrôles COMPLÈTE** (les douze `P01` à `P12`, chacun une fois, aucun inconnu,
  aucun élément illisible), drapeaux `required` conformes à l'étape (P08 facultatif et non joué à l'étape statique seulement, obligatoire à l'étape complète — une omission ou un drapeau falsifié
  n'est jamais cru), étapes requises réussies, zéro émission, scanner `clean`. `failed` (1) si un rapport est rouge ou incohérent ; sinon `incomplete_validation` (3), avec `partial: true` si le
  complet n'a pas été joué. Les écarts sont listés par identifiant attendu (`full:missing:P08-oracle-environment`, `static:required_flag:P01-launch-context`, `product_tree_absent`…).
* **Journal minimal.** Aucune sortie brute du produit ni du scanner n'est affichée ni déposée : le journal ne contient que des lignes **dérivées du rapport typé** (codes, verdict, `id : état`) et, si
  aucun rapport n'a été écrit, le seul NOM de classe de la dernière exception. Le détail est dans le rapport complet, scanné puis déposé. (Toute expurgation de texte se fait avant troncature.)

## Preuve locale (C69) et limites

* Hors ligne seulement : tests du contrat et du harnais ; **répétition** des contrôles hors ligne avec l'environnement exact du workflow (`evidence/w5-c69-rehearsal.json`) : P01 à P06,
  P09 et P10 réussissent, la forme du manifeste V2 est acceptée ; P07, P11, P12 et P08 ne se jouent qu'à distance (GitHub réel, Docker). C'est une **preuve partielle**, pas une exécution du produit
  contre GitHub.
* Jamais exécuté à distance : le premier dispatch pourra révéler une divergence (autorisations Actions, image, lecture de P11/P12). Les artefacts et la provenance sont faits pour le diagnostiquer
  sans rouvrir de secret. Aucune écriture n'est possible avec ce workflow.
