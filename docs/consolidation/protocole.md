# Protocole de consolidation — plan opérationnel

Les règles durables (rôles, worktrees, propriété des fichiers, niveaux de tests, conditions de merge,
budget) sont dans [`AGENTS.md`](../../AGENTS.md). Ce document en donne l'application : vagues,
contrats, procédure d'intégration et **checklist de revue indépendante**, propre à chaque faille.

- Base : `main` = `51ab3fcce70f30c1a50a4dcde334ca1269582f19`.
- Audit de référence : 2351 passed, 36 skipped, 9 deselected ; couverture 72,98 % ; `ruff check` OK ;
  `ruff format --check` en échec (exemples Python de `collegue/client/README.md`, Ruff 0.16.9) ; Python 3.12.12.
- Référence de campagne : `~/.codex/collegue-consolidation/20260928/` (`PLAN.md`, `briefs/`, `reports/`, `evidence/`).

## 1. Vagues

Chaque vague est livrée et vérifiée sur `main` avant la suivante. (La vague 4 est livrée mais sa validation réelle avec modèles est restée incomplète : la vague 5 la reprend.)

| Vague | A | B |
|---|---|---|
| 1 | Fermer la frontière Git/sandbox sur **tous** les chemins (capture/recapture, seed/retry, compounding, snapshots, revert) | OAuth fail-closed, profil réseau local sur loopback, nightly et smoke Docker honnêtes |
| 2 | Registre de budget transactionnel durable (BUILD/IMPROVE/tous rôles/retries), idempotence, réservation avant appel, worker borné + deadline, reprise | Wheel complet, ressources et migrations installables hors checkout, dépendances unifiées et verrouillées depuis `pyproject.toml` |
| 3 | Preuve de livraison commune BUILD/IMPROVE, contrats scellés, revue bloquante, couverture sans régression, preuve négative d'oracle (exit 1 puis exit 0, même hash) | `BUILD_AUTO_MERGE` à `false` par défaut, SHA/checks/base/resync communs, rulesets GitHub, reprise durable d'une fusion déjà réussie, restrictions Phase 5 conservées |
| 4 | Routage fournisseur/modèle/endpoint/auth par rôle (coder et sampling), pas de fuite de clé vers un autre provider, contradictions explicites | MVP fixture FastAPI/SQLite/Alembic à trois tâches dépendantes, DB vierge, redémarrage, amélioration, incident et rollback Phase 5 |
| 5 | Courtier budgétaire (`LLM_TRANSPORT=budget_broker`) : clé uniquement dans le service, codeur `--network none` + relais, `countTokens`→réservation→génération, état durable `0013`, deadline persistée | Câblage métier réel : R04 amélioration livrée, R05 incident déterministe avec vrais contrôles, nettoyage unique après toutes les phases, validation du socle de la fixture par l'API, identité de campagne consommée (C : CI, image, fixture, workflow) |

## 2. Contrats (invariants à ne pas affaiblir)

- **Git** : métadonnées de contrôle hors des montages inscriptibles par le codeur et les tests ; aucun Git hôte sur des métadonnées influençables par le workspace. Livraison depuis un SHA de base fiable ; configs/hooks/redirects non fiables exclus. Opérations post-codeur par un chemin isolé commun.
- **OAuth** : demandé mais indisponible ⇒ le démarrage est interdit. Profil local publié sur loopback.
- **Budget** : le registre durable fait autorité par projet/cycle ; migration additive qui importe une fois les cumuls ; réservations atomiques ; événements idempotents ; usage inconnu conserve la réservation et bloque le mode strict ; un redémarrage ne remet rien à zéro. Coût strict seulement si les appels sont bornables.
- **Preuve de livraison** : elle lie contenu, SHA/hashes, oracles et verdicts. BUILD contrôle la tâche courante et les contrats livrés ; IMPROVE conserve tous les contrats livrés. Un score composite ne compense ni un veto qualité, ni une mesure indisponible, ni une couverture en baisse.
- **Merge BUILD** : opt-in, invariants communs avec la Phase 5 sans affaiblir l'allowlist de celle-ci. Checks requis découverts via rulesets et protections. Échec de resync après merge ⇒ pause durable, ni nouvelle fusion ni exécution sur base périmée.
- **Distribution/config** : `pyproject.toml` source des dépendances, exports verrouillés pour CI/Docker, ressources embarquées. La config d'un rôle résout provider/model/endpoint/auth **ensemble** ; une clé globale n'est héritée que pour le même provider.

## 3. Cycle d'une vague

