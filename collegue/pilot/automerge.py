"""Moteur de politique d'auto-merge (H2, epic #391, Phase 5).

§6 reste le **défaut** : approbation humaine avant chaque merge dans ``main``.
L'auto-merge ne s'active **que** si ``AUTO_MERGE_ENABLED`` est vrai **et** le diff
passe une allowlist **stricte** de faible risque. Sinon : « propose seulement » (la
PR reste pour un humain). **Fail-closed** : tout doute / information manquante (pas
de fichiers, CI inconnue/en attente) → pas d'auto-merge.

Module **isolé** : ne déclenche rien tout seul — c'est l'appelant (pilote / garde H3)
qui décide quand l'invoquer. ``maybe_auto_merge`` est ``dry_run`` par défaut et
n'effectue le merge réel (via H1 ``merge_pr``) que sur autorisation explicite.
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from collegue.pilot.audit import AUTOMERGE_DECISION

# Faible risque = **données / balisage non exécutable** uniquement. On exclut
# volontairement ``tests/**`` du défaut : du code de test est exécuté en CI (avec des
# identifiants) — ``conftest.py`` est même importé automatiquement par pytest → RCE.
DEFAULT_PATH_ALLOWLIST: Tuple[str, ...] = ("**/*.md", "**/*.rst", "docs/**")
DEFAULT_MAX_LOC = 50
_GREEN = "success"

# Extensions de **code/exécutable/config** : toujours bloquées (même si un opérateur
# les met dans l'allowlist). « Faible risque » exclut tout ce qui s'exécute.
_BLOCKED_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".sh",
        ".bash",
        ".zsh",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".rb",
        ".go",
        ".rs",
        ".php",
        ".pl",
        ".ps1",
        ".bat",
        ".cmd",
        ".lua",
        ".java",
        ".c",
        ".cpp",
        ".cc",
        ".h",
        ".hpp",
        ".yml",
        ".yaml",
        ".toml",
        ".cfg",
        ".ini",
        ".tf",
        ".sql",
    }
)
# Basenames exécutés/sensibles indépendamment de l'extension.
_BLOCKED_BASENAMES = frozenset({"dockerfile", "makefile", "conftest.py", "setup.py", "setup.cfg", "pyproject.toml"})


@dataclass(frozen=True)
class AutoMergeDecision:
    """Verdict d'auto-merge pour une PR (toujours accompagné d'une raison)."""

    allowed: bool
    reason: str


@dataclass(frozen=True)
class RiskPolicy:
    """Politique de risque de l'auto-merge (configurable via settings, off par défaut)."""

    enabled: bool = False
    max_loc: int = DEFAULT_MAX_LOC
    path_allowlist: Tuple[str, ...] = DEFAULT_PATH_ALLOWLIST
    method: str = "squash"

    @classmethod
    def from_settings(cls, settings: object) -> "RiskPolicy":
        allowlist = _parse_allowlist(getattr(settings, "AUTO_MERGE_PATH_ALLOWLIST", None))
        raw_loc = int(getattr(settings, "AUTO_MERGE_MAX_LOC", DEFAULT_MAX_LOC) or 0)
        # ``<= 0`` ne signifie PAS « illimité » : un opérateur qui laisse le champ vide
        # (ou met 0) croit RESSERRER la politique — on retombe sur le défaut, jamais sur
        # « pas de plafond ».
        return cls(
            enabled=bool(getattr(settings, "AUTO_MERGE_ENABLED", False)),
            max_loc=raw_loc if raw_loc > 0 else DEFAULT_MAX_LOC,
            path_allowlist=allowlist or DEFAULT_PATH_ALLOWLIST,
            method=str(getattr(settings, "AUTO_MERGE_METHOD", "squash") or "squash"),
        )


def _parse_allowlist(raw: object) -> Tuple[str, ...]:
    if raw is None:
        return DEFAULT_PATH_ALLOWLIST
    items = (
        [str(x).strip() for x in raw] if isinstance(raw, (list, tuple)) else [p.strip() for p in str(raw).split(",")]
    )
    return tuple(p for p in items if p)


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").lstrip("/")


