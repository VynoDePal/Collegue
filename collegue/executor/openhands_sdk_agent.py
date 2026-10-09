"""Agent codeur OpenHands **SDK 1.7** — implémente le contrat ``CodeAgent`` via le sandbox.

OpenHands 1.7 est **SDK-first** : la CLI headless ``openhands.core.main`` (que cible
l'ancien adaptateur :mod:`collegue.executor.openhands_agent`) **n'existe plus**. Cet
agent la remplace pour le run réel : il lance :mod:`collegue.executor.oh_runner`
(``oh_runner.py``, baké dans l'image sandbox via ``docker/sandbox/Dockerfile.openhands``)
sur le workspace monté, et fait fonctionner le **coder par abonnement** (Codex/ChatGPT,
gpt-5.5, sans coût API) quand il est activé — le runner appelle ``subscription_login``.

La **destination du CODER** (fournisseur, modèle, endpoint, clé ou abonnement) est résolue UNE fois par
:func:`collegue.core.llm.roles.resolve_route` puis fournie par l'**environnement du sandbox** (``LLM_MODEL`` au format
LiteLLM du FOURNISSEUR du rôle, ``LLM_BASE_URL``, clé par référence ``env_secrets``, ``subscription_auth_dir``),
**jamais** dans l'argv. L'agent mute
le workspace ; la **capture autoritative du diff** revient à l'exécuteur Collègue (E2).
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import List, Optional

from collegue.core.llm.roles import LLMRole, LLMRoute, canonical_model, resolve_route
from collegue.executor.agent import AgentResult, IssueSpec
from collegue.executor.openhands_agent import parse_usage_from_logs, usage_status_from_run

# Chemin du runner headless baké dans l'image sandbox (cf. Dockerfile.openhands).
RUNNER_PATH = "/opt/oh_runner.py"
# Repli par défaut du runner en mode clé API (``OH_FALLBACK_MODELS`` non fourni par le sandbox).
RUNNER_DEFAULT_FALLBACK = "gemini/gemma-4-26b-a4b-it"


class OHSdkAgent:
    """:class:`CodeAgent` pilotant OpenHands 1.7 (SDK) en headless dans le ``DockerSandbox``.

    ``sandbox`` expose ``run_command(argv, workspace) -> SandboxResult`` (duck-typing).
    ``role`` (défaut ``CODER``) résout le modèle via :func:`resolve_role`.

    **Budget (vague 2)** : sous une allocation (``worker_budget.current_allocation()``), le runner
    reçoit plafonds, échéance et tarifs de chaque modèle de la chaîne et contrôle chaque appel AVANT
    émission, retries et replis compris ; le conteneur s'auto-limite à l'échéance (``timeout``) même si
    le client Docker meurt. ``"in-runner"`` borne les appels du framework d'agent, PAS une commande du
    workspace qui contacterait librement le fournisseur avec la même clé : avec une clé facturable
    ce n'est donc pas une barrière effective et le mode strict sous plafond le REFUSE. Abonnement (0 $/token) : accepté
    UNIQUEMENT sous un plafond USD sans plafond de tokens ; REFUSÉ sous un plafond strict de tokens (le backend ne
    garantit pas la sortie en amont et les commandes du workspace ont les credentials montés — la campagne 2 USD /
    250 000 tokens est donc refusée en strict). Mode ``advisory`` : disponible, sans garantie stricte.
    Voir ``docs/consolidation/w2-budget.md`` et ``docs/consolidation/w4-routing.md``.
    """

    def __init__(
        self,
        sandbox,
        *,
        settings_obj: Optional[object] = None,
        role: LLMRole = LLMRole.CODER,
        runner_path: str = RUNNER_PATH,
        max_iterations: int = 40,
        python_bin: str = "python",
        broker: Optional[object] = None,
    ):
        # ``broker`` (W5) : un ``BrokerRuntime``. Avec lui, TOUTE génération du worker passe par le courtier (socket Unix par
        # allocation, conteneur sans réseau, aucune clé fournisseur dans le sandbox) ; sans lui, comportement historique.
        self._broker = broker
        self._sandbox = sandbox
        self._settings = settings_obj
        self._role = role
        self._runner_path = runner_path
        self._max_iterations = int(max_iterations)
        self._python_bin = python_bin

    @property
    def transport(self) -> str:
        return "budget_broker" if self._broker is not None else "direct"

    @property
    def budget_enforcement(self) -> str:
        """``broker`` seulement si un courtier est attaché — et l'allocation en exige encore la PREUVE (jamais cette chaîne)."""
        return "broker" if self._broker is not None else "in-runner"

    def broker_transport_proof(self):
        """Preuve du transport sur le sandbox RÉEL de cet agent (réseau none, socket unique ro, aucune clé fournisseur)."""
        from collegue.broker.capability import TransportCheck, TransportProof, prove_worker_transport

        if self._broker is None:
            return TransportProof(
                "direct", (TransportCheck("broker_attached", False, "aucun courtier attaché à l'agent"),)
            )
        return prove_worker_transport(self._sandbox, provider_keys=self._broker.provider_keys())

    def persisted_remaining_seconds(self, binding) -> Optional[float]:
        """Secondes restantes de l'échéance GLOBALE PERSISTÉE du scope (courtier), ``None`` si l'horloge n'est pas ouverte / hors courtier."""
        if self._broker is None:
            return None
        return self._broker.service_for(binding.ledger).remaining_seconds(binding.scope_key)

    def runner_model_chain(self) -> List[str]:
        """Modèles tels que le RUNNER les nomme (LiteLLM). Courtier : ``openai/<identité>`` — format Chat Completions du relais ;
        la destination sémantique reste Google (``model_chain`` / route restent ``gemini``)."""
        if self._broker is None:
            return self.model_chain()
        route = self.route()
        return [f"openai/{route.model}", *[f"openai/{name}" for name in self.fallback_models()]]

    def route(self, *, require_credential: bool = False) -> LLMRoute:
        """Destination effective du codeur (fournisseur, modèle, endpoint, authentification) ; lève si incohérente."""
        return resolve_route(self._role, self._settings, require_credential=require_credential)

    def litellm_model(self) -> str:
        """Modèle du codeur au format LiteLLM, préfixé par le FOURNISSEUR DU RÔLE (``gemini/…`` ou ``openai/…``).

        Le préfixe vient de la route : un codeur ``openai/gpt-5.4`` n'est jamais renommé ``gemini/gpt-5.4``. En
        abonnement le modèle est nu (le backend ChatGPT n'a pas de préfixe LiteLLM). Une configuration qui se
        contredit lève :class:`~collegue.core.llm.roles.LLMRoutingError` avant tout lancement.
        """
        return self.route().litellm_model()

    def fallback_models(self) -> List[str]:
        """Replis du codeur (noms nus), toujours du MÊME fournisseur, endpoint et identité que le principal.

        Abonnement : ``CODER_SUBSCRIPTION_FALLBACK``. Sinon ``CODER_FALLBACK_MODELS`` ; à défaut, le repli historique
        ``gemma-4-26b-a4b-it`` pour un codeur Gemini et AUCUN repli pour un autre fournisseur — un repli ne change
        jamais de fournisseur ni de clé.
        """
        route = self.route()
        settings = self._settings
        if route.uses_subscription:
            raw = str(getattr(settings, "CODER_SUBSCRIPTION_FALLBACK", "gpt-5.4") or "")
        else:
            raw = str(getattr(settings, "CODER_FALLBACK_MODELS", "") or "")
            if not raw.strip() and route.provider == "gemini":
                raw = RUNNER_DEFAULT_FALLBACK
        names: List[str] = []
        for item in raw.split(","):
            name = item.strip()
            if not name:
                continue
            canonical = _canonical_fallback(route, name)
            if canonical != route.model and canonical not in names:
                names.append(canonical)
        return names

    def model_chain(self) -> List[str]:
        """Modèles que le runner peut utiliser, dans l'ordre (principal puis replis), tels que le runner les nomme.

        Reproduit EXACTEMENT ce que reçoit le runner (``runtime._coder_sandbox_env``) : ``LLM_MODEL`` puis
        ``OH_FALLBACK_MODELS``, tous de la route du codeur ; la tarification budgétaire s'appuie sur cette chaîne.
        """
        route = self.route()
        chain = [route.litellm_model()]
        for name in self.fallback_models():
            chain.append(replace(route, model=name).litellm_model())
        return chain

    def _budget_args(self, alloc) -> List[str]:
        """Arguments d'allocation du runner (vide sans allocation : comportement historique)."""
        if alloc is None or self._broker is not None:
            return []  # courtier : plafonds, échéance et usage sont appliqués par le service de confiance, hors du worker
        args: List[str] = []
        if alloc.max_micro_usd:
            args += ["--budget-usd", f"{alloc.max_usd:.6f}"]
        if alloc.max_tokens:
            args += ["--budget-tokens", str(alloc.max_tokens)]
        if alloc.deadline_epoch is not None:
            args += ["--deadline-epoch", f"{alloc.deadline_epoch:.3f}"]
        if alloc.prices:
            # Tarif de CHAQUE modèle de la chaîne (repli compris) : un repli n'hérite pas du prix du principal.
            table = {name: [float(price_in), float(price_out)] for name, price_in, price_out in alloc.prices}
            args += ["--prices", json.dumps(table, sort_keys=True)]
        if alloc.byte_bounded_models:
            args += ["--byte-bounded-models", ",".join(alloc.byte_bounded_models)]
        if alloc.strict:
            args.append("--strict")
        if not alloc.billable:
            args.append("--no-billing")
        if not args:
            # Allocation sans plafond ni échéance : armer quand même la garde (marqueurs d'usage complets).
            args += ["--budget-tokens", str(10**12)]
        return args

    def build_command(self, issue: IssueSpec) -> List[str]:
        """Argv lançant le runner headless OpenHands sur le workspace (pur, testable).

        Le **nom du modèle** (non secret) et l'éventuel ``LLM_SUBSCRIPTION`` sont injectés
        par le sandbox via l'environnement ; la clé API via ``env_passthrough``. La consigne
        est ``issue.to_prompt()`` (déjà sanitizée). On passe un **argv** (pas de ``sh -c``) :
        aucune injection shell.
        """
        from collegue.executor.worker_budget import current_allocation

        return [
            self._python_bin,
            self._runner_path,
            "--max-iterations",
            str(self._max_iterations),
            *self._budget_args(current_allocation()),
            "-t",
            issue.to_prompt(),
        ]

    def implement_issue(self, workspace: str, issue: IssueSpec) -> AgentResult:
        """Lance OpenHands (SDK) dans le sandbox sur ``workspace`` pour ``issue``.

        Le diff autoritatif est capturé par l'exécuteur (E2) ; ici on ne renvoie que le
        statut (code de sortie du sandbox) et les logs (bornés). Un runner qui crashe
        (ex. 503 LLM après retries) ⇒ ``success=False`` (fail-closed amont). L'usage
        ``[collegue-usage]`` émis par le runner remonte au ledger du run (#441/#464/#504 :
        ``cost_authoritative`` = abonnement non facturé → coût 0 à NE PAS re-tarifer #484).
        """
        from collegue.executor.worker_budget import current_allocation

        alloc = current_allocation()
        if self._broker is not None:
            return self._implement_via_broker(workspace, issue, alloc)
        run_kwargs = {}
        if alloc is not None and alloc.runtime_seconds is not None and _accepts_timeout(self._sandbox):
            # Échéance d'allocation : le conteneur s'auto-limite et l'hôte tue par nom au dépassement.
            run_kwargs["timeout"] = alloc.runtime_seconds
        result = self._sandbox.run_command(self.build_command(issue), workspace, **run_kwargs)
        logs = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        prompt_tokens, completion_tokens, cost_usd, cost_authoritative = parse_usage_from_logs(logs)
        usage_lines = bool(prompt_tokens or completion_tokens or cost_usd)
        status, reason = ("reported", "")
        if alloc is not None:
            status, reason = usage_status_from_run(
                logs, timed_out=bool(getattr(result, "timed_out", False)), usage_lines=usage_lines
            )
        return AgentResult(
            success=result.ok,
            logs=logs[-8000:],
            summary=f"OpenHands SDK sur l'issue #{issue.number}",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            cost_authoritative=cost_authoritative,
            usage_status=status,
            usage_reason=reason,
        )

    def _implement_via_broker(self, workspace: str, issue: IssueSpec, alloc) -> AgentResult:
        """Worker raccordé au courtier : session sur la réservation parent, sandbox sans réseau, usage = autorité du courtier."""
        from collegue.core.llm.budget_guard import current_binding
        from collegue.state.budget_ledger import REFUSED_UNBOUNDED, BudgetRefused

        binding = current_binding()
        if alloc is None or binding is None:
            # Le courtier n'accepte pas un contexte absent comme exemption : sans registre ni allocation, aucun lancement.
            raise BudgetRefused(
                REFUSED_UNBOUNDED, "mode courtier : aucune allocation ni registre lié au contexte — worker non lancé"
            )
        with self._broker.attach_worker(
            ledger=binding.ledger, allocation=alloc, role=self._role.value, sandbox=self._sandbox
        ) as attached:
            # Le délai du CONTENEUR est borné par l'échéance persistée : le processus de travail est ARRÊTÉ à cette échéance
            # (auto-limite coreutils dans le conteneur + kill par nom côté hôte), même s'il dort ou calcule sans appeler le
            # courtier. ``attached.timeout_seconds`` = min(allocation, échéance persistée − maintenant), calculé au lancement.
            run_kwargs = {}
            if attached.timeout_seconds is not None and _accepts_timeout(self._sandbox):
                run_kwargs["timeout"] = attached.timeout_seconds
            result = attached.sandbox.run_command(self.build_command(issue), workspace, **run_kwargs)
        summary = attached.summary
        logs = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        unknown = summary is None or summary.unknown
        return AgentResult(
            success=result.ok,
            logs=logs[-8000:],
            summary=f"OpenHands SDK (courtier) sur l'issue #{issue.number}",
            prompt_tokens=0 if summary is None else summary.prompt_tokens,
            completion_tokens=0 if summary is None else summary.completion_tokens,
            cost_usd=0.0,
            cost_authoritative=True,  # Gemma 4 : 0 $ attesté par identité exacte + endpoint officiel, pas par une clé
            usage_status="incomplete" if unknown else "reported",
            usage_reason=(summary.unknown_reason if summary is not None else None)
            or ("session non consolidée" if unknown else ""),
            usage_source="broker",
        )


def _accepts_timeout(sandbox) -> bool:
    """Le sandbox accepte-t-il ``run_command(..., timeout=...)`` ? (les doubles de test historiques non)."""
    import inspect

    try:
        params = inspect.signature(sandbox.run_command).parameters
    except (TypeError, ValueError, AttributeError):
        return False
    return "timeout" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _canonical_fallback(route: LLMRoute, name: str) -> str:
    """Nom nu d'un repli, validé contre le fournisseur de la route (un repli d'un autre fournisseur est refusé)."""
    return canonical_model(route.provider, name, where="repli du codeur")
