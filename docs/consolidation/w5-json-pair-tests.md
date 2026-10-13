# Paire JSON — tests adverses (lot B37/B38)

Contrat : `briefs/w5-json-pair-contract.md`, arbitrage définitif `briefs/w5-json-pair-cli-ruling.md` et notes `evidence/w5-manager-json-pair-review-notes.md`. Fichiers de B : `tests/test_w5_json_pair_adversarial.py`, `tests/w5_json_pair_support.py`, ce document. **Aucun fichier de A ou de C.**
Ces tests n'ont **pas encore été exécutés contre le lot A** : la collecte échoue sur `from collegue.broker import json_pair` tant que le module n'est pas intégré (état attendu d'un test écrit avant son lot, ce n'est pas un rouge métier). Aucun `skip`/`xfail`.

## Principes

* **API publique seulement** : `MODEL`, `VARIANT_NAMES`, `payloads()`, `run_pair(database=, campaign_id=, upstream=, clock=)`, `read_report(database=, campaign_id=)`.
* **Vérité terrain indépendante** : un faux Google natif (`httpx.MockTransport` derrière le VRAI `GoogleUpstream`) journalise dans un fichier partagé (`O_APPEND`) chaque `countTokens`/`generateContent` reçu, tous processus confondus ; il enregistre aussi, en lecture seule, l'état du registre au moment de la requête (échéance ouverte, tentative `emitting`, jetons réservés).
* **Registre relu par SQLite** sur les tables **existantes** du registre budgétaire (`budget_scopes`, `broker_attempts`, `broker_clocks`…), jamais sur le journal privé de A.
* **Processus réels** (`subprocess`) : crash (`os._exit(137)`), SIGKILL pendant une génération en vol, reprise par un processus neuf avec un fournisseur SAIN (un renvoi serait détectable), concurrence de 4 à 6 processus avec barrière de départ simultané sur un fichier inexistant.
* Aucune clé réelle ni réseau ; clés/URL/identifiants factices hostiles recherchés dans rapport, registre (octets), journaux et sorties des processus.

## Couverture (contrat § « Tests et livraison »)

| Exigence | Tests |
|---|---|
| objet compté = objet émis, corps/empreintes fixes, différence = MIME seul | surface publique, `payloads` (copies profondes), corps normalisés, chemin nominal (4 requêtes exactement) |
| 200 valides ; témoin non JSON ou vide | `test_a_200_witness_…` (4 cas), `test_a_200_native_answer_…` (3 cas) |
| 4xx à chaque position | 4 positions × {400, 403, 404, 429} : arrêt, aucun renvoi, reprise avec fournisseur sain sans requête |
| 500 / timeouts avant et après émission | 7 ambiguïtés × 2 variantes, échec de connexion, `countTokens` en échec (6 comportements × 2) |
| usage absent / contradictoire / borne | `missing`, `inconsistent`, `over_count`, `over_cap` × 2 variantes |
| modèle interdit | `MODEL` modifié (26B, Gemini, chemin hostile, vide) : seule la 31B peut être appelée |
| réservation avant génération ; échéance avant le premier comptage | registre observé par le faux Google à chaque requête |
| interruption, crash, reprise sans renvoi | 4 positions de crash + SIGKILL + réouvertures : aucune requête lors de la reprise, réservation conservée, règlements intacts |
| concurrence interprocessus SQLite | 3 tours × 6 processus (un seul conducteur, 2 émissions), refus 4xx concurrent, deux identités en course, lecteurs pendant un run |
| plafond de deux émissions, unicité de variante | invariants vérifiés après CHAQUE scénario (`assert_wire_invariants`) |
| réentrée | 4 `run_pair` simultanés dans un processus ; fournisseur hostile qui réentre pendant la première génération |
| échéance globale | +899 s (autorisé), +900/+901/+5000 s (aucun comptage ni génération), horloge injectée, échéance jamais déplacée |
| compteurs honnêtes, rapports sans secret | voir ci-dessous ; 5 scénarios hostiles contre rapport/registre/journaux/sorties |

## Verdicts attendus (arbitrage manager)

* témoin 200 invalide/vide à usage connu puis natif 200 objet JSON valide : `status == "observed"`, `variants[0].json_valid == false` ;
* seconde réponse (natif) 200 non JSON/vide : **exactement** `status == "failed"` et `reason == "json_output_invalid"`, jamais `observed`, même usage réglé ;
* refus HTTP explicite (4xx, comptage ou génération) : `status == "failed"` ;
* ambiguïté sur le premier appel, incident interne (exception arbitraire), refus de reprise après crash : `status == "incomplete"` ; ambiguïté sur le second appel : `failed` ou `incomplete` (jamais `observed`) ; usage/borne incohérents et échéance : jamais `observed`.

## Compteurs

`emissions` distingue : **émission réellement mesurée** (nom contenant `emitted`/`sent`/`transport`) → `null` ou **égale** à la vérité terrain du faux Google ; **admission durable** (`admit`) → au moins la vérité terrain et au plus 2 ; **issue de tentative** (réglée/libérée/inconnue) et **limites de configuration** (`max`/`limit`/`cap`) → jamais comparées au nombre de requêtes. Aucune égalité globale sur un nom contenant « generation ». Les nombres d'émissions ne prétendent pas prouver une réception effective par Google ; une fenêtre de crash ambiguë n'est jamais un zéro mesuré.

## Relecture et réconciliation

`read_report` peut être pur **ou** une réconciliation explicitement appelée : les tests exigent seulement de ne rien émettre, de ne pas libérer l'inconnue, de ne pas toucher aux règlements, tentatives et horloges, et que deux relectures successives donnent le même rapport. Une relecture d'un registre absent ne fabrique ni résultat ni dépense.

## Exécuter contre A (passe suivante)

Depuis la racine du worktree B intégrant le SHA de A figé par le manager : `COLLEGUE_HOME` et `STATE_DATABASE_URL` neufs, `TMPDIR` ancêtre de `--basetemp` sous `/tmp`, `PYTHONDONTWRITEBYTECODE=1`, `python -m pytest -p no:cacheprovider tests/test_w5_json_pair_adversarial.py` (≈ 30 s). Tout échec est à arbitrer (défaut de A ou attente trop stricte de B), jamais masqué.

## Preuves de satisfaisabilité hors dépôt (≠ validation de A)

Une référence **de scratch** (`evidence/w5-b38-scratch/`) et une implémentation **naïve** (service nu enchaîné deux fois) ont servi à montrer que les tests sont satisfaisables et discriminants ; leurs résultats et les limites de cette démarche sont dans `reports/w5-b38-json-pair.md`. Ce n'est pas du code livré et cela ne dit rien du code de A.
