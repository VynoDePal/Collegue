"""Matérialisation de la SPEC approuvée sur une base PROTÉGÉE, par PR (campagne W5).

Les bases ``collegue-business/*`` de la fixture sont protégées par un ruleset (PR obligatoire, check requis, base à jour, aucun bypass) :
le commit direct de ``SPEC.md`` que fait ``plan sync --execute`` (``planner.github_sync._commit_spec``, PUT Contents sur la base) y est
REFUSÉ par GitHub. Sans ce module, la campagne s'arrêterait à ``plan sync``, après avoir dépensé la planification.

Parcours minimal et conforme, SANS contournement ni écriture directe ni assouplissement d'une protection :

1. la SPEC vient du SNAPSHOT APPROUVÉ du projet (jamais d'un fichier, d'un modèle ou d'un argument) ; la cible (dépôt, base, nom de fichier)
   est celle que l'approbation a scellée ;
2. SPEC déjà identique sur la base ⇒ rien à faire (pas de PR en double) ; SPEC divergente ⇒ refus (jamais écrasée) ;
3. contrôles de la base comparés au socle de confiance, check requis exigé par les protections serveur ;
4. une branche de tête hors du motif protégé (``collegue-spec/<tag>``) est créée sur le sommet de la base (ou reprise si elle porte
   EXACTEMENT base + blob SPEC, refusée sinon, jamais réécrite en aveugle) et reçoit UN seul fichier : le blob SPEC approuvé ;
5. la tête est relue sur l'arbre Git distant : base + exactement ce blob, contrôles ``.github/``/``ci/`` du socle ;
6. PR documentaire ouverte (ce n'est PAS une tâche BUILD : aucune preuve de livraison n'est fabriquée) ; checks REQUIS attendus avec
   leur provenance réelle (check-run → job → exécution du workflow approuvé), dans l'échéance globale ; rouge, absent ou provenance
   douteuse ⇒ arrêt explicite ;
7. fusion à la tête et à la base exactes ; réponse de fusion perdue ⇒ relecture de la PR, jamais de seconde fusion ; le commit de fusion
   est relu (parent = base, arbre = base + blob SPEC) ; la SPEC distante est relue identique.

Un refus à n'importe quelle étape est un ARRÊT explicite (``SpecMaterializationError``) AVANT les BUILD. Les ressources créées (branche,
PR) sont consignées dans un fichier d'intention écrit AVANT la création, que le nettoyage de la campagne connaît.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from collegue.pilot import w5_business_policy as fixture_policy

SPEC_HEAD_PREFIX = "collegue-spec/"
SPEC_MARKER = "<!-- collegue-spec:{digest} -->"
DEFAULT_POLL_SECONDS = 10.0


class SpecMaterializationError(RuntimeError):
    """La SPEC approuvée ne peut pas être matérialisée sur la base protégée : arrêt explicite avant tout BUILD."""


class SpecDeadline(SpecMaterializationError):
    """L'échéance globale est atteinte pendant la matérialisation (aucune poursuite)."""


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data, usedforsecurity=False).hexdigest()


def head_branch_for(base: str) -> str:
    prefix = fixture_policy.CAMPAIGN_BASE_PREFIX
    tag = base[len(prefix) :] if base.startswith(prefix) else base.replace("/", "-")
    return f"{SPEC_HEAD_PREFIX}{tag}"


def record_path(manifest_path: str) -> str:
    return str(manifest_path) + ".spec.json"


