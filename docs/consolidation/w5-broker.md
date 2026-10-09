# Vague 5 — A : le courtier budgétaire (budget EFFECTIF, clé Google hors du conteneur)

État : implémenté et testé localement (aucun appel fournisseur réel, aucune clé). Contrat : `briefs/w5-common.md`.
Interface publique concrète : `reports/w5-a-interfaces.md`. Rapport : `reports/w5-a-broker.md`.

## Pourquoi

Jusqu'à la W4, un worker OpenHands recevait la clé fournisseur dans son conteneur et ne pouvait être borné que par une garde
in-runner (`budget_enforcement="in-runner"`) : avec une clé facturable la W2 REFUSE l'allocation sous plafond strict
(`unbounded_transport`) — une commande du workspace pouvait contacter le fournisseur hors de tout contrôle. Le courtier supprime
la cause : la clé n'existe que dans un service de confiance, le conteneur n'a aucun réseau, et chaque génération est réservée,
marquée et réglée par ce service.

## Architecture

```
 hôte (processus de confiance)                                   conteneur du codeur (--network none)
 ┌───────────────────────────────────────────┐                  ┌────────────────────────────────────┐
 │ BrokerService ── GoogleUpstream ──► Google│                  │ SDK OpenHands (LiteLLM openai/…)   │
 │   │ (clé Google : ICI SEULEMENT)          │                  │   │ HTTP loopback                  │
 │   ▼                                       │   socket Unix    │   ▼                                │
 │ BrokerSocketServer(session) ◄─────────────┼── (dir 0700, ───►│ oh_broker_relay 127.0.0.1:<port>   │
 │   registre W2 (scope enfant) + 0013       │   broker.sock,   │   (recopie d'octets, rien d'autre) │
 └───────────────────────────────────────────┘   monté ro)      └────────────────────────────────────┘
```

* **Un socket par allocation** : le répertoire hôte ne contient QUE `broker.sock` (0600, répertoire 0700, hors workspace) ; il est
  monté en lecture seule sur `/run/collegue-broker` via `DockerSandbox.with_broker` (garde W1 `git_control_exposure`
  inchangée, revalidée à chaque lancement). Le socket ne donne accès ni au registre, ni aux autres sessions, ni au contrôle Git,
  ni à un fichier hôte : le serveur n'implémente qu'`POST /v1/chat/completions`.
* **Droits décidés côté serveur** : la session (rôle, modèles, plafond de sortie, échéance, scope enfant) est enregistrée à
  l'ouverture ; le jeton (`cbk_…`, hashé SHA-256 en base) n'ouvre que CETTE session. Ni rôle, ni scope, ni URL, ni modèle, ni
  authentification soumis par le client ne les élargissent (champ inconnu ⇒ 400 nommant le champ). Le jeton visible dans son
  sandbox n'est PAS une clé fournisseur et n'a aucune autorité sur d'autres rôles.
* **Identités** : seules `gemma-4-31b-it` et `gemma-4-26b-a4b-it` existent ; le repli 26B est réservé au rôle `coder`. Les
  préfixes `models/`, `gemini/` et `openai/` (format Chat Completions du relais) sont acceptés : la destination sémantique reste
  Google (`resolve_route` : `gemini`), jamais reclassée OpenAI.
* **Traduction native** : Chat Completions → `generateContentRequest` (`contents`, `systemInstruction`,
  `tools.functionDeclarations` avec `parametersJsonSchema`, `toolConfig`, `generationConfig`). `countTokens` reçoit
  `{"generateContentRequest": <objet>}` et `generateContent` le MÊME objet (inclut `model: models/<id>`, instructions, outils,
  `candidateCount=1`, `maxOutputTokens` effectif). Streaming, médias, URL, `n≠1`, champs inconnus, kwargs libres : refusés.
  **À confirmer en campagne** (aucun appel réel ici) : `parametersJsonSchema` côté Gemma 4 et le comportement de `countTokens`
  sur les deux identités ; l'absence de `countTokens`/de borne réelle ⇒ refus explicite, jamais d'estimation de remplacement.
