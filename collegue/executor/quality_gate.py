"""Gate qualité d'un diff produit par l'agent (E3, epic #362).

Deux vérifications, **fail-closed** :

1. **Tests** dans le :class:`~collegue.sandbox.executor.DockerSandbox` (C8) sur
   l'arbre patché — du code non fiable ne tourne donc jamais sur l'hôte.
2. **Revue experte** via l'outil ``code_review`` existant (rôle LLM ``REVIEWER``),
   derrière un :class:`Reviewer` injectable (mocké en CI, réel en ``integration``).

``passed`` n'est vrai que si les tests passent **et** la revue ne bloque pas
**et** aucune erreur n'est survenue. Toute incertitude (tests non exécutables,
exception du reviewer) ⇒ ``passed=False`` : on ne laisse jamais un doute valider
le diff. Les ``BaseException`` (ex. ``BudgetExceeded``, C4) ne sont **pas**
avalées — elles remontent.

:meth:`QualityReport.to_markdown` produit le rapport pour le corps de PR (E4) ;
le texte de revue (potentiellement non fiable) est **inline-isé puis fencé** pour
qu'il ne puisse pas forger de fausse bannière/section (cf. P5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, Tuple, runtime_checkable

from collegue.executor.agent import IssueSpec
from collegue.sandbox.executor import DockerSandbox
from collegue.textnorm import inline

# `python -m pytest` (et non le script `pytest`) ajoute le répertoire de travail
# (la racine du workspace, montée sur `/workspace`) à `sys.path`. Sans ça, les tests
# d'un projet en layout `src/`/`app/` qui importent par package (`from app.x import …`,
# `from src.x import …`) lèvent `ModuleNotFoundError` à la collecte → le gate échoue
# à tort (tests verts vus comme rouges). Voir issue #413.
DEFAULT_TEST_COMMAND = "python -m pytest -q"
# Triple-backtick de remplacement : neutralise les fences pour qu'un texte non
# fiable ne puisse pas refermer le bloc de code et forger une fausse section.
_FENCE = "```"
_FENCE_SAFE = "ʼʼʼ"


def _fence_safe_line(text) -> str:
    """Une ligne sûre dans un bloc fencé : inline-isée + fences neutralisés."""
    return inline(text).replace(_FENCE, _FENCE_SAFE)


# En-dessous de ce score (cf. seuil interne de code_review), la revue bloque.
DEFAULT_MIN_QUALITY = 0.5
# Sévérités qui bloquent à elles seules, quel que soit le score. On inclut
# ``error`` (pas seulement ``critical``) : l'expert code_review émet ``error`` pour
# des problèmes sérieux (complexité élevée, motifs d'injection) et traite lui-même
# critical+error comme graves. Comme le score est normalisé par la taille, un seul
# ``error`` sur un gros diff donnerait un score ~0.96 et passerait sinon — le gate
# resterait laxiste. On reste donc fail-closed côté sévérité.
BLOCKING_SEVERITIES = frozenset({"critical", "error"})


@dataclass(frozen=True)
class ReviewFindingLite:
    """Finding de revue, découplé du modèle Pydantic de ``code_review``."""

    category: str
    severity: str
    title: str


@dataclass(frozen=True)
class ReviewOutcome:
    """Résultat normalisé d'une revue."""

    summary: str
    quality_score: float
    findings: Tuple[ReviewFindingLite, ...] = ()
    blocking: bool = False


@runtime_checkable
class Reviewer(Protocol):
    """Revue d'un diff. Async pour autoriser un reviewer LLM (rôle REVIEWER)."""

    async def review(self, diff: str, ctx, *, issue: Optional[IssueSpec] = None) -> ReviewOutcome: ...


@dataclass
class QualityReport:
    """Verdict combiné tests + revue d'un diff."""

    tests_passed: bool
    test_exit_code: int
    test_output: str
    review_summary: str
    review_findings: Tuple[ReviewFindingLite, ...]
    review_blocking: bool
    passed: bool
    review_error: Optional[str] = None

    def to_markdown(self) -> str:
        """Rapport Markdown pour le corps de PR (texte de revue fencé, anti-injection)."""
        tests_badge = "✅ réussis" if self.tests_passed else "❌ échec"
        lines = [
            "## Gate qualité",
            "",
            f"**Tests** : {tests_badge} (code de sortie {self.test_exit_code})",
            "",
            "<details><summary>Sortie des tests</summary>",
            "",
            "```text",
            # multi-ligne préservé ; on neutralise seulement le délimiteur de fence.
            self.test_output.replace(_FENCE, _FENCE_SAFE) or "(vide)",
            "```",
            "",
            "</details>",
            "",
            f"**Revue experte** : {'⛔ bloquante' if self.review_blocking else '✅ non bloquante'}",
        ]
        if self.review_error:
            lines.append(f"> ⚠️ revue indisponible : {_fence_safe_line(self.review_error)}")
        lines += ["", "```text", _fence_safe_line(self.review_summary) or "(pas de résumé)"]
        for finding in self.review_findings:
            lines.append(
                "- "
                f"[{_fence_safe_line(finding.severity)}] "
                f"{_fence_safe_line(finding.category)} : {_fence_safe_line(finding.title)}"
            )
        lines += ["```", "", f"**Verdict** : {'✅ PASSÉ' if self.passed else '❌ NON PASSÉ'}"]
        return "\n".join(lines)


