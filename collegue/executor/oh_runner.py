"""Runner headless OpenHands (SDK 1.7) — exécuté DANS le sandbox Docker.

OpenHands 1.7 est SDK-first (plus de CLI ``openhands.core.main``). Ce script est
l'entrypoint headless : il lit une tâche (``-t``), configure le LLM gemma via env
(``LLM_MODEL`` au format LiteLLM ``gemini/...`` + ``LLM_API_KEY``), construit
l'agent par défaut (tools terminal/éditeur/grep/glob) et fait tourner la
conversation sur le workspace monté (``/workspace``). L'agent édite les fichiers
en place ; l'exécuteur Collègue capture ensuite le diff autoritatif via git.

Sortie : ``OH_RUNNER_DONE`` (succès) ou un message d'erreur sur stderr + code != 0.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import threading
import time


class _UsageDeltaEmitter:
    """Émet les deltas d'usage d'un unique objet LLM.

    Les métriques OpenHands sont cumulatives *par objet* ``LLM``. Un fallback
    construit un nouvel objet dont les compteurs repartent de zéro : partager la
    dernière valeur du modèle précédent ferait donc disparaître le début de
    l'usage du fallback. Chaque tentative possède son propre emitter et sa propre
    base de compteurs.
    """

    def __init__(self, *, subscription: bool) -> None:
        self._subscription = subscription
        self._last_emitted = {"prompt": 0, "completion": 0, "cost": 0.0}

    def emit(self, llm) -> None:
        # Contrat moteur #441/#464 : lignes `[collegue-usage] {json}` en DELTAS
        # (parse_usage_from_logs SOMME les occurrences — émettre des cumuls
        # compterait double). Best-effort : télémétrie, jamais une cause d'échec.
        try:
            metrics = getattr(llm, "metrics", None)
            usage = getattr(metrics, "accumulated_token_usage", None)
            prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion = int(getattr(usage, "completion_tokens", 0) or 0)
            # En abonnement (Codex/ChatGPT), il n'y a AUCUNE facturation au token :
            # litellm estime quand même un prix API (fantôme). On force 0 pour que le
            # ledger $ du run reflète la réalité (les tokens, eux, restent comptés).
            cost = 0.0 if self._subscription else float(getattr(metrics, "accumulated_cost", 0.0) or 0.0)
            payload = {
                "prompt_tokens": max(0, prompt - self._last_emitted["prompt"]),
                "completion_tokens": max(0, completion - self._last_emitted["completion"]),
                "cost_usd": max(0.0, cost - self._last_emitted["cost"]),
                # #504 : en abonnement, le run n'est PAS facturé → cost_usd=0 est
                # AUTORITAIRE. Le flag dit au moteur de NE PAS re-tarifer ce 0 au prix
                # de secours #484 (sinon coût fantôme). Hors abonnement, billable=true
                # → un cost=0 reste « inconnu » (modèle non mappé) → #484 légitime.
                "billable": not self._subscription,
            }
            if payload["prompt_tokens"] or payload["completion_tokens"] or payload["cost_usd"]:
                print(f"[collegue-usage] {json.dumps(payload)}", flush=True)
                self._last_emitted.update(prompt=prompt, completion=completion, cost=cost)
        except Exception as exc:  # noqa: BLE001
            print(f"oh_runner: usage indisponible ({exc})", file=sys.stderr)


# ── garde budgétaire (vague 2) ────────────────────────────────────────────────────────────
#
# Le runner tourne DANS le conteneur : l'hôte ne voit pas ses appels. L'hôte lui remet donc une
# ALLOCATION (plafonds USD/tokens, échéance, tarifs PAR MODÈLE de la chaîne) réservée dans le registre durable
# avant le lancement ; ce runner contrôle chaque appel AVANT émission — retries et replis de modèle compris —
# et s'arrête quand l'allocation ou l'échéance est atteinte. Il n'importe que la stdlib : le script est copié
# seul dans l'image.
#
# Ce que la garde établit, et ce qu'elle n'établit PAS :
# - le prompt est borné par les OCTETS UTF-8 de la requête SÉRIALISÉE (messages, outils, schémas, structure et
#   séparateurs compris) : un tokenizer à repli octet ne produit jamais plus de tokens que d'octets. Valable
#   pour un couple (endpoint, famille) connu (``gemini/gemini…``, ``openai/gpt-…`` ou ``gpt-…`` en abonnement)
#   ou attesté par l'opérateur — pas pour un simple préfixe de nom ; sinon, ou pour une modalité non textuelle,
#   le modèle est écarté ;
# - la sortie est bornée par ``max_output_tokens`` du LLM (obligatoire) ; que le fournisseur l'honore est une
#   hypothèse, d'où le contrôle a posteriori (compteurs SDK) ;
# - une erreur HTTP ne prouve pas l'absence de facturation : seuls les rejets avant traitement (400/401/403/404/
#   405/413/415/422/429) et l'échec de connexion sont « sans consommation » ; tout autre échec rend l'usage
#   INCONNU (marqueur ``unknown``), sans retry ;
# - le marqueur ``final`` ne prouve pas que les compteurs SDK ont tout vu : une réponse sans usage compté ou une
#   borne démentie rend aussi l'usage inconnu ;
# - l'échéance est appliquée PENDANT un appel en vol (chien de garde) : le worker est arrêté, usage inconnu.
#
# Périmètre exact de la garantie : elle borne les appels du framework d'agent. Elle ne protège PAS
# d'une commande du workspace qui contacterait librement le fournisseur avec la clé facturable
# (l'hôte refuse donc le mode strict avec une clé facturable accessible, voir ``worker_budget``).

BUDGET_MARKER = "[collegue-budget]"
DEFAULT_MAX_OUTPUT_TOKENS = 8192
# Identités EXACTES (jamais des préfixes de nom) dont la famille de tokenizer est documentée — même liste que
# ``collegue.core.llm.budget_guard.HOSTED_KNOWN_MODELS`` (un test vérifie l'égalité : ce script est copié seul).
HOSTED_KNOWN_MODELS = {
    "gemini": frozenset(
        {
            "gemini-3.5-flash",
            "gemini-3-flash-preview",
            "gemini-3.1-flash-lite",
            "gemini-3.1-pro-preview",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.5-pro",
            "gemma-4-31b-it",
            "gemma-4-26b-a4b-it",
        }
    ),
    "openai": frozenset(
        {
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4.1",
            "gpt-4.1-mini",
            "gpt-4.1-nano",
            "gpt-5",
            "gpt-5-mini",
            "gpt-5-nano",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.5",
            "o1",
            "o3",
            "o3-mini",
            "o4-mini",
        }
    ),
}
_SNAPSHOT_SUFFIX = re.compile(r"-(?:\d{4}-\d{2}-\d{2}|\d{8})$")
# Destinations hébergées connues (même table que ``collegue.core.llm.roles.HOSTED_ENDPOINT_HOSTS`` ; ce script est copié seul).
_HOSTED_HOSTS = {"generativelanguage.googleapis.com": "gemini", "api.openai.com": "openai"}
PER_MESSAGE_FRAMING_TOKENS = 16
PER_TOOL_FRAMING_TOKENS = 64
REQUEST_FRAMING_TOKENS = 64
_NON_TEXT_PART_TYPES = frozenset(
    {
        "image_url",
        "image",
        "input_image",
        "input_audio",
        "audio",
        "audio_url",
        "file",
        "input_file",
        "video",
        "video_url",
    }
)
_PROVEN_REJECTED_STATUS = frozenset({400, 401, 403, 404, 405, 413, 415, 422, 429})
_RETRYABLE_REJECTED_STATUS = frozenset({429})
_LEGACY_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})  # advisory : comportement historique
_LEGACY_RETRYABLE_NAMES = frozenset({"RateLimitError", "ServiceUnavailableError", "InternalServerError", "Timeout"})
_CONNECT_ERROR_NAMES = frozenset({"ConnectError", "ConnectTimeout"})
RELEASE, UNKNOWN = "release", "unknown"


class AllocationExhausted(RuntimeError):
    """L'allocation budgétaire (ou l'échéance) interdit l'appel suivant : il n'est PAS émis."""


class UsageUnknown(AllocationExhausted):
    """La consommation d'un appel émis n'est pas établie : le worker s'arrête (usage inconnu, pas de retry)."""


