"""Label Commands for GitHub Operations (P3, #354).

Création/garantie de labels et application à une issue. **Idempotent** :
``ensure_label`` pagine la liste complète (insensible à la casse) et, si le create
échoue (course 422 / élément hors page), re-vérifie avant de propager. Additif —
non câblé au runtime tant que la synchronisation du plan (P4) ne l'appelle pas.
"""

from typing import List, Optional

from pydantic import BaseModel

from ..base import ToolExecutionError
from ..clients import GitHubClient
from ._helpers import paginate, validate_ref


class LabelInfo(BaseModel):
    name: str
    color: str = "ededed"
    description: Optional[str] = None


class LabelCommands(GitHubClient):
    def list_labels(self, owner: str, repo: str) -> List[LabelInfo]:
        validate_ref(owner, "owner")
        validate_ref(repo, "repo")
        data = paginate(self._api_get, f"/repos/{owner}/{repo}/labels")
        return [
            LabelInfo(name=label["name"], color=label.get("color", "ededed"), description=label.get("description"))
            for label in data
        ]

    def _find_label(self, owner: str, repo: str, name: str) -> Optional[LabelInfo]:
        target = name.lower()
        for label in self.list_labels(owner, repo):
            if label.name.lower() == target:
                return label
        return None

    def ensure_label(
        self, owner: str, repo: str, name: str, color: str = "ededed", description: Optional[str] = None
    ) -> LabelInfo:
        """Retourne le label existant (insensible à la casse) ou le crée. Idempotent."""
        existing = self._find_label(owner, repo, name)
        if existing is not None:
            return existing
        try:
            resp = self._api_post(
                f"/repos/{owner}/{repo}/labels",
                {"name": name, "color": color, "description": description or ""},
            )
        except ToolExecutionError:
            # Course / élément hors page : re-vérifier avant de propager (ex. 422 déjà-existant).
            again = self._find_label(owner, repo, name)
            if again is not None:
                return again
            raise
        if not resp:
            raise ToolExecutionError(f"Réponse de création de label vide pour '{name}'.")
        return LabelInfo(name=resp["name"], color=resp.get("color", "ededed"), description=resp.get("description"))

    def add_labels_to_issue(self, owner: str, repo: str, issue_number: int, labels: List[str]) -> List[str]:
        """Ajoute des labels à une issue ; retourne la liste des labels résultants."""
        validate_ref(owner, "owner")
        validate_ref(repo, "repo")
        resp = self._api_post(
            f"/repos/{owner}/{repo}/issues/{issue_number}/labels",
            {"labels": list(labels)},
        )
        return [label["name"] for label in (resp or [])]