def _seg_match(path_segs: List[str], pat_segs: List[str]) -> bool:
    """Match glob **segment par segment** : ``**`` = 0+ segments, ``*`` reste dans un
    segment (ne traverse PAS ``/``). Évite qu'un ``*`` global déborde de la portée."""
    if not pat_segs:
        return not path_segs
    head, rest = pat_segs[0], pat_segs[1:]
    if head == "**":
        # zéro segment consommé, ou un de plus en gardant le '**'.
        if _seg_match(path_segs, rest):
            return True
        return bool(path_segs) and _seg_match(path_segs[1:], pat_segs)
    if not path_segs:
        return False
    # fnmatch sur UN segment : '*' n'y rencontre pas de '/'.
    if fnmatch.fnmatch(path_segs[0], head):
        return _seg_match(path_segs[1:], rest)
    return False


def _match_allowlist(path: str, patterns: Sequence[str]) -> bool:
    segs = [s for s in _norm(path).split("/") if s]
    for pattern in patterns:
        if _seg_match(segs, [s for s in str(pattern).split("/") if s]):
            return True
    return False


def is_sensitive(path: str) -> bool:
    """Fichiers **toujours** bloqués (même dans l'allowlist) : garde dure, non configurable.

    Insensible à la casse. Bloque : traversée (``..``), secrets (``.env*``), lockfiles
    (``*.lock``), config/CI (``.github/`` à n'importe quelle profondeur), migrations
    (``migrations/`` ou ``alembic/versions/``), et tout **code/exécutable/config**
    (extension dans ``_BLOCKED_EXTENSIONS`` ou basename dans ``_BLOCKED_BASENAMES``) —
    « faible risque » exclut tout ce qui s'exécute (ex. ``tests/conftest.py`` = RCE CI).
    """
    p = _norm(path).lower()
    segments = [s for s in p.split("/") if s]
    if ".." in segments:
        return True
    base = segments[-1] if segments else ""
    if base.startswith(".env") or base.endswith(".lock"):
        return True
    if ".github" in segments:
        return True
    if "migrations" in segments:
        return True
    if "alembic" in segments and "versions" in segments:
        return True
    if base in _BLOCKED_BASENAMES:
        return True
    dot = base.rfind(".")
    if dot > 0 and base[dot:] in _BLOCKED_EXTENSIONS:
        return True
    return False


def _checks_all_green(checks: Optional[Sequence[str]]) -> bool:
    # None / vide → état inconnu → fail-closed. Tout doit valoir "success" (refuse
    # "pending"/"failure"/conclusion manquante).
    if not checks:
        return False
    return all(str(c).strip().lower() == _GREEN for c in checks)


def evaluate_automerge(
    files_changed: Sequence[str],
    *,
    additions: int = 0,
    deletions: int = 0,
    checks: Optional[Sequence[str]] = None,
    policy: RiskPolicy,
    files_complete: bool = True,
) -> AutoMergeDecision:
    """Décide si une PR peut être auto-mergée. **Toutes** les conditions sont requises.

    Ordre (fail-closed) : flag activé → plafond LOC configuré → liste de fichiers
    complète → diff non vide → aucun fichier sensible → tous dans l'allowlist → LOC
    valide et sous plafond → toutes les vérifs CI vertes.

    ``files_complete=False`` (l'appelant n'a pas pu récupérer TOUS les fichiers, ex.
    PR > 100 fichiers non paginée) → refus : on ne juge pas faible-risque un diff
    partiellement vu. ``checks`` doit être l'ensemble **complet** des vérifs requises
    (la complétude est de la responsabilité de l'appelant — cf. branch protection).
    """
    if not policy.enabled:
        return AutoMergeDecision(False, "auto-merge désactivé (AUTO_MERGE_ENABLED off) — merge humain (§6)")
    if policy.method not in {"squash", "merge"}:
        # ``rebase`` peut intégrer plusieurs commits Contents API ; le SHA renvoyé
        # ne désigne alors que le dernier et n'est pas réversible atomiquement.
        return AutoMergeDecision(False, "méthode rebase/non supportée : rollback atomique non prouvable")
    if policy.max_loc <= 0:
        return AutoMergeDecision(False, "plafond LOC non configuré (<= 0) — fail-closed")
    if not files_complete:
        return AutoMergeDecision(False, "liste de fichiers incomplète (diff tronqué) — fail-closed")
    files = [f for f in (files_changed or []) if f and str(f).strip()]
    if not files:
        return AutoMergeDecision(False, "aucun fichier au diff (état inconnu) — fail-closed")
    for f in files:
        if is_sensitive(f):
            return AutoMergeDecision(False, f"fichier sensible interdit à l'auto-merge: {f}")
    outside = [f for f in files if not _match_allowlist(f, policy.path_allowlist)]
    if outside:
        return AutoMergeDecision(False, f"hors allowlist de faible risque: {', '.join(outside[:3])}")
    add, dele = int(additions or 0), int(deletions or 0)
    if add < 0 or dele < 0:
        return AutoMergeDecision(False, "compte de lignes négatif (malformé) — fail-closed")
    loc = add + dele
    if loc > policy.max_loc:
        return AutoMergeDecision(False, f"trop volumineux: {loc} LOC > plafond {policy.max_loc}")
    if not _checks_all_green(checks):
        return AutoMergeDecision(False, "vérifications CI non toutes vertes (ou en attente/inconnues) — fail-closed")
    return AutoMergeDecision(True, f"faible risque: {len(files)} fichier(s), {loc} LOC, CI verte")


