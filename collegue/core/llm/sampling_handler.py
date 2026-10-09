"""Handler de sampling FastMCP avec capture d'usage et routage par modèle.

Défini au niveau module (et non dans un ``if`` de ``app.py``) pour être
importable et testable. Deux différences avec ``OpenAISamplingHandler`` :

1. **Capture des tokens réels** : le handler de base ne propage pas
   ``response.usage`` ; on enveloppe ``chat.completions.create`` pour exposer
   l'usage aux outils via un ContextVar (``collegue/monitoring/sampling_usage``).
2. **Honore un modèle arbitraire** : le handler de base ne retient que les
   modèles OpenAI connus (``ChatModel``) et ignorerait silencieusement un modèle
   Gemini/local passé en préférence → le routage par rôle serait un no-op. Ici,
   toute préférence explicite non vide prime, sinon le modèle par défaut.
3. **Garde budget dur (C4)** : avant chaque appel, ``enforce_budget()`` vérifie
   le plafond $/tokens cumulé. C'est le chokepoint universel (tous les
   ``ctx.sample()`` passent ici) → une boucle LLM emballée est stoppée (auto-pause)
   au lieu de brûler tout le budget. No-op si les plafonds sont désactivés.

4. **Une destination par rôle (vague 4)** : :class:`RoutingSamplingHandler` résout, pour CHAQUE requête, la route du
   rôle porté par le hint ``collegue-route:<rôle>`` des ``modelPreferences`` (fournisseur, modèle, endpoint, clé) et
   délègue à un handler dédié à cette route (un client par destination+identité, jamais partagé). Le rôle voyage dans le
   NOM d'un hint, donc survit à la sérialisation MCP ; un attribut Python ajouté à l'objet de préférences y serait perdu.
   Deux rôles de même modèle mais de clés/endpoints distincts restent distinguables, y compris en appels concurrents.

``build_sampling_handler`` / ``build_routing_sampling_handler`` sont tolérants : ils retournent ``None`` si
``fastmcp`` / ``openai`` ne sont pas disponibles, pour ne pas casser le démarrage.

Portée : ce handler n'est utilisé que si le client MCP n'annonce pas la capacité de sampling
(``sampling_handler_behavior="fallback"``). Un client externe qui échantillonne lui-même choisit seul destination et
identifiants : Collègue ne les contrôle ni ne les budgétise (il ne reçoit que le modèle canonique et le hint de route).
"""

from __future__ import annotations

from typing import Any, Optional

# Plafond de sortie appliqué quand l'appelant n'en fournit aucun (sortie bornée ⇒ coût borné).
DEFAULT_BOUNDED_MAX_TOKENS = 4096


_OUTPUT_LIMIT_KEYS = ("max_completion_tokens", "max_tokens")


class OutputLimitError(ValueError):
    """Limite de sortie invalide ou contradictoire : refusée AVANT toute réservation et toute émission."""


def normalize_output_limit(kwargs: dict, *, default: Optional[int] = None) -> Optional[int]:
    """Ramène ``kwargs`` à UNE seule limite de sortie effective (modifie ``kwargs``) et la renvoie.

    FastMCP transmet ``max_completion_tokens`` ; un appelant direct peut passer ``max_tokens``. Les deux ensemble
    donneraient un corps HTTP à deux bornes qui peuvent diverger : la réservation budgétaire et la requête réellement
    émise doivent partager la MÊME. Valeurs non entières, booléennes ou ≤ 0 : refus ; deux valeurs différentes : refus
    (jamais la plus petite ou la plus grande « au hasard »). Absente, la limite vaut ``default`` (nom historique
    ``max_tokens``) ; sans ``default`` elle reste absente (``None``). La limite demandée par l'appelant n'est jamais réduite.
    """
    present = {}
    for key in _OUTPUT_LIMIT_KEYS:
        if key in kwargs and kwargs[key] is not None:
            value = kwargs[key]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise OutputLimitError(f"{key} invalide ({type(value).__name__}) : entier strictement positif requis")
            present[key] = value
    for key in _OUTPUT_LIMIT_KEYS:
        kwargs.pop(key, None)
    if len(set(present.values())) > 1:
        raise OutputLimitError(
            "max_tokens et max_completion_tokens contradictoires : une seule limite de sortie permise"
        )
    if present:
        key = next(k for k in _OUTPUT_LIMIT_KEYS if k in present)
        kwargs[key] = present[key]
        return present[key]
    if default is not None:
        kwargs["max_tokens"] = int(default)
        return int(default)
    return None


def _usage_of(resp):
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    prompt, completion = getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)
    if not isinstance(prompt, int) or not isinstance(completion, int) or prompt < 0 or completion < 0:
        return None
    return prompt, completion, str(getattr(resp, "model", "") or "")


