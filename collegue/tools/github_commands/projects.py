"""Project (board) Commands for GitHub Operations (P3, #354).

GitHub **Projects v2** (kanban) n'a **que** l'API GraphQL (pas de REST) : on poste
donc sur ``/graphql`` via le client unifié. Permet de garantir un board (par titre,
sous un owner user/org) et d'y ajouter une issue. Idempotent : ``ensure_project``
réutilise un board de même titre. Additif — non câblé au runtime tant que P4 ne
l'appelle pas.

Limitation connue (client partagé) : ``GitHubClient`` retente les POST sur 5xx /
timeout. Un ``createProjectV2`` qui réussit côté serveur puis timeout côté client
serait rejoué → board en double. Mitigé par ``ensure_project`` (réutilisation par
titre au prochain appel) ; un correctif propre (mutations non-retentées) relève du
client de base et est différé.
"""

from typing import Any, Dict, Optional

from pydantic import BaseModel

from ..base import ToolExecutionError
from ..clients import GitHubClient
from ._helpers import validate_ref

_FIND_PROJECT = """
query($login: String!) {
  repositoryOwner(login: $login) {
    ... on ProjectV2Owner {
      projectsV2(first: 100) { nodes { id number title url } }
    }
  }
}
""".strip()

_OWNER_ID = "query($login: String!) { repositoryOwner(login: $login) { id } }"

_CREATE_PROJECT = """
mutation($ownerId: ID!, $title: String!) {
  createProjectV2(input: {ownerId: $ownerId, title: $title}) {
    projectV2 { id number title url }
  }
}
""".strip()

_ADD_ITEM = """
mutation($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
    item { id }
  }
}
""".strip()


class ProjectInfo(BaseModel):
    id: str
    number: int
    title: str
    url: Optional[str] = None


class ProjectCommands(GitHubClient):
    def _graphql(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._api_post("/graphql", {"query": query, "variables": variables})
        resp = resp or {}
        if resp.get("errors"):
            raise ToolExecutionError(f"GraphQL GitHub a échoué: {resp['errors']}")
        return resp.get("data") or {}

    def _owner_id(self, owner: str) -> str:
        data = self._graphql(_OWNER_ID, {"login": owner})
        node = data.get("repositoryOwner")
        if not node or not node.get("id"):
            raise ToolExecutionError(f"Owner GitHub introuvable: {owner}")
        return node["id"]

    def find_project(self, owner: str, title: str) -> Optional[ProjectInfo]:
        validate_ref(owner, "owner")
        data = self._graphql(_FIND_PROJECT, {"login": owner})
        owner_node = data.get("repositoryOwner")
        if owner_node is None:
            raise ToolExecutionError(f"Owner GitHub introuvable: {owner}")
        nodes = (owner_node.get("projectsV2") or {}).get("nodes") or []
        for node in nodes:
            if node.get("title") == title:
                return ProjectInfo(id=node["id"], number=node["number"], title=node["title"], url=node.get("url"))
        return None

    def ensure_project(self, owner: str, title: str) -> ProjectInfo:
        """Retourne le board existant (même titre) ou le crée. Idempotent."""
        existing = self.find_project(owner, title)
        if existing is not None:
            return existing
        owner_id = self._owner_id(owner)
        data = self._graphql(_CREATE_PROJECT, {"ownerId": owner_id, "title": title})
        node = (data.get("createProjectV2") or {}).get("projectV2")
        if not node or not node.get("id"):
            raise ToolExecutionError(f"Création du board GitHub a échoué (réponse vide) pour '{title}'.")
        return ProjectInfo(id=node["id"], number=node["number"], title=node["title"], url=node.get("url"))

    def issue_node_id(self, owner: str, repo: str, issue_number: int) -> str:
        """node_id GraphQL d'une issue (exposé par l'API REST), requis pour l'ajouter au board."""
        data = self._api_get(f"/repos/{owner}/{repo}/issues/{issue_number}")
        node_id = (data or {}).get("node_id")
        if not node_id:
            raise ToolExecutionError(f"node_id introuvable pour l'issue #{issue_number}")
        return node_id

    def add_issue_to_project(self, project_id: str, issue_node_id: str) -> str:
        """Ajoute une issue (par node_id) au board ; retourne l'ID de l'item créé."""
        data = self._graphql(_ADD_ITEM, {"projectId": project_id, "contentId": issue_node_id})
        item = (data.get("addProjectV2ItemById") or {}).get("item")
        if not item or not item.get("id"):
            raise ToolExecutionError("Ajout de l'issue au board GitHub a échoué (réponse vide).")
        return item["id"]
