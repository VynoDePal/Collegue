"""Ouverture de la Pull Request d'une issue (E4, epic #362).

Transforme un diff **validé** (E3) en Pull Request : crée la branche, committe les
fichiers modifiés, ouvre la PR dont le corps porte **code + tests + rapport de
revue** et ``Closes #N``. ``dry_run=True`` par défaut (aucune écriture) ; l'écriture
réelle est exercée en ``integration``.

Réutilise les commandes GitHub existantes (``BranchCommands`` / ``FileCommands`` /
``PRCommands`` de ``collegue.tools.github_commands``) plutôt que d'en réimplémenter
(non-goal §9). Idempotence : si une PR ouverte existe déjà pour la branche, on la
retourne sans recréer ; un marqueur ``<!-- collegue-exec:<N> -->`` trace l'origine.

Limite (MVP) : les fichiers sont poussés via la Contents API en **texte UTF-8**
(stack cible web Python+JS/TS). Depuis la vague 3, tout format NON représentable (binaire, lien
symbolique, mode exécutable perdu, sous-module) est REFUSÉ avant publication — jamais sauté avec un simple
avertissement : annoncer un candidat complet en omettant un fichier requis est interdit.

**Preuve de livraison (vague 3)** : en mode réel, ``open_pr`` exige le contenu testé (``ProofDraft``). Il vérifie la
base distante (même arbre que la base testée), publie, lit l'objet commit distant de la tête et exige que son ARBRE
soit exactement l'arbre testé (fichier omis, suppression en trop, mode perdu ⇒ refus), puis lie et PERSISTE la preuve
(:mod:`collegue.executor.delivery_proof`) sur ``head_sha``. Une PR préexistante de même nom de branche n'est
réutilisée que si sa tête a ce même arbre.

**Contrôles de la fixture de campagne (W5)** : sur le dépôt fixture et ses bases ``collegue-business/*`` (identification sans drapeau, voir
:mod:`collegue.pilot.w5_business_policy`), la publication est refusée AVANT la première écriture distante si le contenu testé, le
payload à pousser ou la tête d'une PR préexistante touchent ``.github/`` ou ``ci/`` par rapport au socle de confiance. Une tête hostile
publiée, même brièvement, pourrait exécuter son workflow avec un jeton Actions : la barrière avant fusion seule serait trop tardive.
Hors campagne, aucun changement de comportement.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Optional, Tuple

from collegue.executor.agent import IssueSpec
from collegue.executor.delivery_proof import (
    DeliveryDriftError,
    DeliveryProof,
    DeliveryProofError,
    DeliveryRemoteError,
    ProofDraft,
    TestedContent,
    describe_refusal,
    persist_or_reuse_delivery_proof,
    seal_proof,
    verify_remote_base,
    verify_remote_head,
)
from collegue.executor.quality_gate import QualityReport
from collegue.executor.workspace import Workspace
from collegue.textnorm import inline

DEFAULT_BASE_BRANCH = "main"

DELIVERY_UPDATE = "update"
DELIVERY_DELETE = "delete"
DELIVERY_SKIP_BINARY = "skip_binary"
DELIVERY_SKIP_SYMLINK = "skip_symlink"


def exec_marker(issue_number: int) -> str:
    """Marqueur HTML traçant la PR générée par l'exécuteur pour une issue."""
    return f"<!-- collegue-exec:{int(issue_number)} -->"


def diff_sha256_marker(digest: str) -> str:
    """Marqueur HTML liant la PR au diff dont le snapshot a été validé."""
    return f"<!-- collegue-diff-sha256:{digest} -->"


def _safe_rel_path(path: str) -> str:
    """Chemin de dépôt sûr (anti-traversée) : pas d'absolu, pas de segment ``..``/``.``/vide."""
    cleaned = path.strip().lstrip("/")
    segments = cleaned.split("/")
    if not cleaned or "\\" in cleaned or any(seg in ("", ".", "..") for seg in segments):
        raise ValueError(f"chemin de fichier invalide: {path!r}")
    return cleaned