class FakeReviewer:
    """:class:`Reviewer` déterministe pour la CI (aucun LLM)."""

    def __init__(
        self,
        *,
        summary: str = "revue simulée : RAS",
        quality_score: float = 0.9,
        findings: Optional[List[ReviewFindingLite]] = None,
        blocking: bool = False,
        raises: Optional[Exception] = None,
    ):
        self._summary = summary
        self._quality_score = quality_score
        self._findings = tuple(findings or ())
        self._blocking = blocking
        self._raises = raises

    async def review(self, diff: str, ctx, *, issue: Optional[IssueSpec] = None) -> ReviewOutcome:
        if self._raises is not None:
            raise self._raises
        return ReviewOutcome(
            summary=self._summary,
            quality_score=self._quality_score,
            findings=self._findings,
            blocking=self._blocking,
        )


async def run_quality_gate(
    workspace: str,
    diff: str,
    ctx,
    *,
    sandbox: Optional[DockerSandbox] = None,
    reviewer: Optional[Reviewer] = None,
    issue: Optional[IssueSpec] = None,
    test_command: str = DEFAULT_TEST_COMMAND,
) -> QualityReport:
    """Exécute les tests (sandbox) + la revue (reviewer) sur un diff. Fail-closed.

    ``sandbox``/``reviewer`` sont injectables (mockés en CI). Tout échec ou
    indisponibilité ⇒ ``passed=False``. Les ``BaseException`` remontent.
    """
    sandbox = sandbox or DockerSandbox()
    reviewer = reviewer or _default_reviewer()

    # 1. Tests dans le sandbox. Une incapacité à les exécuter = non passé
    #    (fail-closed), pas une exception qui remonterait.
    try:
        test_res = sandbox.run_tests(workspace, test_command)
        tests_passed = test_res.ok
        test_exit_code = test_res.exit_code
        test_output = "\n".join(part for part in (test_res.stdout, test_res.stderr) if part).strip()
    except Exception as exc:  # noqa: BLE001 - fail-closed ; BaseException (budget) remonte
        tests_passed = False
        test_exit_code = -1
        test_output = f"tests non exécutables : {exc}"

    # 2. Revue. Une exception du reviewer = bloquant (fail-closed).
    review_error: Optional[str] = None
    try:
        outcome = await reviewer.review(diff, ctx, issue=issue)
        review_summary = outcome.summary
        review_findings = outcome.findings
        review_blocking = outcome.blocking
    except Exception as exc:  # noqa: BLE001 - fail-closed ; BaseException (budget) remonte
        review_error = str(exc) or repr(exc)
        review_summary = "revue indisponible (erreur)"
        review_findings = ()
        review_blocking = True

    passed = bool(tests_passed and not review_blocking and review_error is None)
    return QualityReport(
        tests_passed=tests_passed,
        test_exit_code=test_exit_code,
        test_output=test_output,
        review_summary=review_summary,
        review_findings=review_findings,
        review_blocking=review_blocking,
        passed=passed,
        review_error=review_error,
    )


def _default_reviewer() -> Reviewer:
    """Reviewer par défaut : l'expert ``code_review`` réel (rôle REVIEWER)."""
    return ExpertReviewer()


def outcome_from_review(response, *, min_quality: float = DEFAULT_MIN_QUALITY) -> ReviewOutcome:
    """Mappe une ``CodeReviewResponse`` vers un :class:`ReviewOutcome` (pur, testable).

    Bloquant si le score est sous le seuil **ou** s'il existe un finding de sévérité
    bloquante (``critical`` ou ``error``, cf. :data:`BLOCKING_SEVERITIES`).
    """
    findings = tuple(
        ReviewFindingLite(category=f.category, severity=f.severity, title=f.title) for f in response.findings
    )
    blocking = response.quality_score < min_quality or any(f.severity in BLOCKING_SEVERITIES for f in findings)
    return ReviewOutcome(
        summary=response.summary,
        quality_score=response.quality_score,
        findings=findings,
        blocking=blocking,
    )


class ExpertReviewer:
    """Adaptateur :class:`Reviewer` vers l'outil expert ``code_review`` (réel).

    Réutilise l'expert existant (non-goal §9 : ne pas le réécrire). L'exécution
    réelle (analyse statique + boucle agentique LLM, rôle ``REVIEWER``) a lieu en
    ``integration`` ; ``code_review`` n'est importé que paresseusement ici pour ne
    pas alourdir l'import de l'exécuteur. Le **mapping** réponse→outcome
    (:func:`outcome_from_review`) est, lui, pur et testé en CI.
    """

    def __init__(self, *, min_quality: float = DEFAULT_MIN_QUALITY, tool=None):
        self._min_quality = min_quality
        self._tool = tool  # injectable pour les tests ; sinon construit à la volée

    async def review(self, diff: str, ctx, *, issue: Optional[IssueSpec] = None) -> ReviewOutcome:
        from collegue.tools.code_review.models import CodeReviewRequest

        tool = self._tool or self._build_tool()
        request = CodeReviewRequest(
            code=diff or "(diff vide)",
            language="python",
            context=issue.to_prompt() if issue is not None else None,
        )
        response = await tool.execute_async(request, ctx=ctx)
        return outcome_from_review(response, min_quality=self._min_quality)

    @staticmethod
    def _build_tool():  # pragma: no cover - chemin réel (integration)
        from collegue.tools.code_review.tool import CodeReviewTool

        return CodeReviewTool()