def _write_record(path: str, payload: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(json.dumps(dict(payload), sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, destination)


def load_record(manifest_path: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(Path(record_path(manifest_path)).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise SpecMaterializationError(f"fichier d'intention de la SPEC illisible ({type(exc).__name__})") from exc


@dataclass
class Clock:
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Any] = time.sleep


@dataclass
class SpecOutcome:
    state: str  # "already_identical" | "merged"
    spec_sha256: str
    pr_number: Optional[int] = None
    head_branch: Optional[str] = None
    head_sha: Optional[str] = None
    merge_sha: Optional[str] = None
    base_before: Optional[str] = None
    base_after: Optional[str] = None
    waited_seconds: float = 0.0
    notes: List[str] = field(default_factory=list)

    def to_fact(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def _status(exc: BaseException) -> Optional[int]:
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def _rows(branches: Any, owner: str, repo: str, tree_sha: str) -> Dict[str, Any]:
    try:
        return fixture_policy.read_remote_rows(branches, owner, repo, tree_sha)
    except fixture_policy.PolicyRefusal as refused:
        raise SpecMaterializationError(f"arbre distant illisible : {refused.reason}") from refused


def materialize_approved_spec(
    *,
    clients: Any,
    owner: str,
    repo: str,
    manager: Any,
    project_id: int,
    manifest_path: str,
    trust_manifest_path: Optional[str],
    deadline_monotonic: Optional[Callable[[], float]] = None,
    clock: Optional[Clock] = None,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> SpecOutcome:
    """Matérialise la SPEC approuvée sur la base de campagne par une PR documentaire fusionnée sous les protections réelles."""
    from collegue.pilot import merge_policy
    from collegue.planner.plan_review import load_plan_snapshot

    clock = clock or Clock()

    def remaining() -> Optional[float]:
        return None if deadline_monotonic is None else deadline_monotonic() - clock.now()

    def checkpoint(where: str) -> None:
        left = remaining()
        if left is not None and left <= 0:
            raise SpecDeadline(
                f"échéance globale atteinte pendant la matérialisation de la SPEC ({where}) : aucune poursuite"
            )

    snapshot = load_plan_snapshot(manager, project_id, require_approval=True, require_target=True)
    if snapshot is None or not snapshot.spec:
        raise SpecMaterializationError("SPEC approuvée absente du snapshot : rien à matérialiser")
    target = snapshot.plan_sync_config or {}
    if target.get("owner") != owner or target.get("repo") != repo:
        raise SpecMaterializationError("la cible scellée par l'approbation n'est pas le dépôt de campagne")
    base, path, spec = str(target["base_branch"]), str(target["spec_filename"]), str(snapshot.spec)
    if not fixture_policy.applies(owner, repo, base):
        raise SpecMaterializationError(
            f"{owner}/{repo}@{base} n'est pas une base de campagne : matérialisation refusée"
        )
    if fixture_policy.is_protected_path(path) or "/" in path.strip("/") or path.startswith("."):
        raise SpecMaterializationError(f"nom de fichier de SPEC non admis ({path!r}) : racine du dépôt, hors contrôles")
    spec_bytes = spec.encode("utf-8")
    digest = hashlib.sha256(spec_bytes).hexdigest()
    blob = git_blob_sha(spec_bytes)
    branches, files, prs = clients.branches, clients.files, clients.prs
    head = head_branch_for(base)
    record = {"head_branch": head, "base": base, "spec_file": path, "spec_sha256": digest, "pr_number": None, "head_sha": None,
              "merge_sha": None, "state": "intent"}  # fmt: skip

    # 1. SPEC distante : identique ⇒ fin ; divergente ⇒ refus ; 404 établi ⇒ matérialisation ; autre erreur ⇒ arrêt.
    try:
        current = files.get_file_content(owner, repo, path, branch=base)
    except Exception as exc:  # noqa: BLE001
        if _status(exc) != 404:
            raise SpecMaterializationError(
                f"lecture de {path} sur {base} impossible ({exc}) : absence non établie"
            ) from exc
        current = None
    if isinstance(current, dict) and current.get("content") == spec:
        outcome = SpecOutcome("already_identical", digest, base_before=str(branches.get_branch_sha(owner, repo, base)))
        outcome.base_after = outcome.base_before
        return outcome
    if isinstance(current, dict) and current.get("content") is not None:
        raise SpecMaterializationError(f"{path} existe sur {base} avec un contenu divergent : refus de l'écraser")

    # 2. protections serveur, socle de confiance, contrôles de la base.
    try:
        policy = merge_policy.discover_server_policy(clients, owner, repo, base)
        merge_policy._campaign_check_required(policy)
        if not policy.strict_sources:
            raise merge_policy.MergeRefused(
                "règle « base à jour » non établie pour l'acteur", code=merge_policy.CODE_POLICY
            )
        anchor = fixture_policy.load_trust_anchor(branches, owner, repo, trust_manifest_path)
    except merge_policy.MergeRefused as refused:
        raise SpecMaterializationError(f"protections de la base non établies : {refused.reason}") from refused
    except fixture_policy.PolicyRefusal as refused:
        raise SpecMaterializationError(f"socle de confiance non établi : {refused.reason}") from refused
    checkpoint("avant lecture de la base")
    base_tip = str(branches.get_branch_sha(owner, repo, base)).lower()
    base_tree = branches.get_git_commit(owner, repo, base_tip).tree_sha
    base_rows = _rows(branches, owner, repo, base_tree)
    try:
        fixture_policy.assert_remote_head_clean(branches, owner, repo, head_tree_sha=base_tree, anchor=anchor.rows)
    except fixture_policy.PolicyRefusal as refused:
        raise SpecMaterializationError(f"la base diverge du socle de confiance : {refused.reason}") from refused
    expected_rows = dict(base_rows)
    expected_rows[path] = ("100644", blob)

    def head_state(tip: str) -> str:
        commit = branches.get_git_commit(owner, repo, tip)
        if list(commit.parents) != [base_tip]:
            return "other"
        return "spec" if _rows(branches, owner, repo, commit.tree_sha) == expected_rows else "other"

    # 3. branche de tête : écriture-d'abord de l'intention, puis création/reprise, jamais de réécriture aveugle.
    _write_record(record_path(manifest_path), record)
    try:
        tip: Optional[str] = str(branches.get_branch_sha(owner, repo, head)).lower()
    except Exception as exc:  # noqa: BLE001
        if _status(exc) != 404:
            raise SpecMaterializationError(f"sommet de '{head}' illisible ({exc}) : aucune écriture") from exc
        tip = None
    if tip is not None and tip != base_tip:
        if head_state(tip) != "spec":
            raise SpecMaterializationError(
                f"la branche '{head}' existe déjà ({tip[:12]}) sans être la base ni exactement base + SPEC approuvée : "
                "ni réutilisée ni réécrite"
            )
    elif tip is None or tip == base_tip:
        checkpoint("avant création de la branche")
        created = branches.ensure_branch(owner, repo, head, from_branch=base)
        landed = str(getattr(created, "commit_sha", "") or "").lower()
        if landed != base_tip:
            raise SpecMaterializationError(
                f"la branche '{head}' n'est pas sur la base lue ({landed[:12] or '?'} ≠ {base_tip[:12]})"
            )
        files.update_file(owner, repo, path, f"docs: SPEC approuvée (campagne {digest[:12]})", spec, branch=head)
    head_sha = str(branches.get_branch_sha(owner, repo, head)).lower()
    if head_state(head_sha) != "spec":
        raise SpecMaterializationError(f"la tête '{head}' ({head_sha[:12]}) n'est pas exactement base + SPEC approuvée")
    try:
        head_tree = branches.get_git_commit(owner, repo, head_sha).tree_sha
        fixture_policy.assert_remote_head_clean(branches, owner, repo, head_tree_sha=head_tree, anchor=anchor.rows)
    except fixture_policy.PolicyRefusal as refused:
        raise SpecMaterializationError(f"contrôles de la tête altérés : {refused.reason}") from refused
    record.update(head_sha=head_sha, state="head_ready")
    _write_record(record_path(manifest_path), record)

    # 4. PR documentaire (reprise d'une PR ouverte de même tête).
    existing = prs.find_pr_by_head(owner, repo, head, base=base)
    marker = SPEC_MARKER.format(digest=digest)
    if existing is not None:
        number = int(existing.number)
        if str(getattr(existing, "head_sha", head_sha)).lower() != head_sha:
            raise SpecMaterializationError(f"la PR #{number} observe une autre tête que la branche vérifiée")
    else:
        checkpoint("avant création de la PR")
        created_pr = prs.create_pr(
            owner, repo, "docs: SPEC approuvée de la campagne", head, base,
            f"PR documentaire de la campagne W5 : matérialise la SPEC approuvée ({path}, sha256 {digest}).\n\n"
            f"Ce n'est pas une tâche BUILD : aucun code, aucune preuve de livraison.\n\n{marker}",
        )  # fmt: skip
        number = int(created_pr.number)
    record.update(pr_number=number, state="pr_open")
    _write_record(record_path(manifest_path), record)

    # 5. checks requis réels + provenance, dans l'échéance globale.
    started = clock.now()
    while True:
        checkpoint("attente des checks requis")
        details = prs.get_commit_check_details(owner, repo, head_sha)
        try:
            if not bool(getattr(details, "complete", False)):
                raise merge_policy.MergeRefused("liste des checks incomplète", code=merge_policy.CODE_API)
            merge_policy.evaluate_checks(policy.required_checks, list(details.checks))
            merge_policy._campaign_provenance(prs, owner, repo, head_sha, details.checks)
            break
        except merge_policy.MergeRefused as refused:
            if not refused.retryable:
                raise SpecMaterializationError(
                    f"check requis refusé pour la PR #{number} : {refused.reason}"
                ) from refused
        left = remaining()
        pause = poll_seconds if left is None else max(0.0, min(poll_seconds, left))
        clock.sleep(pause)

    # 6. relecture finale puis fusion à la tête ET à la base exactes ; réponse perdue ⇒ relecture, jamais de seconde fusion.
    info = prs.get_pr(owner, repo, number)
    if (
        info.state != "open"
        or getattr(info, "draft", False)
        or info.base_branch != base
        or str(info.head_sha).lower() != head_sha
    ):
        raise SpecMaterializationError(
            f"la PR #{number} a changé pendant l'attente (état {info.state}, tête {str(info.head_sha)[:12]})"
        )
    if str(branches.get_branch_sha(owner, repo, base)).lower() != base_tip:
        raise SpecMaterializationError(
            f"la base '{base}' a bougé pendant l'attente : fusion refusée (base à jour exigée)"
        )
    checkpoint("avant fusion")
    try:
        merged = prs.merge_pr(owner, repo, number, method="squash", expected_head_sha=head_sha,
                              expected_base_branch=base, expected_base_sha=base_tip)  # fmt: skip
        merge_sha = str(merged.sha or "").lower() if getattr(merged, "merged", False) else ""
    except Exception as exc:  # noqa: BLE001 - réponse perdue ? relire la PR avant toute conclusion
        reread = prs.get_pr(owner, repo, number)
        if not getattr(reread, "merged", False):
            raise SpecMaterializationError(f"fusion de la PR #{number} refusée ou non confirmée : {exc}") from exc
        merge_sha = str(getattr(reread, "merge_commit_sha", "") or "").lower()
    if not merge_sha:
        raise SpecMaterializationError(f"la fusion de la PR #{number} n'est pas confirmée (SHA absent)")
    commit = branches.get_git_commit(owner, repo, merge_sha)
    if list(commit.parents) != [base_tip] or _rows(branches, owner, repo, commit.tree_sha) != expected_rows:
        raise SpecMaterializationError(
            f"le commit de fusion {merge_sha[:12]} n'est pas exactement base + SPEC approuvée"
        )
    record.update(merge_sha=merge_sha, state="merged")
    _write_record(record_path(manifest_path), record)
    after = files.get_file_content(owner, repo, path, branch=base)
    if after.get("content") != spec:
        raise SpecMaterializationError(f"{path} relu sur {base} après fusion n'est pas identique à la SPEC approuvée")
    return SpecOutcome("merged", digest, number, head, head_sha, merge_sha, base_tip,
                       str(branches.get_branch_sha(owner, repo, base)).lower(), clock.now() - started)  # fmt: skip


def cleanup_spec_resources(clients: Any, owner: str, repo: str, manifest_path: str) -> Dict[str, Any]:
    """Ressources de la PR documentaire connues du fichier d'intention : PR encore ouverte fermée (gardes d'identité), branche de tête
    supprimée seulement si son sommet est celui consigné. Idempotent ; jamais la base, jamais une PR fusionnée."""
    record = load_record(manifest_path)
    if record is None:
        return {"spec": "aucune ressource consignée"}
    out: Dict[str, Any] = {"head_branch": record.get("head_branch"), "pr_number": record.get("pr_number")}
    head, number, head_sha = record.get("head_branch"), record.get("pr_number"), record.get("head_sha")
    if not head or not str(head).startswith(SPEC_HEAD_PREFIX):
        raise SpecMaterializationError(
            "fichier d'intention de la SPEC incohérent : branche hors motif, aucune suppression"
        )
    marker = SPEC_MARKER.format(digest=record.get("spec_sha256"))
    if number:
        info = clients.prs.get_pr(owner, repo, int(number))
        if info.state == "open" and not getattr(info, "merged", False):
            clients.prs.close_pr(owner, repo, int(number), expected_head_sha=str(info.head_sha),
                                 expected_head_branch=str(head), expected_base_branch=str(record.get("base")),
                                 body_marker=marker)  # fmt: skip
            out["closed_pr"] = int(number)
    try:
        tip = str(clients.branches.get_branch_sha(owner, repo, str(head))).lower()
    except Exception as exc:  # noqa: BLE001
        if _status(exc) == 404:
            out["branch"] = "déjà absente"
            return out
        raise
    if head_sha and tip != str(head_sha).lower():
        raise SpecMaterializationError(
            f"la branche '{head}' a bougé depuis la preuve ({tip[:12]}) : aucune suppression"
        )
    clients.branches.delete_branch(owner, repo, str(head), expected_sha=tip)
    out["branch"] = "supprimée"
    return out


# ── nettoyage de ce que le nettoyage nightly ne connaît pas ───────────────────────────────────────────────────────────────────

RESIDUAL_HEADS = {
    "collegue/improve-": r"<!-- collegue-exec:(-?\d+) -->",
    "collegue/revert-": r"<!-- collegue-auto-revert:[0-9a-f]{40}:[0-9a-f]{40} -->",
}
MAX_BASE_WALK = 300


def close_residual_pull_requests(clients: Any, owner: str, repo: str, base: str) -> List[Dict[str, Any]]:
    """PR OUVERTES de la campagne que le nettoyage nightly refuserait (« PR non corrélée ») : amélioration non fusionnée, revert non
    fusionné. Chacune est fermée avec les gardes d'identité (tête, branche, base, marqueur du corps) puis sa branche supprimée si son
    sommet est celui de la PR. Une PR ouverte étrangère n'est jamais touchée (le nettoyage nightly la signalera)."""
    import re

    closed: List[Dict[str, Any]] = []
    for pr in clients.prs.list_prs(owner, repo, state="open", limit=100, base=base):
        head = str(pr.head_branch or "")
        pattern = next((rx for prefix, rx in RESIDUAL_HEADS.items() if head.startswith(prefix)), None)
        if pattern is None:
            continue
        full = clients.prs.get_pr(owner, repo, int(pr.number))
        found = re.search(pattern, str(full.body or ""))
        if found is None or full.merged or full.state != "open" or not full.head_sha:
            continue
        clients.prs.close_pr(owner, repo, int(pr.number), expected_head_sha=str(full.head_sha), expected_head_branch=head,
                             expected_base_branch=base, body_marker=found.group(0))  # fmt: skip
        clients.branches.delete_branch(owner, repo, head, expected_sha=str(full.head_sha))
        closed.append({"pr": int(pr.number), "head": head})
    return closed


def advance_recorded_base(
    clients: Any, config: Any, *, anchor_rows: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """Les fusions de la campagne (SPEC, BUILD, amélioration, incident, revert) font avancer la base ; le nettoyage nightly ne
    tolère qu'une base déplacée par le seul commit de SPEC. Le sommet courant est adopté comme base enregistrée SEULEMENT s'il
    descend de l'ancienne base par la chaîne du premier parent (aucun commit étranger intercalé hors de cette chaîne) et, si une ancre
    est fournie, si ses contrôles ``.github/``/``ci/`` sont ceux du socle. Sinon : aucune adoption, le nettoyage refusera (ancres conservées)."""
    from collegue.pilot.nightly_e2e import _load_manifest, _write_manifest

    manifest = _load_manifest(config.manifest_path)
    if manifest is None or manifest.base_sha is None:
        return {"base": "aucun manifeste ou base non créée"}
    try:
        tip = str(clients.branches.get_branch_sha(config.owner, config.repo, config.base_branch)).lower()
    except Exception as exc:  # noqa: BLE001
        if _status(exc) == 404:
            return {"base": "déjà absente"}
        raise
    if tip == manifest.base_sha:
        return {"base": "inchangée"}
    cursor, walked = tip, 0
    while cursor != manifest.base_sha:
        walked += 1
        if walked > MAX_BASE_WALK:
            raise SpecMaterializationError("base trop éloignée de la base enregistrée : aucune adoption")
        commit = clients.branches.get_git_commit(config.owner, config.repo, cursor)
        if not commit.parents:
            raise SpecMaterializationError("la base courante ne descend pas de la base enregistrée : aucune adoption")
        cursor = str(commit.parents[0]).lower()
    if anchor_rows is not None:
        tree = clients.branches.get_git_commit(config.owner, config.repo, tip).tree_sha
        try:
            fixture_policy.assert_remote_head_clean(
                clients.branches, config.owner, config.repo, head_tree_sha=tree, anchor=anchor_rows
            )
        except fixture_policy.PolicyRefusal as refused:
            raise SpecMaterializationError(f"contrôles de la base courante altérés : {refused.reason}") from refused
    previous, manifest.base_sha = manifest.base_sha, tip
    _write_manifest(config.manifest_path, manifest)
    return {"base": "avancée", "from": previous, "to": tip, "commits": walked}


def reconcile_merged_heads(clients: Any, config: Any) -> Dict[str, Any]:
    """Têtes de PR FUSIONNÉES de la campagne (le dépôt fixture conserve les têtes : ``delete_branch_on_merge=false``).

    * ``collegue/issue-<N>`` d'une issue du manifeste, PR fusionnée sur la base, marqueur ``collegue-exec:N``, sommet = tête de la PR :
      le SHA est CONSIGNÉ au manifeste (sans quoi le nettoyage nightly refuse ``SHA non prouvé`` et conserve la base) ;
    * ``collegue/improve-…`` et ``collegue/revert-…`` : branches de la campagne dont la PR est fusionnée, supprimées avec garde
      d'identité (sommet = tête de la PR). Toute branche non prouvée est laissée en place."""
    import re

    from collegue.pilot.nightly_e2e import _load_manifest, _write_manifest

    manifest = _load_manifest(config.manifest_path)
    if manifest is None:
        return {"heads": "aucun manifeste"}
    recorded: List[str] = []
    deleted: List[str] = []
    for pr in clients.prs.list_prs(config.owner, config.repo, state="all", limit=100, base=config.base_branch):
        full = clients.prs.get_pr(config.owner, config.repo, int(pr.number))
        head = str(full.head_branch or "")
        if not full.merged or not full.head_sha:
            continue
        try:
            tip = str(clients.branches.get_branch_sha(config.owner, config.repo, head)).lower()
        except Exception as exc:  # noqa: BLE001
            if _status(exc) == 404:
                continue
            raise
        if tip != str(full.head_sha).lower():
            continue  # branche déplacée depuis la PR : aucune preuve, rien n'est ni consigné ni supprimé
        issue = re.fullmatch(r"collegue/issue-([1-9][0-9]*)", head)
        if issue and int(issue.group(1)) in set(manifest.issue_numbers):
            if (
                f"<!-- collegue-exec:{issue.group(1)} -->" in str(full.body or "")
                and manifest.head_shas.get(head) != tip
            ):
                manifest.head_shas[head] = tip
                recorded.append(head)
        elif head.startswith(tuple(RESIDUAL_HEADS)):
            clients.branches.delete_branch(config.owner, config.repo, head, expected_sha=tip)
            deleted.append(head)
    if recorded:
        _write_manifest(config.manifest_path, manifest)
    return {"recorded": recorded, "deleted": deleted}