def _resolve_in_workspace(workspace_path: str, rel: str) -> str:
    """Résout ``rel`` dans le workspace en **refusant toute évasion**.

    L'agent est **non fiable** : sans cette garde, un chemin passant par un
    répertoire intermédiaire symlinké vers l'hôte (``dir → ~/.ssh``) serait lu sur
    l'hôte et poussé dans la PR. Le confinement est vérifié via ``realpath``.
    Les **fichiers** symlinks, eux, sont sautés en amont par :func:`open_pr` sans
    jamais être lus (même politique que les binaires, cf. #423) — ils ne passent
    donc pas par cette résolution.
    """
    full = os.path.join(workspace_path, rel)
    root = os.path.realpath(workspace_path)
    real = os.path.realpath(full)
    if real != root and os.path.commonpath([real, root]) != root:
        raise ValueError(f"chemin hors du workspace: {rel!r}")
    return full


@dataclass
class PrClients:
    """Commandes GitHub nécessaires à l'ouverture d'une PR (injectables/mockables)."""

    branches: object  # BranchCommands.ensure_branch(owner, repo, branch, from_branch)
    files: object  # FileCommands.update_file/delete_file(owner, repo, path, message, content, branch)
    prs: object  # PRCommands.find_pr_by_head / create_pr(owner, repo, title, head, base, body)


@dataclass(frozen=True)
class DeliveryFile:
    """État immuable d'un chemin au moment où le livrable est figé.

    ``content`` n'est renseigné que pour ``update``. Les suppressions et les
    formats que la Contents API ne sait pas pousser restent représentés
    explicitement dans le manifeste. ``source_sha256`` permet de détecter aussi
    la dérive d'un binaire ou de la cible d'un symlink sans jamais les livrer.
    """

    path: str
    operation: str
    content: Optional[str] = None
    source_sha256: Optional[str] = None


@dataclass(frozen=True)
class DeliverySnapshot:
    """Manifeste immuable : payloads à pousser + empreinte du diff validé."""

    files: Tuple[DeliveryFile, ...]
    diff_sha256: str

    @property
    def paths(self) -> Tuple[str, ...]:
        return tuple(item.path for item in self.files)

    @property
    def skipped_binaries(self) -> Tuple[str, ...]:
        return tuple(item.path for item in self.files if item.operation == DELIVERY_SKIP_BINARY)

    @property
    def skipped_symlinks(self) -> Tuple[str, ...]:
        return tuple(item.path for item in self.files if item.operation == DELIVERY_SKIP_SYMLINK)


class DeliveryRefusedError(DeliveryProofError):
    """Livraison refusée AVANT publication : format non représentable ou contenu non vérifiable."""


def assert_deliverable(snapshot: "DeliverySnapshot") -> None:
    """Refuse tout chemin que la Contents API ne sait pas pousser fidèlement (binaire, lien symbolique).

    Omettre un tel fichier ferait annoncer un candidat COMPLET alors que la livraison ne le contient pas : refus
    explicite, avec la liste des chemins, plutôt qu'un simple avertissement dans le corps de la PR.
    """
    skipped = snapshot.skipped_binaries + snapshot.skipped_symlinks
    if skipped:
        raise DeliveryRefusedError(
            "LIVRAISON REFUSÉE — format non pris en charge par la publication (binaire ou lien symbolique) : "
            + ", ".join(skipped[:10])
            + ". Remplace-les par des fichiers texte UTF-8 ou retire-les du livrable."
        )


