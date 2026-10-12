# W5 — diagnostic fermé des erreurs HTTP Google (A37)

Lot de télémétrie **fermée** autorisé après le HTTP 500 de la campagne `w5-gemma-20261012-9e7c4d76` (cause interne restée indéterminée, voir A35/A36).
Il ne change NI le corps Google, NI l'endpoint, NI les modèles, NI les allocations, réservations, admissions, états `unknown`, retries, repli ou verdicts. Invariant conservé :
corps compté = corps émis. **Le diagnostic ne décide jamais de la comptabilité** : seul le statut HTTP (`PROVEN_REJECTED_STATUS`, `service.py`) tranche.

## Ce qui est conservé (valeurs fermées, rien de recopié)
`UpstreamHTTPError.diagnostic` — `{}` pour un double qui n'en fournit pas ; pour une réponse HTTP non-200 observée par le vrai `GoogleUpstream` :

| Champ | Valeurs |
|---|---|
| `operation` | `countTokens` ou `generateContent` (l'appel réellement fait) |
| `http_status` | entier du statut effectivement observé (100–599) |
| `body_kind` | `json`, `html`, `other`, `empty`, `invalid_json` |
| `rpc_status` | un des 17 statuts Google RPC (`OK … UNAUTHENTICATED`, sensible à la casse) ou `absent` (pas de `error.status`) ou `other` (présent mais inconnu / mal typé) |
| `request_id_present` | booléen : un en-tête d'identifiant reconnu (`x-goog-request-id`, `x-request-id`, `request-id`, `x-cloud-trace-context`) est non vide — **jamais sa valeur, jamais un hash** |

Jamais conservés : corps brut, message d'erreur, `details`, URL (donc pas de `Location` d'une redirection), valeur d'en-tête, identifiant amont, clé, prompt. Aucune expression régulière n'est présentée
comme garantie : la sécurité vient de la fermeture des valeurs (liste écrite dans `collegue/broker/diagnostics.py`).

**Ce que les catégories ne disent pas.** Elles permettent de CLASSER une erreur, pas d'en attribuer la cause ni l'origine : un corps JSON ou HTML ne prouve pas qui l'a émis, et un statut RPC `INTERNAL` ne désigne pas une cause interne.

## Où cela remonte (chemins existants, aucune migration, aucun registre ni journal nouveau)
* `UpstreamHTTPError` (typé) → `service.py` : `diagnostic_suffix(exc)` = ` [diag http=500 op=generateContent rpc=INTERNAL body=json request_id=absent]` (ASCII, < 100 caractères) ajouté
  **après** le texte historique aux messages contrôlés (`BrokerUpstreamAmbiguous`, `BrokerUpstreamRejected`, `BrokerUnsupported` pour `countTokens`) et à `broker_attempts.error_detail` (donc `blocked_reason`,
  événement `unknown`, `CapabilityResult.detail`, `QualificationReport.reason` par les chemins déjà en place). Un double sans diagnostic donne exactement les anciens messages.
* Pas de diagnostic pour les échecs de transport (`UpstreamTransportError`) ni les réponses 200 illisibles (`UpstreamBadBody`) : hors périmètre HTTP non-200.

## Canaux de représentation de l'exception inspectés
`str`, `repr`, `args` (`("HTTP 500",)`), `__dict__` (seulement `status`, `diagnostic`), `pickle` (`__reduce__` explicite : statut + diagnostic, jamais le détail), `traceback` normal et **avec variables locales**
(`capture_locals`, ce que font Sentry ou `--showlocals`). L'ancien `detail` (300 caractères du corps amont) est supprimé ; l'argument est encore accepté et ignoré (doubles existants). Dans `_post`, le
corps, le dernier morceau (variable de boucle `chunk`), les en-têtes de la requête (clé), le client et la réponse sont remis à `None` avant toute levée : un test échoue si un marqueur de réponse,
d'URL de redirection, d'identifiant ou la clé apparaît dans l'un de ces canaux. Le prompt reste visible dans les cadres APPELANTS (`request`) : c'est la requête, pas la réponse, hors de ce lot.
Un diagnostic fourni par un double est normalisé par `sanitize_diagnostic` (valeurs hors liste → `other`, structure non fiable → `{}`).

## Fail-safe
Corps JSON invalide / tronqué, clés dupliquées, `NaN`, types inattendus, profondeur excessive (`RecursionError` comprise), octets non UTF-8, en-têtes qui lèvent : `http_diagnostic` ne lève jamais et ne masque jamais le statut HTTP ; une ambiguïté
(500) n'est jamais transformée en rejet certain, un rejet établi (400/429…) reste un rejet. Plafond de réponse (`MAX_RESPONSE_BYTES`) inchangé (au-delà : `UpstreamBadBody` comme avant). Redirection : non suivie, statut 3xx diagnostiqué.
