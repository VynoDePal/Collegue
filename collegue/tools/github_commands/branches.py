"""
Branch Commands for GitHub Operations.

Handles branch listing, creation, and commit operations.
"""

from typing import Iterable, List, Optional

from pydantic import BaseModel

from ..base import ToolExecutionError
from ..clients import GitHubClient
from ._helpers import validate_ref

# Branches refusées par défaut à la suppression (garde-fou contre la perte de la base).
PROTECTED_BRANCHES = ("main", "master")


class BranchInfo(BaseModel):
    name: str
    commit_sha: str
    protected: bool = False


class CommitInfo(BaseModel):
    sha: str
    message: str
    author: str
    date: str
    html_url: str


class BranchCommands(GitHubClient):
    def list_branches(self, owner: str, repo: str, limit: int = 30) -> List[BranchInfo]:
        data = self._api_get(f"/repos/{owner}/{repo}/branches", {"per_page": limit})
        return [
            BranchInfo(name=b["name"], commit_sha=b["commit"]["sha"], protected=b.get("protected", False))
            for b in data[:limit]
        ]

    def list_commits(self, owner: str, repo: str, branch: Optional[str] = None, limit: int = 30) -> List[CommitInfo]:
        params = {"per_page": limit}
        if branch:
            params["sha"] = branch

        data = self._api_get(f"/repos/{owner}/{repo}/commits", params)
        return [
            CommitInfo(
                sha=c["sha"],
                message=c["commit"]["message"],
                author=c["commit"]["author"]["name"],
                date=c["commit"]["author"]["date"],
                html_url=c["html_url"],
            )
            for c in data[:limit]
        ]

    def _get_branch_sha(self, owner: str, repo: str, branch: str) -> str:
        try:
            resp = self._api_get(f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
            return resp["object"]["sha"]
        except ToolExecutionError as e:
            raise ToolExecutionError(f"Source branch '{branch}' not found") from e

    def create_branch(self, owner: str, repo: str, branch: str, from_branch: Optional[str] = None) -> BranchInfo:
        if not from_branch:
            repo_info = self._api_get(f"/repos/{owner}/{repo}")
            from_branch = repo_info.get("default_branch", "main")

        sha = self._get_branch_sha(owner, repo, from_branch)

        data = {"ref": f"refs/heads/{branch}", "sha": sha}
        resp = self._api_post(f"/repos/{owner}/{repo}/git/refs", data)
        return BranchInfo(name=branch, commit_sha=resp["object"]["sha"], protected=False)

    def _branch_sha_or_none(self, owner: str, repo: str, branch: str) -> Optional[str]:
        """SHA de la branche, ou None si elle n'existe pas (variante non-levante)."""
        try:
            return self._get_branch_sha(owner, repo, branch)
        except ToolExecutionError:
            return None

    def ensure_branch(self, owner: str, repo: str, branch: str, from_branch: Optional[str] = None) -> BranchInfo:
        """Retourne la branche existante ou la crée depuis ``from_branch``. Idempotent.

        Évite le 422 « Reference already exists » lors d'un retry (ex. reprise après
        échec partiel) et gère la course création (re-vérifie avant de propager).
        """
        existing = self._branch_sha_or_none(owner, repo, branch)
        if existing is not None:
            return BranchInfo(name=branch, commit_sha=existing, protected=False)
        try:
            return self.create_branch(owner, repo, branch, from_branch)
        except ToolExecutionError:
            again = self._branch_sha_or_none(owner, repo, branch)
            if again is not None:
                return BranchInfo(name=branch, commit_sha=again, protected=False)
            raise

    def delete_branch(
        self,
        owner: str,
        repo: str,
        branch: str,
        *,
        protect: Iterable[str] = PROTECTED_BRANCHES,
        default_branch: Optional[str] = None,
    ) -> bool:
        """Supprime une branche. **Idempotent** + **refuse les branches protégées**.

        - Refuse ``main``/``master``, tout nom dans ``protect``, **et la vraie branche
          par défaut du dépôt** (résolue via l'API) : un dépôt dont la base est
          ``develop``/``trunk`` est protégé aussi, pas seulement ``main``/``master``.
          La résolution est **fail-closed** : si on ne peut pas déterminer la base, on
          refuse (op destructive). Le caller peut passer ``default_branch`` pour
          éviter le round-trip.
        - La comparaison est normalisée (casse + ``/``/``.`` final) en défense en
          profondeur contre les variantes.
        - Idempotent : si la branche n'existe pas (déjà supprimée), renvoie ``True``
          sans erreur. Gère aussi la **course** (suppression concurrente pendant
          l'appel) en re-vérifiant l'absence avant de propager.
        """
        validate_ref(owner, "owner")
        validate_ref(repo, "repo")
        # Les noms de branche GitHub peuvent contenir des '/' (``feat/x``), donc on
        # ne réutilise pas ``validate_ref`` (alphanum strict) : on bloque seulement la
        # traversée / les caractères dangereux avant interpolation dans l'URL.
        if not branch or branch.startswith("/") or ".." in branch or any(c.isspace() for c in branch):
            raise ToolExecutionError(f"nom de branche invalide: {branch!r}")

        def _norm(name: str) -> str:
            return name.strip().rstrip("/.").lower()

        norm = _norm(branch)
        # 1) Garde littérale (main/master) : refus SANS round-trip réseau.
        if norm in {_norm(p) for p in protect if p}:
            raise ToolExecutionError(f"refus de supprimer la branche protégée: {branch!r}")
        # 2) Vraie branche par défaut (fail-closed si non résolvable).
        if default_branch is None:
            try:
                repo_info = self._api_get(f"/repos/{owner}/{repo}") or {}
            except ToolExecutionError as e:
                raise ToolExecutionError(
                    f"branche par défaut de {owner}/{repo} non résolue — refus de supprimer (fail-closed)"
                ) from e
            default_branch = repo_info.get("default_branch")
        if default_branch and norm == _norm(default_branch):
            raise ToolExecutionError(f"refus de supprimer la branche par défaut: {branch!r}")

        if self._branch_sha_or_none(owner, repo, branch) is None:
            return True  # déjà absente → succès idempotent
        try:
            self._request_json("DELETE", f"/repos/{owner}/{repo}/git/refs/heads/{branch}")
        except ToolExecutionError:
            # Course : supprimée entre-temps ? Si oui, succès idempotent ; sinon propage.
            if self._branch_sha_or_none(owner, repo, branch) is None:
                return True
            raise
        return True
