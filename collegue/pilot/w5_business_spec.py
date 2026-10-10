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
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from collegue.pilot import w5_business_ownership as ownership
from collegue.pilot import w5_business_policy as fixture_policy
from collegue.tools.base import ToolExecutionError

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
    identity = ownership.identity_of(
        owner, repo, base, project_id=project_id, plan_hash=getattr(snapshot, "plan_hash", None)
    )

    def note(event: str, **fields: Any) -> None:
        ownership.append_event(manifest_path, identity, event, **fields)

    try:
        events = ownership.read_events(manifest_path, repo=f"{owner}/{repo}", base=base, project_id=project_id)
    except ownership.OwnershipError as exc:
        raise SpecMaterializationError(f"registre d'appartenance inutilisable : {exc}") from exc
    merged_before = [e for e in events if e["event"] == "spec_merged" and e.get("spec_sha256") == digest]

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
        tip_now = str(branches.get_branch_sha(owner, repo, base)).lower()
        if merged_before and tip_now != str(merged_before[-1].get("merge_sha", "")).lower():
            # Reprise « idempotente » : une SPEC identique ne masque pas un état distant incohérent avec ce que la campagne a fusionné.
            raise SpecMaterializationError(
                f"la SPEC est identique mais la base '{base}' ({tip_now[:12]}) n'est plus le commit de fusion consigné "
                f"({str(merged_before[-1].get('merge_sha'))[:12]}) : état distant incohérent, aucune poursuite"
            )
        recorded_prs = [e for e in events if e["event"] == "spec_pr" and e.get("spec_sha256") == digest]
        if recorded_prs and not merged_before:
            # Arrêt entre la fusion et sa ligne au registre : relire la PR de CETTE campagne et exiger que la base soit SON commit de fusion.
            number_seen = int(recorded_prs[-1]["pr_number"])
            reread = prs.get_pr(owner, repo, number_seen)
            if getattr(reread, "merged", False):
                merge_seen = str(getattr(reread, "merge_commit_sha", "") or "").lower()
                if merge_seen != tip_now:
                    raise SpecMaterializationError(
                        f"la SPEC est identique mais la base '{base}' ({tip_now[:12]}) n'est pas le commit de fusion de la PR "
                        f"#{number_seen} ({merge_seen[:12] or '?'}) : état distant incohérent, aucune poursuite ni seconde fusion"
                    )
                note("spec_merged", pr_number=number_seen, merge_sha=merge_seen, spec_sha256=digest)
        outcome = SpecOutcome("already_identical", digest, base_before=tip_now, base_after=tip_now)
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

    # 3. branche de tête. Une intention n'est écrite QUE si l'ABSENCE est établie (404 confirmé) ; une branche déjà présente et non
    #    consignée par cette campagne est étrangère : refusée, signalée, jamais reprise ni supprimée.
    try:
        tip: Optional[str] = str(branches.get_branch_sha(owner, repo, head)).lower()
    except Exception as exc:  # noqa: BLE001
        if _status(exc) != 404:
            raise SpecMaterializationError(f"sommet de '{head}' illisible ({exc}) : aucune écriture") from exc
        tip = None
    intents = [
        e for e in events if e["event"] == "spec_branch_intent" and e.get("head") == head and e.get("absent_verified") is True
        and e.get("spec_sha256") == digest
    ]  # fmt: skip
    if tip is None:
        checkpoint("avant création de la branche")
        note("spec_branch_intent", head=head, base_tip=base_tip, absent_verified=True, spec_sha256=digest,
             spec_file=path, spec_blob=blob)  # fmt: skip
        try:
            created = branches.create_branch(owner, repo, head, from_branch=base)
        except ToolExecutionError as exc:  # la branche est-elle apparue entre l'absence et la création ?
            try:
                appeared: Optional[str] = str(branches.get_branch_sha(owner, repo, head)).lower()
            except Exception as reread:  # noqa: BLE001
                if _status(reread) != 404:
                    raise SpecMaterializationError(
                        f"création de '{head}' non confirmée ({exc}) et relecture impossible : aucune écriture"
                    ) from exc
                appeared = None
            if appeared is None:
                raise SpecMaterializationError(
                    f"création de la branche '{head}' refusée ({exc}) : aucune écriture"
                ) from exc
            note("foreign_branch_seen", head=head, tip=appeared)
            raise SpecMaterializationError(
                f"la branche '{head}' ({appeared[:12]}) est apparue pendant la création : non possédée ; ni écrite ni supprimée"
            ) from exc
        landed = str(getattr(created, "commit_sha", "") or "").lower()
        if landed != base_tip:
            note("foreign_branch_seen", head=head, tip=landed)
            raise SpecMaterializationError(
                f"la branche '{head}' n'est pas sur la base lue ({landed[:12] or '?'} ≠ {base_tip[:12]}) : apparue pendant "
                "la création, non possédée ; ni écrite ni supprimée"
            )
        note("spec_branch_created", head=head, sha=landed)
        files.update_file(owner, repo, path, f"docs: SPEC approuvée (campagne {digest[:12]})", spec, branch=head)
    else:
        creations = [
            e for e in events if e["event"] == "spec_branch_created" and e.get("head") == head
            and str(e.get("sha", "")).lower() == base_tip
        ]  # fmt: skip
        if not intents or str(intents[-1].get("base_tip", "")).lower() != base_tip or not creations:
            note("foreign_branch_seen", head=head, tip=tip)
            raise SpecMaterializationError(
                f"la branche '{head}' existe déjà ({tip[:12]}) sans que cette campagne l'ait créée (aucune création consignée sur "
                "cette base : une intention seule ne prouve rien) : conservée, ni reprise, ni réécrite, ni supprimée"
            )
        # Création interrompue de CETTE campagne, sous preuves exactes : base lue = base de l'intention, sommet = base ou base + SPEC.
        if tip == base_tip:
            files.update_file(owner, repo, path, f"docs: SPEC approuvée (campagne {digest[:12]})", spec, branch=head)
        elif head_state(tip) != "spec":
            raise SpecMaterializationError(
                f"la branche '{head}' ({tip[:12]}) consignée par cette campagne n'est plus ni la base ni base + SPEC approuvée : "
                "conservée, ni reprise ni réécrite"
            )
    head_sha = str(branches.get_branch_sha(owner, repo, head)).lower()
    if head_state(head_sha) != "spec":
        raise SpecMaterializationError(f"la tête '{head}' ({head_sha[:12]}) n'est pas exactement base + SPEC approuvée")
    try:
        head_tree = branches.get_git_commit(owner, repo, head_sha).tree_sha
        fixture_policy.assert_remote_head_clean(branches, owner, repo, head_tree_sha=head_tree, anchor=anchor.rows)
    except fixture_policy.PolicyRefusal as refused:
        raise SpecMaterializationError(f"contrôles de la tête altérés : {refused.reason}") from refused
    note("spec_written", head=head, head_sha=head_sha)

    # 4. PR documentaire (reprise d'une PR ouverte de même tête).
    existing = prs.find_pr_by_head(owner, repo, head, base=base)
    marker = SPEC_MARKER.format(digest=digest)
    if existing is not None:
        number = int(existing.number)
        live = prs.get_pr(owner, repo, number)
        if str(getattr(live, "head_sha", "")).lower() != head_sha or marker not in str(getattr(live, "body", "") or ""):
            raise SpecMaterializationError(
                f"la PR #{number} de la branche '{head}' n'est ni sur la tête vérifiée ni marquée pour cette SPEC : ni adoptée ni fermée"
            )
    else:
        checkpoint("avant création de la PR")
        created_pr = prs.create_pr(
            owner, repo, "docs: SPEC approuvée de la campagne", head, base,
            f"PR documentaire de la campagne W5 : matérialise la SPEC approuvée ({path}, sha256 {digest}).\n\n"
            f"Ce n'est pas une tâche BUILD : aucun code, aucune preuve de livraison.\n\n{marker}",
        )  # fmt: skip
        number = int(created_pr.number)
    note("spec_pr", pr_number=number, head=head, head_sha=head_sha, spec_sha256=digest)

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
    note("spec_merged", pr_number=number, merge_sha=merge_sha, spec_sha256=digest)
    after = files.get_file_content(owner, repo, path, branch=base)
    if after.get("content") != spec:
        raise SpecMaterializationError(f"{path} relu sur {base} après fusion n'est pas identique à la SPEC approuvée")
    tip_after = str(branches.get_branch_sha(owner, repo, base)).lower()
    if tip_after != merge_sha:
        # Le blob SPEC identique ne suffit pas : la suite exige le sommet post-fusion EXACT vérifié (aucune seconde fusion pour « réparer »).
        raise SpecMaterializationError(
            f"la base '{base}' ({tip_after[:12]}) n'est pas le commit de fusion vérifié ({merge_sha[:12]}) : écriture extérieure "
            "juste après la fusion, aucune poursuite"
        )
    return SpecOutcome("merged", digest, number, head, head_sha, merge_sha, base_tip, tip_after, clock.now() - started)


