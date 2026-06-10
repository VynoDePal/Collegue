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

import os
from dataclasses import dataclass
from typing import Optional, Tuple

from collegue.executor.agent import IssueSpec
from collegue.executor.quality_gate import QualityReport
from collegue.executor.workspace import Workspace
from collegue.textnorm import inline

DEFAULT_BASE_BRANCH = "main"


def exec_marker(issue_number: int) -> str:
    """Marqueur HTML traçant la PR générée par l'exécuteur pour une issue."""
    return f"<!-- collegue-exec:{int(issue_number)} -->"


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


def build_pr_body(quality_report: QualityReport, issue: IssueSpec, *, closes_issue: bool = True) -> str:
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
    return "\n".join(lines)


def open_pr(
    workspace: Workspace,
    quality_report: QualityReport,
    issue: IssueSpec,
    owner: str,
    repo: str,
    *,
    files_changed: Tuple[str, ...] = (),
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
    head = workspace.branch
    title = f"{inline(issue.title)} (issue #{int(issue.number)})"
    body = build_pr_body(quality_report, issue, closes_issue=closes_issue)

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

    skipped_binaries: list[str] = []
    skipped_symlinks: list[str] = []
    for path in files_changed:
        rel = _safe_rel_path(path)
        if os.path.islink(os.path.join(workspace.path, rel)):
            # Lien symbolique (ex. `node_modules/.bin/*` après un `npm install`) :
            # la Contents API ne représente pas les symlinks, et un lien peut viser
            # un secret hôte — on ne le LIT jamais. Même politique que les binaires :
            # SAUTER + tracer, plutôt que faire échouer toute la tâche. [#423]
            skipped_symlinks.append(rel)
            continue
        full = _resolve_in_workspace(workspace.path, rel)
        message = f"collegue: issue #{int(issue.number)} — {rel}"
        if os.path.isfile(full):
            try:
                with open(full, "r", encoding="utf-8") as handle:
                    content = handle.read()
            except UnicodeDecodeError:
                # Fichier binaire (PNG/PDF/…) : la Contents API n'est câblée qu'en
                # texte UTF-8 (limite §MVP). On le SAUTE plutôt que de faire échouer
                # toute la tâche sur un asset — la PR porte le reste du diff. [#410]
                skipped_binaries.append(rel)
                continue
            clients.files.update_file(owner, repo, rel, message, content, branch=head)
        else:
            # Fichier supprimé par l'agent : on le retire aussi sur la branche.
            clients.files.delete_file(owner, repo, rel, message, branch=head)

    if skipped_binaries:
        # Trace visible pour le relecteur (la PR ne contient pas ces binaires).
        body += "\n\n> ⚠️ Fichiers binaires non poussés (non supportés) : " + ", ".join(
            f"`{p}`" for p in skipped_binaries
        )
    if skipped_symlinks:
        body += "\n\n> ⚠️ Liens symboliques non poussés (jamais lus, non supportés) : " + ", ".join(
            f"`{p}`" for p in skipped_symlinks
        )

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
        skipped_binaries=tuple(skipped_binaries),
        skipped_symlinks=tuple(skipped_symlinks),
    )


def _default_clients(token: Optional[str] = None) -> PrClients:  # pragma: no cover - chemin réel (integration)
    from collegue.tools.github_commands import BranchCommands, FileCommands, PRCommands

    return PrClients(
        branches=BranchCommands(token=token),
        files=FileCommands(token=token),
        prs=PRCommands(token=token),
    )
