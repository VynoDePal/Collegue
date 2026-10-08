"""Politique de fusion COMMUNE (BUILD et Phase 5) : preuve, SHA, base, checks requis, précondition serveur.

Un seul chemin de validation, appliqué à toutes les fusions automatiques (boucle normale du merge-bot, drain de fin
de run, reprise après crash, Phase 5). Il ne fait AUCUNE écriture : il lit GitHub et la preuve de livraison durable
puis rend soit une :class:`MergeApproval` (ancres vérifiées), soit lève :class:`MergeRefused` avec la raison.

Ce qui est vérifié, dans l'ordre (tout échec ou toute vérification inaccessible refuse — jamais d'« absence = OK ») :

1. **PR** : ouverte, non brouillon, branche de base attendue, branche de tête attendue (la PR retrouvée par nom de
   branche peut être une PR préexistante différente : la preuve, liée à la tête exacte, tranche).
2. **Preuve de livraison durable** (``executor.delivery_proof.load_delivery_proof``, fournie par le lot A) chargée
   pour ``(projet, dépôt, PR, tête observée)`` depuis l'état contrôlé — jamais depuis le corps de la PR — avec
   ``passed`` strictement vrai, la phase attendue, base/tree complets. Module absent ou refus => pas de fusion.
3. **Contenu** : la tête distante contient la base de confiance de la preuve (comparaison Git) et son tree est
   EXACTEMENT celui de la preuve.
4. **Base** : le sommet distant de la branche de base est la base de la preuve (les contrôles ont porté sur ce contenu).
5. **Checks requis** : découverts dans les protections classiques ET les rulesets applicables (avec ``app_id`` /
   ``integration_id`` quand présent), tous présents et ``success`` sur la tête exacte ; check absent, en attente,
   failed/cancelled/skipped/neutral, liste incomplète ou erreur de lecture => pas de fusion.
6. **Précondition serveur contre la course sur la base** : au moins une protection « branche à jour avant fusion »
   (``strict``) EFFECTIVEMENT applicable à l'acteur du jeton (ruleset actif non contournable par lui, ou protection
   classique appliquée aux administrateurs / acteur non administrateur). L'API de fusion ne prend qu'un ``sha`` de tête :
   deux lectures successives ne démontrent rien, seule cette règle côté serveur fait refuser une fusion dont la base a
   bougé entre notre dernière lecture et l'évaluation serveur. Si elle ne peut pas être établie, on refuse.

La tête est transmise à l'API de fusion (``sha``). Après la fusion, :func:`verify_merge_result` contrôle que le commit
produit a bien la base et le tree de la preuve (détection a posteriori si la précondition serveur avait été trompée).
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence, Tuple

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PROOF_ID_RE = re.compile(r"^[0-9a-f]{64}$")

#: états de check « en cours » : on attend (avec délai borné) plutôt que de refuser tout de suite.
PENDING_STATES = frozenset({"pending", "queued", "in_progress", "requested", "waiting", "expected"})
#: états terminaux qui NE valent PAS succès (y compris skipped/neutral : un check requis sauté ne prouve rien).
GREEN_STATE = "success"
#: états terminaux rouges qui bloquent même un check non requis.
RED_STATES = frozenset({"failure", "error", "cancelled", "timed_out", "action_required", "stale", "startup_failure"})

SUPPORTED_METHODS = frozenset({"squash", "merge"})

CODE_PENDING = "pending"
CODE_MISSING_CHECK = "missing_check"
CODE_FAILED_CHECK = "failed_check"
CODE_MOVED = "moved"
CODE_NO_PROOF = "no_proof"
CODE_POLICY = "policy"
CODE_API = "api_error"
CODE_ALREADY_MERGED = "already_merged"
CODE_STATE = "state"
RETRYABLE_CODES = frozenset({CODE_PENDING, CODE_MISSING_CHECK})


class MergeRefused(RuntimeError):
    """La fusion automatique est refusée (ou ne peut pas être justifiée) ; ``code`` classe la raison."""

    def __init__(self, reason: str, *, code: str = CODE_POLICY):
        super().__init__(reason)
        self.reason = reason
        self.code = code

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


@dataclass(frozen=True)
class RequiredCheck:
    context: str
    app_id: Optional[int]  # None = n'importe quelle source
    origin: str  # "classic" | "ruleset:<id>"


@dataclass(frozen=True)
class ServerPolicy:
    """Ce que le serveur GitHub applique réellement à l'acteur de fusion sur la branche de base."""

    branch: str
    actor: str
    actor_role: str
    required_checks: Tuple[RequiredCheck, ...]
    strict_sources: Tuple[str, ...]  # protections « à jour avant fusion » effectives et non contournables
    notes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class MergeApproval:
    """Ancres vérifiées d'une fusion autorisée (toutes réutilisées pour le write-ahead durable)."""

    owner: str
    repo: str
    base_branch: str
    pr_number: int
    head_sha: str
    pr_base_sha: str
    base_sha: str  # sommet de la base == base de la preuve
    tree_sha: str
    proof_id: str
    phase: str
    method: str
    server_policy: ServerPolicy
    checks: Tuple[Any, ...] = field(default_factory=tuple)