# ── nettoyage : UNIQUEMENT ce qu'un état durable de CETTE campagne désigne, recoupé avec GitHub ─────────────────────────────────


def _owned_view(
    manager: Any, project_id: Optional[int], owner: str, repo: str, base: Optional[str], manifest_path: str
):
    try:
        events = ownership.read_events(manifest_path, repo=f"{owner}/{repo}", base=base, project_id=project_id)
        owned = ownership.owned_pull_requests(manager, project_id, owner, repo, events)
    except ownership.OwnershipError as exc:
        raise SpecMaterializationError(f"appartenance des ressources non établie : {exc}") from exc
    return events, owned


def cleanup_spec_resources(clients: Any, owner: str, repo: str, manifest_path: str) -> Dict[str, Any]:
    """Branche et PR de la SPEC que CETTE campagne a créées : branche dont l'ABSENCE a été établie avant sa création (intention consignée
    seulement alors), dont la CRÉATION a été consignée (une intention seule ne prouve rien) et dont le sommet est exactement l'une
    des étapes attendues (sommet créé, base + SPEC consignée, ou base + blob SPEC exact d'une écriture interrompue). Une branche déjà présente, empruntée ou ambiguë est CONSERVÉE et signalée
    (``SpecMaterializationError`` en fin de passe : nettoyage incomplet) ; une PR de cette branche n'est fermée que si sa tête et son
    marqueur sont ceux de la SPEC. Jamais la base, jamais une PR fusionnée."""
    try:
        events = ownership.read_events(manifest_path, repo=f"{owner}/{repo}")
    except ownership.OwnershipError as exc:
        raise SpecMaterializationError(f"registre d'appartenance inutilisable : {exc}") from exc
    intents: Dict[tuple, Dict[str, Any]] = {}
    for entry in events:
        if entry["event"] == "spec_branch_intent" and entry.get("absent_verified") is True:
            intents[(entry["base"], entry["head"])] = entry
    created_heads = {e["head"] for e in events if e["event"] == "spec_branch_created"}
    seen_foreign = {e["head"] for e in events if e["event"] == "foreign_branch_seen"}
    # Une branche constatée ÉTRANGÈRE (apparue après notre intention, refusée par le matérialiseur) n'est jamais possédée.
    intents = {key: value for key, value in intents.items() if key[1] not in seen_foreign}
    foreign = sorted(seen_foreign)
    out: Dict[str, Any] = {"branches_etrangeres_conservees": foreign}
    if not intents:
        out["spec"] = "aucune ressource possédée"
        return out
    kept: List[str] = []
    for (base, head), intent in sorted(intents.items()):
        digest = str(intent.get("spec_sha256"))
        expected = {
            str(e.get("sha", "")).lower()
            for e in events
            if e["event"] == "spec_branch_created" and e.get("head") == head
        }
        expected |= {
            str(e.get("head_sha", "")).lower()
            for e in events
            if e["event"] in {"spec_written", "spec_pr"} and e.get("head") == head
        }
        try:
            tip = str(clients.branches.get_branch_sha(owner, repo, head)).lower()
        except Exception as exc:  # noqa: BLE001
            if _status(exc) == 404:
                out.setdefault("deja_absentes", []).append(head)
                continue
            raise
        # Une INTENTION seule ne prouve aucune création : sans création consignée, la branche présente est ambiguë ⇒ conservée.
        proven = head in created_heads and tip in expected
        if (
            head in created_heads and not proven
        ):  # écriture faite, jamais consignée (arrêt entre l'écriture et la ligne du registre) : preuve par le contenu exact
            commit = clients.branches.get_git_commit(owner, repo, tip)
            base_commit = clients.branches.get_git_commit(owner, repo, str(intent["base_tip"]))
            wanted = _rows(clients.branches, owner, repo, base_commit.tree_sha)
            wanted[str(intent["spec_file"])] = ("100644", str(intent["spec_blob"]))
            proven = (
                list(commit.parents) == [str(intent["base_tip"])]
                and _rows(clients.branches, owner, repo, commit.tree_sha) == wanted
            )
        if not proven:
            kept.append(f"{head}@{tip[:12]}")
            continue
        marker = SPEC_MARKER.format(digest=digest)
        pr = clients.prs.find_pr_by_head(owner, repo, head, base=base, state="all")
        if pr is not None:
            live = clients.prs.get_pr(owner, repo, int(pr.number))
            if (
                marker in str(live.body or "")
                and str(live.head_sha).lower() == tip
                and live.state == "open"
                and not live.merged
            ):
                clients.prs.close_pr(owner, repo, int(pr.number), expected_head_sha=tip, expected_head_branch=head,
                                     expected_base_branch=base, body_marker=marker)  # fmt: skip
                out.setdefault("pr_fermees", []).append(int(pr.number))
        clients.branches.delete_branch(owner, repo, head, expected_sha=tip)
        out.setdefault("branches_supprimees", []).append(head)
    if kept:
        out["conservees"] = kept
        raise SpecMaterializationError(
            "branche(s) de SPEC consignée(s) mais dans un état non reconnu — conservée(s), nettoyage incomplet : "
            + ", ".join(kept)
        )
    return out