1. Le manager publie les briefs (périmètre par fichier, acceptation, rapports attendus).
2. A et B implémentent en parallèle, commits locaux, rapports `w<N>-a.md` / `w<N>-b.md`.
3. Le manager donne à C les **SHA** de A et B.
4. C intègre (§4), applique la checklist (§5), lance le niveau 2 et écrit `w<N>-c.md`.
5. Codex teste le SHA intégré sur un checkout propre (niveau 3). Une correction est renvoyée à son auteur (ou raccordement mécanique par C) ; on reprend en 4 sur le **nouveau** SHA.
6. Sur consigne du manager : C pousse la branche de vague et ouvre la PR vers `main`.
7. **Étape 1 (C)** : C observe les 5 checks requis et les revues sur la tête exacte, puis rapporte au manager URL, SHA tête/base, résultat de chaque check, état de chaque revue et texte de chaque finding, avec son évaluation. Il ne fusionne pas.
8. **Arbitrage (Codex)** : le manager examine ce rapport. Tout finding pertinent pour les critères de la vague est tranché explicitement (correction maintenant, ou limite justifiée) **avant** toute fusion. Une correction change le SHA et repasse par les étapes 4 à 7.
9. **Étape 2 (C)** : sur l'instruction finale de Codex pour cette tête exacte, C fusionne (squash, tête attendue contrôlée, aucun bypass). La méthode de fusion est celle que les protections du dépôt autorisent.
10. Vérification de `main` après fusion (SHA, arbre identique à l'arbre accepté, 5 checks du push) avant la vague suivante.

Une revue absente, en cours ou en échec (quota) et un check manquant ou ignoré sont rapportés tels quels : ils ne valent jamais succès.

## 4. Procédure d'intégration (C)

Les worktrees partagent le même dépôt local : les branches de A et B sont visibles sans réseau.

```bash
BASE=51ab3fcce70f30c1a50a4dcde334ca1269582f19
# 0. Le SHA fourni par le manager doit être la tête actuelle de la branche, et descendre de la base
git rev-parse codex/consolidation-w<N>-a          # = SHA annoncé ?
git merge-base --is-ancestor $BASE <SHA_A> && echo ok

# 1. Périmètres disjoints : l'intersection doit être vide
comm -12 <(git diff --name-only $BASE <SHA_A> | sort) <(git diff --name-only $BASE <SHA_B> | sort)

# 2. Fusion par SHA (jamais par une branche encore mobile), sans fast-forward
git merge --no-ff <SHA_A> -m "Intégrer le lot A de la vague <N>"
git merge --no-ff <SHA_B> -m "Intégrer le lot B de la vague <N>"
```

- Un conflit textuel entre A et B signale une violation de propriété : ne pas le résoudre par choix arbitraire, le renvoyer au manager.
- Résoudre seulement les raccordements **mécaniques** (import, signature, renommage) et les consigner dans le rapport.
- Niveau 2, depuis la racine du worktree avec le venv de C :

```bash
PY=~/.codex/collegue-consolidation/20260928/envs/c/bin/python
$PY -m pytest -p no:cacheprovider                       # suite complète, marqueur integration exclu
$PY -m ruff check --no-cache collegue tests
$PY -m ruff format --check --no-cache collegue tests
git status --short                                      # l'arbre suivi doit rester propre
```

- Comparer au dernier résultat connu (§ en-tête) : les *passed* ne baissent pas, les *skipped* n'augmentent pas sans raison écrite, aucun seuil de couverture n'est abaissé.
- Les builds Docker passent par `flock ~/.codex/collegue-consolidation/20260928/heavy.lock` ; sinon la preuve de build est celle de la CI distante.

### Rouge sur la base

Pour chaque test de défaut annoncé par un auteur, C vérifie qu'il échoue **sur la base, pour la bonne raison** :

```bash
TMP=$(mktemp -d) && git archive $BASE | tar -x -C "$TMP"
for f in $(git diff --name-only --diff-filter=AM $BASE <SHA> -- tests); do
  mkdir -p "$TMP/$(dirname $f)" && git show <SHA>:$f > "$TMP/$f"
done
(cd "$TMP" && $PY -c "import collegue; print(collegue.__file__)" && $PY -m pytest -p no:cacheprovider <tests>)
```

Un rouge dû à un `ImportError`/`AttributeError` sur une interface qui n'existe pas encore **ne prouve pas** le défaut : exiger l'assertion sur le comportement (témoin hôte créé, `app.auth is None`, statut CI vert malgré un échec). Comparer aussi le hash du fichier de test entre l'exécution rouge et l'exécution verte.

## 5. Checklist de revue indépendante — vague 1

Pour chaque ligne : la preuve attendue est un test (ou une sortie conservée) qui **échoue sans le correctif**. « Le code semble correct » n'est pas une preuve.

### 5.1 Frontière Git/sandbox (lot A)

Le défaut de l'audit : `run_issue` lance `git add`/`git diff` sur l'hôte via `LocalCommandRunner` dans un workspace monté en écriture dans le conteneur ; un `core.fsmonitor` écrit par l'agent s'exécute sur l'hôte.

**Vecteurs à couvrir** (un test rouge→vert chacun, ou une justification explicite de non-applicabilité) :

- [ ] Config du dépôt : `core.fsmonitor`, `core.hooksPath`, `core.pager`, `core.editor`, `core.sshCommand`, `core.gitProxy`, `core.worktree`, `diff.external`, `diff.<x>.textconv`, `filter.<x>.clean|smudge|process`, `merge.<x>.driver`, `alias.*`, `credential.helper`, `url.*.insteadOf`.
- [ ] Inclusion de config : `include.path`, `includeIf`, y compris vers un fichier du workspace ou hors de celui-ci.
- [ ] Hooks : `.git/hooks/*` (dont `pre-commit`, `post-checkout`, `post-merge`, `reference-transaction`, `fsmonitor-watchman`).
- [ ] Redirection du dépôt : `.git` remplacé par un **fichier** `gitdir: …` ou un **lien symbolique**, `commondir`, `objects/info/alternates`, `GIT_DIR`/`GIT_*` hérités de l'environnement hôte, config globale et système.
- [ ] Autorité de la base : `HEAD`, refs, `packed-refs` et index modifiés par le codeur ou par les tests **ne changent ni la base, ni le diff livré**. Index piégé (`skip-worktree`, `assume-unchanged`) : le changement caché reste visible ou la capture échoue.
- [ ] Attributs : `.gitattributes`, `.git/info/attributes`, `.gitmodules` référençant des drivers/filtres/sous-modules.
- [ ] Liens symboliques du workspace qui sortent de celui-ci : la capture ne les suit pas, ne lit ni ne publie de contenu hôte.
- [ ] Contenu bénin **inchangé** : modification, ajout, suppression, renommage, fichier binaire, bit exécutable, noms avec espaces/unicode, gros diff, workspace repris. Le diff capturé est publiable et identique à celui d'avant.

**Chemins à couvrir** (chaque chemin passe par la même frontière) :

- [ ] `prepare_workspace` puis `implement_issue` hostile puis capture — cycle **réel**, pas un mock de `run_command`.
- [ ] Recapture après exécution des tests ; vérification de stabilité du snapshot (`executor/pr.py`).
- [ ] Seed/retry (`apply_seed_diff`) et compounding IMPROVE (diff cumulatif).
- [ ] Revert (`executor/revert.py`, `prepare_revert`) et cleanup/resync (`resync_repository_base`).

**Recherche des usages oubliés** (à relancer sur le SHA intégré ; l'inventaire de A est dans `docs/consolidation/w1-isolation.md` et épinglé par `test_host_subprocess_usage_is_inventoried`) :

```bash
git grep -nE 'LocalCommandRunner|runner or |run_command\(\[' -- collegue
git grep -nE '"git"|git_bin|subprocess.*git' -- collegue
```

- [ ] Plus aucun `runner or LocalCommandRunner()` qui sert de défaut de production sur un workspace non fiable ; les runners locaux injectés restent des fixtures de confiance dans les tests. Pas de repli silencieux vers un runner local si l'isolation est indisponible : **erreur explicite** (fail closed).
- [ ] Sites qui invoquent Git à la base, **hors périmètre déclaré de A**, à examiner un par un (le répertoire visé est-il jamais monté en écriture dans le conteneur ?) : `pilot/guard.py` (`clone`, `cat-file`, `checkout --detach`, `rev-parse` sur le clone, tous **avant** `sandbox.run_tests` à la base : vérifier qu'aucun Git hôte ne s'exécute après les tests), `pilot/remote_revert.py` (`status`, `rev-list`, `rev-parse` sur le workspace de revert), `pilot/runtime.py` (`_resync_repo_source`, clone local de l'utilisateur), `pilot/nightly_e2e.py` (`remote get-url`, `rev-parse`), `autonomous/proactive_monitor.py`, `improve/loop.py` (`LocalCommandRunner`). Un site laissé tel quel doit être justifié par écrit dans le rapport de A ; un changement de `pilot/` est un raccordement à signaler.
- [ ] La sonde de l'audit (`/tmp/collegue-review-gSxXex/git_boundary_probe.py`, chemins fixes) est rejouée sur des dépôts **neufs** ; le témoin hôte est absent après correction. Les témoins restent dans des répertoires temporaires.
- [ ] `collegue/sandbox/executor.py` : `-v {ws}:/workspace` reste le seul montage hôte ; le nouveau dispositif ne monte pas les métadonnées de contrôle en écriture ni ne monte un second chemin hôte.

### 5.2 OAuth fail-closed et exposition réseau (lot B)

Le défaut de l'audit : `app.py` journalise puis garde `auth_provider = None` ; FastMCP démarre sans authentification.

- [ ] **Trois** chemins permissifs à la base, tous à rendre bloquants quand `OAUTH_ENABLED=true` : (1) `ImportError` de `JWTVerifier` (simple `warning`), (2) exception du constructeur (`auth_provider = None`), (3) ni `OAUTH_JWKS_URI` ni `OAUTH_PUBLIC_KEY` (simple `warning`). Le brief B n'en cite que deux : vérifier le troisième.
- [ ] Le test échoue sur la base pour la bonne raison (`app.auth is None` après import), et le démarrage échoue **réellement** : import qui lève, pas seulement un log d'erreur.
- [ ] Le mode local sans OAuth reste possible, **explicite** et testé (`OAUTH_ENABLED=false`), avec un message clair ; un OAuth valide (JWKS, clé publique) démarre avec `app.auth` non nul.
- [ ] Niveau conteneur : `entrypoint.sh` (mode http) sortait en code 0 après `wait $MCP_PID` + `cleanup` et affichait « All services started successfully! » sans condition. Ruling manager : le fichier appartient à B en vague 1. Vérifier que le code exact de `fastmcp run` est restitué (jamais converti en 0 par `cleanup`), qu'un MCP sorti avant d'être prêt vaut un échec, que « prêt » exige le health server **et** le MCP (`mcp-ready` : 2xx, 401 ou 403 seulement), et que le healthcheck Compose utilise cette sonde.
- [ ] `PILOT_TOOL_ENABLED` : `pilot/mcp_tool.py` se fie au drapeau `OAUTH_ENABLED`. Cohérent seulement si le drapeau implique désormais une authentification effective.
- [ ] Loopback : **tous** les ports publiés de `docker-compose.yml` à la base (4121, 4122, 4123, 4125, 8088), pas seulement le port MCP, sont limités à `127.0.0.1` par défaut. L'exposition distante est une action explicite (variable ou override) documentée avec OAuth. `MCP_HOST: 0.0.0.0` à l'intérieur du conteneur est conservé. `tests/test_docker_compose_config.py` couvre le résultat.
- [ ] Docs alignées : `README.md`, `README.en.md`, `.env.example`, `docs/moteur_autonome.md`, `content.md` ne décrivent plus une exposition ou un défaut devenu faux.

### 5.3 CI fidèle (lot B)

Les défauts de l'audit : `pytest | tee` sans `pipefail` ; « Bilan » qui accepte `passed|failed|error` ; smoke Docker `docker run -d … &` puis `|| true`.

- [ ] Le test du pipeline exécute le **texte réel** de l'étape du workflow (extrait du YAML) avec le shell de GitHub — sans `shell:` explicite c'est `bash -e {0}` (sans `pipefail`), avec `shell: bash` c'est `bash --noprofile --norc -eo pipefail {0}`. Un test qui réimplémente la commande ne prouve rien. Cas obligatoire : producteur `exit 1` + `tee` `exit 0` ⇒ échec global.
- [ ] Le statut de pytest est propagé exactement, sans dépendre du wording (`failed`/`error`/`passed`) ; l'analyse structurée (code de sortie, JUnit XML) est préférée au grep du résumé.
- [ ] `integration-report.xml` et `pytest-integration.log` sont conservés en échec (`if: always()`).
- [ ] L'E2E produit non exécuté (`vars.INTEGRATION_E2E_ENABLED != 'true'`) est visible comme **non exécuté** dans le run (résumé, annotation) et jamais présenté comme une preuve verte. À la base, `notify-failure` traite `skipped` comme un non-échec et rien d'autre ne signale l'absence de preuve.
- [ ] Le test réel qui attend 2 délégations et en obtient 3 n'est **pas** assoupli pour passer ; l'échec devenu visible est rapporté avec sa cause connue.
- [ ] Smoke Docker : prêt à recevoir (santé/MCP), attente bornée, sortie prématurée = échec, logs conservés, nettoyage exécuté **sans** écraser le statut initial ; plus de `|| true` sur le statut, plus de `docker run -d … &`. Le test utilise un Docker stub : crash, indisponible, succès, plus logs et nettoyage vérifiés.
- [ ] Les 5 noms de checks requis sont inchangés : `Ruff`, `Pytest (Python 3.11)`, `Pytest (Python 3.12)`, `Dependency audit`, `Docker build`. Un renommage casserait le ruleset sans qu'on le voie.
- [ ] `collegue/client/README.md` : `ruff format --check collegue tests` passe sur l'arbre complet (échec connu à la base : 1 fichier à reformater).
- [ ] Aucun secret ni valeur `INTEGRATION_*` dans les fichiers ou les logs ajoutés.

### 5.4 Transverse (chaque vague)

- [ ] Intersection des fichiers modifiés par A et B vide ; aucun fichier hors du périmètre du brief, sauf raccordement signalé.
- [ ] Aucun test modifié ou supprimé pour « faire passer » : lister les assertions retirées ou assouplies. `git diff $BASE -- tests | grep -nE '^\+.*(xfail|skip|importorskip)'` est vide ou justifié.
- [ ] Le nombre de tests passés ne baisse pas ; les *skipped* n'augmentent pas ; aucune exclusion ajoutée dans `pyproject.toml`.
- [ ] `git status --short` propre après les tests ; aucune écriture hors des répertoires temporaires ; aucune sortie de secret dans les journaux.
- [ ] Docs/commentaires qui décrivent un comportement remplacé (par ex. « NON raccordé », « défaut `True` ») mis à jour dans le même lot.
- [ ] Le rapport de chaque auteur indique les limites et les points non couverts ; C les reporte tels quels, sans les arrondir.

## 6. Points d'attaque des vagues suivantes

À détailler dans la checklist de la vague au moment de l'intégration ; les points ci-dessous fixent l'attention dès maintenant.

**Vague 2 — A (budget).** Rejouer les deux sondes de l'audit : deux tâches à 0,60 $ séparées par une fusion sous un plafond de 1 $ (le contrôleur répondait `continue`), deux tentatives IMPROVE à 0,70 $. Inventaire de **tous** les sites d'appel LLM (coder, reviewer, QA, planner, juge, sampling) : chacun réserve avant l'appel et règle après. Un même événement rejoué ne débite qu'une fois ; crash entre réservation et règlement ; usage inconnu ⇒ réservation conservée et mode strict bloqué ; redémarrage sans remise à zéro ; migration additive rejouée deux fois n'importe pas deux fois ; providers gratuits (coût 0) protégés par le budget de tokens ; worker OpenHands borné par la deadline restante.
**Vague 2 — B (distribution).** Construire le wheel dans un environnement jetable et lister l'archive : templates YAML/JSON, `SKILL.md` et migrations Alembic présents (absents à la base). Installer hors checkout (autre `cwd`, sans `PYTHONPATH`) puis importer les ressources et appliquer les migrations. `requirements.txt`/`pyproject.toml` ne divergent plus (dashboard, `aiohttp`) ; CI et Docker consomment le même export verrouillé ; `pip-audit` passe. Build Docker via `flock`.

**Vague 3 — A (preuve).** BUILD et IMPROVE partagent la même preuve ; les oracles scellés sont rejoués après chaque amélioration ; un contrat livré altéré par le codeur est détecté ; couverture 90 % → 80 % refusée quel que soit le score ; preuve négative : même test, même hash, exit 1 avant puis exit 0 après ; veto qualité non compensable.
**Vague 3 — B (merge).** `BUILD_AUTO_MERGE` à `false` partout (`config.py`, `.env.example`, compose, docs, tests) ; fusion avec le SHA approuvé, checks requis **découverts** (rulesets et protections, pas une liste figée), PR modifiée après le gate refusée ; échec de resync ⇒ pause durable et aucune nouvelle fusion ; reprise après une fusion déjà réussie sans double fusion ; l'allowlist Phase 5 n'est pas élargie.

**Vague 4 — A (routage).** `LLM_PROVIDER_CODER=openai` + `LLM_MODEL_CODER=gpt-5.4` ne produit plus `gemini/gpt-5.4` ; aucune clé d'un provider n'est envoyée à un autre (capturer arguments et en-têtes avec un double) ; configuration contradictoire ⇒ erreur explicite ; le sampling suit le rôle (client et endpoint), pas seulement le nom du modèle.
**Vague 4 — B (MVP).** Fixture FastAPI/SQLite/Alembic à trois tâches dépendantes ; DB vierge ; redémarrage ; amélioration ; incident ; rollback autorisé Phase 5 ; oracles rouges avant intervention et verts après. Aucun appel réel avant la validation du manager.

## 7. Campagne réelle finale

Une seule campagne, plafonnée à **2 USD au total, 250 000 tokens et 900 secondes**, sans relance payante automatique, sur un dépôt fixture dédié. Ce budget est distinct des quotas Claude Code. Rien ne démarre avant la fin de la vague 4 et la validation du manager. Une preuve manquante rend la validation incomplète, jamais réussie par un skip.

## 8. Bilan d'intégration de la vague 1

Base `51ab3fc` ; lot A `f146915` ; lot B `329a11a` ; intégration par merges locaux `--no-ff` par SHA (aucun commit
de A ou de B réécrit). Intersection des fichiers modifiés par A et B : **vide**. Aucun conflit textuel. Les preuves
(journaux intégraux avec code retour) sont sous `evidence/w1-c-*` et le rapport `reports/w1-c.md`.

### Interfaces à connaître

- Git : `collegue.executor.git_boundary` (`TrustedGit`, `HardenedGitRunner`, `require_trusted_checkout`),
  `workspace.trusted_base` / `advance_base`, `sandbox.paths.workspace_file`. `Workspace(path, branch, base_commit)` inchangé.
  Un workspace géré a un répertoire de contrôle frère `<workspace>.control` (jamais monté). `runner=None` = production.
- OAuth : `collegue.core.server_auth.build_auth_provider` / `OAuthConfigurationError` ; `OAUTH_ALGORITHM` non vide
  obligatoire ; `OAUTH_ALGORITHM` et `OAUTH_REQUIRED_SCOPES` transmis à `JWTVerifier`.
- Réseau : `COLLEGUE_PUBLISH_HOST` (4121, 4122, 8088), `COLLEGUE_DASHBOARD_PUBLISH_HOST` (4125),
  `COLLEGUE_KEYCLOAK_PUBLISH_HOST` (4123), tous `127.0.0.1` par défaut. `Settings.HOST` vaut `127.0.0.1`.
- CI : `scripts/ci_docker_smoke.sh`, `scripts/ci_integration_bilan.py`, job `e2e-status`, artifact `docker-smoke-logs`.
  Les 5 noms de checks requis sont inchangés.

### Changements de comportement à annoncer dans la PR

1. Les scopes de `OAUTH_REQUIRED_SCOPES` et l'algorithme sont désormais **imposés** : des jetons sans ces scopes sont refusés.
2. `docker compose up` ne publie plus rien hors loopback ; l'exposition distante est un choix explicite.
   Elle n'est **pas** bloquée sans OAuth : avertissement seulement.
3. Un OAuth demandé mais inutilisable interdit le démarrage ; en Docker le conteneur sort en code non nul
   (`restart: always` le relance : lire les logs).
4. Un workspace non géré fait lever `WorkspaceError` (plus de repli silencieux sur `LocalCommandRunner`) ; les
   workspaces antérieurs à la frontière (sans `.control`) sont refusés.
5. Le diff capturé utilise `--no-renames` : un renommage supprime l'ancien fichier dans la PR.
6. Le nightly est fidèle à pytest : il devient rouge sur un échec réel ; l'E2E produit non exécuté est annoncé
   « NON EXÉCUTÉ » (code 0, pas une preuve).

### Limites connues (à ne pas arrondir)

- **`pip-audit` est absent de l'image `docker/sandbox/Dockerfile`** : activer `dep_vulns_enabled` **refuse** donc la
  mesure (composite non fini, rejet par le gate), jamais « 0 vulnérabilité ». Aucun câblage produit n'active ce
  drapeau aujourd'hui. Ajouter l'outil à l'image est à planifier (build lourd, donc hors vague 1).
- **Python 3.11 n'est pas exécuté localement** (compilation seulement) : la preuve est le check `Pytest (Python 3.11)`.
- **Le smoke Docker n'a jamais tourné sur une image réelle** (pas de build local : 421 Mo de disque libre). La preuve
  est le check `Docker build` de la PR ; en cas d'échec, lire d'abord l'artifact `docker-smoke-logs`. Les sondes
  semi-réelles (vrai `entrypoint.sh`, vrai serveur, shim `docker`) ont passé.
- `ruff` (lint, format, `autofix_lint`) tourne toujours sur l'hôte, sur des chemins confinés ; `repo_source` et le clone
  nightly sont tenus pour fiables par construction (inventaire de A) ; le sandbox reste en `--user` hôte avec
  workspace en lecture-écriture : la vague ferme les effets **hôte** via Git, pas ceux du code non fiable dans le conteneur.
- La vue Git de l'agent est une copie complète du contrôle par tentative et par round (coût disque) ; Git LFS et la
  config utilisateur ne sont pas chargés dans les workspaces gérés ; un `repo_source` d'un autre propriétaire peut
  échouer au clone (`safe.directory` ignoré) ; les workspaces `improve` ne sont toujours pas nettoyés (fuite préexistante).
- Le test d'inventaire des sous-processus est textuel : il ne voit pas `asyncio.create_subprocess_*`.
- Les collisions de port 4122 sont possibles si plusieurs sessions lancent la suite complète en parallèle
  (`TestHealthServer` ne saute plus : il échoue).
- La campagne `integration` n'a pas été lancée (payante). Seul `test_real_delegation_engine_evaluation`, déterministe, a
  été exécuté seul, hors ligne (namespace réseau vide) ; sa clé factice ne sert qu'à lever le `skipif`.

## 9. Suivi correctif de la vague 1 (livrée partiellement, non clôturée)

**Contexte.** La PR #608 est fusionnée (`fe763b5`, arbre identique à `e996f4f`, 5 checks du push verts), mais la vague 1 **n'est pas clôturée**. La revue automatisée Codex GitHub a produit, après le rapport de C, deux findings P2 sur la tête fusionnée ; C les avait laissés dans les limites de la PR. Le manager a décidé de les **corriger avant la vague 2** et a établi, par reproduction sur `e996f4f`, que le périmètre réel est plus large (règle d'arbitrage : `AGENTS.md`, « Livraison et merge », points 5 et 6). A et B repartent de `fe763b5` sur `codex/consolidation-w1-followup-{a,b}` ; C sur `codex/consolidation-w1-followup-c`. C n'intègre leurs commits qu'une fois leurs SHA finaux communiqués par le manager.

**Constats du manager (preuves `evidence/w1-manager-followup-before.json`, `w1-manager-nested-control-candidate.json`, sonde `manager_w1_followup_probe.py`).**
- Sur `e996f4f`, la garde de montage laisse passer : un workspace ancêtre plus haut contenant `deep/project/workspace.control` ; ce même contrôle fourni comme `pip_cache_dir` ; un ancêtre fourni comme `pip_cache_dir` ; ce contrôle fourni comme `subscription_auth_dir`. Seuls le workspace et ses enfants directs étaient contrôlés.
- `entrypoint.sh` : `HEALTH_READY_ATTEMPTS=abc` et `MCP_READY_ATTEMPTS=abc` ne sont pas rejetés (`[` en erreur dans un `if` que `set -e` ignore : attente sans fin tant que le service n'est pas prêt).
- Premier candidat de correctif de A (retour du manager) : une dispense pour un workspace apparié est **contournable** par un workspace géré imbriqué sous un autre (`outer/workspace/nested/workspace.control` accepté). Toute dispense doit être exacte, pas par préfixe de chemin.

### 9.1 Checklist — montages du sandbox (lot A)

Preuve attendue pour chaque ligne : un test rouge sur `fe763b5`, vert ensuite, avec assertion de comportement (le refus lui-même), pas un `ImportError`.

- [ ] **Inventaire de tous les montages hôte** produits par `DockerSandbox._build_run_argv` : workspace (`{ws}:/workspace`), cache pip (`pip_cache_dir` → `/tmp/.pip_cache`), auth d'abonnement (`subscription_auth_dir` → `{home}/.openhands`), plus tout montage ajouté. Chacun passe par **la même** garde ; le commentaire « SEUL chemin hôte monté » (`sandbox/executor.py`) est mis à jour.
- [ ] **Autres sites `docker run`** (`git grep -nE '"docker"|docker_bin'`) : `collegue/core/llm/sampling_ctx.py` (~l. 311-335) construit son propre `docker run -v {subscription_auth_dir}:/home/sandbox/.openhands` (RW, `--network host`) **hors** `DockerSandbox`. Soit il est routé par la même garde, soit la raison de le laisser est écrite et testée. Ne pas supposer que `DockerSandbox` est le seul point de montage.
- [ ] **Positions du contrôle par rapport à la source montée**, pour chaque type de montage : la source est elle-même un contrôle (marqueur) ; contrôle enfant direct ; contrôle descendant profond (plusieurs niveaux) ; contrôle dans un ancêtre ; contrôle atteint via un lien symbolique (source résolue par `realpath`).
- [ ] **Contrôle imbriqué** : `outer/workspace/nested/workspace.control` (reproduction du manager) est refusé. Si une dispense « workspace apparié » existe, elle ne compare pas des préfixes de chemin, ne s'applique qu'au contrôle exact du workspace monté (qui est **frère** du workspace, donc hors du montage), et un test prouve qu'un contrôle d'un **autre** workspace sous le montage reste refusé, y compris sous un workspace géré légitime.
- [ ] **Bornage du scan** : profondeur, nombre d'entrées et temps bornés ; **dépassement ⇒ refus** (fail-closed), jamais « autorisé faute de temps ». Test sur un arbre volumineux (par ex. milliers de fichiers type `node_modules`) : coût raisonnable, aucun parcours de liens (`followlinks` interdit), aucune sortie du répertoire monté.
- [ ] **Erreurs fail-closed** : à `fe763b5`, `_git_control_within` fait `except OSError: return None`, soit un **échec ouvert**. Vérifier que `PermissionError`, répertoire illisible, entrée disparue pendant le scan et erreur de `lstat` refusent le montage, avec un message qui ne cite ni contenu ni noms de fichiers d'authentification.
- [ ] **Reprise et cycle de vie** : workspace repris (`kept_workspace`) ou recréé ; contrôle déposé après la validation (fenêtre entre validation et `docker run`) : limite écrite si non fermable, ou validation refaite à la construction de l'argv. Tous les points d'entrée (`run_command`, `run_tests`, appel direct de `_build_run_argv`) traversent la garde.
- [ ] **Non-régression bénigne** : workspace ordinaire avec `.git` ordinaire, cache pip partagé ordinaire hors de tout workspace, répertoire d'auth ordinaire, workspace géré légitime (contrôle frère hors montage) : toujours montés. Chemins contenant `:` toujours refusés pour les trois montages.
- [ ] **Sonde du manager** : `python evidence/manager_w1_followup_probe.py <worktree> <sortie.json>` rend `blocked: true` pour les quatre cas de montage ; le cas imbriqué de `w1-manager-nested-control-candidate.json` rend `blocked: true`. Rejouer aussi la sonde Git de la vague 1 (`manager_git_probe.py --expect-safe`).
- [ ] `docs/consolidation/w1-isolation.md` décrit la garde finale et ses limites ; le test d'inventaire des sous-processus reste vert ; aucun nouveau fichier hors périmètre de A (`collegue/sandbox/**` et tests correspondants, plus `sampling_ctx.py` seulement si le manager l'ajoute).

### 9.2 Checklist — `entrypoint.sh` (lot B)

- [ ] **Validation préalable**, avant tout démarrage de processus : `HEALTH_READY_ATTEMPTS` et `MCP_READY_ATTEMPTS` sont des entiers positifs en décimal avec une borne haute documentée ; `READY_POLL_INTERVAL` est un nombre positif borné. Sont rejetés : `abc`, vide, `0`, négatif, `1.5`, `1e3`, valeurs avec espaces ou signe, valeur énorme (débordement de `[ -ge ]` sous `dash`). Les défauts (30, 120, 1) sont inchangés.
- [ ] **Échec explicite** : code non nul propre à la validation, message sur stderr nommant la variable fautive, ni `health_server.py` ni `fastmcp` démarrés, aucun processus fils orphelin, code non converti en 0 par `cleanup`. La sous-commande `mcp-ready` n'est pas affectée.
- [ ] **Sonde curl du health server bornée** : la boucle `until curl -s -f http://localhost:4122/_health` n'a ni `--max-time` ni `--connect-timeout` à `fe763b5` (la sonde MCP en a un, 3 s). Un health server qui accepte la connexion mais ne répond jamais doit faire sortir l'entrypoint en échec dans un délai borné et **calculable** (au plus tentatives × (intervalle + délai de la sonde)), test à l'appui avec un faux serveur qui bloque.
- [ ] **Cohérence des autres sondes** : le healthcheck Compose et `HEALTHCHECK_CMD` de `scripts/ci_docker_smoke.sh` doivent rester identiques (test `test_smoke_runs_exactly_the_healthcheck_command_declared_in_compose`) ; si la commande du healthcheck change, les deux et le test changent ensemble.
- [ ] **Pas de régression de cycle de vie** : code exact de `fastmcp` restitué, MCP sorti avant d'être prêt = échec, mort du health server en service = échec, arrêt par SIGTERM propre, tests de `test_entrypoint_lifecycle.py` non assouplis.
- [ ] **Tests non vacuants** : chaque cas rouge sur `fe763b5` échoue sur l'assertion (par ex. « rejeté » faux, délai dépassé), et une mutation qui retire la validation ou le délai de curl fait échouer un test. Exécution sous le `sh` de l'image (`dash`), pas seulement `bash`.
- [ ] **Sonde du manager** : la partie `entrypoint` de `manager_w1_followup_probe.py` rend `rejected: true` pour les deux compteurs.
- [ ] `docs/consolidation/w1-auth-ci.md` et `.env.example` documentent les bornes ; le smoke Docker et les 5 noms de checks sont inchangés ; fichiers limités au périmètre de B (`entrypoint.sh`, `scripts/ci_docker_smoke.sh` si nécessaire, tests correspondants, docs de lot).

### 9.3 Intégration et livraison du suivi (C)

1. Attendre les SHA finaux communiqués par le manager ; vérifier qu'ils descendent de `fe763b5` et que l'intersection des fichiers modifiés par A et B est vide.
2. Fusion `--no-ff` par SHA dans `codex/consolidation-w1-followup-c`, puis recherche des usages oubliés (`git grep -nE '"-v"|pip_cache_dir|subscription_auth_dir|READY_(ATTEMPTS|POLL)'`), suite complète hors `integration`, `ruff check` et `ruff format --check collegue tests`, sondes du manager, rouge sur la base `fe763b5` pour chaque test de défaut annoncé (§4).
3. Codex valide (niveau 3). Puis, sur consigne : push de la branche, PR vers `main`, **étape 1** de livraison (observer checks et revues, rapporter), arbitrage Codex, **étape 2** (fusion sur instruction pour la tête exacte), vérification de `main`.
4. La vague 1 n'est clôturée qu'après cette vérification de `main` et la fermeture des deux findings.

### 9.4 Interfaces, changements de comportement et limites du suivi (pour la PR corrective)

Lots intégrés : A `d3a8786071b691926a4ee491cc479d88cbc25c71` (garde de montage), B `fd9a48ec9d9a555decbea4e84646c342ad3cf3b3` (readiness de l'entrypoint), tous deux depuis `fe763b5`, intersection vide. Résultats et SHA du candidat : `reports/w1-c-followup-integration.md`.

**Interfaces.** `git_control_exposure(path)` (`collegue/sandbox/executor.py`) remplace `_git_control_within` ; bornes `GIT_CONTROL_SCAN_MAX_DIRS` (250 000), `GIT_CONTROL_SCAN_MAX_ENTRIES` (1 000 000), `GIT_CONTROL_SCAN_MAX_DEPTH` (64). `LocalSamplingContext._validated_subscription_mounts()` applique la même garde au sampler. `entrypoint.sh` : compteurs 1–999999, `READY_POLL_INTERVAL` de 0.001 à 3600 s, code de sortie **2** sur réglage invalide, sondes curl `--connect-timeout 2 --max-time 3`, pauses interruptibles.

**Changements de comportement à annoncer.**
1. Un workspace, un cache pip ou un répertoire d'auth dont l'arbre contient un répertoire de contrôle, ou qui se trouve sous l'un d'eux, est refusé. Un marqueur `.collegue-git-control` planté par l'agent dans son workspace fait refuser les lancements suivants du sandbox sur ce workspace (faux positif assumé, fail-closed ; il ne peut jamais autoriser quoi que ce soit).
2. Un arbre monté de plus de 250 000 répertoires, 1 000 000 entrées ou 64 niveaux est refusé (« vérification impossible »). Un projet de très grande taille (par ex. `node_modules` énorme dans le workspace) peut donc être refusé ; le coût mesuré par A est de l'ordre de 0,06 s pour 30 000 répertoires, par lancement de conteneur.
3. Un chemin dont la vérification est impossible (`EACCES`, lien pendant, boucle de liens, erreur d'E/S) est refusé au lieu d'être traité comme absent.
4. Un réglage de readiness invalide (`abc`, `0`, `1.5`, valeur énorme, blanc…) fait sortir le conteneur en **code 2** sans rien démarrer ; avec `restart: always`, Compose le relance donc en boucle jusqu'à correction de la variable (lire les logs). Une variable vide (`VAR=`) équivaut à absente.

**Limites (à ne pas arrondir).** Fenêtre entre la vérification et le `docker run` (un marqueur créé entre les deux n'est pas vu) ; le démon Docker reste privilégié et le sandbox garde le workspace en lecture-écriture ; le healthcheck Compose lance `curl -f` sans `--max-time`, borné seulement par son `timeout: 5s` ; Python 3.11 n'est pas exécuté localement (preuve : check `Pytest (Python 3.11)` de la PR) ; les revues externes (Copilot, Codex GitHub) ne sont examinées que lorsqu'elles sont disponibles : leur absence est rapportée telle quelle et ne vaut ni succès ni prérequis ; seuls les cinq checks CI restent obligatoires au ruleset.

## 10. Vague 2

La vague 1 est close (PR #608 et #609, `main` = `9862b39`). La vague 2 (A : registre de budget transactionnel durable et exécution bornée ; B : wheel installable, ressources, migrations existantes, dépendances verrouillées depuis `pyproject.toml`) a son protocole d'intégration, sa checklist et son état intégré : [`w2-integration.md`](w2-integration.md). Les règles de livraison (deux étapes, arbitrage des findings avant fusion, preuve absente ≠ succès, cinq checks requis, aucun bypass) sont inchangées (`AGENTS.md`).

État et limites à citer sans les arrondir :
- **Strict ≠ advisory.** La garantie stricte porte sur les appels que le framework émet (réservation avant émission, usage inconnu bloquant, transport non bornable refusé) ; elle ne couvre pas un programme du workspace qui disposerait d'une clé. `advisory` n'offre aucune garantie.
- **Abonnement** : plafond USD seul accepté (0 $ établi) ; plafond de tokens strict refusé. **Fournisseurs, modèles, endpoints** : identité exacte reconnue ou attestée, famille de tarif liée à la destination ; destination inconnue, passerelle ou LAN non attesté : refus sans prix configuré. Un loopback et un hôte attesté sont crus sur parole (un tunnel payant n'est pas détectable avant le routage complet de la vague 4).
- **Reprise** : même identité de cycle ⇒ même solde ; création du projet et lien au scope en une transaction ; droit de cycle exclusif à échéance de 2 h.
- **Usage inconnu / historique ambigu** : réservation conservée, suite stricte bloquée avec un motif durable, résolution par l'opérateur ; un cumul historique décroissant ou invalide bloque jusqu'à résolution.
- **Prompts** : l'ancien état d'une installation précédente est importé sans être modifié ; le nouvel état gagne toujours.
- **Preuves restant à la CI** : Python 3.11, image Docker et smoke, wheel complet avec verrous, PostgreSQL sur le runner. Toute suppression d'une de ces preuves est une régression.

## 11. Vague 3 (livrée)

La vague 2 est close (PR #610, `main` = `58355a4`). La vague 3 (A : preuve de livraison commune BUILD/IMPROVE, oracles conservés, veto qualité, intégrité du contenu publié ; B : opt-in de fusion, politique SHA/checks/base commune, rulesets, reprise durable après fusion et synchronisation, classification des chemins sensibles) est **livrée** : PR #611 fusionnée en squash, `main` = `5c1cbf51cec854b2fa73e3951273951e7a7212be` (arbre `206d53ac…`, parent unique `58355a4`), cinq checks du push verts (run `37860048078`, 3852 tests passés / 36 ignorés, PostgreSQL réel 21/23/5 sans skip). Les revues externes ont été indisponibles par quota (aucun avis favorable). Protocole, répartition, contrat et checklist : `docs/consolidation/w3-integration.md`.

Points d'attaque propres à cette vague (à rejouer sur le SHA figé, entrées publiques, témoin bénin et motif du refus vérifiés) :
- **Preuve** : rechargement par une nouvelle instance du manager, identités exactes, refus des preuves absentes, d'une autre PR, d'un ancien SHA, d'un contenu altéré ; jamais reconstruite depuis le corps de PR ; ré-exécution identique idempotente (une seule preuve par tête), vrai conflit conservé.
- **Contenu** : arbre Git complet et reconstruit depuis le distant ; binaire, lien, suppression, mode, fichier de base modifié, module ignoré (dépôt imbriqué compris) nécessaire à l'oracle ⇒ refus.
- **Oracles** : mêmes empreintes, rouge par assertion en phase d'appel sur la préimage puis vert ; collecte, import, zéro test, skip, `xfail`, rapport incomplet, plafonné ou multiple, code de sortie incohérent ⇒ refus ; contrats livrés rejoués (BUILD et IMPROVE) ; l'état qui exige les oracles prévaut sur le réglage `GATE_ACCEPTANCE_TESTS`.
- **Qualité** : couverture en baisse, mesure requise absente, revue bloquante ou absente ⇒ refus malgré un meilleur score ; aucune dérogation ajoutée.
- **Fusion** : défaut désactivé ; SHA exact émis ; checks (classiques et rulesets, `app_id`, pagination) présents et réussis ; tête ou base déplacée avant et pendant l'appel ; **pas de garantie atomique sur la base via l'API REST** (règle serveur « à jour » exigée) ; rôle personnalisé ou inconnu présumé contournant ; restrictions de faible risque conservées pour locks, requirements, Dockerfiles, `pyproject.toml` et migrations empaquetées même avec une allowlist élargie.
- **Reprise** : fusion distante confirmée puis synchronisation en échec ⇒ pas de seconde fusion, aucune tâche (indépendante comprise) sur un checkout périmé ; reprise depuis un nouveau manager, y compris pour une fusion hors moteur sans cycle initial et quand la découverte des PR est indisponible ; crash entre l'appel distant et l'écriture locale réconcilié sans refusion.

Limites à citer sans les arrondir : Contents API texte seul ; journal de preuves non signé ; oracle non résistant à du code arbitraire dans le même interpréteur ; jeton d'application GitHub refusé ; deux PR simultanées sur la même base (la seconde reste ouverte, base périmée) ; PR empilées d'IMPROVE non auto-fusionnables vers `main` avant leur parente ; aucune campagne réelle. Les limites de la vague 2 (strict borné aux transports bornables, abonnement USD seul, droit de planification 2 h) restent valables.

## 12. Vague 4 (livrée, validation réelle incomplète)

La vague 3 est livrée (`main` = `5c1cbf5`). **État exact : la vague 4 est livrée** (PR #612 fusionnée, `main` = `869ed3c8936b2bfaa72ac9c64746b1420a827825`, arbre `2825ca37…`, parent unique `5c1cbf5`, cinq checks du push verts, run `37903659594`) **mais la validation réelle avec modèles est INCOMPLÈTE** : l'unique lancement de la campagne finale (run `37905627368`, plafonds 2 USD / 250 000 tokens / 900 s) a été refusé au préflight `P06-worker-capacity` (`unbounded_transport`) avant toute émission — 0 appel modèle, 0 USD ; il est consommé et n'est pas relancé. Les étapes métier avec modèle réel (BUILD, R04 amélioration, R05 incident/rollback) restent sans preuve, et R04/R05 n'étaient pas câblés dans le lanceur réel. La vague 4 (A : résolution cohérente fournisseur/modèle/endpoint/authentification par rôle ; B : référence métier FastAPI + SQLite + Alembic à trois tâches dépendantes sérialisées, PDF, reprise, amélioration, incident et rollback Phase 5 déterministes, préflight de campagne) a sa répartition, ses interfaces, ses preuves et sa checklist dans `docs/consolidation/w4-integration.md`.

Points d'attaque propres à cette vague (à rejouer sur le SHA figé, entrées publiques, témoin bénin et motif du refus vérifié) :
- **Routage** : destination, modèle et provenance du credential *réellement émis* pour chaque rôle (CODER, PLANNER, QA, REVIEWER, DEFAULT), appels concurrents de deux rôles de même modèle ; aucune clé globale vers un autre fournisseur ; erreur avant émission sans credential adapté ; contradictions fournisseur/préfixe/modèle refusées ; secrets absents des `repr`, journaux, exceptions et `argv` ; point de construction du SDK OpenHands (préfixe, endpoint, provenance de la clé).
- **Budget** : réservation sur la destination effective, retries et replis compris ; refus des transports non bornables inchangé ; l'abonnement ne garantit pas un plafond de tokens strict.
- **Scénario métier** : base vierge, oracles rouges par assertion puis verts (même empreinte), contenu intégré vérifié avant la tâche suivante, PDF lu par un vrai lecteur avec témoin négatif, reprise avec le même registre, rollback Phase 5 restaurant comportement ET état durable, politiques W3 actives.
- **Campagne** : commande unique, 2 USD / 250 000 tokens / 900 s depuis le registre de W2, préflight sans dépense, transport incompatible ⇒ refus avant émission (*validation incomplète*), aucun nightly ni test payant déclenché.

Limites à citer sans les arrondir : W2 (plafond strict seulement pour les transports bornables ; abonnement sans plafond de tokens strict) et W3 (Contents API texte seul ; journal non signé ; oracle dans le même interpréteur ; base distante exigeant une protection stricte applicable ; pas de re-livraison automatique d'une PR périmée ; GitHub App refusé).

## 13. Vague 5 (autorisée, en cours)

La vague 4 est livrée (`main` = `869ed3c`), validation réelle incomplète (§ 12). **État exact : W1 à W4 livrées ; W5 en cours — A (courtier budgétaire), B (câblage métier réel R04/R05, validation du socle de la fixture) et C (CI, image, fixture, workflow) développent en parallèle sur `codex/consolidation-w5-{a,b,c}` ; rien n'est intégré ni publié ; aucune campagne réelle démarrée, aucun secret créé.** C'est une **nouvelle** campagne autorisée par le plan « Qualification complète de Collègue avec Gemma 4 », pas une relance de celle de W4. Répartition, contrats, préparation de C, checklist d'intégration et décisions ouvertes : `docs/consolidation/w5-integration.md`.

Points d'attaque propres à cette vague (à rejouer sur le SHA figé, depuis le vrai point d'émission, avec un faux fournisseur qui COMPTE les appels reçus ; plan du manager : `reports/w5-manager-validation-plan.md`) :
- **Frontières du courtier** : clé absente des variables, des `argv`, des montages et des journaux du codeur et du gate ; `--network none` ; un seul socket restreint sans accès aux autres rôles, scopes ni à l'administration ; relais sans destination fournie par la requête.
- **Faux fournisseur observant** : `countTokens` et `generateContent` portent le même objet et le même modèle ; sortie bornée ; schémas d'outils et JSON, `usage` complet ; refus des médias, des outils hébergés, du flux, des JSON dupliqués, des paramètres contradictoires, d'une URL ou d'une authentification injectées, des redirections.
- **Comptabilité** : appels réellement reçus pour le rejeu, la collision d'identité, la concurrence, le crash entre réservation et émission, le crash après émission avant règlement ; une inconnue conserve sa borne et bloque tout le scope parent ; allocation parent/enfant sans double consommation ni libération prématurée ; SQLite et PostgreSQL ; tous les rôles passent par la même autorité (omettre le contexte ne donne pas de direct gratuit) ; échéance persistée sans remise à zéro ; capacités effectives établies (une chaîne `budget_enforcement` seule ou une sortie stdout du worker n'est pas une preuve).
- **Métier** : R04 exige une amélioration **livrée** (pas seulement une PR ouverte) avec gain réel et mêmes contrats ; R05 : injection explicitement déterministe mais vrais contrôles, aucun verdict de revue, de fusion ou de mesure simulé, étape non réussie si la garde refuse ; PDF réellement altéré puis restauré, PR/checks/revert/synchronisation, arbres et SHA, incident durable récupéré, acquittement CAS, reprise ; nettoyage après toutes les étapes sans masquer la cause d'origine ; graine et ressources étrangères préservées.
- **Fixture et CI** : workflow et socle comparés aux **objets Git réels**, pas à une chaîne du manifeste ; déclenchement réellement démontré (jamais `pull_request_target` avec une branche par défaut immuable sans workflow) ; check de l'application Actions sur la tête exacte, rouge pour un vrai échec `pytest` ; une PR qui modifie le workflow, le CODEOWNERS ou le verrou approuvé ne fusionne pas même avec un check vert (le CODEOWNERS à 0 approbation est observé INEFFICACE côté serveur en C47 : le rempart est le contrôle de `.github/` ET `ci/` AVANT publication (garde commun de B23) et AVANT fusion, plus la provenance du check-run → job → exécution ; une fusion manuelle hors produit n'est pas couverte ; jamais rejouer les têtes qui altèrent les contrôles, mais prouver par la vraie entrée publique, faux GitHub à ZÉRO écriture) ; création des bases sans bypass ni faux check ; code candidat jamais exécuté sur l'hôte, dépendances issues du seul verrou approuvé (une dépendance hors pile est refusée), sans secret ni socket Docker, sources de montage = répertoires réels (pas de lien vers le socket) ; verrous de l'image broker audités en strict ; étape PostgreSQL du courtier sans skip ; preuve du transport dans l'image broker sans réseau avec le vrai SDK ; scanner de fuite en octets.
- **Campagne** : un seul lancement, identifiant neuf consommé, canaris des deux Gemma dans l'enveloppe commune ; en cas d'incompatibilité `countTokens`/borne : aucune estimation de secours, aucune seconde campagne ; run ID et SHA capturés aussitôt ; clé temporaire retirée même sur refus ; journaux sans valeur de clé.

Limites à citer sans les arrondir : celles de W2, W3 et W4 (§§ 10 à 12) restent valables ; Gemma est gratuit selon l'affichage de Google mais la garantie repose sur l'identité exacte du modèle et l'endpoint officiel, jamais sur une clé « non facturable » supposée ; le déclenchement du check de la fixture, le comportement du code owner avec 0 approbation requise et la règle de création des bases sont des comportements de GitHub établis seulement par la contre-épreuve distante ; le verrou historique `sandbox-openhands` reste rouge à l'audit strict (non réparé).