# ── preuve de livraison ─────────────────────────────────────────────────────────────────


def default_proof_loader() -> Callable[..., Any]:
    """``load_delivery_proof`` du lot A, importé paresseusement. Absent => refus (jamais de dispense)."""
    try:
        from collegue.executor.delivery_proof import load_delivery_proof
    except ImportError as exc:
        raise MergeRefused(
            f"module de preuve de livraison indisponible ({exc}) — fusion automatique impossible",
            code=CODE_NO_PROOF,
        ) from exc
    return load_delivery_proof


def _full_sha(value: Any, label: str, *, code: str = CODE_STATE) -> str:
    text = str(value or "").strip().lower()
    if not _SHA_RE.fullmatch(text):
        raise MergeRefused(f"{label} absent ou invalide: {value!r}", code=code)
    return text


def load_proof(
    loader: Optional[Callable[..., Any]],
    manager: Any,
    *,
    project_id: int,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    expected_phase: str,
) -> Any:
    """Charge et revalide la preuve durable pour la tête EXACTEMENT observée."""
    loader = loader or default_proof_loader()
    try:
        proof = loader(manager, project_id, owner=owner, repo=repo, pr_number=pr_number, head_sha=head_sha)
    except MergeRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - tout refus ou panne de la preuve interdit la fusion
        raise MergeRefused(f"preuve de livraison absente ou invalide: {exc}", code=CODE_NO_PROOF) from exc
    if proof is None:
        raise MergeRefused("preuve de livraison absente", code=CODE_NO_PROOF)
    problems = []
    if getattr(proof, "passed", None) is not True:
        problems.append("passed n'est pas strictement vrai")
    if getattr(proof, "owner", None) != owner or getattr(proof, "repo", None) != repo:
        problems.append("dépôt différent")
    pid = getattr(proof, "project_id", None)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid != project_id:
        problems.append("projet différent")
    number = getattr(proof, "pr_number", None)
    if not isinstance(number, int) or isinstance(number, bool) or number != pr_number:
        problems.append("PR différente")
    if str(getattr(proof, "head_sha", "")).lower() != head_sha:
        problems.append("tête différente")
    if getattr(proof, "phase", None) != expected_phase:
        problems.append(f"phase {getattr(proof, 'phase', None)!r} != {expected_phase!r}")
    for name in ("base_sha", "tree_sha"):
        if not _SHA_RE.fullmatch(str(getattr(proof, name, "")).lower()):
            problems.append(f"{name} invalide")
    if not _PROOF_ID_RE.fullmatch(str(getattr(proof, "proof_id", "")).lower()):
        problems.append("proof_id invalide")
    if problems:
        raise MergeRefused("preuve de livraison incohérente: " + "; ".join(problems), code=CODE_NO_PROOF)
    return proof