RESIDUAL_HEADS = {
    "collegue/improve-": r"<!-- collegue-exec:(-?\d+) -->",
    "collegue/revert-": r"<!-- collegue-auto-revert:[0-9a-f]{40}:[0-9a-f]{40} -->",
}
MAX_BASE_WALK = 300


def close_residual_pull_requests(
    clients: Any, owner: str, repo: str, base: str, owned: Optional[Mapping[int, Mapping[str, Any]]] = None
) -> List[Dict[str, Any]]:
    """PR OUVERTES d'amélioration/revert que le nettoyage nightly refuserait (« PR non corrélée ») : fermées avec leurs gardes
    d'identité puis leur branche supprimée — SEULEMENT si la PR est dans ``owned`` (preuves de livraison de CE projet, registre de la
    campagne) et si sa tête vivante est celle consignée. Un préfixe de branche et un marqueur de corps sont publics : ils ne prouvent rien.
    Sans ``owned`` (état durable non fourni), rien n'est fermé."""
    closed: List[Dict[str, Any]] = []
    for number, info in sorted((owned or {}).items()):
        live = clients.prs.get_pr(owner, repo, int(number))
        head = str(live.head_branch or "")
        pattern = next((rx for prefix, rx in RESIDUAL_HEADS.items() if head.startswith(prefix)), None)
        if pattern is None or live.state != "open" or live.merged or live.base_branch != base:
            continue
        if str(live.head_sha).lower() not in info["heads"]:
            continue  # tête déplacée depuis l'ouverture : plus la ressource consignée
        found = re.search(pattern, str(live.body or ""))
        if found is None:
            continue
        clients.prs.close_pr(owner, repo, int(number), expected_head_sha=str(live.head_sha), expected_head_branch=head,
                             expected_base_branch=base, body_marker=found.group(0))  # fmt: skip
        clients.branches.delete_branch(owner, repo, head, expected_sha=str(live.head_sha))
        closed.append({"pr": int(number), "head": head})
    return closed


