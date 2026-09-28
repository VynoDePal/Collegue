# Protocole de consolidation — plan opérationnel

Les règles durables (rôles, worktrees, propriété des fichiers, niveaux de tests, conditions de merge,
budget) sont dans [`AGENTS.md`](../../AGENTS.md). Ce document en donne l'application : vagues,
contrats, procédure d'intégration et **checklist de revue indépendante**, propre à chaque faille.

- Base : `main` = `51ab3fcce70f30c1a50a4dcde334ca1269582f19`.
- Audit de référence : 2351 passed, 36 skipped, 9 deselected ; couverture 72,98 % ; `ruff check` OK ;
  `ruff format --check` en échec (exemples Python de `collegue/client/README.md`, Ruff 0.16.9) ; Python 3.12.12.
- Référence de campagne : `~/.codex/collegue-consolidation/20260928/` (`PLAN.md`, `briefs/`, `reports/`, `evidence/`).

## 1. Vagues

Chaque vague est livrée et vérifiée sur `main` avant la suivante.

| Vague | A | B |
|---|---|---|
| 1 | Fermer la frontière Git/sandbox sur **tous** les chemins (capture/recapture, seed/retry, compounding, snapshots, revert) | OAuth fail-closed, profil réseau local sur loopback, nightly et smoke Docker honnêtes |
| 2 | Registre de budget transactionnel durable (BUILD/IMPROVE/tous rôles/retries), idempotence, réservation avant appel, worker borné + deadline, reprise | Wheel complet, ressources et migrations installables hors checkout, dépendances unifiées et verrouillées depuis `pyproject.toml` |
| 3 | Preuve de livraison commune BUILD/IMPROVE, contrats scellés, revue bloquante, couverture sans régression, preuve négative d'oracle (exit 1 puis exit 0, même hash) | `BUILD_AUTO_MERGE` à `false` par défaut, SHA/checks/base/resync communs, rulesets GitHub, reprise durable d'une fusion déjà réussie, restrictions Phase 5 conservées |
| 4 | Routage fournisseur/modèle/endpoint/auth par rôle (coder et sampling), pas de fuite de clé vers un autre provider, contradictions explicites | MVP fixture FastAPI/SQLite/Alembic à trois tâches dépendantes, DB vierge, redémarrage, amélioration, incident et rollback Phase 5 |

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
7. C attend l'acceptation Codex sur le SHA exact de la tête de PR **et** les 5 checks requis verts sur ce SHA.
8. C fusionne ; la méthode de fusion est celle que les protections du dépôt autorisent. Aucun bypass.
9. Vérification de `main` après fusion (SHA, CI) avant la vague suivante.

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

**Recherche des usages oubliés** (à relancer sur le SHA intégré) :

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
- [ ] Niveau conteneur : `entrypoint.sh` (mode http) lance `fastmcp run` en arrière-plan, puis `wait $MCP_PID` et `cleanup` qui se termine par `exit 0`, et affiche « All services started successfully! » sans condition. Un échec de démarrage OAuth peut donc **sortir en code 0** malgré le correctif Python. Ce fichier n'est attribué à aucun lot de la vague 1 (voir le rapport de C) : décision du manager requise ; sans elle, le smoke Docker doit détecter ce cas.
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
