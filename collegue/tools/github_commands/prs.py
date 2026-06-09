"""
Pull Request Commands for GitHub Operations.

Handles PR listing, details, files, comments, and creation.
"""

from typing import List, Optional

from pydantic import BaseModel

from ..base import ToolExecutionError
from ..clients import GitHubClient
from ._helpers import validate_ref

_MERGE_METHODS = ("merge", "squash", "rebase")


class PRInfo(BaseModel):
    number: int
    title: str
    state: str
    html_url: str
    user: str
    base_branch: str
    head_branch: str
    created_at: str
    updated_at: str
    labels: List[str] = []
    draft: bool = False


class IssueInfo(BaseModel):
    number: int
    title: str
    state: str
    html_url: str
    user: str
    labels: List[str] = []
    assignees: List[str] = []
    comments: int = 0
    created_at: str
    updated_at: str
    body: Optional[str] = None
    patch: Optional[str] = None


class BranchInfo(BaseModel):
    name: str
    commit: str
    protected: bool = False


class FileChange(BaseModel):
    filename: str
    status: str
    additions: int
    deletions: int
    changes: int = 0
    patch: Optional[str] = None


class Comment(BaseModel):
    id: int
    user: str
    body: str
    created_at: str
    path: Optional[str] = None
    line: Optional[int] = None


class MergeResult(BaseModel):
    merged: bool
    sha: Optional[str] = None
    message: str = ""
    already_merged: bool = False


