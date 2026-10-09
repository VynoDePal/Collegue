# Vague 5 (B) — campagne réelle complète : activation, R04 / R05, durée de vie, socle prouvé

Complète `w4-e2e.md` (qui décrit l'état historique de la campagne W4). Code : `collegue/pilot/w5_business.py`, `collegue/pilot/w4_business.py`
(lifecycle, préflight, rapport), `collegue/pilot/nightly_e2e.py` (base posée sur le socle), `collegue/pilot/merge_policy.py`
(barrière d'intégrité des contrôles). Aucune génération réelle n'est faite par ces modules à l'import ni par les tests.

## 1. Parcours et rapport

`run` (étape de préflight **launch**, clé légitime, jamais affichée) : revendication → activation → BUILD → R02 → **R04** → **R05** → R03 → R06.

| Étape | Contenu | Réussite exige |
| --- | --- | --- |
| P01–P08 | contexte, enveloppe, portée des clés (`GOOGLE_API_KEY`, `LLM_API_KEY[_RÔLE]`, …), identité du dépôt, routes, capacité, protections W3, image du gate | voir `w4-e2e.md` |
| P09 | modèles imposés : `gemma-4-31b-it` pour tous les rôles, repli du **codeur seulement** `gemma-4-26b-a4b-it`, destination Google, aucun détournement par rôle, aucun substitut (abonnement) | exactitude |
| P10 | relais budgétaire sélectionné **et** preuve de capacité du transport réellement instancié (interface publique d'A) | `accepted is True` |
| P06 | avec `LLM_TRANSPORT=budget_broker`, la capacité du worker est celle de la preuve du relais (la matrice des workers à clé directe ne décide plus) | idem |
| P11 | socle de bootstrap **prouvé par l'API** (§ 2) | tous les contrôles |
| P12 | identifiant de campagne jamais revendiqué | référence absente |
| R01 | revendication de l'identifiant, activation (scope + qualification), plan, approbation, synchronisation, trois tâches BUILD | `completed` |
| R02 | vérification métier en conteneur durci (base vierge, HTTP, PDF **réellement extrait**, redémarrage, mention légale) | `passed` |
| R04 | amélioration réelle LIVRÉE (fusionnée par Phase 5) | §3 |
| R05 | incident déterministe signalé, rollback Phase 5 prouvé, acquittement CAS, reprise | §4 |
| R03 | registre final dans l'enveloppe | bornes respectées |
| R06 | nettoyage UNIQUE, après toutes les phases | pas d'erreur |

Le verdict est `validated` seulement si **toutes** les étapes requises ont réussi. Exit : 0 validé, 1 échec, 3 validation incomplète, 4 arrêt budget.

## 2. Socle (manifeste `collegue-fixture-bootstrap/1`) — jamais une preuve à lui seul

`validate_bootstrap_manifest` relit par l'API, au préflight **et** juste avant la création de la base : forme fermée du manifeste ;
identité du dépôt (id, nom, public, branche par défaut) ; `main` toujours égal à la graine `8e3691d8…` ; `bootstrap_sha` descendant
**direct** de la graine (`ahead_by == 1`) ; arbre Git réel = graine + exactement les fichiers approuvés (modes `100644` seulement) ;
octets de chaque fichier relus et comparés au sha256 approuvé ; les huit fichiers de la graine inchangés, **sauf**
`requirements.txt` si le manifeste le déclare dans `modified_seed_files` (les deux versions sont hachées et doivent différer ;
uniquement des versions **épinglées**, aucune source externe) ; ajouts limités à un workflow, des documents `docs/*.md` et des listes
`requirements*.txt|in` (aucune implémentation métier) ; workflow producteur du check requis (`check_producer` : déclencheur
`pull_request_target` seul, aucun secret, permissions minimales, extractions sans identifiants, étape qui publie `Fixture tests`
sur `github.event.pull_request.head.sha`) ; ruleset actif ; check requis `Fixture tests` associé à l'application déclarée
(`check_app_id`) sur une base stricte. Les contrôles (`.github/`) sont relus après le BUILD et après chaque phase.

**Barrière de fusion** (`merge_policy`, chemin COMMUN BUILD / drain / reprise / Phase 5) : le sous-arbre `.github/` du tree de la tête
doit être identique à celui de la base de confiance (SHA de sous-arbre Git), même si la contribution fabrique un check vert de la bonne
application et de la bonne tête ; lecture impossible = refus.

## 3. Activation et budget

Avant toute création distante : (1) l'identifiant est **revendiqué** par une référence `collegue-business-claims/<id>` créée de façon
exclusive (jamais supprimée par le nettoyage : un identifiant consommé ne donne pas d'essai gratuit) ; (2) le scope durable
`planning:cycle:<id>` est ouvert (2 USD / 250 000 tokens, strict) ; (3) les deux modèles sont qualifiés sur CE scope par l'API publique du
lot A (aucune estimation de repli) ; (4) le brouillon public reprend le même cycle (`--cycle-id`). Le registre est relu **par scope** dès
l'activation, après chaque phase et à chaque sortie. Aucune phase qui émet ne démarre après l'échéance globale, sur un usage inconnu
ou une enveloppe atteinte. L'échéance durable du relais ne fait que **resserrer** la fenêtre de la campagne.

## 4. R04 / R05

* **R04** — l'entrée publique IMPROVE avec le vrai modèle (`agent=None`) retire des identifiants d'EXEMPLE factices de
  `docs/runbook-ops.md`. Exigé : preuve IMPROVE durable, les trois contrats livrés rejoués verts, gain mesuré par le vrai scan,
  couverture non dégradée, revue non bloquante, PR **purement documentaire** et **distincte** du support d'incident, fusion par Phase 5.
  Une PR promue mais non fusionnée n'est pas livrée (étape incomplète) ; chaque refus de garde est conservé dans le détail.
* **R05** — injection **déterministe signalée** (`DeterministicIncidentAgent`, aucun modèle) : retire les identifiants de
  `docs/deploiement.md` et la ligne de mention légale de `docs/export_header.md`. Tout le reste est réel : mesure, revue, politique
  de faible risque (non élargie), checks, santé indépendante (la sonde métier, commande sans opérateur shell), vraie PR de revert avec
  ses checks requis, fusion, synchronisation, arbre restauré, incident `recovered`, acquittement CAS (révision périmée et rejeu refusés),
  reprise libre. Une garde qui refuse la contribution laisse le rollback **non exercé** (étape incomplète) ; un incident actif à l'entrée
  est réconcilié avant toute nouvelle passe (aucune seconde injection) ; un lease de revert non expiré (≥ 3600 s côté produit) laisse l'incident
  actif et R05 incomplète.

## 5. Limites

* Aucune génération réelle n'a eu lieu : tout est prouvé sur le produit, un vrai Git local et des clients de test.
* R04 dépend du comportement du modèle (fichiers touchés, taille ≤ 50 lignes) ; un écart est un refus conservé, jamais un succès.
* La santé s'exécute dans le sandbox du gate (réseau selon la configuration d'A) ; la présence de `timeout`, de `pypdf` et de la pile des
  oracles dans l'image est contrôlée par P08, pas par ce lot.
* Le chemin de revert (`remote_revert`) ne passe pas par `verify_merge_candidate` : il fusionne un revert de la fusion déjà gardée.
