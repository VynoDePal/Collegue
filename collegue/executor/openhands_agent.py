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

import math
import time
from typing import List, Optional, Tuple

from collegue.core.llm.roles import LLMRole, resolve_role
from collegue.executor.agent import AgentResult, IssueSpec

# Point d'entrée headless d'OpenHands (module exécuté dans le conteneur sandbox).
OPENHANDS_ENTRYPOINT = "openhands.core.main"

# Politique retries/backoff par défaut du canal coder (#422) — alignée sur les
# settings ``CODER_LLM_*`` de ``collegue.config``.
DEFAULT_CODER_NUM_RETRIES = 8
DEFAULT_CODER_RETRY_MIN_WAIT = 8
DEFAULT_CODER_RETRY_MAX_WAIT = 90


def _int_setting(settings_obj, name: str, default: int) -> int:
    """Lit un entier de settings, robuste (None/str/invalide/négatif → défaut)."""
    try:
        value = int(getattr(settings_obj, name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


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
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        self._sandbox = sandbox
        self._role = role
        self._settings = settings_obj
        self._entrypoint = entrypoint
        self._python_bin = python_bin
        # Back-pressure du canal coder (#422) : horloge/sleep injectables (tests).
        self._clock = clock
        self._sleep = sleep
        self._last_launch: Optional[float] = None

    def _resolved_settings(self):
        if self._settings is not None:
            return self._settings
        from collegue.config import settings

        return settings

    def retry_policy(self) -> Tuple[int, int, int]:
        """``(num_retries, retry_min_wait, retry_max_wait)`` du canal coder (#422).

        Le moteur n'intercepte pas les appels LiteLLM faits DANS le sandbox : la
        seule régulation possible est de propager une politique de retries/backoff
        au worker OpenHands (qui la lit via l'environnement ``LLM_*``).
        """
        s = self._resolved_settings()
        return (
            _int_setting(s, "CODER_LLM_NUM_RETRIES", DEFAULT_CODER_NUM_RETRIES),
            _int_setting(s, "CODER_LLM_RETRY_MIN_WAIT", DEFAULT_CODER_RETRY_MIN_WAIT),
            _int_setting(s, "CODER_LLM_RETRY_MAX_WAIT", DEFAULT_CODER_RETRY_MAX_WAIT),
        )

    def min_interval_seconds(self) -> float:
        """Espacement minimal (s) entre deux lancements coder (0 = désactivé)."""
        try:
            value = float(getattr(self._resolved_settings(), "CODER_MIN_INTERVAL_SECONDS", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) and value > 0 else 0.0

    def _respect_min_interval(self) -> float:
        """Back-pressure start-to-start (#422) : attend s'il le faut, renvoie l'attente.

        Protège le quota fournisseur partagé contre des lancements coder en rafale
        (ex. retries de tâche pendant une fenêtre 503) sans toucher au sandbox.
        """
        interval = self.min_interval_seconds()
        if interval <= 0 or self._last_launch is None:
            return 0.0
        remaining = interval - (self._clock() - self._last_launch)
        if remaining > 0:
            self._sleep(remaining)
            return remaining
        return 0.0

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
        le texte de l'issue. La politique retries/backoff du canal coder (#422,
        non secrète) est propagée via l'environnement ``LLM_*`` d'OpenHands.
        """
        _provider, model = self.resolved_model()
        num_retries, retry_min, retry_max = self.retry_policy()
        return [
            "env",
            f"LLM_MODEL={model}",
            f"LLM_NUM_RETRIES={num_retries}",
            f"LLM_RETRY_MIN_WAIT={retry_min}",
            f"LLM_RETRY_MAX_WAIT={retry_max}",
            self._python_bin,
            "-m",
            self._entrypoint,
            "-t",
            issue.to_prompt(),
        ]

    def implement_issue(self, workspace: str, issue: IssueSpec) -> AgentResult:
        """Lance OpenHands dans le sandbox sur ``workspace`` pour ``issue``.

        Le diff autoritatif est capturé par l'exécuteur (E2) ; ici on ne renvoie que
        le statut (code de sortie du sandbox) et les logs. Les lancements sont
        espacés de ``CODER_MIN_INTERVAL_SECONDS`` (back-pressure, #422).
        """
        self._respect_min_interval()
        self._last_launch = self._clock()
        result = self._sandbox.run_command(self.build_command(issue), workspace)
        logs = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        return AgentResult(
            success=result.ok,
            logs=logs,
            summary=f"OpenHands sur l'issue #{issue.number}",
        )
