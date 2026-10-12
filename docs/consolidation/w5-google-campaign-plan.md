# Lot diagnostic Google, puis campagne réelle unique — préparation (C81)

**État (12 octobre 2026, base `main` = `ed548683175fdc88967e2e116d41b66071da76f2`).** L'utilisateur a répondu « Vas y » à : *livrer un petit lot de diagnostic des erreurs HTTP Google, puis lancer UNE nouvelle campagne réelle*
(identifiant neuf, **2 USD / 250 000 tokens / 900 s** au total, canaris compris). **Une campagne NEUVE est donc autorisée, mais elle n'est pas lancée** : le manager ordonnera séparément la publication du lot, sa fusion sur un SHA précis, la
préparation temporaire de la clé après livraison, puis le dispatch exact. Ce document est une **préparation** : aucune des commandes de la § 6 n'a été exécutée, aucun appel Google (`countTokens` compris), aucune lecture de clé, aucune mutation GitHub.
Les campagnes closes restent des faits historiques intacts : `w5-gemma-20261010-dcf645da` (arrêtée en P07, zéro émission) et `w5-gemma-20261012-9e7c4d76` (une génération du 31B, HTTP 500, 266 tokens `unknown`, registre intact — SHA-256
`ac1df7da8cba9aaf362768a080e0d77fe72f42f6408501d7d220a7d3cbb90321`) ; leurs identifiants sont consommés, elles ne se relancent jamais (voir [`w5-post500.md`](w5-post500.md)).

## 1. Le lot de diagnostic : objet, responsabilités, critères

**Objet.** Conserver, pour une réponse HTTP non-200 de Google **lue par le vrai transport dans les bornes déjà imposées**, un diagnostic **fermé et borné** qui permette de *classer* l'erreur lors de la prochaine campagne. La cause interne du précédent 500 reste
**indéterminée** ; ce lot ne la cherche pas par des variations expérimentales. **Hors périmètre, inchangés** : corps Google, endpoint, modèles, allocations, réservations, admissions, états `unknown`, retries, repli, verdicts, invariant « corps compté = corps émis ».
Le diagnostic ne décide jamais de la comptabilité.

**Contrat commun** (brief `w5-google-diagnostics-common.md`) : `UpstreamHTTPError.diagnostic`, dictionnaire de données fermées (vide pour un ancien double), pour une réponse non-200 du vrai `GoogleUpstream` lue dans les bornes existantes (plafond de réponse inchangé) : `operation` (`countTokens` ou
`generateContent`), `http_status` (entier observé), `body_kind` (`json` / `html` / `other` / `empty` / `invalid_json`), `rpc_status` (valeur exacte d'un statut Google RPC fermé de 17 valeurs, ou `absent` / `other`), `request_id_present` (booléen, **jamais** la valeur
ni un hachage). Aucun message libre du fournisseur, corps brut, URL ni valeur d'en-tête n'est conservé. La donnée est ajoutée de façon bornée (< 300 caractères) aux messages contrôlés et à `AttemptRecord.error_detail` existant, et remonte au rapport par les
chemins existants ; aucun nouveau registre, migration ni système de journalisation. Un parsing défaillant (JSON invalide, doublons, types inattendus, profondeur) ne lève aucune erreur nouvelle et ne masque jamais l'état comptable ; le plafond de réponse existant est conservé : **un corps qui le dépasse garde le refus existant (`UpstreamBadBody`) et n'a pas forcément ce diagnostic** — le lot ne promet pas de couvrir toute réponse que le transport ne lit pas jusqu'au bout.

| Rôle | Possède | Rend |
|---|---|---|
| **A** | `collegue/broker/upstream.py`, éventuel `collegue/broker/diagnostics.py`, raccords diagnostics seuls de `collegue/broker/service.py`, `tests/test_w5_broker_upstream.py`, `tests/test_w5_broker_diagnostics.py`, `w5-google-diagnostics.md` | implémentation, tests rouge→vert, rapport |
| **B** | `tests/test_w5_google_diagnostics_security.py` (intégration, cas adverses), `w5-google-diagnostics-tests.md` | tests adverses, aucun code produit |
| **C** | `AGENTS.md`, `CLAUDE.md`, ce document, `protocole.md` si utile ; plan de campagne ; **intégration et livraison** | intégration des SHA acceptés, suite complète, Ruff, wheel/migrations, PR + checks + fusion en deux étapes sous ordre |

