"""Agent codeur OpenHands **SDK 1.7** — implémente le contrat ``CodeAgent`` via le sandbox.

OpenHands 1.7 est **SDK-first** : la CLI headless ``openhands.core.main`` (que cible
l'ancien adaptateur :mod:`collegue.executor.openhands_agent`) **n'existe plus**. Cet
agent la remplace pour le run réel : il lance :mod:`collegue.executor.oh_runner`
(``oh_runner.py``, baké dans l'image sandbox via ``docker/sandbox/Dockerfile.openhands``)
sur le workspace monté, et fait fonctionner le **coder par abonnement** (Codex/ChatGPT,
gpt-5.5, sans coût API) quand il est activé — le runner appelle ``subscription_login``.

Le **modèle CODER** et la **clé API** (ou l'abonnement) sont fournis par
l'**environnement du sandbox** (injectés par ``DockerSandbox`` : ``env`` /
``env_passthrough`` / ``subscription_auth_dir``), **jamais** dans l'argv. L'agent mute
le workspace ; la **capture autoritative du diff** revient à l'exécuteur Collègue (E2).
"""

from __future__ import annotations

from typing import List, Optional

from collegue.core.llm.roles import LLMRole, resolve_role
from collegue.executor.agent import AgentResult, IssueSpec
from collegue.executor.openhands_agent import parse_usage_from_logs, usage_status_from_run

# Chemin du runner headless baké dans l'image sandbox (cf. Dockerfile.openhands).
RUNNER_PATH = "/opt/oh_runner.py"


class OHSdkAgent:
    """:class:`CodeAgent` pilotant OpenHands 1.7 (SDK) en headless dans le ``DockerSandbox``.

    ``sandbox`` expose ``run_command(argv, workspace) -> SandboxResult`` (duck-typing).
    ``role`` (défaut ``CODER``) résout le modèle via :func:`resolve_role`.

    **Budget (vague 2)** : sous une allocation (``worker_budget.current_allocation()``), le runner
    reçoit plafonds, échéance et tarifs et contrôle chaque appel AVANT émission, retries et replis
    compris ; le conteneur s'auto-limite à l'échéance (``timeout``) même si le client Docker meurt.
    Garantie bornée aux appels du framework d'agent, pas à une commande du workspace qui
    contacterait librement le fournisseur avec la clé (voir ``docs/consolidation/w2-budget.md``).
    """

    budget_enforcement = "in-runner"

    def __init__(
        self,
        sandbox,
        *,
        settings_obj: Optional[object] = None,
        role: LLMRole = LLMRole.CODER,
        runner_path: str = RUNNER_PATH,
        max_iterations: int = 40,
        python_bin: str = "python",
    ):
        self._sandbox = sandbox
        self._settings = settings_obj
        self._role = role
        self._runner_path = runner_path
        self._max_iterations = int(max_iterations)
        self._python_bin = python_bin

    def litellm_model(self) -> str:
        """Modèle CODER au format LiteLLM (``gemini/<modèle>``) pour OpenHands.

        Préfixe ``gemini/`` ajouté si absent (LiteLLM route le provider par préfixe) ;
        un modèle déjà préfixé (``provider/modèle``) est laissé tel quel. Sert au mode
        **clé API** ; en mode abonnement, le modèle (gpt-5.5 nu) vient de l'env du sandbox.
        """
        _provider, model = resolve_role(self._role, self._settings)
        if not model:
            model = "gemma-4-31b-it"
        return model if "/" in model else f"gemini/{model}"

    def _budget_args(self, alloc) -> List[str]:
        """Arguments d'allocation du runner (vide sans allocation : comportement historique)."""
        if alloc is None:
            return []
        args: List[str] = []
        if alloc.max_micro_usd:
            args += ["--budget-usd", f"{alloc.max_usd:.6f}"]
        if alloc.max_tokens:
            args += ["--budget-tokens", str(alloc.max_tokens)]
        if alloc.deadline_epoch is not None:
            args += ["--deadline-epoch", f"{alloc.deadline_epoch:.3f}"]
        if alloc.price_in is not None and alloc.price_out is not None:
            args += ["--price-in", repr(float(alloc.price_in)), "--price-out", repr(float(alloc.price_out))]
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


def _accepts_timeout(sandbox) -> bool:
    """Le sandbox accepte-t-il ``run_command(..., timeout=...)`` ? (les doubles de test historiques non)."""
    import inspect

    try:
        params = inspect.signature(sandbox.run_command).parameters
    except (TypeError, ValueError, AttributeError):
        return False
    return "timeout" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
