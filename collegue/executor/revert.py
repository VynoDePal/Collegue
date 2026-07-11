"""Revert d'un commit mergé (H1, epic #391, Phase 5).

Primitive **git** réutilisable par la garde post-merge (H3) : à partir d'un commit
déjà mergé sur ``main``, produit un commit d'annulation sur une branche dédiée —
**sans rien pousser ni merger** (capacité seule, décision : aucun déclenchement auto
en H1). Le push de la branche + l'ouverture de la PR de revert relèvent de H3 /
``integration``.

Plomberie git **locale** sur un dépôt de confiance (clone d'un ``repo_source``),
comme :mod:`collegue.executor.workspace`. Jamais d'exécution de code non fiable ici.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from typing import Optional

from collegue.executor.command import CommandRunner, LocalCommandRunner

# SHA git (court ou complet). Validé avant d'être passé à ``git`` (défense en
# profondeur : l'argv n'est pas un shell, mais on refuse tout ce qui n'est pas un SHA).
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
REVERT_BRANCH_PREFIX = "collegue/revert-"
# Identité de commit du bot (le clone peut n'avoir aucune config git en CI/Docker).
_BOT_EMAIL = "collegue-bot@users.noreply.github.com"
_BOT_NAME = "Collègue Bot"


@dataclass(frozen=True)
class RevertResult:
    """Résultat d'un revert local (aucune écriture distante)."""

    reverted: bool
    message: str
    revert_sha: Optional[str] = None
    branch: Optional[str] = None
    workspace: Optional[str] = None


class RevertError(RuntimeError):
    """Échec de préparation du revert (source invalide, SHA invalide, git en erreur)."""


def _validate_sha(sha: str) -> str:
    if not sha or not _SHA_RE.match(sha):
        raise RevertError(f"SHA de commit invalide: {sha!r}")
    return sha


def revert_commit(
    workspace_path: str,
    sha: str,
    *,
    merge_parent: Optional[int] = None,
    runner: Optional[CommandRunner] = None,
    git_bin: str = "git",
) -> RevertResult:
    """``git revert --no-edit`` de ``sha`` dans un workspace déjà préparé.

    ``merge_parent`` (ex. ``1``) est requis pour révertir un **commit de merge**
    (option ``-m``). **Fail-closed** : si le revert échoue (conflit…), on **abort**
    (laisse le workspace propre) et on renvoie ``reverted=False`` avec le message —
    le caller (H3) décide ; on ne laisse jamais un état conflictuel exploitable.
    """
    _validate_sha(sha)
    runner = runner or LocalCommandRunner()

    # Identité fournie en ``-c`` (éphémère, pas de config persistante) : le revert
    # crée un commit, et un workspace cloné en CI/Docker peut n'avoir AUCUNE identité
    # git → le commit échouerait. ``revert_commit`` est réutilisé directement par H3,
    # donc l'identité doit vivre ici, pas seulement dans ``prepare_revert``.
    cmd = [git_bin, "-c", f"user.email={_BOT_EMAIL}", "-c", f"user.name={_BOT_NAME}", "revert", "--no-edit"]
    if merge_parent is not None:
        cmd += ["-m", str(int(merge_parent))]
    cmd.append(sha)

    res = runner.run_command(cmd, workspace_path)
    if not res.ok:
        # Best-effort : nettoyer un revert partiel/conflictuel pour ne pas bloquer un retry.
        runner.run_command([git_bin, "revert", "--abort"], workspace_path)
        return RevertResult(
            reverted=False,
            message=(res.stderr.strip() or res.stdout.strip() or "git revert a échoué"),
            workspace=workspace_path,
        )

    head = runner.run_command([git_bin, "rev-parse", "HEAD"], workspace_path)
    revert_sha = head.stdout.strip() if head.ok else None
    return RevertResult(reverted=True, message="revert appliqué", revert_sha=revert_sha, workspace=workspace_path)


def prepare_revert(
    repo_source: str,
    sha: str,
    *,
    merge_parent: Optional[int] = None,
    branch: Optional[str] = None,
    runner: Optional[CommandRunner] = None,
    git_bin: str = "git",
    dest_root: Optional[str] = None,
) -> RevertResult:
    """Clone ``repo_source``, crée une branche de revert depuis la tête clonée et y
    applique le revert de ``sha``. **Ne pousse rien** (capacité locale ; le push +
    l'ouverture de la PR de revert = H3 / ``integration``).
    """
    _validate_sha(sha)
    runner = runner or LocalCommandRunner()
    source = os.path.realpath(os.path.abspath(repo_source))
    if not os.path.isdir(os.path.join(source, ".git")):
        raise RevertError(f"repo_source n'est pas un dépôt git: {repo_source}")

    owns_parent = dest_root is None
    parent = dest_root or tempfile.mkdtemp(prefix="collegue-revert-")
    os.makedirs(parent, exist_ok=True)
    dest = os.path.join(parent, "workspace")
    clone = runner.run_command([git_bin, "clone", "--quiet", source, dest], parent)
    if not clone.ok:
        if owns_parent:  # #466 : un clone raté ne laisse pas de répertoire orphelin
            shutil.rmtree(parent, ignore_errors=True)
        raise RevertError(f"git clone a échoué: {clone.stderr.strip() or clone.stdout.strip()}")

    # L'identité de commit est fournie par ``revert_commit`` (en ``-c``), pas besoin de
    # config persistante ici.
    revert_branch = branch or f"{REVERT_BRANCH_PREFIX}{sha[:12]}"
    checkout = runner.run_command([git_bin, "checkout", "-q", "-b", revert_branch], dest)
    if not checkout.ok:
        if owns_parent:
            shutil.rmtree(parent, ignore_errors=True)
        raise RevertError(f"git checkout -b {revert_branch} a échoué: {checkout.stderr.strip()}")

    result = revert_commit(dest, sha, merge_parent=merge_parent, runner=runner, git_bin=git_bin)
    # Rattacher le nom de branche (``revert_commit`` ne le connaît pas).
    return RevertResult(
        reverted=result.reverted,
        message=result.message,
        revert_sha=result.revert_sha,
        branch=revert_branch,
        workspace=dest,
    )


def revert_pr_preview(sha: str, *, base: str = "main", reason: str = "", automatic: bool = False) -> dict:
    """Titre + corps proposés pour la PR de revert (**aperçu dry_run**, sans écriture)."""
    _validate_sha(sha)
    short = sha[:12]
    lines = [f"Annulation automatique du commit `{short}` (garde post-merge).", ""]
    if reason:
        lines += [f"Raison : {reason}", ""]
    if automatic:
        lines.append("PR de revert de sécurité générée par Collègue (Phase 5) ; merge automatique sous gardes CI/SHA.")
    else:
        lines.append("PR de revert générée par Collègue (Phase 5, H3). Merge sous approbation humaine (§6).")
    return {"title": f"Revert de {short} sur {base}", "body": "\n".join(lines)}
