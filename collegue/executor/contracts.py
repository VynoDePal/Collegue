"""Contrats d'acceptation scellés (vague 3) : rejeu des contrats courant et déjà livrés, preuve négative.

Les oracles §4.7 sont écrits AVANT le code, scellés dans l'état durable (``Task.acceptance_test_*``) et approuvés
avec le plan. Ce module les relit depuis cet état — jamais depuis le workspace — et les rejoue :

- **BUILD** : le contrat COURANT (nouveau contrat) ET tous les contrats déjà livrés (tâches ``done``/``merged``) ;
- **IMPROVE** : TOUS les contrats livrés (non-régression).

Preuve négative (nouveau contrat uniquement) : le MÊME oracle (même SHA-256) exécuté sur la PRÉIMAGE connue (base de
confiance, avant tout changement de la tâche) doit échouer par une assertion de la phase ``call`` d'un test réellement
exécuté ; collecte, import, timeout, skip, panne réseau ne suffisent pas. Il doit ensuite réussir sur le candidat. Un
contrat déjà livré a normalement une baseline verte : il n'est pas exigé rouge, seulement vert sur le candidat.

Toute preuve incomplète est refusée : plan non approuvé/modifié, provenance ou empreinte incohérente, oracle absent,
rapport d'exécution incomplet.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Iterator, List, Optional, Sequence, Tuple

from collegue.executor.delivery_proof import OracleEvidence, OracleRun
from collegue.executor.oracle import (
    STATUS_GREEN,
    STATUS_RED_ASSERTION,
    collect_oracle_report,
    judge_oracle_run,
    new_nonce,
    oracle_pytest_command,
)

DELIVERED_STATUSES = frozenset({"done", "merged"})
ROLE_CURRENT = "current"
ROLE_DELIVERED = "delivered"
PREIMAGE_NOT_REQUIRED = "not-required"
PREIMAGE_RED = "red-assertion"

SCHEMA_VERSION = 1
GENERATOR = "collegue.planner.acceptance_tests"


class ContractError(RuntimeError):
    """Contrat scellé introuvable, non approuvé, altéré ou invérifiable : preuve refusée."""


@dataclass(frozen=True)
class SealedContract:
    """Contrat relu et VÉRIFIÉ depuis l'état durable (source + empreintes + provenance)."""

    task_id: int
    title: str
    source: str
    source_sha256: str
    contract_sha256: str
    provenance_sha256: str
    role: str

    @property
    def label(self) -> str:
        return f"task-{self.task_id}"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _valid_sha256(value: Any) -> bool:
    import re

    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def require_plan_approved(manager: Any, project_id: int, approval_check: Optional[Callable[..., Any]] = None) -> None:
    """Plan toujours approuvé et inchangé depuis son approbation, sinon :class:`ContractError`."""
    try:
        checker = approval_check
        if checker is None:
            from collegue.planner.plan_review import require_approved

            checker = require_approved
        checker(manager, project_id)
    except Exception as exc:  # noqa: BLE001 - tout doute sur l'approbation bloque
        raise ContractError(f"plan non approuvé ou modifié depuis son approbation : {exc}") from exc


