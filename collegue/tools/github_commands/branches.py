"""
Branch Commands for GitHub Operations.

Handles branch listing, creation, and commit operations.
"""

import re
from typing import Iterable, List, Optional

from pydantic import BaseModel

from ..base import ToolExecutionError
from ..clients import GitHubClient
from ._helpers import validate_ref

# Branches refusées par défaut à la suppression (garde-fou contre la perte de la base).
PROTECTED_BRANCHES = ("main", "master")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


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


class GitCommitInfo(BaseModel):
    """Objet commit Git autoritatif (Git Data API), réduit aux invariants utiles."""

    sha: str
    tree_sha: str
    parents: List[str]
    message: str


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
            # Une absence est la seule erreur que les appelants idempotents
            # peuvent convertir en ``None``. Préserver explicitement le 404 ;
            # auth, rate-limit, 5xx et pannes réseau doivent rester bloquants.
            if getattr(e, "status_code", 0) == 404:
                raise ToolExecutionError(f"Source branch '{branch}' not found", status_code=404) from e
            raise

    def get_branch_sha(self, owner: str, repo: str, branch: str) -> str:
        """SHA courant d'une branche (API publique, fail-closed)."""
        validate_ref(owner, "owner")
        validate_ref(repo, "repo")
        self._validate_branch_name(branch)
        return self._get_branch_sha(owner, repo, branch)

    @staticmethod
    def _validate_branch_name(branch: str) -> None:
        invalid_char = any(c.isspace() or ord(c) < 32 or c in "~^:?*[\\" for c in branch)
        invalid_part = any(not part or part.endswith(".lock") for part in branch.split("/"))
        if (
            not branch
            or branch.startswith(("/", "."))
            or branch.endswith(("/", "."))
            or ".." in branch
            or "@{" in branch
            or invalid_char
            or invalid_part
        ):
            raise ToolExecutionError(f"nom de branche invalide: {branch!r}")

    @staticmethod
    def _validate_full_sha(sha: str, label: str) -> str:
        if not sha or not _SHA_RE.fullmatch(str(sha)):
            raise ToolExecutionError(f"{label} invalide: {sha!r}")
        return str(sha)

    def get_git_commit(self, owner: str, repo: str, sha: str) -> GitCommitInfo:
        """Lit un objet commit Git et valide strictement tree, parents et message."""
        validate_ref(owner, "owner")
        validate_ref(repo, "repo")
        sha = self._validate_full_sha(sha, "SHA du commit")
        data = self._api_get(f"/repos/{owner}/{repo}/git/commits/{sha}") or {}
        response_sha = self._validate_full_sha(data.get("sha"), "SHA du commit retourné")
        if response_sha != sha:
            raise ToolExecutionError(f"commit Git inattendu: demandé {sha}, reçu {response_sha}")
        tree_sha = self._validate_full_sha((data.get("tree") or {}).get("sha"), "SHA du tree")
        raw_parents = data.get("parents")
        if not isinstance(raw_parents, list):
            raise ToolExecutionError("parents du commit Git absents ou malformés")
        parents = [
            self._validate_full_sha(parent.get("sha") if isinstance(parent, dict) else None, "SHA parent")
            for parent in raw_parents
        ]
        message = data.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ToolExecutionError("message du commit Git absent ou malformé")
        return GitCommitInfo(sha=response_sha, tree_sha=tree_sha, parents=parents, message=message)

    def _branch_matches_commit_tree(
        self,
        owner: str,
        repo: str,
        branch_sha: str,
        *,
        parent_sha: str,
        tree_sha: str,
        message: str,
    ) -> bool:
        try:
            commit = self.get_git_commit(owner, repo, branch_sha)
        except ToolExecutionError:
            return False
        return commit.tree_sha == tree_sha and commit.parents == [parent_sha] and commit.message == message

    def ensure_commit_branch(
        self,
        owner: str,
        repo: str,
        branch: str,
        *,
        parent_sha: str,
        tree_sha: str,
        message: str,
    ) -> BranchInfo:
        """Crée une branche sur un nouveau commit qui réutilise un tree Git existant.

        Aucun fichier local n'est téléversé : le tree doit déjà appartenir au dépôt.
        Idempotent après crash, sans force-push : une branche existante n'est admise
        que si son commit possède exactement ``parent_sha``, ``tree_sha`` et
        ``message``.
        """
        validate_ref(owner, "owner")
        validate_ref(repo, "repo")
        self._validate_branch_name(branch)
        parent_sha = self._validate_full_sha(parent_sha, "SHA parent")
        tree_sha = self._validate_full_sha(tree_sha, "SHA tree")
        if not isinstance(message, str) or not message.strip():
            raise ToolExecutionError("message du commit vide — refus fail-closed")

        existing = self._branch_sha_or_none(owner, repo, branch)
        if existing is not None:
            if self._branch_matches_commit_tree(
                owner,
                repo,
                existing,
                parent_sha=parent_sha,
                tree_sha=tree_sha,
                message=message,
            ):
                return BranchInfo(name=branch, commit_sha=existing, protected=False)
            raise ToolExecutionError(f"branche {branch!r} déjà présente avec un autre parent/tree/message — refus")

        commit = (
            self._api_post(
                f"/repos/{owner}/{repo}/git/commits",
                {"message": message, "tree": tree_sha, "parents": [parent_sha]},
            )
            or {}
        )
        commit_sha = self._validate_full_sha(commit.get("sha"), "commit créé")
        if not self._branch_matches_commit_tree(
            owner,
            repo,
            commit_sha,
            parent_sha=parent_sha,
            tree_sha=tree_sha,
            message=message,
        ):
            raise ToolExecutionError("le commit de branche créé ne correspond pas au parent/tree/message demandés")
        try:
            self._api_post(
                f"/repos/{owner}/{repo}/git/refs",
                {"ref": f"refs/heads/{branch}", "sha": commit_sha},
            )
        except ToolExecutionError:
            # Course/reprise : seule une branche exactement équivalente est admise.
            raced = self._branch_sha_or_none(owner, repo, branch)
            if raced is None or not self._branch_matches_commit_tree(
                owner,
                repo,
                raced,
                parent_sha=parent_sha,
                tree_sha=tree_sha,
                message=message,
            ):
                raise
            commit_sha = raced
        current = self._branch_sha_or_none(owner, repo, branch)
        if current != commit_sha or not self._branch_matches_commit_tree(
            owner,
            repo,
            commit_sha,
            parent_sha=parent_sha,
            tree_sha=tree_sha,
            message=message,
        ):
            raise ToolExecutionError(f"branche {branch!r} mobile ou commit distant non conforme")
        return BranchInfo(name=branch, commit_sha=commit_sha, protected=False)

    def create_branch(self, owner: str, repo: str, branch: str, from_branch: Optional[str] = None) -> BranchInfo:
        if not from_branch:
            repo_info = self._api_get(f"/repos/{owner}/{repo}")
            from_branch = repo_info.get("default_branch", "main")

        sha = self._get_branch_sha(owner, repo, from_branch)

        data = {"ref": f"refs/heads/{branch}", "sha": sha}
        resp = self._api_post(f"/repos/{owner}/{repo}/git/refs", data)
        return BranchInfo(name=branch, commit_sha=resp["object"]["sha"], protected=False)

    def _branch_sha_or_none(self, owner: str, repo: str, branch: str) -> Optional[str]:
        """SHA de la branche, ou ``None`` uniquement sur un 404 confirmé."""
        try:
            return self._get_branch_sha(owner, repo, branch)
        except ToolExecutionError as exc:
            if getattr(exc, "status_code", 0) == 404:
                return None
            raise

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
        expected_sha: Optional[str] = None,
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
        - Si ``expected_sha`` est fourni, refuse de supprimer une branche existante
          dont la tête a bougé depuis son inventaire par le caller.
        """
        validate_ref(owner, "owner")
        validate_ref(repo, "repo")
        # Les noms de branche GitHub peuvent contenir des '/' (``feat/x``), donc on
        # ne réutilise pas ``validate_ref`` (alphanum strict) : on bloque seulement la
        # traversée / les caractères dangereux avant interpolation dans l'URL.
        self._validate_branch_name(branch)

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

        current_sha = self._branch_sha_or_none(owner, repo, branch)
        if current_sha is None:
            return True  # déjà absente → succès idempotent
        if expected_sha is not None and current_sha != expected_sha:
            raise ToolExecutionError(
                f"refus de supprimer la branche {branch!r}: SHA attendu {expected_sha}, vu {current_sha}"
            )
        try:
            self._request_json("DELETE", f"/repos/{owner}/{repo}/git/refs/heads/{branch}")
        except ToolExecutionError:
            # Course : supprimée entre-temps ? Si oui, succès idempotent ; sinon propage.
            if self._branch_sha_or_none(owner, repo, branch) is None:
                return True
            raise
        # Un DELETE 2xx ne suffit pas comme preuve pour une opération destructive :
        # confirmer l'absence distante. Toute lecture autre qu'un 404 reste
        # bloquante via ``_branch_sha_or_none``.
        remaining_sha = self._branch_sha_or_none(owner, repo, branch)
        if remaining_sha is not None:
            raise ToolExecutionError(
                f"suppression de la branche {branch!r} non confirmée: tête distante {remaining_sha}"
            )
        return True
