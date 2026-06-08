"""Milestone Commands for GitHub Operations (P3, #354).

Création/garantie de milestones (jalons de phase/deadline) et assignation à une
issue. **Idempotent** : ``ensure_milestone`` pagine la liste complète (open+closed)
et re-vérifie si le create échoue. Additif — non câblé tant que P4 ne l'appelle pas.
"""

from typing import List, Optional

from pydantic import BaseModel

from ..base import ToolExecutionError
from ..clients import GitHubClient
from ._helpers import paginate, validate_ref


class MilestoneInfo(BaseModel):
    number: int
    title: str
    state: str = "open"
    due_on: Optional[str] = None
    html_url: Optional[str] = None


def _to_info(m: dict) -> MilestoneInfo:
    return MilestoneInfo(
        number=m["number"],
        title=m["title"],
        state=m.get("state", "open"),
        due_on=m.get("due_on"),
        html_url=m.get("html_url"),
    )


class MilestoneCommands(GitHubClient):
    def list_milestones(self, owner: str, repo: str, state: str = "all") -> List[MilestoneInfo]:
        validate_ref(owner, "owner")
        validate_ref(repo, "repo")
        data = paginate(self._api_get, f"/repos/{owner}/{repo}/milestones", {"state": state})
        return [_to_info(m) for m in data]

    def _find_milestone(self, owner: str, repo: str, title: str) -> Optional[MilestoneInfo]:
        for milestone in self.list_milestones(owner, repo):
            if milestone.title == title:
                return milestone
        return None

    def ensure_milestone(
        self, owner: str, repo: str, title: str, description: Optional[str] = None, due_on: Optional[str] = None
    ) -> MilestoneInfo:
        """Retourne le milestone existant (même titre) ou le crée. Idempotent."""
        existing = self._find_milestone(owner, repo, title)
        if existing is not None:
            return existing
        payload = {"title": title}
        if description:
            payload["description"] = description
        if due_on:
            payload["due_on"] = due_on  # ISO-8601, ex. "2026-12-31T23:59:59Z"
        try:
            resp = self._api_post(f"/repos/{owner}/{repo}/milestones", payload)
        except ToolExecutionError:
            again = self._find_milestone(owner, repo, title)
            if again is not None:
                return again
            raise
        if not resp:
            raise ToolExecutionError(f"Réponse de création de milestone vide pour '{title}'.")
        return _to_info(resp)

    def assign_milestone(self, owner: str, repo: str, issue_number: int, milestone_number: int) -> None:
        """Rattache une issue à un milestone (PATCH de l'issue)."""
        validate_ref(owner, "owner")
        validate_ref(repo, "repo")
        self._request_json(
            "PATCH",
            f"/repos/{owner}/{repo}/issues/{issue_number}",
            json_data={"milestone": milestone_number},
        )
