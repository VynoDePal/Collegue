"""Preuve de livraison immuable (vague 3) : ce qui a été testé est ce qui est livré.

Une livraison BUILD ou IMPROVE n'est plus un ``passed=True`` en mémoire ni un hash dans le corps d'une PR :
c'est une **preuve** (:class:`DeliveryProof`) qui identifie

- le **contenu testé** : base de confiance (``base_sha``/``base_tree_sha``) et ``tree_sha`` = arbre Git COMPLET
  (modes, suppressions et fichiers de base inclus) de l'index de contrôle après ``git add -A`` ;
- les **oracles** exécutés (SHA-256 du source scellé, empreinte du contrat et de la provenance, verdict sur la
  préimage et sur le candidat) ;
- les **verdicts** de chaque contrainte (tests, revue, contrats, couverture…), chacun requis ou facultatif.

La preuve est liée à la tête distante (``head_sha``) APRÈS vérification de l'objet commit publié, puis persistée
dans le journal de décisions existant (``ProjectStateManager.record_decision``), hors du workspace. Elle se relit
depuis une NOUVELLE instance du manager avec :func:`load_delivery_proof`.

Fail-closed : un contenu modifié après le début des contrôles, un fichier de base altéré, un résidu ignoré qui
aurait nourri les tests, une tête distante dont l'arbre diffère ⇒ pas de preuve (:class:`DeliveryProofError`).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from collegue.executor.git_boundary import TrustedGit, WorkspaceError

PROOF_SCHEMA = "collegue.delivery-proof/1"
PHASE_BUILD = "build"
PHASE_IMPROVE = "improve"
PHASES = (PHASE_BUILD, PHASE_IMPROVE)

# Verdicts OBLIGATOIRES par phase : une preuve qui n'en porte pas un est incomplète (refus au chargement).
MANDATORY_VERDICTS = {
    PHASE_BUILD: ("content_integrity", "tests", "review"),
    PHASE_IMPROVE: ("content_integrity", "tests", "review", "coverage", "secret_scan"),
}

DECISION_PREFIX = "delivery-proof:v1:"
MAX_PARENT_WALK = 500  # profondeur maximale de remontée de la chaîne de commits distante
MAX_PURGED_LISTED = 50

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class DeliveryProofError(RuntimeError):
    """Preuve de livraison absente, invalide, incohérente ou non vérifiable (refus explicite)."""


class DeliveryDriftError(DeliveryProofError):
    """Le contenu vivant (ou publié) ne correspond plus au contenu testé."""


class DeliveryRemoteError(DeliveryProofError):
    """Le dépôt distant n'a pas pu être LU (réseau, API) : ce n'est pas un verdict sur le contenu.

    Refus fail-closed (aucune preuve), mais classé comme panne d'infrastructure retentable par le pipeline (jamais
    présenté à l'agent comme un défaut de son code).
    """


# ── modèle immuable ──────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Verdict:
    """Verdict d'UNE contrainte. ``required`` : son échec (ou son absence) interdit ``passed``."""

    name: str
    required: bool
    passed: bool
    reason: str = ""


@dataclass(frozen=True)
class OracleRun:
    """Résumé d'UNE exécution d'oracle (préimage ou candidat), tiré du rapport pytest complet."""

    phase: str  # preimage | candidate
    status: str  # green | red-assertion | invalid
    reason: str = ""
    executed: int = 0  # tests ayant atteint la phase call
    passed: int = 0
    failed: int = 0
    assertion_failures: int = 0
    skipped: int = 0
    xfailed: int = 0
    xpassed: int = 0
    collection_errors: int = 0
    errors: int = 0  # erreurs setup/teardown ou échecs hors assertion
    tests: Tuple[str, ...] = ()


@dataclass(frozen=True)
class OracleEvidence:
    """Preuve d'un contrat scellé : même SHA-256 sur préimage (si requis) et candidat."""

    task_id: int
    role: str  # current | delivered
    source_sha256: str
    contract_sha256: str
    provenance_sha256: str
    expected_preimage: str  # red-assertion | not-required
    preimage: Optional[OracleRun]
    candidate: Optional[OracleRun]
    passed: bool
    reason: str = ""