class PRCommands(GitHubClient):
    def list_prs(self, owner: str, repo: str, state: str = "open", limit: int = 30) -> List[PRInfo]:
        data = self._api_get(
            f"/repos/{owner}/{repo}/pulls", {"state": state, "per_page": limit, "sort": "updated", "direction": "desc"}
        )

        return [
            PRInfo(
                number=pr["number"],
                title=pr["title"],
                state=pr["state"],
                html_url=pr["html_url"],
                user=pr["user"]["login"],
                base_branch=pr["base"]["ref"],
                head_branch=pr["head"]["ref"],
                created_at=pr["created_at"],
                updated_at=pr["updated_at"],
                labels=[l["name"] for l in pr.get("labels", [])],
                draft=pr.get("draft", False),
            )
            for pr in data[:limit]
        ]

    def find_pr_by_head(
        self, owner: str, repo: str, head: str, base: Optional[str] = None, state: str = "open"
    ) -> Optional[PRInfo]:
        """PR filtrée nativement par branche ``head`` (``owner:branch``), ou None.

        Idempotence fiable (vs scanner ``list_prs`` borné) : interroge directement
        l'API avec le filtre ``head`` pour retrouver une PR déjà ouverte.
        """
        # head = "owner:branch" : on suppose une PR intra-dépôt (head sur le même
        # owner). Un head cross-fork (autre owner) ne serait pas retrouvé ici.
        params = {"head": f"{owner}:{head}", "state": state}
        if base:
            params["base"] = base
        data = self._api_get(f"/repos/{owner}/{repo}/pulls", params)
        if not data:
            return None
        pr = data[0]
        return PRInfo(
            number=pr["number"],
            title=pr["title"],
            state=pr["state"],
            html_url=pr["html_url"],
            user=pr["user"]["login"],
            base_branch=pr["base"]["ref"],
            head_branch=pr["head"]["ref"],
            created_at=pr["created_at"],
            updated_at=pr["updated_at"],
            labels=[label["name"] for label in pr.get("labels", [])],
            draft=pr.get("draft", False),
        )

    def get_pr(self, owner: str, repo: str, pr_number: int) -> PRInfo:
        data = self._api_get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
        return PRInfo(
            number=data["number"],
            title=data["title"],
            state=data["state"],
            html_url=data["html_url"],
            user=data["user"]["login"],
            base_branch=data["base"]["ref"],
            head_branch=data["head"]["ref"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            labels=[l["name"] for l in data.get("labels", [])],
            draft=data.get("draft", False),
        )

    def get_pr_files(self, owner: str, repo: str, pr_number: int, limit: int = 100) -> List[FileChange]:
        """Get files changed in a pull request."""
        data = self._api_get(f"/repos/{owner}/{repo}/pulls/{pr_number}/files", {"per_page": limit})

        return [
            FileChange(
                filename=f["filename"],
                status=f["status"],
                additions=f.get("additions", 0),
                deletions=f.get("deletions", 0),
                changes=f.get("changes", 0),
                patch=f.get("patch", "")[:2000] if f.get("patch") else None,
            )
            for f in data[:limit]
        ]

    def get_pr_comments(self, owner: str, repo: str, pr_number: int, limit: int = 100) -> List[Comment]:
        """Get comments on a pull request."""
        review_comments = self._api_get(f"/repos/{owner}/{repo}/pulls/{pr_number}/comments", {"per_page": limit})
        issue_comments = self._api_get(f"/repos/{owner}/{repo}/issues/{pr_number}/comments", {"per_page": limit})

        all_comments = []

        for c in review_comments:
            all_comments.append(
                Comment(
                    id=c["id"],
                    user=c["user"]["login"],
                    body=c.get("body", ""),
                    created_at=c["created_at"],
                    path=c.get("path"),
                    line=c.get("line"),
                )
            )

        for c in issue_comments:
            all_comments.append(
                Comment(id=c["id"], user=c["user"]["login"], body=c.get("body", ""), created_at=c["created_at"])
            )

        all_comments.sort(key=lambda x: x.created_at)
        return all_comments[:limit]

    def create_pr(self, owner: str, repo: str, title: str, head: str, base: str, body: Optional[str] = None) -> PRInfo:
        data = {"title": title, "head": head, "base": base, "body": body or ""}
        resp = self._api_post(f"/repos/{owner}/{repo}/pulls", data)
        return PRInfo(
            number=resp["number"],
            title=resp["title"],
            state=resp["state"],
            html_url=resp["html_url"],
            user=resp["user"]["login"],
            base_branch=resp["base"]["ref"],
            head_branch=resp["head"]["ref"],
            created_at=resp["created_at"],
            updated_at=resp["updated_at"],
            draft=resp.get("draft", False),
        )

    def merge_pr(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        method: str = "squash",
        commit_title: Optional[str] = None,
        commit_message: Optional[str] = None,
        expected_head_sha: Optional[str] = None,
    ) -> MergeResult:
        """Merge une PR ouverte. **Idempotent** + **garde anti-course**.

        - Idempotence : si la PR est déjà mergée, retourne ``already_merged`` sans
          re-tenter (un second merge renverrait 405).
        - Garde anti-course : si ``expected_head_sha`` est fourni et ne correspond pas
          à la tête actuelle de la PR, **refuse** (on ne merge pas un état non vu). Le
          SHA est aussi transmis à l'API GitHub (double vérification côté serveur).

        ``method`` ∈ {merge, squash, rebase}. **Fail-closed** : toute erreur HTTP
        inattendue propage (``ToolExecutionError``) — on ne « réussit » jamais
        silencieusement. Aucun auto-déclenchement ici (H1, capacité seule) : c'est un
        appel explicite, la décision de merger appartient à H2 (auto-merge).
        """
        validate_ref(owner, "owner")
        validate_ref(repo, "repo")
        if method not in _MERGE_METHODS:
            raise ToolExecutionError(f"méthode de merge invalide: {method!r} (attendu: {_MERGE_METHODS})")

        current = self._api_get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
        if not isinstance(current, dict) or "state" not in current:
            # Réponse vide/partielle : on n'a pas confirmé l'état de la PR → on ne merge
            # pas un état non vu (fail-closed).
            raise ToolExecutionError(f"état de la PR #{pr_number} indisponible — refus de merger (fail-closed)")
        if current.get("merged"):
            return MergeResult(
                merged=True, sha=current.get("merge_commit_sha"), message="PR déjà mergée", already_merged=True
            )
        if current.get("state") == "closed":
            raise ToolExecutionError(f"PR #{pr_number} fermée sans merge — refus de merger")
        head_sha = (current.get("head") or {}).get("sha")
        if expected_head_sha and head_sha != expected_head_sha:
            raise ToolExecutionError(
                f"la tête de la PR #{pr_number} a bougé (attendu {expected_head_sha}, vu {head_sha}) — "
                "refus (anti-course)"
            )

        body: dict = {"merge_method": method}
        if commit_title:
            body["commit_title"] = commit_title
        if commit_message:
            body["commit_message"] = commit_message
        if expected_head_sha:
            body["sha"] = expected_head_sha

        resp = self._api_put(f"/repos/{owner}/{repo}/pulls/{pr_number}/merge", body) or {}
        # Un merge réussi renvoie toujours ``{"merged": true, "sha": ...}`` ; l'absence
        # de confirmation = échec (défaut False), jamais un succès supposé (fail-closed).
        return MergeResult(merged=bool(resp.get("merged", False)), sha=resp.get("sha"), message=resp.get("message", ""))
