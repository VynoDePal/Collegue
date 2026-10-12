# Vague 5 après la campagne du 12 octobre — état courant (C75, mis à jour en C76, C78 et C81)

**État exact (12 octobre 2026, après la PR #615).** La vague 5 est **livrée sur `main`** (PR #613 `734c7f5`, PR #614 `b88e7bf`, **PR #615 `ed548683175fdc88967e2e116d41b66071da76f2`**, arbre `028bbee2…`, parent unique `b88e7bf`, cinq checks du push verts, run `38249841619`).
**La qualification réelle avec modèles reste INCOMPLÈTE** : la campagne `w5-gemma-20261012-9e7c4d76` a émis une seule génération, reçu un HTTP 500 et s'est arrêtée sur la protection budgétaire. **Deux choses distinctes** :
- *la qualification réelle* : incomplète, inchangée par la PR #615 ;
- *les correctifs de compte rendu* (B28 → B30, puis **B31 → B32** qui corrige le finding P2 de la revue Codex de la PR #615) : **livrés par la PR #615** (les sections qui les décrivent, ci-dessous, parlent d'« intégration locale » : c'est l'état historique avant publication, conservé). Le diagnostic de A (A35/A36)
  est **terminé et accepté sans correctif produit**.
- **Étape suivante, autorisée mais NON lancée** : un petit lot de diagnostic fermé des erreurs HTTP Google (**intégré localement, non publié** : A `a4ea9ec`, B `750b1b8`), puis UNE campagne réelle NEUVE (identifiant neuf, 2 USD / 250 000 tokens / 900 s) — préparation, responsabilités, critères et plan de commandes dans [`w5-google-campaign-plan.md`](w5-google-campaign-plan.md).
  Aucun appel Google, aucune clé, aucun secret/environnement/variable, aucun dispatch avant l'ordre exact du manager.

Ce document est l'état courant ; l'historique de préparation et d'intégration est dans [`w5-integration.md`](w5-integration.md) (§ 1 à 9, conservés), les règles générales dans [`AGENTS.md`](../../AGENTS.md).

## 1. Chronologie vérifiée

| Date | Fait | Référence |
|---|---|---|
| 10 oct. | PR #613 fusionnée (courtier budgétaire Gemma 4, campagne métier R04/R05, gardes de publication et de fusion, CI du transport broker) | `main` = `734c7f57221bfce096a03a6858ebccaac75451f8`, arbre `881666125180263561fa04441f6af117425804dd`, parent `869ed3c` ; cinq checks du push verts (run `38025147445`), revues distantes sans avis (quota) |
| 10-11 oct. | Première campagne `w5-gemma-20261010-dcf645da` (run `38027369635`) : arrêtée au préflight statique **P07** (HTTP 403 sur la protection classique de la base), **zéro émission**, clôturée incomplète ; son lancement est consommé | cause établie ensuite : permission `Administration:read` absente du jeton de la fixture |
| 11 oct. | Sonde de droits du jeton (branche `codex/w5-fixture-token-diagnostic-c`, `2841e3a`, **non fusionnée**) : run `38181684484`, tentative 1 rouge (3 × 403), tentative 2 verte (15 lectures) après ajout de `Administration:read` par l'utilisateur | secret du dépôt inchangé ; aucune protection modifiée |
| 12 oct. | PR #614 fusionnée : préflight produit distant sans modèle (workflow `w5-product-preflight.yml` en `workflow_dispatch`, `scripts/w5_product_preflight.py`, inventaire des sous-processus) | `main` = `b88e7bf2960ca82164c3256a11acf4462b1d0444`, arbre `f43dde5f41bf491665f3a9a8cc5feb0a663f8f4a`, parent unique `734c7f5` ; run PR `38186406715` : cinq checks verts (5537 passés / 38 ignorés / 27 désélectionnés par Python) |
| 12 oct. | Préflight dédié (run `38190067431`) : statique 11 contrôles requis réussis (P08 facultatif non joué), complet 12/12 | aucun modèle, aucune mutation |
| 12 oct. | **Campagne `w5-gemma-20261012-9e7c4d76`** (run `38230467960`, tentative 1, un seul dispatch) | § 2 |

## 2. La campagne du 12 octobre — validation INCOMPLÈTE

- Produit `b88e7bf`. Préflights statique et complet et revalidation avant activation : verts. Destination : API Google native (`generativelanguage.googleapis.com/v1beta`), modèle `gemma-4-31b-it`, enveloppe
  **2 USD / 250 000 tokens / 900 s**, mode strict, échéance persistée ouverte à 09:33:54Z (exactement 900 s, jamais remise à zéro ; l'arrêt n'est pas dû à l'échéance).
- Premier canari texte du 31B : `countTokens` accepté (**10 tokens**), réservation de **266 tokens** (10 + sortie maximale de 256) avant émission, puis **une génération `generateContent` émise** → **HTTP 500**
  après 26 s environ. Aucun usage renvoyé. **Aucun retry, aucun repli** (ni le 26B), aucun BUILD, IMPROVE, incident, rollback ni reprise.
- Registre (SQLite relu en lecture seule, `integrity_check` vert) : une tentative, une réservation, deux événements ; **0 token réglé** ; **consommation réelle inconnue**, réservation de 266 tokens maintenue
  en `unknown`, **scope bloqué** ; 0 USD enregistré (tarification gratuite explicitement configurée, sans usage fournisseur reçu).
- **Lecture correcte des zéros.** « 0 usage réglé » ne signifie **jamais** « zéro émission » : une génération a été émise et son usage est inconnu. Le compteur `facts.llm_calls_emitted` du rapport produit
  (hérité du préflight, resté à 0) n'est pas un total de campagne ; c'est le registre (`broker_attempts.emitted_at`) qui fait foi.
- Verdict : workflow `failure`, rapport produit `failed` (code 1), **qualification globale : validation incomplète**. Les capacités JSON/outils du 31B et les trois capacités du 26B, la borne de sortie réelle et
  la compatibilité complète restent **non prouvées**.
- **Cause du HTTP 500 : indéterminée.** Le statut ne prouve ni panne transitoire, ni quota épuisé, ni clé invalide ; le corps de l'erreur, le statut RPC et les en-têtes de la réponse ne sont pas conservés, et
  aucun appel supplémentaire n'est autorisé pour les retrouver. **Le budget et l'échéance ne sont pas la cause de l'arrêt** (horloge à 26,6 s sur 900 s ; 266 tokens réservés sur 250 000). Le diagnostic hors ligne de A
  (A35, calibré par A36, **accepté**) conclut : **cause interne indéterminée, aucun changement produit justifié**, aucune modification de transport. Hypothèses abandonnées, à ne pas reprendre : un `countTokens` réussi
  n'établit que l'acceptation de cette opération (ni la validité de la clé pour `generateContent`, ni la santé du service) ; la présence du champ `model` dans le corps n'est qu'une *différence* avec les exemples
  documentés (tolérance indéterminée, ni cause supposée ni correctif : le retirer serait une variation expérimentale non justifiée) ; aucun classement de vraisemblance des pistes ; la forme du corps d'erreur ne prouve pas
  l'origine. Une instrumentation du corps d'erreur n'est ni développée ni autorisée.
- Clôture vérifiée par le manager : secret temporaire `W5_GOOGLE_API_KEY`, environnement `w5-gemma-campaign` et variable `W5_BOOTSTRAP_MANIFEST_JSON` **supprimés** ; état initial des secrets/environnements/variables rétabli ;
  scanner du runner et scan indépendant sans fuite ; aucune ressource de campagne (projet, branche, PR, issue) créée ; seule référence conservée volontairement :
  `collegue-business-claims/w5-gemma-20261012-9e7c4d76` (la revendication durable, qui interdit la réutilisation de l'identifiant). Fixture (graine, socle V2, rulesets) et `main` inchangés.
- Preuves du manager : `reports/w5-manager-result-w5-gemma-20261012-9e7c4d76.md`, `evidence/w5-manager-w5-gemma-20261012-9e7c4d76/` (dont `manager-independent-verdict.json`).

## 3. Règles en vigueur

- **Les campagnes `w5-gemma-20261010-dcf645da` et `w5-gemma-20261012-9e7c4d76` sont closes et ne se relancent jamais** (identifiants consommés). **Une campagne NEUVE est autorisée** (« Vas y » de l'utilisateur : identifiant neuf, 2 USD / 250 000 tokens / 900 s) **mais n'est pas lancée** : elle exige, sur ordre exact et distinct du manager, la remise en place
  temporaire de l'environnement, du secret et de la variable, puis le dispatch (plan : [`w5-google-campaign-plan.md`](w5-google-campaign-plan.md)). Toute autre campagne exigerait un nouvel accord explicite.
- Aucun appel modèle, **`countTokens` compris**, aucun dispatch ni rerun de `consolidation-e2e.yml`, de `w5-product-preflight.yml` ou du nightly sans ordre exact ; aucun accès à un fichier de clé ; aucun
  changement de secret, de protection ou de ruleset ; aucune mutation de la fixture (`scripts/w5_fixture_bootstrap.py apply|probe|cleanup`) sans le jeton d'ordre.
- Un préflight vert ne prouve ni BUILD, ni R04/R05, ni les modèles : il établit que le produit franchit ses contrôles contre GitHub sans émettre. Une campagne incomplète n'est pas un échec masqué ni un succès.
- Ne jamais convertir une inconnue en succès : usage inconnu après émission ≠ absence d'émission ; point d'arrêt ≠ câblage absent.

## 4. Ce qui a été intégré (C76, C78) puis livré par la PR #615 — état historique avant publication

**Livraison de correctifs de compte rendu, pas de qualification.** Fusion par SHA exact, sans conflit (périmètres disjoints), des commits de B : B28 `e9aedb8`, B29 `f4e9b64`, B30 `b6a53f3` (parent `b88e7bf`), puis B31 `eef0e71` et B32 `121879b` (voir plus bas) — `collegue/pilot/w4_business.py`,
`collegue/pilot/w5_business.py`, `tests/test_w5_report_activity.py` (nouveau), `tests/test_w5_business_activation.py` (une assertion de message), `docs/consolidation/w5-business.md`. Relu depuis `b88e7bf` : **aucun changement des plafonds, réservations,
inconnues, retries, autorisations ni du verdict**. Ce que le rapport de campagne affirme désormais :

- **Total d'émissions non mesuré = `null`**, jamais 0 : dès qu'une étape émettrice (R01, R04, R05) a été appelée, `llm_calls_emitted` et `billable_actions_emitted` (premier niveau et `facts.emissions`) valent `null` avec la liste des
  étapes émettrices démarrées ; le zéro n'est affirmé que si aucune étape émettrice n'a été appelée (préflight non validé, ou arrêt avant l'activation).
- **Zéros du préflight scopés** : les compteurs hérités du préflight ne sont plus recopiés comme total de campagne ; ils restent lisibles sous `facts.preflight_emissions` (portée « preflight »).
- **Absence de règlement ≠ absence d'émission** : le refus de qualification dit « aucune consommation réglée établie par le registre » et, après un usage inconnu, « le règlement est absent, l'émission n'est pas écartée » ; le
  faux message « les canaris n'ont pas traversé le pipeline réel » disparaît.
- **Callbacks** : `scope.played` = étapes dont le callback a réellement été appelé (admission accordée) ; `scope.wired` et `scope.not_wired` **partitionnent** `scope.announced` (B31/B32, ci-dessous) ; `scope.wired_not_played` = `wired` moins `played`
  (arrêt amont ou refus à l'admission) ; un R02 sans vérificateur est une validation incomplète « appelant absent », pas un refus à l'admission.
- **Arrêt effectif** : `scope.stop_step` / `scope.stop_point` calculés à la sortie, **après** le nettoyage (R06 compte ; le premier arrêt métier n'est jamais masqué par une erreur de nettoyage), `scope.cleanup_state` toujours indiqué ; le rapport humain
  affiche émissions, point d'arrêt et phases appelées / câblées non jouées / non câblées.

**Correction du finding P2 de la PR #615 (B31 → B32).** La revue Codex GitHub (commentaire 4247241756, `w4_business.py:1921`) relevait que, avec `verify=None`, `business` restait dans `wired` alors que `not_wired` ne regardait que R04/R05. Le manager
**retient** ce P2 et révise explicitement la tolérance B30 pour la portée historique limitée de `not_wired` (sa matrice reproduisait 24 échecs sur 32 combinaisons ; décision et preuve : `reports/w5-manager-report-followup.md`). Résultat :
- `wired` et `not_wired` **partitionnent** `announced` ; `build` (lanceur obligatoire) et `registry` (validateur interne, un lecteur absent est une *erreur* de ce validateur) restent toujours câblés ; `business`, `improvement`, `incident_rollback` et
  `cleanup` sont câblés **seulement si leur callback est fourni**, sinon dans `not_wired` ; `played` reste inclus dans `wired`.
- **B32 : la condition de câblage reprend la condition d'appel existante** de chaque callback — `verify` et `cleanup` : `is not None` ; `improve` et `incident` : vérité booléenne (comportement historique, volontairement non normalisé : un callable
  « faux » n'y est ni appelé ni annoncé câblé). **Aucun callback effectivement invoqué ne change.**
- Le point d'arrêt d'une phase non câblée dit « NON CÂBLÉE… aucun appelant fourni, callback jamais appelé » ; **aucun changement des admissions, émissions, budgets, inconnues, retries, autorisations ni verdicts**. Précision documentaire (C78) : dans
  `w5-business.md`, le paragraphe « Condition de câblage = condition d'appel » a été déplacé après la fin du paragraphe « Compatibilité » (raccord mécanique, sens inchangé).

**Limites de cette livraison.**
- Éprouvée par les tests de B et les sondes du manager sur **faux fournisseur** seulement ; le nouveau format n'a jamais été produit par une campagne réelle ni contre l'API Google.
- Le **nombre exact d'émissions n'est pas lisible** dans le modèle de données actuel (`emitted_at` absent d'`AttemptRecord`) : il reste `null`, c'est une limite documentée par B28, pas une mesure ; une interface de lecture serait une décision du manager.
- Les artefacts de la campagne historique restent **intacts** (leurs champs hérités du préflight sont corrigés par le rapport du manager, pas réécrits) ; la campagne reste close, incomplète, jamais relancée.
- **Publication (historique)** : la PR #615 avait été ouverte sur la tête `b1b8f54` (P2 non encore corrigé), puis mise à jour avec B31/B32 (`e0321a3`) et **fusionnée** en `ed54868` (cinq checks verts, Copilot indisponible par quota, aucune revue Codex sur la dernière tête, limite acceptée par le manager). Le diagnostic de A n'a produit **aucun commit produit**. Aucun appel Google ni nouvelle campagne.

| Lot | État |
|---|---|
| **B28 → B30** | livrés par la PR #615 (`ed54868`) |
| **B31 / B32** | livrés par la PR #615 (finding P2 corrigé et éprouvé indépendamment) |
| **A35 / A36** | terminés, acceptés, aucun changement produit ; cause du 500 indéterminée ; aucun lot expérimental de transport |
| **C** | ce document, `AGENTS.md`, `CLAUDE.md`, `w5-integration.md` ; vérification de l'intégration (§ 5) |

Propriété inchangée : A (`collegue/broker/**`, runtime, transport), B (`pilot/w4_business.py`, `pilot/w5_business*`, tests et `w5-business.md`), C (workflows, scripts CI, documents généraux). C ne modifie pas les fichiers de A/B.

## 5. Vérification de l'intégration (appliquée en C76 ; commandes, codes et résultats dans le rapport C76, hors du dépôt)

1. **Entrée.** Intégrer seulement les SHA communiqués par le manager (B28, éventuellement A), par fusion de SHA exacts depuis `b88e7bf`, périmètres disjoints, sans conflit fonctionnel (à renvoyer à l'auteur). Relire les diffs (aucune
   modification de plafond, d'autorisation, de retry ni de verdict ; aucune écriture de transport sans décision du manager).
2. **Environnement.** Python du venv de C `~/.codex/collegue-consolidation/20260928/envs/c/bin/python` et dépendances communes (`envs/dependencies-20261008`) en **lecture seule** ; **aucun `pip install`**, aucun
   cache dans `~/.cache`. Racine du disque très réduite (≈ 450 Mo, parfois < 100 Mo) : scratch, caches, homes, rapports volumineux et logs sous **`/tmp/<C>/…`** (ou `/dev/shm`), `PYTHONDONTWRITEBYTECODE=1`,
   puis résumés dans les preuves persistantes (`evidence/w5-c<n>-*`). **`TMPDIR` doit être un ancêtre de `--basetemp`** (par exemple `TMPDIR=/tmp/<C>/tmp` et `--basetemp=/tmp/<C>/tmp/bt`) : les tests de confinement de
   `cleanup_workspace` l'exigent (l'ancien défaut d'environnement qui faisait échouer le nettoyage ne doit pas se reproduire). `COLLEGUE_HOME` **neuf** pour chaque exécution (jamais un home persistant).
3. **Contrôles locaux.**
   - Suite complète, un seul lancement lourd à la fois (`flock ~/.codex/collegue-consolidation/20260928/heavy.lock`) :
     `python -m pytest -p no:cacheprovider --basetemp=/tmp/<C>/tmp/bt tests/ > /tmp/<C>/logs/full.log 2>&1 ; echo $? > /tmp/<C>/logs/full.rc` (options par défaut du dépôt : `-m 'not integration and not w5_image'`),
     compte attendu proche de 5537 passés / 38 ignorés / 27 désélectionnés (à comparer, pas à imposer) ; PostgreSQL réel et preuve d'image : **CI seulement** (planchers 139/21/23/5 et 18).
   - Ruff **check ET format** complets : `python -m ruff check --no-cache collegue tests` et `python -m ruff format --check --no-cache collegue tests` (mêmes chemins que le job Ruff) ; `git diff --check`.
   - Wheel **hors checkout** et migrations sans installer : `git archive HEAD | tar -x -C /tmp/<C>/src` puis `python -m pip wheel --no-deps --no-build-isolation -w /tmp/<C>/wheel /tmp/<C>/src` (construction depuis un export, le checkout n'est pas sali ; n'écrit qu'une roue), la dézipper sous `/tmp/<C>/wheel-x`, puis depuis un
     répertoire neutre lancer `python -m collegue.migrations upgrade` avec `PYTHONPATH=/tmp/<C>/wheel-x`, `COLLEGUE_HOME` neuf et une base SQLite jetable : tête `0013`, 13 migrations (valeurs actuelles de `main`). La vérification complète du
     verrou `runtime` (`scripts/verify_wheel.py --lock runtime`, qui installe) est celle du job « Dependency audit » en CI, pas un `pip install` local.
   - `python scripts/locks.py check` (cohérence `pyproject.toml` / `locks/`), sans `--recompile` (CI).
4. **Docker/OpenHands lourds : CI uniquement.** Aucun build ni fermeture du SDK en local. Les **cinq checks requis restent inchangés** : `Ruff`, `Pytest (Python 3.11)`, `Pytest (Python 3.12)`, `Dependency audit`,
   `Docker build`.
5. **Publication** seulement sur ordre : push de la branche, PR, observation des cinq checks et des revues (texte intégral de chaque finding, une revue indisponible n'est pas un avis favorable), fusion sur l'instruction
   exacte pour la tête, puis contrôle de `main` (SHA, arbre, cinq checks du push). Toute nouvelle tête invalide l'acceptation.
6. **Interdits dans ce plan** : appel modèle (même `countTokens`), dispatch/rerun, lecture d'un fichier de clé, secret, mutation de la fixture ou des règles, relance de la campagne close.

## 6. Suite

La PR #615 est livrée et `main` validé. Prochaine étape, autorisée et non lancée : le lot de diagnostic Google (A, B) puis, sur ordres distincts du manager, publication, fusion sur SHA précis, préflight dédié sans clé, préparation temporaire de la configuration et dispatch unique d'une campagne neuve ;
tout le détail (responsabilités, critères, commandes exactes, prérequis et critères d'arrêt) est dans [`w5-google-campaign-plan.md`](w5-google-campaign-plan.md). Les campagnes closes ne se relancent jamais.
