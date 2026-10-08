"""Promotion d'une amélioration (vague 3) : contraintes bloquantes COMMUNES à BUILD et IMPROVE.

Le score composite est un objectif ; il ne rachète jamais une contrainte bloquante. Ce module compose, pour un round
d'amélioration, les verdicts d'une :class:`~collegue.executor.delivery_proof.ProofDraft` (même modèle de preuve que le
BUILD) :

- ``content_integrity`` : le contenu mesuré/testé est exactement l'arbre Git scellé, livrable sans omission ;
- ``tests`` : la commande de test du projet est verte sur le candidat ;
- ``review`` : un reviewer a rendu un verdict sur le diff et il n'est pas bloquant (une panne ≠ revue propre) ;
- ``coverage`` : mesurée avant ET après et sans baisse (mesure indispensable) ;
- ``secret_scan`` : le scan STATIQUE de secrets (regex, hors tests/fixtures/lockfiles) ne s'aggrave pas — ce n'est
  pas un audit de sécurité complet ;
- ``contracts`` : TOUS les contrats d'acceptation scellés des tâches déjà livrées restent verts sur le candidat ;
- ``gate`` : le gate métrique (gain réel, lint, complexité, vulns) accepte.

Aucune dérogation n'existe : ni revue facultative, ni baisse de couverture tolérée, ni contrats livrés ignorés. Les
contrats sont exigés dès que l'ÉTAT DURABLE les demande (``Project.acceptance_tests_required``) ou qu'une tâche déjà
livrée porte un oracle scellé — jamais sur la foi d'un paramètre de l'appelant.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Tuple

from collegue.executor.contracts import (
    ContractError,
    contract_evidence,
    execute_oracles,
    has_sealed_contracts,
    load_delivered_contracts,
    project_requires_contracts,
)
from collegue.executor.delivery_proof import (
    PHASE_IMPROVE,
    OracleEvidence,
    ProofDraft,
    TestedContent,
    describe_refusal,
)
from collegue.improve.gate import GateDecision
from collegue.improve.metrics import SECRET_SCAN_SCOPE, ProjectQualityMetrics

_EPSILON = 1e-9


@dataclass(frozen=True)
class ContractReplay:
    """Résultat du rejeu des contrats livrés sur un candidat d'amélioration."""

    required: bool
    ok: bool
    reason: str
    evidence: Tuple[OracleEvidence, ...] = ()


NO_CONTRACTS = ContractReplay(required=False, ok=True, reason="aucun contrat scellé dans les tâches livrées")


def replay_delivered_contracts(workspace: str, manager: Any, project_id: int, *, sandbox: Any) -> ContractReplay:
    """Rejoue tous les contrats scellés des tâches livrées sur ``workspace`` (sources lues dans l'ÉTAT, pas le workspace).

    Les contrats sont EXIGÉS dès que l'état durable les demande (``Project.acceptance_tests_required``) ou qu'au moins
    une tâche livrée porte un oracle ; aucun paramètre d'appelant ne peut lever cette exigence. Toute incohérence (plan
    modifié, provenance invalide, une tâche livrée sans oracle alors que d'autres en ont, exigence sans aucun contrat,
    rapport incomplet) ⇒ ``ok=False``. Sans manager, l'exigence est invérifiable : refus.
    """
    if manager is None or project_id is None:
        return ContractReplay(True, False, "état durable absent : exigence de contrats invérifiable")
    try:
        required = project_requires_contracts(manager, project_id) or has_sealed_contracts(manager, project_id)
        if not required:
            return NO_CONTRACTS
        contracts = load_delivered_contracts(manager, project_id)
    except ContractError as exc:
        return ContractReplay(True, False, str(exc))
    if not contracts:
        return ContractReplay(
            True, False, "contrats exigés par l'état du projet mais aucune tâche livrée ne porte d'oracle scellé"
        )
    batch = execute_oracles(workspace, contracts, sandbox=sandbox, phase="candidate")
    evidence = tuple(
        contract_evidence(c, expected_preimage="not-required", preimage=None, candidate=batch.runs.get(c.task_id))
        for c in contracts
    )
    failing = [e for e in evidence if not e.passed]
    if failing:
        first = failing[0]
        return ContractReplay(
            True,
            False,
            f"contrat livré de la tâche {first.task_id} cassé par l'amélioration : {first.reason}",
            evidence,
        )
    return ContractReplay(True, True, f"{len(evidence)} contrat(s) livré(s) toujours verts sur le candidat", evidence)


def build_improvement_draft(
    content: TestedContent,
    delivered_paths: Tuple[str, ...],
    before: ProjectQualityMetrics,
    after: ProjectQualityMetrics,
    gate: GateDecision,
    contracts: ContractReplay,
) -> ProofDraft:
    """Verdicts de la preuve IMPROVE d'après les métriques, le gate et le rejeu des contrats."""
    draft = ProofDraft(
        phase=PHASE_IMPROVE,
        content=content,
        contracts_required=contracts.required,
        delivered_paths=tuple(delivered_paths),
    )
    draft.add(
        "content_integrity", True, "arbre Git complet inchangé depuis le scellement, résidus non livrables retirés"
    )
    draft.add("tests", bool(after.tests_passed), "commande de test du projet sur le candidat")

    review_ok = after.review_measured and not after.review_blocking and not after.review_error
    if after.review_error:
        review_reason = f"revue indisponible : {after.review_error}"
    elif after.review_blocking:
        review_reason = "finding bloquant de la revue (veto)"
    elif not after.review_measured:
        review_reason = "aucune revue rendue sur le diff"
    else:
        review_reason = "revue non bloquante"
    draft.add("review", bool(review_ok), review_reason)

    measured = before.coverage_measured and after.coverage_measured
    held = measured and after.coverage_pct >= before.coverage_pct - _EPSILON
    if not measured:
        coverage_reason = "couverture non mesurée (avant ou après)"
    elif held:
        coverage_reason = f"{before.coverage_pct:.1f}% → {after.coverage_pct:.1f}% (sans baisse)"
    else:
        coverage_reason = f"baisse de couverture {before.coverage_pct:.1f}% → {after.coverage_pct:.1f}%"
    draft.add("coverage", bool(held), coverage_reason)

    scan_ok = math.isfinite(after.security_weighted) and after.security_weighted <= before.security_weighted
    draft.add(
        "secret_scan",
        scan_ok,
        f"{SECRET_SCAN_SCOPE} : {before.security_weighted:.1f} → {after.security_weighted:.1f} (pondéré)"
        " ; ce n'est pas un audit de sécurité",
    )

    if contracts.required:
        draft.oracles.extend(contracts.evidence)
        draft.add("contracts", contracts.ok, contracts.reason)
    draft.add("gate", bool(gate.accepted), gate.reason)
    return draft


def promotion_refusal(gate: GateDecision, draft: ProofDraft) -> str:
    """Motif du refus : celui du gate s'il rejette, sinon la preuve incomplète/refusée."""
    return gate.reason if not gate.accepted else describe_refusal(draft)
