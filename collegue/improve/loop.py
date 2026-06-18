"""Boucle d'amélioration continue (G4, epic #382, Phase 4 — capstone).

Après le MVP, fait tourner : **mesurer (G1) → proposer (G3) → générer un diff
(exécuteur) → mesurer après → gater (G2) → promouvoir (PR) ou jeter**, sous le
budget-temps (F2), et **s'arrête sur rendements décroissants** (les gains
plafonnent) ou au budget.

Gate **AVANT la PR** : un diff qui régresse (tests/sécu) ou n'améliore pas le score
n'ouvre **pas** de PR (« rollback » = abandon avant promotion). Le merge des PR
d'amélioration reste **humain** (§6). ``dry_run`` par défaut (aucune écriture).

``measure_fn`` est injectable (mesures scriptées en CI) ; les briques de
l'exécuteur sont importées **paresseusement** pour garder ``collegue.improve``
léger. Le câblage du **mode `improving`** du pilote (enchaîner build → amélioration)
est laissé à l'appelant/F4 (optionnel) — ce module fournit l'entrée ``run_improvement``.

Module **isolé** : non câblé au runtime.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from collegue.improve.gate import DEFAULT_MIN_GAIN, evaluate
from collegue.improve.metrics import DEFAULT_WEIGHTS, CompositeWeights, autofix_lint, measure, persist
from collegue.improve.proposer import AttemptRecord, build_improvement_task, next_dimension

# Raisons d'arrêt.
STOP_PLATEAU = "plateau"  # rendements décroissants : les gains plafonnent
STOP_PAUSED_BUDGET = "paused_budget"
STOP_DEADLINE = "deadline_reached"
STOP_SAFETY_CAP = "safety_cap"


@dataclass(frozen=True)
class PromotedImprovement:
    """Une amélioration promue en PR."""

    dimension: str
    delta: float
    pr_number: Optional[int]


@dataclass
class ImprovementResult:
    """Bilan d'un run d'amélioration continue."""

    stop_reason: str
    rounds: int
    promoted: List[PromotedImprovement] = field(default_factory=list)
    rejected: List[Tuple[str, str]] = field(default_factory=list)  # (dimension, raison)
    initial_score: Optional[float] = None
    final_score: Optional[float] = None

    @property
    def promoted_prs(self) -> List[int]:
        return [p.pr_number for p in self.promoted if p.pr_number is not None]


def _improvement_quality_report(dimension, before, after, delta):
    """Synthétise un QualityReport (corps de PR) à partir du delta de métriques.

    Réutilise le rendu fencé anti-injection d'E4 ; pas un gate E3 (le gate ici est
    métrique, G2). La couverture est la métrique comparable fiable avant/après.
    """
    from collegue.executor.quality_gate import QualityReport

    summary = (
        f"Amélioration « {dimension} » : score composite {before.composite:.3f} → "
        f"{after.composite:.3f} (Δ{delta:+.3f}). "
        f"Couverture {before.coverage_pct:.0f}% → {after.coverage_pct:.0f}% ; "
        f"sécu pondérée {before.security_weighted:.1f} → {after.security_weighted:.1f} ; "
        f"lint {before.lint_violations} → {after.lint_violations} ; "
        f"complexité {before.complexity_bad_blocks} → {after.complexity_bad_blocks} ; "
        f"vulns deps {before.dep_vulns} → {after.dep_vulns} ; "
        f"docstrings {before.doc_coverage:.0%} → {after.doc_coverage:.0%}."
    )
    return QualityReport(
        tests_passed=after.tests_passed,
        test_exit_code=0 if after.tests_passed else 1,
        test_output="(amélioration continue — gate par métrique)",
        review_summary=summary,
        review_findings=(),
        review_blocking=False,
        passed=True,
    )


def _seed_promoted_diffs(workspace, diffs, *, git_bin: str = "git") -> int:
    """Réapplique ET COMMITE les diffs déjà promus sur le clone neuf (#545, Étape 2).

    Levier 2 du redesign : ``apply_seed_diff`` (git apply -3) réapplique chaque diff
    promu ; ici on le **commite** pour que ``HEAD`` reflète l'état cumulé. Sans commit,
    le diff capturé du round courant (``git diff`` vs HEAD, cf. ``capture_diff``)
    ré-embarquerait les changements déjà promus → double-comptage au round suivant.
    Après commit, la mesure baseline porte sur le projet **cumulé amélioré** : le score
    monte round après round et le proposeur (métrique-driven) passe à la dimension
    suivante car la métrique d'une dimension réglée redevient bonne sur l'état cumulé.
    ``execution.diff`` ne contient alors que les nouveaux changements du round.

    Best-effort : un diff inapplicable (conflit, base déplacée) est **sauté** —
    ``apply_seed_diff`` restaure alors un arbre propre — plutôt que d'échouer le run.
    Renvoie le nombre de diffs effectivement intégrés (commités).
    """
    from collegue.executor.command import LocalCommandRunner
    from collegue.executor.workspace import apply_seed_diff

    runner = LocalCommandRunner()
    applied = 0
    for index, diff in enumerate(diffs):
        if not apply_seed_diff(workspace, diff, git_bin=git_bin):
            continue
        if not runner.run_command([git_bin, "add", "-A"], workspace.path).ok:
            continue
        commit = runner.run_command(
            [
                git_bin,
                "-c",
                "user.email=improve@collegue.local",
                "-c",
                "user.name=collegue-improve",
                "commit",
                "-q",
                "-m",
                f"compounding: amélioration promue #{index + 1}",
            ],
            workspace.path,
        )
        if commit.ok:
            applied += 1
    return applied


async def run_improvement(
    project_id: int,
    repo_source: str,
    ctx,
    *,
    agent,
    owner: str,
    repo: str,
    manager,
    budget=None,  # BudgetTimeController (importé paresseusement) ; None → défaut
    sandbox=None,
    reviewer=None,
    clients=None,
    runner=None,
    base: str = "main",
    dry_run: bool = True,
    plateau_rounds: int = 2,
    min_gain: float = DEFAULT_MIN_GAIN,
    max_iterations: int = 50,
    weights: CompositeWeights = DEFAULT_WEIGHTS,
    measure_fn=measure,
) -> ImprovementResult:
    """Fait tourner la boucle d'amélioration jusqu'au plateau ou au budget.

    Pour chaque round : workspace → mesure baseline → propose une dimension →
    exécute l'agent (diff) → mesure après → gate (G2). Si accepté : PR (E4) + métrique
    persistée. Sinon : diff jeté. Stop quand ``plateau_rounds`` rounds consécutifs
    n'apportent pas de gain (≥ ``min_gain``), ou au budget/deadline.

    En mode réel (``dry_run=False``), les PR d'amélioration sont **stackées** (#554) :
    chaque PR a pour base la branche de la promotion précédente (la 1ʳᵉ sur ``base``),
    pour des diffs incrémentaux mergeables dans l'ordre sans conflit (le compounding
    rendrait sinon les PR cumulatives).
    """
    # Imports paresseux : garder l'import de ``collegue.improve`` léger. ``pilot.budget``
    # est aussi lazy car importer le sous-module déclenche ``pilot/__init__`` (→ driver
    # → exécuteur) — on ne veut pas tirer tout ça au simple import du package improve.
    from collegue.executor.agent import IssueSpec
    from collegue.executor.runner import capture_diff, run_issue
    from collegue.executor.workspace import prepare_workspace
    from collegue.pilot.budget import ACTION_PAUSED_BUDGET, BudgetTimeController

    budget = budget or BudgetTimeController()
    history: List[AttemptRecord] = []
    # Compounding (#545) : diffs déjà promus, réappliqués sur le clone neuf de chaque
    # round pour une baseline cumulative (le score monte ; le proposeur avance).
    promoted_diffs: List[str] = []
    # Stacking des PR (#554) : en mode --execute, chaque PR d'amélioration prend pour
    # base la branche de la promotion PRÉCÉDENTE (au lieu de `base`/main), pour que son
    # diff ne contienne QUE les changements de son round (sinon le compounding rend les
    # PR cumulatives → conflits une fois les premières mergées, vécu au run V10). En
    # dry_run, reste None → base inchangée (aucune PR créée de toute façon).
    last_promoted_branch: Optional[str] = None
    result = ImprovementResult(stop_reason=STOP_PLATEAU, rounds=0)
    plateau = 0
    round_num = 0

    while True:
        if round_num >= max_iterations:
            result.stop_reason = STOP_SAFETY_CAP
            break
        decision = budget.should_continue()
        if not decision.ok:
            result.stop_reason = STOP_PAUSED_BUDGET if decision.action == ACTION_PAUSED_BUDGET else STOP_DEADLINE
            break

        round_num += 1
        task = IssueSpec(number=round_num, title=f"Amélioration continue (round {round_num})")
        workspace = prepare_workspace(repo_source, task)

        # Compounding (#545) : réapplique les diffs promus sur le clone neuf AVANT la
        # mesure baseline → l'objectif porte sur l'état cumulé (le score monte ; une
        # dimension réglée n'est plus proposée car sa métrique redevient bonne).
        if promoted_diffs:
            _seed_promoted_diffs(workspace, promoted_diffs)

        # Levier 1 (#541) : la mesure baseline porte sur le WORKSPACE sur disque, pas
        # sur un diff (il n'y en a pas encore) — objectif symétrique avant/après.
        before = await measure_fn(workspace.path, ctx, sandbox=sandbox, reviewer=reviewer, weights=weights)

        # Baseline non fiable (composite non fini, ex. scan sécu en échec → inf) :
        # round à vide. On NE lance PAS l'agent (coûteux) pour rien et on n'enregistre
        # pas de score fantôme (inf) ; fail-closed — rien ne sera promu (#541).
        if not math.isfinite(before.composite):
            result.rejected.append(("baseline", "mesure baseline non fiable (composite non fini)"))
            plateau += 1
            if plateau >= plateau_rounds:
                result.stop_reason = STOP_PLATEAU
                break
            continue

        if result.initial_score is None:
            result.initial_score = before.composite
        result.final_score = before.composite

        dimension = next_dimension(before, history=history)
        improvement = build_improvement_task(dimension, before, number=round_num)
        execution = run_issue(agent, workspace, improvement, runner=runner)

        if not execution.changed:
            # L'agent n'a rien produit : pas une amélioration → round « à vide ».
            history.append(AttemptRecord(dimension, improved=False))
            result.rejected.append((dimension.value, "aucun diff produit"))
            plateau += 1
            if plateau >= plateau_rounds:
                result.stop_reason = STOP_PLATEAU
                break
            continue

        # Auto-fix lint déterministe (#549) : nettoie le lint auto-corrigible des
        # fichiers touchés AVANT la mesure (le gate est tolérance-0 sur le lint, donc
        # le code de test/refactor du coder ne doit pas être recalé pour un import
        # inutilisé ou un espacement). On re-capture le diff : mesure, PR et compounding
        # utilisent la version corrigée. Un fix cassant un test ⇒ rejeté par le gate.
        autofix_lint(workspace.path, execution.files_changed)
        final_diff, final_files = capture_diff(workspace)

        after = await measure_fn(
            workspace.path, ctx, sandbox=sandbox, reviewer=reviewer, diff=final_diff, weights=weights
        )
        result.final_score = after.composite
        gate = evaluate(before, after, min_gain=min_gain)
        history.append(AttemptRecord(dimension, improved=gate.accepted))

        if gate.accepted:
            report = _improvement_quality_report(dimension.value, before, after, gate.delta)
            from collegue.executor.pr import open_pr

            pr = open_pr(
                workspace,
                report,
                improvement,
                owner,
                repo,
                files_changed=final_files,
                # Stacking (#554) : base = branche de la promotion précédente si elle
                # existe (mode --execute), sinon la base d'origine. → diff de PR propre.
                base=(last_promoted_branch or base),
                clients=clients,
                dry_run=dry_run,
                manager=manager,
                project_id=project_id,
                # Le numéro est un compteur de round, pas une vraie issue → pas de Closes.
                closes_issue=False,
            )
            if not dry_run:
                persist(manager, project_id, after)
                # Stacking (#554) : la PROCHAINE PR se basera sur celle-ci (chaîne de PR
                # mergeables dans l'ordre). Uniquement en réel (en dry_run aucune branche
                # n'existe → on garde la base d'origine).
                last_promoted_branch = pr.head
            # Compounding (#545) : mémorise le diff promu (corrigé) pour le réappliquer
            # aux rounds suivants (baseline cumulative) — y compris en dry_run.
            promoted_diffs.append(final_diff)
            result.promoted.append(PromotedImprovement(dimension.value, gate.delta, pr.number))
            plateau = 0
        else:
            result.rejected.append((dimension.value, gate.reason))
            plateau += 1
            if plateau >= plateau_rounds:
                result.stop_reason = STOP_PLATEAU
                break

    result.rounds = round_num
    return result