def _make_handler_class():
    """Construit la classe handler (import paresseux de fastmcp/openai)."""
    from fastmcp.client.sampling.handlers.openai import OpenAISamplingHandler

    from collegue.monitoring.sampling_usage import record_usage

    class UsageTrackingSamplingHandler(OpenAISamplingHandler):
        """Handler OpenAI-compatible : capture d'usage + modèle arbitraire honoré."""

        def __init__(self, *args, provider: Optional[str] = None, **kwargs):
            super().__init__(*args, **kwargs)
            self.route_provider = provider
            inner = self.client.chat.completions.create

            async def _create(*a, **kw):
                from collegue.core.llm.budget_guard import TRANSPORT_HTTP, current_binding, guarded_call

                binding = current_binding()
                if binding is not None:
                    # Registre durable lié (moteur autonome) : réservation AVANT chaque tentative, pas de
                    # retry interne du SDK (max_retries=0), règlement avec l'usage réel.
                    # La sortie DOIT être bornée par UNE limite réellement transmise (celle de l'appelant, sinon la
                    # borne par défaut) : la même sert à la réservation et au corps de la requête.
                    limit = normalize_output_limit(kw, default=DEFAULT_BOUNDED_MAX_TOKENS)
                    once = self.client.with_options(max_retries=0) if hasattr(self.client, "with_options") else None
                    target = once.chat.completions.create if once is not None else inner

                    async def _emit():
                        return await target(*a, **kw)

                    response = await guarded_call(
                        _emit,
                        binding=binding,
                        model=str(kw.get("model") or self.default_model or ""),
                        messages=kw.get("messages"),
                        tools=kw.get("tools"),
                        max_tokens=int(limit),
                        transport=TRANSPORT_HTTP,
                        usage_of=_usage_of,
                        max_attempts=3,  # = les 2 retries par défaut du SDK, désormais réservés un à un
                        endpoint=str(getattr(self.client, "base_url", None) or "") or None,
                        provider=self.route_provider,
                    )
                else:
                    # Garde budget dur historique (C4, MetricsCollector) : NON couverte par la garantie
                    # stricte du registre durable (serveur MCP sans projet). No-op si plafonds désactivés.
                    from collegue.monitoring.metrics import enforce_budget

                    normalize_output_limit(kw)  # une seule borne dans le corps, même sans registre
                    enforce_budget()
                    response = await inner(*a, **kw)
                usage = getattr(response, "usage", None)
                if usage is not None:
                    record_usage(
                        getattr(usage, "prompt_tokens", 0) or 0,
                        getattr(usage, "completion_tokens", 0) or 0,
                        getattr(response, "model", "") or "",
                    )
                return response

            self.client.chat.completions.create = _create

        def _select_model_from_preferences(self, model_preferences):
            from collegue.core.llm.roles import ROUTE_HINT_PREFIX

            for name in self._iter_models_from_preferences(model_preferences):
                if name and not str(name).startswith(ROUTE_HINT_PREFIX):
                    return name
            return self.default_model

    return UsageTrackingSamplingHandler


def resolve_openai_endpoint(settings_obj: Any) -> tuple[str, Optional[str], Optional[str]]:
    """``(default_model, api_key, base_url)`` du rôle PAR DÉFAUT pour un client OpenAI-compatible (compatibilité).

    Dérive de :func:`collegue.core.llm.roles.resolve_route` (source unique de la destination). ``api_key`` peut être
    ``None`` (aucune clé définie) : la validation stricte appartient au transport, qui refuse avant d'émettre.
    """
    from collegue.core.llm.roles import LLMRole, resolve_route

    route = resolve_route(LLMRole.DEFAULT, settings_obj, require_credential=False)
    return route.model, route.credential() or ("local" if route.auth == "none" else None), route.endpoint


def build_sampling_handler(default_model: str, api_key: Optional[str], base_url: Optional[str]) -> Optional[Any]:
    """Handler STATIQUE (une seule destination) ; ``None`` si les dépendances manquent. Voir le handler routé."""
    try:
        from openai import AsyncOpenAI
    except ImportError:
        return None
    try:
        handler_cls = _make_handler_class()
    except ImportError:
        return None
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    return handler_cls(default_model=default_model, client=client)


def build_routing_sampling_handler(settings_obj: Any) -> Optional[Any]:
    """Handler de sampling ROUTÉ par rôle (voir :class:`RoutingSamplingHandler`) ; ``None`` si fastmcp/openai manquent."""
    try:
        import openai  # noqa: F401

        inner_cls = _make_handler_class()
    except ImportError:
        return None
    return _make_routing_class(inner_cls)(settings_obj)


def _make_routing_class(inner_cls):
    from collegue.core.llm.roles import LLMRole, LLMRoutingError, parse_route_preferences, resolve_route

    class RoutingSamplingHandler:
        """Résout la route du rôle de CHAQUE requête puis délègue au handler de cette route.

        Les handlers par route sont mis en cache sur ``(fournisseur, endpoint, authentification, empreinte de clé)`` :
        une rotation de clé ou un autre endpoint produit un autre client, jamais le client d'une autre identité.
        Les erreurs de routage (contradiction, clé absente, abonnement non supporté ici) sont levées AVANT toute émission.
        """

        def __init__(self, settings_obj: Any) -> None:
            self._settings = settings_obj
            self._handlers: dict = {}

        def _handler_for(self, route):
            from openai import AsyncOpenAI

            if route.uses_subscription:
                raise LLMRoutingError(
                    f"rôle {route.role} : l'authentification par abonnement n'est pas supportée par le handler "
                    "serveur (réservée au ctx offline et au worker) — refusé, aucune bascule vers une clé API"
                )
            key = route.cache_key()
            handler = self._handlers.get(key)
            if handler is None:
                client = AsyncOpenAI(api_key=route.transport_key(), base_url=route.endpoint)
                handler = inner_cls(default_model=route.model, client=client, provider=route.provider)
                self._handlers[key] = handler
            return handler

        async def __call__(self, messages, params, context):
            role, models = parse_route_preferences(getattr(params, "modelPreferences", None))
            route = resolve_route(role or LLMRole.DEFAULT, self._settings)
            if models and models[0] != route.model:
                raise LLMRoutingError(
                    f"préférence de modèle {models[0]!r} contradictoire avec la route du rôle {route.role!r} "
                    f"({route.model!r}) : refusée"
                )
            return await self._handler_for(route)(messages, params, context)

    return RoutingSamplingHandler