class ModelNotBounded(RuntimeError):
    """Ce modèle de la chaîne ne peut pas être borné (tarif/tokenizer inconnu) : on passe au suivant."""


class UnboundablePayload(ValueError):
    """Modalité non textuelle : ses tokens ne se déduisent pas de ses octets."""


def _plain(value, _depth=0):
    """Réduit ``value`` à des types JSON ; refuse ce qui n'est pas du texte (image, audio, binaire…)."""
    if _depth > 64:
        raise UnboundablePayload("payload trop profond")
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else str(value)
    if isinstance(value, (bytes, bytearray)):
        raise UnboundablePayload("contenu binaire")
    if isinstance(value, dict):
        if str(value.get("type", "")).lower() in _NON_TEXT_PART_TYPES or any(
            str(key).lower() in _NON_TEXT_PART_TYPES for key in value
        ):
            raise UnboundablePayload(f"modalité non textuelle ({value.get('type')!r})")
        return {str(k): _plain(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item, _depth + 1) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _plain(dump(), _depth + 1)
    if hasattr(value, "__dict__"):
        return _plain(vars(value), _depth + 1)
    return str(value)


def _payload_bytes(value) -> int:
    """Octets UTF-8 de la requête SÉRIALISÉE (structure et séparateurs compris) : borne haute du texte découpé."""
    if value is None:
        return 0
    return len(json.dumps(_plain(value), ensure_ascii=False, separators=(", ", ": ")).encode("utf-8", "surrogatepass"))


def classify_failure(exc: BaseException, *, strict: bool = True):
    """``(RELEASE|UNKNOWN, retryable)`` : une réservation n'est libérée que si l'absence de conso est ÉTABLIE."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool) and getattr(exc, "response", None) is not None:
        if status in _PROVEN_REJECTED_STATUS:
            return RELEASE, status in _RETRYABLE_REJECTED_STATUS
        if not strict:
            return RELEASE, status in _LEGACY_RETRYABLE_STATUS
        return UNKNOWN, False
    link, seen = exc, 0
    while link is not None and seen < 6:
        if type(link).__name__ in _CONNECT_ERROR_NAMES:
            return RELEASE, True
        link, seen = (link.__cause__ or link.__context__), seen + 1
    if not strict:
        legacy_status = isinstance(status, int) and status in _LEGACY_RETRYABLE_STATUS
        return RELEASE, legacy_status or type(exc).__name__ in _LEGACY_RETRYABLE_NAMES
    return UNKNOWN, False


class BudgetGuard:
    """Contrôle chaque appel LLM avant émission contre l'allocation reçue de l'hôte."""

    def __init__(
        self,
        *,
        max_usd=None,
        max_tokens=None,
        deadline_epoch=None,
        prices=None,
        billable=True,
        strict=True,
        max_attempts=1,
        attested_models=(),
        subscription=False,
        endpoint=None,
        clock=None,
        sleep=None,
        exit_fn=None,
    ) -> None:
        self.max_usd = max_usd if max_usd and max_usd > 0 else None
        self.max_tokens = max_tokens if max_tokens and max_tokens > 0 else None
        self.deadline_epoch = deadline_epoch
        self.prices = {str(k): (float(v[0]), float(v[1])) for k, v in (prices or {}).items()}
        self.billable = billable
        self.strict = strict
        self.max_attempts = max(1, int(max_attempts))
        self.attested_models = tuple(m.strip().lower() for m in attested_models if m and m.strip())
        self.subscription = bool(subscription)
        # URL de base personnalisée (``LLM_BASE_URL``) : le préfixe LiteLLM ``openai/`` ne prouve plus alors le backend
        # OpenAI hébergé (passerelle, serveur local) — seule une attestation exacte de l'opérateur justifie la borne.
        self.endpoint_family = None
        self.custom_endpoint = bool(endpoint)
        if endpoint:
            from urllib.parse import urlparse

            self.endpoint_family = _HOSTED_HOSTS.get((urlparse(str(endpoint)).hostname or "").lower())
        # Résolus à l'appel (et non figés à la définition) : horloge/sommeil substituables.
        self._clock = clock or (lambda: time.time())
        self._sleep = sleep or (lambda seconds: time.sleep(seconds))
        self._exit = exit_fn or os._exit
        self._llms: list = []  # (llm, (price_in, price_out) | None)
        self._lock = threading.Lock()
        self._inflight = 0
        self.refused = 0
        self.tainted = None  # raison durable : l'usage de ce run n'est PAS établi

    # ── configuration par modèle ─────────────────────────────────────────────────────────
    def price_of(self, model):
        return self.prices.get(model)

    def _byte_bounded(self, model) -> bool:
        """« tokens ≤ octets » est justifié pour une IDENTITÉ exacte sur un endpoint connu, pas pour un préfixe.

        - ``gemini/<identité Gemini/Gemma connue>`` : endpoint hébergé Gemini ;
        - ``openai/<identité OpenAI connue>`` ou nom nu SOUS ``LLM_SUBSCRIPTION=1`` : backend OpenAI ;
        - sinon : uniquement une identité ATTESTÉE EXACTEMENT par l'opérateur (``--byte-bounded-models``).
        Un instantané daté (``-AAAA-MM-JJ``, ``-AAAAMMJJ``) d'une identité connue est admis.
        """
        name = model.strip().lower()
        provider, _, bare = name.rpartition("/")
        undated = _SNAPSHOT_SUFFIX.sub("", bare)
        if name in self.attested_models or bare in self.attested_models or undated in self.attested_models:
            return True
        family = (
            "gemini"
            if provider == "gemini"
            else "openai"
            if provider == "openai" or (not provider and self.subscription)
            else None
        )
        if self.custom_endpoint and family != self.endpoint_family:
            family = None  # destination personnalisée non hébergée reconnue : le nom ne prouve aucun tokenizer
        return family is not None and (bare in HOSTED_KNOWN_MODELS[family] or undated in HOSTED_KNOWN_MODELS[family])

    def needs_price(self) -> bool:
        return bool(self.max_usd and self.billable)

    def admit(self, model) -> None:
        """Lève :class:`ModelNotBounded` si ``model`` ne peut pas être borné (tarif / tokenizer inconnu)."""
        if self.needs_price() and self.price_of(model) is None:
            raise ModelNotBounded(f"modèle {model!r} sans tarif autoritaire sous plafond USD")
        if self.strict and (self.max_usd or self.max_tokens) and not self._byte_bounded(model):
            raise ModelNotBounded(f"famille de tokenizer inconnue pour {model!r} : borne en octets non justifiée")

    def validate(self, primary=None) -> None:
        """Refuse de démarrer si le coût du modèle principal n'est pas bornable."""
        if primary is not None:
            try:
                self.admit(primary)
            except ModelNotBounded as exc:
                raise AllocationExhausted(f"dépense non bornable, worker refusé : {exc}") from exc
        elif self.needs_price() and not self.prices:
            raise AllocationExhausted("plafond USD sans tarif autoritaire : dépense non bornable, worker refusé")

    def register(self, llm, model=None) -> None:
        self._llms.append((llm, self.price_of(model) if model is not None else None))

    def spent(self) -> tuple:
        prompt = completion = 0
        cost = priced = 0.0
        for llm, price in self._llms:
            metrics = getattr(llm, "metrics", None)
            usage = getattr(metrics, "accumulated_token_usage", None)
            p, c = int(getattr(usage, "prompt_tokens", 0) or 0), int(getattr(usage, "completion_tokens", 0) or 0)
            prompt += p
            completion += c
            cost += float(getattr(metrics, "accumulated_cost", 0.0) or 0.0)
            if price is not None:
                priced += p * price[0] + c * price[1]
        self._priced = priced
        return prompt, completion, cost

    # ── usage inconnu ────────────────────────────────────────────────────────────────────
    def taint(self, reason: str) -> None:
        """Marque l'usage comme NON établi (une fois) : le marqueur ``final`` ne sera pas émis."""
        with self._lock:
            if self.tainted is not None:
                return
            self.tainted = reason
        print(f"{BUDGET_MARKER} unknown {json.dumps({'reason': reason})}", flush=True)

    def _enter(self) -> None:
        with self._lock:
            self._inflight += 1

    def _leave(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)

    # ── échéance ─────────────────────────────────────────────────────────────────────────
    def deadline_reached(self) -> bool:
        return self.deadline_epoch is not None and self._clock() >= self.deadline_epoch

    def enforce_deadline(self) -> bool:
        """Appelé par le chien de garde : à l'échéance, un appel en vol est ANNULÉ (usage inconnu) et le process
        s'arrête ; sinon (aucun appel en vol) le worker reçoit SIGTERM. Renvoie vrai si l'échéance est atteinte."""
        if not self.deadline_reached():
            return False
        with self._lock:
            inflight = self._inflight
        if inflight:
            self.taint("échéance atteinte pendant un appel en vol : usage inconnu")
        else:
            print(f"{BUDGET_MARKER} deadline", flush=True)
        self._exit(5)
        return True

    def start_watchdog(self, period: float = 0.5) -> None:
        if self.deadline_epoch is None:
            return

        def watch() -> None:
            while not self.enforce_deadline():
                time.sleep(period)

        threading.Thread(target=watch, name="collegue-budget-deadline", daemon=True).start()

    # ── contrôle avant émission ──────────────────────────────────────────────────────────
    def precheck(self, llm, payload, model=None):
        """Lève :class:`AllocationExhausted` si l'appel ne tient plus dans l'allocation (rien n'est émis).

        Renvoie ``(prompt_bound, max_out)`` : la borne HAUTE du prompt et de la sortie, vérifiée après l'appel.
        """
        if self.deadline_reached():
            self.refused += 1
            raise AllocationExhausted("échéance de l'allocation atteinte")
        try:
            body = _payload_bytes(payload)
        except UnboundablePayload as exc:
            self.refused += 1
            raise AllocationExhausted(f"requête non bornable ({exc}) : refusée avant émission") from exc
        args, kwargs = payload if isinstance(payload, tuple) and len(payload) == 2 else ((), {})
        messages = kwargs.get("messages", args[0] if args else None)
        n_messages = len(messages) if isinstance(messages, (list, tuple)) else 1
        tools = kwargs.get("tools")
        n_tools = len(tools) if isinstance(tools, (list, tuple)) else (1 if tools else 0)
        prompt_bound = (
            body + n_messages * PER_MESSAGE_FRAMING_TOKENS + n_tools * PER_TOOL_FRAMING_TOKENS + REQUEST_FRAMING_TOKENS
        )
        max_out = getattr(llm, "max_output_tokens", None)
        if not isinstance(max_out, int) or isinstance(max_out, bool) or max_out <= 0:
            self.refused += 1
            raise AllocationExhausted("sortie non bornée (max_output_tokens absent du LLM) : refusée avant émission")
        prompt_spent, completion_spent, reported_cost = self.spent()
        if self.max_tokens is not None and prompt_spent + completion_spent + prompt_bound + max_out > self.max_tokens:
            self.refused += 1
            raise AllocationExhausted(
                f"allocation de tokens épuisée ({prompt_spent + completion_spent} + {prompt_bound + max_out} > {self.max_tokens})"
            )
        if self.max_usd is not None and self.billable:
            price = self._price_for(llm)
            if price is None:
                self.refused += 1
                raise AllocationExhausted("plafond USD sans tarif pour ce modèle : appel refusé")
            projected = max(self._priced, reported_cost) + prompt_bound * price[0] + max_out * price[1]
            if projected > self.max_usd:
                self.refused += 1
                raise AllocationExhausted(f"allocation USD épuisée ({projected:.6f} > {self.max_usd:.6f})")
        return prompt_bound, max_out

    def _price_for(self, llm):
        for known, price in self._llms:
            if known is llm:
                return price
        return None

    def install(self, llm, model=None) -> None:
        """Enveloppe les points d'émission du LLM. Échoue (fail-closed) si aucun n'est contrôlable."""
        names = [n for n in ("completion", "responses") if callable(getattr(llm, n, None))]
        if not names:
            raise AllocationExhausted("aucun point d'émission contrôlable sur le LLM : garde budgétaire indisponible")
        self.register(llm, model)
        for name in names:
            object.__setattr__(llm, name, self._wrap(llm, getattr(llm, name)))

    def _wrap(self, llm, original):
        guard = self

        def guarded(*args, **kwargs):
            for attempt in range(guard.max_attempts):
                if guard.tainted is not None and guard.strict:
                    raise UsageUnknown(f"usage inconnu ({guard.tainted}) : plus aucun appel n'est émis")
                prompt_bound, max_out = guard.precheck(llm, (args, kwargs))  # avant CHAQUE tentative
                before = guard.spent()
                guard._enter()
                try:
                    result = original(*args, **kwargs)
                except BaseException as exc:  # noqa: BLE001 - classé ci-dessous, jamais avalé
                    guard._leave()
                    if not isinstance(exc, Exception):
                        guard.taint(f"appel interrompu ({type(exc).__name__}) : usage inconnu")
                        raise
                    verdict, retryable = classify_failure(exc, strict=guard.strict)
                    if verdict == UNKNOWN:
                        reason = (
                            f"appel indéterminé ({type(exc).__name__}) : l'absence de facturation n'est pas établie"
                        )
                        guard.taint(reason)
                        raise UsageUnknown(reason) from exc
                    if retryable and attempt + 1 < guard.max_attempts:
                        delay = min(30.0, 2.0**attempt)
                        left = None if guard.deadline_epoch is None else guard.deadline_epoch - guard._clock()
                        if left is not None and delay >= left:
                            raise AllocationExhausted("échéance trop proche pour retenter") from exc
                        guard._sleep(delay)
                        continue
                    raise
                guard._leave()
                after = guard.spent()
                d_prompt, d_completion = after[0] - before[0], after[1] - before[1]
                if guard.strict and d_prompt + d_completion <= 0:
                    reason = "réponse reçue sans usage comptabilisé par le SDK : consommation inconnue"
                    guard.taint(reason)
                    raise UsageUnknown(reason)
                if guard.strict and (d_prompt > prompt_bound or d_completion > max_out):
                    reason = (
                        f"borne de tokens démentie par le fournisseur (prompt {d_prompt}>{prompt_bound} "
                        f"ou sortie {d_completion}>{max_out})"
                    )
                    guard.taint(reason)
                    raise UsageUnknown(reason)
                return result
            raise AllocationExhausted("aucune tentative émise")  # pragma: no cover

        return guarded


def _guard_from_args(args) -> "BudgetGuard | None":
    """Construit la garde depuis l'allocation hôte ; ``None`` si aucune allocation n'est fournie."""
    if not (args.budget_usd or args.budget_tokens or args.deadline_epoch):
        return None
    try:
        prices = json.loads(args.prices) if args.prices else {}
    except ValueError as exc:
        raise AllocationExhausted(f"table de tarifs illisible ({exc})") from exc
    return BudgetGuard(
        max_usd=args.budget_usd,
        max_tokens=args.budget_tokens,
        deadline_epoch=args.deadline_epoch,
        prices=prices,
        billable=not args.no_billing,
        strict=args.strict,
        max_attempts=int(os.environ.get("OH_NUM_RETRIES", "8")) + 1,
        attested_models=[m for m in (args.byte_bounded_models or "").split(",")],
        subscription=os.environ.get("LLM_SUBSCRIPTION", "") == "1",
        endpoint=os.environ.get("LLM_BASE_URL") or None,
    )


# Arguments du constructeur ``openhands.sdk.LLM`` que ce runner utilise. ``base_url`` est un champ de ``LLM`` (SDK
# 1.19.1, verrou ``locks/sandbox-openhands.txt``) ; le contrôle d'image ``scripts``/CI vérifie qu'ils sont tous dans
# ``LLM.model_fields`` (voir docs/consolidation/w4-routing.md).
LLM_CONSTRUCTOR_KWARGS = (
    "model",
    "api_key",
    "base_url",
    "service_id",
    "num_retries",
    "retry_min_wait",
    "retry_max_wait",
    "timeout",
    "max_output_tokens",
)


def resolve_credential(model: str, environ=None):
    """Clé API du worker, avec sa PROVENANCE : ``(clé | None, nom de la variable)``.

    ``LLM_API_KEY`` est la seule référence normale (injectée par l'hôte, par référence, hors argv). L'ancien nom
    ``GEMINI_API_KEY`` n'est lu que pour un modèle ``gemini/…`` : une clé Gemini ne part jamais vers un autre fournisseur.
    """
    environ = os.environ if environ is None else environ
    if environ.get("LLM_API_KEY"):
        return environ["LLM_API_KEY"], "LLM_API_KEY"
    if str(model).lower().startswith("gemini/") and environ.get("GEMINI_API_KEY"):
        return environ["GEMINI_API_KEY"], "GEMINI_API_KEY"
    return None, ""


def llm_kwargs(model: str, api_key, base_url, common: dict) -> dict:
    """Arguments exacts du ``LLM(...)`` en mode clé API : modèle (préfixe du fournisseur), clé, endpoint si nommé."""
    kwargs = dict(model=model, api_key=api_key)
    if base_url:
        kwargs["base_url"] = base_url
    kwargs.update(common)
    return kwargs


def main() -> int:
    ap = argparse.ArgumentParser(prog="oh_runner")
    ap.add_argument("-t", "--task", required=True, help="Consigne donnée à l'agent.")
    ap.add_argument("--workspace", default=os.environ.get("OH_WORKSPACE", "/workspace"))
    ap.add_argument("--max-iterations", type=int, default=int(os.environ.get("OH_MAX_ITER", "40")))
    # Allocation budgétaire remise par l'hôte (vague 2) : tous optionnels, absents = comportement historique.
    ap.add_argument("--budget-usd", type=float, default=None)
    ap.add_argument("--budget-tokens", type=int, default=None)
    ap.add_argument("--deadline-epoch", type=float, default=None)
    ap.add_argument(
        "--prices", default=None, help='JSON {"modèle": [usd/token entrée, usd/token sortie]} pour TOUTE la chaîne'
    )
    ap.add_argument(
        "--byte-bounded-models", default="", help="CSV de préfixes de modèles à tokenizer attesté par l'opérateur"
    )
    ap.add_argument("--strict", action="store_true", help="mode strict : échec indéterminé = usage inconnu, sans retry")
    ap.add_argument("--no-billing", action="store_true", help="abonnement : aucune facturation au token")
    args = ap.parse_args()

    try:
        guard = _guard_from_args(args)
    except AllocationExhausted as exc:
        print(f"oh_runner: {exc}", file=sys.stderr)
        return 3

    os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

    from openhands.sdk import LLM, Conversation
    from openhands.tools.preset.default import get_default_agent

    primary = os.environ.get("LLM_MODEL", "gemini/gemma-4-31b-it")
    # Fallback (CSV) : si le primaire échoue (ex. 503 persistants après retries), on
    # relance la conversation sur le MÊME workspace avec le modèle suivant (l'agent
    # repart de l'état courant des fichiers). gemma-4-31b-it étant flaky ("high
    # demand"), le défaut bascule sur le 26b.
    fallbacks = [
        m.strip() for m in os.environ.get("OH_FALLBACK_MODELS", "gemini/gemma-4-26b-a4b-it").split(",") if m.strip()
    ]
    # Mode abonnement (Codex via ChatGPT Plus/Pro) : pas de clé API, OpenHands
    # s'authentifie via subscription_login (creds en cache ~/.openhands/auth,
    # montées depuis l'hôte ; login fait en amont en device_code). Opt-in strict :
    # sans LLM_SUBSCRIPTION=1, le chemin clé-API (gemma) reste inchangé.
    subscription = os.environ.get("LLM_SUBSCRIPTION", "") == "1"
    api_key, _credential_name = resolve_credential(primary)
    base_url = os.environ.get("LLM_BASE_URL") or None
    local_endpoint = bool(base_url) and primary.lower().startswith("openai/")
    if not subscription and not api_key and not local_endpoint:
        print("oh_runner: LLM_API_KEY manquante", file=sys.stderr)
        return 2
    if not subscription and not api_key:
        api_key = "local"  # serveur local OpenAI-compatible sans clé : valeur fictive explicite, jamais une clé hôte

    chain = [primary, *[m for m in fallbacks if m != primary]]
    if guard is not None:
        try:
            guard.validate(primary)
        except AllocationExhausted as exc:
            print(f"oh_runner: {exc}", file=sys.stderr)
            return 3
        # SIGTERM (échéance, `timeout` du conteneur) → arrêt PROPRE : les `finally` vident l'usage.
        signal.signal(signal.SIGTERM, lambda _signum, _frame: (_ for _ in ()).throw(SystemExit(143)))
        guard.start_watchdog()

    def run_with(model: str) -> None:
        # Résilience 503 : on retente longtemps (le budget-temps global du run borne).
        common = dict(
            service_id="coder",
            # Sous allocation, le SDK ne retente JAMAIS en interne : la garde boucle et contrôle avant
            # chaque nouvelle émission.
            num_retries=0 if guard is not None else int(os.environ.get("OH_NUM_RETRIES", "8")),
            retry_min_wait=int(os.environ.get("OH_RETRY_MIN", "8")),
            retry_max_wait=int(os.environ.get("OH_RETRY_MAX", "90")),
            timeout=int(os.environ.get("OH_LLM_TIMEOUT", "300")),
        )
        if guard is not None:
            guard.admit(model)  # ModelNotBounded : ce modèle est écarté, le suivant de la chaîne est tenté
            # La sortie DOIT être bornée par un plafond réellement porté par le LLM (reasoning compris).
            common["max_output_tokens"] = int(os.environ.get("OH_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS))
        if subscription:
            # L'allow-list client d'OpenHands (OPENAI_CODEX_MODELS) est désynchronisée
            # du backend ChatGPT : elle liste des SKU *-codex que le serveur REFUSE et
            # EXCLUT gpt-5.5/gpt-5.4 que le compte sert réellement (vérifié au smoke v6).
            # On laisse le SERVEUR être l'autorité : on étend l'allow-list avec le
            # modèle demandé (le serveur valide de toute façon ; un modèle non servi
            # lève une BadRequestError explicite).
            try:
                import openhands.sdk.llm.auth.openai as _oa

                _oa.OPENAI_CODEX_MODELS = frozenset(set(_oa.OPENAI_CODEX_MODELS) | {model})
            except Exception:
                pass
            # open_browser=False : headless, on réutilise les creds en cache (le
            # login interactif device_code a déjà eu lieu hors-run).
            llm = LLM.subscription_login(vendor="openai", model=model, open_browser=False, **common)
        else:
            llm = LLM(**llm_kwargs(model, api_key, base_url, common))
        if guard is not None:
            guard.install(llm, model)  # AllocationExhausted si aucun point d'émission n'est contrôlable
        agent = get_default_agent(llm=llm, cli_mode=True)
        conv = Conversation(agent=agent, workspace=args.workspace, max_iteration_per_run=args.max_iterations)
        conv.send_message(args.task)
        # Les compteurs OpenHands appartiennent à l'objet LLM courant. Un emitter
        # neuf est indispensable au fallback, dont les compteurs repartent à zéro.
        usage_emitter = _UsageDeltaEmitter(subscription=subscription)
        # Contrat #464 : émission INCRÉMENTALE (deltas périodiques) — un agrégat
        # final unique dans un finally perd TOUT au docker-kill (timeout sandbox).
        stop = threading.Event()
        pump = threading.Thread(target=_pump_usage, args=(llm, stop, usage_emitter), daemon=True)
        pump.start()
        try:
            conv.run()
        finally:
            stop.set()
            pump.join(timeout=10)
            usage_emitter.emit(llm)  # flush final (delta restant)

    def _pump_usage(llm, stop: "threading.Event", emitter: _UsageDeltaEmitter) -> None:
        # Un delta toutes les 30 s : au pire, un docker-kill ne perd que la
        # dernière fenêtre (vs la tentative ENTIÈRE avant #464).
        while not stop.wait(30.0):
            emitter.emit(llm)

    last_exc = None
    if guard is not None:
        print(f"{BUDGET_MARKER} armed {json.dumps({'usd': args.budget_usd, 'tokens': args.budget_tokens})}", flush=True)
    try:
        for idx, model in enumerate(chain):
            try:
                print(f"oh_runner: modèle {model} (essai {idx + 1}/{len(chain)})", file=sys.stderr)
                run_with(model)
                if guard is not None and guard.tainted is not None:
                    # Le SDK a pu avaler l'arrêt (la conversation se termine « normalement ») : l'usage reste inconnu.
                    print(f"oh_runner: usage inconnu ({guard.tainted})", file=sys.stderr)
                    return 4
                print("OH_RUNNER_DONE")
                return 0
            except ModelNotBounded as exc:
                last_exc = exc
                print(f"oh_runner: modèle {model} écarté ({exc})", file=sys.stderr)
            except AllocationExhausted as exc:
                # L'allocation est épuisée : basculer sur un autre modèle ne l'agrandit pas.
                print(f"oh_runner: allocation budgétaire atteinte ({exc})", file=sys.stderr)
                return 4
            except Exception as exc:  # noqa: BLE001 - on bascule sur le fallback
                last_exc = exc
                print(f"oh_runner: échec avec {model}: {exc}", file=sys.stderr)
                if guard is not None and guard.tainted is not None and guard.strict:
                    return 4  # usage inconnu : aucun repli (il dépenserait sans compteur fiable)
        print(f"oh_runner: tous les modèles ont échoué ({last_exc})", file=sys.stderr)
        return 1
    finally:
        if guard is not None and guard.tainted is None:
            # Marqueur FINAL : tout l'usage a été émis ET compté (les `finally` de run_with ont vidé les deltas).
            # Un usage inconnu l'interdit : ``final`` ne prouve rien si un appel a pu échapper aux compteurs.
            print(f"{BUDGET_MARKER} final", flush=True)


if __name__ == "__main__":
    sys.exit(main())