**Critères d'intégration par C** (sur SHA acceptés par le manager, jamais avant) : fusion par SHA exact sans conflit fonctionnel ; relecture du diff contre la liste « inchangé » ci-dessus (aucun plafond, réservation, admission, inconnue, retry, repli ni verdict ne bouge, corps compté = corps émis) ; absence de fuite (aucun texte fourni par le fournisseur ni valeur d'en-tête dans `error_detail`, les messages, les rapports, les artefacts ; revue du scanner de fuite) ; tests hors ligne uniquement (aucun appel réel) ; suite complète dans un home neuf avec **vraie ligne de résumé et JUnit** (un seul `-q`), Ruff check ET format complets,
`git diff --check`, `scripts/locks.py check`, roue hors checkout + migrations SQLite vers `0013` ; Docker/OpenHands/PostgreSQL réels et les **cinq checks requis** (`Ruff`, `Pytest (Python 3.11)`, `Pytest (Python 3.12)`, `Dependency audit`, `Docker build`) en CI uniquement ; revues lues en texte intégral (quota ≠ avis favorable).

## 2. Collecte du diagnostic fermé — ce qu'elle établit et ce qu'elle n'établit pas

- **Où elle apparaît** (si une erreur HTTP survient pendant la campagne) : exception du transport (`diagnostic`) → message contrôlé du courtier → `AttemptRecord.error_detail` (registre durable SQLite, artefact `w5-business-registry`) → rapport de qualification et de campagne (artefact `w5-business-report`). Les identifiants **locaux** de tentative et leurs horodatages existent déjà pour la corrélation.
- **Ce qu'elle apporte** : statut HTTP, opération, famille de corps, statut RPC fermé, présence d'un identifiant de requête amont — de quoi *classer* (par exemple `INTERNAL`/`UNAVAILABLE` vs `RESOURCE_EXHAUSTED` vs `PERMISSION_DENIED`).
- **Ce qu'elle ne fait pas** : établir à elle seule une cause ou une origine (la forme du corps ne prouve pas l'origine ; le statut RPC n'explique pas une cause interne ; l'absence d'identifiant ne prouve rien), ni autoriser un retry, un repli ou un changement de comptabilité. Si un 500 (issue ambiguë, cas 3) survient de nouveau, la campagne s'arrête sur la même protection budgétaire (usage `unknown`, scope bloqué) et le diagnostic *classe* l'erreur sans la résoudre ; un rejet établi (cas 2) ou un échec de `countTokens` (cas 1) suit la politique propre à son cas.
- **Politique d'issue inchangée — le diagnostic ne décide de RIEN.** Le sort d'une erreur reste celui du code existant du courtier (`collegue/broker/service.py`, `PROVEN_REJECTED_STATUS = {400, 401, 403, 404, 405, 413, 415, 422, 429}`) ; le lot n'ajoute aucun arrêt nouveau sur « n'importe quel non-200 » :
  1. **Échec de `countTokens`** (HTTP, transport, corps inexploitable) : **aucune génération n'a pu partir et aucune réservation n'est prise** (la réservation vient après `countTokens`) ; la tentative est libérée (`count_tokens_failed`), le refus est marqué réessayable côté courtier (renvoyer la même requête est sans risque) ; la **qualification est interrompue** sur cet échec ; autorisations de retry du produit inchangées. Aucune consommation, connue ou inconnue.
  2. **Génération ayant reçu un rejet établi** (statut dans `PROVEN_REJECTED_STATUS` ; de même un fournisseur injoignable *avant envoi*, où rien n'est émis) : **réservation libérée** (`upstream_rejected`), **non assimilée à une consommation inconnue**, scope non bloqué ; la qualification s'arrête sur l'échec ; **les autorisations de retry/repli du produit restent inchangées**.
  3. **Génération d'issue ambiguë** (par exemple HTTP 500 ou tout statut hors de l'ensemble ci-dessus, transport perdu après émission, corps de réponse inexploitable, interruption) : **réservation inconnue conservée, scope bloqué, aucune nouvelle émission ni repli** (c'est le cas de la campagne du 12 octobre).
- Elle n'est vérifiée qu'**hors ligne** (faux fournisseur) jusqu'au dispatch réel ; aucune autre télémétrie n'est ajoutée.

## 3. Revue en lecture seule des workflows (fichiers de `main` `ed54868`, aucune modification justifiée pour ce lot)

**`consolidation-e2e.yml` (campagne, `workflow_dispatch` seul).** Entrées : `confirm` (phrase exacte `LANCER-UNE-FOIS-2USD-250000TOKENS-900S`, condition du job) et `campaign_id` (regex `^[a-z0-9][a-z0-9-]{2,39}$`, **neuf**). Job unique, `environment: w5-gemma-campaign`,
40 min max, concurrence `consolidation-e2e` ; enveloppe `2 / 250000 / 900` et réglages du transport au niveau du job (identiques au préflight dédié, un test les compare) ; la variable de dépôt `W5_BOOTSTRAP_MANIFEST_JSON` alimente le manifeste ; le secret de dépôt
`INTEGRATION_FIXTURE_GITHUB_TOKEN` n'est exposé que dans les étapes de lecture/campagne/nettoyage ; **`W5_GOOGLE_API_KEY` (secret d'environnement) n'atteint que l'étape de campagne, sous le nom `LLM_API_KEY`**. Ordre : santé Phase 5 → migrations → préflight statique (sans clé) → image du courtier → préflight complet (sans clé, P08 inclus) → campagne (`w4_business run`) →
scanner de fuite (`scripts/w5_leak_scan.py`, clé cherchée en octets et en noms) → nettoyage `always()` → dépôt des artefacts scannés (`w5-business-{report,registry,resources,diagnostic}`). Un préflight refusé = « validation incomplète » (code 3) avant toute émission.

