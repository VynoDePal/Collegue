"""Campagne métier multi-tâches W4 : rapport à états distincts, préflight sans dépense, vérification métier, invocation unique.

Ce module ne contient AUCUN appel de modèle. Il fournit, pour la campagne déterministe (``tests/w4_business_campaign.py``) comme
pour l'invocation réelle ponctuelle (``python -m collegue.pilot.w4_business``) :

* un **rapport** (machine JSON + humain) qui distingue cinq états d'étape — ``succeeded``, ``not_executed``, ``budget_stop``,
  ``failed``, ``incomplete_validation`` — et dont le verdict n'est ``validated`` que si TOUTES les étapes requises ont réussi ;
* un **préflight** sans dépense : environnement borné (2 USD / 250 000 tokens / 900 s), identité du dépôt fixture, capacité du
  transport de worker à tenir simultanément ces plafonds (règles de ``executor.worker_budget``, jamais dupliquées), protections
  W3 applicables à la base éphémère (règles de ``pilot.merge_policy``, jamais dupliquées), environnement des oracles ;
* une **vérification métier** du livrable (base réellement vierge, migration, création/lecture d'un audit, redémarrage, PDF lu par
  un vrai lecteur) indépendante de ce que l'agent a écrit comme tests ;
* l'**invocation unique** : le lancement payant n'a lieu que si tout le préflight a réussi, jamais deux fois pour la même
  campagne, jamais depuis une récurrence.

Un préflight qui ne peut pas ÉTABLIR un plafond ou une protection termine ``incomplete_validation`` AVANT toute émission : ce n'est
ni un succès ni une preuve du parcours avec modèles réels.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from collegue.sandbox import DEFAULT_SANDBOX_IMAGE

# ── états d'étape et verdict ──────────────────────────────────────────────────────────────────────────────────────────────

STEP_SUCCEEDED = "succeeded"  # l'étape a tourné et son contrat est établi
STEP_NOT_EXECUTED = "not_executed"  # l'étape n'a pas tourné (arrêt amont) : jamais lue comme un succès
STEP_BUDGET_STOP = "budget_stop"  # arrêt par budget/échéance (registre W2) — pas un échec fonctionnel
STEP_FAILED = "failed"  # contrat violé (assertion, état durable incohérent, exception)
STEP_INCOMPLETE = "incomplete_validation"  # la preuve n'a pas pu être ÉTABLIE (prérequis manquant, transport refusé)
STEP_STATES = (STEP_SUCCEEDED, STEP_NOT_EXECUTED, STEP_BUDGET_STOP, STEP_FAILED, STEP_INCOMPLETE)

VERDICT_VALIDATED = "validated"
VERDICT_FAILED = "failed"
VERDICT_BUDGET_STOP = "budget_stop"
VERDICT_INCOMPLETE = "incomplete_validation"
EXIT_CODES = {VERDICT_VALIDATED: 0, VERDICT_FAILED: 1, VERDICT_INCOMPLETE: 3, VERDICT_BUDGET_STOP: 4}

REPORT_SCHEMA = "w4-business-report/1"
REDACTION = "[REDACTED]"
SECRET_ENV_NAMES = ("GITHUB_TOKEN", "LLM_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


class BudgetStop(Exception):
    """Arrêt par budget ou échéance : l'étape se termine ``budget_stop``."""


class IncompleteValidation(Exception):
    """La preuve ne peut pas être établie (prérequis, transport, protection) : l'étape se termine ``incomplete_validation``."""


@dataclass
class Step:
    id: str
    title: str
    state: str = STEP_NOT_EXECUTED
    required: bool = True
    detail: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)


