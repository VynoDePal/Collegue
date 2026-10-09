"""Boucle d'amélioration continue (G4, epic #382, Phase 4 — capstone).

Après le MVP, fait tourner : **mesurer (G1) → proposer (G3) → générer un diff
(exécuteur) → mesurer après → gater (G2) → promouvoir (PR) ou jeter**, sous le
budget-temps (F2), et **s'arrête sur rendements décroissants** (les gains
plafonnent) ou au budget.

Gate **AVANT la PR** : un diff qui régresse (tests/revue/couverture/scan de secrets/contrats) ou n'améliore pas le score
n'ouvre **pas** de PR (« rollback » = abandon avant promotion). Le merge des PR
d'amélioration reste **humain** (§6). ``dry_run`` par défaut (aucune écriture).

``measure_fn`` est injectable (mesures scriptées en CI) ; les briques de
l'exécuteur sont importées **paresseusement** pour garder ``collegue.improve``
léger. Le câblage du **mode `improving`** du pilote (enchaîner build → amélioration)
est laissé à l'appelant/F4 (optionnel) — ce module fournit l'entrée ``run_improvement``.

Le runtime l'appelle après le handoff BUILD → IMPROVE et peut lui fournir le hook
Phase 5 d'auto-merge ; sans ce hook, le comportement reste strictement humain.
"""

from __future__ import annotations

import inspect
import math
from dataclasses import dataclass, field, replace
from typing import Any, List, Optional, Tuple

from collegue.improve.gate import DEFAULT_MIN_GAIN, evaluate
from collegue.improve.metrics import (
    DEFAULT_COVERAGE_COMMAND,
    DEFAULT_WEIGHTS,
    CompositeWeights,
    autofix_lint,
    measure,
    persist,
)
from collegue.improve.proposer import AttemptRecord, build_improvement_task, next_dimension
from collegue.state.budget_ledger import REFUSED_DEADLINE, BudgetRefused

# Raisons d'arrêt.
STOP_PLATEAU = "plateau"  # rendements décroissants : les gains plafonnent
STOP_PAUSED_BUDGET = "paused_budget"
STOP_DEADLINE = "deadline_reached"
STOP_SAFETY_CAP = "safety_cap"
STOP_AUTOMERGE_BLOCKED = "auto_merge_blocked"
STOP_POST_MERGE_GUARD = "post_merge_guard_failed"
STOP_AUTO_REVERT_RECOVERED = "auto_revert_recovered"
STOP_AUTO_REVERT_PENDING = "auto_revert_pending"
STOP_AUTO_REVERT_BASE_MOVED = "auto_revert_base_moved"
STOP_AUTO_REVERT_PUBLISH_FAILED = "auto_revert_publish_failed"
STOP_AUTO_REVERT_MERGE_FAILED = "auto_revert_merge_failed"
STOP_AUTO_REVERT_HEALTH_FAILED = "auto_revert_health_failed"
STOP_PHASE5_INCIDENT_PENDING = "phase5_incident_pending"