def reconcile_merged_heads(
    clients: Any, config: Any, *, manager: Any = None, project_id: Optional[int] = None
) -> Dict[str, Any]:
    """Têtes de PR FUSIONNÉES de CETTE campagne (le dépôt fixture conserve les têtes : ``delete_branch_on_merge=false``).

    Une PR n'est considérée que si elle est possédée (preuve de livraison persistée du projet ou registre de la campagne) et si sa
    tête vivante est celle consignée : ``collegue/issue-<N>`` d'une issue du manifeste ⇒ SHA CONSIGNÉ au manifeste (sans quoi le nettoyage
    nightly refuse « SHA non prouvé ») ; ``collegue/improve-…`` / ``collegue/revert-…`` ⇒ branche supprimée avec garde de sommet. Une PR
    étrangère fusionnée (marqueur et préfixe publics) n'est ni consignée ni supprimée ; sans état durable fourni, rien n'est fait."""
    from collegue.pilot.nightly_e2e import _load_manifest, _write_manifest

    manifest = _load_manifest(config.manifest_path)
    if manifest is None:
        return {"heads": "aucun manifeste"}
    project = project_id or manifest.project_id
    _events, owned = _owned_view(manager, project, config.owner, config.repo, config.base_branch, config.manifest_path)
    recorded: List[str] = []
    deleted: List[str] = []
    for number, info in sorted(owned.items()):
        live = clients.prs.get_pr(config.owner, config.repo, int(number))
        head = str(live.head_branch or "")
        if not live.merged or not live.head_sha or live.base_branch != config.base_branch:
            continue
        if str(live.head_sha).lower() not in info["heads"]:
            continue
        try:
            tip = str(clients.branches.get_branch_sha(config.owner, config.repo, head)).lower()
        except Exception as exc:  # noqa: BLE001
            if _status(exc) == 404:
                continue
            raise
        if tip != str(live.head_sha).lower():
            continue  # branche déplacée depuis la PR : aucune preuve, rien n'est ni consigné ni supprimé
        issue = re.fullmatch(r"collegue/issue-([1-9][0-9]*)", head)
        if issue and int(issue.group(1)) in set(manifest.issue_numbers):
            if (
                f"<!-- collegue-exec:{issue.group(1)} -->" in str(live.body or "")
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


def advance_recorded_base(
    clients: Any,
    config: Any,
    *,
    anchor_rows: Optional[Mapping[str, Any]] = None,
    manager: Any = None,
    project_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Les fusions de la campagne font avancer la base ; le nettoyage nightly ne tolère qu'une base déplacée par le seul commit de SPEC.
    Le sommet courant n'est adopté comme base enregistrée que si CHAQUE commit entre l'ancienne base et lui (chaîne du premier parent,
    fusions comprises) est une fusion EXPLIQUÉE par une attribution durable à la campagne (SPEC, cycles de fusion BUILD, PR possédées
    fusionnées — amélioration, incident, revert) et si ses contrôles sont ceux du socle. Un commit inconnu, étranger ou un trou ⇒ aucune
    adoption : la base et les ancres sont conservées et le nettoyage est rapporté incomplet."""
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
    project = project_id or manifest.project_id
    events, owned = _owned_view(manager, project, config.owner, config.repo, config.base_branch, config.manifest_path)
    try:
        explained = ownership.merge_commits(manager, project, clients, config.owner, config.repo, owned, events)
    except ownership.OwnershipError as exc:
        raise SpecMaterializationError(f"fusions de la campagne non établies : {exc}") from exc
    cursor, walked, origins = tip, 0, []
    while cursor != manifest.base_sha:
        walked += 1
        if walked > MAX_BASE_WALK:
            raise SpecMaterializationError("base trop éloignée de la base enregistrée : aucune adoption")
        if cursor not in explained:
            raise SpecMaterializationError(
                f"le commit {cursor[:12]} de la base '{config.base_branch}' n'est attribué à aucune fusion durable de cette campagne "
                "(contribution étrangère, trou ou ressource inconnue) : base et ancres conservées, nettoyage incomplet "
                f"[fusions attribuées avant lui : {', '.join(reversed(origins)) or 'aucune'} ; connues : {len(explained)}]"
            )
        origins.append(explained[cursor])
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
    return {"base": "avancée", "from": previous, "to": tip, "commits": walked, "attributions": list(reversed(origins))}