**`w5-product-preflight.yml` (préflight dédié sans modèle).** `workflow_dispatch` seul, même phrase pour P01, aucun environnement GitHub, jeton de la fixture dans deux étapes, identifiant `w5-preflight-<run_id>` jamais revendiqué ; produit une provenance typée (`validated` seulement si les deux étapes sont complètes, arbres git prouvés, zéro émission, scanner propre) ; une relance Actions (tentative 2) est refusée par P01.

**Points d'attention** : (a) un dispatch **doit viser `main`** (le fichier n'est déclenchable que sur la branche par défaut, et l'environnement de campagne est limité à `main`) ; (b) `INTEGRATION_E2E_ENABLED` doit rester faux (**vérifié : `false`**) sinon P01/P02 refusent ; (c) le workflow de campagne relit son manifeste d'une **variable** : sans elle, P11 est incomplet ; (d) la campagne rejoue elle-même un préflight statique + complet sans clé, le préflight dédié préalable est une ceinture supplémentaire, pas un substitut.

## 4. Fixture V2 et intégrité (GET en lecture seule, 12 octobre 2026)

| Vérification | Constat |
|---|---|
| `main` de la fixture | `8e3691d8e4f311e00d620c9c2ca2d9edbd8b136a` (graine, inchangée) |
| Socle V2 `collegue-business/bootstrap-w5-v2` | `ad56c0fa03066b1f6efb3cf6a5aa26cb496372ca`, parent unique = graine, arbre `6e6bea13c2d45cd848349b853aceec7fea5f7e85` |
| Fichiers approuvés (6) relus par l'API au SHA V2 vs hachages du manifeste | **conformes** (`.github/CODEOWNERS`, `.github/workflows/fixture-tests.yml`, `ci/requirements-approved.lock`, `docs/deploiement.md`, `docs/runbook-ops.md`, `requirements.txt`) |
| Manifeste suivi `tests/fixtures/w5-business/manifest.v2.json` | 1978 octets, SHA-256 `49693d403b9be0e5e20e186d43625a939e04ef99566d766950f1e20bece6b6e6` (identique à la variable utilisée pour la campagne du 12 octobre) |
| Rulesets de la fixture | `18840666` (graine, actif) et `24793056` (bases `collegue-business/*`, actif) ; pas de protection classique (404 attendu) |
| PR / issues ouvertes de la fixture | 0 / 0 |
| Références `collegue-business*` | `collegue-business/bootstrap-w5` (V1, historique), `bootstrap-w5-v2` ; **revendications** : seulement `collegue-business-claims/w5-gemma-20261012-9e7c4d76` (conservée) |
| Produit : `main` | `ed548683…` ; rulesets `17899028` (PR, cinq checks stricts, **aucun bypass**) et `13827133` ; runs actifs : 0 |
| Produit : secrets / environnements / variables | `W5_GOOGLE_API_KEY` **absent**, environnement `w5-gemma-campaign` **absent** (404), variable `W5_BOOTSTRAP_MANIFEST_JSON` **absente** ; `INTEGRATION_FIXTURE_GITHUB_TOKEN` présent (mis à jour le 12 juillet 2026) |

