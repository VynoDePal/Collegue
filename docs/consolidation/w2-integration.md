# Vague 2 — protocole et checklist d'intégration (C)

Document de préparation, rédigé **avant** les livraisons de A et B. Il ne décrit aucun résultat de leur travail : il fixe
ce que C vérifie quand le manager communique leurs SHA finaux. Règles générales : [`AGENTS.md`](../../AGENTS.md) ;
protocole de la vague 1 et checklists précédentes : [`protocole.md`](protocole.md).

- **Base commune** : `main` = `9862b3952e81fd543a0ed78be1fca6ddce6fd29d` (PR #608 et #609 ; arbre identique à celui accepté ; 5 checks du push verts).
- **Branches** : A `codex/consolidation-w2-a`, B `codex/consolidation-w2-b`, C `codex/consolidation-w2-c` (même base).
- **Objet** : A = registre de budget transactionnel durable et exécution bornée ; B = distribution installable (wheel, ressources, migrations) et dépendances verrouillées depuis `pyproject.toml`.
- **Non-objectifs** : le routage fournisseur/modèle par rôle (vague 4), la preuve de livraison commune et la politique de fusion (vague 3), le nettoyage des `serve.py` de `quality_gate` (vague 3), la campagne réelle (2 USD / 250 000 tokens / 900 s, après la vague 4).

## 1. Propriété des fichiers et raccordements convenus

| Qui | Possède |
|---|---|
| **A** | `collegue/state/models.py`, `collegue/state/manager.py`, nouveau module de registre (sous `state/` ou `pilot/`), `pilot/{budget,audit,driver,runtime}.py`, `improve/loop.py`, `monitoring/{metrics,sampling_usage}.py`, chemins de dépense `core/llm` / sampling / `executor/{oh_runner,openhands_sdk_agent,openhands_agent,agent,runner}.py`, timeout du sandbox, configuration budgétaire (`config.py`, `.env.example`), `planner/{spec_generator,decomposer,acceptance_tests}.py` (raccord budgétaire uniquement), la migration `0011`, leurs tests, `docs/consolidation/w2-budget.md` |
| **B** | `pyproject.toml`, locks et requirements générés, packaging, loaders de ressources / templates / skills, ressources non Python embarquées, **migrations existantes (0001–0010)** et leur configuration Alembic/CLI, workflows CI, Dockerfiles (hors code worker de A), scripts de vérification de locks / installation, leurs tests, `docs/consolidation/w2-installation.md` |
| **C** | `docs/consolidation/w2-integration.md`, `AGENTS.md`, `CLAUDE.md`, `protocole.md`, raccordements **strictement mécaniques** ci-dessous |

Un fichier a un seul propriétaire. Un besoin sur un fichier d'un autre rôle est écrit dans le rapport, pas édité. Aucun comportement de A ou de B n'est réécrit en silence par C ; une incompatibilité réelle retourne aux auteurs via le manager.

### Raccordements convenus (à exécuter par C à l'intégration, pas avant les SHA finaux)

1. **Migration `0011`.** A crée `alembic/versions/0011_budget_ledger.py` avec `revision="0011"` et `down_revision="0010"` (IDs réels, sans suffixe ; les noms de fichiers ne sont pas les IDs). B embarque le graphe jusqu'à `0010` (`0010_phase5_incidents`, `down_revision="0009"`). **C seul** déplace `0011` au même emplacement que les migrations embarquées, par `git mv` (renommage détecté à 100 %), sans modifier son contenu ni ses IDs ; si le déplacement exige une édition autre que le chemin (import relatif, `script_location`), c'est une incompatibilité et elle retourne aux auteurs.
2. **Tests PostgreSQL réels.** A fournit, dans son rapport, le module et la commande exacte des tests de concurrence PostgreSQL. B prépare une CI déterministe avec service PostgreSQL, sans secret ni appel LLM. C raccorde le chemin réel dès qu'il est connu. **Ne jamais déclencher le nightly existant** (`integration-nightly.yml` appelle des modèles).
3. **Scripts worker copiés dans l'image.** `docker/sandbox/Dockerfile.openhands` copie isolément `collegue/executor/oh_runner.py` et `oh_sampler.py` dans `/opt`, dans une image qui n'embarque **pas** le package `collegue`. Aujourd'hui ces scripts n'importent que la bibliothèque standard et `openhands.*`. A documente tout import ajouté ; B/C raccordent le `COPY` correspondant. Un `import collegue…` ajouté sans copie casse l'image sans casser la suite de tests.
4. **Pins OpenHands.** `openhands-ai==1.7.0`, SDK et tools `1.19.1`, `lmnr==0.7.52` et le patch `scripts/patch_openhands_gemma4_terminal.py` (préimage 1.19.1) restent testés et verrouillés ; la CI conserve l'étape « Verify OpenHands Gemma 4 terminal contract ».
5. **Livraison.** Trois niveaux de tests, livraison en deux étapes, arbitrage manager de tout finding avant fusion, cinq checks requis inchangés (`Ruff`, `Pytest (Python 3.11)`, `Pytest (Python 3.12)`, `Dependency audit`, `Docker build`), aucun bypass (voir `AGENTS.md`).
6. `collegue/state/__init__.py` (exports communs du registre) : raccordement mécanique par C si A ajoute des exports et que B n'y touche pas.

## 2. Ordre d'intégration

1. Attendre les SHA finaux et la consigne du manager. Ne pas intégrer de travail en cours.
2. Pour chacun : tête de branche = SHA annoncé, arbre propre, descendance de `9862b39`, rapport lu (dernier addendum plutôt que les sections historiques).
3. Intersection des fichiers modifiés par A et B : **doit être vide** (`comm -12 <(git diff --name-only 9862b39 <A>|sort) <(git diff --name-only 9862b39 <B>|sort)`). Sinon, arrêt et retour au manager.
4. `git merge --no-ff <SHA_B>` puis `<SHA_A>` dans `codex/consolidation-w2-c` (B d'abord : il fixe l'emplacement des migrations).
5. Raccordements mécaniques, chacun dans **son propre commit** (déplacement de `0011`, exports, chemin des tests PostgreSQL dans le workflow, `COPY` éventuels) ; aucune autre modification.
6. Fixer le SHA propre, l'écrire dans `reports/w2-c-candidate.json` (`{"sha":…,"testing":true}`) **avant** les tests, pour que Codex valide en parallèle ; tout changement ultérieur est signalé et invalide les preuves.
7. Vérifications (§4), rapport `reports/w2-c-integration.md`, puis arrêt : aucun push sans consigne.

## 3. Checklist de revue

Preuve attendue pour chaque ligne : un test (ou une sortie conservée) **rouge sur `9862b39`**, vert ensuite, avec assertion sur un comportement public. Un rouge par `ImportError`/`AttributeError` ne prouve pas le défaut. Un test sauté ou un service absent n'est **jamais** un succès.

### 3.1 Import des cumuls historiques (A)

Les colonnes `run_cost_usd` et `run_tokens` des métriques sont des **snapshots cumulatifs ordonnés**, pas des deltas.
- [ ] Trois snapshots cumulatifs d'un même projet (par ex. 0,60 puis 1,20 puis 1,20) importent **1,20** (le dernier cumul), jamais 3,00. Idem pour les tokens.
- [ ] L'ordre est défini par une clé stable et documentée (pas l'ordre d'insertion implicite) ; un snapshot décroissant ou incohérent est traité de façon conservatrice et visible, pas écrasé en silence.
- [ ] L'import a lieu **une seule fois** : second `upgrade`, second démarrage, migration rejouée ou `downgrade` puis `upgrade` n'ajoutent rien ; test sur base existante à `0010` contenant des données, puis sur base vide.
- [ ] Projets sans métriques, projets de plusieurs cycles, valeurs nulles ou négatives : comportement explicite.
- [ ] Précision monétaire conservatrice (entiers en micro-unités ou `Decimal`, jamais un cumul de `float`) ; arrondi toujours en faveur du plafond.
- [ ] `0011` : `revision="0011"`, `down_revision="0010"`, tête unique du graphe ; additive (aucune colonne ni table existante modifiée ou supprimée).
- [ ] **Parité modèle ↔ migration** : après `upgrade head`, `alembic` ne détecte aucune différence avec `Base.metadata` ; `ProjectStateManager(create=True)` utilise `create_all` (`state/manager.py`) tandis que la production migre : les deux chemins doivent produire le même schéma, contraintes d'unicité comprises.

### 3.2 Réservations, idempotence et usage inconnu (A)

- [ ] **Réserver avant de dépenser**, avec un identifiant durable unique ; rejouer la même réservation renvoie la même, sans doubler.
- [ ] **Concurrence réelle** : deux connexions (SQLite) et deux sessions/processus (PostgreSQL) réservant en même temps un solde insuffisant pour les deux ne dépassent jamais le plafond ; atomicité par transaction, contrainte ou compare-and-swap, **pas par un verrou de process**. Le test PostgreSQL tourne sur un service réel ; un mock ne prouve rien.
- [ ] **Engager** la consommation est idempotent : un événement rejoué (même identifiant) ne double pas ; un engagement supérieur à la réservation est enregistré (dépassement visible), pas tronqué.
- [ ] **Libérer** uniquement ce dont l'absence de consommation est établie ; jamais par simple expiration ou après une erreur d'origine inconnue.
- [ ] **Usage inconnu** (réponse sans compte, crash entre émission et comptabilisation, erreur de persistance) : la réservation est **conservée**, le mode strict est **bloqué avec un motif durable**, qui survit au redémarrage ; jamais converti en zéro.
- [ ] Crash avant émission, après émission et avant comptabilisation, après comptabilisation : trois cas distincts testés, avec le solde final attendu pour chacun.
- [ ] Nouvelle instance + même base ⇒ même consommé / réservé / solde ; aucun remise à zéro au redémarrage.
- [ ] Les métriques et audits affichés proviennent du registre ; aucun **double comptage** avec `MetricsCollector` (comparer les totaux des deux sources sur un scénario commun).

### 3.3 Cycle complet : planification → BUILD → IMPROVE → reprise (A)

- [ ] Rejouer la sonde du manager (`manager_w2_budget_baseline.py`, hors dépôt) : BUILD 2 × 0,60 $ et IMPROVE 2 × 0,70 $ sous un plafond de 1 $ donnaient `continue` ; la décision devient un arrêt / une pause.
- [ ] `run_project` est rappelé entre les fusions : l'accumulateur du codeur ne repart plus de zéro ; le total vient du registre.
- [ ] **Planification** : `plan_project` appelle `generate_spec` avant `persist_spec` / `create_project`. Un contexte durable existe **avant** la première dépense ; les frais de la SPEC, de la décomposition, de la QA et de l'approbation ne sont pas perdus ; un échec de planification est conservé ; le même solde est visible après redémarrage. Preuve à l'entrée publique de planification, pas seulement en BUILD / IMPROVE.
- [ ] Le registre est commun à BUILD, IMPROVE, au sampling des autres rôles, aux retries, aux replis / changements de modèle et aux reprises.
- [ ] Les paramètres budgétaires (`config.py`, `.env.example`, documentation) décrivent le mode strict et le mode non strict **sous des noms distincts** ; le mode non strict n'annonce pas de garantie.

### 3.4 Appels réellement émis et échéance (A)

- [ ] **Inventaire de tous les sites d'appel** (`git grep` sur les clients LLM, le sampling, le codeur OpenHands, le reviewer, la QA, le planner, le juge, les retries et replis) : chacun réserve **avant** l'émission. Un site oublié est un défaut.
- [ ] Un fournisseur factice compte les appels **émis** : un dépassement est refusé **avant** l'appel (zéro appel émis), et le solde du registre est cohérent avec le nombre d'appels émis.
- [ ] **Worker** : l'allocation est bornée par le solde et par l'échéance ; les retries et replis internes du worker sont aussi contrôlés avant émission.
- [ ] **Échéance pendant la tâche** : le worker est interrompu à l'échéance, pas seulement entre deux tâches ; **aucun appel facturé après l'échéance** (assertion sur l'horodatage des appels du double).
- [ ] **Mort du client Docker** : le conteneur est nommé et tué ; il ne continue pas de dépenser en arrière-plan (test avec un Docker factice ; pas de Docker lourd local).
- [ ] **Périmètre de la garantie** : liste explicite des transports **garantis** et **non garantis** dans `w2-budget.md`. Un transport dont les appels ou les coûts ne sont pas bornables est **refusé explicitement en mode strict**. Un compteur après réponse, ou un simple timeout, ne borne pas le coût et ne doit pas être présenté comme une garantie. Si la clé facturable reste accessible aux commandes du workspace qui peuvent joindre librement le fournisseur, la garantie ne couvre pas un programme malveillant ; toute garantie dans ce cas exige d'empêcher la dépense hors du canal réservé. Relire chaque phrase « garanti », « strict » et « plafond dur » de la documentation et des messages.
- [ ] Les imports ajoutés aux scripts worker (`oh_runner.py`, `oh_sampler.py`) sont listés ; aucun `import collegue…` sans `COPY` correspondant ; le script s'importe dans un dossier isolé qui ne contient que lui (pas de package `collegue`).

### 3.5 Distribution : wheel, ressources, migrations (B)

- [ ] Le wheel est construit depuis les **fichiers suivis** (export propre), puis installé **non éditable** dans un environnement vierge, avec les locks. Exécution depuis un dossier **hors checkout**, `PYTHONPATH` nettoyé ; `collegue.__file__` désigne `site-packages` (chemin effectif vérifié).
- [ ] Contenu du wheel : YAML / JSON, `SKILL.md`, templates, graines de prompts et migrations présents (la base : 237 entrées, **zéro** de chacun) ; comparer avec la liste annoncée par B, pas avec une liste écrite après coup. Sonde du manager `manager_w2_wheel_probe.py` (hors dépôt) rejouée.
- [ ] Aucune ressource ne dépend du cwd, d'un répertoire `/app` ni d'un lien vers le checkout ; un test qui masque le checkout (autre cwd, arbre source renommé) échoue si une ressource est oubliée.
- [ ] Les overrides opérateur explicites existants (variables / chemins) sont respectés.
- [ ] Le stockage modifiable de prompts vit dans l'état applicatif séparé ; les graines sont dans le package ; aucune écriture dans `site-packages`.
- [ ] **Migrations** : exécution documentée depuis le package installé et un cwd quelconque ; `upgrade head` sur SQLite vide, puis seconde exécution idempotente, puis reprise d'une base déjà à `0010`. Les IDs `0001`–`0010` et leurs `down_revision` sont inchangés.
- [ ] `alembic/env.py` insère aujourd'hui la racine du dépôt dans `sys.path` (`Path(__file__).parents[1]`) : ce raccord ne doit plus masquer l'installation ; vérifier que le chemin fonctionne sans checkout.
- [ ] Les usages existants suivent le nouvel emplacement : `integration-nightly.yml` (`python -m alembic upgrade head`), `tests/test_project_state.py`, `tests/test_phase5_incident_state.py` (`REPO_ROOT / "alembic"`), documentation et README.
- [ ] **Sensibilité Phase 5 conservée** : `pilot/automerge.py::is_sensitive` bloque les migrations par les segments `alembic`+`versions` ou `migrations`. L'emplacement choisi par B doit rester classé sensible (vérifier par appel direct de `is_sensitive` sur le chemin final) ; sinon c'est un affaiblissement de la Phase 5, à remonter (le fichier est hors lots W2).
- [ ] Import et parcours minimal du serveur depuis le wheel, sans appel LLM, réseau coupé si nécessaire.

### 3.6 Verrouillage des dépendances (B)

- [ ] `pyproject.toml` est la **source unique** (runtime, extras, dashboard, contraintes de sécurité existantes) ; les divergences de la base (dashboard, `aiohttp`) sont résolues ; aucun fichier de requirements écrit à la main ne redevient une deuxième source.
- [ ] Mécanisme de verrouillage documenté, reproductible, compatible Python **3.11 et 3.12** (versions réellement exécutées déclarées) ; version de l'outil épinglée.
- [ ] **Dérive source → lock détectée** : une dépendance modifiée dans `pyproject.toml` sans régénération fait échouer la vérification (mutation à reproduire par C).
- [ ] CI et Docker **installent depuis les locks** : `git grep -nE 'pip install (-r requirements|[^-]*>=)'` dans workflows et Dockerfiles ne laisse aucune installation flottante ; `ruff>=0.15.0` du job Ruff est à traiter (pin hérité de la vague 1) ou documenté.
- [ ] Fermeture OpenHands : groupe / lock séparé si incompatible avec le serveur ou le pytest de développement ; **patch `TerminalAction` non désactivé** ; préimage 1.19.1 validée ; `lmnr==0.7.52` et OpenHands `1.7.0` verrouillés ; pas d'installation OpenHands complète locale (preuve = CI).
- [ ] `Dependency audit` (`pip-audit --strict`) vérifie l'environnement issu des locks.
- [ ] Empreintes des locks et du wheel consignées dans le rapport de B.
- [ ] Les noms de locks sont à confronter à la Phase 5 : `is_sensitive` classe `*.lock` et `pyproject.toml` comme sensibles, mais **pas** `requirements.txt`, `requirements-lock.txt` ni `Dockerfile.openhands` à la base. Le choix de nom n'est pas bloquant pour la vague, mais l'écart est à rapporter au manager (la politique de fusion relève de la vague 3).
- [ ] Si `pip-audit` est embarqué dans l'image sandbox : portée documentée sans prétendre à une base de vulnérabilités hors réseau ; une indisponibilité reste un **échec de mesure**, jamais un zéro.

### 3.7 SQLite et PostgreSQL : mesures séparées

- [ ] Les résultats SQLite et PostgreSQL sont rapportés **séparément**, avec version du moteur et du pilote ; jamais agrégés en une seule ligne verte.
- [ ] Le test de concurrence PostgreSQL s'exécute sur un **vrai service**, déterministe, sans secret ni appel LLM, dans la CI de PR. S'il tourne dans un job non requis, son échec ou son absence ne bloque pas la fusion : le dire et demander au manager où le rattacher (étape d'un des cinq checks requis, ou autre décision) ; ne pas inventer un sixième check obligatoire.
- [ ] Un test PostgreSQL qui se saute faute de service est un **manque de preuve**, pas un succès.
- [ ] Les verrous de ligne / contraintes sont ceux de la base, pas ceux du process (rejouer avec deux processus).

### 3.8 Transverse

- [ ] Intersection des fichiers de A et B vide ; aucun fichier hors périmètre sauf raccordement signalé.
- [ ] Aucun test affaibli, `xfail` ou skip opportuniste ; nombre de tests passés non décroissant ; skips listés et comparés à la base (36 hors vague : clés API, endpoints legacy, serveur `:8088`, tests hérités).
- [ ] Aucun secret, aucune valeur `INTEGRATION_*` dans les fichiers ou journaux ajoutés ; aucun appel payant ; le nightly n'est pas déclenché.
- [ ] Documentation alignée (`w2-budget.md`, `w2-installation.md`, README, `.env.example`) ; pas de promesse plus forte que la preuve.
- [ ] Les limites de chaque rapport sont reportées telles quelles dans la description de PR.

## 4. Vérifications de C sur le candidat intégré

Depuis la racine du worktree, avec le venv de C (dépendances partagées en lecture seule ; caches et artefacts propres sous `/tmp/w2c-*`, rien dans `~/.cache`) :

1. Rouge indépendant sur `9862b39` : tests de défaut de A et de B copiés à l'identique sur un export de la base (dépôt jetable sous `/tmp`) ; sondes du manager (budget, wheel) rejouées ; assertions de comportement, pas d'`ImportError`.
2. Suite complète hors `integration` (`-p no:cacheprovider -rfEs`, journal intégral, code retour réel) ; comparer passés / skips à la base (2800 / 36 / 9).
3. `ruff check` et `ruff format --check` sur `collegue tests` (arbre complet) ; `sh -n` / `dash -n` / `bash -n` des scripts modifiés ; `git diff --check`.
4. Wheel construit depuis un export propre sous `/tmp`, installation non éditable dans un venv jetable, exécution hors checkout, migrations jusqu'à `head` ; seulement si l'espace le permet (≈1,6 Go libres sur `/`, ≈6,8 Go sous `/tmp`) ; sinon le dire et ne pas présenter la preuve comme faite. Pas d'installation OpenHands complète ni de build Docker local.
5. Sondes d'intégration propres à C : `is_sensitive` sur les chemins finaux (migrations, locks), grep des installations flottantes, grep des imports des scripts worker, parité modèle ↔ migration.
6. Rapport `reports/w2-c-integration.md` : SHA final, diff exact des raccordements, commandes avec codes retour, SQLite et PostgreSQL séparés, limites. Aucune preuve manquante présentée comme réussie.

## 5. Livraison (après validation Codex)

Push, PR vers `main` (base à vérifier), description en français (`--body-file`) tirée des rapports et de ce document, observation des cinq checks **sur la tête publiée** (pas ceux d'un commit précédent) et des revues disponibles, rapport au manager, arbitrage de tout finding, puis instruction de squash sur un SHA précis et vérification de `main` (parent, arbre identique, cinq checks du push). Copilot en quota ou Codex GitHub non redéclenché : absences à nommer, ni favorables ni prérequis.

## 6. Limites de cette préparation

- Rédigée sans avoir lu les rapports de A et B ; aucune de leurs décisions (emplacement des migrations, nom des locks, format du registre, commande PostgreSQL) n'est supposée ici.
- Les noms de sondes du manager sont cités à titre de référence ; elles restent hors dépôt.
- La suite complète de la base n'a pas été rejouée pour ce travail documentaire (dernière preuve : vague 1, 2800 / 36 / 9 sur `66f12f8`, puis `main` `9862b39` verte en CI).

## 7. État intégré (vague 2)

Section ajoutée à l'intégration ; les résultats chiffrés, les SHA et les preuves sont dans `reports/w2-c-integration.md`
(hors dépôt). Les §1 à §6 ci-dessus décrivent la préparation et restent le protocole.

- **Contributions** : lot B `23de03d`, lot A `d0b0aab`, fusionnés `--no-ff` par SHA (B d'abord) sur la base `9862b39`.
  Le correctif d'usage Python 3.11 de A (`5b8a438`, `sample_with_timeout` : `asyncio.timeout` à la place de `asyncio.wait_for`)
  est intégré par un second merge `--no-ff` : sous 3.11, `wait_for` exécute la coroutine dans une tâche enfant et l'usage
  écrit dans la `ContextVar` y restait enfermé. Les suites complètes sont exécutées sous **3.11 et 3.12** avec les verrous.
  Le correctif `uv` de B (`d72446b`, `uv==0.11.33` dans le job d'audit, la résolution des verrous et l'image OpenHands) est
  intégré par un troisième merge `--no-ff` : le job `Dependency audit` de la PR #610 était rouge sur `5d37700` parce que
  `uv 0.9.28` (GHSA-pjjw-68hj-v9mw, GHSA-4gg8-gxpx-9rph) était installé dans l'environnement audité. L'audit n'est pas
  contourné. `pyproject.toml` et les six verrous sont inchangés ; leur ligne d'en-tête `# uv: 0.9.28` reste vraie comme
  provenance historique (informative, ignorée par la comparaison), `generate` écrira `0.11.33`. Version unique :
  `UV_VERSION` de `scripts/locks.py`, vérifiée par test contre le workflow et le Dockerfile.
- **Raccords mécaniques réalisés** (commits séparés) :
  1. `0011_budget_ledger.py` : `alembic/versions/` → `collegue/migrations/versions/`, mêmes identifiants (`0011`/`0010`), blob
     identique à celui de A au moment du déplacement (le fichier contient la table `budget_blocks` et les champs de claim) ;
  2. chemins `script_location` des tests de A (`test_budget_ledger.py`, `test_budget_ledger_postgres.py`) et références
     documentaires A/B vers `collegue/migrations` ;
  3. réordonnancement d'imports seul de `0011` et de ces deux tests (le dossier racine `alembic/` n'existe plus, `alembic`
     devient un paquet tiers pour isort et `collegue/` est dans le périmètre Ruff) : AST de `0011` identique à celui de A ;
  4. `tests/test_project_state.py` : union des quatre tables du registre (A) et des chemins empaquetés (B) ;
  5. CI : service `postgres:16` et étape dédiée dans le job `Pytest` requis (3.11 et 3.12), garde
     `scripts/ci_require_junit.py` (rapport JUnit complet, plancher 21, 0 skip, 0 échec), test du câblage
     `tests/test_ci_postgres_budget.py`.
- **Sensibilité Phase 5** : le segment `versions` du nouveau chemin conserve la classification sensible des migrations ;
  `scripts/ci_require_junit.py` et `collegue/migrations/**` sont sensibles ; les fichiers `locks/*.txt` et les `Dockerfile.*`
  ne le sont toujours pas (écart connu, traité par la politique de fusion de la vague 3).
- **Hors CI locale** : Python 3.11 et l'image Docker (smoke compris) restent prouvés par les cinq checks de la PR ; le plancher
  PostgreSQL (21) doit être relevé quand A ajoute des cas, jamais abaissé pour masquer une perte.