@dataclass(frozen=True)
class AutoMergeOutcome:
    """Résultat d'une tentative d'auto-merge (décision + effet éventuel)."""

    decision: AutoMergeDecision
    merged: bool = False
    dry_run: bool = False
    merge_result: Optional[object] = None


@dataclass(frozen=True)
class PromotionAutoMergeOutcome:
    """Verdict complet Phase 5 pour une promotion de la boucle improve."""

    merged: bool
    continue_loop: bool
    stop_reason: Optional[str]
    reason: str
    automerge: Optional[AutoMergeOutcome] = None
    guard: Optional[object] = None
    remote_revert: Optional[object] = None


def maybe_auto_merge(
    pr: object,
    files_changed: Sequence[str],
    *,
    additions: int = 0,
    deletions: int = 0,
    checks: Optional[Sequence[str]] = None,
    policy: RiskPolicy,
    clients: object,
    owner: str,
    repo: str,
    dry_run: bool = True,
    files_complete: bool = True,
    audit: object = None,
) -> AutoMergeOutcome:
    """Évalue puis (si autorisé **et** pas ``dry_run``) merge via H1 ``merge_pr``.

    Refusé ou ``dry_run`` → **aucun** merge (la PR reste ouverte pour un humain). Le
    merge réel **exige** un SHA de tête connu (``pr.head_sha``/``pr.sha``) : la garde
    anti-course de H1 est précisément ce qui justifie d'automatiser le merge — sans
    elle, un commit poussé entre l'évaluation et le merge passerait inaperçu → refus.

    ``audit`` (H4) : si fourni, chaque décision émet un événement ``automerge_decision``
    (tracé/auditable, visible au dashboard) — best-effort, ne casse jamais le flux.
    """

    def _emit(**detail):
        if audit is not None:
            try:
                audit.record(AUTOMERGE_DECISION, **detail)
            except Exception:
                pass

    decision = evaluate_automerge(
        files_changed,
        additions=additions,
        deletions=deletions,
        checks=checks,
        policy=policy,
        files_complete=files_complete,
    )
    pr_number = getattr(pr, "number", None)
    if not decision.allowed:
        _emit(pr_number=pr_number, allowed=False, merged=False, reason=decision.reason)
        return AutoMergeOutcome(decision=decision, merged=False)
    if dry_run:
        _emit(pr_number=pr_number, allowed=True, merged=False, dry_run=True, reason=decision.reason)
        return AutoMergeOutcome(decision=decision, merged=False, dry_run=True)
    expected = getattr(pr, "head_sha", None) or getattr(pr, "sha", None)
    if not expected:
        refused = AutoMergeDecision(False, "SHA de tête inconnu — garde anti-course requise pour l'auto-merge")
        _emit(pr_number=pr_number, allowed=False, merged=False, reason=refused.reason)
        return AutoMergeOutcome(decision=refused, merged=False)
    result = clients.prs.merge_pr(
        owner,
        repo,
        pr.number,
        method=policy.method,
        expected_head_sha=expected,
        expected_base_branch=getattr(pr, "base_branch", None),
        expected_base_sha=getattr(pr, "base_sha", None),
    )
    merged = bool(getattr(result, "merged", False))
    _emit(pr_number=pr_number, allowed=True, merged=merged, reason=decision.reason)
    return AutoMergeOutcome(decision=decision, merged=merged, merge_result=result)