def assert_representable(content: TestedContent) -> None:
    """Refuse un contenu testé dont un mode Git (exécutable, lien, sous-module) ne peut pas être publié.

    La Contents API écrit un fichier NEUF en 100644 : le bit exécutable d'un script neuf serait perdu en silence et
    l'arbre publié ne serait plus l'arbre testé. Refus explicite, avant toute écriture distante.
    """
    if content.special_modes:
        raise DeliveryRefusedError(
            "LIVRAISON REFUSÉE — mode Git non représentable par la publication (fichier exécutable, lien ou sous-module "
            "neuf/modifié) : " + ", ".join(content.special_modes[:10]) + ". Retire le bit exécutable (chmod 644)."
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _capture_delivery_file(workspace_path: str, path: str) -> DeliveryFile:
    """Capture un chemin une seule fois, sans jamais suivre un symlink terminal."""
    rel = _safe_rel_path(path)
    candidate = os.path.join(workspace_path, rel)
    if os.path.islink(candidate):
        target = os.readlink(candidate)
        return DeliveryFile(
            path=rel,
            operation=DELIVERY_SKIP_SYMLINK,
            source_sha256=_sha256(os.fsencode(target)),
        )

    full = _resolve_in_workspace(workspace_path, rel)
    if not os.path.isfile(full):
        return DeliveryFile(path=rel, operation=DELIVERY_DELETE)

    with open(full, "rb") as handle:
        raw = handle.read()
    digest = _sha256(raw)
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return DeliveryFile(path=rel, operation=DELIVERY_SKIP_BINARY, source_sha256=digest)
    return DeliveryFile(path=rel, operation=DELIVERY_UPDATE, content=content, source_sha256=digest)


def capture_delivery_snapshot(
    workspace: Workspace,
    files_changed: Tuple[str, ...],
    *,
    diff: str = "",
) -> DeliverySnapshot:
    """Fige immédiatement tous les payloads qui pourront atteindre GitHub.

    Le SHA-256 porte sur les octets UTF-8 exacts du diff fourni. Une fois cette
    fonction revenue, le manifeste ne dépend plus du workspace vivant : même une
    mutation pendant les appels réseau ne peut modifier le contenu livré.
    """
    files = tuple(_capture_delivery_file(workspace.path, path) for path in files_changed)
    return DeliverySnapshot(files=files, diff_sha256=_sha256((diff or "").encode("utf-8")))


def verify_delivery_snapshot(
    workspace: Workspace,
    snapshot: DeliverySnapshot,
    *,
    ignored_paths: Tuple[str, ...] = (),
) -> None:
    """Lève :class:`DeliveryDriftError` si un chemin figé a changé.

    La vérification est volontairement séparée de :func:`open_pr` : le pipeline
    peut la placer juste après ses gates, tandis que ``open_pr(snapshot=...)`` ne
    relit ensuite jamais le filesystem et pousse exclusivement les payloads figés.
    ``ignored_paths`` autorise une exception nominative et bornée (par exemple
    ``requirements.txt`` pendant sa remédiation déterministe) avant de refiger le
    livrable et de rejouer le gate sur son nouveau diff.
    """
    ignored = frozenset(_safe_rel_path(path) for path in ignored_paths)
    expected_files = tuple(item for item in snapshot.files if item.path not in ignored)
    try:
        live = tuple(_capture_delivery_file(workspace.path, item.path) for item in expected_files)
    except (OSError, ValueError) as exc:
        raise DeliveryDriftError(f"snapshot de livraison invérifiable: {exc}") from exc
    if live == expected_files:
        return
    changed = [expected.path for expected, current in zip(expected_files, live, strict=True) if expected != current]
    raise DeliveryDriftError("workspace modifié depuis le snapshot: " + ", ".join(changed))


@dataclass(frozen=True)
class PrResult:
    """Résultat (ou aperçu) d'une ouverture de PR."""

    dry_run: bool
    title: str
    head: str
    base: str
    body: str
    number: Optional[int] = None
    html_url: Optional[str] = None
    skipped: bool = False  # PR déjà existante (idempotence)
    skipped_binaries: Tuple[str, ...] = ()  # (aperçu dry-run uniquement) binaires que la publication refuserait
    skipped_symlinks: Tuple[str, ...] = ()  # (aperçu dry-run uniquement) liens que la publication refuserait
    # Vague 3 : preuve immuable liée à la tête distante VÉRIFIÉE et persistée (None en dry-run ou sans vérification).
    proof: Optional[DeliveryProof] = None
    head_sha: Optional[str] = None


def build_pr_body(
    quality_report: QualityReport,
    issue: IssueSpec,
    *,
    closes_issue: bool = True,
    diff_sha256: Optional[str] = None,
    tree_sha: Optional[str] = None,
    base_tree_sha: Optional[str] = None,
) -> str:
    """Corps de PR : contexte issue + rapport qualité (fencé) + ``Closes`` + marqueur.

    ``closes_issue=False`` omet la ligne ``Closes #N`` : à utiliser quand le numéro
    ne référence PAS une vraie issue GitHub (ex. tâche d'amélioration G4, dont le
    numéro est un compteur de round) — sinon on fermerait une issue sans rapport.
    """
    lines = [
        f"## Exécution automatique de l'issue #{int(issue.number)}",
        "",
        f"> {inline(issue.title)}",
        "",
        "_PR générée par l'exécuteur Collègue (Phase 2). Merge sous CI verte + approbation humaine._",
        "",
        quality_report.to_markdown(),
        "",
    ]
    if closes_issue:
        lines += [f"Closes #{int(issue.number)}", ""]
    lines.append(exec_marker(issue.number))
    if diff_sha256 is not None:
        lines.append(diff_sha256_marker(diff_sha256))
    # Traçabilité humaine UNIQUEMENT : l'autorité est la preuve persistée (journal de décisions), jamais ce texte.
    if tree_sha is not None:
        lines.append(f"<!-- collegue-tree-sha:{tree_sha} -->")
    if base_tree_sha is not None:
        lines.append(f"<!-- collegue-base-tree-sha:{base_tree_sha} -->")
    return "\n".join(lines)


def open_pr(
    workspace: Workspace,
    quality_report: QualityReport,
    issue: IssueSpec,
    owner: str,
    repo: str,
    *,
    files_changed: Tuple[str, ...] = (),
    snapshot: Optional[DeliverySnapshot] = None,
    diff: str = "",
    base: str = DEFAULT_BASE_BRANCH,
    clients: Optional[PrClients] = None,
    dry_run: bool = True,
    manager: Optional[object] = None,
    project_id: Optional[int] = None,
    closes_issue: bool = True,
    draft: Optional[ProofDraft] = None,
) -> PrResult:
    """Ouvre (ou prévisualise) la PR de l'issue.

    ``dry_run=True`` (défaut) : renvoie un aperçu fidèle (titre/head/base/corps)
    **sans aucune écriture**. Sinon : vérification de la base distante, création de branche, commit des
    fichiers (suppression incluse), **vérification de l'objet commit distant** (arbre == arbre testé), ouverture de
    PR, liaison et persistance de la PR/preuve, et journalisation du numéro de PR si ``manager``+``project_id``.
    ``closes_issue=False`` n'ajoute pas ``Closes #N`` (numéro ≠ vraie issue, ex. G4).

    ``draft`` (vague 3) : contenu testé + verdicts + oracles de l'exécution (:class:`ProofDraft`). OBLIGATOIRE en
    mode réel, sans aucune dérogation : une publication réelle exige un brouillon de preuve qui PASSE, un ``manager`` et
    un ``project_id`` (la preuve est persistée hors du workspace), un dépôt distant relu et un arbre publié identique à
    l'arbre testé. Lève
    :class:`~collegue.executor.delivery_proof.DeliveryProofError` (et sous-classes) pour tout refus : binaire/lien non
    représentable, base déplacée, arbre publié différent, PR préexistante de contenu différent, preuve non persistable.
    """
    # Compatibilité des appelants historiques : sans manifeste explicite, on
    # capture UNE fois, avant tout appel réseau. Le reste de la fonction ne lit
    # ensuite plus le workspace. Les pipelines sensibles peuvent figer le
    # snapshot avant leurs gates puis le fournir ici.
    if snapshot is None:
        snapshot = capture_delivery_snapshot(workspace, files_changed, diff=diff)
    elif files_changed and tuple(_safe_rel_path(path) for path in files_changed) != snapshot.paths:
        raise ValueError("files_changed ne correspond pas au snapshot de livraison")

    verify = not dry_run
    content = draft.content if draft is not None else None
    if verify:
        assert_deliverable(snapshot)  # binaire/lien : refus explicite, jamais un simple avertissement
        if draft is None or content is None:
            raise DeliveryRefusedError(
                "LIVRAISON REFUSÉE — aucun contenu testé fourni : la PR ne peut pas être liée à une preuve"
            )
        assert_representable(content)
        if not draft.passed:
            raise DeliveryRefusedError(describe_refusal(draft))
        if manager is None or project_id is None:
            raise DeliveryRefusedError(
                "LIVRAISON REFUSÉE — manager et project_id requis : la preuve doit être persistée hors du workspace"
            )

    head = workspace.branch
    title = f"{inline(issue.title)} (issue #{int(issue.number)})"
    body = build_pr_body(
        quality_report,
        issue,
        closes_issue=closes_issue,
        diff_sha256=snapshot.diff_sha256,
        tree_sha=None if content is None else content.tree_sha,
        base_tree_sha=None if content is None else content.base_tree_sha,
    )

    skipped_binaries = snapshot.skipped_binaries
    skipped_symlinks = snapshot.skipped_symlinks
    if skipped_binaries:
        body += "\n\n> ⚠️ Fichiers binaires non poussés (non supportés) : " + ", ".join(
            f"`{p}`" for p in skipped_binaries
        )
    if skipped_symlinks:
        body += "\n\n> ⚠️ Liens symboliques non poussés (jamais lus, non supportés) : " + ", ".join(
            f"`{p}`" for p in skipped_symlinks
        )

    if dry_run:
        return PrResult(
            dry_run=True,
            title=title,
            head=head,
            base=base,
            body=body,
            skipped_binaries=skipped_binaries,
            skipped_symlinks=skipped_symlinks,
        )

    clients = clients or _default_clients()

    remote_base = None
    if verify:
        # AVANT toute écriture : la base distante doit avoir l'arbre de la base sur laquelle les contrôles ont tourné.
        remote_base = verify_remote_base(clients.branches, owner, repo, base, content)

    # Fixture de campagne : AUCUN contrôle (.github/, ci/) ne se publie par ce chemin. Lectures seules, AVANT ensure_branch, toute
    # écriture Git/Contents et toute création de PR ; preuve indisponible = refus. Hors campagne : sans effet.
    controls_anchor = None
    if verify:
        from collegue.pilot import w5_business_policy as _policy

        if _policy.applies(owner, repo, base):
            controls_anchor = _assert_publication_controls(
                workspace, snapshot, content, clients, owner, repo, remote_base
            )

    existing = clients.prs.find_pr_by_head(owner, repo, head, base=base)
    if existing is not None:
        proof = None
        existing_head = None
        if verify:
            number = getattr(existing, "number", None)
            existing_head = getattr(existing, "head_sha", None)
            if existing_head is None and number is not None:
                existing_head = getattr(clients.prs.get_pr(owner, repo, number), "head_sha", None)
            if not existing_head or number is None:
                raise DeliveryRefusedError(
                    "LIVRAISON REFUSÉE — la PR existante ne donne pas sa tête : contenu invérifiable"
                )
            # Une PR de même nom de branche mais de révision différente n'est JAMAIS présentée comme la livraison.
            verify_remote_head(
                clients.branches,
                owner,
                repo,
                head_sha=str(existing_head),
                content=content,
                remote_base_sha=remote_base.sha,
            )
            if controls_anchor is not None:
                _assert_remote_head_controls(clients, owner, repo, str(existing_head), controls_anchor)
            proof = seal_proof(
                draft,
                owner=owner,
                repo=repo,
                project_id=int(project_id),
                pr_number=int(number),
                head_sha=str(existing_head),
                base_sha=remote_base.sha,
            )
            proof = persist_or_reuse_delivery_proof(manager, proof)
        return PrResult(
            dry_run=False,
            title=title,
            head=head,
            base=base,
            body=body,
            number=getattr(existing, "number", None),
            html_url=getattr(existing, "html_url", None),
            skipped=True,
            proof=proof,
            head_sha=None if existing_head is None else str(existing_head),
        )

    clients.branches.ensure_branch(owner, repo, head, from_branch=base)

    for item in snapshot.files:
        message = f"collegue: issue #{int(issue.number)} — {item.path}"
        if item.operation == DELIVERY_UPDATE:
            clients.files.update_file(owner, repo, item.path, message, item.content or "", branch=head)
        elif item.operation == DELIVERY_DELETE:
            clients.files.delete_file(owner, repo, item.path, message, branch=head)

    head_sha = None
    if verify:
        try:
            head_sha = str(clients.branches.get_branch_sha(owner, repo, head))
        except Exception as exc:  # noqa: BLE001 - tête distante illisible = refus
            raise DeliveryRemoteError(f"tête distante '{head}' illisible: {exc}") from exc
        verify_remote_head(
            clients.branches, owner, repo, head_sha=head_sha, content=content, remote_base_sha=remote_base.sha
        )
        if (
            controls_anchor is not None
        ):  # défense en profondeur : le payload RÉELLEMENT publié, relu sur l'arbre distant
            _assert_remote_head_controls(clients, owner, repo, head_sha, controls_anchor)

    pr = clients.prs.create_pr(owner, repo, title, head, base, body)
    number = getattr(pr, "number", None)
    html_url = getattr(pr, "html_url", None)

    proof = None
    if verify:
        if number is None:
            raise DeliveryProofError("la PR créée ne porte pas de numéro : preuve non liable")
        info = clients.prs.get_pr(owner, repo, number)
        observed_head = getattr(info, "head_sha", None)
        if observed_head != head_sha:
            raise DeliveryDriftError(
                f"la PR #{number} observe une tête ({str(observed_head)[:12]}) différente de celle vérifiée "
                f"({head_sha[:12]}) : la branche a bougé pendant la publication"
            )
        observed_base = getattr(info, "base_sha", None)
        if observed_base is not None and observed_base != remote_base.sha:
            raise DeliveryDriftError(
                f"la PR #{number} observe une base ({str(observed_base)[:12]}) différente de la base vérifiée "
                f"({remote_base.sha[:12]}) : la base a bougé pendant la publication"
            )
        proof = seal_proof(
            draft,
            owner=owner,
            repo=repo,
            project_id=int(project_id),
            pr_number=int(number),
            head_sha=head_sha,
            base_sha=remote_base.sha,
        )
        proof = persist_or_reuse_delivery_proof(manager, proof)

    if manager is not None and project_id is not None and number is not None:
        manager.record_decision(
            project_id,
            f"PR #{number} ouverte pour l'issue #{int(issue.number)}",
            rationale=html_url,
        )

    return PrResult(
        dry_run=False,
        title=title,
        head=head,
        base=base,
        body=body,
        number=number,
        html_url=html_url,
        proof=proof,
        head_sha=head_sha,
    )


def _local_protected_rows(workspace: Workspace, content: TestedContent, policy) -> Tuple[dict, dict]:
    """Objets protégés de la base testée et du contenu testé, lus dans le Git de CONTRÔLE de l'hôte (jamais le ``.git`` de l'agent)."""
    from collegue.executor.git_boundary import TrustedGit, WorkspaceError

    repo = TrustedGit.locate(workspace.path)
    if repo is None:
        raise policy.PolicyRefusal(
            "workspace sans répertoire de contrôle Git : contenu testé invérifiable, publication refusée",
            kind="unavailable",
        )

    def rows(tree: str) -> dict:
        try:
            out = repo.must("ls-tree", "-r", "-z", "--full-tree", tree, what="lecture de l'arbre pour les contrôles")
        except WorkspaceError as exc:
            raise policy.PolicyRefusal(f"arbre {tree[:12]} illisible : {exc}", kind="unavailable") from exc
        found: dict = {}
        for record in out.split("\0"):
            if not record:
                continue
            meta, _, path = record.partition("\t")
            mode, _kind, sha = meta.split(" ")
            found[path] = (mode, sha.lower())
        return policy.select_protected(found)

    return rows(content.base_tree_sha), rows(content.tree_sha)


def _assert_publication_controls(
    workspace: Workspace,
    snapshot: DeliverySnapshot,
    content: TestedContent,
    clients: PrClients,
    owner: str,
    repo: str,
    remote_base,
):
    """Garde de publication de la fixture de campagne : à appeler AVANT toute écriture distante. Retourne l'ancre de confiance."""
    from collegue.pilot import w5_business_policy as policy

    try:
        anchor = policy.load_trust_anchor(clients.branches, owner, repo, os.environ.get(policy.TRUST_ANCHOR_ENV))
        remote = policy.select_protected(policy.read_remote_rows(clients.branches, owner, repo, remote_base.tree_sha))
        local_base, tested = _local_protected_rows(workspace, content, policy)
        policy.assert_publication_clean(
            anchor=anchor.rows, remote_base=remote, local_base=local_base, tested=tested, payload_paths=snapshot.paths
        )
    except policy.PolicyRefusal as refused:
        if refused.kind == "transient":  # panne de l'API : rien n'a été écrit, la livraison est refusée MAIS retentable
            raise DeliveryRemoteError(
                f"LIVRAISON REFUSÉE (contrôles de la fixture illisibles) — {refused.reason}"
            ) from refused
        raise DeliveryRefusedError(f"LIVRAISON REFUSÉE — {refused.reason}") from refused
    return anchor


def _assert_remote_head_controls(clients: PrClients, owner: str, repo: str, head_sha: str, anchor) -> None:
    """Relit les contrôles de la tête DISTANTE (PR préexistante ou tête publiée) : une publication antérieure ne la rend pas sûre."""
    from collegue.pilot import w5_business_policy as policy

    try:
        tree = clients.branches.get_git_commit(owner, repo, head_sha).tree_sha
        policy.assert_remote_head_clean(clients.branches, owner, repo, head_tree_sha=tree, anchor=anchor.rows)
    except policy.PolicyRefusal as refused:
        if refused.kind == "transient":
            raise DeliveryRemoteError(
                f"LIVRAISON REFUSÉE (contrôles de la tête illisibles) — {refused.reason}"
            ) from refused
        raise DeliveryRefusedError(f"LIVRAISON REFUSÉE — {refused.reason}") from refused
    except Exception as exc:  # noqa: BLE001 - tête distante illisible = refus
        raise DeliveryRemoteError(f"contrôles de la tête distante {head_sha[:12]} illisibles: {exc}") from exc


def _default_clients(token: Optional[str] = None) -> PrClients:  # pragma: no cover - chemin réel (integration)
    from collegue.tools.github_commands import BranchCommands, FileCommands, PRCommands

    return PrClients(
        branches=BranchCommands(token=token),
        files=FileCommands(token=token),
        prs=PRCommands(token=token),
    )