@dataclass(frozen=True)
class DeliveryProof:
    """Preuve immuable de livraison. Consommée par B via des ATTRIBUTS en lecture seule."""

    schema: str
    proof_id: str  # SHA-256 du contenu canonique (hors proof_id)
    owner: str
    repo: str
    project_id: int
    pr_number: int
    head_sha: str
    base_sha: str  # base distante de confiance (tip de la branche de base) dont l'arbre == base_tree_sha
    base_tree_sha: str
    tree_sha: str  # arbre Git COMPLET testé == arbre de head_sha
    phase: str
    passed: bool
    verdicts: Tuple[Verdict, ...]
    oracles: Tuple[OracleEvidence, ...]
    contracts_required: bool = False
    content_sha256: str = ""
    delivered_paths: Tuple[str, ...] = ()
    ignored_inputs_removed: Tuple[str, ...] = ()
    created_at: str = ""

    def verdict(self, name: str) -> Optional[Verdict]:
        for item in self.verdicts:
            if item.name == name:
                return item
        return None


@dataclass(frozen=True)
class TestedContent:
    """Contenu FIGÉ avant les contrôles : arbre Git complet + manifeste ; sert à détecter toute dérive."""

    __test__ = False  # pas une classe de test pour pytest

    base_sha: str
    base_tree_sha: str
    tree_sha: str
    content_sha256: str
    files_count: int
    ignored_inputs_removed: Tuple[str, ...] = ()
    purged_count: int = 0
    # Chemins NOUVEAUX ou dont le MODE a changé et n'est pas 100644 (exécutable, lien, sous-module) : la Contents API
    # écrit les fichiers neufs en 100644 et conserve le mode d'un fichier existant (contenu seul modifié = représentable) ;
    # elle ne sait pas représenter le reste → livraison refusée avant publication.
    special_modes: Tuple[str, ...] = ()


@dataclass
class ProofDraft:
    """Accumulateur MUTABLE (avant liaison à la PR) des verdicts et oracles d'une exécution."""

    phase: str
    content: Optional[TestedContent] = None
    verdicts: List[Verdict] = field(default_factory=list)
    oracles: List[OracleEvidence] = field(default_factory=list)
    contracts_required: bool = False
    delivered_paths: Tuple[str, ...] = ()

    def add(self, name: str, passed: bool, reason: str = "", *, required: bool = True) -> None:
        # Un verdict ne se remplace jamais en silence : le dernier verdict d'un même nom fait foi, mais
        # un échec ne peut pas être effacé par un succès postérieur du même nom (fail-closed).
        for index, existing in enumerate(self.verdicts):
            if existing.name == name:
                if existing.passed or not passed:
                    self.verdicts[index] = Verdict(name, required or existing.required, passed, reason)
                return
        self.verdicts.append(Verdict(name, required, passed, reason))

    @property
    def passed(self) -> bool:
        return derive_passed(tuple(self.verdicts), tuple(self.oracles), self.phase, self.contracts_required)


def derive_passed(
    verdicts: Sequence[Verdict], oracles: Sequence[OracleEvidence], phase: str, contracts_required: bool
) -> bool:
    """Résultat DÉRIVÉ : tous les verdicts requis passent, toutes les obligations de phase sont présentes."""
    names = {v.name for v in verdicts}
    for mandatory in MANDATORY_VERDICTS.get(phase, ()):
        if mandatory not in names:
            return False
    if contracts_required:
        if not oracles or "contracts" not in names:
            return False
        if not all(o.passed for o in oracles):
            return False
    return all(v.passed for v in verdicts if v.required) and bool(verdicts)


def describe_refusal(draft: "ProofDraft") -> str:
    """Motif lisible d'une preuve incomplète/refusée : verdicts requis en échec + obligations de phase absentes."""
    failing = [v for v in draft.verdicts if v.required and not v.passed]
    present = {v.name for v in draft.verdicts}
    missing = [name for name in MANDATORY_VERDICTS.get(draft.phase, ()) if name not in present]
    if draft.contracts_required and "contracts" not in present:
        missing.append("contracts")
    parts = [f"{v.name} ({v.reason})" if v.reason else v.name for v in failing] + [f"{n} (absent)" for n in missing]
    if draft.contracts_required:
        parts += [f"oracle tâche {o.task_id} ({o.reason or 'refusé'})" for o in draft.oracles if not o.passed]
    return "PREUVE DE LIVRAISON INCOMPLÈTE OU REFUSÉE — " + " ; ".join(parts or ["obligation requise non satisfaite"])


