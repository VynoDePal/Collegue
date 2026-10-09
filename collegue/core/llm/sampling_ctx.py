"""Contexte de sampling **offline** (hors serveur MCP) pour piloter le moteur en CLI.

Le planner (``generate_spec``/``decompose``), le reviewer (``code_review`` via
``BaseTool``) et la boucle agentique attendent un objet ``ctx`` exposant une
coroutine ``sample(...)`` (sampling FastMCP). En CLI il n'y a **pas** de serveur
MCP, donc pas de ``ctx`` : ``run_project_from_settings`` recevait ``ctx=None`` et
les chemins LLM réels échouaient.

Ce module fournit un ``ctx`` qui appelle le LLM via un client **OpenAI-compatible**
(``AsyncOpenAI``), en réutilisant la MÊME résolution de destination que le handler serveur
(:func:`collegue.core.llm.roles.resolve_route`) → **multi-provider** (Gemini OpenAI-compat / OpenAI /
lmstudio / ollama / unsloth), pas de lock-in (brief §8). Vague 4 : chaque appel résout la route DE SON RÔLE
(fournisseur, modèle, endpoint, clé) ; un client est construit par route, jamais partagé entre deux identités. Au même chokepoint que le handler serveur, on applique la
**garde budget dur** (``enforce_budget``, C4) et la **capture d'usage**
(``record_usage``) — tous les ``ctx.sample()`` offline transitent ici.

Contrat reproduit fidèlement (lu dans le code) :
- ``sample(messages, *, system_prompt, result_type, temperature, max_tokens,
  model_preferences, **ignored)`` renvoie un objet avec ``.text`` (texte brut) et
  ``.result`` (instance ``result_type`` si parsable, sinon le texte) — exactement
  ce que lisent ``tools/base.py`` (``result.result if result_type else result.text``)
  et le planner ;
- ``model_preferences=[modèle, "collegue-route:<rôle>"]`` (voir ``model_preferences_for_role``) route vers la destination
  complète du rôle ; une préférence qui contredit le modèle du rôle est REFUSÉE (elle changerait le modèle sans changer
  client, endpoint ni clé) ;
- stubs ``info/debug/warning/error/report_progress`` no-op attendus par les outils ;
- ``max_output_tokens`` généreux par défaut (quirk gemma : trop bas ⇒ contenu vide).

Le client ``AsyncOpenAI`` est construit **paresseusement** (au 1er ``sample``) : la
seule construction du ctx n'ouvre aucune connexion ni n'exige de clé (les tests qui
n'échantillonnent pas restent inertes).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from collegue.core.llm.roles import (
    LLMRole,
    LLMRoute,
    LLMRoutingError,
    parse_route_preferences,
    resolve_route,
)

DEFAULT_MAX_TOKENS = 8192

# Sortie du sampler d'abonnement (oh_sampler.py) — texte entre marqueurs.
_SAMPLE_RE = re.compile(r"<<<SAMPLE_BEGIN>>>(.*?)<<<SAMPLE_END>>>", re.S)
_SAMPLE_USAGE_RE = re.compile(r"<<<SAMPLE_USAGE>>>(.*?)<<<SAMPLE_USAGE_END>>>", re.S)


@dataclass
class SampleResult:
    """Réponse de sampling : ``.text`` brut + ``.result`` structuré optionnel."""

    text: str = ""
    result: Any = None


class PerModelRateLimiter:
    """Cap glissant par modèle : ``per_minute`` req/60 s et ``per_day`` req/24 h.

    ``acquire`` bloque (``asyncio.sleep``) jusqu'à ce qu'un créneau se libère. Le
    décompte est par **nom de modèle**. Thread-safe (lock court) ; attentes hors-lock.
    Sert à respecter un quota partagé (ex. Gemini free-tier) pour les appels de CE ctx.
    """

    def __init__(self, per_minute: int = 0, per_day: int = 0):
        self.per_minute = max(0, int(per_minute or 0))
        self.per_day = max(0, int(per_day or 0))
        self._minute: Dict[str, deque] = {}
        self._day: Dict[str, deque] = {}
        self._lock = threading.Lock()

    def _wait_needed(self, model: str, now: float) -> float:
        mdq = self._minute.setdefault(model, deque())
        ddq = self._day.setdefault(model, deque())
        while mdq and now - mdq[0] >= 60:
            mdq.popleft()
        while ddq and now - ddq[0] >= 86400:
            ddq.popleft()
        wait = 0.0
        if self.per_minute and len(mdq) >= self.per_minute:
            wait = max(wait, 60.0 - (now - mdq[0]))
        if self.per_day and len(ddq) >= self.per_day:
            wait = max(wait, 86400.0 - (now - ddq[0]))
        return wait

    async def acquire(self, model: str) -> None:
        while True:
            now = time.time()
            with self._lock:
                wait = self._wait_needed(model, now)
                if wait <= 0:
                    self._minute[model].append(now)
                    self._day[model].append(now)
                    return
            await asyncio.sleep(min(wait, 5.0) + 0.05)


def _pick_model(model_preferences: Any, default_model: str) -> str:
    """Choisit le modèle effectif depuis ``model_preferences`` (rôle → modèle)."""
    if isinstance(model_preferences, (list, tuple)) and model_preferences:
        first = str(model_preferences[0]).strip()
        if first:
            return first
    return default_model


def to_openai_messages(messages: Any, system_prompt: Optional[str]) -> List[Dict[str, str]]:
    """Normalise ``messages`` (str ou liste ``{role,content}``) en messages OpenAI.

    ``BaseTool.sample_llm`` envoie soit une chaîne (+ ``system_prompt`` séparé), soit
    ``[{"role":"system","content":...},{"role":"user","content":...}]``. L'endpoint
    OpenAI-compatible gère nativement le rôle ``system`` (pas de pliage gemma requis).
    Garantit au moins un tour ``user`` (l'API refuse une conversation sans user).
    """
    out: List[Dict[str, str]] = []
    if system_prompt:
        out.append({"role": "system", "content": str(system_prompt)})
    if isinstance(messages, str):
        if messages:
            out.append({"role": "user", "content": messages})
    elif isinstance(messages, (list, tuple)):
        for m in messages:
            if isinstance(m, dict):
                role = str(m.get("role", "user"))
                content = m.get("content", m.get("text", ""))
                if content is None:
                    content = ""
                elif isinstance(content, (list, tuple)):
                    content = " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
                out.append({"role": role, "content": str(content)})
            else:
                out.append({"role": "user", "content": str(m)})
    elif messages:
        out.append({"role": "user", "content": str(messages)})
    if not any(m["role"] == "user" for m in out):
        out.append({"role": "user", "content": "(vide)"})
    return out


def _coerce(text: str, result_type: Any) -> Any:
    """Parse ``text`` (JSON, fences tolérées) en ``result_type`` ; sinon rend le texte."""
    try:
        from collegue.planner._parsing import json_from_text
    except Exception:  # noqa: BLE001 - parsing best-effort
        json_from_text = None
    data = json_from_text(text) if json_from_text else None
    if isinstance(data, dict) and hasattr(result_type, "model_validate"):
        try:
            return result_type.model_validate(data)
        except Exception:  # noqa: BLE001 - retombe sur le texte brut
            return text
    return text


class LocalSamplingContext:
    """``ctx`` de sampling offline routé vers un endpoint OpenAI-compatible.

    Construire le ctx n'ouvre aucune connexion : le client ``AsyncOpenAI`` est créé
    au premier ``sample`` (ou injecté en test via ``client=``).
    """

    def __init__(
        self,
        *,
        default_model: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        rate_limiter: Optional[PerModelRateLimiter] = None,
        default_max_tokens: int = DEFAULT_MAX_TOKENS,
        max_retries: int = 4,
        client: Any = None,
        subscription_enabled: bool = False,
        subscription_auth_dir: Optional[str] = None,
        sampler_image: str = "collegue-sandbox:latest",
        sampler_script: Optional[str] = None,
        sampler_timeout: float = 240.0,
        runner: Any = None,
        settings_obj: Any = None,
        subscription_models: Iterable[str] = (),
    ):
        self._settings = settings_obj  # présent ⇒ mode ROUTÉ (une route par rôle) ; absent ⇒ ctx statique (tests)
        self._clients: Dict[tuple, Any] = {}
        self._subscription_models = frozenset(str(m).strip().lower() for m in subscription_models if str(m).strip())
        self._default_model = default_model or ""
        self._api_key = api_key
        self._base_url = base_url
        self._limiter = rate_limiter
        self._default_max_tokens = int(default_max_tokens)
        self._max_retries = int(max_retries)
        self._client = client
        # Abonnement (Codex/ChatGPT) : échantillonné via le sandbox (le SDK subscription_login n'est PAS dans le
        # process principal). Choisi EXPLICITEMENT : par la route du rôle (``auth == subscription``) ou, pour un ctx
        # statique, par ``subscription_models`` — jamais déduit du nom du modèle.
        self._subscription_enabled = bool(subscription_enabled and subscription_auth_dir and sampler_script)
        self._subscription_auth_dir = subscription_auth_dir
        self._sampler_image = sampler_image
        self._sampler_script = sampler_script
        self._sampler_timeout = float(sampler_timeout)
        self._runner = runner  # injection de test : callable(argv, input) -> (rc, stdout, stderr)
        # Stubs ctx attendus par les outils (no-op async).
        self.info = self._noop
        self.debug = self._noop
        self.warning = self._noop
        self.error = self._noop
        self.report_progress = self._noop

    @classmethod
    def from_settings(cls, settings_obj: Any) -> "LocalSamplingContext":
        """Construit le ctx ROUTÉ depuis la config : la destination se résout à chaque appel, par rôle.

        Construire n'ouvre aucune connexion et n'exige aucune clé : une route incohérente ou sans credential lève
        :class:`LLMRoutingError` au ``sample`` correspondant, avant toute émission.

        **Pas de rate-limiter par défaut** : les réglages ``LLM_RATE_LIMIT_*`` bornent
        le middleware **serveur par-client** (autre couche) ; les y réutiliser
        throttlerait le moteur (15/min, 500/jour) bien en-dessous d'un run réel. Les
        429 sont gérés par le retry du SDK (``max_retries``) et le coût est borné par
        ``enforce_budget`` (C4). Un ``PerModelRateLimiter`` reste **injectable** au
        constructeur pour qui veut cadencer un quota free-tier.
        """
        # Abonnement : explicite par rôle (CODER_SUBSCRIPTION / LLM_AUTH_<ROLE>=subscription) ; le montage des
        # creds (``SANDBOX_SUBSCRIPTION_AUTH_DIR``) est requis au moment où une route d'abonnement est utilisée.
        auth = os.path.expanduser(str(getattr(settings_obj, "SANDBOX_SUBSCRIPTION_AUTH_DIR", "") or "").strip())
        # oh_sampler.py committé : collegue/executor/oh_sampler.py (ce fichier = collegue/core/llm/).
        sampler_script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "executor",
            "oh_sampler.py",
        )
        return cls(
            default_model="",
            subscription_enabled=bool(auth),
            subscription_auth_dir=auth or None,
            sampler_script=sampler_script,
            # Le sampler d'abonnement (reviewer/juge) tourne dans la MÊME image que le coder.
            sampler_image=str(
                getattr(settings_obj, "SANDBOX_IMAGE", "collegue-sandbox:latest") or "collegue-sandbox:latest"
            ),
            settings_obj=settings_obj,
        )

    async def _noop(self, *args: Any, **kwargs: Any) -> None:  # ctx.info/debug/...
        return None

    def _client_obj(self, route: Optional[LLMRoute] = None) -> Any:
        """Client OpenAI-compatible de la route (un par destination+identité) ; l'injecté prime (tests)."""
        if self._client is not None:
            return self._client
        from openai import AsyncOpenAI

        if route is None:
            self._client = AsyncOpenAI(api_key=self._api_key, base_url=self._base_url, max_retries=self._max_retries)
            return self._client
        key = route.cache_key()
        client = self._clients.get(key)
        if client is None:
            # ``base_url`` et ``api_key`` TOUJOURS explicites : le SDK relirait sinon OPENAI_BASE_URL/OPENAI_API_KEY
            # de l'environnement hôte, c'est-à-dire une autre destination ou une autre identité que la route.
            client = AsyncOpenAI(
                api_key=route.credential() or "local", base_url=route.endpoint, max_retries=self._max_retries
            )
            self._clients[key] = client
        return client

    def _route_for(self, model_preferences: Any) -> tuple:
        """``(route | None, modèle)`` : la route du rôle porté par les préférences (rôle DEFAULT sinon).

        Mode routé : la destination vient de la config, la préférence ne peut que la CONFIRMER. Ctx statique : le premier
        nom de modèle des préférences, sinon le modèle par défaut (comportement historique des tests).
        """
        role, models = parse_route_preferences(model_preferences)
        if self._settings is None:
            return None, (models[0] if models else self._default_model)
        route = resolve_route(role or LLMRole.DEFAULT, self._settings)
        if models and models[0] != route.model:
            raise LLMRoutingError(
                f"préférence de modèle {models[0]!r} contradictoire avec la route du rôle {route.role!r} "
                f"({route.model!r}) : refusée (elle changerait le modèle sans changer client, endpoint ni clé)"
            )
        return route, route.model

    async def sample(
        self,
        messages: Any = "",
        *,
        system_prompt: Optional[str] = None,
        result_type: Any = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        model_preferences: Any = None,
        **_ignored: Any,
    ) -> SampleResult:
        route, model = self._route_for(model_preferences)
        oai_messages = to_openai_messages(messages, system_prompt)
        if self._is_subscription(route, model):
            # Abonnement EXPLICITEMENT choisi pour ce rôle → sampler dans le sandbox (subscription_login).
            if not self._subscription_enabled:
                raise LLMRoutingError(
                    f"rôle {route.role if route else '?'} : authentification par abonnement sélectionnée mais "
                    "SANDBOX_SUBSCRIPTION_AUTH_DIR (creds montées) est absent"
                )
            text = await self._sample_subscription(model, oai_messages)
        else:
            # On RESPECTE le ``max_tokens`` explicite de l'appelant (parité avec le handler
            # serveur — sinon on inflerait ×4 les caps voulus, ex. 2000) ; seul un appel SANS
            # cap retombe sur ``_default_max_tokens`` (généreux : un modèle « raisonnant »
            # type gemma coupé trop tôt rend un contenu vide).
            eff_max = int(max_tokens) if max_tokens else self._default_max_tokens
            if self._limiter is not None:
                await self._limiter.acquire(model)
            text = await self._create(model, oai_messages, temperature, eff_max, route=route)
        res = SampleResult(text=text)
        if result_type is not None:
            res.result = _coerce(text, result_type)
        return res

    def _is_subscription(self, route: Optional[LLMRoute], model: str) -> bool:
        """Vrai si l'abonnement a été CHOISI : route ``subscription`` (config) ou modèle listé (ctx statique).

        Plus aucune heuristique « modèle non-Gemini ⇒ abonnement » : un modèle sans choix explicite part sur le
        transport HTTP de SA route, avec SA clé.
        """
        if route is not None:
            return route.uses_subscription
        return self._subscription_enabled and (model or "").strip().lower() in self._subscription_models

    def _validated_subscription_mounts(self) -> tuple:
        """Chemins CANONIQUES (auth RW, script RO) à monter, ou ``RuntimeError`` (fail-closed).

        Garantie commune des bind mounts (``collegue.sandbox.executor.git_control_exposure``) : ni le
        répertoire d'auth ni le script ne peuvent être, contenir ou se trouver dans un répertoire de
        contrôle Git, quelle que soit la profondeur ; une vérification impossible vaut refus. Le script
        monté en lecture seule doit en outre être un FICHIER régulier (un répertoire exposerait son
        arbre), et aucun chemin ne peut contenir ``:`` (injection d'options de ``-v``).
        """
        from collegue.sandbox.executor import git_control_exposure

        mounts = []
        for label, raw in (
            ("auth d'abonnement", self._subscription_auth_dir),
            ("script sampler", self._sampler_script),
        ):
            real = os.path.realpath(os.path.abspath(os.fspath(raw)))
            if ":" in real:
                raise RuntimeError(f"sampler abonnement : chemin {label} invalide (contient ':'): {real}")
            reason = git_control_exposure(raw)  # chemin BRUT : lien pendant, erreur de stat… ⇒ refus
            if reason is not None:
                raise RuntimeError(f"sampler abonnement : montage {label} refusé, contrôle Git exposé ({reason})")
            mounts.append(real)
        if os.path.lexists(mounts[1]) and not os.path.isfile(mounts[1]):
            raise RuntimeError(f"sampler abonnement : le script monté doit être un fichier régulier: {mounts[1]}")
        return mounts[0], mounts[1]

    async def _sample_subscription(self, model: str, oai_messages: List[Dict[str, str]]) -> str:
        """Échantillonne ``model`` via l'abonnement (Codex/ChatGPT) en lançant ``oh_sampler.py``
        dans le sandbox (le ``subscription_login`` du SDK n'est pas dans le process principal).

        Mêmes invariants que le coder : creds ``~/.openhands`` montées, ``LLM_SUBSCRIPTION=1`` ;
        coût 0 (abonnement). Le runner est injectable en test.

        Durcissement : ``--cap-drop ALL`` + ``--security-opt no-new-privileges`` (le conteneur
        ne lance que le SDK LLM, aucun code projet) ; il tourne sous l'utilisateur ``sandbox``
        (uid 1000) de l'image, le script est monté en lecture seule. ``--network host`` est
        requis (le bridge Docker stalle les transferts LLM — chemin réseau prouvé du harnais) ;
        le montage des creds est RW (rafraîchissement éventuel du jeton d'abonnement).
        """
        # Frontière Git : ce constructeur de ``docker run -v`` est hors ``DockerSandbox`` ; il
        # applique le même vérificateur de montages AVANT toute émission du runner.
        auth_mount, script_mount = self._validated_subscription_mounts()
        system_text = "\n\n".join(m["content"] for m in oai_messages if m["role"] == "system")
        user_text = "\n\n".join(m["content"] for m in oai_messages if m["role"] != "system")
        from collegue.core.llm.budget_guard import TRANSPORT_SUBSCRIPTION_SAMPLER, current_binding, guarded_call

        binding = current_binding()
        request: Dict[str, Any] = {"system": system_text, "prompt": user_text}
        if binding is not None and binding.ledger.snapshot(binding.scope_key).strict:
            # Budget strict : sortie bornée par la réservation, aucun retry interne du SDK (cf. oh_sampler).
            request.update(strict=True, max_output_tokens=SUBSCRIPTION_MAX_OUTPUT_ESTIMATE)
        payload = json.dumps(request)
        # Conteneur NOMMÉ + auto-limité (coreutils ``timeout`` : TERM puis KILL) : tuer le client
        # ``docker`` ne tue pas le conteneur, qui continuerait à dépenser si l'hôte meurt ou expire.
        container = f"collegue-smp-{uuid.uuid4().hex[:12]}"
        argv = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--name",
            container,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--network",
            "host",
            "-e",
            "HOME=/home/sandbox",
            "-e",
            f"LLM_MODEL={model}",
            "-e",
            "LLM_SUBSCRIPTION=1",
            "-e",
            "OPENHANDS_SUPPRESS_BANNER=1",
            "-v",
            f"{auth_mount}:/home/sandbox/.openhands",
            "-v",
            f"{script_mount}:/oh_sampler.py:ro",
            self._sampler_image,
            "timeout",
            "--signal=TERM",
            "--kill-after=15",
            str(max(1, int(self._sampler_timeout))),
            "python",
            "/oh_sampler.py",
        ]
        if binding is None:
            rc, out, err = await self._run_sampler(argv, payload, container)
        else:
            # Registre durable : réservation AVANT le lancement du conteneur (abonnement non facturé : 0 $,
            # mais des tokens), règlement avec l'enveloppe d'usage de confiance ; tout échec = usage inconnu.
            async def _emit():
                return await self._run_sampler(argv, payload, container)

            def _usage_of(result):
                r_rc, r_out, _ = result
                if r_rc != 0:
                    return None
                parsed = _parse_usage_envelope(r_out, model)
                return None if parsed is None else (parsed[0], parsed[1], parsed[2])

            rc, out, err = await guarded_call(
                _emit,
                binding=binding,
                model=model,
                messages=oai_messages,
                max_tokens=SUBSCRIPTION_MAX_OUTPUT_ESTIMATE,
                transport=TRANSPORT_SUBSCRIPTION_SAMPLER,
                billable=False,
                usage_of=_usage_of,
                max_attempts=1,
                # Le backend abonnement peut ignorer le plafond de sortie (non vérifiable hors ligne) : il ne
                # fournit donc AUCUNE garantie de plafond de tokens (refusé en strict sous MAX_TOKENS_BUDGET).
                output_bound_proven=False,
            )
        match = _SAMPLE_RE.search(out or "")
        if rc != 0 or not match:
            raise RuntimeError(f"sampler abonnement {model} en échec (rc={rc}) : {((err or out) or '')[:300]}")
        text = match.group(1)
        # Un verdict VIDE (stream + LLMResponse final tous deux vides, rc=0) serait mal
        # interprété en aval (reviewer/juge) → fail-closed plutôt que rendre "".
        if not text.strip():
            raise RuntimeError(f"sampler abonnement {model} : réponse vide")
        parsed = _parse_usage_envelope(out, model, strict=True)
        if parsed is not None:
            from collegue.monitoring.sampling_usage import record_usage

            record_usage(parsed[0], parsed[1], parsed[2])
        return text

    async def _run_sampler(self, argv: List[str], payload: str, container: Optional[str] = None):
        if self._runner is not None:
            return self._runner(argv, payload)
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                argv,
                input=payload,
                capture_output=True,
                text=True,
                timeout=self._sampler_timeout + SAMPLER_HOST_MARGIN,
            )
        except (subprocess.TimeoutExpired, asyncio.CancelledError):
            if container:
                _kill_container(container)
            raise
        return proc.returncode, proc.stdout, proc.stderr

    def _endpoint_url(self, client, route: Optional[LLMRoute] = None) -> Optional[str]:
        """Destination RÉELLE des appels HTTP (URL de base du client qui émet), pour justifier la borne de tokens.

        ``None`` = inconnue (client factice sans ``base_url``, ni URL de config) : la garde retombe alors sur le
        routage de la config et n'admet que les destinations hébergées connues. En mode routé, l'URL de la route
        remplace le repli sur une URL globale (un client injecté sans ``base_url`` ne rend pas la destination inconnue).
        """
        url = getattr(client, "base_url", None) or (route.endpoint if route is not None else None) or self._base_url
        return str(url) if url else None

    async def _create(
        self,
        model: str,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: int,
        *,
        route: Optional[LLMRoute] = None,
    ) -> str:
        from collegue.core.llm.budget_guard import TRANSPORT_HTTP, current_binding, guarded_call
        from collegue.monitoring.sampling_usage import record_usage

        binding = current_binding()
        client = self._client_obj(route)
        if binding is None:
            # Hors registre (serveur MCP sans projet) : garde historique C4 basée sur le
            # MetricsCollector — NON couverte par la garantie stricte (cf. w2-budget.md).
            from collegue.monitoring.metrics import enforce_budget

            enforce_budget()
            resp = await client.chat.completions.create(
                model=model, messages=messages, temperature=temperature, max_tokens=max_tokens
            )
        else:
            # Registre durable : une RÉSERVATION par tentative AVANT émission, retries compris. Le
            # SDK ne retente jamais en interne (max_retries=0) : c'est guarded_call qui boucle.
            once = client.with_options(max_retries=0) if hasattr(client, "with_options") else client

            async def _emit():
                return await once.chat.completions.create(
                    model=model, messages=messages, temperature=temperature, max_tokens=max_tokens
                )

            resp = await guarded_call(
                _emit,
                binding=binding,
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                transport=TRANSPORT_HTTP,
                usage_of=_openai_usage,
                max_attempts=self._max_retries + 1,
                endpoint=self._endpoint_url(client, route),
                provider=route.provider if route is not None else None,
            )
        usage = getattr(resp, "usage", None)
        if usage is not None:
            record_usage(
                getattr(usage, "prompt_tokens", 0) or 0,
                getattr(usage, "completion_tokens", 0) or 0,
                getattr(resp, "model", model) or model,
            )
        return _extract_text(resp)

    async def aclose(self) -> None:
        for client in list(self._clients.values()):
            close = getattr(client, "close", None) or getattr(client, "aclose", None)
            if close is not None:
                maybe = close()
                if asyncio.iscoroutine(maybe):
                    await maybe
        self._clients.clear()
        if self._client is not None:
            close = getattr(self._client, "close", None) or getattr(self._client, "aclose", None)
            if close is not None:
                maybe = close()
                if asyncio.iscoroutine(maybe):
                    await maybe
            self._client = None


