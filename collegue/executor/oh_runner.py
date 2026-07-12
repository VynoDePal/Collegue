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
import sys
import threading


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


def main() -> int:
    ap = argparse.ArgumentParser(prog="oh_runner")
    ap.add_argument("-t", "--task", required=True, help="Consigne donnée à l'agent.")
    ap.add_argument("--workspace", default=os.environ.get("OH_WORKSPACE", "/workspace"))
    ap.add_argument("--max-iterations", type=int, default=int(os.environ.get("OH_MAX_ITER", "40")))
    args = ap.parse_args()

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
            num_retries=int(os.environ.get("OH_NUM_RETRIES", "8")),
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
    for idx, model in enumerate(chain):
        try:
            print(f"oh_runner: modèle {model} (essai {idx + 1}/{len(chain)})", file=sys.stderr)
            run_with(model)
            print("OH_RUNNER_DONE")
            return 0
        except Exception as exc:  # noqa: BLE001 - on bascule sur le fallback
            last_exc = exc
            print(f"oh_runner: échec avec {model}: {exc}", file=sys.stderr)
    print(f"oh_runner: tous les modèles ont échoué ({last_exc})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
