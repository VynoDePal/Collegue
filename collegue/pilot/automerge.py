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

import fnmatch
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

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
    result = clients.prs.merge_pr(owner, repo, pr.number, method=policy.method, expected_head_sha=expected)
    merged = bool(getattr(result, "merged", False))
    _emit(pr_number=pr_number, allowed=True, merged=merged, reason=decision.reason)
    return AutoMergeOutcome(decision=decision, merged=merged, merge_result=result)
