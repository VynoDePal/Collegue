# Vague 4 — A : destination et authentification résolues ensemble par rôle

Avant la vague 4, le modèle d'un rôle pouvait changer sans que le client, l'endpoint ni la clé changent
(`gpt-5.4` pour le planificateur partait chez Gemini avec la clé Gemini ; le codeur `openai/gpt-5.4` devenait
`gemini/gpt-5.4` ; l'abonnement se déduisait du nom du modèle). Désormais **une seule résolution**,
`collegue.core.llm.roles.resolve_route`, produit la destination effective d'un rôle, et chaque transport l'applique
telle quelle.

## Route

`LLMRoute` (dataclass gelée) : `role`, `provider`, `model` (nom canonique nu), `endpoint`, `explicit_endpoint`,
`auth` (`api_key` | `none` | `subscription`), `credential_source` (`role` | `global` | `none` | `subscription`),
`credential_fingerprint` (SHA-256 tronqué à 12 caractères). La clé n'est accessible que par `route.credential()`,
jamais par `repr`, `str`, `describe()`, les erreurs de configuration ni les `ValidationError` (`hide_input_in_errors`,
`SecretStr` pour les clés de rôle).

Rôles : `CODER`, `QA`, `REVIEWER`, `PLANNER`, `DEFAULT`. Fournisseurs : `gemini`, `openai` (hébergés), `lmstudio`,
`ollama`, `unsloth` (locaux). Tout autre fournisseur est refusé.

### Règles de résolution

1. Fournisseur du rôle = `LLM_PROVIDER_<ROLE>` sinon `LLM_PROVIDER`. Modèle = `LLM_MODEL_<ROLE>` sinon `LLM_MODEL`
   **uniquement si le fournisseur du rôle est le fournisseur global** (un autre fournisseur exige son modèle).
2. Le modèle doit appartenir au fournisseur : `gemini-*`/`gemma-*` refusés sous `openai`, `gpt-*` refusé sous `gemini`
   ; un préfixe `gemini/` ou `openai/` contradictoire est refusé. Un préfixe cohérent est accepté et retiré.
3. Clé : `LLM_API_KEY_<ROLE>` sinon `LLM_API_KEY` **seulement si le fournisseur du rôle est le fournisseur global**.
   Fournisseur local **sans choix d'authentification** ou avec `LLM_AUTH_<ROLE>=none` : accepté sans clé (`auth=none`,
   valeur fictive explicite `local` au transport) ; `none` avec une clé effective (de rôle ou héritée) est une
   contradiction refusée. `LLM_AUTH_<ROLE>=api_key` est un choix **explicite** : sans clé effective (de rôle, ou globale du
   MÊME fournisseur) la route est refusée avant tout transport (`LLMMissingCredentialError`), même pour un fournisseur
   local — jamais dégradée en accès anonyme. Hébergé sans clé : refus.
   `require_credential=False` (nom, préflight, tarification) valide la cohérence mais conserve `auth=api_key` sans
   credential ; aucun transport émetteur ne l'accepte (`LLMRoute.transport_key()` refuse), et `validate_role_routes` /
   `resolve_route` sont stricts par défaut. Au démarrage, ce cas est un rôle « sans credential » (refusé à son appel), pas une
   contradiction : les autres rôles valides continuent de servir.