@dataclass(frozen=True)
class PromotedImprovement:
    """Une amélioration promue en PR."""

    dimension: str
    delta: float
    pr_number: Optional[int]
    auto_merged: bool = False
    reverted: bool = False
    # Vague 3 : preuve de livraison persistée (None en dry-run) et tête distante vérifiée de la PR.
    proof: Optional[Any] = None
    head_sha: Optional[str] = None


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
        f"scan statique de secrets (regex) pondéré {before.security_weighted:.1f} → {after.security_weighted:.1f} ; "
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
    promu ; ici on le **commite** pour que la base de comparaison reflète l'état
    cumulé. Sans commit, le diff capturé du round courant (vs base, cf.
    ``capture_diff``) ré-embarquerait les changements déjà promus → double-comptage au
    round suivant. Après commit, la mesure baseline porte sur le projet **cumulé
    amélioré** : le score monte round après round et le proposeur (métrique-driven) passe
    à la dimension suivante car la métrique d'une dimension réglée redevient bonne sur
    l'état cumulé. ``execution.diff`` ne contient alors que les nouveaux changements du
    round.

    **Frontière Git (vague 1)** : le seed ET le commit passent par les métadonnées de
    contrôle du workspace (``<workspace>.control``, hors montage) — jamais par son
    ``.git`` que l'agent/les tests ont pu écrire. La nouvelle base fiable est le
    ``HEAD`` de CONTRÔLE (``advance_base``) ; ni hook, ni config, ni filtre du workspace
    ne s'exécute pendant le compounding.

    Best-effort : un diff inapplicable (conflit, base déplacée) est **sauté** —
    ``apply_seed_diff`` restaure alors un arbre propre — plutôt que d'échouer le run.
    Renvoie le nombre de diffs effectivement intégrés (commités). Un workspace non géré
    lève ``WorkspaceError`` (fail-closed).
    """
    from collegue.executor.workspace import advance_base, apply_seed_diff, refresh_agent_view

    applied = 0
    for index, diff in enumerate(diffs):
        if not apply_seed_diff(workspace, diff, git_bin=git_bin, refresh_view=False):
            continue
        if advance_base(
            workspace,
            f"compounding: amélioration promue #{index + 1}",
            git_bin=git_bin,
            email="improve@collegue.local",
            name="collegue-improve",
            refresh_view=False,
        ):
            applied += 1
    if applied:
        # une seule régénération de la copie jetable de l'agent pour toute la cascade
        refresh_agent_view(workspace, git_bin=git_bin)
    return applied


async def _run_improvement_impl(
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
    coverage_command: str = DEFAULT_COVERAGE_COMMAND,
    measure_fn=measure,
    promotion_hook=None,
    recovery_hook=None,
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

    ``runner`` : réservé aux fixtures non gérées — les workspaces de cette boucle sont
    GÉRÉS (frontière Git : métadonnées de contrôle hors montage, cf.
    ``collegue.executor.git_boundary``), sur lesquels un runner injecté est refusé ;
    en production il reste ``None``.

    **Contraintes bloquantes (vague 3), sans dérogation** — le score composite ne rachète jamais : tests verts, revue
    rendue ET non bloquante, couverture mesurée avant/après et sans baisse, scan statique de secrets non aggravé, TOUS
    les contrats d'acceptation scellés des tâches déjà livrées toujours verts (exigés dès que l'état durable les demande
    ou qu'une tâche livrée en porte), et contenu testé == contenu publié (arbre Git scellé, binaire/lien/mode refusés,
    dérive détectée). Une PR réelle n'est ouverte qu'avec une preuve de livraison vérifiée contre l'arbre distant,
    persistée dans le journal de décisions (``load_delivery_proof``).

    ``recovery_hook`` est appelé avant le premier round réel et doit réconcilier
    tout incident Phase 5 durable. ``promotion_hook`` (Phase 5, opt-in) est appelé immédiatement après chaque PR
    réelle. Il doit réaliser la séquence CI → merge → resync → garde. La boucle
    s'arrête au premier refus : continuer créerait une PR enfant sur une branche
    non intégrée. Absent (défaut) : comportement historique, PR stackées et merge
    humain. Jamais appelé en dry-run.
    """
    # Imports paresseux : garder l'import de ``collegue.improve`` léger. ``pilot.budget``
    # est aussi lazy car importer le sous-module déclenche ``pilot/__init__`` (→ driver
    # → exécuteur) — on ne veut pas tirer tout ça au simple import du package improve.
    from collegue.executor.agent import IssueSpec
    from collegue.executor.delivery_proof import (
        DeliveryProofError,
        seal_tested_content,
        verify_tested_content,
    )
    from collegue.executor.pr import (
        DeliveryDriftError,
        assert_deliverable,
        assert_representable,
        capture_delivery_snapshot,
        verify_delivery_snapshot,
    )
    from collegue.executor.runner import capture_diff, run_issue
    from collegue.executor.workspace import prepare_workspace
    from collegue.improve.promotion import (
        NO_CONTRACTS,
        build_improvement_draft,
        promotion_refusal,
        replay_delivered_contracts,
    )
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

    # #573 : transmettre la commande de test configurée (``GATE_TEST_COMMAND``, alias
    # ``coverage_command`` ici, acheminée par le driver) à ``measure()`` pour que
    # ``tests_passed`` reflète le code de sortie de la VRAIE commande de test du projet.
    # Sans ça, ``measure()`` retombe sur ``DEFAULT_COVERAGE_COMMAND`` (pytest --cov en dur)
    # → tests rouges sur un projet à setup non trivial (make, monorepo, service DB) → garde
    # dure G2 rejette TOUTE amélioration. (#577 : ``parse_coverage`` reconnaît désormais
    # aussi go / JS-TS / lcov / cobertura, donc une commande custom à format non pytest-cov
    # rend quand même la couverture mesurable ; sinon terme conservateur 0.) Le kwarg n'est
    # émis qu'en override (≠ défaut) → chemin par défaut (et mesures scriptées en CI)
    # inchangé (symétrie avec ``_gate_options`` qui n'émet ``test_command`` qu'en override).
    measure_extra: dict = {}
    if coverage_command and coverage_command != DEFAULT_COVERAGE_COMMAND:
        measure_extra["coverage_command"] = coverage_command

    if not dry_run and recovery_hook is not None:
        try:
            recovered = recovery_hook()
            if inspect.isawaitable(recovered):
                recovered = await recovered
        except Exception as exc:  # reprise durable indisponible => aucun agent/PR
            result.stop_reason = STOP_PHASE5_INCIDENT_PENDING
            result.rejected.append(("phase5_recovery", f"reprise Phase 5 impossible: {exc}"))
            return result
        if not bool(getattr(recovered, "continue_loop", False)):
            result.stop_reason = str(getattr(recovered, "stop_reason", "phase5_incident_pending"))
            result.rejected.append(("phase5_recovery", str(getattr(recovered, "reason", "incident non résolu"))))
            return result

    try:
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
            before = await measure_fn(
                workspace.path, ctx, sandbox=sandbox, reviewer=reviewer, weights=weights, **measure_extra
            )

            # Baseline non fiable (composite non fini, ex. scan de secrets en échec → inf) :
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
            delivery_snapshot = capture_delivery_snapshot(
                workspace,
                final_files,
                diff=final_diff,
            )

            # Vague 3 : (1) tout format que la publication ne sait pas pousser fidèlement (binaire, lien) est refusé
            # AVANT la mesure ; (2) le contenu qui va être mesuré ET livré est figé (arbre Git complet) et les résidus
            # non livrables sont retirés : les tests ne peuvent pas réussir grâce à un fichier qui ne sera pas livré.
            try:
                assert_deliverable(delivery_snapshot)
                content = seal_tested_content(workspace.path)
                assert_representable(content)
            except DeliveryProofError as exc:
                history.append(AttemptRecord(dimension, improved=False))
                result.rejected.append((dimension.value, f"livraison non représentable : {exc}"))
                plateau += 1
                if plateau >= plateau_rounds:
                    result.stop_reason = STOP_PLATEAU
                    break
                continue

            after = await measure_fn(
                workspace.path,
                ctx,
                sandbox=sandbox,
                reviewer=reviewer,
                diff=final_diff,
                weights=weights,
                **measure_extra,
            )
            # Non-régression des contrats livrés : sources relues dans l'ÉTAT durable (jamais le workspace), rejouées
            # sur le candidat. Un seul contrat cassé, absent ou invérifiable refuse la promotion.
            contracts = NO_CONTRACTS
            if getattr(after, "tests_passed", False):
                contracts = replay_delivered_contracts(workspace.path, manager, project_id, sandbox=sandbox)
            try:
                verify_delivery_snapshot(workspace, delivery_snapshot)
                # Arbre COMPLET : une mesure/un contrat qui modifie un fichier suivi invalide la preuve.
                verify_tested_content(workspace.path, content)
            except DeliveryDriftError as exc:
                # #582 : measure_fn exécute du code projet en RW. Une mesure verte
                # portant sur des octets différents du snapshot livrable est invalide.
                history.append(AttemptRecord(dimension, improved=False))
                result.rejected.append((dimension.value, f"intégrité du livrable refusée : {exc}"))
                plateau += 1
                if plateau >= plateau_rounds:
                    result.stop_reason = STOP_PLATEAU
                    break
                continue
            result.final_score = after.composite
            gate = evaluate(before, after, min_gain=min_gain)
            draft = build_improvement_draft(content, delivery_snapshot.paths, before, after, gate, contracts)
            promotable = gate.accepted and draft.passed
            history.append(AttemptRecord(dimension, improved=promotable))

            if promotable:
                report = _improvement_quality_report(dimension.value, before, after, gate.delta)
                from collegue.executor.pr import open_pr
                from collegue.executor.workspace import branch_for_improvement

                try:
                    # Identité de publication DISTINCTE du BUILD et des passes précédentes (le compteur de round
                    # repart à 1 à chaque passe) : liée au contenu publié, jamais à ``collegue/issue-<round>``.
                    publish_workspace = replace(
                        workspace,
                        branch=branch_for_improvement(round_num, content.base_tree_sha, content.tree_sha),
                    )
                    pr = open_pr(
                        publish_workspace,
                        report,
                        improvement,
                        owner,
                        repo,
                        files_changed=final_files,
                        snapshot=delivery_snapshot,
                        # Stacking (#554) : base = branche de la promotion précédente si elle
                        # existe (mode --execute), sinon la base d'origine. → diff de PR propre.
                        base=(last_promoted_branch or base),
                        clients=clients,
                        dry_run=dry_run,
                        manager=manager,
                        project_id=project_id,
                        # Le numéro est un compteur de round, pas une vraie issue → pas de Closes.
                        closes_issue=False,
                        draft=draft,
                    )
                except DeliveryProofError as exc:
                    # Publication refusée (base déplacée, arbre distant ≠ arbre testé, PR préexistante d'une autre
                    # révision, preuve non persistable…) : AUCUNE promotion, le diff est jeté.
                    history[-1] = AttemptRecord(dimension, improved=False)
                    result.rejected.append((dimension.value, f"livraison refusée : {exc}"))
                    plateau += 1
                    if plateau >= plateau_rounds:
                        result.stop_reason = STOP_PLATEAU
                        break
                    continue
                hook_outcome = None
                hook_error = None
                if not dry_run and promotion_hook is not None:
                    if pr.skipped and pr.proof is None:
                        # Une PR retrouvée peut avoir été modifiée hors du snapshot de ce round : pas d'auto-merge
                        # sans preuve d'identité complète. Depuis la vague 3, une PR retrouvée dont la tête a été
                        # RE-VÉRIFIÉE contre l'arbre testé porte sa preuve (``pr.proof``) et peut être fusionnée.
                        hook_error = "PR préexistante : auto-merge refusé sans nouvelle preuve de livraison"
                    else:
                        try:
                            hook_outcome = promotion_hook(pr)
                            if inspect.isawaitable(hook_outcome):
                                hook_outcome = await hook_outcome
                        except Exception as exc:  # noqa: BLE001 - hook externe fail-closed
                            hook_error = f"hook Phase 5 en erreur: {exc}"
                auto_merged = bool(getattr(hook_outcome, "merged", False))
                auto_reverted = bool(
                    getattr(getattr(hook_outcome, "remote_revert", None), "restored", False)
                    or getattr(hook_outcome, "stop_reason", None) == "auto_revert_recovered"
                )
                if not dry_run and not auto_reverted:
                    persist(manager, project_id, after)
                if auto_reverted:
                    # Le diff a été retiré de main : ne pas présenter son score comme
                    # l'état courant ni le réinjecter au prochain round/run.
                    result.final_score = before.composite
                    history[-1] = AttemptRecord(dimension, improved=False)
                    auto_merged = False
                    last_promoted_branch = None
                    promoted_diffs.clear()
                elif auto_merged:
                    # Le hook a resynchronisé repo_source sur main : le diff promu y est
                    # déjà présent. Le réappliquer via compounding le dupliquerait.
                    last_promoted_branch = None
                    promoted_diffs.clear()
                else:
                    if not dry_run:
                        # Stacking humain historique (#554).
                        last_promoted_branch = pr.head
                    promoted_diffs.append(final_diff)
                result.promoted.append(
                    PromotedImprovement(
                        dimension.value,
                        gate.delta,
                        pr.number,
                        auto_merged,
                        reverted=auto_reverted,
                        proof=pr.proof,
                        head_sha=pr.head_sha,
                    )
                )
                plateau = 0
                if hook_error is not None:
                    result.rejected.append((dimension.value, hook_error))
                    result.stop_reason = STOP_AUTOMERGE_BLOCKED
                    break
                if hook_outcome is not None and not bool(getattr(hook_outcome, "continue_loop", False)):
                    reason = str(getattr(hook_outcome, "reason", "auto-merge non abouti"))
                    result.rejected.append((dimension.value, reason))
                    result.stop_reason = str(getattr(hook_outcome, "stop_reason", None) or STOP_AUTOMERGE_BLOCKED)
                    break
            else:
                result.rejected.append((dimension.value, promotion_refusal(gate, draft)))
                plateau += 1
                if plateau >= plateau_rounds:
                    result.stop_reason = STOP_PLATEAU
                    break

    except BudgetRefused as refusal:
        # Refus AVANT émission (plafond, blocage strict, échéance, tarif inconnu…) : on s'arrête en pause
        # budget en CONSERVANT le bilan partiel (promotions déjà acquises, rejets).
        result.rejected.append(("budget", f"refus budgétaire ({refusal.code}) : {refusal}"))
        result.stop_reason = STOP_DEADLINE if refusal.code == REFUSED_DEADLINE else STOP_PAUSED_BUDGET
    result.rounds = round_num
    return result


async def run_improvement(project_id: int, repo_source: str, ctx, **kwargs) -> ImprovementResult:
    """Fait tourner la boucle d'amélioration (voir :func:`_run_improvement_impl`) sous le registre durable.

    Pour un run RÉEL, ouvre/retrouve le scope du projet (le MÊME que le BUILD : cumul commun BUILD →
    IMPROVE, y compris après redémarrage), le rend autoritaire pour ``budget`` et lie registre et scope
    au contexte : chaque round réserve l'allocation du worker et chaque appel de sampling AVANT émission.
    """
    import contextlib

    from collegue.core.llm.budget_guard import bind_budget, current_binding
    from collegue.pilot.budget import BudgetTimeController, attach_project_budget

    budget = kwargs.get("budget") or BudgetTimeController()
    kwargs["budget"] = budget
    manager = kwargs.get("manager")
    binding = contextlib.nullcontext()
    if not kwargs.get("dry_run", True) and current_binding() is None:
        scope = attach_project_budget(budget, manager, project_id)
        if scope is not None:
            binding = bind_budget(
                manager.budget_ledger, scope.scope_key, settings=budget.settings, deadline=budget.deadline
            )
    with binding:
        return await _run_improvement_impl(project_id, repo_source, ctx, **kwargs)