def verify_task_contract(manager: Any, project_id: int, task: Any, *, role: str) -> SealedContract:
    """Vérifie provenance, empreintes et SHA-256 de l'oracle d'UNE tâche ; renvoie le contrat scellé."""
    from collegue.planner.acceptance_tests import (
        criteria_text,
        known_acceptance_prompt_sha256,
        sha256_text,
        spec_text,
        task_contract_sha256,
        validate_pytest_source,
    )

    try:
        project = manager.get_project(project_id)
        tasks = manager.get_tasks(project_id)
    except Exception as exc:  # noqa: BLE001 - état durable indisponible = fail-closed
        raise ContractError(f"lecture de l'oracle plan-time impossible : {exc}") from exc
    if project is None or task is None:
        raise ContractError("tâche ou projet de l'oracle plan-time introuvable")
    task_id = int(getattr(task, "id", 0) or 0)
    if int(getattr(task, "project_id", -1)) != int(project_id):
        raise ContractError(f"l'oracle de la tâche {task_id} appartient à un autre projet")
    source = getattr(task, "acceptance_test_source", None)
    stored_sha = getattr(task, "acceptance_test_sha256", None)
    provenance = getattr(task, "acceptance_test_provenance", None)
    if not isinstance(source, str) or not source.strip():
        raise ContractError(f"source de l'oracle d'acceptation absent (tâche {task_id})")
    if not _valid_sha256(stored_sha):
        raise ContractError(f"SHA-256 de l'oracle absent ou invalide (tâche {task_id})")
    import hmac

    if not hmac.compare_digest(stored_sha, _sha256(source)):
        raise ContractError(f"SHA-256 de l'oracle incohérent avec son source (tâche {task_id})")
    if not isinstance(provenance, dict):
        raise ContractError(f"provenance de l'oracle absente ou invalide (tâche {task_id})")
    if provenance.get("schema_version") != SCHEMA_VERSION:
        raise ContractError("version de provenance de l'oracle non supportée")
    if provenance.get("generator") != GENERATOR:
        raise ContractError("générateur de provenance de l'oracle non autorisé")
    if provenance.get("role") != "qa":
        raise ContractError("rôle de provenance de l'oracle non indépendant du codeur")
    if provenance.get("runner") != "pytest":
        raise ContractError("runner de l'oracle d'acceptation non supporté")
    for field in ("requested_provider", "requested_model"):
        if not isinstance(provenance.get(field), str) or not provenance[field].strip():
            raise ContractError(f"provenance de l'oracle incomplète : {field} absent")
    for field in ("prompt_sha256", "spec_sha256", "criteria_sha256", "contract_sha256"):
        if not _valid_sha256(provenance.get(field)):
            raise ContractError(f"provenance de l'oracle incomplète : {field} invalide")
    try:
        # Même défense qu'au verdict historique : une source+SHA remplacés puis ré-approuvés ne peuvent
        # pas devenir un `skip`/`xfail`, un module sans test ou une tautologie.
        validate_pytest_source(source)
        expected = {
            "spec_sha256": sha256_text(spec_text(getattr(project, "spec", "") or "")),
            "criteria_sha256": sha256_text(criteria_text(task)),
            "contract_sha256": task_contract_sha256(project_id, task),
            "prompt_sha256": known_acceptance_prompt_sha256(
                getattr(project, "spec", "") or "", task, tasks, project_id
            ),
        }
    except Exception as exc:  # noqa: BLE001 - oracle/provenance douteux = fail-closed
        raise ContractError(f"oracle ou empreintes de provenance invérifiables (tâche {task_id}) : {exc}") from exc
    labels = {
        "spec_sha256": "SPEC",
        "criteria_sha256": "critères",
        "contract_sha256": "contrat de tâche",
        "prompt_sha256": "prompt QA",
    }
    for field, wanted in expected.items():
        # Le prompt QA peut avoir plusieurs versions CONNUES (courante + historiques) ; les autres champs sont exacts.
        candidates = wanted if isinstance(wanted, tuple) else (wanted,)
        if not any(hmac.compare_digest(provenance[field], candidate) for candidate in candidates):
            raise ContractError(f"empreinte {labels[field]} de l'oracle d'acceptation incohérente (tâche {task_id})")
    stamp = provenance.get("generated_at")
    if not isinstance(stamp, str) or not stamp.endswith("Z"):
        raise ContractError("horodatage de provenance de l'oracle absent ou invalide")
    from datetime import datetime

    try:
        datetime.fromisoformat(stamp[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractError("horodatage de provenance de l'oracle absent ou invalide") from exc
    return SealedContract(
        task_id=task_id,
        title=str(getattr(task, "title", "") or ""),
        source=source,
        source_sha256=stored_sha,
        contract_sha256=expected["contract_sha256"],
        provenance_sha256=_sha256(_canonical(provenance)),
        role=role,
    )


def load_current_contract(
    manager: Any, project_id: int, issue: Any, *, approval_check: Optional[Callable[..., Any]] = None
) -> SealedContract:
    """Contrat de la tâche COURANTE d'après ``issue.source_task_id`` (critères et titre doivent correspondre)."""
    from collegue.textnorm import inline

    require_plan_approved(manager, project_id, approval_check)
    task_id = getattr(issue, "source_task_id", None)
    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0:
        raise ContractError("source_task_id absent ou invalide : oracle plan-time introuvable")
    try:
        task = manager.get_task(task_id)
    except Exception as exc:  # noqa: BLE001
        raise ContractError(f"lecture de l'oracle plan-time impossible : {exc}") from exc
    if task is None:
        raise ContractError("tâche ou projet de l'oracle plan-time introuvable")
    if int(getattr(task, "project_id", -1)) != int(project_id):
        raise ContractError("l'oracle référencé appartient à un autre projet")
    expected_criterion = inline(getattr(task, "acceptance", "") or "")
    actual = tuple(inline(value) for value in issue.acceptance_criteria if inline(value))
    if actual != ((expected_criterion,) if expected_criterion else ()):
        raise ContractError("critères de l'issue différents du contrat QA persisté")
    if inline(issue.title) != inline(getattr(task, "title", "") or ""):
        raise ContractError("titre de l'issue différent du contrat QA persisté")
    return verify_task_contract(manager, project_id, task, role=ROLE_CURRENT)


def project_requires_contracts(manager: Any, project_id: int) -> bool:
    """Vrai si l'état durable EXIGE des contrats scellés pour ce projet (``Project.acceptance_tests_required``).

    L'état prévaut sur tout booléen d'un appelant ou d'un rapport : un projet qui les exige ne peut recevoir ni preuve
    ni promotion sans contrats rejoués. Un état illisible est un refus (impossible d'affirmer qu'ils ne sont pas exigés).
    """
    try:
        project = manager.get_project(project_id)
    except Exception as exc:  # noqa: BLE001 - état illisible = on ne peut pas affirmer l'absence d'exigence
        raise ContractError(f"lecture du projet impossible : {exc}") from exc
    if project is None:
        raise ContractError("projet introuvable : exigence de contrats invérifiable")
    return bool(getattr(project, "acceptance_tests_required", False))


def has_sealed_contracts(manager: Any, project_id: int) -> bool:
    """Vrai si au moins une tâche déjà livrée porte un oracle scellé (le BUILD a donc EXIGÉ des contrats).

    Sert à IMPROVE pour décider si la non-régression des contrats est une obligation de la preuve. Un projet dont
    AUCUNE tâche livrée n'a d'oracle (gate d'acceptation jamais activé) n'a rien à rejouer ; dès qu'un seul existe, tous
    les contrats livrés doivent être valides (:func:`load_delivered_contracts`).
    """
    try:
        tasks = list(manager.get_tasks(project_id))
    except Exception as exc:  # noqa: BLE001 - lecture impossible = on ne peut pas affirmer l'absence de contrat
        raise ContractError(f"lecture des tâches livrées impossible : {exc}") from exc
    return any(
        str(getattr(task, "status", "") or "") in DELIVERED_STATUSES
        and (getattr(task, "acceptance_test_source", None) or getattr(task, "acceptance_test_sha256", None))
        for task in tasks
    )


def load_delivered_contracts(
    manager: Any,
    project_id: int,
    *,
    exclude_task_id: Optional[int] = None,
    approval_check: Optional[Callable[..., Any]] = None,
) -> Tuple[SealedContract, ...]:
    """Contrats de TOUTES les tâches déjà livrées (``done``/``merged``) ; un seul invérifiable ⇒ refus."""
    require_plan_approved(manager, project_id, approval_check)
    try:
        tasks = list(manager.get_tasks(project_id))
    except Exception as exc:  # noqa: BLE001
        raise ContractError(f"lecture des tâches livrées impossible : {exc}") from exc
    contracts: List[SealedContract] = []
    for task in sorted(tasks, key=lambda item: int(getattr(item, "id", 0) or 0)):
        if str(getattr(task, "status", "") or "") not in DELIVERED_STATUSES:
            continue
        if exclude_task_id is not None and int(task.id) == int(exclude_task_id):
            continue
        # Une tâche livrée SANS contrat scellé rend la non-régression invérifiable : refus (preuve incomplète).
        contracts.append(verify_task_contract(manager, project_id, task, role=ROLE_DELIVERED))
    return tuple(contracts)


# ── exécution ────────────────────────────────────────────────────────────────────────────────────────


def _run_command(workspace: str, entries: Sequence[Tuple[str, str]], nonce: str, *, sandbox) -> str:
    from collegue.executor.quality_gate import deps_install_prelude

    pytest_cmd = oracle_pytest_command(entries, nonce)
    prelude = deps_install_prelude(workspace)
    return f"({prelude}) && {pytest_cmd}" if prelude else pytest_cmd


def _strip_report(text: str) -> str:
    """Sortie du conteneur sans la ligne de rapport machine (inutile à un humain ou à un agent)."""
    from collegue.executor.oracle import ORACLE_MARKER

    kept = [line for line in (text or "").splitlines() if ORACLE_MARKER not in line]
    return "\n".join(kept).strip()


@dataclass(frozen=True)
class OracleBatch:
    runs: dict  # {task_id: OracleRun}
    output: str


def execute_oracles(workspace: str, contracts: Sequence[SealedContract], *, sandbox, phase: str) -> OracleBatch:
    """Exécute ``contracts`` dans UN conteneur et juge chacun d'après le rapport complet.

    Toute impossibilité d'exécuter (exception du sandbox, délai, rapport absent) rend CHAQUE contrat ``invalid`` avec
    son motif : jamais un vert par défaut.
    """
    if not contracts:
        return OracleBatch({}, "")
    nonce = new_nonce()
    entries = [(c.label, c.source) for c in contracts]
    command = _run_command(workspace, entries, nonce, sandbox=sandbox)
    try:
        res = sandbox.run_tests(workspace, command)
    except Exception as exc:  # noqa: BLE001 - checker activé = erreur bloquante
        failure = OracleRun(phase=phase, status="invalid", reason=f"exécution de l'oracle impossible : {exc}")
        return OracleBatch({c.task_id: failure for c in contracts}, "")
    text = "\n".join(part for part in (getattr(res, "stdout", ""), getattr(res, "stderr", "")) if part)
    report, problem = collect_oracle_report(
        text,
        nonce,
        exit_code=getattr(res, "exit_code", None),
        timed_out=bool(getattr(res, "timed_out", False)),
    )
    runs = {c.task_id: judge_oracle_run(report, c.label, phase=phase, problem=problem) for c in contracts}
    return OracleBatch(runs, _strip_report(text))


@dataclass(frozen=True)
class ReplayResult:
    ok: bool
    reason: str
    evidence: Tuple[OracleEvidence, ...]
    output: str = ""


PreimageProvider = Callable[[], "contextlib.AbstractContextManager[str]"]


def contract_evidence(
    contract: SealedContract,
    *,
    expected_preimage: str,
    preimage: Optional[OracleRun],
    candidate: Optional[OracleRun],
) -> OracleEvidence:
    reason = ""
    ok = True
    if candidate is None:
        ok, reason = False, "contrat non exécuté sur le candidat"
    elif candidate.status != STATUS_GREEN:
        ok = False
        reason = f"candidat {candidate.status}" + (f" : {candidate.reason}" if candidate.reason else "")
    if ok and expected_preimage == PREIMAGE_RED:
        if preimage is None:
            ok, reason = False, "preuve négative absente : préimage non exécutée"
        elif preimage.status == STATUS_GREEN:
            ok, reason = (
                False,
                ("l'oracle courant passe déjà sur la préimage : il ne discrimine pas (preuve négative absente)"),
            )
        elif preimage.status != STATUS_RED_ASSERTION:
            ok, reason = (
                False,
                "preuve négative invalide sur la préimage" + (f" : {preimage.reason}" if preimage.reason else ""),
            )
    return OracleEvidence(
        task_id=contract.task_id,
        role=contract.role,
        source_sha256=contract.source_sha256,
        contract_sha256=contract.contract_sha256,
        provenance_sha256=contract.provenance_sha256,
        expected_preimage=expected_preimage,
        preimage=preimage,
        candidate=candidate,
        passed=ok,
        reason=reason,
    )


def replay_contracts(
    candidate_workspace: str,
    *,
    current: Optional[SealedContract],
    delivered: Sequence[SealedContract],
    sandbox,
    preimage: Optional[PreimageProvider] = None,
    require_negative: bool = True,
) -> ReplayResult:
    """Rejoue les contrats sur le candidat (+ preuve négative du contrat courant sur la préimage).

    ``current`` est exigé rouge-par-assertion sur la préimage puis vert sur le candidat ; ``delivered`` doit rester vert
    sur le candidat. La préimage n'est exécutée que si le candidat est déjà vert (le gate est sinon rouge).
    """
    contracts: List[SealedContract] = ([current] if current is not None else []) + list(delivered)
    if not contracts:
        return ReplayResult(ok=True, reason="aucun contrat à rejouer", evidence=())
    seen = set()
    for contract in contracts:
        if contract.task_id in seen:
            raise ContractError(f"contrat dupliqué pour la tâche {contract.task_id}")
        seen.add(contract.task_id)
    batch = execute_oracles(candidate_workspace, contracts, sandbox=sandbox, phase="candidate")
    candidate_runs = batch.runs
    preimage_run: Optional[OracleRun] = None
    if current is not None and require_negative:
        candidate_ok = candidate_runs[current.task_id].status == STATUS_GREEN
        if candidate_ok and all(candidate_runs[c.task_id].status == STATUS_GREEN for c in delivered):
            if preimage is None:
                preimage_run = OracleRun(
                    phase="preimage",
                    status="invalid",
                    reason="préimage indisponible : aucune base connue sur laquelle prouver que l'oracle échoue",
                )
            else:
                try:
                    with preimage() as base_path:
                        preimage_run = execute_oracles(base_path, [current], sandbox=sandbox, phase="preimage").runs[
                            current.task_id
                        ]
                except ContractError:
                    raise
                except Exception as exc:  # noqa: BLE001 - préimage inexploitable = refus
                    preimage_run = OracleRun(
                        phase="preimage", status="invalid", reason=f"préimage inexploitable : {exc}"
                    )
    evidence: List[OracleEvidence] = []
    for contract in contracts:
        is_current = contract.role == ROLE_CURRENT
        evidence.append(
            contract_evidence(
                contract,
                expected_preimage=PREIMAGE_RED if (is_current and require_negative) else PREIMAGE_NOT_REQUIRED,
                preimage=preimage_run if is_current else None,
                candidate=candidate_runs.get(contract.task_id),
            )
        )
    failures = [e for e in evidence if not e.passed]
    if failures:
        # Le motif le plus utile d'abord : un contrat dont le CANDIDAT n'est pas vert (régression réelle) avant une
        # preuve négative simplement non exécutée parce que le candidat n'était pas entièrement vert.
        failures.sort(key=lambda e: 0 if (e.candidate is None or e.candidate.status != STATUS_GREEN) else 1)
        first = failures[0]
        return ReplayResult(
            ok=False,
            reason=f"contrat {first.role} de la tâche {first.task_id} refusé : {first.reason}",
            evidence=tuple(evidence),
            output=batch.output,
        )
    return ReplayResult(ok=True, reason="", evidence=tuple(evidence), output=batch.output)


@contextlib.contextmanager
def fresh_preimage_workspace(repo_source: str, issue: Any, expected_base_sha: str) -> Iterator[str]:
    """Clone neuf de ``repo_source`` à la base de confiance attendue, supprimé ensuite (jamais l'ancien workspace).

    Refuse si la base clonée n'est pas celle sur laquelle les contrôles du candidat ont tourné.
    """
    import shutil

    from collegue.executor.workspace import prepare_workspace, trusted_base

    workspace = prepare_workspace(repo_source, issue)
    try:
        base = trusted_base(workspace)
        if base != expected_base_sha:
            raise ContractError(
                f"la préimage ({base[:12]}) n'est pas la base testée ({expected_base_sha[:12]}) : dépôt déplacé"
            )
        yield workspace.path
    finally:
        shutil.rmtree(_parent_of(workspace.path), ignore_errors=True)


def _parent_of(path: str) -> str:
    import os

    return os.path.dirname(os.path.realpath(path))