4. Endpoint : `LLM_BASE_URL_<ROLE>` ; sinon `LLM_BASE_URL` global si le fournisseur est celui du global ; sinon défaut
   du fournisseur. Un endpoint configuré n'est **jamais ignoré en silence**, y compris pour `gemini` : il est respecté
   (passerelle compatible) ou refusé. Un endpoint dont l'hôte est un AUTRE fournisseur hébergé (`api.openai.com` sous
   `gemini`, `generativelanguage.googleapis.com` sous `openai`) est une contradiction refusée avant émission, car il
   recevrait la clé de ce rôle. Le worker OpenHands, lui, refuse explicitement un endpoint personnalisé pour un
   codeur `gemini` (LiteLLM route `gemini/…` vers l'API Google).
5. Abonnement (ChatGPT/Codex) : **jamais déduit du nom du modèle**. Explicite par `CODER_SUBSCRIPTION=true` (codeur)
   ou `LLM_AUTH_<ROLE>=subscription`, fournisseur `openai` uniquement, sans clé de rôle contradictoire.
6. Préférences d'appelant : une préférence de modèle qui différerait du modèle du rôle est refusée
   (`normalize_preferences`), car elle changerait le modèle sans changer client, endpoint ni clé.

Toute violation lève `LLMRoutingError` **avant** toute émission.

## Tableau des combinaisons prises en charge

| Fournisseur du rôle | Authentification | Sampling offline (`LocalSamplingContext`) | Handler serveur FastMCP | Worker OpenHands (codeur) |
|---|---|---|---|---|
| `gemini` | clé (rôle ou globale Gemini) | oui, endpoint Google | oui | oui, `gemini/<modèle>`, repli `gemma-4-26b-a4b-it` par défaut |
| `openai` | clé du rôle (ou globale si le global est openai) | oui, `api.openai.com` ou `LLM_BASE_URL_<ROLE>` | oui | oui, `openai/<modèle>`, `LLM_BASE_URL` si nommé, aucun repli par défaut |
| `openai` | `subscription` | oui (sandbox `oh_sampler`) | **refusé** (pas de repli vers une clé API) | oui (`subscription_login`, modèle nu) |
| `lmstudio` / `ollama` / `unsloth` | `none` ou clé | oui, endpoint local par défaut ou nommé | oui | oui, `openai/<modèle>` + `LLM_BASE_URL` |
| autre | — | refusé | refusé | refusé |

## Transport du rôle

* `model_preferences_for_role(role, settings)` renvoie `[modèle_canonique, "collegue-route:<rôle>"]`. Le rôle voyage
  dans un **nom de hint** : il survit à la sérialisation MCP (un attribut Python ajouté à l'objet y serait perdu).
  Le premier hint reste le modèle canonique pour un client MCP externe.
* `accounted_sample` normalise les préférences, puis le transport résout `resolve_route` pour le rôle lu dans le hint.
* `LocalSamplingContext.from_settings(settings)` : un client `AsyncOpenAI` par `(fournisseur, endpoint, auth,
  empreinte de clé)`, toujours créé avec `api_key` et `base_url` explicites (le SDK ne relit jamais
  `OPENAI_API_KEY`/`OPENAI_BASE_URL` de l'hôte). Une rotation de clé produit un autre client.
* `RoutingSamplingHandler` (`build_routing_sampling_handler`) : même résolution par requête, un handler interne (et un
  client) par route ET par modèle — le modèle fait partie de la clé de cache, car le handler interne fige son modèle de
  repli (requête sans modèle) ; aucun état partagé n’est muté, donc sûr en appels concurrents. Utilisé par `app.py` au démarrage (le rôle `DEFAULT` doit être cohérent, sinon le serveur refuse de
  construire le handler).
* Worker : `runtime._coder_sandbox_env` (non secret : `LLM_MODEL` au format LiteLLM du fournisseur du rôle,
  `LLM_BASE_URL`, `OH_FALLBACK_MODELS` toujours posé — vide = aucun repli) et `_coder_sandbox_secrets` (clé de SA
  route). La clé passe par `DockerSandbox(env_secrets=...)` : `-e LLM_API_KEY` sans valeur dans l'argv, valeur remise
  au seul sous-process `docker` (`subprocess.run(env=...)`), `os.environ` n'est jamais muté. Le repli du codeur est
  toujours du même fournisseur (`CODER_FALLBACK_MODELS`).
* `oh_runner` construit `LLM(**llm_kwargs(...))` avec `model`, `api_key`, `base_url` (si nommé) et les paramètres
  communs, tous dans `LLM_CONSTRUCTOR_KWARGS`.

## Budget (W2 inchangé)

La réservation juge la destination **réellement émise** : `guarded_call(provider=...)` reçoit le fournisseur de la
route et `RouteSettingsView` remplace `LLM_PROVIDER`/`llm_base_url` globaux par ceux de la route pour le tarif, le
tokenizer et la gratuité d'un fournisseur local. Chaque tentative (retries compris) est réservée avant émission, sur
le client du rôle. Une destination non attestée (passerelle dont l'identité de modèle n'est pas reconnue) est refusée
en mode strict avant émission. `worker_budget` tarife chaque modèle de la chaîne selon l'endpoint et la famille de la
route (abonnement : famille openai, 0 $). La capacité du worker reste unique dans `worker_budget`
(`budget_enforcement = "in-runner"` ne borne pas une commande du workspace qui réutiliserait les credentials) :

* clé facturable sous plafond strict : **refus** ;
* abonnement sous plafond strict de **tokens** : **refus** (le backend abonnement ne garantit pas le plafond de sortie
  en amont, les commandes du workspace ont les credentials montés). La campagne 2 USD / 250 000 tokens / 900 s est donc
  refusée en strict avec un codeur par abonnement comme avec une clé facturable ;
* seul un plafond **USD sans plafond de tokens** est accepté pour l'abonnement (0 $ établi) ;
* mode `advisory` : disponible, sans garantie stricte.

Rien n'est assoupli.

### Limite de sortie du handler serveur

FastMCP transmet `max_completion_tokens` ; le handler ramène la requête à UNE seule limite effective
(`normalize_output_limit`), la même pour la réservation et pour le corps HTTP. La limite de l'appelant n'est jamais
réduite ni complétée ; sans limite (appel direct) la borne par défaut est 4096 (`max_tokens`) ; une valeur invalide
(non entière, booléenne, ≤ 0) ou deux limites différentes sont refusées avant réservation et émission.

### Montage d'abonnement du worker

`SANDBOX_SUBSCRIPTION_AUTH_DIR` n'est monté (avec `HOME` hors `/tmp`) que si la route **du codeur** est en abonnement.
Un reviewer ou un QA en abonnement n'impose pas ce montage à un codeur par clé API ; un codeur en abonnement sans ce
dossier est refusé avant lancement. La garde W1 (`HOME` hors `/tmp`) est inchangée.

## Contrat du constructeur `openhands.sdk.LLM` (SDK 1.19.1)

* `LLM` déclare `extra="ignore"` : un argument inconnu est **ignoré sans erreur**. L'ancien `service_id="coder"` n'existe pas
  dans 1.19.1 (le champ canonique est `usage_id`, défaut `"default"`) : l'identité du coder n'était donc jamais posée. Le runner
  passe `usage_id="coder"` et le sampler d'abonnement `usage_id="sampler"` ; `LLM_CONSTRUCTOR_KWARGS` ne contient que des champs de
  `LLM.model_fields`.
* Abonnement : `LLM.subscription_login(vendor, model, …, **kwargs)` transmet `kwargs` à `LLM(...)` via `create_llm`, qui fixe déjà
  `max_output_tokens=None` (le backend Codex ne le supporte pas ; le SDK ne l'envoie jamais en mode abonnement) : le repasser lève
  `TypeError`. Le runner et le sampler ne transmettent donc que `LLM_SUBSCRIPTION_KWARGS` (`usage_id`, `num_retries`,
  `retry_min_wait`, `retry_max_wait`, `timeout`).
* Conséquence budgétaire (inchangée, à décider hors de ce lot) : un LLM d'abonnement n'a pas de `max_output_tokens` local ; sous
  allocation la garde du runner constate une sortie non bornée et REFUSE l'émission (fail-closed).

## Démarrage du serveur (`collegue/app.py`)

* `validate_llm_config()` est **locale** : elle résout chaque rôle (`default`, `coder`, `qa`, `reviewer`, `planner`) avec
  `check_role_routes` et n'émet **aucune requête** (plus de `models.retrieve` OpenAI, `models.list` local ni
  `google.genai`). Elle valide la cohérence de la configuration et **ne prouve pas** que le fournisseur est joignable ni
  que le modèle existe : la disponibilité n'est jamais présumée, et aucune clé ne part ailleurs que par un appel routé.
* Une configuration **contradictoire** (modèle hors famille du fournisseur, endpoint d'un autre fournisseur hébergé,
  fournisseur hors catalogue — `anthropic` compris —, abonnement incohérent) refuse le démarrage (`ValueError`, message
  sans secret) et aucun handler n'est attaché.
* Un rôle **cohérent mais sans clé de son fournisseur** (rôle `default` compris) n'empêche pas les autres de servir : son
  appel est refusé avant émission (`LLMMissingCredentialError`). Quatre clés de rôle (`LLM_API_KEY_<ROLE>`) sans
  `LLM_API_KEY` global suffisent donc à démarrer et à servir ces quatre rôles. Si **aucun** rôle ne peut servir (aucune
  clé nulle part, ou uniquement des routes d'abonnement que le handler serveur ne sert pas), le démarrage est refusé.
* Le handler routé est attaché à l'application (`app.sampling_handler`) dès qu'au moins un rôle peut servir et qu'aucune
  route n'est contradictoire.
* OAuth (W1) est inchangé : `OAUTH_ENABLED=true` reste fail-closed, sans repli anonyme.
* Les profils CI en `LLM_PROVIDER=anthropic` (smoke Docker `--network none`, vérification de la roue) sont désormais
  refusés ; un profil supporté sans réseau est `LLM_PROVIDER=gemini LLM_API_KEY=test-key LLM_MODEL=test-model`
  (la validation ne contacte rien).

## Portée exacte du sampling délégué

Si le client MCP annonce la capacité de sampling, il échantillonne **lui-même** : il choisit destination et
identifiants, Collègue ne les contrôle ni ne les budgétise ; il ne reçoit que le modèle canonique et le hint
`collegue-route:<rôle>`. Le handler serveur n'est qu'un repli (`sampling_handler_behavior="fallback"`). Le serveur ne
peut donc pas garantir la route ni le plafond pour un client externe qui échantillonne.

## Exemples de configuration (sans secret)

```env
# Tout Gemini (défaut)
LLM_PROVIDER=gemini
LLM_MODEL=gemini-2.5-flash
LLM_API_KEY=<clé Gemini>

# Planificateur/QA/revue sur OpenAI, avec leurs propres clés ; codeur sur la clé globale Gemini
LLM_PROVIDER_PLANNER=openai
LLM_MODEL_PLANNER=gpt-5.4
LLM_API_KEY_PLANNER=<clé OpenAI planificateur>
LLM_PROVIDER_QA=openai
LLM_MODEL_QA=gpt-5.4
LLM_API_KEY_QA=<clé OpenAI QA>
LLM_BASE_URL_QA=https://passerelle.exemple/v1
LLM_MODEL_CODER=gemma-4-31b-it

# QA local sans clé
LLM_PROVIDER_QA=lmstudio
LLM_MODEL_QA=qwen3
LLM_BASE_URL_QA=http://127.0.0.1:1234/v1

# Codeur par abonnement (explicite)
CODER_SUBSCRIPTION=true
CODER_SUBSCRIPTION_MODEL=gpt-5.5
SANDBOX_SUBSCRIPTION_AUTH_DIR=~/.openhands
```

## Migration et limites

* Un rôle non codeur qui utilisait l'abonnement parce que son modèle n'était « pas Gemini » doit maintenant le demander :
  `LLM_AUTH_<ROLE>=subscription` (avec `LLM_PROVIDER_<ROLE>=openai`). Sans cela il part sur l'API facturée avec la
  clé du rôle, ou est refusé faute de clé.
* `LLM_BASE_URL` global n'est plus ignoré pour `gemini` : respecté pour le sampling, refusé s'il désigne un autre
  fournisseur hébergé. Un endpoint (de rôle ou hérité du global) pour un codeur `gemini` est refusé par le worker
  (LiteLLM route `gemini/…` vers l'API Google) : retirer `LLM_BASE_URL` pour ce codeur.
* Repli du codeur : Gemini garde `gemma-4-26b-a4b-it` ; tout autre fournisseur n'a aucun repli sans
  `CODER_FALLBACK_MODELS` (un repli d'un autre fournisseur est refusé).
* L'abonnement n'est pas supporté par le handler serveur FastMCP (refus explicite, pas de bascule vers une clé).
* Le SDK OpenHands (1.19.1, `locks/sandbox-openhands.txt`) n'est pas installé dans l'environnement de développement :
  le constructeur est testé avec un module `openhands.sdk` factice. Le contrôle réel
  (`LLM_CONSTRUCTOR_KWARGS` ⊂ `LLM.model_fields`) est porté par `scripts/ci_w4_worker_routing.py` (C), qui charge
  `/opt/oh_runner.py` par chemin dans l'image : le paquet `collegue` n'y est PAS installé, aucun `import collegue`.
* `validate_role_routes(settings, roles)` : préflight sans émission ni dépense, sortie sans secret.
