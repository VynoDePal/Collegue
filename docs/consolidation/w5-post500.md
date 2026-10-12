# Vague 5 après la campagne du 12 octobre — état courant (C75)

**État exact (12 octobre 2026).** La vague 5 est **livrée sur `main`** (PR #613 `734c7f5`, PR #614 `b88e7bf`). **La qualification réelle avec modèles reste INCOMPLÈTE** : la campagne
`w5-gemma-20261012-9e7c4d76` a émis une seule génération, reçu un HTTP 500 et s'est arrêtée sur la protection budgétaire. Aucune campagne ni appel modèle n'est en cours ni autorisé. Ce document est
l'état courant ; l'historique de préparation et d'intégration est dans [`w5-integration.md`](w5-integration.md) (§ 1 à 9, conservés), les règles générales dans [`AGENTS.md`](../../AGENTS.md).

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
- **Cause du HTTP 500 : indéterminée.** Le statut ne prouve ni panne transitoire, ni quota épuisé, ni clé invalide ; le corps de l'erreur n'est pas conservé dans les preuves. Le diagnostic hors ligne de A (A35,
  calibré par A36) est en cours : **ses conclusions ne sont pas reprises ici** et ne justifient, à ce jour, aucune modification de transport ; en particulier ni « aucune authentification n'est possible » ni
  « un champ `model` dans le corps est interdit » ne sont établis.
- Clôture vérifiée par le manager : secret temporaire `W5_GOOGLE_API_KEY`, environnement `w5-gemma-campaign` et variable `W5_BOOTSTRAP_MANIFEST_JSON` **supprimés** ; état initial des secrets/environnements/variables rétabli ;
  scanner du runner et scan indépendant sans fuite ; aucune ressource de campagne (projet, branche, PR, issue) créée ; seule référence conservée volontairement :
  `collegue-business-claims/w5-gemma-20261012-9e7c4d76` (la revendication durable, qui interdit la réutilisation de l'identifiant). Fixture (graine, socle V2, rulesets) et `main` inchangés.
- Preuves du manager : `reports/w5-manager-result-w5-gemma-20261012-9e7c4d76.md`, `evidence/w5-manager-w5-gemma-20261012-9e7c4d76/` (dont `manager-independent-verdict.json`).

## 3. Règles en vigueur

- **Cette campagne est close et ne se relance jamais** (identifiant consommé). Une nouvelle campagne réelle exige un accord explicite, un identifiant neuf et une enveloppe explicite, et la remise en place
  temporaire de l'environnement, du secret et de la variable, sur ordre exact du manager.
- Aucun appel modèle, **`countTokens` compris**, aucun dispatch ni rerun de `consolidation-e2e.yml`, de `w5-product-preflight.yml` ou du nightly sans ordre exact ; aucun accès à un fichier de clé ; aucun
  changement de secret, de protection ou de ruleset ; aucune mutation de la fixture (`scripts/w5_fixture_bootstrap.py apply|probe|cleanup`) sans le jeton d'ordre.
- Un préflight vert ne prouve ni BUILD, ni R04/R05, ni les modèles : il établit que le produit franchit ses contrôles contre GitHub sans émettre. Une campagne incomplète n'est pas un échec masqué ni un succès.
- Ne jamais convertir une inconnue en succès : usage inconnu après émission ≠ absence d'émission ; point d'arrêt ≠ câblage absent.

## 4. Travaux en cours (aucun intégré à ce jour)

| Lot | Objet | État |
|---|---|---|
| **B28** (branche séparée depuis `b88e7bf`) | corriger le compte rendu hérité du préflight : compteur d'émissions exact ou explicitement non établi, usage inconnu distingué d'une absence d'émission, point d'arrêt et callbacks « non atteints » distingués d'un câblage absent ; **aucun** changement des plafonds, autorisations, retries, inconnues ni du verdict | patch **non accepté** : C n'intègre qu'avec le SHA accepté communiqué par le manager. Sondes immuables du manager : `test_w5_manager_post500_report.py` (3 échecs par assertion et 2 témoins sur `b88e7bf`) |
| **A35 / A36** | diagnostic hors ligne du HTTP 500 (requête reconstruite sans appel, faux fournisseur), calibration des conclusions | cause indéterminée ; décisions du manager attendues sur toute évolution du transport (champ `model`, politique de retry 5xx, diagnostic sûr du corps d'erreur) |
| **C** | ce document, préparation de l'intégration et plan de vérification (§ 5) | local |

Propriété inchangée : A (`collegue/broker/**`, runtime, transport), B (`pilot/w4_business.py`, `pilot/w5_business*`, tests et `w5-business.md`), C (workflows, scripts CI, documents généraux). C ne touche pas aux fichiers de A/B.

## 5. Plan de vérification du prochain tour C (après SHA acceptés par le manager)

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

## 6. Disponibilité de C

Prêt à intégrer B28 (et A si le manager l'ordonne) dès réception des SHA acceptés ; rien n'est intégré avant.
