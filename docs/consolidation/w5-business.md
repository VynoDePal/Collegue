# Vague 5 (B) — campagne réelle complète : activation, R04 / R05, durée de vie, socle prouvé, garde de fusion

Complète `w4-e2e.md` (qui décrit l'état historique de la campagne W4). Code : `collegue/pilot/w5_business.py`, `collegue/pilot/w4_business.py`
(lifecycle, préflight, rapport, preuve de dépense), `collegue/pilot/w5_business_policy.py` (garde de fusion de la fixture),
`collegue/pilot/merge_policy.py` (chemin de fusion commun), `collegue/pilot/nightly_e2e.py` (base posée sur le socle). Aucune génération
réelle n'est faite par ces modules à l'import ni par les tests.

## 1. Parcours et rapport

`run` (étape de préflight **launch**, clé légitime, jamais affichée) : revendication → activation → BUILD → R02 → **R04** → **R05** → R03 → R06.

| Étape | Contenu | Réussite exige |
|---|---|---|
| P01–P08 | contexte, enveloppe, portée des clés (seule `LLM_API_KEY` est la clé de campagne), identité du dépôt, routes, capacité, protections W3, image du gate | voir `w4-e2e.md` |
| P09 | modèles imposés : `gemma-4-31b-it` pour tous les rôles, repli du **codeur seulement** `gemma-4-26b-a4b-it`, destination Google, aucun détournement par rôle, aucun substitut (abonnement) | exactitude |
| P10 | relais budgétaire sélectionné **et** preuve de capacité du transport réellement instancié (`collegue.broker.capability_proof`, lot A) | `accepted is True` |
| P06 | avec `LLM_TRANSPORT=budget_broker`, la capacité du worker est celle de la preuve du relais | idem |
| P11 | socle de bootstrap **prouvé par l'API** (§ 2), y compris la lecture des exécutions Actions par le jeton | tous les contrôles |
| P12 | identifiant de campagne jamais revendiqué | référence absente |
| R01 | revendication, activation (scope + qualification réelle), plan, approbation, synchronisation, trois tâches BUILD | `completed` |
| R02 | vérification métier en conteneur durci (base vierge, HTTP, PDF **réellement extrait**, redémarrage, mention légale) | `passed` |
| R04 | amélioration réelle LIVRÉE (fusionnée par Phase 5) | § 4 |
| R05 | incident déterministe signalé, rollback Phase 5 prouvé, acquittement CAS, reprise | § 4 |
| R03 | preuve de dépense : TOUT l'historique des lectures du registre (§ 3) | aucune inconnue, aucun blocage, aucune lacune non réparée |
| R06 | nettoyage UNIQUE, après toutes les phases ; sa propre fenêtre (600 s), sans génération | pas d'erreur |

Le verdict est `validated` seulement si **toutes** les étapes requises ont réussi. Exit : 0 validé, 1 échec, 3 validation incomplète, 4 arrêt budget.

## 2. Socle (manifeste `collegue-fixture-bootstrap/1`) et garde de fusion — jamais une preuve à lui seul

### Socle

`validate_bootstrap_manifest` relit par l'API, au préflight **et** juste avant la création de la base : forme fermée du manifeste ;
identité du dépôt (id, nom, public, branche par défaut) ; `main` toujours égal à la graine `8e3691d8…` ; `bootstrap_sha` descendant **direct** de la
graine (`ahead_by == 1`) ; arbre Git réel = graine + exactement les fichiers approuvés (octets hachés relus) ; seul `requirements.txt` de la graine
peut être modifié (`modified_seed_files`, `modified_seed_hashes` = hachage de la graine ET de la version approuvée, vérifiés par l'API ; décision
manager : aligner sur la pile verrouillée de l'image) ; contrôles obligatoires parmi les ajouts : le workflow `.github/workflows/fixture-tests.yml`,
`.github/CODEOWNERS` et le verrou haché `ci/requirements-approved.lock` ; `protected_prefixes == [".github/", "ci/"]` ; `code_owner` (CODEOWNERS : signal de revue, **pas** une barrière) ;
`requirements.txt` épinglé et **couvert par le verrou** (toute dépendance hors pile est refusée, jamais ignorée) ; ruleset actif dont le check requis n'est pas exempté à la création, associé à l'application Actions sur une base stricte (le drapeau
`require_code_owner_review` est consigné à titre **informatif** : il n'est jamais compté comme protection) ; le jeton lit
les exécutions Actions. `check_producer` n'existe plus (`pull_request_target` s'exécute depuis la branche par défaut — la graine, sans workflow — et
ne se déclencherait jamais sur une base éphémère) : le contrôle est décrit par `check_workflow` et **lu sur l'arbre réel** (déclencheurs exactement
`pull_request` et `push`, aucun secret, permissions de lecture seule, un job `Fixture tests` sans condition ni `continue-on-error`, extractions sans
identifiants conservés ; `if: false` est un booléen faux en YAML : la clé est testée, pas sa valeur). La pile approuvée est annoncée au codeur
(hors ligne) dans le problème du brouillon, sans créer de quatrième tâche. Le socle de référence est la **V2** fournie par C (commit `ad56c0fa…`, branche `collegue-business/bootstrap-w5-v2`, ruleset 24793056 adopté
tel quel ; la V1 `10750c7b…` reste intacte côté distant) : les copies de référence ont exactement les sha256 que son manifeste approuve.

### Garde de PUBLICATION (`executor/pr.py::open_pr`, chemin COMMUN BUILD / IMPROVE / reprises) — AVANT toute écriture distante

Observé en C47 : GitHub a **fusionné** des PR qui modifiaient le workflow, CODEOWNERS et le verrou (0 approbation requise). Et une tête hostile
PUBLIÉE peut exécuter son workflow avec un jeton Actions : une barrière avant fusion seule est trop tardive. Le codeur n'a ni réseau ni
identifiant, la seule porte vers GitHub est `open_pr`. Sur la fixture de campagne (même identification que ci-dessous), AVANT `ensure_branch`,
toute écriture Git/Contents et toute création de PR, quatre témoins doivent avoir les MÊMES objets (chemin, mode, sha) sous `.github/` et `ci/` :
le **socle de confiance** (commit du manifeste fourni par l'environnement du lancement `W5_BOOTSTRAP_MANIFEST`, relu par l'API : schéma, dépôt, graine,
préfixes, descendant DIRECT de la graine immuable, workflow approuvé présent), la **base distante** (arbre récursif), la **base testée** et le
**contenu testé** (arbre Git du dépôt de CONTRÔLE de l'hôte, jamais le `.git` de l'agent). Puis aucun chemin du **payload réellement envoyé** ne peut
viser un contrôle (normalisation NFKC, casse, séparateurs, `./`, points/espaces terminaux ; `..` refusé) et aucun objet irrégulier (lien,
sous-module) n'est toléré sous ces racines. Une liste de changements déclarative, tronquée ou décalée de la preuve ne fait pas foi : le contenu
testé est comparé en entier. Preuve nécessaire indisponible (pas de manifeste, lecture d'arbre impossible ou tronquée) ⇒ refus. Pour une PR
préexistante (reprise), la tête distante est relue et comparée : une publication antérieure ne la rend pas sûre. Après publication, le payload
réellement publié est relu une dernière fois. Hors campagne : aucun changement de comportement ; aucun paramètre de `open_pr` ni de la
contribution ne désactive la garde. Limite : `remote_revert` publie par l'API Git Data une pré-image saine (arbre de la base, jamais du codeur) sans
passer par `open_pr` ; ses checks sont gardés avant fusion.

### Garde de fusion (chemin COMMUN BUILD / drain / reprise / Phase 5 / checks du revert)

Identification **sans drapeau** (`w5_business_policy.applies`) : dépôt = la fixture de campagne ET base `collegue-business/*`, deux valeurs
qui viennent du projet lui-même (dépôt cible, base de la PR). Aucune variable ni paramètre ne la désactive ; les installations hors campagne
(autres dépôts, autres bases) sont inchangées (testé). Pour la campagne :

1. **Intégrité des contrôles** — les entrées racine `.github/` ET `ci/` du tree distant de la tête sont identiques (type + SHA de sous-arbre) à celles
   de la base de confiance ; ajout, modification, suppression ou renommage ⇒ refus, même avec un check vert de bon nom, bonne application et bonne
   tête. Comparaison des objets Git réels (jamais une liste de fichiers, qui peut être incomplète) ; lecture impossible ou tronquée ⇒ refus. Pour le
   revert (pas de preuve de livraison) la base de confiance est le sommet courant de la base, déjà passé par la barrière à sa fusion.
2. **Check requis exigé** — `Fixture tests` de l'application 15368 doit figurer dans les protections serveur de la base, sinon refus.
3. **Provenance du check** — `check-run.id` → `actions/jobs/{id}` (404 pour un check publié par l'API des checks : refus) → `actions/runs/{run_id}` :
   job de la tête attendue et nommé `Fixture tests`, exécution de la même tête, du fichier de workflow approuvé, d'un événement `pull_request`, du
   même dépôt (sans fork), terminée avec succès. API indisponible ⇒ refus. Ceci couvre une PR qui, en modifiant son workflow, publie un check de
   bon nom et bonne application sur une AUTRE tête.

La fusion manuelle hors produit reste soumise aux seules protections GitHub : la qualification prouve le chemin annoncé de Collègue. C a observé
que `require_code_owner_review` avec 0 approbation n'empêche pas la fusion par l'auteur-propriétaire : CODEOWNERS n'est pas une protection ; les gardes de
publication et de fusion sont le rempart du produit, avec les protections GitHub réelles (PR, check requis, base à jour, aucun bypass).

## 3. Activation, budget et preuve de dépense

Avant toute création distante : (1) l'identifiant est **revendiqué** par une référence `collegue-business-claims/<id>` créée de façon exclusive
(jamais supprimée) ; (2) le scope durable `planning:cycle:<id>` est ouvert (2 USD / 250 000 tokens, strict) ; (3) `await collegue.broker.qualify_models(
settings, ledger, scope_key)` (lot A) qualifie les deux modèles sur CE scope par le vrai pipeline et rend un `QualificationReport` : B contrôle
explicitement le scope, `ok`, l'absence de blocage, une consommation établie, la destination native Google, les DEUX identités et leur rôle (31B
`default`, 26B `coder`), les trois capacités (texte, JSON, outils) de chacune avec leur identité durable `qualify:<scope>:<modèle>:<capacité>`, et
l'ÉCHÉANCE ABSOLUE durable (fuseau horaire présent, non dépassée, au plus la fenêtre de 900 s) dont il dérive le temps restant — jamais une seconde
fenêtre. Un mapping « accepté » n'est plus une preuve. Les détails de chaque capacité sont conservés dans le rapport, **y compris en refus**, et le
registre est capturé dès les canaris. (4) Le brouillon public reprend le même cycle (`--cycle-id`).

**Preuve de dépense** (`RegistryProof`, rapport `facts.registry_proof`). Une lecture du registre est obligatoire après chaque phase qui a tourné et à
la sortie. Règles :

* une lecture qui révèle une consommation inconnue, un blocage, un dépassement ou un recul des compteurs cumulatifs est un défaut **permanent** : une
  lecture ultérieure propre ne l'efface pas, aucun verdict complet ne peut être rendu, même si l'inconnue n'arrive qu'à la dernière lecture (R03 juge
  l'historique, et la lecture de sortie retire le succès déjà accordé à R03) ;
* une lecture obligatoire manquante est une **lacune** : elle ferme définitivement les émissions (la phase suivante est refusée `incomplete_validation`,
  cause d'origine conservée) et laisse la preuve incomplète ;
* une lacune ne peut être **réparée** que par la PREMIÈRE lecture ultérieure et seulement si (a) aucune émission n'a eu lieu entre-temps, (b) elle porte
  sur le même scope et ne régresse sur aucun compteur monotone par rapport à la dernière lecture valide, (c) elle ne révèle ni inconnue, ni blocage, ni
  dépassement. La réparation couvre la preuve de dépense (consignée avec sa cause) ; elle ne rouvre jamais les émissions ni ne rattrape les phases
  refusées entre-temps, qui restent non jouées : le verdict reste incomplet. En pratique seule une lacune après la dernière phase émettrice (R05) peut
  être réparée par la lecture finale.

Aucune phase qui émet ne démarre après l'échéance globale, sur un usage inconnu ou sur une enveloppe atteinte. La collecte et le nettoyage ne
génèrent rien : ils continuent après l'échéance (fenêtre propre de 600 s).

## 4. R04 / R05

* **R04** — l'entrée publique IMPROVE avec le vrai modèle (`agent=None`) retire des identifiants d'EXEMPLE factices de `docs/runbook-ops.md`.
  Exigé : preuve IMPROVE durable, les trois contrats livrés rejoués verts, gain mesuré par le vrai scan, couverture non dégradée, revue non bloquante,
  PR **purement documentaire** et **distincte** du support d'incident, fusion par Phase 5. Une PR promue mais non fusionnée n'est pas livrée.
* **R05** — injection **déterministe signalée** (`DeterministicIncidentAgent`, aucun modèle) : retire les identifiants de `docs/deploiement.md` et la
  ligne de mention légale de `docs/export_header.md`. Tout le reste est réel : mesure, revue, politique de faible risque (non élargie), checks, santé
  indépendante, vraie PR de revert avec ses checks requis, fusion, synchronisation, arbre restauré, incident `recovered`, acquittement CAS, reprise.
  À la reprise après un crash où l'incident est déjà fusionné, la base courante CONTIENT l'incident : la préimage saine n'est jamais lue dessus. Elle est
  désignée par trois témoins qui doivent coïncider — l'ancre durable de Phase 5 (`base_sha_before_merge`), le premier parent de la fusion de l'incident et
  la base livrée par R04 — et son arbre doit être celui de R04 ; un désaccord est un échec (aucune restauration contre une mauvaise préimage).
  Un lease de revert non expiré (≥ 3600 s côté produit) laisse R05 incomplète : il n'est pas raccourci pour forcer un succès.

### Comment R04 est amené à ne modifier que le runbook cible

La consigne d'amélioration du produit est générique (« corriger les N problèmes de sécurité détectés ») : rien ne désigne un fichier. Sans ciblage, un
modèle consciencieux retirerait les exemples des DEUX documents, fusionnerait, et R05 n'aurait plus rien à régresser. Le ciblage ne passe ni par un
finding inventé ni par un assouplissement de la politique : il **resserre** le réglage produit existant `AUTO_MERGE_PATH_ALLOWLIST` (défaut
`docs/**,**/*.md,**/*.rst`) à la cible de la phase, via un `Settings` construit depuis l'environnement validé avec cette seule différence :

| Phase | `AUTO_MERGE_PATH_ALLOWLIST` |
|---|---|
| R04 | `docs/runbook-ops.md` |
| R05 (injection et reprise) | `docs/deploiement.md,docs/export_header.md` |

Un modèle qui touche aussi `docs/deploiement.md` voit sa PR **refusée par la vraie politique** de Phase 5 : non fusionnée (donc jamais annoncée
livrée), support d'incident préservé, R04 `incomplete_validation` avec les motifs du produit. Le modèle n'est pas instruit par ce mécanisme (il n'a
que la consigne générique) : un modèle hors cible rend la campagne **incomplète**, jamais réussie. Un marqueur « document d'exercice » dans
`docs/deploiement.md` augmenterait la probabilité d'un R04 dans la cible, mais change les octets du socle (donc `bootstrap_sha` et le jeton d'application
de C) : proposition au manager, non appliquée ici.

## 5. Limites

* Aucune génération réelle n'a eu lieu : tout est prouvé sur le produit, un vrai Git local et des clients de test. Le raccord à `qualify_models` et
  `capability_proof` a été rejoué contre le code d'A (arbre en cours, non figé) avec un faux fournisseur amont dans une copie de travail, hors dépôt.
* R04 dépend du comportement du modèle (fichiers touchés, taille ≤ 50 lignes) ; un écart est un refus conservé, jamais un succès.
* La santé s'exécute dans le sandbox du gate ; la présence de `timeout`, `pypdf` et de la pile des oracles dans l'image est contrôlée par P08.
* `remote_revert` fusionne le revert d'une fusion déjà gardée : il passe par `verify_required_checks`, qui applique désormais la même garde
  (contrôles protégés et provenance) à la campagne.
* La garde interdit, pour la campagne, toute modification de `.github/` et `ci/` : une évolution légitime du socle exige un nouveau socle et un
  nouvel identifiant de campagne.

## 6. Lancement CLI sur base PROTÉGÉE (B25)

Deux raccords que des faux JSON à la place de `adapter.product` ne prouvaient pas, désormais exercés par la VRAIE CLI en processus
(`tests/test_w5_business_public_launch.py` : argparse, validations, `runtime`, `github_sync`, vrais clients GitHub derrière un vrai dépôt Git dont la
frontière HTTP REFUSE les écritures directes sur la base, avec le texte réel GH013) :

1. **`--nightly-exact-task-count 3`** : la CLI acceptait seulement `1` (smoke nightly) alors que le lanceur de la campagne émet `3` et que le
   décomposeur accepte `[1, MAX_TASKS]`. La validation suit désormais la borne du décomposeur ; `0`, négatif, `> MAX_TASKS`, non entier restent refusés à
   l'analyse des arguments (aucun appel de modèle) ; le témoin nightly (`1`) et l'absence de l'option sont inchangés.
2. **SPEC sur une base protégée** : `plan sync --execute` committe `SPEC.md` par un PUT Contents DIRECT sur la base, que le ruleset (PR obligatoire,
   check requis, base à jour, aucun bypass) refuse : la campagne se serait arrêtée à `plan sync`, planification dépensée, avant tout BUILD.
   Parcours minimal et conforme (`pilot/w5_business_spec.py`, appelé par `launch_campaign(materialize_spec=…)` entre l'approbation et le
   `plan sync`) : la SPEC vient du SNAPSHOT approuvé (jamais d'un argument) ; SPEC déjà identique ⇒ rien ; divergente ⇒ refus ; protections serveur,
   socle de confiance et contrôles de la base vérifiés ; branche de tête `collegue-spec/<tag>` hors du motif protégé, créée sur le sommet de la base
   (ou reprise si elle porte EXACTEMENT base + blob SPEC, refusée sinon, jamais réécrite) avec UN seul fichier ; tête relue sur l'arbre distant ;
   PR documentaire (pas une tâche BUILD, aucune preuve de livraison fabriquée) ; checks REQUIS et provenance réelle (check-run → job → exécution)
   attendus dans l'échéance globale de 900 s (temps en général d'une à deux minutes sur le workflow de la fixture, pris sur la fenêtre) ; fusion à la
   tête et à la base exactes ; réponse de fusion perdue ⇒ relecture de la PR, jamais de seconde fusion ; commit de fusion relu (parent = base, arbre =
   base + blob) ; SPEC distante relue identique, que `plan sync` relit alors identique (aucun PUT). Un refus à une étape est un arrêt explicite
   (`SpecMaterializationError`, échec de R01) ; l'échéance atteinte est un arrêt budget. Aucun contournement, aucune écriture directe, aucun
   relâchement du ruleset, aucun code métier ajouté au socle.
3. **Nettoyage** (`cleanup_campaign(before=…)` → `cleanup_campaign_resources`) : le nettoyage nightly refuse une PR ouverte non corrélée, une tête
   `collegue/issue-N` dont le SHA n'est pas consigné et une base déplacée par autre chose que le commit de SPEC — donc, après les fusions de la campagne, il
   aurait conservé toutes les ancres. Avant lui : la PR/branche de la SPEC sont connues d'un fichier d'intention écrit AVANT leur création (PR encore ouverte
   fermée avec gardes d'identité, branche supprimée si son sommet est celui consigné) ; les PR d'amélioration/revert ouvertes sont fermées (gardes) ;
   les têtes `collegue/issue-N` de PR fusionnées sont consignées au manifeste (sommet = tête de la PR, marqueur du corps) et les têtes
   d'amélioration/revert fusionnées supprimées ; la base enregistrée avance au sommet courant seulement s'il descend de l'ancienne base par la chaîne du
   premier parent et si ses contrôles sont ceux du socle.
Limites : le nettoyage nightly complet n'est pas simulé contre un GitHub réel (issues, labels, base) ; seules ses préconditions le sont.