SUBSCRIPTION_MAX_OUTPUT_ESTIMATE = 8192
SAMPLER_HOST_MARGIN = 45.0


def _kill_container(name: str) -> None:
    """Tue le conteneur NOMMÉ (best-effort) : tuer le client ``docker`` ne l'arrête pas."""
    try:
        subprocess.run(["docker", "kill", name], capture_output=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass


def _parse_usage_envelope(out: str, model: str, *, strict: bool = False):
    """``(prompt, completion, modèle)`` de l'enveloppe d'usage de confiance, ou ``None`` si absente/invalide.

    ``strict=True`` : une enveloppe PRÉSENTE mais non fiable lève ``RuntimeError`` (comportement historique).
    """
    match = _SAMPLE_USAGE_RE.search(out or "")
    if match is None:
        return None
    try:
        usage = json.loads(match.group(1))
        if not isinstance(usage, dict) or usage.get("billable") is not False:
            raise ValueError("enveloppe non fiable")
        prompt_tokens = int(usage["prompt_tokens"])
        completion_tokens = int(usage["completion_tokens"])
        usage_model = str(usage["model"] or model)
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ValueError("tokens négatifs")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if strict:
            raise RuntimeError(f"sampler abonnement {model} : enveloppe usage invalide") from exc
        return None
    return prompt_tokens, completion_tokens, usage_model


def _openai_usage(resp: Any):
    """``(prompt, completion, modèle)`` d'une réponse chat.completions, ou ``None`` si absent."""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    if not isinstance(prompt, int) or not isinstance(completion, int) or prompt < 0 or completion < 0:
        return None
    return prompt, completion, str(getattr(resp, "model", "") or "")


def _extract_text(resp: Any) -> str:
    """Texte du 1er choix d'une réponse chat.completions (OpenAI-compatible)."""
    choices = getattr(resp, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", "") if message is not None else ""
    return content or ""
