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
manquante après un crash. L'identifiant de la réservation est DÉTERMINISTE (`broker:<attempt_id>`, puis `…#<runs>` après une
réouverture) et ÉCRIT dans la tentative AVANT `ledger.reserve` : un crash entre la réservation et la suite laisse une tentative qui
désigne sa réserve, jamais une réserve orpheline. Une tentative encore `prepared` n'a PROUVABLEMENT rien émis (libérée) ; `emitting` ⇒ inconnu, jamais
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


## Revue indépendante (A26) — ce qui a changé et les contrats qui en découlent

### Admission transactionnelle à l'émission
`BrokerStore.admit_emission` fait passer `prepared → emitting` dans UNE transaction qui revérifie, sous verrou de ligne (`FOR UPDATE` sur
PostgreSQL ; `BEGIN IMMEDIATE` explicite sur SQLite, dont le mode historique n'ouvre pas de transaction pour un `SELECT`) : aucun scope
(enfant, parent / racine) n'est bloqué, la session est `open` sans inconnue et dans son échéance, la réservation parent est encore
`reserved`, la réservation de la tentative existe. Un blocage / une fermeture validés avant cette transaction l'emportent (libération,
rien n'est émis) ; ceux qui la suivent trouvent une émission déjà marquée (en vol, donc légitime). Le contrôle initial avant `countTokens`
n'est qu'une optimisation.

### Allocation nulle
Une réservation `worker` sans tokens (`reserved_tokens = 0`) n'ouvre PAS de session (`zero_allocation`) et `allocate_worker` refuse en mode
courtier un scope sans plafond de tokens. Le scope enfant reprend les montants EXACTS du parent (0 reste 0, jamais « sans plafond ») ;
la dimension USD est indépendante.

### Propriété des tentatives (contrat de reprise)
Chaque instance de service a un `owner_id` (table `broker_owners` : hôte, pid, date de démarrage du processus, battement de cœur).
Une tentative / session d'un propriétaire VIVANT — même hôte : processus existant avec la même date de démarrage ; autre hôte : battement
de cœur de moins d'une heure — n'est JAMAIS réparée par un autre processus. Celles d'un propriétaire disparu, terminé (`shutdown()`) ou
inconnu (`NULL`) le sont. `recover_all()` (à appeler au démarrage, avant tout appel) répare les tentatives de TOUS les producteurs
(workers, planner, QA, reviewer…) : `prepared` ⇒ libérée (rien n'a été émis, même si la tentative n'a pas encore écrit son identifiant),
`emitting` ⇒ inconnue, scope bloqué, jamais rejouée ; il ferme et consolide les sessions orphelines. `include_own=True` (défaut)
revendique aussi les restes de CETTE instance et est refusé (`recovery_while_busy`) si elle a des appels en vol. Toute génération
répare d'abord les restes abandonnés de ses scopes : leurs inconnues bloquent avant la nouvelle émission.

### Aucune réservation enfant non consolidée
La fermeture balaie les réserves `broker:*` du scope enfant : celle d'une tentative réglée / libérée dont le registre est en retard est
rejouée ; celle SANS état durable (aucune tentative) est marquée INCONNUE (conservée, bloquante) — jamais libérée ; une réserve enfant
encore ouverte à la consolidation fait passer le parent à `unknown`. `_block_parent` ne masque plus une erreur du registre : seul un parent
réellement `unknown` est acceptable, un parent déjà réglé est bloqué au niveau du scope, toute autre erreur se propage.

### Qualification des deux modèles (canaris)
`BrokerService.qualify_models(scope_key)` (et `collegue.broker.qualify_models(settings, ledger, scope_key)`) exerce `gemma-4-31b-it` (rôle
`default`) et `gemma-4-26b-a4b-it` (rôle `coder`) : texte, JSON (`response_format`), appel d'outil (`tool_choice=required`) — six générations par
le pipeline complet. Identités durables `qualify:<scope>:<modèle>:<capacité>` (relancer ne réémet rien). Le premier refus, la première
ambiguïté ou la première réponse inexploitable ARRÊTE la qualification (`QualificationReport.ok=False`, `reason`, capacités suivantes « non
exécuté »), sans repli ni estimation ni renvoi. L'échéance globale est ouverte ICI (premier accès réel). Rapport sans secret (`to_dict()`).

### Échéance persistée appliquée au processus
Le délai du conteneur est `floor(min(allocation, échéance persistée − maintenant))` calculé AU LANCEMENT (`AttachedWorker.timeout_seconds`) et
passé à `DockerSandbox.run_command(timeout=…)` : auto-limite `timeout --signal=TERM` DANS le conteneur + kill par nom côté hôte. Un nouveau
`BudgetBinding` à fenêtre locale tardive ne l'allonge jamais ; si l'horloge n'est pas encore ouverte elle est ouverte au lancement réel ;
échéance atteinte ⇒ `allocate_worker` refuse (`deadline`) et `attach_worker` lève `SandboxRefused` (rien n'est lancé, le parent est laissé
à l'appelant). Un worker qui dort ou calcule après son dernier appel est arrêté ; une requête déjà émise à l'arrêt reste réservée (inconnue,
projet bloqué). Du planning au projet : la liaison conserve la ligne de scope (`planning:cycle:<id>`), donc l'horloge.

### Capacité publique et clé
`collegue.broker.capability_proof(settings, …) -> {"transport": "budget_broker", "accepted": bool, "reason": str, …}` : acceptée ssi toutes les
vérifications REQUISES passent sur le transport réellement instancié ; la présence de la clé est une information (`required=False`) sauf
`require_provider_key=True` (étape réelle). Les préflights statique et complet réussissent SANS clé ; aucune clé factice n'est injectée. La clé
reste `LLM_API_KEY` (réglages), lue seulement par le service de confiance ; C y lie son secret temporaire (`W5_GOOGLE_API_KEY → LLM_API_KEY`
dans l'étape réelle uniquement), sans alias global.

## Échéance absolue au lancement et séquencement des modèles (A27)

### Échéance absolue portée jusqu'à l'autorité qui supervise le conteneur
`AttachedWorker.deadline_epoch` (epoch UTC) = allocation ∩ échéance globale persistée. Elle est passée à
`DockerSandbox.run_command(..., deadline_epoch=…)`, qui la **revalide au dernier point avant `docker run`** (après la préparation du
workspace, des fichiers de sortie et de l'argv) : déjà atteinte (ou < 1 s) ⇒ `SandboxRefused`, rien n'est lancé, la réservation parent
est libérée par l'appelant. Sinon, le délai du conteneur et celui du filet hôte sont **recalculés à cet instant** : ni la préparation amont
(session, socket, preuve de transport) ni un démarrage Docker lent ne prolongent l'échéance.

Aucun délai de grâce de travail : le processus interne est lancé sous `timeout --signal=KILL N` (KILL, pas TERM — un worker peut ignorer
TERM — et sans `--kill-after`), et le filet hôte tue le conteneur **par nom** `DEADLINE_HOST_MARGIN` (2 s) après l'échéance si le conteneur a
démarré tard. Borne de vie du worker : `échéance + DEADLINE_HOST_MARGIN + tolérance d'ordonnancement` (démarrage lent), `échéance +
tolérance` sinon ; la collecte et le nettoyage se poursuivent ensuite. Le chemin direct (`timeout=`, TERM puis KILL à +15 s) est
inchangé. Une requête déjà émise sans usage connu reste réservée/bloquante (inchangé).

### Règle serveur de séquencement des modèles d'une session
Appliquée dans `BrokerStore.admit_emission` (transaction d'admission, donc sans course entre connexions) :
* le repli `gemma-4-26b-a4b-it` n'est admis qu'après une tentative principale (31B) **terminée** de la session : `released` (refus établi
  avant traitement) ou `settled` (consommation connue). Une réservation bornée n'est pas un antécédent ; sans tentative principale, pas de repli ;
* aucune génération d'un modèle différent tant qu'une autre génération de la session est `emitting` ou d'usage inconnu (le client a pu perdre
  la réponse ; le fournisseur la traite peut-être encore), dans les deux sens (repli → principal compris) ;
* refus : `BrokerForbidden(code="fallback_not_authorized")` (403), tentative libérée, rien d'émis ni gardé ;
* inchangé : sessions distinctes (autres rôles/allocations) restent concurrentes ; les canaris 26B côté hôte (`sampling_completion`, sans session)
  restent possibles ; un renvoi du MÊME modèle est une génération distincte, réservée et imputée à part (jamais gratuite) ; le même `request_id`
  rejoue le résultat sans régénérer.

### Politique du runner (`oh_runner`) en mode courtier
`num_retries=0` (le SDK ne retente jamais, même si `OH_NUM_RETRIES` est fourni) ; toute nouvelle émission est une décision du runner, que le
courtier tranche. `broker_failure_verdict(exc)` : code `upstream_rejected` / `count_tokens_failed` ⇒ bascule permise ; tout autre code du courtier
(budget, blocage, échéance, session fermée, modèle interdit…) ⇒ fin (`rc=4`), aucun repli ; échec de LLM sans verdict (délai dépassé, connexion
perdue) ⇒ issue inconnue, aucun repli (`rc=4`) ; exception étrangère au LLM ⇒ bascule historique. Preuve de bout en bout :
`tests/test_w5_broker_fallback_runner.py` (vrai `oh_runner.main()` → relais → socket → courtier → faux fournisseur).

## Admission finale (A28) : argv, démarrage tardif, sérialisation par session, reprise du serveur MCP

### Échéance absolue portée jusqu'au démarrage du travail dans le conteneur
`run_command(..., deadline_epoch=…)` construit d'abord l'argv COMPLET (la construction peut être lente), puis relit l'horloge **une dernière
fois, immédiatement avant `subprocess.run`** : échéance atteinte (ou < 1 s) ⇒ `SandboxRefused`, aucun `docker run`. La commande transporte
l'échéance ABSOLUE (`floor(deadline_epoch)`), pas une durée : `timeout --signal=KILL <plafond hôte> sh -c DEADLINE_GUARD_SCRIPT collegue-deadline
<epoch> <commande…>`. La garde, exécutée par le conteneur au démarrage effectif du travail, relit l'horloge du conteneur (même noyau que
l'hôte), sort en 124 sans rien exécuter si l'échéance est atteinte, sinon `exec timeout --signal=KILL <reste> <commande>`. Un démon qui
démarre le conteneur en retard ne peut donc pas faire travailler après l'échéance ; le filet hôte (kill du conteneur par nom à
`échéance + DEADLINE_HOST_MARGIN`) subsiste pour un conteneur qui ne démarre pas. Aucun délai de grâce de travail. Exigence image : `sh`,
`date` et `timeout` (coreutils/busybox), déjà utilisés ; **aucun changement de Dockerfile**.

### Une génération en vol par session worker
`_model_sequence_refusal` (transaction d'admission) refuse toute nouvelle émission d'une session tant qu'une AUTRE tentative de cette session
est `emitting` ou d'usage inconnu, quel que soit le modèle : `429 generation_in_flight` (rien d'émis, tentative libérée, réserve de la
première intacte, aucune inconnue artificielle). Un renvoi avec un nouvel identifiant après un délai client est donc refusé tant que l'issue
de la première n'est pas connue ; ensuite il est une génération légitime (réservée, imputée à part). Le rejeu du même `request_id` ne régénère
rien. Les sessions distinctes (autres workers, même scope) émettent en parallèle ; les producteurs hors session (planner, QA, reviewer,
canaris) ne sont pas concernés. L'antécédent du repli reste exigé (`403 fallback_not_authorized`, vérifié avant la sérialisation).

### Reprise au démarrage du serveur MCP
`collegue.broker.runtime.recover_at_startup(settings)` est appelée par `core_lifespan` (`collegue/app.py`) juste après `validate_llm_config()` :
hors mode courtier ou sans `STATE_DATABASE_URL` ⇒ sans effet ; état jamais migré (pas de tables du courtier) ⇒ rien à réparer ; sinon
`recover_all()` d'un service JETABLE (`BrokerRuntime.recovery_service`) sur le registre de `STATE_DATABASE_URL` (celui du pilote et des
outils). Émission abandonnée ⇒ inconnue + projet bloqué ; propriétaire vivant conservé ; aucune échéance ouverte, aucune requête Google.
Toute autre erreur (état illisible) REFUSE le démarrage. Aucun opérateur n'a à appeler `recover_all`.

## Précontrôle de séquence avant countTokens (A29) et précision de la garde

**Précontrôle.** Dans `_generate`, après création de la tentative et avant toute requête fournisseur, `BrokerStore.sequence_refusal` applique en lecture seule la
règle de séquence (pas de 26B sans antécédent ; pas de nouvelle génération sur une session occupée). Refus précoce : même erreur que l'admission finale
(`403 fallback_not_authorized` / `429 generation_in_flight`), **aucun countTokens**, tentative journalisée et libérée (`error_code` = le code), aucune réservation prise, aucun
changement d'usage connu, réserve de la génération en vol intacte. Ce n'est qu'un précontrôle : l'**admission transactionnelle après countTokens reste obligatoire et décisive**
(une génération peut être admise, un blocage ou une fermeture survenir pendant le comptage ; test : une génération admise pendant le countTokens de la première lui interdit
l'émission et sa réserve est libérée).

**Précision de la garde (mesurée, pas postulée).** La garde interne tronque l'échéance (`floor(deadline_epoch)`) ET l'heure courante (`date +%s`) : l'arrêt tombe dans
`[D_int, D_int + 1[` secondes d'horloge conteneur, soit **entre −1 s et +1 s** de l'échéance réelle `D`, plus la latence d'ordonnancement. Le travail PEUT donc dépasser l'échéance de
moins d'une seconde ; il n'est pas vrai qu'il ne puisse « jamais gagner » du temps. Mesure (12 exécutions, faux docker + vrai `DockerSandbox`, worker qui ignore TERM, dernier battement −
échéance) : sans retard de démarrage −1,00 s à −0,97 s ; démarrage retardé de 2,5 s −0,50 s à +0,50 s (`evidence/w5-a29-guard-measure.json`). Critère retenu, étroit et explicite :
`SCHEDULING_TOLERANCE = 1,0 s` dans les tests (le filet hôte à `+DEADLINE_HOST_MARGIN` = 2 s ne couvre que le conteneur qui ne démarre pas). Hypothèse non vérifiée ici : horloge du conteneur = horloge de l'hôte.

## Arrêt à l'échéance sous `timeout` GNU : fidélité du banc, arbitrage (A32 → A33)

**Défaut établi : la traduction des codes de sortie du banc.** Le rouge distant (Python 3.11 et 3.12) venait du faux `docker` des tests : avec le `timeout` de GNU coreutils
(CI) le processus du conteneur simulé meurt de SIGKILL (code brut −9) et le faux `docker` rendait `sys.exit(-9)` = 247, un code que Docker ne produit pas (il rend 137 = 128+9).
Le sandbox reconnaît 124 et 137 ; avec 247, `timed_out` restait faux et le journal vide. Avec le `timeout` d'uutils (poste local) le code est 124 : le défaut ne se voyait pas.
Le faux `docker` rend désormais 128+N pour un signal N, consigne le code brut et le code rendu, et la suite d'échéance est rejouée sous les DEUX `timeout` (uutils, et GNU 9.7 par PATH).

**Observation locale, non reclassée en défaut produit.** Le `timeout` externe (plafond hôte) et le `timeout` interne (garde) créent chacun leur groupe de processus ; l'externe peut partir le
premier (jusqu'à ~1 s avant l'échéance, tolérance A29) et ne tuer que le `timeout` interne, laissant le worker vivant jusqu'à la mort du PID 1 du conteneur. Dans un conteneur réel, la
mort du PID 1 démonte son PID namespace et tue TOUS les processus restants : c'est l'isolation et la supervision de Docker, non un mécanisme qui « masquerait » un défaut. L'exigence est
qu'aucun worker ne survive AU CONTENEUR et au délai accepté. Le banc émule ce démontage et vérifie l'absence de survivants APRÈS lui ; les survivants vivants à l'instant du démontage sont
consignés à titre d'information, sans être exigés nuls. **Le produit (`collegue/sandbox/executor.py`) est inchangé depuis A29** (garde, marges et tolérance de 1 s d'origine). Un correctif
de supervision proposé en A32 (`+2 s` au filet, `−1 s` à la garde) a été écarté à l'arbitrage : non nécessaire au défaut distant, et la troncature de `floor(échéance)` sur une échéance
flottante pouvait arrêter près de 2 s trop tôt.

**Journal des refus avant tentative (revue A31).** Il ne recopie plus rien de ce que le client a fourni : ni le texte de l'exception, ni un nom de champ arbitraire, ni une valeur
de rôle / modèle. Il porte `code` (liste fermée `LOGGABLE_REFUSAL_CODES`, sinon « autre »), `statut`, les noms de paramètres CONNUS (liste fermée `KNOWN_REFUSED_PARAMETERS`, ex.
`reasoning_effort`) et le NOMBRE des autres. Le client reçoit toujours le refus explicite complet.
