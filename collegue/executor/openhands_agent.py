"""Adaptateur OpenHands du contrat :class:`CodeAgent` (E1, epic #362).

OpenHands est le codeur autonome retenu (décision §8). Il est **lourd** (runtime
propre, configuration LLM, Docker) : on ne l'importe donc **jamais** comme
dépendance Python de Collègue. Il tourne comme **processus** dans l'image du
:class:`~collegue.sandbox.executor.DockerSandbox` (C8). Conséquence : aucun
``import openhands`` ici — l'adaptateur ne fait que **construire la commande**
headless et la **lancer via le sandbox**.

Testabilité (cf. C8) : la **construction** de la commande et la **résolution du
modèle** (rôle ``CODER``) sont pures et testées sans OpenHands ; l'**exécution
réelle** est derrière le marqueur ``integration``.

Pré-requis d'une exécution réelle (différés, integration) :
- l'image sandbox doit embarquer OpenHands et ses dépendances ;
- OpenHands a besoin du réseau (appels LLM) : utiliser un ``DockerSandbox`` avec
  ``network`` ≠ ``none`` pour ce run précis ;
- la **clé API** (secrète) est injectée au niveau du sandbox via l'environnement
  (``-e LLM_API_KEY`` sans valeur dans l'argv, pour ne pas fuiter dans ``ps``),
  **pas** dans la commande construite ici. Seul le **nom du modèle** (non secret)
  est mis dans la commande.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from collegue.core.llm.roles import LLMRole, resolve_role
from collegue.executor.agent import AgentResult, IssueSpec

# Point d'entrée headless d'OpenHands (module exécuté dans le conteneur sandbox).
OPENHANDS_ENTRYPOINT = "openhands.core.main"


class OpenHandsAgent:
    """:class:`CodeAgent` pilotant OpenHands en headless dans le ``DockerSandbox``.

    ``sandbox`` est un objet exposant ``run_command(argv, workspace) -> SandboxResult``
    (duck-typing : un faux sandbox suffit en CI). ``role`` détermine le couple
    (provider, modèle) via :func:`resolve_role` (défaut : ``CODER``).
    """

    def __init__(
        self,
        sandbox,
        *,
        role: LLMRole = LLMRole.CODER,
        settings_obj: Optional[object] = None,
        entrypoint: str = OPENHANDS_ENTRYPOINT,
        python_bin: str = "python",
    ):
        self._sandbox = sandbox
        self._role = role
        self._settings = settings_obj
        self._entrypoint = entrypoint
        self._python_bin = python_bin

    def resolved_model(self) -> Tuple[str, str]:
        """Retourne ``(provider, model)`` pour le rôle de cet agent (défaut CODER).

        Délègue à :func:`resolve_role` : config dédiée au rôle si présente, sinon
        fallback sur le couple global ``LLM_PROVIDER`` / ``LLM_MODEL``.
        """
        return resolve_role(self._role, self._settings)

    def build_command(self, issue: IssueSpec) -> List[str]:
        """Construit l'argv OpenHands headless (pur, testable sans OpenHands).

        Le **nom du modèle** (non secret) est passé via ``env LLM_MODEL=...`` ; la
        clé API reste hors argv (injectée par le sandbox à l'exécution réelle). La
        consigne est ``issue.to_prompt()`` (déjà sanitizée contre l'injection).
        On passe un **argv** (pas de ``sh -c``) : aucun risque d'injection shell via
        le texte de l'issue.
        """
        _provider, model = self.resolved_model()
        return [
            "env",
            f"LLM_MODEL={model}",
            self._python_bin,
            "-m",
            self._entrypoint,
            "-t",
            issue.to_prompt(),
        ]

    def implement_issue(self, workspace: str, issue: IssueSpec) -> AgentResult:
        """Lance OpenHands dans le sandbox sur ``workspace`` pour ``issue``.

        Le diff autoritatif est capturé par l'exécuteur (E2) ; ici on ne renvoie que
        le statut (code de sortie du sandbox) et les logs.
        """
        result = self._sandbox.run_command(self.build_command(issue), workspace)
        logs = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        return AgentResult(
            success=result.ok,
            logs=logs,
            summary=f"OpenHands sur l'issue #{issue.number}",
        )
