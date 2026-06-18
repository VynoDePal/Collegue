"""Contexte de sampling **offline** (hors serveur MCP) pour piloter le moteur en CLI.

Le planner (``generate_spec``/``decompose``), le reviewer (``code_review`` via
``BaseTool``) et la boucle agentique attendent un objet ``ctx`` exposant une
coroutine ``sample(...)`` (sampling FastMCP). En CLI il n'y a **pas** de serveur
MCP, donc pas de ``ctx`` : ``run_project_from_settings`` recevait ``ctx=None`` et
les chemins LLM réels échouaient.

Ce module fournit un ``ctx`` qui appelle le LLM via un client **OpenAI-compatible**
(``AsyncOpenAI`` + ``base_url`` par provider), en réutilisant la MÊME résolution
provider→endpoint que le handler serveur (``resolve_openai_endpoint``) → **multi-
provider** (Gemini OpenAI-compat / OpenAI / lmstudio / ollama / unsloth), pas de
lock-in (brief §8). Au même chokepoint que le handler serveur, on applique la
**garde budget dur** (``enforce_budget``, C4) et la **capture d'usage**
(``record_usage``) — tous les ``ctx.sample()`` offline transitent ici.

Contrat reproduit fidèlement (lu dans le code) :
- ``sample(messages, *, system_prompt, result_type, temperature, max_tokens,
  model_preferences, **ignored)`` renvoie un objet avec ``.text`` (texte brut) et
  ``.result`` (instance ``result_type`` si parsable, sinon le texte) — exactement
  ce que lisent ``tools/base.py`` (``result.result if result_type else result.text``)
  et le planner ;
- ``model_preferences=[modèle]`` route vers ce modèle (rôle → modèle, C1/C2) ;
- stubs ``info/debug/warning/error/report_progress`` no-op attendus par les outils ;
- ``max_output_tokens`` généreux par défaut (quirk gemma : trop bas ⇒ contenu vide).

Le client ``AsyncOpenAI`` est construit **paresseusement** (au 1er ``sample``) : la
seule construction du ctx n'ouvre aucune connexion ni n'exige de clé (les tests qui
n'échantillonnent pas restent inertes).
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

DEFAULT_MAX_TOKENS = 8192


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
    ):
        self._default_model = default_model or ""
        self._api_key = api_key
        self._base_url = base_url
        self._limiter = rate_limiter
        self._default_max_tokens = int(default_max_tokens)
        self._max_retries = int(max_retries)
        self._client = client
        # Stubs ctx attendus par les outils (no-op async).
        self.info = self._noop
        self.debug = self._noop
        self.warning = self._noop
        self.error = self._noop
        self.report_progress = self._noop

    @classmethod
    def from_settings(cls, settings_obj: Any) -> "LocalSamplingContext":
        """Construit le ctx depuis la config (provider→endpoint).

        **Pas de rate-limiter par défaut** : les réglages ``LLM_RATE_LIMIT_*`` bornent
        le middleware **serveur par-client** (autre couche) ; les y réutiliser
        throttlerait le moteur (15/min, 500/jour) bien en-dessous d'un run réel. Les
        429 sont gérés par le retry du SDK (``max_retries``) et le coût est borné par
        ``enforce_budget`` (C4). Un ``PerModelRateLimiter`` reste **injectable** au
        constructeur pour qui veut cadencer un quota free-tier.
        """
        from collegue.core.llm.sampling_handler import resolve_openai_endpoint

        default_model, api_key, base_url = resolve_openai_endpoint(settings_obj)
        return cls(default_model=default_model, api_key=api_key, base_url=base_url)

    async def _noop(self, *args: Any, **kwargs: Any) -> None:  # ctx.info/debug/...
        return None

    def _client_obj(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self._api_key, base_url=self._base_url, max_retries=self._max_retries)
        return self._client

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
        model = _pick_model(model_preferences, self._default_model)
        oai_messages = to_openai_messages(messages, system_prompt)
        # On RESPECTE le ``max_tokens`` explicite de l'appelant (parité avec le handler
        # serveur — sinon on inflerait ×4 les caps voulus, ex. 2000) ; seul un appel SANS
        # cap retombe sur ``_default_max_tokens`` (généreux : un modèle « raisonnant »
        # type gemma coupé trop tôt rend un contenu vide).
        eff_max = int(max_tokens) if max_tokens else self._default_max_tokens
        if self._limiter is not None:
            await self._limiter.acquire(model)
        text = await self._create(model, oai_messages, temperature, eff_max)
        res = SampleResult(text=text)
        if result_type is not None:
            res.result = _coerce(text, result_type)
        return res

    async def _create(self, model: str, messages: List[Dict[str, str]], temperature: float, max_tokens: int) -> str:
        # Garde budget dur (C4) + capture d'usage AU MÊME chokepoint que le handler
        # serveur : tous les ctx.sample() offline passent ici. ``enforce_budget`` lève
        # ``BudgetExceeded`` (BaseException) si le plafond cumulé est atteint — on NE la
        # capture pas (auto-pause volontaire). No-op si plafonds désactivés.
        from collegue.monitoring.metrics import enforce_budget
        from collegue.monitoring.sampling_usage import record_usage

        enforce_budget()
        resp = await self._client_obj().chat.completions.create(
            model=model, messages=messages, temperature=temperature, max_tokens=max_tokens
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
        if self._client is not None:
            close = getattr(self._client, "close", None) or getattr(self._client, "aclose", None)
            if close is not None:
                maybe = close()
                if asyncio.iscoroutine(maybe):
                    await maybe
            self._client = None


def _extract_text(resp: Any) -> str:
    """Texte du 1er choix d'une réponse chat.completions (OpenAI-compatible)."""
    choices = getattr(resp, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", "") if message is not None else ""
    return content or ""
