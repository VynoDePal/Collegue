"""Garde post-merge : auto-revert si ``main`` régresse (H3, epic #391, Phase 5).

Filet de sécurité de l'auto-merge (H2) : après un auto-merge, on vérifie la **santé
de ``main``** (tests en sandbox, C8) ; si elle est rouge, on **prépare un revert**
automatique (réutilise H1) et on journalise la décision. Borne le risque de
l'auto-merge — un changement « faible risque » qui casse quand même ``main`` est
annulé sans attendre.

**Fail-closed** : si la mesure de santé est **non concluante** (clone/sandbox
indisponible), on considère ``main`` **non sain** → revert (sécurité > disponibilité).
N'a d'effet que si la politique est active (suit l'auto-merge). Le revert produit est
**local** (branche + commit d'annulation) ; le push + l'ouverture de la PR de revert
relèvent de ``integration`` (comme H1), le merge restant humain (§6).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Optional

from collegue.executor.command import CommandRunner, LocalCommandRunner
from collegue.executor.revert import RevertError, RevertResult, prepare_revert
from collegue.executor.workspace import cleanup_workspace

DEFAULT_HEALTH_COMMAND = "pytest -q"
# Sorties signalant qu'AUCUN test n'a réellement tourné (exit 0 trompeur).
_NO_TEST_MARKERS = ("no tests ran", "collected 0 items", "no tests collected")
# Opérateurs shell qui pourraient masquer un échec (ex. ``pytest || true``) → on refuse.
_SHELL_OPERATORS = (";", "|", "&", "`", "$(", ">", "<", "\n")


@dataclass(frozen=True)
class RevertPolicy:
    """Politique de garde santé et de préparation du revert."""

    enabled: bool = False
    revert_enabled: bool = True
    health_command: str = DEFAULT_HEALTH_COMMAND

    @classmethod
    def from_settings(cls, settings: object) -> "RevertPolicy":
        # Le filet n'a de sens que si l'auto-merge est actif ; activé par défaut quand
        # il l'est (AUTO_REVERT_ENABLED défaut True → désactivable explicitement).
        auto_merge = bool(getattr(settings, "AUTO_MERGE_ENABLED", False))
        auto_revert = bool(getattr(settings, "AUTO_REVERT_ENABLED", True))
        return cls(
            # La santé reste vérifiée dès que l'auto-merge est actif, même si
            # l'opérateur désactive explicitement la préparation du revert.
            enabled=auto_merge,
            revert_enabled=auto_merge and auto_revert,
            health_command=str(
                getattr(settings, "AUTO_REVERT_HEALTH_COMMAND", DEFAULT_HEALTH_COMMAND) or DEFAULT_HEALTH_COMMAND
            ),
        )


@dataclass(frozen=True)
class HealthResult:
    """Santé de ``main`` après un merge."""

    healthy: bool
    reason: str
    exit_code: Optional[int] = None


@dataclass(frozen=True)
class GuardOutcome:
    """Résultat de la garde post-merge."""

    checked: bool
    healthy: Optional[bool] = None
    reverted: bool = False
    revert_failed: bool = False  # main rouge MAIS revert impossible → intervention humaine
    health: Optional[HealthResult] = None
    revert: Optional[RevertResult] = None
    reason: str = ""


def _clone_main(repo_source: str, *, runner: CommandRunner, git_bin: str) -> str:
    source = os.path.realpath(os.path.abspath(repo_source))
    if not os.path.isdir(os.path.join(source, ".git")):
        raise RuntimeError(f"repo_source n'est pas un dépôt git: {repo_source}")
    parent = tempfile.mkdtemp(prefix="collegue-health-")
    dest = os.path.join(parent, "workspace")
    clone = runner.run_command([git_bin, "clone", "--quiet", source, dest], parent)
    if not clone.ok:
        raise RuntimeError(f"git clone a échoué: {clone.stderr.strip() or clone.stdout.strip()}")
    return dest


def check_main_health(
    repo_source: str,
    *,
    sandbox: object,
    command: str = DEFAULT_HEALTH_COMMAND,
    merge_sha: Optional[str] = None,
    runner: Optional[CommandRunner] = None,
    git_bin: str = "git",
) -> HealthResult:
    """Clone ``main`` et lance les tests en sandbox. **Fail-closed** : tout résultat
    non concluant → ``healthy=False``.

    Non concluant inclut : commande de santé non sûre (opérateurs shell pouvant masquer
    un échec), clone/sandbox en erreur, pas de résultat, clone ne contenant pas
    ``merge_sha`` (on testerait le mauvais arbre), et **sortie sans test exécuté**
    (exit 0 trompeur : « collected 0 items »…). Le clone est toujours nettoyé.
    """
    runner = runner or LocalCommandRunner()
    if not command or any(op in command for op in _SHELL_OPERATORS):
        return HealthResult(False, "commande de santé non sûre (opérateurs shell) — non concluant")
    try:
        workspace = _clone_main(repo_source, runner=runner, git_bin=git_bin)
    except Exception as exc:  # clone impossible → on ne peut pas affirmer la santé
        return HealthResult(False, f"clone de main impossible: {exc}")
    parent = os.path.dirname(workspace)
    try:
        # Le clone DOIT contenir le commit mergé, sinon on testerait un arbre périmé
        # (source pré-merge) et un « vert » ne dirait rien de la vraie main.
        if merge_sha is not None:
            present = runner.run_command([git_bin, "cat-file", "-e", f"{merge_sha}^{{commit}}"], workspace)
            if not present.ok:
                return HealthResult(False, "le clone ne contient pas le commit mergé — non concluant")
            checkout = runner.run_command([git_bin, "checkout", "--detach", merge_sha], workspace)
            if not checkout.ok:
                return HealthResult(False, "impossible de positionner la garde sur le commit mergé — non concluant")
            exact = runner.run_command([git_bin, "rev-parse", "HEAD"], workspace)
            if not exact.ok or exact.stdout.strip() != merge_sha:
                return HealthResult(False, "HEAD de la garde différent du commit mergé — non concluant")
        try:
            result = sandbox.run_tests(workspace, command)
        except Exception as exc:  # sandbox indisponible → non concluant → non sain
            return HealthResult(False, f"sandbox indisponible: {exc}")
        if result is None:
            return HealthResult(False, "résultat de test indisponible — non concluant")
        exit_code = getattr(result, "exit_code", None)
        if not bool(getattr(result, "ok", False)):
            return HealthResult(False, "tests rouges sur main", exit_code)
        # Exit 0 mais aucun test réellement exécuté → on ne déclare PAS la main saine.
        output = f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}".lower()
        if any(marker in output for marker in _NO_TEST_MARKERS):
            return HealthResult(False, "aucun test exécuté (exit 0 trompeur) — non concluant", exit_code)
        return HealthResult(True, "main vert", exit_code)
    finally:
        shutil.rmtree(parent, ignore_errors=True)  # pas de fuite de clones en boucle longue


def guard_post_merge(
    repo_source: str,
    merge_sha: str,
    *,
    sandbox: object,
    policy: RevertPolicy,
    merge_parent: Optional[int] = None,
    manager: Optional[object] = None,
    project_id: Optional[int] = None,
    audit: Optional[object] = None,
    runner: Optional[CommandRunner] = None,
    git_bin: str = "git",
) -> GuardOutcome:
    """Vérifie la santé de ``main`` après un merge et prépare un revert si elle est rouge.

    Désactivé (politique off) → ``checked=False`` (rien). Sain → aucun revert. Rouge
    ou **non concluant** (fail-closed) → ``prepare_revert`` (H1, local), décision
    journalisée + événement d'audit (H4). Le push + la PR de revert sont ``integration``.
    """
    if not policy.enabled:
        return GuardOutcome(checked=False, reason="auto-revert désactivé (politique off)")

    health = check_main_health(
        repo_source, sandbox=sandbox, command=policy.health_command, merge_sha=merge_sha, runner=runner, git_bin=git_bin
    )
    if health.healthy:
        return GuardOutcome(checked=True, healthy=True, health=health, reason="main sain — aucun revert")

    # Rouge / non concluant → revert (fail-closed). Le revert lui-même peut échouer
    # (SHA invalide, commit de merge sans ``merge_parent``, source sans le commit) :
    # ce cas — main rouge ET revert impossible — est le plus critique et doit
    # ESCALADER vers un humain, pas passer pour un succès.
    revert: Optional[RevertResult] = None
    command_runner = runner or LocalCommandRunner()
    source_head = command_runner.run_command([git_bin, "rev-parse", "HEAD"], repo_source)
    source_is_exact = bool(source_head.ok and source_head.stdout.strip() == merge_sha)
    if policy.revert_enabled and source_is_exact:
        try:
            revert = prepare_revert(
                repo_source,
                merge_sha,
                merge_parent=merge_parent,
                runner=command_runner,
                git_bin=git_bin,
            )
        except RevertError:
            revert = None
    reverted = bool(revert and revert.reverted)
    revert_failed = bool(policy.revert_enabled and not reverted)
    branch = getattr(revert, "branch", None)
    if revert_failed and revert is not None and getattr(revert, "workspace", None):
        # #466 : un revert en échec (abort, workspace laissé propre) n'a produit
        # AUCUNE branche utile — son clone est purgé au lieu de fuir dans /tmp.
        # Un revert RÉUSSI garde le sien : sa branche locale est le livrable
        # (push humain / H3) ; le balayage d'ancienneté le ramassera au-delà.
        cleanup_workspace(revert.workspace)
    if not policy.revert_enabled:
        summary = f"Main rouge après merge {merge_sha[:12]} — revert désactivé, intervention requise ({health.reason})"
        audit_kind = "auto_revert_disabled"
    elif not source_is_exact:
        summary = f"ÉCHEC du revert de {merge_sha[:12]} — source non resynchronisée, intervention requise"
        audit_kind = "auto_revert_failed"
    elif revert_failed:
        summary = f"ÉCHEC du revert de {merge_sha[:12]} — intervention humaine requise ({health.reason})"
        audit_kind = "auto_revert_failed"
    else:
        summary = f"Auto-revert: main rouge après merge {merge_sha[:12]} ({health.reason})"
        audit_kind = "auto_revert"
    if manager is not None and project_id is not None:
        try:
            manager.record_decision(project_id, summary, rationale=f"branche de revert: {branch}")
        except Exception:
            pass  # journalisation best-effort, ne bloque pas la garde
    if audit is not None:
        try:
            audit.record(audit_kind, merge_sha=merge_sha, reason=health.reason, reverted=reverted, branch=branch)
        except Exception:
            pass
    return GuardOutcome(
        checked=True,
        healthy=False,
        reverted=reverted,
        revert_failed=revert_failed,
        health=health,
        revert=revert,
        reason=summary,
    )
