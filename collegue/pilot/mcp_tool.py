"""Outil MCP du pilote, avec garde stricte (H6, epic #391, Phase 5).

Expose le pilote autonome (``run_project_from_settings``) comme outil MCP — mais
c'est la surface la plus dangereuse du serveur (déclenche Docker, l'agent codeur,
des PR et des écritures GitHub). La garde est donc **stricte** :

- **Hors de ``collegue/tools/``** : l'auto-découverte (``register_tools`` /
  ``discover_tools``) ne scanne que ``collegue/tools/`` → cet outil ne peut PAS être
  enregistré par accident. Seul ``register_pilot_tool`` (appel explicite gaté) le pose.
- **Off par défaut** (``PILOT_TOOL_ENABLED``).
- **Refuse de s'enregistrer sans OAuth** : ``PILOT_TOOL_ENABLED=true`` +
  ``OAUTH_ENABLED=false`` → ``register_pilot_tool`` **lève** (refus de démarrer).
- **Allowlist d'appelants** (sujets OAuth) — vide = personne (fail-closed).
- **``dry_run`` par défaut** sur l'outil. ``app.py`` n'auto-démarre jamais de run.

``run_project_from_settings`` est importé **paresseusement** (l'import de ce module
reste léger, et le pilote/exécuteur ne sont pas tirés tant que l'outil n'est pas appelé).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional, Set

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

PILOT_TOOL_NAME = "pilot_run_project"


def _settings():
    from collegue.config import settings

    return settings


def _as_bool(value: object) -> bool:
    """Coercition booléenne sûre : une CHAÎNE « false »/« 0 »/« no »/« off »/vide est
    fausse (``bool("false")`` vaut True en Python — piège pour un settings non-Pydantic)."""
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "off")
    return bool(value)


class PilotToolRequest(BaseModel):
    """Paramètres d'un run piloté déclenché via MCP."""

    project_id: int = Field(..., description="Id du projet (état durable).")
    repo_source: str = Field(..., description="Dépôt git source (chemin/clone).")
    owner: str = Field(..., description="Owner GitHub cible des PR.")
    repo: str = Field(..., description="Repo GitHub cible des PR.")
    base: str = Field("main", description="Branche de base des PR.")
    dry_run: bool = Field(True, description="Aperçu sans écriture (défaut, sûr).")
    max_iterations: Optional[int] = Field(None, description="Garde-fou anti-boucle.")


class PilotToolResult(BaseModel):
    """Bilan d'un run piloté."""

    stop_reason: str
    iterations: int
    opened_prs: list = Field(default_factory=list)
    project_status: Optional[str] = None
    dry_run: bool = True


class PilotToolError(RuntimeError):
    """Refus / échec d'un appel à l'outil pilote (gate, allowlist, run)."""


@dataclass(frozen=True)
class PilotGateDecision:
    allowed: bool
    reason: str
    misconfigured: bool = False  # PILOT_TOOL_ENABLED sans OAuth → refus DUR de démarrer


def evaluate_pilot_gate(settings: object) -> PilotGateDecision:
    """Décide si l'outil pilote peut être exposé. **Fail-closed**.

    Off → non exposé. On mais ``OAUTH_ENABLED=false`` → ``misconfigured`` (refus dur :
    on n'expose jamais des actions dangereuses sans authentification).
    """
    if not _as_bool(getattr(settings, "PILOT_TOOL_ENABLED", False)):
        return PilotGateDecision(False, "outil pilote désactivé (PILOT_TOOL_ENABLED off)")
    if not _as_bool(getattr(settings, "OAUTH_ENABLED", False)):
        return PilotGateDecision(
            False,
            "PILOT_TOOL_ENABLED=true mais OAUTH_ENABLED=false — refus : pas d'actions "
            "dangereuses (Docker/PR/GitHub) sans authentification",
            misconfigured=True,
        )
    return PilotGateDecision(True, "outil pilote activé (OAuth présent)")


def _allowed_subjects(settings: object) -> Set[str]:
    raw = getattr(settings, "PILOT_TOOL_ALLOWED_SUBJECTS", "") or ""
    items: Iterable[str] = raw if isinstance(raw, (list, tuple, set)) else str(raw).split(",")
    return {str(s).strip() for s in items if str(s).strip()}