Preuve : `evidence/w5-c81-remote-readonly.log`. Ces lectures datent de la préparation : **à refaire juste avant le dispatch** (le P11 du produit revalide de toute façon le socle V2 par l'API : graine immuable, ascendance, contenu exact, check requis).

## 5. Secret temporaire et revendication durable de l'identifiant

- **Secret temporaire** : `W5_GOOGLE_API_KEY`, *secret d'environnement* de l'environnement `w5-gemma-campaign`, créé par le manager **après** la livraison et le préflight vert, retiré à la fin (même sur refus). Le workflow ne le référence qu'une fois, dans l'étape de campagne, sous `LLM_API_KEY` ; aucun autre nom de clé n'est admis (P03 le refuse). L'environnement observé lors du 12 octobre avait une politique de branche **`main` seulement** (`deployment_branch_policy`). Variable temporaire `W5_BOOTSTRAP_MANIFEST_JSON` = octets exacts du manifeste suivi du SHA accepté (SHA-256 à comparer). Rien de cela n'existe aujourd'hui.
- **Revendication durable** : `collegue-business-claims/<campaign_id>` sur la graine, créée en **exclusivité** par le produit **avant** toute création distante et **jamais supprimée** par le nettoyage ; P12 (préflight) vérifie qu'elle n'existe pas ; une revendication existante ou un échec de création refuse le lancement. Conséquence : le dispatch consomme l'identifiant même si la campagne s'arrête tôt ; un nouvel essai exige un nouvel identifiant et un nouvel accord. Identifiant neuf au format `w5-gemma-AAAAMMJJ-<8 hex>`, jamais réutilisé, vérifié absent juste avant.

## 6. Plan de commandes exact (NON exécuté à cette étape)

`<SHA>` = futur `main` accepté (après livraison du lot diagnostic) ; `<ID>` = identifiant neuf ; `P` = `VynoDePal/Collegue`, `F` = `VynoDePal/collegue-e2e-fixture`. Attentes bornées à 60 s, un relevé toutes les 45 à 60 s.

**A. Prérequis et lectures seules (avant chaque étape distante)**
```
gh api repos/P/git/ref/heads/main --jq .object.sha                        # = <SHA>
gh api "repos/P/actions/runs?status=in_progress" --jq .total_count; (idem queued, waiting)   # 0
gh api repos/P/actions/variables/INTEGRATION_E2E_ENABLED --jq .value      # false
gh api repos/P/environments/w5-gemma-campaign; gh api "repos/P/actions/variables?per_page=100"; gh api repos/P/actions/secrets   # absences attendues avant préparation
gh api repos/F/git/ref/heads/main --jq .object.sha                        # 8e3691d8…
gh api repos/F/git/ref/heads/collegue-business/bootstrap-w5-v2 --jq .object.sha   # ad56c0fa…
gh api "repos/F/git/matching-refs/heads/collegue-business-claims"         # <ID> absent
gh api "repos/F/pulls?state=open" --jq length; gh api "repos/F/issues?state=open" --jq length   # 0 et 0
git archive <SHA> tests/fixtures/w5-business/manifest.v2.json | tar -xO | sha256sum   # 49693d40…
```

**B. Livraison du lot (C, ordres distincts du manager)** : intégration locale des SHA A/B acceptés → vérifications locales de la § 1 → push normal → PR → observation des cinq checks et des revues → fusion squash `gh pr merge <n> --repo P --squash --match-head-commit <tête>` sur ordre → contrôle de `main` (SHA, arbre, parent, cinq checks du push).

**C. Préflight dédié sans clé sur `<SHA>` (une seule fois)**
```
gh workflow run w5-product-preflight.yml --repo P --ref main -f confirm=LANCER-UNE-FOIS-2USD-250000TOKENS-900S
gh run list --repo P --workflow w5-product-preflight.yml --event workflow_dispatch --limit 2 --json databaseId,headSha,status,conclusion
# attendu : headSha=<SHA>, tentative 1, statique 11 requis réussis + P08 facultatif non joué, complet 12/12, provenance « validated », zéro émission, scanner propre ; ne jamais relancer (tentative 2 refusée par P01)
```

**D. Préparation temporaire de la configuration (manager seul ; ordre distinct, après C vert)** — *commandes déduites de l'état observé le 12 octobre (environnement avec politique de branche `main`) et de l'API GitHub, à ajuster à la procédure réelle du manager* :
```
gh api -X PUT repos/P/environments/w5-gemma-campaign -F 'deployment_branch_policy[protected_branches]=false' -F 'deployment_branch_policy[custom_branch_policies]=true'
gh api -X POST repos/P/environments/w5-gemma-campaign/deployment-branch-policies -f name=main -f type=branch
gh variable set W5_BOOTSTRAP_MANIFEST_JSON --repo P < <(git archive <SHA> tests/fixtures/w5-business/manifest.v2.json | tar -xO)   # puis comparer le SHA-256 relu à 49693d40…
gh secret set W5_GOOGLE_API_KEY --env w5-gemma-campaign --repo P           # en DERNIER ; clé lue par le seul manager, jamais affichée ni écrite dans un journal
```

**E. Dispatch unique (ordre exact du manager, une seule exécution, enregistrer la requête avant)**
```
gh workflow run consolidation-e2e.yml --repo P --ref main -f confirm=LANCER-UNE-FOIS-2USD-250000TOKENS-900S -f campaign_id=<ID>
gh run list --repo P --workflow consolidation-e2e.yml --event workflow_dispatch --limit 3 --json databaseId,headSha,createdAt,status   # run unique, headSha=<SHA>, tentative 1
```
**F. Suivi** : `gh run view <run> --json status,jobs` toutes les 45 à 60 s ; ni `rerun`, ni second dispatch, ni nouvel essai si le run s'arrête.

**G. Clôture (même sur refus ou arrêt)** :
```
gh secret delete W5_GOOGLE_API_KEY --env w5-gemma-campaign --repo P
gh variable delete W5_BOOTSTRAP_MANIFEST_JSON --repo P
gh api -X DELETE repos/P/environments/w5-gemma-campaign
# vérifier l'absence des trois, l'égalité des secrets/variables/environnements préexistants avec l'état initial, la revendication <ID> conservée sur la graine,
# télécharger les artefacts (scanner : zéro fuite), relire le registre SQLite en lecture seule (integrity_check, tentatives, réservations, unknown), conserver les preuves sans réécriture
```

## 7. Prérequis, critères d'arrêt, limites

**Prérequis** : lot diagnostic livré sur `main` (cinq checks du push verts, SHA accepté communiqué par le manager) ; préflight dédié vert sur ce SHA ; ordre explicite et distinct pour D puis E ; identifiant neuf libre ; `INTEGRATION_E2E_ENABLED=false` ; aucun run actif ; fixture inchangée (§ 4) ; quota Google et clé préparés par le manager seul ; scratch et gros logs sous `/tmp`, racine du disque très réduite.

**Critères d'arrêt** (aucune réaction automatique) : tout refus de préflight (code 3, avant émission) ; une **génération d'issue ambiguë** (par exemple HTTP 500) → usage `unknown`, scope bloqué, **aucune nouvelle émission, aucun repli, aucune seconde campagne** ; un **échec de `countTokens`** interrompt la qualification sans consommation ; un **rejet établi** (`PROVEN_REJECTED_STATUS`) libère la réservation et arrête la qualification sur l'échec, sous les autorisations de retry/repli inchangées du produit (voir § 2 : le diagnostic ne décide de rien) ; échéance de 900 s atteinte ; plafond atteint ; fuite détectée par le scanner. Le résultat est alors consigné tel quel (validation incomplète), sans le convertir en succès.

**Ce que la campagne peut prouver** : les canaris des deux modèles (capacités JSON/outils du 31B, trois capacités du 26B), la borne de sortie réelle et, si le parcours aboutit, BUILD (3 tâches dépendantes), amélioration R04, incident contrôlé et rollback R05. **Ce qu'elle ne peut pas** : diagnostiquer à coup sûr un nouveau refus Google (le diagnostic classe), ni être relancée sous le même identifiant, ni valider autre chose que ce qui a été réellement joué.

**Limites de cette préparation** : le code du lot n'est pas écrit au moment de ce document (A/B en cours) ; son contrat est celui du brief commun et devra être recoupé avec `w5-google-diagnostics.md` à l'intégration ; les lectures de la § 4 datent de la préparation ; les commandes de la § 6 n'ont pas été exécutées et celles de D sont déduites ; aucune permission n'a été contournée.
