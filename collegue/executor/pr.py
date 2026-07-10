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
(stack cible web Python+JS/TS) ; les fichiers binaires ne sont pas supportés.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Optional, Tuple

from collegue.executor.agent import IssueSpec
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


class DeliveryDriftError(RuntimeError):
    """Le workspace vivant ne correspond plus au snapshot autorisé."""


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
    skipped_binaries: Tuple[str, ...] = ()  # fichiers binaires non poussés (cf. #410)
    skipped_symlinks: Tuple[str, ...] = ()  # liens symboliques non poussés (cf. #423)


def build_pr_body(
    quality_report: QualityReport,
    issue: IssueSpec,
    *,
    closes_issue: bool = True,
    diff_sha256: Optional[str] = None,
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
) -> PrResult:
    """Ouvre (ou prévisualise) la PR de l'issue.

    ``dry_run=True`` (défaut) : renvoie un aperçu fidèle (titre/head/base/corps)
    **sans aucune écriture**. Sinon : idempotence (PR ouverte existante retournée),
    création de branche, commit des fichiers (suppression incluse), ouverture de PR,
    et journalisation du numéro de PR si ``manager``+``project_id`` sont fournis.
    ``closes_issue=False`` n'ajoute pas ``Closes #N`` (numéro ≠ vraie issue, ex. G4).
    """
    # Compatibilité des appelants historiques : sans manifeste explicite, on
    # capture UNE fois, avant tout appel réseau. Le reste de la fonction ne lit
    # ensuite plus le workspace. Les pipelines sensibles peuvent figer le
    # snapshot avant leurs gates puis le fournir ici.
    if snapshot is None:
        snapshot = capture_delivery_snapshot(workspace, files_changed, diff=diff)
    elif files_changed and tuple(_safe_rel_path(path) for path in files_changed) != snapshot.paths:
        raise ValueError("files_changed ne correspond pas au snapshot de livraison")

    head = workspace.branch
    title = f"{inline(issue.title)} (issue #{int(issue.number)})"
    body = build_pr_body(
        quality_report,
        issue,
        closes_issue=closes_issue,
        diff_sha256=snapshot.diff_sha256,
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
        return PrResult(dry_run=True, title=title, head=head, base=base, body=body)

    clients = clients or _default_clients()

    existing = clients.prs.find_pr_by_head(owner, repo, head, base=base)
    if existing is not None:
        return PrResult(
            dry_run=False,
            title=title,
            head=head,
            base=base,
            body=body,
            number=getattr(existing, "number", None),
            html_url=getattr(existing, "html_url", None),
            skipped=True,
        )

    clients.branches.ensure_branch(owner, repo, head, from_branch=base)

    for item in snapshot.files:
        message = f"collegue: issue #{int(issue.number)} — {item.path}"
        if item.operation == DELIVERY_UPDATE:
            clients.files.update_file(owner, repo, item.path, message, item.content or "", branch=head)
        elif item.operation == DELIVERY_DELETE:
            clients.files.delete_file(owner, repo, item.path, message, branch=head)

    pr = clients.prs.create_pr(owner, repo, title, head, base, body)
    number = getattr(pr, "number", None)
    html_url = getattr(pr, "html_url", None)

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
        skipped_binaries=skipped_binaries,
        skipped_symlinks=skipped_symlinks,
    )


def _default_clients(token: Optional[str] = None) -> PrClients:  # pragma: no cover - chemin réel (integration)
    from collegue.tools.github_commands import BranchCommands, FileCommands, PRCommands

    return PrClients(
        branches=BranchCommands(token=token),
        files=FileCommands(token=token),
        prs=PRCommands(token=token),
    )
