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

import json
import math
import time
from typing import List, Optional, Tuple

from collegue.core.llm.roles import LLMRole, resolve_role
from collegue.executor.agent import AgentResult, IssueSpec

# Point d'entrée headless d'OpenHands (module exécuté dans le conteneur sandbox).
OPENHANDS_ENTRYPOINT = "openhands.core.main"

# Contrat d'usage LLM du canal coder (#441, précisé par #464) : le runner
# (entrypoint OpenHands ou tout wrapper SDK) imprime une ligne
#   [collegue-usage] {"prompt_tokens": 1200, "completion_tokens": 480, "cost_usd": 0.0021}
# Toutes les occurrences sont SOMMÉES par :func:`parse_usage_from_logs` —
# chaque ligne doit donc porter un DELTA (l'usage d'UN appel LLM), jamais un
# cumul (sommer des cumuls compterait double). Émission INCRÉMENTALE exigée
# (#464) : une ligne par appel, au fil de l'eau — un runner qui n'émet qu'un
# agrégat final dans un ``finally`` perd TOUT son usage quand le process est
# tué de l'extérieur (timeout sandbox → ``docker kill``, OOM) : c'est
# précisément la tentative la plus longue, donc la plus coûteuse, qui devient
# invisible du ledger (run FacNor v3 : 41 min de tokens hors budget). Résiduel
# assumé : un kill en plein appel perd au plus le DERNIER delta non émis (et ce
# cas n'émet pas d'événement ``usage_lost`` — il y a déjà de l'usage compté).
USAGE_MARKER = "[collegue-usage]"


def parse_usage_from_logs(logs: str) -> Tuple[int, int, float, bool]:
    """``(prompt_tokens, completion_tokens, cost_usd, cost_authoritative)`` (#441/#504).

    Best-effort : une ligne marquée mais illisible est ignorée (l'usage est une
    télémétrie, jamais une cause d'échec). ``(0, 0, 0.0, False)`` si rien n'est
    rapporté — le ledger distingue « zéro rapporté » de « gouvernance morte » par
    la présence des événements ``cost_observed``.

    ``cost_authoritative`` (#504) : ``True`` dès qu'une ligne porte ``billable:
    false`` — le coder SAIT que le run n'est pas facturé (abonnement Codex/ChatGPT,
    coût réel **0**). Le pilote ne doit alors PAS re-tarifer ce 0 au prix de secours
    (#484) : ce serait un coût FANTÔME (run v6 : ledger ~$2 sur un run abonnement
    pourtant gratuit). Absent / ``billable: true`` → un ``cost_usd`` nul reste un
    coût INCONNU (modèle non mappé litellm) → re-tarification #484 légitime.
    """
    prompt = completion = 0
    cost = 0.0
    cost_authoritative = False
    for line in (logs or "").splitlines():
        index = line.find(USAGE_MARKER)
        if index < 0:
            continue
        payload = line[index + len(USAGE_MARKER) :].strip()
        try:
            data = json.loads(payload)
            prompt += max(0, int(data.get("prompt_tokens") or 0))
            completion += max(0, int(data.get("completion_tokens") or 0))
            usd = float(data.get("cost_usd") or 0.0)
            if math.isfinite(usd) and usd > 0:
                cost += usd
            if data.get("billable") is False:
                cost_authoritative = True  # #504 : 0 AUTORITAIRE (run non facturé)
        except (ValueError, TypeError, AttributeError):
            continue
    return prompt, completion, cost, cost_authoritative


def estimate_cost_usd(prompt_tokens: int, completion_tokens: int, settings_obj=None) -> float:
    """Coût USD estimé via les prix configurés ``LLM_PRICE_*_PER_1M`` (#484).

    Filet quand litellm ne mappe pas le modèle : le runner émet ``cost_usd: 0``
    malgré des tokens — sans prix configurés on retourne 0.0 (l'appelant signale
    alors un coût INCONNU au lieu d'un zéro silencieux). Robuste : prix absent /
    non numérique / négatif / non fini → 0 (désactivé).
    """
    if settings_obj is None:
        from collegue.config import settings as settings_obj

    def _price(name: str) -> float:
        try:
            value = float(getattr(settings_obj, name, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) and value > 0 else 0.0

    prompt_price = _price("LLM_PRICE_PROMPT_PER_1M")
    completion_price = _price("LLM_PRICE_COMPLETION_PER_1M")
    if prompt_price <= 0 and completion_price <= 0:
        return 0.0
    try:
        total = max(0, int(prompt_tokens)) * prompt_price + max(0, int(completion_tokens)) * completion_price
    except OverflowError:
        # Ligne d'usage empoisonnée (int JSON géant) : estimer 0 plutôt que de
        # tuer le run — l'audit ne casse jamais le run.
        return 0.0
    return total / 1_000_000.0 if math.isfinite(total) else 0.0


def coder_pricing_resolvable(settings_obj=None) -> bool:
    """Vrai si le coût coder est résolvable (#502).

    Un prix de secours positif rend le coût calculable quand LiteLLM ne connaît
    pas le modèle. Un modèle dont la grille autoritaire fixe explicitement le
    coût à zéro (Gemma 4 Free Tier) est également résolvable sans prix de
    secours. Un modèle distant inconnu reste fail-closed. Même robustesse que
    :func:`estimate_cost_usd` (prix absent/non numérique/négatif/non fini → 0).
    """
    if settings_obj is None:
        from collegue.config import settings as settings_obj

    def _price(name: str) -> float:
        try:
            value = float(getattr(settings_obj, name, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) and value > 0 else 0.0

    return (
        _price("LLM_PRICE_PROMPT_PER_1M") > 0
        or _price("LLM_PRICE_COMPLETION_PER_1M") > 0
        or coder_pricing_is_explicitly_free(settings_obj)
    )


def coder_pricing_is_explicitly_free(settings_obj=None) -> bool:
    """Vrai si le couple provider/modèle effectif du rôle coder vaut 0 USD."""
    if settings_obj is None:
        from collegue.config import settings as settings_obj

    from collegue.monitoring.pricing import is_explicitly_free

    provider, model = resolve_role(LLMRole.CODER, settings_obj)
    return is_explicitly_free(model, provider=provider)


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
        prompt_tokens, completion_tokens, cost_usd, cost_authoritative = parse_usage_from_logs(logs)
        return AgentResult(
            success=result.ok,
            logs=logs,
            summary=f"OpenHands sur l'issue #{issue.number}",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            cost_authoritative=cost_authoritative,
        )