_PENDING_CHECK_STATES = frozenset({"pending", "queued", "in_progress", "requested", "waiting"})
_INCIDENT_UNSET = object()
_REVERT_LEASE_GRACE_SECONDS = 3600.0


async def _sleep(value: float, sleep_fn: Callable[[float], object]) -> None:
    result = sleep_fn(max(0.0, float(value)))
    if inspect.isawaitable(result):
        await result


async def auto_merge_promotion(
    pr: object,
    *,
    policy: RiskPolicy,
    revert_policy: object,
    clients: object,
    owner: str,
    repo: str,
    repo_source: str,
    base: str,
    sandbox: object,
    manager: object = None,
    project_id: Optional[int] = None,
    audit: object = None,
    dry_run: bool = False,
    ci_timeout_seconds: float = 900.0,
    ci_poll_seconds: float = 10.0,
    sleep_fn: Callable[[float], object] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    sync_base_fn: Optional[Callable[[str, str], bool]] = None,
    guard_fn: Optional[Callable[..., object]] = None,
    remote_revert_fn: Optional[Callable[..., object]] = None,
    continue_fn: Optional[Callable[[], object]] = None,
) -> PromotionAutoMergeOutcome:
    """Tente l'auto-merge d'une PR Phase 4 puis vérifie la santé de ``main``.

    Le chemin est strictement séquentiel : contexte PR exhaustif, tête immobile,
    CI complètement verte, merge protégé par SHA, resync de la base, puis garde
    post-merge. Tout refus laisse la PR ouverte et arrête la boucle d'amélioration
    afin qu'aucune PR enfant ne soit mergée dans une branche non intégrée.

    Politique off ou dry-run : aucun appel GitHub, aucun effet.
    """

    def blocked(reason: str, *, stop_reason: str = "auto_merge_blocked", merged: bool = False, **kwargs):
        if audit is not None:
            try:
                audit.record(
                    AUTOMERGE_DECISION,
                    pr_number=getattr(pr, "number", None),
                    allowed=False,
                    merged=merged,
                    reason=reason,
                )
            except Exception:
                pass
        return PromotionAutoMergeOutcome(
            merged=merged,
            continue_loop=False,
            stop_reason=stop_reason,
            reason=reason,
            **kwargs,
        )

    incident = None

    def incident_transition(new_state: str, *, merge_sha_value=_INCIDENT_UNSET, last_error=None):
        nonlocal incident
        if incident is None:
            raise RuntimeError("incident Phase 5 absent")
        params = {
            "expected_state": incident.state,
            "expected_revision": incident.revision,
            "expected_source_pr_number": incident.source_pr_number,
            "expected_source_head_sha": incident.source_head_sha,
            "new_state": new_state,
            "last_error": last_error,
        }
        if merge_sha_value is not _INCIDENT_UNSET:
            params["merge_sha"] = merge_sha_value
        if getattr(incident, "state", None) == "revert_in_progress":
            params["expected_revert_claim_token"] = incident.revert_claim_token
        incident = manager.transition_phase5_incident(project_id, **params)
        return incident

    def incident_attention(message: str) -> None:
        if incident is None or getattr(incident, "state", None) == "attention":
            return
        try:
            incident_transition("attention", last_error=message)
        except Exception:
            pass

    def incident_clear() -> None:
        nonlocal incident
        if incident is None:
            raise RuntimeError("incident Phase 5 absent")
        manager.clear_phase5_incident(
            project_id,
            expected_state=incident.state,
            expected_revision=incident.revision,
            expected_source_pr_number=incident.source_pr_number,
            expected_source_head_sha=incident.source_head_sha,
        )
        incident = None

    if not policy.enabled:
        return PromotionAutoMergeOutcome(False, True, None, "auto-merge désactivé")
    if dry_run:
        return PromotionAutoMergeOutcome(False, True, None, "dry-run : aucun auto-merge")
    if not bool(getattr(revert_policy, "enabled", False)):
        return blocked("garde post-merge désactivée — auto-merge refusé (fail-closed)")
    number = getattr(pr, "number", None)
    if number is None:
        return blocked("numéro de PR absent — contexte invérifiable")

    prs = getattr(clients, "prs", None)
    branches = getattr(clients, "branches", None)
    if prs is None or branches is None:
        return blocked("clients GitHub PR/branches absents — contexte invérifiable")
    try:
        current = prs.get_pr(owner, repo, int(number))
    except Exception as exc:  # noqa: BLE001 - frontière réseau fail-closed
        return blocked(f"lecture de la PR impossible: {exc}")
    head_sha = getattr(current, "head_sha", None)
    if not head_sha:
        return blocked("SHA de tête inconnu — garde anti-course impossible")
    base_sha = getattr(current, "base_sha", None)
    if not base_sha:
        return blocked("SHA de base inconnu — diff non stabilisé")
    if getattr(current, "state", None) != "open" or bool(getattr(current, "draft", False)):
        return blocked("PR fermée ou encore en draft — auto-merge interdit")
    if getattr(current, "base_branch", None) != base:
        return blocked(f"base de PR inattendue ({getattr(current, 'base_branch', None)!r} != {base!r})")
    raw_stats = (
        getattr(current, "additions", None),
        getattr(current, "deletions", None),
        getattr(current, "changed_files", None),
    )
    if any(value is None for value in raw_stats):
        return blocked("statistiques de PR absentes — diff invérifiable")
    try:
        expected_additions, expected_deletions, expected_files = (int(value) for value in raw_stats)
    except (TypeError, ValueError):
        return blocked("statistiques de PR malformées — diff invérifiable")
    if expected_additions < 0 or expected_deletions < 0 or expected_files < 0:
        return blocked("statistiques de PR négatives — diff invérifiable")

    try:
        files_snapshot = prs.get_pr_files_snapshot(
            owner,
            repo,
            int(number),
            expected_count=expected_files,
        )
    except Exception as exc:  # noqa: BLE001 - frontière réseau fail-closed
        return blocked(f"lecture exhaustive du diff impossible: {exc}")
    files = list(getattr(files_snapshot, "files", ()) or ())
    filenames = [str(getattr(item, "filename", "")) for item in files]
    additions = sum(int(getattr(item, "additions", 0) or 0) for item in files)
    deletions = sum(int(getattr(item, "deletions", 0) or 0) for item in files)
    files_complete = bool(getattr(files_snapshot, "complete", False))
    if additions != expected_additions or deletions != expected_deletions:
        files_complete = False

    # Évalue d'abord le risque intrinsèque avec une CI fictivement verte. Un diff
    # code/sensible ne justifie pas d'attendre plusieurs minutes les checks.
    preflight = evaluate_automerge(
        filenames,
        additions=additions,
        deletions=deletions,
        checks=[_GREEN],
        policy=policy,
        files_complete=files_complete,
    )
    if not preflight.allowed:
        return blocked(preflight.reason)

    deadline = clock() + max(0.0, float(ci_timeout_seconds))
    checks: Sequence[str] = ()
    while True:
        if continue_fn is not None:
            try:
                continuation = continue_fn()
            except Exception as exc:  # noqa: BLE001 - contrôleur indisponible
                return blocked(f"contrôle budget/deadline impossible: {exc}")
            if not bool(getattr(continuation, "ok", continuation)):
                reason = str(getattr(continuation, "reason", "budget ou deadline atteint"))
                return blocked(f"attente CI interrompue: {reason}")
        try:
            observed = prs.get_pr(owner, repo, int(number))
            if getattr(observed, "head_sha", None) != head_sha:
                return blocked("la tête de la PR a bougé pendant l'attente CI — refus anti-course")
            if getattr(observed, "base_sha", None) != base_sha:
                return blocked("la base de la PR a bougé pendant l'attente CI — diff à réévaluer")
            observed_stats = (
                getattr(observed, "additions", None),
                getattr(observed, "deletions", None),
                getattr(observed, "changed_files", None),
            )
            if observed_stats != raw_stats:
                return blocked("les statistiques de la PR ont changé pendant l'attente CI")
            if getattr(observed, "state", None) != "open" or bool(getattr(observed, "draft", False)):
                return blocked("état de la PR modifié pendant l'attente CI")
            check_snapshot = prs.get_commit_checks(owner, repo, head_sha)
        except Exception as exc:  # noqa: BLE001 - frontière réseau fail-closed
            return blocked(f"lecture des vérifications CI impossible: {exc}")
        if not bool(getattr(check_snapshot, "complete", False)):
            return blocked("liste des vérifications CI incomplète — fail-closed")
        checks = tuple(str(state).strip().lower() for state in (getattr(check_snapshot, "states", ()) or ()))
        if checks and all(state == _GREEN for state in checks):
            current = observed
            break
        terminal = [state for state in checks if state != _GREEN and state not in _PENDING_CHECK_STATES]
        if terminal:
            return blocked(f"CI non verte: {', '.join(terminal[:3])}")
        if clock() >= deadline:
            return blocked("délai d'attente CI dépassé (checks absents ou pending)")
        await _sleep(ci_poll_seconds, sleep_fn)

    required_state_methods = (
        "begin_phase5_incident",
        "claim_phase5_revert",
        "transition_phase5_incident",
        "clear_phase5_incident",
    )
    if manager is None or project_id is None or not all(hasattr(manager, name) for name in required_state_methods):
        return blocked(
            "état durable Phase 5 indisponible — merge refusé avant toute écriture",
            stop_reason="phase5_incident_pending",
        )
    try:
        incident = manager.begin_phase5_incident(
            project_id,
            owner=owner,
            repo=repo,
            base_branch=base,
            source_pr_number=int(number),
            source_head_sha=head_sha,
            base_sha_before_merge=base_sha,
            merge_method=policy.method,
            health_command=str(getattr(revert_policy, "health_command", "") or "pytest -q"),
            revert_enabled=bool(getattr(revert_policy, "revert_enabled", False)),
        )
    except Exception as exc:  # write-ahead obligatoire avant le merge GitHub
        return blocked(
            f"écriture write-ahead Phase 5 impossible: {exc}",
            stop_reason="phase5_incident_pending",
        )
    if getattr(incident, "state", None) != "merge_pending":
        return blocked(
            f"incident Phase 5 existant à reprendre ({getattr(incident, 'state', 'inconnu')})",
            stop_reason="phase5_incident_pending",
        )

    try:
        merge_outcome = maybe_auto_merge(
            current,
            filenames,
            additions=additions,
            deletions=deletions,
            checks=checks,
            policy=policy,
            clients=clients,
            owner=owner,
            repo=repo,
            dry_run=False,
            files_complete=files_complete,
            audit=audit,
        )
    except Exception as exc:  # noqa: BLE001 - GitHub refuse : PR laissée à l'humain
        # Réponse réseau ambiguë : le write-ahead reste en merge_pending. Une
        # reprise relira GitHub avant toute nouvelle amélioration.
        return blocked(f"merge GitHub non confirmé: {exc}", stop_reason="phase5_incident_pending")
    if not merge_outcome.merged:
        try:
            incident_clear()
        except Exception as exc:
            return blocked(f"merge refusé et incident impossible à clore: {exc}", stop_reason="phase5_incident_pending")
        return blocked(merge_outcome.decision.reason, automerge=merge_outcome)
    merge_sha = getattr(merge_outcome.merge_result, "sha", None)
    if not merge_sha:
        incident_attention("merge confirmé sans SHA")
        return blocked(
            "merge confirmé sans SHA — garde post-merge impossible",
            stop_reason="post_merge_guard_failed",
            merged=True,
            automerge=merge_outcome,
        )
    try:
        incident_transition("health_pending", merge_sha_value=merge_sha)
    except Exception as exc:
        return blocked(
            f"merge réussi mais transition durable health_pending impossible: {exc}",
            stop_reason="phase5_incident_pending",
            merged=True,
            automerge=merge_outcome,
        )

    try:
        remote_main_sha = branches.get_branch_sha(owner, repo, base)
    except Exception as exc:
        try:
            incident_transition("health_pending", last_error=f"HEAD post-merge illisible: {exc}")
        except Exception:
            pass
        return blocked(
            f"tête distante post-merge invérifiable: {exc}",
            stop_reason="post_merge_guard_failed",
            merged=True,
            automerge=merge_outcome,
        )
    if remote_main_sha != merge_sha:
        incident_attention("main a avancé après l'auto-merge")
        return blocked(
            "main a avancé après l'auto-merge",
            stop_reason="post_merge_guard_failed",
            merged=True,
            automerge=merge_outcome,
        )

    if sync_base_fn is None:
        from collegue.executor.workspace import resync_repository_base

        sync_base_fn = resync_repository_base
    try:
        synced = bool(sync_base_fn(repo_source, base))
    except Exception as exc:  # noqa: BLE001 - plomberie git fail-closed
        try:
            incident_transition("health_pending", last_error=f"resynchronisation post-merge impossible: {exc}")
        except Exception:
            pass
        return blocked(
            f"resynchronisation post-merge impossible: {exc}",
            stop_reason="post_merge_guard_failed",
            merged=True,
            automerge=merge_outcome,
        )
    if not synced:
        try:
            incident_transition("health_pending", last_error="resynchronisation post-merge échouée")
        except Exception:
            pass
        return blocked(
            "resynchronisation post-merge échouée",
            stop_reason="post_merge_guard_failed",
            merged=True,
            automerge=merge_outcome,
        )
    try:
        remote_main_sha = branches.get_branch_sha(owner, repo, base)
    except Exception as exc:
        try:
            incident_transition("health_pending", last_error=f"HEAD avant garde illisible: {exc}")
        except Exception:
            pass
        return blocked(
            f"tête distante avant garde invérifiable: {exc}",
            stop_reason="post_merge_guard_failed",
            merged=True,
            automerge=merge_outcome,
        )
    if remote_main_sha != merge_sha:
        incident_attention("main a avancé pendant la resynchronisation")
        return blocked(
            "main a avancé pendant la resynchronisation",
            stop_reason="post_merge_guard_failed",
            merged=True,
            automerge=merge_outcome,
        )

    if guard_fn is None:
        from collegue.pilot.guard import guard_post_merge

        guard_fn = guard_post_merge
    try:
        guard = guard_fn(
            repo_source,
            merge_sha,
            sandbox=sandbox,
            policy=revert_policy,
            merge_parent=1 if policy.method == "merge" else None,
            manager=manager,
            project_id=project_id,
            audit=audit,
        )
        if inspect.isawaitable(guard):
            guard = await guard
    except Exception as exc:  # noqa: BLE001 - santé non prouvée
        try:
            incident_transition("health_pending", last_error=f"garde post-merge impossible: {exc}")
        except Exception:
            pass
        return blocked(
            f"garde post-merge impossible: {exc}",
            stop_reason="post_merge_guard_failed",
            merged=True,
            automerge=merge_outcome,
        )
    try:
        remote_main_sha = branches.get_branch_sha(owner, repo, base)
    except Exception as exc:
        try:
            incident_transition("health_pending", last_error=f"HEAD après garde illisible: {exc}")
        except Exception:
            pass
        return blocked(
            f"tête distante après garde invérifiable: {exc}",
            stop_reason="post_merge_guard_failed",
            merged=True,
            automerge=merge_outcome,
            guard=guard,
        )
    if remote_main_sha != merge_sha:
        incident_attention("main a avancé pendant la garde de santé")
        return blocked(
            "main a avancé pendant la garde de santé",
            stop_reason="post_merge_guard_failed",
            merged=True,
            automerge=merge_outcome,
            guard=guard,
        )
    if bool(getattr(guard, "checked", False)) and getattr(guard, "healthy", None) is not True:
        local_revert = getattr(guard, "revert", None)
        if (
            bool(getattr(revert_policy, "revert_enabled", False))
            and bool(getattr(guard, "reverted", False))
            and local_revert is not None
        ):
            try:
                incident_transition("revert_pending", last_error=getattr(guard, "reason", "main rouge"))
                incident = manager.claim_phase5_revert(
                    project_id,
                    expected_state=incident.state,
                    expected_revision=incident.revision,
                    expected_source_pr_number=incident.source_pr_number,
                    expected_source_head_sha=incident.source_head_sha,
                    lease_seconds=max(300.0, float(ci_timeout_seconds) + _REVERT_LEASE_GRACE_SECONDS),
                )
            except Exception as exc:
                incident_attention(f"transition revert_pending impossible: {exc}")
                return blocked(
                    f"revert local prêt mais transition durable impossible: {exc}",
                    stop_reason="phase5_incident_pending",
                    merged=True,
                    automerge=merge_outcome,
                    guard=guard,
                )
            if remote_revert_fn is None:
                from collegue.pilot.remote_revert import publish_and_merge_revert

                remote_revert_fn = publish_and_merge_revert
            try:
                remote_revert = remote_revert_fn(
                    local_revert,
                    merge_sha,
                    base_sha,
                    merge_method=policy.method,
                    enabled=True,
                    clients=clients,
                    owner=owner,
                    repo=repo,
                    base=base,
                    repo_source=repo_source,
                    sandbox=sandbox,
                    health_command=str(getattr(revert_policy, "health_command", "") or "pytest -q"),
                    reason=getattr(guard, "reason", ""),
                    manager=manager,
                    project_id=project_id,
                    audit=audit,
                    dry_run=False,
                    ci_timeout_seconds=ci_timeout_seconds,
                    ci_poll_seconds=ci_poll_seconds,
                    sleep_fn=sleep_fn,
                    clock=clock,
                    sync_base_fn=sync_base_fn,
                )
                if inspect.isawaitable(remote_revert):
                    remote_revert = await remote_revert
            except Exception as exc:  # noqa: BLE001 - récupération fail-closed
                try:
                    incident_transition("revert_pending", last_error=f"rollback distant impossible: {exc}")
                except Exception:
                    pass
                return blocked(
                    f"rollback distant impossible: {exc}",
                    stop_reason="auto_revert_publish_failed",
                    merged=True,
                    automerge=merge_outcome,
                    guard=guard,
                )
            remote_status = str(getattr(remote_revert, "status", "auto_revert_publish_failed"))
            try:
                if bool(getattr(remote_revert, "restored", False)):
                    incident_transition("recovered", last_error=getattr(remote_revert, "reason", remote_status))
                elif remote_status == "auto_revert_pending":
                    incident_transition("revert_pending", last_error=getattr(remote_revert, "reason", remote_status))
                else:
                    incident_transition("attention", last_error=getattr(remote_revert, "reason", remote_status))
            except Exception as exc:
                return blocked(
                    f"rollback distant terminé mais état durable incohérent: {exc}",
                    stop_reason="phase5_incident_pending",
                    merged=True,
                    automerge=merge_outcome,
                    guard=guard,
                    remote_revert=remote_revert,
                )
            return blocked(
                getattr(remote_revert, "reason", "rollback distant terminé sans verdict"),
                stop_reason=remote_status,
                merged=True,
                automerge=merge_outcome,
                guard=guard,
                remote_revert=remote_revert,
            )
    if not bool(getattr(guard, "checked", False)) or getattr(guard, "healthy", None) is not True:
        incident_attention(getattr(guard, "reason", "santé de main non concluante"))
        return blocked(
            getattr(guard, "reason", "santé de main non concluante"),
            stop_reason="post_merge_guard_failed",
            merged=True,
            automerge=merge_outcome,
            guard=guard,
        )
    try:
        incident_clear()
    except Exception as exc:
        return blocked(
            f"main verte mais clôture durable Phase 5 impossible: {exc}",
            stop_reason="phase5_incident_pending",
            merged=True,
            automerge=merge_outcome,
            guard=guard,
        )
    return PromotionAutoMergeOutcome(
        merged=True,
        continue_loop=True,
        stop_reason=None,
        reason="PR auto-mergée, base resynchronisée et main verte",
        automerge=merge_outcome,
        guard=guard,
    )