# ── arbre Git (hachage pur, vérifié contre git dans les tests) ─────────────────────────────────────────


def git_blob_sha1(data: bytes) -> str:
    """SHA-1 d'un objet blob Git (``blob <taille>\\0<contenu>``)."""
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data, usedforsecurity=False).hexdigest()


def tree_sha_from_entries(entries: Mapping[str, Tuple[str, str]]) -> str:
    """SHA d'arbre Git d'un ensemble ``{chemin: (mode, sha_blob)}`` (récursif, ordre Git)."""
    root: dict = {}
    for path, (mode, sha) in entries.items():
        parts = path.split("/")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise DeliveryProofError(f"chemin en conflit fichier/dossier: {path}")
        if isinstance(node.get(parts[-1]), dict):
            raise DeliveryProofError(f"chemin en conflit fichier/dossier: {path}")
        node[parts[-1]] = (mode, sha)

    def build(node: dict) -> str:
        items = []
        for name, value in node.items():
            if isinstance(value, dict):
                items.append((name + "/", b"40000", name, build(value)))
            else:
                mode, sha = value
                items.append((name, mode.encode(), name, sha))
        items.sort(key=lambda item: item[0].encode("utf-8"))
        body = b"".join(
            mode + b" " + name.encode("utf-8") + b"\0" + bytes.fromhex(sha) for _key, mode, name, sha in items
        )
        return hashlib.sha1(b"tree " + str(len(body)).encode() + b"\0" + body, usedforsecurity=False).hexdigest()

    return build(root)


# ── contenu testé (git de CONTRÔLE : jamais le .git du workspace) ──────────────────────────────────────


def _repo(workspace_path: str, *, git_bin: str = "git") -> TrustedGit:
    repo = TrustedGit.locate(workspace_path, git_bin=git_bin)
    if repo is None:
        raise DeliveryProofError(
            f"workspace non géré (aucun répertoire de contrôle Git) : {workspace_path} — contenu invérifiable"
        )
    return repo


def _ls_tree(repo: TrustedGit, tree: str) -> List[Tuple[str, str, str]]:
    out = repo.must("ls-tree", "-r", "-z", "--full-tree", tree, what="lecture de l'arbre testé")
    rows: List[Tuple[str, str, str]] = []
    for record in out.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        mode, kind, sha = meta.split(" ")
        rows.append((path, mode, sha if kind == "blob" else sha))
    return rows


def manifest_sha256(rows: Sequence[Tuple[str, str, str]]) -> str:
    digest = hashlib.sha256()
    for path, mode, sha in sorted(rows):
        digest.update(f"{mode} {sha} {path}\n".encode("utf-8"))
    return digest.hexdigest()


