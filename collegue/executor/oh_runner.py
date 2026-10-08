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
# ALLOCATION (plafonds USD/tokens, échéance, tarifs) réservée dans le registre durable avant le
# lancement ; ce runner contrôle chaque appel AVANT émission — retries et replis de modèle compris —
# et s'arrête quand l'allocation ou l'échéance est atteinte. Il n'importe que la stdlib : le
# script est copié seul dans l'image.
#
# Périmètre exact de la garantie : elle borne les appels du framework d'agent. Elle ne protège PAS
# d'une commande du workspace qui contacterait librement le fournisseur avec la clé facturable.

BUDGET_MARKER = "[collegue-budget]"
_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
_RETRYABLE_NAMES = frozenset({"RateLimitError", "ServiceUnavailableError", "InternalServerError", "Timeout"})
CHARS_PER_TOKEN_ESTIMATE = 2
PROMPT_OVERHEAD_TOKENS = 32
DEFAULT_MAX_OUTPUT_TOKENS = 8192


class AllocationExhausted(RuntimeError):
    """L'allocation budgétaire (ou l'échéance) interdit l'appel suivant : il n'est PAS émis."""


class BudgetGuard:
    """Contrôle chaque appel LLM avant émission contre l'allocation reçue de l'hôte."""

    def __init__(
        self,
        *,
        max_usd=None,
        max_tokens=None,
        deadline_epoch=None,
        price_in=None,
        price_out=None,
        billable=True,
        max_attempts=1,
        clock=None,
        sleep=None,
    ) -> None:
        self.max_usd = max_usd if max_usd and max_usd > 0 else None
        self.max_tokens = max_tokens if max_tokens and max_tokens > 0 else None
        self.deadline_epoch = deadline_epoch
        self.price_in = price_in
        self.price_out = price_out
        self.billable = billable
        self.max_attempts = max(1, int(max_attempts))
        # Résolus à l'appel (et non figés à la définition) : horloge/sommeil substituables.
        self._clock = clock or (lambda: time.time())
        self._sleep = sleep or (lambda seconds: time.sleep(seconds))
        self._llms: list = []
        self.refused = 0

    def validate(self) -> None:
        """Refuse de démarrer si le coût n'est pas bornable (plafond USD sans tarif sur un modèle facturé)."""
        if self.max_usd and self.billable and (self.price_in is None or self.price_out is None):
            raise AllocationExhausted("plafond USD sans tarif autoritaire : dépense non bornable, worker refusé")

    def register(self, llm) -> None:
        self._llms.append(llm)

    def spent(self) -> tuple:
        prompt = completion = 0
        cost = 0.0
        for llm in self._llms:
            metrics = getattr(llm, "metrics", None)
            usage = getattr(metrics, "accumulated_token_usage", None)
            prompt += int(getattr(usage, "prompt_tokens", 0) or 0)
            completion += int(getattr(usage, "completion_tokens", 0) or 0)
            cost += float(getattr(metrics, "accumulated_cost", 0.0) or 0.0)
        return prompt, completion, cost

    def precheck(self, llm, payload) -> None:
        """Lève :class:`AllocationExhausted` si l'appel ne tient plus dans l'allocation (rien n'est émis)."""
        if self.deadline_epoch is not None and self._clock() >= self.deadline_epoch:
            self.refused += 1
            raise AllocationExhausted("échéance de l'allocation atteinte")
        prompt_spent, completion_spent, reported_cost = self.spent()
        est_prompt = len(str(payload)) // CHARS_PER_TOKEN_ESTIMATE + PROMPT_OVERHEAD_TOKENS
        max_out = int(getattr(llm, "max_output_tokens", None) or DEFAULT_MAX_OUTPUT_TOKENS)
        if self.max_tokens is not None and prompt_spent + completion_spent + est_prompt + max_out > self.max_tokens:
            self.refused += 1
            raise AllocationExhausted(
                f"allocation de tokens épuisée ({prompt_spent + completion_spent} + {est_prompt + max_out} > {self.max_tokens})"
            )
        if self.max_usd is not None and self.billable:
            priced = prompt_spent * self.price_in + completion_spent * self.price_out
            projected = max(priced, reported_cost) + est_prompt * self.price_in + max_out * self.price_out
            if projected > self.max_usd:
                self.refused += 1
                raise AllocationExhausted(f"allocation USD épuisée ({projected:.6f} > {self.max_usd:.6f})")

    def install(self, llm) -> None:
        """Enveloppe les points d'émission du LLM. Échoue (fail-closed) si aucun n'est contrôlable."""
        names = [n for n in ("completion", "responses") if callable(getattr(llm, n, None))]
        if not names:
            raise AllocationExhausted("aucun point d'émission contrôlable sur le LLM : garde budgétaire indisponible")
        self.register(llm)
        for name in names:
            object.__setattr__(llm, name, self._wrap(llm, getattr(llm, name)))

    def _wrap(self, llm, original):
        guard = self

        def guarded(*args, **kwargs):
            for attempt in range(guard.max_attempts):
                guard.precheck(llm, (args, kwargs))  # avant CHAQUE tentative, retries compris
                try:
                    return original(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 - classé ci-dessous
                    if attempt + 1 < guard.max_attempts and _retryable(exc):
                        guard._sleep(min(30.0, 2.0**attempt))
                        continue
                    raise

        return guarded


def _retryable(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    return (isinstance(status, int) and status in _RETRYABLE_STATUS) or type(exc).__name__ in _RETRYABLE_NAMES


def _guard_from_args(args) -> "BudgetGuard | None":
    """Construit la garde depuis l'allocation hôte ; ``None`` si aucune allocation n'est fournie."""
    if not (args.budget_usd or args.budget_tokens or args.deadline_epoch):
        return None
    return BudgetGuard(
        max_usd=args.budget_usd,
        max_tokens=args.budget_tokens,
        deadline_epoch=args.deadline_epoch,
        price_in=args.price_in,
        price_out=args.price_out,
        billable=not args.no_billing,
        max_attempts=int(os.environ.get("OH_NUM_RETRIES", "8")) + 1,
    )


def main() -> int:
    ap = argparse.ArgumentParser(prog="oh_runner")
    ap.add_argument("-t", "--task", required=True, help="Consigne donnée à l'agent.")
    ap.add_argument("--workspace", default=os.environ.get("OH_WORKSPACE", "/workspace"))
    ap.add_argument("--max-iterations", type=int, default=int(os.environ.get("OH_MAX_ITER", "40")))
    # Allocation budgétaire remise par l'hôte (vague 2) : tous optionnels, absents = comportement historique.
    ap.add_argument("--budget-usd", type=float, default=None)
    ap.add_argument("--budget-tokens", type=int, default=None)
    ap.add_argument("--deadline-epoch", type=float, default=None)
    ap.add_argument("--price-in", type=float, default=None, help="USD par token d'entrée")
    ap.add_argument("--price-out", type=float, default=None, help="USD par token de sortie")
    ap.add_argument("--no-billing", action="store_true", help="abonnement : aucune facturation au token")
    args = ap.parse_args()

    guard = _guard_from_args(args)
    if guard is not None:
        try:
            guard.validate()
        except AllocationExhausted as exc:
            print(f"oh_runner: {exc}", file=sys.stderr)
            return 3
        # SIGTERM (échéance, `timeout` du conteneur) → arrêt PROPRE : les `finally` vident l'usage.
        signal.signal(signal.SIGTERM, lambda _signum, _frame: (_ for _ in ()).throw(SystemExit(143)))

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
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not subscription and not api_key:
        print("oh_runner: LLM_API_KEY/GEMINI_API_KEY manquante", file=sys.stderr)
        return 2

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
            llm = LLM(model=model, api_key=api_key, **common)
        if guard is not None:
            guard.install(llm)  # AllocationExhausted si aucun point d'émission n'est contrôlable
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

    chain = [primary, *[m for m in fallbacks if m != primary]]
    last_exc = None
    if guard is not None:
        print(f"{BUDGET_MARKER} armed {json.dumps({'usd': args.budget_usd, 'tokens': args.budget_tokens})}", flush=True)
    try:
        for idx, model in enumerate(chain):
            try:
                print(f"oh_runner: modèle {model} (essai {idx + 1}/{len(chain)})", file=sys.stderr)
                run_with(model)
                print("OH_RUNNER_DONE")
                return 0
            except AllocationExhausted as exc:
                # L'allocation est épuisée : basculer sur un autre modèle ne l'agrandit pas.
                print(f"oh_runner: allocation budgétaire atteinte ({exc})", file=sys.stderr)
                return 4
            except Exception as exc:  # noqa: BLE001 - on bascule sur le fallback
                last_exc = exc
                print(f"oh_runner: échec avec {model}: {exc}", file=sys.stderr)
        print(f"oh_runner: tous les modèles ont échoué ({last_exc})", file=sys.stderr)
        return 1
    finally:
        if guard is not None:
            # Marqueur FINAL : tout l'usage a été émis (les `finally` de run_with ont vidé les deltas).
            print(f"{BUDGET_MARKER} final", flush=True)


if __name__ == "__main__":
    sys.exit(main())