class CampaignReport:
    """Rapport d'une campagne : étapes DÉCLARÉES d'avance (toute étape non jouée reste ``not_executed``) et faits."""

    def __init__(self, kind: str, campaign_id: str, *, secrets: Sequence[str] = ()):
        self.kind = kind
        self.campaign_id = campaign_id
        self.steps: List[Step] = []
        self.facts: Dict[str, Any] = {}
        self.halted = False
        self._secrets = tuple(s for s in secrets if s and len(s) >= 4)

    # ── déclaration et exécution ────────────────────────────────────────────────────────────────────────────────────────
    def declare(self, step_id: str, title: str, *, required: bool = True) -> Step:
        if any(s.id == step_id for s in self.steps):
            raise ValueError(f"étape déjà déclarée: {step_id}")
        step = Step(step_id, title, required=required)
        self.steps.append(step)
        return step

    def step(self, step_id: str) -> Step:
        for item in self.steps:
            if item.id == step_id:
                return item
        raise KeyError(step_id)

    def run(self, step_id: str, fn: Callable[[Step], Any]) -> bool:
        """Joue ``fn`` pour l'étape ``step_id`` sauf si une étape précédente a stoppé la campagne.

        Retourne ``True`` si l'étape a réussi. Toute autre issue arrête la suite (``halted``) : les étapes restantes demeurent
        ``not_executed``."""
        step = self.step(step_id)
        if self.halted:
            return False
        try:
            fn(step)
            if step.state == STEP_NOT_EXECUTED:
                step.state = STEP_SUCCEEDED
        except BudgetStop as exc:
            step.state, step.detail = STEP_BUDGET_STOP, str(exc)
        except IncompleteValidation as exc:
            step.state, step.detail = STEP_INCOMPLETE, str(exc)
        except Exception as exc:  # noqa: BLE001 - le contrat de l'étape est violé : l'échec est consigné, jamais masqué
            step.state, step.detail = STEP_FAILED, f"{type(exc).__name__}: {exc}"
        if step.state != STEP_SUCCEEDED:
            self.halted = True
        return step.state == STEP_SUCCEEDED

    async def arun(self, step_id: str, fn: Callable[[Step], Any]) -> bool:
        """Variante asynchrone de :meth:`run` (même sémantique d'états et d'arrêt)."""
        step = self.step(step_id)
        if self.halted:
            return False
        try:
            await fn(step)
            if step.state == STEP_NOT_EXECUTED:
                step.state = STEP_SUCCEEDED
        except BudgetStop as exc:
            step.state, step.detail = STEP_BUDGET_STOP, str(exc)
        except IncompleteValidation as exc:
            step.state, step.detail = STEP_INCOMPLETE, str(exc)
        except Exception as exc:  # noqa: BLE001
            step.state, step.detail = STEP_FAILED, f"{type(exc).__name__}: {exc}"
        if step.state != STEP_SUCCEEDED:
            self.halted = True
        return step.state == STEP_SUCCEEDED

    # ── verdict ──────────────────────────────────────────────────────────────────────────────────────────────────────────
    def verdict(self) -> str:
        required = [s for s in self.steps if s.required]
        if any(s.state == STEP_FAILED for s in required):
            return VERDICT_FAILED
        if any(s.state == STEP_BUDGET_STOP for s in required):
            return VERDICT_BUDGET_STOP
        if all(s.state == STEP_SUCCEEDED for s in required) and required:
            return VERDICT_VALIDATED
        return VERDICT_INCOMPLETE  # étape requise non jouée / validation incomplète : jamais « validé »

    def exit_code(self) -> int:
        return EXIT_CODES[self.verdict()]

    # ── rendu ────────────────────────────────────────────────────────────────────────────────────────────────────────────
    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTION)
        return text

    def to_machine(self) -> Dict[str, Any]:
        counts = {state: sum(1 for s in self.steps if s.state == state) for state in STEP_STATES}
        payload = {
            "schema": REPORT_SCHEMA,
            "kind": self.kind,
            "campaign_id": self.campaign_id,
            "verdict": self.verdict(),
            "exit_code": self.exit_code(),
            "counts": counts,
            "steps": [asdict(s) for s in self.steps],
            "facts": self.facts,
        }
        return json.loads(self._redact(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)))

    def to_json(self) -> str:
        return json.dumps(self.to_machine(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def to_human(self) -> str:
        marks = {
            STEP_SUCCEEDED: "OK     ",
            STEP_NOT_EXECUTED: "NON JOUÉE",
            STEP_BUDGET_STOP: "ARRÊT BUDGET",
            STEP_FAILED: "ÉCHEC  ",
            STEP_INCOMPLETE: "VALIDATION INCOMPLÈTE",
        }
        lines = [f"Campagne W4 « {self.campaign_id} » ({self.kind}) — verdict : {self.verdict()}"]
        for item in self.steps:
            optional = "" if item.required else " (facultative)"
            lines.append(f"  [{marks[item.state]}] {item.id} — {item.title}{optional}")
            if item.detail:
                lines.append(f"      {item.detail}")
        counts = {state: sum(1 for s in self.steps if s.state == state) for state in STEP_STATES}
        lines.append("  Bilan : " + ", ".join(f"{state}={counts[state]}" for state in STEP_STATES))
        return self._redact("\n".join(lines)) + "\n"


# ── bornes globales et environnement de la campagne ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CampaignBounds:
    """Enveloppe GLOBALE de la campagne réelle (planification + exécution + retries internes confondus)."""

    max_cost_usd: float = 2.0
    max_tokens: int = 250_000
    max_seconds: int = 900


CAMPAIGN_BOUNDS = CampaignBounds()

FIXTURE_REPOSITORY = "VynoDePal/collegue-e2e-fixture"
FIXTURE_REPOSITORY_ID = 1298596453
FIXTURE_ROOT_BRANCH = "main"
FIXTURE_SEED_SHA = "8e3691d8e4f311e00d620c9c2ca2d9edbd8b136a"
FIXTURE_MARKER_PATH = ".collegue-nightly-fixture"
FIXTURE_SENTINEL = "COLLEGUE_NIGHTLY_FIXTURE_V1\n"
FIXTURE_SEED_FILES = (
    ".collegue-nightly-fixture",
    ".gitignore",
    "README.md",
    "app/__init__.py",
    "app/main.py",
    "requirements.txt",
    "tests/__init__.py",
    "tests/test_app.py",
)
# Préfixe de la base éphémère : la règle de protection du dépôt fixture (à créer par son propriétaire) doit viser ce motif.
BASE_BRANCH_PREFIX = "collegue-business"
LAUNCH_CONFIRMATION = "LANCER-UNE-FOIS-2USD-250000TOKENS-900S"

#: Réglages que l'invocation réelle impose (toute dérive est refusée au préflight).
CAMPAIGN_SETTINGS: Dict[str, str] = {
    "BUDGET_MODE": "strict",
    "BUDGET_EXHAUSTED_ACTION": "pause",
    "BUILD_AUTO_MERGE": "true",
    "AUTO_MERGE_ENABLED": "false",
    "DEPS_REQUIRE_MERGED": "true",
    "STRICT_MAX_INFLIGHT_PRS": "1",
    "TASK_MAX_ATTEMPTS": "1",
    "GATE_ACCEPTANCE_TESTS": "true",
    "REQUIRE_COST_PRICING": "true",
}

BUSINESS_PROBLEM = (
    "Sur le dépôt fixture Python existant (FastAPI), construis en TROIS tâches dépendantes une petite application d'audit "
    "persistée dans SQLite avec Alembic, sans toucher à `/health` : "
    "(1) persistance et migration : `alembic.ini`, `migrations/env.py` et une révision `0001` qui créent les tables "
    "`audits` (id, title, auditor, summary) et `findings` (id, audit_id, position, severity, description) sur une base "
    "VIERGE désignée par la variable d'environnement `DATABASE_URL` ; "
    "(2) `POST /audits` (201, corps JSON title, auditor, summary, findings[{severity, description}]) puis `GET /audits/{id}` "
    "(200, mêmes données ; 404 si inconnu), données lues dans la base ; "
    "(3) `GET /audits/{id}/export.pdf` (application/pdf) dont le TEXTE contient le titre, l'auditeur et chaque constat de cet "
    "audit, lu depuis la base. Ajoute les tests de chaque tâche et les dépendances nécessaires à `requirements.txt`."
)


def campaign_environment(campaign_id: str, home: str, bounds: CampaignBounds = CAMPAIGN_BOUNDS) -> Dict[str, str]:
    """Variables d'environnement de l'invocation réelle : bornes globales et registre durable propre à la campagne."""
    state_db = os.path.join(home, f"{campaign_id}.sqlite3")
    return {
        **CAMPAIGN_SETTINGS,
        "MAX_COST_USD": f"{bounds.max_cost_usd:g}",
        "MAX_TOKENS_BUDGET": str(bounds.max_tokens),
        "COLLEGUE_RUN_DEADLINE_SECONDS": str(bounds.max_seconds),
        "COLLEGUE_HOME": home,
        "STATE_DATABASE_URL": f"sqlite:///{state_db}",
    }


def _num(env: Mapping[str, str], name: str) -> Optional[float]:
    try:
        value = float(str(env.get(name, "")).strip())
    except ValueError:
        return None
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def validate_campaign_environment(env: Mapping[str, str], bounds: CampaignBounds = CAMPAIGN_BOUNDS) -> List[str]:
    """Écarts entre ``env`` et l'enveloppe de la campagne (liste vide = conforme). Jamais de correction silencieuse."""
    problems: List[str] = []
    for name, expected in CAMPAIGN_SETTINGS.items():
        if str(env.get(name, "")).strip().lower() != expected:
            problems.append(f"{name} doit valoir {expected!r} (vu {env.get(name)!r})")
    caps = (
        ("MAX_COST_USD", bounds.max_cost_usd),
        ("MAX_TOKENS_BUDGET", float(bounds.max_tokens)),
        ("COLLEGUE_RUN_DEADLINE_SECONDS", float(bounds.max_seconds)),
    )
    for name, limit in caps:
        value = _num(env, name)
        if value is None or value <= 0:
            problems.append(f"{name} absent ou invalide (plafond global obligatoire)")
        elif value > limit:
            problems.append(f"{name}={value:g} dépasse l'enveloppe globale ({limit:g})")
    home = str(env.get("COLLEGUE_HOME", "") or "")
    if not os.path.isabs(home):
        problems.append("COLLEGUE_HOME doit être un chemin absolu (registre durable partagé entre commandes)")
    url = str(env.get("STATE_DATABASE_URL", "") or "")
    if not url.startswith("sqlite:////"):
        problems.append("STATE_DATABASE_URL doit viser une SQLite absolue (registre W2 de la campagne)")
    if str(env.get("INTEGRATION_E2E_ENABLED", "")).strip().lower() in {"1", "true", "yes", "on"}:
        problems.append("INTEGRATION_E2E_ENABLED est actif : la campagne ponctuelle refuse toute récurrence")
    return problems


# ── préflight : chaque contrôle rend un Step ; aucun ne peut émettre d'appel facturable ─────────────────────────────────


def check_launch_context(env: Mapping[str, str], report: CampaignReport, step: Step) -> None:
    """Déclenchement ponctuel : ``workflow_dispatch`` explicite, première tentative, confirmation exacte, pas de récurrence."""
    event = str(env.get("GITHUB_EVENT_NAME", "") or "")
    attempt = str(env.get("GITHUB_RUN_ATTEMPT", "") or "")
    step.evidence.update(event=event, run_attempt=attempt, run_id=str(env.get("GITHUB_RUN_ID", "") or ""))
    if event != "workflow_dispatch":
        raise IncompleteValidation(
            f"déclencheur {event!r} refusé : seule une exécution manuelle ponctuelle est autorisée"
        )
    if attempt != "1":
        raise IncompleteValidation(f"tentative {attempt!r} refusée : aucune relance payante automatique de la campagne")
    if str(env.get("W4_BUSINESS_CONFIRM", "")) != LAUNCH_CONFIRMATION:
        raise IncompleteValidation("confirmation de lancement unique absente ou inexacte")
    if str(env.get("INTEGRATION_E2E_ENABLED", "")).strip().lower() in {"1", "true", "yes", "on"}:
        raise IncompleteValidation("INTEGRATION_E2E_ENABLED est actif : récurrence interdite pour cette campagne")


def check_environment(env: Mapping[str, str], report: CampaignReport, step: Step) -> None:
    problems = validate_campaign_environment(env)
    step.evidence["bounds"] = asdict(CAMPAIGN_BOUNDS)
    if problems:
        raise IncompleteValidation("environnement hors enveloppe : " + " ; ".join(problems))


LLM_KEY_NAME = re.compile(r"^(LLM_API_KEY(_[A-Z]+)?|GEMINI_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY)$")
STAGE_STATIC, STAGE_FULL, STAGE_LAUNCH = "static", "full", "launch"
PREFLIGHT_STAGES = (STAGE_STATIC, STAGE_FULL, STAGE_LAUNCH)


def secret_values(env: Mapping[str, str]) -> List[str]:
    """Valeurs d'environnement à masquer dans tout rapport (jetons et clés, y compris par rôle) — jamais leurs noms."""
    pattern = re.compile(r"(^|_)(KEY|TOKEN|SECRET|PASSWORD)(_|$)", re.I)  # MAX_TOKENS_BUDGET n'est PAS un secret
    return [value for name, value in env.items() if value and len(value) >= 4 and pattern.search(name)]


def check_secret_scope(
    env: Mapping[str, str], report: CampaignReport, step: Step, *, stage: str = STAGE_STATIC
) -> None:
    """Portée des clés de modèle, DISTINCTE selon l'étape.

    * ``static`` / ``full`` — contrôles SANS clé : aucune clé de modèle ne doit être exposée à cette étape (elles ne vivent que
      dans l'étape qui les consomme) ;
    * ``launch`` — validation effective juste avant le lancement : l'environnement reçoit LÉGITIMEMENT la clé du transport
      choisi ; seuls les NOMS présents sont consignés, jamais une valeur (le rapport masque aussi les valeurs)."""
    present = sorted(name for name in env if LLM_KEY_NAME.match(name) and env.get(name))
    step.evidence["llm_secret_names_present"] = present
    step.evidence["stage"] = stage
    if stage == STAGE_LAUNCH:
        step.evidence["scope"] = (
            "lancement : les clés ne sont lues que par l'étape qui les consomme, valeurs jamais affichées"
        )
        return
    if present:
        raise RuntimeError(f"clé(s) de modèle exposée(s) à l'étape de préflight : {', '.join(present)}")


def check_fixture_identity(clients: Any, report: CampaignReport, step: Step, *, owner: str, repo: str) -> None:
    """Identité immuable du dépôt fixture et de sa graine — lectures seules, aucune écriture."""
    info = clients.repos.get_repo(owner, repo)
    step.evidence.update(
        repository=getattr(info, "full_name", None),
        repository_id=getattr(info, "id", None),
        default_branch=getattr(info, "default_branch", None),
    )
    if int(getattr(info, "id", 0) or 0) != FIXTURE_REPOSITORY_ID:
        raise RuntimeError("identité immuable du dépôt fixture incorrecte")
    if str(getattr(info, "full_name", "")).lower() != FIXTURE_REPOSITORY.lower():
        raise RuntimeError("coordonnée du dépôt fixture incorrecte")
    if bool(getattr(info, "is_private", True)) or bool(getattr(info, "archived", False)):
        raise RuntimeError("le dépôt fixture doit être public et non archivé")
    if getattr(info, "default_branch", None) != FIXTURE_ROOT_BRANCH:
        raise RuntimeError("branche par défaut du dépôt fixture inattendue")
    marker = clients.files.get_file_content(owner, repo, FIXTURE_MARKER_PATH, branch=FIXTURE_ROOT_BRANCH)
    if marker.get("content") != FIXTURE_SENTINEL:
        raise RuntimeError("sentinelle du dépôt fixture absente ou invalide")
    head = str(clients.branches.get_branch_sha(owner, repo, FIXTURE_ROOT_BRANCH) or "").lower()
    step.evidence["root_sha"] = head
    if head != FIXTURE_SEED_SHA:
        raise RuntimeError("le commit seed de la fixture a bougé — zéro écriture")


def worker_capacity_matrix(bounds: CampaignBounds = CAMPAIGN_BOUNDS) -> List[Dict[str, Any]]:
    """Pour chaque transport de worker RÉEL, la décision de ``worker_budget.allocate_worker`` sous les plafonds de la campagne.

    Aucune règle n'est recopiée : on pose un registre JETABLE (SQLite temporaire, strict, mêmes plafonds) et on demande à la
    fonction de production d'allouer. Rien n'est dépensé : aucun appel de modèle, seulement une réservation dans le registre
    jetable. Un refus est un refus ``BudgetRefused`` de production, avec son motif."""
    from datetime import datetime, timedelta, timezone

    from collegue.core.llm.budget_guard import BudgetBinding
    from collegue.executor import OHSdkAgent
    from collegue.executor.openhands_agent import OpenHandsAgent
    from collegue.executor.worker_budget import allocate_worker
    from collegue.state import ProjectStateManager
    from collegue.state.budget_ledger import BudgetRefused

    candidates = [
        ("OHSdkAgent / clé API facturable", OHSdkAgent, False, "gemini", "gemini-2.5-flash"),
        ("OHSdkAgent / abonnement", OHSdkAgent, True, "openai", "gpt-5.5"),
        ("OpenHandsAgent (legacy) / clé API facturable", OpenHandsAgent, False, "gemini", "gemini-2.5-flash"),
    ]
    matrix: List[Dict[str, Any]] = []
    for label, agent_cls, subscription, provider, model in candidates:
        with tempfile.TemporaryDirectory(prefix="w4-capacity-") as folder:
            manager = ProjectStateManager.from_url(f"sqlite:///{folder}/capacity.db", create=True)
            ledger = manager.budget_ledger
            scope = ledger.create_planning_scope(
                max_cost_usd=bounds.max_cost_usd,
                max_tokens=bounds.max_tokens,
                strict=True,
                scope_key="planning:capacity",
            )
            settings = SimpleNamespace(
                LLM_PROVIDER=provider,
                LLM_MODEL=model,
                CODER_SUBSCRIPTION=subscription,
                CODER_SUBSCRIPTION_MODEL=model,
                LLM_PRICE_PROMPT_PER_1M=0.3,
                LLM_PRICE_COMPLETION_PER_1M=2.5,
            )
            deadline = datetime.now(timezone.utc) + timedelta(seconds=bounds.max_seconds)
            binding = BudgetBinding(ledger=ledger, scope_key=scope.scope_key, settings=settings, deadline=deadline)
            declared = type(agent_cls.__name__, (), {"budget_enforcement": agent_cls.budget_enforcement})()
            try:
                allocation = allocate_worker(
                    binding, agent=declared, label="w4-capacity", timeout_seconds=bounds.max_seconds
                )
                outcome = {
                    "accepted": True,
                    "max_micro_usd": allocation.max_micro_usd,
                    "max_tokens": allocation.max_tokens,
                }
            except BudgetRefused as exc:
                outcome = {"accepted": False, "code": getattr(exc, "code", None), "reason": str(exc)[:400]}
            matrix.append({"transport": label, "declared_enforcement": agent_cls.budget_enforcement, **outcome})
    return matrix


class SentinelSandbox:
    """Sandbox SENTINELLE : toute tentative d'exécution est une erreur (le préflight n'exécute rien, il interroge les règles)."""

    def __getattr__(self, name: str) -> Callable[..., Any]:
        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(f"sandbox sentinelle : {name}() interdit pendant le préflight")

        return refuse


@contextlib.contextmanager
def _process_environment(env: Mapping[str, str]):
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def effective_settings(env: Mapping[str, str], *, cwd: Optional[str] = None) -> Any:
    """Réglages EFFECTIFS du produit pour cet environnement (``collegue.config.Settings``).

    Les commandes produit héritent du répertoire courant et ``Settings`` y lit ``.env`` : un ``.env`` présent rendrait la
    configuration VALIDÉE (environnement seul) différente de la configuration ÉMISE. Plutôt que de lire un fichier local
    (clés comprises) ou de modifier la sémantique de ``Settings`` (lot A), la validation REFUSE explicitement avant tout
    lancement ; le fichier n'est ni lu ni cité. Le workflow dédié (checkout vierge) n'a pas de ``.env``."""
    from collegue.config import Settings

    dotenv = os.path.join(cwd or os.getcwd(), ".env")
    if os.path.lexists(dotenv):
        raise IncompleteValidation(
            "configuration locale ambiguë : un fichier .env existe dans le répertoire de lancement et serait lu par le "
            "produit, mais pas par ce préflight (environnement seul) — le retirer ou lancer depuis un répertoire sans .env"
        )
    with _process_environment(env):
        return Settings(_env_file=None)


def gate_image(settings: Any) -> str:
    """Image que le gate de production exécute réellement (``runtime._build_gate_sandbox`` : ``SANDBOX_IMAGE``, comme le codeur)."""
    from collegue.sandbox import DEFAULT_SANDBOX_IMAGE

    return str(getattr(settings, "SANDBOX_IMAGE", DEFAULT_SANDBOX_IMAGE) or DEFAULT_SANDBOX_IMAGE)


ROLE_NAMES = ("PLANNER", "QA", "REVIEWER", "CODER")


def route_validator() -> Callable[..., Mapping[str, Any]]:
    """``validate_role_routes`` de la branche A, importé à l'appel ; absent ⇒ validation incomplète explicite (jamais un succès)."""
    try:
        from collegue.core.llm import LLMRole, validate_role_routes
    except ImportError as exc:
        raise IncompleteValidation(
            "API publique de routage par rôle (validate_role_routes, lot A) absente de ce code : "
            "la configuration effective ne peut pas être validée avant la campagne"
        ) from exc

    def validate(settings: Any, *, require_credential: bool) -> Mapping[str, Any]:
        roles = [getattr(LLMRole, name) for name in ROLE_NAMES]
        return validate_role_routes(settings, roles=roles, require_credential=require_credential)

    return validate


def check_effective_routes(
    report: CampaignReport,
    step: Step,
    *,
    settings: Any,
    require_credential: bool,
    validator: Optional[Callable[..., Mapping[str, Any]]] = None,
) -> None:
    """Destination effective de chaque rôle appelé (planificateur, QA, relecteur, codeur), AVANT toute planification payante."""
    if settings is None:
        raise IncompleteValidation("configuration effective illisible : routes non validables")
    validate = validator or route_validator()
    step.evidence["credential_required"] = require_credential
    step.evidence["llm_calls_emitted"] = 0
    try:
        routes = validate(settings, require_credential=require_credential)
    except Exception as exc:  # noqa: BLE001 - tout refus de route bloque ; le message d'A ne contient jamais de clé
        raise IncompleteValidation(f"route de rôle refusée ({type(exc).__name__}) : {exc}") from exc
    step.evidence["routes"] = dict(routes)


def effective_worker_capacity(settings: Any, bounds: CampaignBounds = CAMPAIGN_BOUNDS) -> Dict[str, Any]:
    """Décision de ``worker_budget.allocate_worker`` pour le worker RÉELLEMENT sélectionné et la configuration effective.

    Registre jetable (SQLite temporaire, strict, mêmes plafonds), VRAIE instance ``OHSdkAgent`` (celle du runtime) bâtie sur un
    sandbox sentinelle : sa chaîne de modèles et sa capacité sont celles de la production. Aucune règle recopiée, aucun appel de
    modèle, aucune dépense — la réservation vit dans le registre jetable, pas dans celui de la campagne."""
    from datetime import datetime, timedelta, timezone

    from collegue.core.llm.budget_guard import BudgetBinding
    from collegue.executor import OHSdkAgent
    from collegue.executor.worker_budget import allocate_worker
    from collegue.state import ProjectStateManager
    from collegue.state.budget_ledger import BudgetRefused

    agent = OHSdkAgent(SentinelSandbox(), settings_obj=settings)
    with tempfile.TemporaryDirectory(prefix="w4-capacity-") as folder:
        manager = ProjectStateManager.from_url(f"sqlite:///{folder}/capacity.db", create=True)
        ledger = manager.budget_ledger
        scope = ledger.create_planning_scope(
            max_cost_usd=bounds.max_cost_usd, max_tokens=bounds.max_tokens, strict=True, scope_key="planning:capacity"
        )
        deadline = datetime.now(timezone.utc) + timedelta(seconds=bounds.max_seconds)
        binding = BudgetBinding(ledger=ledger, scope_key=scope.scope_key, settings=settings, deadline=deadline)
        outcome: Dict[str, Any] = {"worker": type(agent).__name__, "declared_enforcement": agent.budget_enforcement}
        try:
            allocation = allocate_worker(binding, agent=agent, label="w4-capacity", timeout_seconds=bounds.max_seconds)
            outcome.update(
                accepted=True,
                max_micro_usd=allocation.max_micro_usd,
                max_tokens=allocation.max_tokens,
                model_chain=agent.model_chain(),
            )
        except BudgetRefused as exc:
            outcome.update(accepted=False, code=getattr(exc, "code", None), reason=str(exc)[:400])
        return outcome


def check_worker_capacity(
    report: CampaignReport,
    step: Step,
    *,
    settings: Any = None,
    capacity: Optional[Callable[[Any], Mapping[str, Any]]] = None,
    matrix: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Le worker EFFECTIVEMENT choisi tient simultanément les trois plafonds, sinon refus AVANT la planification payante.

    ``matrix`` (facultative) est INFORMATIVE : elle est consignée mais ne décide jamais à la place de la configuration choisie.
    Un refus W2 légitime reste une validation réelle incomplète, zéro appel émis."""
    if settings is None:
        raise IncompleteValidation("configuration effective illisible : capacité du worker non établie")
    outcome = dict((capacity or effective_worker_capacity)(settings))
    step.evidence["effective"] = outcome
    step.evidence["bounds"] = asdict(CAMPAIGN_BOUNDS)
    step.evidence["llm_calls_emitted"] = 0
    if matrix is not None:
        step.evidence["matrix_informative"] = matrix
    if not outcome.get("accepted"):
        raise IncompleteValidation(
            f"le worker sélectionné ({outcome.get('worker')}) ne garantit pas simultanément 2 USD, 250000 tokens et 900 s "
            f"en mode strict ({outcome.get('code')} : {outcome.get('reason')}) — refus avant toute planification payante, "
            "zéro appel émis"
        )


def check_base_protection(
    clients: Any, report: CampaignReport, step: Step, *, owner: str, repo: str, run_tag: str
) -> None:
    """Politique de fusion W3 applicable au vrai acteur sur la branche de base éphémère (nom hypothétique, aucune écriture)."""
    from collegue.pilot.merge_policy import MergeRefused, discover_server_policy

    branch = f"{BASE_BRANCH_PREFIX}/{run_tag}"
    step.evidence["probed_branch"] = branch
    try:
        policy = discover_server_policy(clients, owner, repo, branch)
    except MergeRefused as refused:
        step.evidence.update(refusal_code=refused.code)
        raise IncompleteValidation(
            f"protections W3 absentes ou non établies sur {branch!r} : {refused.reason} "
            "(le propriétaire du dépôt fixture doit protéger ce motif de branche ; aucune protection n'est modifiée ici)"
        ) from refused
    step.evidence.update(
        actor=policy.actor,
        actor_role=policy.actor_role,
        required_checks=[c.context for c in policy.required_checks],
        strict_sources=list(policy.strict_sources),
    )


ORACLE_MODULES = ("fastapi", "httpx", "sqlalchemy", "alembic", "pypdf")


def check_oracle_environment(
    report: CampaignReport, step: Step, *, image: str, runner: Callable[[Sequence[str]], "subprocess.CompletedProcess"]
) -> None:
    """L'image qui exécute les oracles contient la pile de la fixture ET un lecteur PDF réel (sinon rouge non valide)."""
    code = (
        "import importlib.util, sys; "
        f"missing=[m for m in {ORACLE_MODULES!r} if importlib.util.find_spec(m) is None]; "
        "import shutil; missing += [] if shutil.which('timeout') else ['timeout (superviseur de durée)']; "
        "print(','.join(missing)); sys.exit(1 if missing else 0)"
    )
    argv = [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        image,
        "python",
        "-c",
        code,
    ]
    step.evidence.update(image=image, modules=list(ORACLE_MODULES))
    try:
        result = runner(argv)
    except Exception as exc:  # noqa: BLE001 - image/Docker indisponible : la preuve n'est pas établie
        raise IncompleteValidation(f"environnement des oracles non vérifiable ({type(exc).__name__})") from exc
    if result.returncode != 0:
        missing = (result.stdout or "").strip() or "inconnu"
        raise IncompleteValidation(
            f"l'image {image!r} ne fournit pas la pile des oracles (manquants : {missing}) : un import absent n'est pas un rouge valide"
        )


def run_preflight(
    env: Mapping[str, str],
    *,
    clients: Any,
    campaign_id: str,
    run_tag: str,
    image_runner: Callable[[Sequence[str]], "subprocess.CompletedProcess"],
    stage: str = STAGE_FULL,
    settings: Any = None,
    capacity: Optional[Callable[[Any], Mapping[str, Any]]] = None,
    capacity_matrix: Optional[List[Dict[str, Any]]] = None,
    route_check: Optional[Callable[..., Mapping[str, Any]]] = None,
) -> CampaignReport:
    """Préflight complet, SANS appel de modèle : l'ordre va du moins coûteux au plus dépendant d'un service externe.

    ``stage`` distingue les contrôles SANS clé de la validation effective avant lancement :

    * ``static`` — sans clé, sans image (construite seulement si ces contrôles passent : P08 facultative et non jouée) ;
    * ``full`` — sans clé, image du gate incluse (verdict autorisant la construction/le lancement) ;
    * ``launch`` — validation EFFECTIVE juste avant l'émission : la clé du transport choisi est légitimement présente (jamais
      affichée), les routes sont exigées AVEC leur credential, l'image du gate est vérifiée. Jamais d'étape ``static`` ici."""
    if stage not in PREFLIGHT_STAGES:
        raise ValueError(f"étape de préflight inconnue: {stage!r}")
    report = CampaignReport("preflight", campaign_id, secrets=secret_values(env))
    owner, _, repo = FIXTURE_REPOSITORY.partition("/")
    launch_stage = stage == STAGE_LAUNCH
    report.declare("P01-launch-context", "Déclenchement ponctuel, première tentative, confirmation, pas de récurrence")
    report.declare("P02-environment", "Environnement dans l'enveloppe 2 USD / 250000 tokens / 900 s, registre durable")
    report.declare(
        "P03-secret-scope",
        "Portée des clés de modèle : légitimes à l'étape de lancement, jamais affichées"
        if launch_stage
        else "Aucune clé de modèle dans l'étape de préflight",
    )
    report.declare("P04-fixture-identity", "Identité du dépôt fixture et de sa graine (lectures seules)")
    report.declare(
        "P05-role-routes",
        "Destination effective de chaque rôle (planificateur, QA, relecteur, codeur) sans émission",
    )
    report.declare(
        "P06-worker-capacity",
        "Le worker réellement sélectionné tient simultanément les trois plafonds (avant toute planification payante)",
    )
    report.declare(
        "P07-base-protection", "Politique de fusion W3 applicable à la base éphémère (acteur et branche réels)"
    )
    # Étape « static » : tout sauf l'image du gate (construite seulement si ces contrôles passent) — P08 est alors
    # facultative ET non jouée ; « full » et « launch » (verdicts qui autorisent le lancement) l'exigent.
    report.declare(
        "P08-oracle-environment",
        "Pile des oracles et lecteur PDF réel dans l'image que le gate exécute réellement",
        required=stage != STAGE_STATIC,
    )
    report.facts.update(
        bounds=asdict(CAMPAIGN_BOUNDS),
        fixture={"repository": FIXTURE_REPOSITORY, "id": FIXTURE_REPOSITORY_ID, "seed_sha": FIXTURE_SEED_SHA},
        llm_calls_emitted=0,
        billable_actions_emitted=0,
        stage=stage,
    )
    settings_error: Optional[str] = None
    if settings is None:
        try:
            settings = effective_settings(env)
        except IncompleteValidation as exc:  # message sûr (jamais de valeur de configuration)
            settings_error = str(exc)
        except Exception as exc:  # noqa: BLE001 - jamais le message : une validation pydantic peut citer une valeur
            fields = [".".join(map(str, e.get("loc", ()))) for e in getattr(exc, "errors", lambda: [])()]
            settings_error = f"{type(exc).__name__}" + (f" ({', '.join(fields)})" if fields else "")
    report.run("P01-launch-context", lambda s: check_launch_context(env, report, s))
    report.run("P02-environment", lambda s: check_environment(env, report, s))
    report.run("P03-secret-scope", lambda s: check_secret_scope(env, report, s, stage=stage))
    report.run("P04-fixture-identity", lambda s: check_fixture_identity(clients, report, s, owner=owner, repo=repo))

    def _routes(step: Step) -> None:
        if settings_error:
            raise IncompleteValidation(f"configuration effective illisible : {settings_error}")
        check_effective_routes(report, step, settings=settings, require_credential=launch_stage, validator=route_check)

    def _capacity(step: Step) -> None:
        if settings_error:
            raise IncompleteValidation(f"configuration effective illisible : {settings_error}")
        check_worker_capacity(report, step, settings=settings, capacity=capacity, matrix=capacity_matrix)

    report.run("P05-role-routes", _routes)
    report.run("P06-worker-capacity", _capacity)
    report.run(
        "P07-base-protection",
        lambda s: check_base_protection(clients, report, s, owner=owner, repo=repo, run_tag=run_tag),
    )
    if stage == STAGE_STATIC:
        return report
    report.run(
        "P08-oracle-environment",
        lambda s: check_oracle_environment(
            report, s, image=gate_image(settings) if settings is not None else gate_image(None), runner=image_runner
        ),
    )
    return report


# ── vérification métier d'un checkout livré ─────────────────────────────────────────────────────────────────────────────

REPORT_MARKER = "W4-BUSINESS-REPORT:"

#: Données de l'audit de référence (accents inclus : seul un vrai lecteur PDF les restitue).
REFERENCE_AUDIT = {
    "title": "Audit de sécurité T3",
    "auditor": "Camille Durand",
    "summary": "Revue des accès",
    "findings": [
        {"severity": "high", "description": "Mot de passe administrateur en clair"},
        {"severity": "low", "description": "En-têtes HTTP manquants"},
    ],
}
LEGAL_NOTICE = "CONFIDENTIEL"

_VERIFY_SCRIPT = r"""
import io, json, os, sqlite3, subprocess, sys, tempfile
from pathlib import Path

REPORT_MARKER = %(marker)r
phase = sys.argv[1]
database = Path(sys.argv[2])
audit_id = int(sys.argv[3]) if sys.argv[3] else None
reference = json.loads(sys.argv[4])
notice = sys.argv[5]
out = {"phase": phase, "checks": {}, "observations": {}}
checks, obs = out["checks"], out["observations"]
env = dict(os.environ, DATABASE_URL=f"sqlite:///{database}")
os.environ["DATABASE_URL"] = env["DATABASE_URL"]
sys.path.insert(0, os.getcwd())

def finish():
    print(REPORT_MARKER + json.dumps(out, ensure_ascii=False))
    sys.exit(0)

try:
    from fastapi.testclient import TestClient
    from pypdf import PdfReader
except ImportError as exc:
    out["incomplete"] = f"lecteur/pile indisponible: {exc}"
    finish()

def client():
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)

def pdf_text(content):
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages)

if phase == "write":
    obs["db_existed_before"] = database.exists()
    checks["database_really_empty"] = not database.exists()
    migrated = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env, capture_output=True, text=True)
    obs["migration_returncode"] = migrated.returncode
    checks["migration_succeeds"] = migrated.returncode == 0
    if migrated.returncode != 0:
        obs["migration_stderr_tail"] = migrated.stderr[-300:]
        finish()
    with sqlite3.connect(database) as con:
        obs["tables"] = sorted(r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        obs["alembic_version"] = [r[0] for r in con.execute("SELECT version_num FROM alembic_version")]
    checks["schema_tables"] = {"audits", "findings"} <= set(obs["tables"])
    http = client()
    created = http.post("/audits", json=reference)
    obs["post_status"] = created.status_code
    checks["audit_created"] = created.status_code == 201
    if created.status_code != 201:
        finish()
    body = created.json()
    obs["audit_id"] = body.get("id")
    read = http.get(f"/audits/{body['id']}")
    obs["get_status"] = read.status_code
    checks["audit_read_back_identical"] = read.status_code == 200 and read.json() == body
    checks["findings_preserved"] = body.get("findings") == reference["findings"]
    checks["unknown_audit_is_404"] = http.get("/audits/987654").status_code == 404
    with sqlite3.connect(database) as con:
        obs["rows"] = {"audits": con.execute("SELECT count(*) FROM audits").fetchone()[0],
                       "findings": con.execute("SELECT count(*) FROM findings").fetchone()[0]}
    checks["rows_persisted"] = obs["rows"] == {"audits": 1, "findings": len(reference["findings"])}
else:
    http = client()
    read = http.get(f"/audits/{audit_id}")
    obs["reread_status"] = read.status_code
    checks["audit_survives_restart"] = read.status_code == 200 and read.json().get("title") == reference["title"]

response = client().get(f"/audits/{audit_id if audit_id is not None else obs.get('audit_id')}/export.pdf")
obs["pdf_status"] = response.status_code
obs["pdf_content_type"] = response.headers.get("content-type")
checks["pdf_served"] = response.status_code == 200 and str(response.headers.get("content-type", "")).startswith("application/pdf")
if checks["pdf_served"]:
    text = pdf_text(response.content)
    obs["pdf_reader"] = "pypdf " + __import__("pypdf").__version__
    obs["pdf_pages"] = len(PdfReader(io.BytesIO(response.content)).pages)
    obs["pdf_text_excerpt"] = text[:400]
    expected = [reference["title"], reference["auditor"]] + [f["description"] for f in reference["findings"]]
    obs["pdf_missing_data"] = [e for e in expected if e not in text]
    checks["pdf_text_has_the_persisted_audit_data"] = not obs["pdf_missing_data"]
    checks["pdf_names_the_audit"] = f"n° {audit_id if audit_id is not None else obs.get('audit_id')}" in text
    obs["pdf_raw_bytes_contain_title"] = reference["title"].encode() in response.content
    checks["legal_notice_present"] = (notice in text) if notice else True
finish()
"""


@dataclass
class BusinessObservation:
    status: str  # passed | failed | incomplete
    checks: Dict[str, bool]
    observations: Dict[str, Any]
    failed: List[str]
    detail: str = ""


#: Superviseur de durée HORS de l'interpréteur non fiable : ``timeout(1)`` est le processus principal du conteneur et lance le
#: script en enfant ; un livrable ne peut ni annuler ni remplacer un minuteur qui ne vit pas dans son propre processus.
#: 124 = échéance atteinte (TERM), 137 = TERM ignoré puis KILL après ``WATCHDOG_KILL_AFTER`` secondes.
DEADLINE_EXITS = (124, 137)
WATCHDOG_KILL_AFTER = 3
#: Marge du client hôte au-delà du superviseur du conteneur : fenêtre de relève et de ``docker kill`` — aucun appel de modèle,
#: aucun travail de vérification supplémentaire (ce n'est PAS une nouvelle enveloppe).
HOST_KILL_MARGIN = 2.0
#: ``docker run`` : 125 = démon/lancement refusé, 126/127 = commande de l'image non exécutable/introuvable.
DOCKER_UNAVAILABLE_EXITS = (125, 126, 127)
DEFAULT_VERIFIER_IMAGE = DEFAULT_SANDBOX_IMAGE  # même défaut que le gate de production (SANDBOX_IMAGE)
#: Variables conservées pour le code vérifié (liste BLANCHE : aucune clé, aucun jeton, rien d'hérité par accident).
VERIFIER_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TZ")
#: Variables conservées pour le CLIENT docker (jamais transmises au conteneur : ``docker run`` ne propage rien sans ``-e``).
DOCKER_CLIENT_ENV_ALLOWLIST = (
    "PATH", "HOME", "LANG", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "XDG_RUNTIME_DIR",
)  # fmt: skip


def credential_free_env(
    allowlist: Sequence[str] = VERIFIER_ENV_ALLOWLIST,
    extra: Optional[Mapping[str, str]] = None,
    *,
    source: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Environnement construit par liste blanche : ne reprend de ``source`` (défaut ``os.environ``) que les noms autorisés."""
    origin = os.environ if source is None else source
    env = {name: origin[name] for name in allowlist if name in origin}
    env.update(extra or {})
    leaked = [name for name in SECRET_ENV_NAMES if name in env]
    if leaked:  # défense en profondeur : une liste blanche modifiée ne doit jamais laisser passer un secret connu
        raise ValueError(f"environnement du vérificateur non sûr : {', '.join(leaked)}")
    return env


def trusted_local_runner(
    argv: Sequence[str], cwd: str, env: Mapping[str, str], timeout: float
) -> "subprocess.CompletedProcess":
    """Exécution SUR L'HÔTE — réservée aux fixtures de CONFIANCE (arbres écrits par les tests eux-mêmes).

    Jamais le chemin par défaut : du code généré par un agent ne doit pas s'exécuter sur l'hôte. L'appelant doit la passer
    explicitement (``runner=trusted_local_runner``). L'environnement reçu est re-filtré par liste blanche."""
    clean = credential_free_env(
        source=env, allowlist=tuple(VERIFIER_ENV_ALLOWLIST) + ("PYTHONDONTWRITEBYTECODE", "PYTHONPATH")
    )
    return subprocess.run(list(argv), cwd=cwd, env=clean, capture_output=True, text=True, timeout=timeout)


def _parse_report(stdout: str) -> Optional[Dict[str, Any]]:
    for line in reversed((stdout or "").splitlines()):
        if line.startswith(REPORT_MARKER):
            try:
                return json.loads(line[len(REPORT_MARKER) :])
            except ValueError:
                return None
    return None


def verify_business_checkout(
    checkout: str,
    *,
    python: Optional[str] = None,
    require_legal_notice: bool = True,
    reference: Optional[Dict[str, Any]] = None,
    timeout: float = 120.0,
    runner: Optional[Callable[..., "subprocess.CompletedProcess"]] = None,
    database_dir: Optional[str] = None,
    image: Optional[str] = None,
    deadline_monotonic: Optional[float] = None,
    clock: Optional[Callable[[], float]] = None,
) -> BusinessObservation:
    """Observe le livrable : base VIERGE → migration → création/lecture → redémarrage → PDF lu par un vrai lecteur.

    Indépendant des tests écrits par l'agent. ``incomplete`` = pile, lecteur PDF, Docker ou échéance indisponible (jamais un
    succès ni un échec de l'application) ; ``failed`` = au moins une assertion métier est fausse.

    **Isolement.** Sans ``runner``, le livrable (code NON FIABLE) s'exécute dans un conteneur Docker durci (voir
    :func:`docker_verifier_command`) ; si Docker ou l'image est indisponible, ou si un montage est refusé par la garde commune
    W1, le résultat est ``incomplete`` — jamais une exécution sur l'hôte. Le runner local (:func:`trusted_local_runner`) est
    réservé aux fixtures de confiance et doit être demandé explicitement.

    **Durée.** ``timeout`` borne chaque phase ; ``deadline_monotonic`` (échéance GLOBALE de la campagne, horloge ``clock``) est
    PARTAGÉE par les deux phases : aucune phase ne démarre après expiration (``BudgetStop``) et chacune est bornée par le temps
    restant. En conteneur, la durée est supervisée par ``timeout(1)`` hors du processus non fiable ET par le client hôte."""
    if runner is None:
        return _verify_in_docker(
            checkout,
            image=image or os.environ.get("SANDBOX_IMAGE") or DEFAULT_VERIFIER_IMAGE,
            require_legal_notice=require_legal_notice,
            reference=reference,
            timeout=timeout,
            deadline_monotonic=deadline_monotonic,
            clock=clock,
        )
    return _observe(
        checkout,
        runner,
        python=python or sys.executable,
        require_legal_notice=require_legal_notice,
        reference=reference,
        timeout=timeout,
        database_dir=database_dir,
        deadline_monotonic=deadline_monotonic,
        clock=clock,
    )


def _observe(
    checkout: str,
    run: Callable[..., "subprocess.CompletedProcess"],
    *,
    python: str,
    require_legal_notice: bool,
    reference: Optional[Dict[str, Any]],
    timeout: float,
    database_dir: Optional[str],
    deadline_monotonic: Optional[float] = None,
    clock: Optional[Callable[[], float]] = None,
) -> BusinessObservation:
    from collegue.sandbox.executor import SandboxRefused

    now = clock or time.monotonic  # résolu à l'appel (horloge contrôlable)
    reference = reference or REFERENCE_AUDIT
    notice = LEGAL_NOTICE if require_legal_notice else ""
    with tempfile.TemporaryDirectory(prefix="w4-business-") as folder:
        # ``database_dir`` : répertoire (vu du process vérifié) d'une base qui n'existe PAS encore ; en conteneur, c'est le
        # montage de travail partagé entre les deux phases (écriture puis relecture après « redémarrage »).
        database = os.path.join(database_dir or folder, "audits.db")
        script = _VERIFY_SCRIPT % {"marker": REPORT_MARKER}
        env = credential_free_env(extra={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""})
        merged: Dict[str, Any] = {"checks": {}, "observations": {}}
        audit_id: Optional[int] = None
        for phase in ("write", "reread"):
            argv = [python, "-c", script, phase, database, str(audit_id or ""), json.dumps(reference), notice]
            limit, bounded = float(timeout), False
            if deadline_monotonic is not None:
                remaining = deadline_monotonic - now()
                if remaining <= 0:  # aucune nouvelle vérification après expiration de l'enveloppe globale
                    raise BudgetStop(f"échéance globale atteinte avant la phase « {phase} » de la vérification métier")
                if remaining < limit:
                    limit, bounded = remaining, True
            try:
                proc = run(argv, checkout, env, limit)
            except subprocess.TimeoutExpired:
                if bounded:
                    raise BudgetStop(
                        f"échéance globale atteinte pendant la phase « {phase} » de la vérification"
                    ) from None
                return BusinessObservation(
                    "incomplete", merged["checks"], merged["observations"], [], "délai de vérification dépassé"
                )
            except SandboxRefused as exc:
                return BusinessObservation(
                    "incomplete", merged["checks"], merged["observations"], [], f"montage refusé (garde W1) : {exc}"
                )
            except OSError as exc:  # docker absent, non exécutable, démon injoignable
                return BusinessObservation(
                    "incomplete", merged["checks"], merged["observations"], [], f"exécution isolée indisponible : {exc}"
                )
            report = _parse_report(proc.stdout)
            if report is None:
                tail = ((proc.stderr or "") + (proc.stdout or ""))[-400:]
                if proc.returncode in DEADLINE_EXITS:
                    if bounded:
                        raise BudgetStop(f"échéance globale atteinte pendant la phase « {phase} » de la vérification")
                    return BusinessObservation(
                        "incomplete",
                        merged["checks"],
                        merged["observations"],
                        [],
                        "échéance de la vérification (superviseur hors du processus non fiable)",
                    )
                if proc.returncode in DOCKER_UNAVAILABLE_EXITS:
                    return BusinessObservation(
                        "incomplete",
                        merged["checks"],
                        merged["observations"],
                        [],
                        f"Docker ou image indisponible (code {proc.returncode}) : {tail}",
                    )
                return BusinessObservation(
                    "failed",
                    merged["checks"],
                    merged["observations"],
                    ["verifier_report_missing"],
                    f"rapport illisible: {tail}",
                )
            if report.get("incomplete"):
                return BusinessObservation(
                    "incomplete", merged["checks"], merged["observations"], [], report["incomplete"]
                )
            merged["checks"].update({f"{phase}:{k}": v for k, v in report["checks"].items()})
            merged["observations"].update({f"{phase}.{k}": v for k, v in report["observations"].items()})
            if phase == "write":
                audit_id = report["observations"].get("audit_id")
                if audit_id is None:
                    break
        failed = sorted(name for name, ok in merged["checks"].items() if not ok)
        return BusinessObservation("failed" if failed else "passed", merged["checks"], merged["observations"], failed)


def _verifier_mount(path: str, label: str) -> str:
    """Chemin de montage validé par la garde COMMUNE W1 (contrôles Git direct/imbriqué/ancêtre, ``:``, liens, erreurs de stat)."""
    from collegue.sandbox.executor import SandboxRefused, git_control_exposure

    raw = os.path.abspath(os.fspath(path))
    real = os.path.realpath(raw)
    if ":" in raw or ":" in real:
        raise SandboxRefused(f"{label} invalide (contient ':')")
    if real == os.path.sep:
        raise SandboxRefused(f"{label} invalide : la racine du FS ne peut pas être montée")
    reason = git_control_exposure(raw)  # chemin BRUT : un lien pendant doit rester visible
    if reason is not None:
        raise SandboxRefused(f"{label} refusé : montage exposant les métadonnées Git de contrôle ({reason})")
    return real


def docker_verifier_command(
    *, image: str, name: str, checkout: str, scratch: str, memory: str = "512m", user: Optional[str] = None
) -> List[str]:
    """Commande Docker DURCIE de la vérification d'un livrable généré (code non fiable) : aucun réseau, aucun secret, racine en
    lecture seule, checkout monté en lecture seule, répertoire de travail ``scratch`` (hôte, vierge) monté sur ``/scratch``,
    conteneur NOMMÉ (donc arrêtable à l'échéance), exécuté sous l'UID de l'appelant (jamais root).

    Les deux montages passent par la garde commune W1 (``git_control_exposure``) : un répertoire de contrôle Git, direct,
    imbriqué ou ancêtre, un ``:`` ou une erreur de stat ⇒ :class:`SandboxRefused` AVANT toute commande."""
    checkout_path = _verifier_mount(checkout, "checkout")
    scratch_path = _verifier_mount(scratch, "scratch")
    if user is None and hasattr(os, "getuid"):
        user = f"{os.getuid()}:{os.getgid()}"
    return [
        "docker", "run", "--rm", "--name", name, "--pull", "never", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", memory, "--cpus", "1",
        "--pids-limit", "128", "--stop-timeout", "5", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
        *(["--user", user] if user else []),
        "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HOME=/tmp", "-w", "/workspace",
        "-v", f"{checkout_path}:/workspace:ro", "-v", f"{scratch_path}:/scratch:rw", image,
    ]  # fmt: skip


def run_in_named_container(
    argv: Sequence[str],
    *,
    name: str,
    timeout: float,
    runner: Optional[Callable[..., "subprocess.CompletedProcess"]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> "subprocess.CompletedProcess":
    """Exécute ``argv`` (un ``docker run --name <name>``) et, à l'échéance (ou à toute interruption), ARRÊTE le conteneur par son
    NOM avant de propager : tuer le client ``docker`` ne tue pas le conteneur. ``subprocess.run`` est résolu à l'appel."""
    run = runner if runner is not None else subprocess.run
    options: Dict[str, Any] = {"capture_output": True, "text": True}
    if env is not None:
        options["env"] = dict(env)
    try:
        return run(list(argv), timeout=timeout, **options)
    except BaseException:
        try:
            run(["docker", "kill", name], timeout=30, **options)
        except (OSError, subprocess.SubprocessError):
            pass  # best-effort : le conteneur est de toute façon sous --rm et sous son échéance autonome
        raise


def _verify_in_docker(
    checkout: str,
    *,
    image: str,
    require_legal_notice: bool,
    reference: Optional[Dict[str, Any]],
    timeout: float,
    deadline_monotonic: Optional[float] = None,
    clock: Optional[Callable[[], float]] = None,
) -> BusinessObservation:
    import shutil
    import uuid

    name = f"w4-verify-{uuid.uuid4().hex[:12]}"
    scratch = tempfile.mkdtemp(prefix="w4-scratch-")  # 0700, vierge, jetable, propre à cette vérification
    client_env = credential_free_env(DOCKER_CLIENT_ENV_ALLOWLIST)

    def runner(argv: Sequence[str], cwd: str, _env: Mapping[str, str], limit: float) -> Any:
        # Le superviseur de durée est le processus principal du conteneur (``timeout`` lance le script en ENFANT) : le code
        # livré, qui s'exécute dans l'enfant, ne peut pas l'annuler. Le client hôte garde une marge de relève, puis tue par NOM.
        supervisor = ["timeout", "--signal=TERM", f"--kill-after={WATCHDOG_KILL_AFTER}", f"{max(0.1, limit):.3f}"]
        command = docker_verifier_command(image=image, name=name, checkout=checkout, scratch=scratch)
        return run_in_named_container(
            command + supervisor + list(argv),
            name=name,
            timeout=limit + WATCHDOG_KILL_AFTER + HOST_KILL_MARGIN,
            env=client_env,
        )

    try:
        return _observe(
            checkout,
            runner,
            python="python",
            require_legal_notice=require_legal_notice,
            reference=reference,
            timeout=timeout,
            database_dir="/scratch",
            deadline_monotonic=deadline_monotonic,
            clock=clock,
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# ── registre W2 : les plafonds viennent du registre durable, jamais d'un compteur propre ─────────────────────────────────


def registry_counters(manager: Any, project_id: int) -> Dict[str, Any]:
    """Compteurs du registre durable d'un projet (consommé / réservé / inconnu / plafonds), lus sans calcul propre."""
    ledger = getattr(manager, "budget_ledger", None)
    snapshot = ledger.snapshot_for_project(int(project_id)) if ledger is not None else None
    if snapshot is None:
        return {"scope": None}
    return {
        "scope": snapshot.scope_key,
        "strict": snapshot.strict,
        "cap_usd": None if snapshot.cap_micro_usd is None else snapshot.cap_micro_usd / 1_000_000,
        "cap_tokens": snapshot.cap_tokens,
        "consumed_micro_usd": snapshot.consumed_micro_usd,
        "consumed_tokens": snapshot.consumed_tokens,
        "reserved_micro_usd": snapshot.reserved_micro_usd,
        "reserved_tokens": snapshot.reserved_tokens,
        "unknown_micro_usd": snapshot.unknown_micro_usd,
        "unknown_tokens": snapshot.unknown_tokens,
        "blocked_reason": snapshot.blocked_reason,
        "revision": snapshot.revision,
    }


def assert_registry_within_bounds(counters: Mapping[str, Any], bounds: CampaignBounds = CAMPAIGN_BOUNDS) -> None:
    """Le registre porte bien l'enveloppe de la campagne (plafonds ≤ bornes, strict) et ne l'a pas dépassée."""
    if counters.get("scope") is None:
        raise IncompleteValidation("aucun scope budgétaire durable pour le projet")
    if not counters.get("strict"):
        raise RuntimeError("le registre n'est pas en mode strict")
    cap_usd, cap_tokens = counters.get("cap_usd"), counters.get("cap_tokens")
    if cap_usd is None or cap_usd > bounds.max_cost_usd + 1e-9:
        raise RuntimeError(f"plafond USD du registre hors enveloppe ({cap_usd!r} > {bounds.max_cost_usd})")
    if cap_tokens is None or cap_tokens > bounds.max_tokens:
        raise RuntimeError(f"plafond de tokens du registre hors enveloppe ({cap_tokens!r} > {bounds.max_tokens})")
    used_usd = (
        counters["consumed_micro_usd"] + counters["reserved_micro_usd"] + counters["unknown_micro_usd"]
    ) / 1_000_000
    used_tokens = counters["consumed_tokens"] + counters["reserved_tokens"] + counters["unknown_tokens"]
    if used_usd > cap_usd + 1e-9 or used_tokens > cap_tokens:
        raise BudgetStop(f"enveloppe atteinte ({used_usd:.6f} $ / {used_tokens} tokens)")


# ── invocation réelle unique ────────────────────────────────────────────────────────────────────────────────────────────


def business_config(env: Mapping[str, str]) -> Any:
    """``NightlyConfig`` du dépôt fixture dont la base éphémère vit sous ``collegue-business/<run>`` (motif à protéger)."""
    from collegue.pilot.nightly_e2e import NightlyConfig

    class _BusinessConfig(NightlyConfig):
        @property
        def base_branch(self) -> str:
            return f"{BASE_BRANCH_PREFIX}/{self.tag}"

    return _BusinessConfig(
        token=str(env.get("GITHUB_TOKEN", "")),
        repository=FIXTURE_REPOSITORY,
        repository_id=FIXTURE_REPOSITORY_ID,
        root_branch=FIXTURE_ROOT_BRANCH,
        seed_sha=FIXTURE_SEED_SHA,
        run_id=str(env.get("GITHUB_RUN_ID", "")),
        run_attempt=str(env.get("GITHUB_RUN_ATTEMPT", "")),
        manifest_path=str(env.get("COLLEGUE_NIGHTLY_MANIFEST", "")),
    )


class NightlyAdapter:
    """Réutilise les briques durcies de ``NightlyE2ERunner`` (base propriétaire, label, nettoyage idempotent) sans le flux
    mono-tâche du smoke : le séquencement multi-tâches est dans :func:`launch_campaign`."""

    def __init__(self, config: Any, clients: Any, command_runner: Callable[..., Any]):
        from collegue.pilot.nightly_e2e import NightlyE2ERunner

        self.inner = NightlyE2ERunner(config, clients=clients, command_runner=command_runner)
        self.config = config
        self.command_runner = command_runner

    def guard_fixture(self) -> str:
        return self.inner._guard_fixture(require_seed=True)

    def create_base(self, manifest: Any) -> str:
        return self.inner._create_owned_base(manifest)

    def create_label(self, manifest: Any) -> None:
        self.inner._create_owned_label(manifest)

    def product(self, *args: str, accepted_codes: tuple = (0,)) -> Dict[str, Any]:
        return self.inner._run_product(*args, accepted_codes=accepted_codes)

    def clone(self, base_sha: str) -> str:
        return self.inner._clone_base(base_sha)

    def cleanup(self) -> Any:
        return self.inner.cleanup()


def bounded_command_runner(
    deadline_monotonic: float, *, clock: Optional[Callable[[], float]] = None
) -> Callable[..., Any]:
    """Exécuteur de commandes BORNÉ par l'échéance globale : aucune commande n'est lancée passé 900 s, et une commande en cours
    à l'échéance est tuée IMMÉDIATEMENT avec son groupe de processus. Aucune grâce : une commande de planification ou de
    développement ne peut pas dépenser après l'expiration (la relève du processus tué n'émet rien)."""
    from collegue.pilot.nightly_e2e import CommandResult

    def run(argv: Sequence[str], *, cwd: Optional[str] = None) -> Any:
        remaining = deadline_monotonic - (clock or time.monotonic)()
        if remaining <= 0:
            raise BudgetStop("échéance globale de 900 s atteinte avant le lancement de la commande suivante")
        process = subprocess.Popen(
            list(argv), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
        )
        try:
            stdout, stderr = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            import signal

            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise BudgetStop("échéance globale atteinte : commande arrêtée avec son groupe de processus") from None
        return CommandResult(process.returncode, stdout, stderr)

    return run


BUILD_STOP_BUDGET = {"paused_budget", "deadline_reached"}


def launch_campaign(
    report: CampaignReport,
    *,
    adapter: Any,
    env: Mapping[str, str],
    python: str = sys.executable,
) -> Dict[str, Any]:
    """Planifie, approuve, synchronise et exécute les TROIS tâches sur la base éphémère (BUILD + fusions W3), puis rend le
    contexte nécessaire à la vérification métier. ``adapter.cleanup()`` est TOUJOURS appelé (ressources distantes éphémères).

    Aucun retry payant : une seule exécution du produit ; un arrêt par budget/échéance est rapporté ``budget_stop``."""
    from collegue.pilot.nightly_e2e import NightlyManifest, _write_manifest

    cfg = adapter.config
    manifest = NightlyManifest.for_config(cfg)
    # Identité du projet/scope conservée DÈS sa création et jusqu'aux sorties d'erreur, de budget ou d'échéance : le rapport
    # lit le MÊME registre après un arrêt, sans dépendre de la valeur de retour (absente quand l'exécution lève).
    context: Dict[str, Any] = report.facts.setdefault("launch", {})
    context.update(base_branch=cfg.base_branch)
    try:
        manifest.root_sha = adapter.guard_fixture()
        _write_manifest(cfg.manifest_path, manifest)
        base_sha = adapter.create_base(manifest)
        context["base_sha"] = base_sha
        draft = adapter.product(
            "plan", "draft", "--name", f"W4 {cfg.tag}", "--problem", BUSINESS_PROBLEM, "--owner", cfg.owner,
            "--repo", cfg.repo, "--base", cfg.base_branch, "--labels", cfg.issue_label, "--milestone", "",
            "--spec-filename", "SPEC.md", "--deadline-hours", "0.25", "--nightly-exact-task-count", "3",
            "--format", "json",
        )  # fmt: skip
        project_id, plan_hash = int(draft.get("project_id") or 0), str(draft.get("plan_hash") or "")
        if draft.get("action") != "draft" or int(draft.get("task_count") or 0) != 3 or project_id <= 0:
            raise RuntimeError("contrat JSON du draft inattendu (trois tâches exigées)")
        manifest.project_id, manifest.plan_hash = project_id, plan_hash
        context.update(project_id=project_id, plan_hash=plan_hash)
        _write_manifest(cfg.manifest_path, manifest)
        approved = adapter.product(
            "plan", "approve", "--project-id", str(project_id), "--expected-plan-hash", plan_hash, "--format", "json"
        )
        if approved.get("plan_hash") != plan_hash or int(approved.get("task_count") or 0) != 3:
            raise RuntimeError("l'approbation n'a pas scellé le hash attendu")
        adapter.create_label(manifest)
        synced = adapter.product("plan", "sync", "--project-id", str(project_id), "--execute", "--format", "json")
        issues = sorted(
            {int(i.get("issue_number") or 0) for i in list(synced.get("issues") or []) if i.get("issue_number")}
        )
        if len(issues) != 3:
            raise RuntimeError("la synchronisation doit créer exactement trois issues")
        manifest.issue_numbers = issues
        context["issue_numbers"] = issues
        _write_manifest(cfg.manifest_path, manifest)
        source = adapter.clone(adapter.inner.clients.branches.get_branch_sha(cfg.owner, cfg.repo, cfg.base_branch))
        result = adapter.product(
            "--project-id", str(project_id), "--repo-source", source, "--owner", cfg.owner, "--repo", cfg.repo,
            "--base", cfg.base_branch, "--execute", "--format", "json", accepted_codes=(0, 1, 2, 3, 4, 5),
        )  # fmt: skip
        stop = str(result.get("stop_reason") or "")
        context.update(
            stop_reason=stop,
            opened_prs=list(result.get("opened_prs") or []),
        )
        if stop in BUILD_STOP_BUDGET:
            raise BudgetStop(f"arrêt du produit : {stop} (enveloppe 2 USD / 250000 tokens / 900 s)")
        if stop != "completed":
            raise RuntimeError(f"le produit s'est arrêté sur {stop!r} au lieu de 'completed'")
        final_sha = adapter.inner.clients.branches.get_branch_sha(cfg.owner, cfg.repo, cfg.base_branch)
        context["final_sha"] = final_sha
        context["final_checkout"] = adapter.clone(final_sha)
        return context
    finally:
        adapter.cleanup()


CAMPAIGN_SCOPE_NOT_WIRED = (
    "R04-improvement",
    "R05-incident-rollback",
)
NOT_WIRED_STOP_POINT = (
    "point d'arrêt documenté : l'invocation réelle ne câble que planification, approbation, synchronisation, BUILD des trois "
    "tâches, vérification métier du livrable et lecture du registre ; la passe d'amélioration (handoff BUILD→IMPROVE) et "
    "l'incident contrôlé avec rollback Phase 5 ne sont pas exécutés par ce lancement (la preuve déterministe est dans "
    "tests/w4_business_campaign.py) — un BUILD réussi n'est pas la validation finale"
)


def run_campaign(
    env: Mapping[str, str],
    *,
    preflight: CampaignReport,
    launch: Callable[[CampaignReport], Any],
    verify: Optional[Callable[[CampaignReport, Any], None]] = None,
    read_registry: Optional[Callable[[Any], Mapping[str, Any]]] = None,
) -> CampaignReport:
    """Invocation réelle : ``launch`` n'est appelé QUE si tout le préflight a réussi, et au plus UNE fois.

    Le rapport retourné reprend les étapes du préflight ; en cas de préflight non validé, ``launch`` n'est jamais appelé, aucune
    action facturable n'est émise et le verdict reste celui du préflight (jamais un résultat métier inventé).

    **Portée annoncée.** Le rapport déclare TOUTES les preuves de sa portée (BUILD, métier, registre, amélioration, incident /
    rollback). Celles que ce lancement ne câble pas restent ``not_executed`` (arrêt amont) ou ``incomplete_validation`` avec le
    point d'arrêt exact : le verdict ne peut pas être ``validated`` tant qu'elles ne sont pas jouées.

    **Identité conservée.** Le contexte (projet, scope, base, tâches) est lu dans ``report.facts['launch']``, alimenté dès sa
    création par ``launch`` : un arrêt budget/échéance/erreur ne le perd pas et le registre lu est bien celui du projet."""
    report = CampaignReport("campaign", preflight.campaign_id, secrets=secret_values(env))
    for item in preflight.steps:
        copy = report.declare(item.id, item.title, required=item.required)
        copy.state, copy.detail, copy.evidence = item.state, item.detail, dict(item.evidence)
    report.facts.update(preflight.facts)
    report.facts["preflight_verdict"] = preflight.verdict()
    report.declare(
        "R01-run", "Planification, approbation, exécution et fusions sur la base éphémère (enveloppe globale)"
    )
    report.declare(
        "R02-business", "Vérification métier du livrable fusionné (base vierge, HTTP, PDF lu par un vrai lecteur)"
    )
    report.declare("R03-registry", "Compteurs du registre durable dans l'enveloppe")
    report.declare(
        "R04-improvement",
        "Passe d'amélioration par l'entrée publique : handoff BUILD→IMPROVE, mesure réelle, PR promue",
    )
    report.declare(
        "R05-incident-rollback", "Incident contrôlé, rollback Phase 5, acquittement et reprise avec observations métier"
    )
    report.facts["scope"] = {
        "announced": ["build", "business", "registry", "improvement", "incident_rollback"],
        "wired": ["build", "business", "registry"],
        "not_wired": ["improvement", "incident_rollback"],
        "stop_point": NOT_WIRED_STOP_POINT,
    }
    if preflight.verdict() != VERDICT_VALIDATED:
        report.halted = True
        report.facts["billable_actions_emitted"] = 0
        report.facts["stop_point"] = "preflight"
        return report
    context: Dict[str, Any] = {}

    def _sync_context() -> None:
        context.update(report.facts.get("launch") or {})

    def _launch(step: Step) -> None:
        try:
            context.update(launch(report) or {})
        finally:
            _sync_context()  # même quand l'exécution lève (budget, échéance, erreur) : l'identité du projet survit

    report.run("R01-run", _launch)
    _sync_context()

    def _verify(step: Step) -> None:
        if verify is None:
            raise IncompleteValidation("aucune vérification métier fournie")
        verify(report, context)

    def _registry(step: Step) -> None:
        if read_registry is None:
            raise IncompleteValidation("registre durable illisible")
        if not context.get("project_id"):
            raise IncompleteValidation(
                "aucun projet créé avant l'arrêt : la dépense éventuelle n'est pas établie (aucun zéro n'est inventé)"
            )
        try:
            counters = dict(read_registry(context))
        except (IncompleteValidation, BudgetStop):
            raise
        except Exception as exc:  # noqa: BLE001 - illisible = preuve manquante, jamais un échec qui masquerait l'arrêt d'origine
            raise IncompleteValidation(
                f"registre durable du projet {context.get('project_id')} illisible ({type(exc).__name__}) : "
                "dépense non établie (aucun zéro n'est inventé)"
            ) from exc
        step.evidence["counters"] = counters
        report.facts["registry_final"] = counters
        assert_registry_within_bounds(counters)

    def _not_wired(step: Step) -> None:
        raise IncompleteValidation(NOT_WIRED_STOP_POINT)

    report.run("R02-business", _verify)
    # Le registre est relu après TOUT arrêt dès que le lancement a eu lieu (projet/scope créé ou non) : la vérification
    # métier qui échoue, est incomplète ou dépasse l'échéance ne fait pas disparaître la dépense déjà réalisée. La lecture ne
    # masque jamais l'arrêt d'origine : impossible ⇒ preuve manquante (incomplete_validation), jamais un zéro inventé.
    halted_before = report.halted
    report.halted = False
    report.run("R03-registry", _registry)
    report.halted = halted_before or report.halted
    for step_id in CAMPAIGN_SCOPE_NOT_WIRED:
        report.run(step_id, _not_wired)  # arrêt amont ⇒ reste not_executed ; sinon validation incomplète documentée
    return report


def _fixture_clients(token: str) -> Any:
    from collegue.pilot.nightly_e2e import NightlyClients

    return NightlyClients.real(token)


def verify_in_container(
    report: CampaignReport,
    context: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    deadline_monotonic: Optional[float] = None,
    clock: Optional[Callable[[], float]] = None,
) -> None:
    """R02 : la vérification métier du livrable généré s'exécute dans un conteneur durci, nommé et arrêté à l'échéance.

    Elle PARTAGE l'échéance globale de la campagne (``deadline_monotonic``) : ni nouvelle fenêtre, ni démarrage après expiration."""
    import shutil

    checkout = str(context["final_checkout"])
    image = str(env.get("SANDBOX_IMAGE", "") or DEFAULT_VERIFIER_IMAGE)
    step = report.step("R02-business")
    try:
        observation = verify_business_checkout(
            checkout, python="python", image=image, deadline_monotonic=deadline_monotonic, clock=clock
        )
    finally:
        shutil.rmtree(os.path.dirname(checkout), ignore_errors=True)
    step.evidence.update(status=observation.status, checks=observation.checks, observations=observation.observations)
    if observation.status == "incomplete":
        raise IncompleteValidation(observation.detail or "vérification métier non établie")
    if observation.status != "passed":
        raise AssertionError("assertions métier fausses : " + ", ".join(observation.failed))


def registry_reader(env: Mapping[str, str]) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    def read(context: Mapping[str, Any]) -> Mapping[str, Any]:
        from collegue.state import ProjectStateManager

        if not context.get("project_id"):
            raise IncompleteValidation("identité du projet absente : registre non lisible")

        manager = ProjectStateManager.from_url(str(env["STATE_DATABASE_URL"]))
        return registry_counters(manager, int(context["project_id"]))

    return read


def _real_preflight(env: Mapping[str, str], campaign_id: str, stage: str = STAGE_FULL) -> CampaignReport:
    token = env.get("GITHUB_TOKEN", "")
    if not token:
        report = CampaignReport("preflight", campaign_id)
        report.declare("P00-github-token", "Jeton de lecture du dépôt fixture")

        def _no_token(step: Step) -> None:
            raise IncompleteValidation("GITHUB_TOKEN absent : l'identité du dépôt fixture ne peut pas être vérifiée")

        report.run("P00-github-token", _no_token)
        return report
    run_tag = f"{env.get('GITHUB_RUN_ID', '0')}-{env.get('GITHUB_RUN_ATTEMPT', '0')}"
    return run_preflight(
        env,
        clients=_fixture_clients(token),
        campaign_id=campaign_id,
        run_tag=run_tag,
        image_runner=lambda argv: subprocess.run(list(argv), capture_output=True, text=True, timeout=120),
        stage=stage,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m collegue.pilot.w4_business")
    parser.add_argument("action", choices=("preflight", "run", "cleanup", "capacity"))
    parser.add_argument(
        "--stage",
        choices=(STAGE_STATIC, STAGE_FULL),
        default=STAGE_FULL,
        help="préflight : « static » saute l'image (contrôles SANS clé) ; « run » exige toujours la validation effective complète",
    )
    parser.add_argument("--output", help="rapport machine JSON")
    parser.add_argument("--human", help="rapport humain (texte)")
    parser.add_argument("--campaign-id", default=os.environ.get("W4_BUSINESS_CAMPAIGN_ID", "w4-business"))
    args = parser.parse_args(argv)
    if args.action == "run" and args.stage != STAGE_FULL:
        parser.error(
            "« run » exige la validation effective complète (image du gate incluse) : --stage static est refusé"
        )
    if args.action == "capacity":
        matrix = worker_capacity_matrix()
        print(json.dumps(matrix, ensure_ascii=False, indent=2))
        return 0 if any(row["accepted"] for row in matrix) else EXIT_CODES[VERDICT_INCOMPLETE]
    env = dict(os.environ)
    if (
        args.action == "cleanup"
    ):  # idempotent, sans clé de modèle : ferme PR/issues et supprime les branches/labels du run
        config = business_config(env)
        payload = NightlyAdapter(
            config, _fixture_clients(config.token), bounded_command_runner(time.monotonic() + 600)
        ).cleanup()
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
        return 0
    # « preflight » : contrôles SANS clé (étape choisie) ; « run » : validation EFFECTIVE juste avant lancement — l'environnement
    # reçoit légitimement la clé du transport choisi (jamais affichée), les routes sont exigées avec leur credential.
    preflight = _real_preflight(env, args.campaign_id, STAGE_LAUNCH if args.action == "run" else args.stage)
    if args.action == "preflight":
        report = preflight
    else:
        deadline = time.monotonic() + CAMPAIGN_BOUNDS.max_seconds
        config = business_config(env)
        adapter = NightlyAdapter(config, _fixture_clients(config.token), bounded_command_runner(deadline))
        report = run_campaign(
            env,
            preflight=preflight,
            launch=lambda r: launch_campaign(r, adapter=adapter, env=env),
            verify=lambda r, ctx: verify_in_container(r, ctx, env=env, deadline_monotonic=deadline),
            read_registry=registry_reader(env),
        )
    if args.output:
        Path(args.output).write_text(report.to_json(), encoding="utf-8")
    text = report.to_human()
    if args.human:
        Path(args.human).write_text(text, encoding="utf-8")
    print(text)
    return report.exit_code()


if __name__ == "__main__":  # pragma: no cover - exercé par le workflow ponctuel
    raise SystemExit(main())