def seal_tested_content(
    workspace_path: str,
    *,
    purge_ignored: bool = True,
    only_paths: Optional[Sequence[str]] = None,
    git_bin: str = "git",
) -> TestedContent:
    """Fige le contenu qui va être testé ET livré.

    1. ``git add -A`` dans l'index PRIVÉ de contrôle (le contenu respecte le ``.gitignore`` : exactement ce que Git
       publierait) ; 2. ``purge_ignored`` : tout ce qui n'est PAS dans cet arbre (fichiers ignorés ou résiduels que
       les tests pourraient exploiter sans qu'ils soient livrés) est supprimé du workspace AVANT les contrôles — les
       sorties de build régénérables (caches, ``node_modules``…) sont reconstruites par le gate lui-même ; un module
       ou une donnée nécessaire mais non livrable fait alors échouer les tests au lieu de les faire réussir ;
       3. ``tree_sha`` = ``git write-tree`` de l'index.

    ``only_paths`` (re-scellement après une mutation nominative, p. ex. ``requirements.txt``) : seuls ces chemins sont
    ajoutés à l'index de contrôle ; le reste du workspace (sorties écrites par le gate : ``node_modules``, bases du
    smoke…) n'entre JAMAIS dans l'arbre testé et est purgé avant le nouveau passage des contrôles.
    """
    repo = _repo(workspace_path, git_bin=git_bin)
    try:
        if only_paths is None:
            repo.must("add", "-A", what="git add -A (contenu testé)")
        else:
            repo.must("add", "-A", "--", *only_paths, what="git add des chemins re-scellés")
        removed: Tuple[str, ...] = ()
        purged = 0
        if purge_ignored:
            listing = repo.run("clean", "-fdxn")
            names = tuple(
                line[len("Would remove ") :].strip()
                for line in listing.stdout.splitlines()
                if line.startswith("Would remove ")
            )
            names = tuple(name for name in names if name.rstrip("/") != ".git")
            if names:
                repo.must("clean", "-fdxq", what="purge des résidus non livrables")
            purged = len(names)
            removed = names[:MAX_PURGED_LISTED]
        tree = repo.must("write-tree", what="write-tree du contenu testé").strip()
        base = repo.head()
        base_tree = repo.must("rev-parse", f"{base}^{{tree}}", what="arbre de la base").strip()
        rows = _ls_tree(repo, tree)
        base_rows = {path: (mode, sha) for path, mode, sha in _ls_tree(repo, base_tree)}
    except WorkspaceError as exc:
        raise DeliveryProofError(f"contenu testé impossible à figer: {exc}") from exc
    for sha in (tree, base, base_tree):
        if not _SHA1_RE.fullmatch(sha):
            raise DeliveryProofError(f"identifiant Git inattendu lors du scellement: {sha!r}")
    return TestedContent(
        base_sha=base,
        base_tree_sha=base_tree,
        tree_sha=tree,
        content_sha256=manifest_sha256(rows),
        files_count=len(rows),
        ignored_inputs_removed=removed,
        purged_count=purged,
        special_modes=tuple(
            f"{path} ({mode})"
            for path, mode, sha in sorted(rows)
            if mode != "100644" and (path not in base_rows or base_rows[path][0] != mode)
        )[:MAX_PURGED_LISTED],
    )


def verify_tested_content(
    workspace_path: str, content: TestedContent, *, allowed_paths: Sequence[str] = (), git_bin: str = "git"
) -> None:
    """Lève :class:`DeliveryDriftError` si le contenu VIVANT diffère du contenu testé (arbre COMPLET).

    ``update-index --really-refresh`` re-hache les fichiers dont le ``stat`` a changé, puis ``diff-files`` liste tout
    chemin SUIVI (fichiers de base compris, pas seulement le diff) dont le contenu, le mode ou le type diffère de
    l'index scellé. Les sorties nouvelles du gate (caches, couverture) sont hors arbre : sans effet. L'index de
    contrôle ne peut être modifié que par l'hôte ; on vérifie néanmoins qu'il représente toujours l'arbre scellé.
    """
    repo = _repo(workspace_path, git_bin=git_bin)
    try:
        index_tree = repo.must("write-tree", what="relecture de l'index scellé").strip()
        if index_tree != content.tree_sha:
            raise DeliveryDriftError("index de contrôle modifié depuis le scellement du contenu testé")
        repo.run("update-index", "-q", "--really-refresh")
        diff = repo.must(
            "diff-files", "--raw", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", what="diff-files"
        )
    except WorkspaceError as exc:
        raise DeliveryDriftError(f"contenu testé invérifiable: {exc}") from exc
    allowed = frozenset(allowed_paths)
    paths = [path for path in _raw_paths(diff) if path not in allowed]
    if paths:
        raise DeliveryDriftError(
            "contenu modifié depuis le scellement (après le début des contrôles) : " + ", ".join(paths[:20])
        )


def _raw_paths(data: str) -> List[str]:
    tokens = data.split("\0")
    paths: List[str] = []
    index = 0
    while index < len(tokens):
        meta = tokens[index]
        if not meta.startswith(":"):
            index += 1
            continue
        count = 2 if meta.split(" ")[-1][:1] in ("R", "C") else 1
        paths.extend(token for token in tokens[index + 1 : index + 1 + count] if token)
        index += 1 + count
    return paths


