# Diagnostic des erreurs HTTP Google — preuves adverses hors ligne (lot B33)

Contrat fermé : `briefs/w5-google-diagnostics-common.md` (base `ed54868`). Ce document décrit les preuves de B ; le code produit (diagnostic, `UpstreamHTTPError.diagnostic`,
raccords `service.py`) appartient au lot A (`docs/consolidation/w5-google-diagnostics.md`). Aucun appel Google (`countTokens` compris), aucune clé réelle, aucun secret, aucun accès au registre
historique : tout se passe sous le répertoire temporaire du test.

## Montage

* **Vrai `GoogleUpstream`** (hôte, chemins, en-tête de clé, plafond de réponse réels) derrière `httpx.MockTransport` : seule la couche réseau est remplacée.
* **Vrai `BrokerService`** et **vrai registre SQLite neuf** par test ; qualification **publique** (`collegue.broker.qualify_models`) ; **reprise par une NOUVELLE instance** du runtime/service
  sur le même registre, puis relecture par un **nouveau gestionnaire** sur le même fichier.
* Scénario synthétique : `countTokens` rend 10, la réservation est de 10 + 256 = **266**, `generateContent` du 31B reçoit un HTTP 500 dont le corps annonce `INTERNAL`.

## Contenus hostiles

Corps JSON et en-têtes portent : clé FAUSSE, **clé configurée renvoyée en écho**, fragment de prompt, URL (avec jeton), sauts de ligne (`\r\n`, `\n`) et fausse requête HTTP, détails **imbriqués** (`details[*].metadata.trace.nested[*]`),
et trois identifiants opaques alphanumériques qui passent une expression régulière. Chaque valeur est cherchée **brute, échappée JSON, en base64 (standard et URL)** ; pour les identifiants aussi sous forme de **hachage**
(SHA-256, SHA-1, MD5 complets et préfixes de 16 caractères) — le diagnostic ne contient ni la valeur ni un hachage d'un identifiant amont (`request_id_present` est un booléen).

Surfaces inspectées : message, `repr`, `args`, attributs (`vars`), trace formatée et **chaîne `__cause__` / `__context__`** de l'exception amont et de l'exception du courtier ; diagnostic ; rapport de qualification (JSON et `repr`) ; tentatives (`AttemptRecord`, vivantes et relues) ;
instantané du registre ; **octets de tous les fichiers SQLite** ; journaux (`caplog` au niveau DEBUG).

## Deux familles, comptées séparément

| Famille | Nom | Rôle | Sur `ed54868` |
|---|---|---|---|
| Nouveau diagnostic | `test_diagnostic_*` | contrat fermé du diagnostic, persistance bornée dans `error_detail` et le rapport, absence de recopie dans l'exception et sa chaîne | **rouges par assertion** (83 cas) |
| Témoins indépendants | `test_witness_*` | comptabilité `unknown`/libération selon le seul statut HTTP, aucun retry ni repli, plafond de réponse inchangé, 200 valide, corps adverses toujours vus comme le statut HTTP observé, absence de recopie dans les surfaces persistées | **verts** (36 cas), à garder verts |

Les absences de contrat (`diagnostic` manquant) échouent par **assertion explicite** (`diagnostic_of`), jamais par erreur d'attribut ou de collecte.

## Ce qui est exigé

* `diagnostic` : exactement `operation`, `http_status`, `body_kind`, `rpc_status`, `request_id_present` ; `operation ∈ {countTokens, generateContent}` ; `http_status` = statut **observé** (jamais la revendication du corps) ; `body_kind ∈ {json, html, other, empty, invalid_json}` ;
  `rpc_status` = l'un des 17 statuts reconnus **copiés exactement**, sinon `absent` (aucune valeur exploitable) ou `other` ; un texte proche (`internal`, `INTERNAL `, valeur injectée) n'est **jamais** arrondi vers un statut reconnu ni copié.
* Corps illisibles ou hostiles (JSON invalide, vide, HTML, texte, tableau/chaîne/nombre/`null` de premier niveau, doublons, profondeur 20 000, objet de 20 000 clés, chaîne de 1,4 Mo, `NaN`/`Infinity`, BOM, octets non UTF-8, caractères de contrôle) :
  l'appelant reçoit toujours `UpstreamHTTPError` avec le statut observé (jamais `RecursionError`, `ValueError`…), diagnostic fermé et < 300 caractères sérialisé.
* Doublons de clés : le parseur strict du produit les refuse ; le statut RPC ne doit être ni l'un ni l'autre (`absent`/`other`).
* Service : le diagnostic est **persisté** (`AttemptRecord.error_detail`, ≤ 330 caractères au total, contenant opération, statut HTTP et statut RPC), **présent dans le rapport** de qualification (détail de la capacité et `reason`), et relu par un nouveau gestionnaire ;
  après rejeu, nouvelle instance ou repli 26B tenté : **aucune requête supplémentaire**, usage `unknown` = 266, scope bloqué, JSON/outils/26B « non exécuté ».
* Comptabilité indépendante du diagnostic : HTTP 500 annonçant `INVALID_ARGUMENT` reste `unknown` ; HTTP 400 annonçant `INTERNAL` reste un rejet démontré (libération) ; 429, 403 idem ; `countTokens` refusé libère tout et ne génère jamais.

## Limites

* Les formats exacts des chaînes ajoutées aux messages sont ceux du lot A : les tests exigent la **présence** de l'opération, du statut HTTP et du statut RPC, pas une mise en forme.
* `request_id_present` n'est vérifié que comme booléen (les noms d'en-têtes reconnus relèvent d'A) ; la non-recopie de la valeur et de ses hachages est, elle, exigée.
* Les doublons et la profondeur sont testés par comportement observable (statut conservé, diagnostic fermé, aucune recopie), sans imposer l'algorithme de A.
* Aucune preuve contre le vrai Google : la cause interne du HTTP 500 historique reste indéterminée.

## Résultat sur le lot A accepté (B34)

Fusion locale `--no-ff` du SHA A accepté `65a2fe92850df9b50818141e9c79a4c9f404f04a` dans la branche B (commit de fusion `838c7dd`, aucun changement fonctionnel ; les fichiers importés de A — `collegue/broker/diagnostics.py`,
`collegue/broker/upstream.py`, `collegue/broker/service.py`, `tests/test_w5_broker_diagnostics.py`, `docs/consolidation/w5-google-diagnostics.md` — gardent A pour auteur et n'ont pas été édités). Les **mêmes 119 tests**, fichier inchangé
(SHA256 `f66999e67d584cc25090b981b54f46945cad531eb3770a4949f97cfa46a857cc`, avant et après la fusion), sont rejoués depuis ce worktree :

| Base | Cas | Résultat |
|---|---|---|
| `ed54868` (sans A) | 119 | 83 rouges (assertions) / 36 témoins verts |
| `ed54868` + A `65a2fe9` | 119 | **119 verts**, 0 échec, 0 erreur, 0 saut |

Aucun test n'a été affaibli ni modifié ; les deux familles (nouveau diagnostic, témoins comptables) sont vertes.