# ── précondition serveur ─────────────────────────────────────────────────────────────────


# Rôles de base GitHub qui ne contournent PAS les protections de branche classiques (administrateurs et rôles
# personnalisés avec « bypass branch protections » les contournent tant que enforce_admins est désactivé).
_NON_BYPASS_BASE_ROLES = frozenset({"read", "triage", "write", "maintain"})


def _int_or_none(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def discover_server_policy(clients: Any, owner: str, repo: str, branch: str) -> ServerPolicy:
    """Checks requis et précondition « à jour » réellement applicables à l'acteur de fusion sur ``branch``.

    Réponses GitHub nécessaires (lecture seule) : ``GET /user`` (acteur ; un jeton d'application refuse),
    ``GET /repos/{o}/{r}/collaborators/{login}/permission`` (rôle), ``GET /repos/{o}/{r}/branches/{b}/protection``
    (404 toléré : absence ou invisibilité), ``GET /repos/{o}/{r}/rules/branches/{b}`` (règles actives, toutes
    pages) et ``GET /repos/{o}/{r}/rulesets/{id}`` (mode d'application et ``current_user_can_bypass``).
    """
    branches = getattr(clients, "branches", None)
    needed = (
        "get_authenticated_login",
        "get_collaborator_role",
        "get_branch_protection",
        "get_branch_rules",
        "get_ruleset",
    )
    if branches is None or not all(hasattr(branches, name) for name in needed):
        raise MergeRefused(
            "client GitHub des protections indisponible — précondition serveur invérifiable", code=CODE_API
        )
    try:
        actor = branches.get_authenticated_login()
        role = branches.get_collaborator_role(owner, repo, actor)
        classic = branches.get_branch_protection(owner, repo, branch)
        rules = list(branches.get_branch_rules(owner, repo, branch))
    except Exception as exc:  # noqa: BLE001 - vérification inaccessible => refus
        raise MergeRefused(f"protections de {branch} illisibles: {exc}", code=CODE_API) from exc

    required: list[RequiredCheck] = []
    strict_sources: list[str] = []
    notes: list[str] = []

    if classic is not None and getattr(classic, "has_required_status_checks", False):
        for spec in classic.required_checks:
            required.append(RequiredCheck(spec.context, _int_or_none(spec.app_id), "classic"))
        # Sans enforce_admins, la protection classique ne s'applique pas aux administrateurs NI aux rôles
        # personnalisés dotés de « bypass branch protections » (doc GitHub). Seuls les rôles de base sans ce droit
        # sont donc tenus pour liés ; tout autre nom de rôle (personnalisé, inconnu) est présumé contournant.
        can_bypass = not classic.enforce_admins and role not in _NON_BYPASS_BASE_ROLES
        if classic.strict and not can_bypass:
            strict_sources.append("classic")
        elif classic.strict:
            notes.append(f"protection classique 'à jour' contournable par l'acteur (rôle {role!r} sans enforce_admins)")
        else:
            notes.append("protection classique sans 'à jour avant fusion' (strict=false)")

    ruleset_ids = sorted({r.ruleset_id for r in rules if getattr(r, "ruleset_id", None) is not None})
    infos: dict[int, Any] = {}
    for ruleset_id in ruleset_ids:
        try:
            infos[ruleset_id] = branches.get_ruleset(owner, repo, ruleset_id)
        except Exception as exc:  # noqa: BLE001
            raise MergeRefused(f"ruleset {ruleset_id} illisible: {exc}", code=CODE_API) from exc

    for rule in rules:
        if rule.type == "merge_queue":
            raise MergeRefused(
                "une file de fusion (merge queue) s'applique à la branche : l'API de fusion directe est inadaptée — "
                "fusion automatique refusée",
                code=CODE_POLICY,
            )
        if rule.type != "required_status_checks":
            continue
        if rule.ruleset_id is None or rule.ruleset_id not in infos:
            raise MergeRefused(
                "règle de checks requis sans ruleset identifiable — application invérifiable", code=CODE_API
            )
        info = infos[rule.ruleset_id]
        if info.enforcement != "active":
            notes.append(f"ruleset {info.id} non actif ({info.enforcement or 'inconnu'}) : ignoré")
            continue
        params = rule.parameters or {}
        listed = params.get("required_status_checks")
        if not isinstance(listed, list):
            raise MergeRefused(f"ruleset {info.id}: checks requis malformés", code=CODE_API)
        for item in listed:
            if not isinstance(item, dict) or not item.get("context"):
                raise MergeRefused(f"ruleset {info.id}: check requis malformé", code=CODE_API)
            required.append(
                RequiredCheck(str(item["context"]), _int_or_none(item.get("integration_id")), f"ruleset:{info.id}")
            )
        non_bypassable = info.current_user_can_bypass == "never"
        if params.get("strict_required_status_checks_policy") is True:
            if non_bypassable:
                strict_sources.append(f"ruleset:{info.id}")
            elif info.current_user_can_bypass is None:
                notes.append(f"ruleset {info.id}: contournement par l'acteur indéterminé (champ absent)")
            else:
                notes.append(f"ruleset {info.id}: contournable par l'acteur ({info.current_user_can_bypass})")
        else:
            notes.append(f"ruleset {info.id} sans 'à jour avant fusion'")

    if not required:
        raise MergeRefused(
            f"aucun check requis découvert sur {branch} (protections classiques ni rulesets) — "
            "la complétude des vérifications ne peut pas être établie",
            code=CODE_POLICY,
        )
    if not strict_sources:
        raise MergeRefused(
            f"aucune protection stricte ('à jour avant fusion') effectivement applicable à l'acteur {actor!r} "
            f"sur {branch} : la course sur la base ne peut pas être écartée côté serveur — "
            + ("; ".join(notes) if notes else "aucune protection trouvée"),
            code=CODE_POLICY,
        )
    unique = tuple(dict.fromkeys(required))
    return ServerPolicy(
        branch=branch,
        actor=actor,
        actor_role=role,
        required_checks=unique,
        strict_sources=tuple(strict_sources),
        notes=tuple(notes),
    )


# ── checks ───────────────────────────────────────────────────────────────────────────────


def evaluate_checks(
    required: Iterable[RequiredCheck], observed: Sequence[Any], *, require_all_green: bool = False
) -> None:
    """Lève :class:`MergeRefused` sauf si TOUS les checks requis sont présents et ``success``.

    Un check requis avec ``app_id`` ne peut être satisfait que par un check-run de cette application. Plusieurs
    observations correspondantes (même nom, sources différentes) doivent TOUTES être vertes. Un check non requis
    en échec terminal bloque aussi ; avec ``require_all_green`` (Phase 5) tout check doit être ``success``.
    """
    missing: list[str] = []
    pending: list[str] = []
    bad: list[str] = []
    for req in required:
        matches = [
            o
            for o in observed
            if o.name == req.context and (req.app_id is None or getattr(o, "app_id", None) == req.app_id)
        ]
        label = req.context if req.app_id is None else f"{req.context} (app {req.app_id})"
        if not matches:
            missing.append(label)
            continue
        states = {str(o.state).strip().lower() for o in matches}
        if states & PENDING_STATES:
            pending.append(label)
        elif states != {GREEN_STATE}:
            bad.append(f"{label}={'/'.join(sorted(states - {GREEN_STATE}))}")
    if bad:
        raise MergeRefused(f"checks requis non réussis: {', '.join(bad)}", code=CODE_FAILED_CHECK)
    for obs in observed:
        state = str(obs.state).strip().lower()
        if state in RED_STATES:
            raise MergeRefused(f"check en échec: {obs.name}={state}", code=CODE_FAILED_CHECK)
        if require_all_green and state != GREEN_STATE and state not in PENDING_STATES:
            raise MergeRefused(f"check non vert: {obs.name}={state}", code=CODE_FAILED_CHECK)
        if require_all_green and state in PENDING_STATES:
            pending.append(obs.name)
    if missing:
        raise MergeRefused(f"checks requis absents: {', '.join(missing)}", code=CODE_MISSING_CHECK)
    if pending:
        raise MergeRefused(f"checks en attente: {', '.join(dict.fromkeys(pending))}", code=CODE_PENDING)


# ── validation complète d'un candidat ──────────────────────────────────────────────────────


def _read_pr(prs: Any, owner: str, repo: str, number: int) -> Any:
    try:
        return prs.get_pr(owner, repo, int(number))
    except Exception as exc:  # noqa: BLE001
        raise MergeRefused(f"lecture de la PR #{number} impossible: {exc}", code=CODE_API) from exc


def verify_merge_candidate(
    clients: Any,
    manager: Any,
    *,
    project_id: int,
    owner: str,
    repo: str,
    base: str,
    pr_number: int,
    expected_phase: str,
    method: str = "squash",
    expected_head_branch: Optional[str] = None,
    expected_head_sha: Optional[str] = None,
    expected_pr_base_sha: Optional[str] = None,
    proof_loader: Optional[Callable[..., Any]] = None,
    require_all_green: bool = False,
) -> MergeApproval:
    """Validation complète d'un candidat (lecture seule). Voir le docstring du module pour l'ordre et la portée."""
    if method not in SUPPORTED_METHODS:
        raise MergeRefused(f"méthode de fusion non supportée: {method!r} (rollback atomique non prouvable)")
    prs = getattr(clients, "prs", None)
    branches = getattr(clients, "branches", None)
    if prs is None or branches is None:
        raise MergeRefused("clients GitHub PR/branches absents — contexte invérifiable", code=CODE_API)

    policy = discover_server_policy(clients, owner, repo, base)

    pr = _read_pr(prs, owner, repo, pr_number)
    if getattr(pr, "merged", False) or (
        getattr(pr, "state", None) == "closed" and getattr(pr, "merge_commit_sha", None)
    ):
        raise MergeRefused(f"PR #{pr_number} déjà fusionnée", code=CODE_ALREADY_MERGED)
    if getattr(pr, "state", None) != "open" or bool(getattr(pr, "draft", False)):
        raise MergeRefused(f"PR #{pr_number} fermée ou brouillon", code=CODE_STATE)
    if getattr(pr, "base_branch", None) != base:
        raise MergeRefused(
            f"base de la PR inattendue ({getattr(pr, 'base_branch', None)!r} != {base!r})", code=CODE_STATE
        )
    if expected_head_branch is not None and getattr(pr, "head_branch", None) != expected_head_branch:
        raise MergeRefused(
            f"branche de tête inattendue ({getattr(pr, 'head_branch', None)!r} != {expected_head_branch!r}) — "
            "PR préexistante différente",
            code=CODE_STATE,
        )
    head_sha = _full_sha(getattr(pr, "head_sha", None), "SHA de tête")
    pr_base_sha = _full_sha(getattr(pr, "base_sha", None), "SHA de base de la PR")
    if expected_head_sha is not None and head_sha != str(expected_head_sha).lower():
        raise MergeRefused("la tête de la PR a bougé depuis l'évaluation", code=CODE_MOVED)
    if expected_pr_base_sha is not None and pr_base_sha != str(expected_pr_base_sha).lower():
        raise MergeRefused("la base de la PR a bougé depuis l'évaluation", code=CODE_MOVED)

    proof = load_proof(
        proof_loader,
        manager,
        project_id=project_id,
        owner=owner,
        repo=repo,
        pr_number=int(pr_number),
        head_sha=head_sha,
        expected_phase=expected_phase,
    )
    proof_base = str(proof.base_sha).lower()
    proof_tree = str(proof.tree_sha).lower()

    try:
        tip = _full_sha(branches.get_branch_sha(owner, repo, base), "sommet de la base")
    except MergeRefused:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MergeRefused(f"sommet de {base} illisible: {exc}", code=CODE_API) from exc
    if tip != proof_base:
        raise MergeRefused(
            f"la base {base} a avancé depuis les contrôles (sommet {tip[:12]}, contrôlé sur {proof_base[:12]})",
            code=CODE_MOVED,
        )
    try:
        relation = branches.compare_commits(owner, repo, proof_base, head_sha)
        commit = branches.get_git_commit(owner, repo, head_sha)
    except Exception as exc:  # noqa: BLE001
        raise MergeRefused(f"contenu distant de la tête illisible: {exc}", code=CODE_API) from exc
    if relation.status != "ahead" or relation.behind_by != 0 or relation.merge_base_sha != proof_base:
        raise MergeRefused(
            f"la tête ne descend pas de la base contrôlée (relation {relation.status}, behind_by={relation.behind_by})",
            code=CODE_MOVED,
        )
    if str(commit.tree_sha).lower() != proof_tree:
        raise MergeRefused("le tree distant de la tête diffère de celui de la preuve de livraison", code=CODE_NO_PROOF)

    try:
        details = prs.get_commit_check_details(owner, repo, head_sha)
    except Exception as exc:  # noqa: BLE001
        raise MergeRefused(f"lecture des checks impossible: {exc}", code=CODE_API) from exc
    if not bool(getattr(details, "complete", False)):
        raise MergeRefused("liste des checks incomplète — fail-closed", code=CODE_API)
    evaluate_checks(policy.required_checks, list(details.checks), require_all_green=require_all_green)

    # Stabilité finale : la PR et la base n'ont pas bougé pendant toutes ces lectures.
    again = _read_pr(prs, owner, repo, pr_number)
    if str(getattr(again, "head_sha", "")).lower() != head_sha or getattr(again, "state", None) != "open":
        raise MergeRefused("la PR a changé pendant la validation", code=CODE_MOVED)
    try:
        tip_again = _full_sha(branches.get_branch_sha(owner, repo, base), "sommet de la base")
    except MergeRefused:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MergeRefused(f"sommet de {base} illisible: {exc}", code=CODE_API) from exc
    if tip_again != tip:
        raise MergeRefused("la base a bougé pendant la validation", code=CODE_MOVED)

    return MergeApproval(
        owner=owner,
        repo=repo,
        base_branch=base,
        pr_number=int(pr_number),
        head_sha=head_sha,
        pr_base_sha=pr_base_sha,
        base_sha=tip,
        tree_sha=proof_tree,
        proof_id=str(proof.proof_id).lower(),
        phase=expected_phase,
        method=method,
        server_policy=policy,
        checks=tuple(details.checks),
    )


def verify_required_checks(
    clients: Any,
    *,
    owner: str,
    repo: str,
    base: str,
    head_sha: str,
    require_all_green: bool = True,
) -> ServerPolicy:
    """Checks requis + précondition serveur pour une PR SANS preuve de livraison (revert mécanique).

    Mêmes lectures que ``verify_merge_candidate`` pour la politique serveur et les checks de la tête ; la
    conformité du contenu est prouvée ailleurs (tree restauré). Lève ``MergeRefused`` au moindre doute."""
    prs = getattr(clients, "prs", None)
    if prs is None:
        raise MergeRefused("client GitHub des PR absent — checks invérifiables", code=CODE_API)
    policy = discover_server_policy(clients, owner, repo, base)
    head = _full_sha(head_sha, "SHA de tête")
    try:
        details = prs.get_commit_check_details(owner, repo, head)
    except Exception as exc:  # noqa: BLE001
        raise MergeRefused(f"lecture des checks impossible: {exc}", code=CODE_API) from exc
    if not bool(getattr(details, "complete", False)):
        raise MergeRefused("liste des checks incomplète — fail-closed", code=CODE_API)
    evaluate_checks(policy.required_checks, list(details.checks), require_all_green=require_all_green)
    return policy


async def await_merge_approval(
    verify: Callable[[], MergeApproval],
    *,
    timeout_seconds: float,
    poll_seconds: float,
    continue_fn: Optional[Callable[[], Any]] = None,
    sleep_fn: Callable[[float], Any] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> MergeApproval:
    """Rejoue ``verify`` tant que le refus est « en attente » (checks en cours ou pas encore créés), jusqu'au délai.

    Les gardes budget/deadline (``continue_fn``) sont consultées à chaque tour : leur refus interrompt l'attente.
    """
    deadline = clock() + max(0.0, float(timeout_seconds))
    while True:
        if continue_fn is not None:
            try:
                continuation = continue_fn()
            except Exception as exc:  # noqa: BLE001
                raise MergeRefused(f"contrôle budget/deadline impossible: {exc}", code=CODE_API) from exc
            if not bool(getattr(continuation, "ok", continuation)):
                reason = str(getattr(continuation, "reason", "budget ou deadline atteint"))
                raise MergeRefused(f"attente interrompue: {reason}", code=CODE_STATE)
        try:
            return verify()
        except MergeRefused as refused:
            if not refused.retryable:
                raise
            if clock() >= deadline:
                raise MergeRefused(f"délai d'attente dépassé: {refused.reason}", code=refused.code) from refused
        result = sleep_fn(max(0.0, float(poll_seconds)))
        if inspect.isawaitable(result):
            await result


# ── exécution et contrôle du résultat ──────────────────────────────────────────────────────


def merge_with_head_guard(clients: Any, approval: MergeApproval) -> Any:
    """Appelle l'API de fusion avec le SHA de tête exact (garde serveur) ; le résultat est contrôlé par l'appelant."""
    return clients.prs.merge_pr(
        approval.owner,
        approval.repo,
        approval.pr_number,
        method=approval.method,
        expected_head_sha=approval.head_sha,
        expected_base_branch=approval.base_branch,
        expected_base_sha=approval.pr_base_sha,
    )


def verify_merge_result(
    clients: Any,
    *,
    owner: str,
    repo: str,
    method: str,
    base_sha: str,
    head_sha: str,
    tree_sha: str,
    merge_sha: str,
) -> None:
    """Le commit de fusion distant a la base et le tree de la preuve (parents attendus selon la méthode).

    C'est la détection A POSTERIORI d'une précondition serveur trompée : si la base a bougé au moment de la
    fusion, le premier parent n'est plus la base contrôlée et/ou le tree n'est plus celui qui a été testé.
    """
    merge_sha = _full_sha(merge_sha, "SHA de fusion", code=CODE_STATE)
    try:
        commit = clients.branches.get_git_commit(owner, repo, merge_sha)
    except Exception as exc:  # noqa: BLE001
        raise MergeRefused(f"commit de fusion illisible: {exc}", code=CODE_API) from exc
    expected_parents = [base_sha] if method == "squash" else [base_sha, head_sha]
    if list(commit.parents) != expected_parents:
        raise MergeRefused(
            f"parents du commit de fusion inattendus ({[p[:12] for p in commit.parents]}) : la base avait bougé",
            code=CODE_MOVED,
        )
    if str(commit.tree_sha).lower() != tree_sha:
        raise MergeRefused("le tree fusionné diffère de celui de la preuve de livraison", code=CODE_NO_PROOF)