def caller_allowed(subject: Optional[str], settings: object) -> bool:
    """``subject`` (sujet OAuth) doit figurer dans l'allowlist. **Fail-closed** :
    allowlist vide → personne n'est autorisé ; ``subject`` vide → refusé."""
    allowed = _allowed_subjects(settings)
    if not allowed:
        return False
    return bool(subject) and subject in allowed


async def _default_run(request: PilotToolRequest, *, ctx, settings: object):
    # Import paresseux : garde ce module léger ; ne tire le pilote/exécuteur qu'à l'appel.
    from collegue.pilot.runtime import run_project_from_settings

    return await run_project_from_settings(
        request.project_id,
        request.repo_source,
        owner=request.owner,
        repo=request.repo,
        base=request.base,
        ctx=ctx,
        dry_run=bool(request.dry_run),
        max_iterations=request.max_iterations,
        settings_obj=settings,
    )


async def run_pilot_tool(
    request: PilotToolRequest,
    *,
    subject: Optional[str],
    settings: object = None,
    ctx: object = None,
    run_fn=None,
    audit: object = None,
) -> PilotToolResult:
    """Vérifie le gate + l'allowlist d'appelant, puis lance le run (``dry_run`` par défaut).

    Lève :class:`PilotToolError` si l'outil n'est pas autorisé (gate off / mauvaise
    config) ou si l'appelant n'est pas dans l'allowlist. ``run_fn`` est injectable (tests).
    """
    settings = settings or _settings()
    gate = evaluate_pilot_gate(settings)
    if not gate.allowed:
        raise PilotToolError(gate.reason)
    if not caller_allowed(subject, settings):
        raise PilotToolError(f"appelant non autorisé pour l'outil pilote: {subject!r}")

    run = run_fn or _default_run
    result = await run(request, ctx=ctx, settings=settings)
    if audit is not None:
        try:
            audit.record(
                "pilot_tool_invoked",
                project_id=request.project_id,
                subject=subject,
                dry_run=bool(request.dry_run),
            )
        except Exception:
            pass
    return PilotToolResult(
        stop_reason=getattr(result, "stop_reason", "unknown"),
        iterations=int(getattr(result, "iterations", 0)),
        opened_prs=list(getattr(result, "opened_prs", []) or []),
        project_status=getattr(result, "project_status", None),
        dry_run=bool(request.dry_run),
    )


def _extract_subject(ctx: object) -> Optional[str]:
    """Sujet OAuth **vérifié** de l'appelant. ``None`` si absent → refusé (fail-closed).

    On n'utilise **jamais** d'en-tête fourni par le client (ex. ``x-user-id``) : un
    en-tête est contrôlé par l'appelant → s'en servir pour autoriser un outil dangereux
    serait une identité **usurpable** (un attaquant mettrait ``x-user-id: <sujet autorisé>``).
    Seul le sujet du token OAuth (authentifié) fait foi ; l'outil n'est de toute façon
    enregistré que si ``OAUTH_ENABLED`` (donc un token est toujours présent).
    """
    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
        subject = getattr(token, "subject", None) or getattr(token, "client_id", None)
        return str(subject) if subject else None
    except Exception:
        return None


def register_pilot_tool(app: object, settings: object = None) -> bool:
    """Enregistre l'outil pilote **si et seulement si** la garde l'autorise.

    Retourne ``True`` s'il a été enregistré, ``False`` s'il est (volontairement) absent.
    **Lève** ``RuntimeError`` si ``PILOT_TOOL_ENABLED=true`` sans OAuth (refus de démarrer
    — ne jamais exposer le pilote sans authentification).
    """
    settings = settings or _settings()
    gate = evaluate_pilot_gate(settings)
    if gate.misconfigured:
        raise RuntimeError(gate.reason)
    if not gate.allowed:
        logger.info("Outil MCP pilote NON enregistré: %s", gate.reason)
        return False

    @app.tool(name=PILOT_TOOL_NAME, description="Pilote autonome (run_project) — OAuth + allowlist requis.")
    async def pilot_run_project(request: PilotToolRequest, ctx) -> PilotToolResult:  # pragma: no cover - endpoint réel
        return await run_pilot_tool(request, subject=_extract_subject(ctx), settings=settings, ctx=ctx)

    logger.info("Outil MCP pilote enregistré (OAuth requis, allowlist d'appelants, dry_run par défaut).")
    return True
