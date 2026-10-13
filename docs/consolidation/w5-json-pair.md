# Paire JSON W5 — lanceur borné durablement (lot A)

Une comparaison ciblée, autorisée une seule fois : le **31B seul** (`gemma-4-31b-it`) reçoit le même prompt JSON sans
`responseMimeType` (`prompt_only`) puis avec (`native_json`). Rien d'autre ne change entre les deux corps normalisés
(`2edf3771…ecc2`, `98d8c344…a300`). Le résultat n'est **jamais** une qualification métier de Gemma 4 hébergé
(`business_validation: "not_executed"`).

Code : `collegue/broker/json_pair.py` (cœur), `scripts/w5_json_pair.py` (CLI), `tests/test_w5_json_pair.py`.
Contrat : `briefs/w5-json-pair-contract.md` et arbitrage `briefs/w5-json-pair-cli-ruling.md` (racine d'orchestration).

## Pourquoi un lanceur

Le service du courtier borne les jetons, les dollars et l'échéance, **pas le nombre d'émissions**. Deux lacunes, établies
par C86 et reproduites par les tests (`test_c86_gap_*`) :

1. **Troisième appel** : `sampling_completion` admet autant de générations qu'il y a d'identifiants distincts.
2. **Renvoi après 400** : une tentative `released` est *rouverte* quand le même `request_id` est renvoyé.

Le service n'est pas modifié. Le lanceur les ferme par un journal SQLite écrit **avant** chaque étape irréversible
(`test_the_launcher_*`, `test_the_journal_*`).

## Journal et admission

Même fichier SQLite que le registre budgétaire, sans migration applicative : tables `w5_json_pair_run` et
`w5_json_pair_variant`, créées par le lanceur. Le journal ne compte jamais un jeton ; **le registre reste l'autorité d'usage**.

| Étape | Garde durable (une transaction `BEGIN IMMEDIATE`) |
|---|---|
| Conducteur | l'insertion de la ligne `run` ; le premier gagne, toute autre entrée réconcilie, rapporte, refuse |
| `claim_variant` | variante `pending`, run ouvert sans arrêt, échéance non atteinte, rien d'autre en vol, précédente `observed`/`parsing_failed` |
| `note_count` | variante `claimed`, un seul `countTokens` par variante |
| `admit_send` | `claimed` → `emitting` **commité avant l'appel au transport** ; au plus 2 admissions, une par variante, aucun échec local noté |

Le fournisseur injecté est enveloppé (`_Guard`) : chaque appel vérifie que l'objet compté est l'objet émis (empreinte
normalisée et corps), passe par le journal, et est borné par `asyncio.wait_for` au temps restant de l'échéance de 900 s.
Les exceptions libres du fournisseur ne traversent pas : seul un nom de classe filtré survit.

Identifiants fixes : `jp-<campagne>-prompt-only`, `jp-<campagne>-native-json` ; scope `json-pair:<campagne>` (2 USD,
250 000 jetons, strict). L'horloge de 900 s est ouverte dans le registre **avant le premier `countTokens`** et jamais déplacée.

## Relecture : le journal est validé, jamais cru

`read_report`, `run_pair` (reprise), `reconcile_pair` et chaque admission (`claim_variant`, `note_count`, `admit_send`) valident la
structure durable avant de s'y fier : `quick_check`, jeux de colonnes exacts, une seule ligne `run` (schéma, modèle, bornes,
empreintes, `scope_key` dérivé de la campagne), exactement deux variantes aux noms, positions, identifiants de requête et
empreintes attendus, états et valeurs cohérents (`observed` ⇒ `json_valid=1` et émission admise, seconde variante jamais avancée
sans la première, une seule en vol, au plus deux admissions, échéance posée dès qu'une variante a démarré). Toute dérive donne
`incomplete`/`journal_corrupt` (variantes `unknown`, aucune émission comptée, budget relu dans le registre) sans exception brute, sans
réinitialisation ni réparation du fichier. Une observation du journal n'est de plus jamais crue sans tentative `settled` dans le registre.

## Reprise, crash, concurrence

Toute entrée sur un registre déjà démarré **ne ré-émet jamais** : `reconcile_pair` répare le registre (`BrokerService.repair`,
sans fournisseur : un `emitting` d'un conducteur disparu devient *inconnu et bloquant*, un `prepared` est libéré), fixe l'état
des variantes restées en vol, ferme le journal et rapporte. Un conducteur vivant (même hôte, pid et date de démarrage
vérifiés ; autre hôte supposé vivant) n'est pas touché : le rapport dit `in_progress`.

## Verdicts

| Situation | `status` | `reason` | code |
|---|---|---|---|
| 2 variantes réglées, seconde = objet JSON valide, rien d'inconnu | `observed` | `ok` | 0 |
| 200 non JSON / vide sur la **première** (usage réglé) | continue ; `variants[0].json_valid=false` | — | — |
| 200 non JSON / vide sur la **seconde** | `failed` | `json_output_invalid` | 1 |
| refus HTTP explicite (400 401 403 404 405 413 415 422 429) | `failed` | `upstream_rejected` | 1 |
| erreur HTTP de comptage | `failed` | `count_failed` | 1 |
| borne ou usage démenti | `failed` | `bound_violation` | 1 |
| 5xx, timeout, réponse perdue, usage absent après émission | `incomplete` | `upstream_ambiguous` | 3 |
| comptage indisponible sans statut HTTP (transport, échéance) | `incomplete` | `count_unavailable` | 3 |
| refus local / rien parti | `incomplete` | `local_refusal` / `not_sent` | 3 |
| arrêt, annulation, SIGTERM, conducteur disparu | `incomplete` | `interrupted` / `in_progress` | 3 |
| échéance globale avant une variante | `incomplete` | `deadline_reached` | 3 |
| incident interne du lanceur | `incomplete` | `internal_error` | 3 |
| base illisible / étrangère / autre campagne / empreintes dérivées | `incomplete` | `registry_unreadable` / `foreign_registry` / `campaign_mismatch` / `payload_drift` | 3 |
| journal durable altéré (ligne ou colonne en moins ou en trop, schéma, modèle, bornes, empreintes, identifiants, états contradictoires) | `incomplete` | `journal_corrupt` | 3 |

`json_valid` = le contenu est un **objet** JSON strict (`NaN`, tableau, balises ```json ou vide : faux). `finish_reason`
est une valeur fermée (`stop`, `length`, `tool_calls`, `content_filter`, `other`), jamais un texte du fournisseur.

`emissions` précise ce qui est mesuré : `admitted` = admissions durables **avant** l'appel au transport (borne haute d'envoi,
pas une preuve de réception par Google) ; `settled`/`released`/`unknown` viennent du registre ; `null` = non mesuré (jamais un
zéro inventé). Une fenêtre de crash entre l'admission et l'appel reste comptée.

## Convention d'initialisation du registre

`run_pair` initialise un fichier **neuf** lui-même (`ProjectStateManager.from_url(create=True)`), donc l'étape de migration
est facultative pour la paire. Un fichier ne contenant que le schéma standard (`python -m collegue.migrations upgrade`,
tables vides hors `alembic_version`) est accepté (`test_a_virgin_standard_migration_database_is_accepted`). Un fichier
portant une autre campagne, une dépense, une table non vide ou un démarrage antérieur n'est **jamais** réutilisé. La
protection contre un dispatch neuf réutilisant l'identité repose sur la référence GitHub persistante, pas sur ce SQLite.

## CLI (référence : `evidence/w5-c88-cli-needs.md`, arbitrée)

`--root` et `--campaign-id` (`[a-z0-9][a-z0-9-]{2,39}`) sont communs ; fichiers sous `--root` :
`proof/preflight.json`, `proof/claim.json`, `home/<id>.sqlite3`, `report/json-pair.{json,txt}`, `report/provenance.json`.
Le CLI crée `proof/ home/ report/` (0700) s'ils manquent et ne supprime rien.

| Commande | Secret admis | Rôle |
|---|---|---|
| `preflight --expected-sha --confirm` | aucun | contexte réel (`workflow_dispatch`, `refs/heads/main`, SHA = attendu, `run_attempt=1`, dépôt, nightly **explicitement** `INTEGRATION_E2E_ENABLED=false`), phrase de confirmation, empreintes, registre non démarré ; écrit une preuve scellée |
| `claim` | `GITHUB_TOKEN` de fixture seul | relit la preuve et le contexte ; crée **exclusivement** `collegue-business-claims/<id>` (`claim_campaign_identity`) ; écrit une preuve liée à identité, référence, `run_id`, `run_attempt`, SHA, dépôt et empreinte du préflight |
| `run` | `LLM_API_KEY` seule | relit les deux preuves, refuse toute autre clé/jeton, appelle `run_pair` avec `GoogleUpstream` ; SIGTERM : plus aucune émission, journal lisible ; registre déjà démarré → réconcilie, rapporte, `resume_refused` |
| `finalize` | aucun (refuse `LLM_API_KEY`) | `reconcile_pair` sans fournisseur, écrit les trois rapports même si `run` n'a jamais démarré |

**Environnement.** Chaque étape n'admet que son propre secret (`claim` : `GITHUB_TOKEN`, `run` : `LLM_API_KEY`, `preflight`/`finalize` :
aucun). Est refusé, quelle que soit la longueur de sa valeur, tout nom non vide dont un segment est `API_KEY`, `SECRET`, `PASSWORD`,
`PRIVATE_KEY`, `TOKEN` ou `PAT` (clés de fournisseur, clés par rôle, `GH_TOKEN` et tous les alias du jeton de fixture) ; les métadonnées
du runner (`GITHUB_RUN_ID`, `RUNNER_*`, `W5_JSON_PAIR_ROOT/CONFIRM/CAMPAIGN_ID/EXPECTED_SHA`…) et les jetons internes d'Actions
énumérés ne le sont pas. Le nightly n'est inactif que si la valeur est exactement `false` : absent, vide ou ambigu →
`nightly_unestablished` (`1/true/yes/on` → `nightly_active`) ; un workflow doit donc fournir `false` explicitement.
`claim.json` lie la référence exacte `collegue-business-claims/<id>`, la vraie fixture, le contexte courant du run et l'empreinte du
préflight utilisé ; `run` refuse une preuve scellée mais mal dirigée (`claim_proof_target/_preflight/_context`).

Aucun argument ne fabrique le contexte GitHub, ne choisit un modèle, une URL, un budget ou un mode sans revendication.
La sortie standard est une ligne fermée ; un incident inattendu n'imprime que le nom de classe.

## Limites

- SQLite de campagne seulement (aucune prétention PostgreSQL) ; aucun test ne touche un fournisseur ni GitHub réels.
- Le service reste inchangé : les gardes valent pour ce lanceur, pas pour un autre appelant du même registre.
- La nature exacte d'un 500 n'est pas éclaircie par la paire ; son succès ne qualifie rien au-delà de ces deux observations.
- Le code HTTP exact n'est pas dans le dict machine (clés fixées par le contrat) ; il est dans le journal (`http_status`).