* **JSON strict et bornes** : clés dupliquées, `NaN`/`Infinity`, profondeur > 32, corps > 256 Kio refusés. `max_tokens` et
  `max_completion_tokens` contradictoires refusés ; une limite plus haute que le plafond du serveur est REFUSÉE (jamais écrêtée) ;
  une limite absente prend le défaut du serveur.
* **Usage** : `prompt + toolUsePrompt + candidats + raisonnement`, vérifié égal à `totalTokenCount` (sinon « usage
  incohérent »). Aucune double addition candidats/raisonnement. Usage absent/invalide/incohérent, sortie > limite, entrée >
  `countTokens`, consommation > réservation : la consommation MESURÉE est engagée telle quelle (ou la réserve conservée si elle
  est inconnue), le scope enfant ET le parent sont BLOQUÉS et l'erreur est signalée (`bound_violation`, HTTP 502).
* **Client Google** (`GoogleUpstream`) : hôte et chemins fixes, clé en en-tête `x-goog-api-key` (jamais dans l'URL), `trust_env=False`
  (ni proxy ni netrc), aucune redirection suivie, réponse plafonnée à 4 Mio. Aucun en-tête, paramètre ni corps du client n'est relayé.

## Flux d'une génération (toutes les entrées : worker par socket, producteurs en processus)

`valider → échéances → admission (session ouverte, in_flight+1) → tentative prepared → countTokens → réservation CAS (prompt +
sortie max) → échéances → emitting (écrit AVANT l'envoi) → generateContent → règlement`.

| Issue du fournisseur | Tentative | Registre | Effet |
|---|---|---|---|
| 200 + usage cohérent dans les bornes | `settled` | commit | succès |
| 400/401/403/404/405/413/415/422/429 (rejet démontré), échec de connexion avant envoi | `released` | release | libérée ; 429 ⇒ HTTP 429 (le SDK peut réessayer = NOUVELLE génération réservée) |
| 5xx, 408, 409, lecture interrompue, corps illisible, annulation, arrêt | `unknown` | mark_unknown | réserve conservée ; scope enfant et réservation parent bloqués ; aucun retry |
| usage absent / incohérent | `unknown` | mark_unknown | idem, `bound_violation` |
| usage mesuré mais borne démentie | `settled` | commit (réel) + blocage `bound_violation` | engagé tel quel, projet bloqué |

L'état durable est écrit PUIS appliqué au registre (clés d'événement déterministes) : `BrokerService.repair` rejoue l'opération
manquante après un crash. Une tentative encore `prepared` n'a PROUVABLEMENT rien émis (libérée) ; `emitting` ⇒ inconnu, jamais
rejoué ni libéré sans preuve. Un `request_id` (en-tête `Idempotency-Key` au socket) rejoué rend le résultat stocké sans seconde
génération ; rejoué après `released` il redémarre (absence d'émission établie) ; rejoué après `emitting`/`unknown` il est refusé.

## État durable (migration additive 0013)

`broker_sessions` (jeton hashé, rôle, scope enfant, réservation parent, modèles, plafond de sortie, `in_flight`, échéance),
`broker_attempts` (machine d'états ci-dessus, `UNIQUE(scope_key, request_id)`), `broker_clocks` (échéance globale). Aucun compteur
de dépense : les montants restent dans `budget_scopes`/`budget_reservations`.

* **Session ⇒ scope enfant** (`kind='child'`, strict, plafonds = montants de la réservation `worker` parent, qui doit exister, être
  `reserved` et n'avoir aucune autre session). Le parent n'est PAS recompté pendant la session : le projet voit sa réservation
  (borne haute) ; à la fermeture le courtier engage `consommé enfant` dans le parent et libère le reliquat (une seule fois,
  `event_key=consolidate:<session>`), ou marque le parent `unknown` si l'enfant l'est.
* **Producteurs hors worker** (planner, QA, reviewer, boucle agentique, handler FastMCP) : mêmes tentatives et mêmes règles dans
  le scope GLOBAL du projet ; un contexte sans registre lié est refusé (`no_budget_context`), jamais une exemption.
* **Fermeture concurrente** : `open → closing` est un CAS ; plus aucune génération n'est admise, les appels en vol se terminent
  (libération ou règlement) AVANT la consolidation ; ceux qui dépassent l'attente sont déclarés inconnus (règlement tardif refusé).
* **Échéance globale** (`BROKER_GLOBAL_DEADLINE_SECONDS`, ex. 900) : ouverte UNE fois, au premier accès RÉEL au fournisseur du scope
  racine (canaris compris), persistée, jamais remise à zéro ; aucune nouvelle génération après ; les réserves en vol sont
  conservées ; fermeture / consolidation / nettoyage restent possibles.

## Capacité (jamais une chaîne)

`OHSdkAgent(..., broker=runtime).budget_enforcement == "broker"` ne suffit pas : `allocate_worker` appelle
`agent.broker_transport_proof()` qui construit l'argv `docker run` RÉEL du sandbox raccordé et vérifie : réseau `none`, un seul
montage du courtier en lecture seule, aucun autre montage hôte (workspace, cache pip), aucune variable de clé/proxy, aucune
valeur égale à la clé fournisseur, aucun `--env-file`/privilège/espace de noms hôte, `env_passthrough` vide. Un faux agent qui
déclare `"broker"`, un sandbox avec réseau ou un double sans `with_broker` est REFUSÉ (`unbounded_transport`). Avec la preuve, les
plafonds stricts 2 USD / 250 000 tokens sont acceptés pour la clé facturable (le courtier réserve et règle chaque appel).
`preflight_broker_transport` / `build_preflight_agent` (`collegue/broker/preflight.py`) établissent la même capacité sans clé ni
inférence.

## Inventaire des sous-processus et montages

* `collegue/broker/**` et `oh_broker_relay.py` : AUCUN sous-processus (test statique AST).
* `collegue/sandbox/executor.py` : exactement trois `subprocess.run` — `docker run` (worker), `docker kill`, `docker version`.
* Montages : workspace (rw), cache pip (opt-in, absent en mode courtier), socket du courtier (ro). Tous via `git_control_exposure`.

## Configuration

`LLM_TRANSPORT=direct|budget_broker` (défaut `direct`, comportement historique inchangé hors W5), `LLM_PROVIDER=gemini`,
`LLM_MODEL=gemma-4-31b-it` (tous rôles), `CODER_FALLBACK_MODELS=gemma-4-26b-a4b-it` (repli du codeur seulement),
`LLM_API_KEY` (lue UNIQUEMENT par le service de confiance), `BROKER_GLOBAL_DEADLINE_SECONDS=900`, `BROKER_MAX_OUTPUT_TOKENS`
(défaut 8192), `BROKER_UPSTREAM_TIMEOUT`, `BROKER_RUN_DIR` (racine COURTE des sockets : AF_UNIX ≤ 108 octets). Refusés au démarrage
en mode courtier : autre fournisseur, autre modèle, repli hors 26B, repli demandé par un autre rôle, abonnement, authentification
autre que `api_key`, tout `LLM_BASE_URL*`.

## Limites assumées

* Aucun appel réel n'a été fait : le comportement de `countTokens`/`generateContent` pour les deux Gemma, `parametersJsonSchema`,
  les en-têtes exacts du SDK (champs refusés = erreur 400 explicite nommant le champ) et le jeu de paramètres envoyés par LiteLLM
  sont à établir par la campagne/canaris. Le SDK OpenHands réel et l'image se vérifient en CI (C).
* Le relais et le socket ne sont pas un canal d'exfiltration étanche : le worker peut appeler le courtier directement, mais
  uniquement dans son allocation ; le contenu des prompts qu'il envoie à Google n'est pas filtré.
* Succès du CLI sans usage : en mode courtier la consommation vient du courtier seul ; zéro tentative = zéro émission prouvée ;
  les journaux de l'agent (`[collegue-usage]`) ne font jamais autorité.