# ── construction / encodage de la preuve ────────────────────────────────────────────────────────────


def _verdict_record(item: Verdict) -> dict:
    return {"name": item.name, "required": bool(item.required), "passed": bool(item.passed), "reason": item.reason}


def _run_record(run: Optional[OracleRun]) -> Optional[dict]:
    if run is None:
        return None
    return {
        "phase": run.phase,
        "status": run.status,
        "reason": run.reason,
        "executed": run.executed,
        "passed": run.passed,
        "failed": run.failed,
        "assertion_failures": run.assertion_failures,
        "skipped": run.skipped,
        "xfailed": run.xfailed,
        "xpassed": run.xpassed,
        "collection_errors": run.collection_errors,
        "errors": run.errors,
        "tests": list(run.tests),
    }


def _oracle_record(item: OracleEvidence) -> dict:
    return {
        "task_id": item.task_id,
        "role": item.role,
        "source_sha256": item.source_sha256,
        "contract_sha256": item.contract_sha256,
        "provenance_sha256": item.provenance_sha256,
        "expected_preimage": item.expected_preimage,
        "preimage": _run_record(item.preimage),
        "candidate": _run_record(item.candidate),
        "passed": bool(item.passed),
        "reason": item.reason,
    }


def _canonical(record: Mapping[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _record_without_id(proof: DeliveryProof) -> dict:
    return {
        "schema": proof.schema,
        "owner": proof.owner,
        "repo": proof.repo,
        "project_id": proof.project_id,
        "pr_number": proof.pr_number,
        "head_sha": proof.head_sha,
        "base_sha": proof.base_sha,
        "base_tree_sha": proof.base_tree_sha,
        "tree_sha": proof.tree_sha,
        "phase": proof.phase,
        "passed": bool(proof.passed),
        "verdicts": [_verdict_record(v) for v in proof.verdicts],
        "oracles": [_oracle_record(o) for o in proof.oracles],
        "contracts_required": bool(proof.contracts_required),
        "content_sha256": proof.content_sha256,
        "delivered_paths": list(proof.delivered_paths),
        "ignored_inputs_removed": list(proof.ignored_inputs_removed),
        "created_at": proof.created_at,
    }


def compute_proof_id(proof: DeliveryProof) -> str:
    """SHA-256 du contenu canonique HORS horodatage : deux exécutions équivalentes (reprise, course entre deux
    workers) produisent le MÊME identifiant et ne créent donc jamais deux « vérités » pour une même tête."""
    record = _record_without_id(proof)
    record.pop("created_at", None)
    return hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()


def seal_proof(
    draft: ProofDraft,
    *,
    owner: str,
    repo: str,
    project_id: int,
    pr_number: int,
    head_sha: str,
    base_sha: str,
    now: Optional[datetime] = None,
) -> DeliveryProof:
    """Lie le brouillon à la tête distante VÉRIFIÉE et calcule ``proof_id``."""
    if draft.content is None:
        raise DeliveryProofError("contenu testé absent : aucune preuve possible")
    for label, value in (("head_sha", head_sha), ("base_sha", base_sha)):
        if not _SHA1_RE.fullmatch(str(value or "")):
            raise DeliveryProofError(f"{label} invalide: {value!r}")
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    content = draft.content
    unsealed = DeliveryProof(
        schema=PROOF_SCHEMA,
        proof_id="",
        owner=str(owner),
        repo=str(repo),
        project_id=int(project_id),
        pr_number=int(pr_number),
        head_sha=head_sha,
        base_sha=base_sha,
        base_tree_sha=content.base_tree_sha,
        tree_sha=content.tree_sha,
        phase=draft.phase,
        passed=draft.passed,
        verdicts=tuple(draft.verdicts),
        oracles=tuple(draft.oracles),
        contracts_required=draft.contracts_required,
        content_sha256=content.content_sha256,
        delivered_paths=tuple(draft.delivered_paths),
        ignored_inputs_removed=tuple(content.ignored_inputs_removed),
        created_at=stamp,
    )
    from dataclasses import replace

    return replace(unsealed, proof_id=compute_proof_id(unsealed))


# ── persistance (journal de décisions existant, hors workspace) ────────────────────────────────────────


def _decision_summary(proof: DeliveryProof) -> str:
    return f"{DECISION_PREFIX}{proof.owner}/{proof.repo}#{proof.pr_number}@{proof.head_sha}:{proof.proof_id}"


def persist_delivery_proof(manager: Any, proof: DeliveryProof) -> int:
    """Enregistre la preuve (immuable : jamais écrasée) dans le journal de décisions du projet."""
    if manager is None or not hasattr(manager, "record_decision"):
        raise DeliveryProofError("aucun manager d'état : la preuve ne peut pas être persistée")
    record = _record_without_id(proof)
    record["proof_id"] = proof.proof_id
    return int(manager.record_decision(proof.project_id, _decision_summary(proof), rationale=_canonical(record)))


def _material(proof: DeliveryProof) -> dict:
    """Contenu d'une preuve HORS identifiant et horodatage : deux exécutions identiques en ont le même."""
    record = _record_without_id(proof)
    record.pop("created_at", None)
    return record


def persist_or_reuse_delivery_proof(manager: Any, proof: DeliveryProof) -> DeliveryProof:
    """Persiste ``proof`` ou réutilise la preuve déjà enregistrée pour la MÊME tête si elle est équivalente.

    Une reprise qui reconstitue la même livraison (même tête, mêmes verdicts) ne crée pas de seconde preuve : la
    première, immuable, fait foi (un horodatage différent n'est pas une différence de contenu). Une preuve déjà
    enregistrée pour cette tête avec un CONTENU différent (verdict, arbre, oracles…) lève
    :class:`DeliveryProofError` : on n'écrase jamais, on ne laisse jamais deux vérités.
    """
    existing: Optional[DeliveryProof] = None
    try:
        existing = load_delivery_proof(
            manager,
            proof.project_id,
            owner=proof.owner,
            repo=proof.repo,
            pr_number=proof.pr_number,
            head_sha=proof.head_sha,
        )
    except DeliveryProofError as exc:
        if "aucune preuve de livraison" not in str(exc):
            raise
    if existing is not None:
        if _material(existing) != _material(proof):
            raise DeliveryProofError(
                "une preuve différente est déjà enregistrée pour cette tête : revalidation requise, jamais d'écrasement"
            )
        return existing
    persist_delivery_proof(manager, proof)
    return proof


def _bad(message: str) -> DeliveryProofError:
    return DeliveryProofError(f"preuve de livraison refusée : {message}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _run_from(record: Any) -> Optional[OracleRun]:
    if record is None:
        return None
    if not isinstance(record, dict):
        raise _bad("exécution d'oracle illisible")
    try:
        return OracleRun(
            phase=str(record["phase"]),
            status=str(record["status"]),
            reason=str(record.get("reason", "")),
            executed=int(record["executed"]),
            passed=int(record["passed"]),
            failed=int(record["failed"]),
            assertion_failures=int(record["assertion_failures"]),
            skipped=int(record["skipped"]),
            xfailed=int(record["xfailed"]),
            xpassed=int(record["xpassed"]),
            collection_errors=int(record["collection_errors"]),
            errors=int(record["errors"]),
            tests=tuple(str(t) for t in record.get("tests", ())),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _bad(f"exécution d'oracle incomplète ({exc})") from exc


def _proof_from_record(record: Any) -> DeliveryProof:
    if not isinstance(record, dict):
        raise _bad("enregistrement illisible")
    try:
        verdicts = tuple(
            Verdict(str(v["name"]), bool(v["required"]), bool(v["passed"]), str(v.get("reason", "")))
            for v in record["verdicts"]
        )
        oracles = tuple(
            OracleEvidence(
                task_id=int(o["task_id"]),
                role=str(o["role"]),
                source_sha256=str(o["source_sha256"]),
                contract_sha256=str(o["contract_sha256"]),
                provenance_sha256=str(o["provenance_sha256"]),
                expected_preimage=str(o["expected_preimage"]),
                preimage=_run_from(o.get("preimage")),
                candidate=_run_from(o.get("candidate")),
                passed=bool(o["passed"]),
                reason=str(o.get("reason", "")),
            )
            for o in record["oracles"]
        )
        proof = DeliveryProof(
            schema=str(record["schema"]),
            proof_id=str(record["proof_id"]),
            owner=str(record["owner"]),
            repo=str(record["repo"]),
            project_id=record["project_id"],
            pr_number=record["pr_number"],
            head_sha=str(record["head_sha"]),
            base_sha=str(record["base_sha"]),
            base_tree_sha=str(record["base_tree_sha"]),
            tree_sha=str(record["tree_sha"]),
            phase=str(record["phase"]),
            passed=record["passed"],
            verdicts=verdicts,
            oracles=oracles,
            contracts_required=bool(record["contracts_required"]),
            content_sha256=str(record["content_sha256"]),
            delivered_paths=tuple(str(p) for p in record.get("delivered_paths", ())),
            ignored_inputs_removed=tuple(str(p) for p in record.get("ignored_inputs_removed", ())),
            created_at=str(record.get("created_at", "")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _bad(f"champ manquant ou invalide ({exc})") from exc
    return proof


def _validate(proof: DeliveryProof) -> None:
    if proof.schema != PROOF_SCHEMA:
        raise _bad(f"schéma inconnu {proof.schema!r}")
    if not _is_int(proof.project_id) or not _is_int(proof.pr_number) or proof.pr_number <= 0:
        raise _bad("project_id/pr_number invalides")
    if not isinstance(proof.passed, bool):
        raise _bad("passed n'est pas un booléen")
    if proof.phase not in PHASES:
        raise _bad(f"phase inconnue {proof.phase!r}")
    for label in ("head_sha", "base_sha", "base_tree_sha", "tree_sha"):
        if not _SHA1_RE.fullmatch(getattr(proof, label)):
            raise _bad(f"{label} invalide")
    if not _SHA256_RE.fullmatch(proof.proof_id) or not _SHA256_RE.fullmatch(proof.content_sha256):
        raise _bad("empreintes invalides")
    if compute_proof_id(proof) != proof.proof_id:
        raise _bad("proof_id incohérent avec le contenu (preuve altérée)")
    names = {v.name for v in proof.verdicts}
    if len(names) != len(proof.verdicts):
        raise _bad("verdicts dupliqués")
    if proof.passed != derive_passed(proof.verdicts, proof.oracles, proof.phase, proof.contracts_required):
        raise _bad("passed incohérent avec les verdicts/oracles (obligation requise manquante ou contredite)")
    for oracle in proof.oracles:
        for label in ("source_sha256", "contract_sha256", "provenance_sha256"):
            if not _SHA256_RE.fullmatch(getattr(oracle, label)):
                raise _bad(f"empreinte d'oracle invalide ({label})")


def load_delivery_proof(
    manager: Any, project_id: int, *, owner: str, repo: str, pr_number: int, head_sha: str
) -> DeliveryProof:
    """Relit la preuve persistée de ``(projet, dépôt, PR, tête)`` ou lève :class:`DeliveryProofError`.

    Source d'autorité : l'état durable, JAMAIS le corps de la PR ni un objet fourni par l'appelant. Refus si :
    aucune preuve, plusieurs preuves DIFFÉRENTES pour la même tête, identités (projet, dépôt, PR, tête) différentes,
    ``proof_id`` qui ne recalcule pas, ``passed`` incohérent, obligation requise manquante, schéma inconnu.
    """
    if manager is None or not hasattr(manager, "get_decision_journal"):
        raise DeliveryProofError("aucun manager d'état : preuve de livraison introuvable")
    if not _is_int(project_id) or not _is_int(pr_number):
        raise _bad("project_id et pr_number doivent être des entiers")
    if not _SHA1_RE.fullmatch(str(head_sha or "")):
        raise _bad("head_sha demandé invalide")
    needle = f"{owner}/{repo}#{pr_number}@{head_sha}:"
    try:
        entries = manager.get_decision_journal(project_id, f"{DECISION_PREFIX}{needle}")
    except Exception as exc:  # noqa: BLE001 - état illisible = refus
        raise DeliveryProofError(f"journal de décisions illisible: {exc}") from exc
    found: List[DeliveryProof] = []
    for entry in entries:
        summary = str(getattr(entry, "summary", "") or "")
        if not summary.startswith(DECISION_PREFIX + needle):
            continue
        try:
            record = json.loads(getattr(entry, "rationale", None) or "")
        except ValueError as exc:
            raise _bad(f"enregistrement JSON illisible ({exc})") from exc
        proof = _proof_from_record(record)
        _validate(proof)
        if summary != _decision_summary(proof):
            raise _bad("entrée du journal incohérente avec la preuve qu'elle porte")
        if (proof.project_id, proof.owner, proof.repo, proof.pr_number, proof.head_sha) != (
            int(project_id),
            owner,
            repo,
            int(pr_number),
            head_sha,
        ):
            raise _bad("identités de la preuve différentes de celles demandées")
        found.append(proof)
    if not found:
        raise DeliveryProofError(
            f"aucune preuve de livraison pour {owner}/{repo}#{pr_number}@{head_sha} (projet {project_id}) : "
            "revalidation requise — jamais reconstruite depuis le texte de la PR"
        )
    if len({p.proof_id for p in found}) != 1:
        raise _bad("plusieurs preuves distinctes pour la même tête (conflit d'immuabilité)")
    return found[-1]


# ── vérification de la publication distante ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RemoteBase:
    """Base distante AVANT publication : tip de la branche de base + égalité d'arbre avec la base testée."""

    sha: str
    tree_sha: str


def verify_remote_base(branches: Any, owner: str, repo: str, base: str, content: TestedContent) -> RemoteBase:
    """La base distante a le MÊME arbre que la base sur laquelle les contrôles ont tourné, sinon refus.

    Le SHA local d'une base « compoundée » (IMPROVE) n'existe pas sur GitHub : l'égalité porte sur l'ARBRE.
    """
    try:
        tip = str(branches.get_branch_sha(owner, repo, base))
        commit = branches.get_git_commit(owner, repo, tip)
    except Exception as exc:  # noqa: BLE001 - base distante illisible = refus
        raise DeliveryRemoteError(f"base distante '{base}' illisible: {exc}") from exc
    if not _SHA1_RE.fullmatch(tip) or commit.tree_sha != content.base_tree_sha:
        raise DeliveryDriftError(
            f"la base distante '{base}' ({tip[:12]}) n'a pas l'arbre de la base testée ({content.base_tree_sha[:12]}) : "
            "base déplacée ou dépôt local périmé — livraison refusée"
        )
    return RemoteBase(sha=tip, tree_sha=commit.tree_sha)


def verify_remote_head(
    branches: Any,
    owner: str,
    repo: str,
    *,
    head_sha: str,
    content: TestedContent,
    remote_base_sha: str,
) -> int:
    """L'objet commit publié a EXACTEMENT l'arbre testé et descend de la base distante ; renvoie la profondeur.

    Remonte la chaîne linéaire (``parents[0]``) depuis ``head_sha`` jusqu'à ``remote_base_sha``. Un commit de fusion,
    une chaîne qui ne rejoint pas la base, ou un arbre différent (fichier omis, supprimé en trop, mode perdu, binaire
    ou lien non représenté) ⇒ :class:`DeliveryDriftError`.
    """
    try:
        head = branches.get_git_commit(owner, repo, head_sha)
    except Exception as exc:  # noqa: BLE001
        raise DeliveryRemoteError(f"objet commit distant {head_sha[:12]} illisible: {exc}") from exc
    if head.tree_sha != content.tree_sha:
        raise DeliveryDriftError(
            f"l'arbre publié ({head.tree_sha[:12]}) diffère de l'arbre testé ({content.tree_sha[:12]}) : "
            "le contenu livré n'est pas celui qui a été validé"
        )
    current = head
    depth = 0
    while current.sha != remote_base_sha:
        if len(current.parents) != 1:
            raise DeliveryDriftError("la chaîne de commits publiée n'est pas linéaire (fusion ou racine rencontrée)")
        depth += 1
        if depth > MAX_PARENT_WALK:
            raise DeliveryDriftError("chaîne de commits publiée trop longue pour être vérifiée")
        try:
            current = branches.get_git_commit(owner, repo, current.parents[0])
        except Exception as exc:  # noqa: BLE001
            raise DeliveryRemoteError(f"commit parent illisible: {exc}") from exc
    return depth
