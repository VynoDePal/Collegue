"""
Issue Commands for GitHub Operations.

Handles issue listing, details, and creation.
"""

from typing import Iterable, List, Optional

from pydantic import BaseModel

from ..base import ToolExecutionError
from ..clients import GitHubClient
from ._helpers import validate_ref


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


class IssueCommands(GitHubClient):
    def list_issues(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        limit: int = 30,
        *,
        labels: Optional[str | Iterable[str]] = None,
    ) -> List[IssueInfo]:
        params = {"state": state, "per_page": limit, "sort": "updated", "direction": "desc"}
        if isinstance(labels, str):
            labels_filter = labels.strip()
        elif labels is None:
            labels_filter = ""
        else:
            try:
                values = tuple(labels)
            except TypeError as exc:
                raise ToolExecutionError("filtre labels invalide") from exc
            if any(not isinstance(label, str) or not label.strip() for label in values):
                raise ToolExecutionError("filtre labels invalide")
            labels_filter = ",".join(label.strip() for label in values)
        if labels_filter:
            params["labels"] = labels_filter
        data = self._api_get(f"/repos/{owner}/{repo}/issues", params)

        issues = [i for i in data if "pull_request" not in i]

        return [
            IssueInfo(
                number=i["number"],
                title=i["title"],
                state=i["state"],
                html_url=i["html_url"],
                user=i["user"]["login"],
                labels=[l["name"] for l in i.get("labels", [])],
                created_at=i["created_at"],
                updated_at=i["updated_at"],
                body=i.get("body", "")[:500] if i.get("body") else None,
            )
            for i in issues[:limit]
        ]

    def get_issue(self, owner: str, repo: str, issue_number: int) -> IssueInfo:
        data = self._api_get(f"/repos/{owner}/{repo}/issues/{issue_number}")
        return IssueInfo(
            number=data["number"],
            title=data["title"],
            state=data["state"],
            html_url=data["html_url"],
            user=data["user"]["login"],
            labels=[l["name"] for l in data.get("labels", [])],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            # Le getter individuel sert aux gardes de cleanup/reprise : le
            # marqueur de corrélation est en fin de corps et ne doit jamais être
            # tronqué (GitHub borne déjà la taille d'un body d'issue).
            body=data.get("body") or None,
        )

    def create_issue(self, owner: str, repo: str, title: str, body: Optional[str] = None) -> IssueInfo:
        data = {"title": title, "body": body or ""}
        resp = self._api_post(f"/repos/{owner}/{repo}/issues", data)
        return IssueInfo(
            number=resp["number"],
            title=resp["title"],
            state=resp["state"],
            html_url=resp["html_url"],
            user=resp["user"]["login"],
            labels=[l["name"] for l in resp.get("labels", [])],
            created_at=resp["created_at"],
            updated_at=resp["updated_at"],
            body=resp.get("body"),
        )

    def create_issue_with_metadata(
        self,
        owner: str,
        repo: str,
        title: str,
        *,
        body: Optional[str] = None,
        labels: Iterable[str] = (),
        milestone_number: Optional[int] = None,
    ) -> IssueInfo:
        """Crée l'issue et ses métadonnées de corrélation dans le même POST.

        Cela supprime la fenêtre ``create_issue`` puis ``add_labels`` pendant
        laquelle une perte de réponse laisserait une issue distante difficile à
        retrouver lors du cleanup/retry.
        """
        validate_ref(owner, "owner")
        validate_ref(repo, "repo")
        try:
            normalized_labels = [label.strip() for label in labels]
        except (TypeError, AttributeError) as exc:
            raise ToolExecutionError("labels de création d'issue invalides") from exc
        if any(not label for label in normalized_labels):
            raise ToolExecutionError("labels de création d'issue invalides")
        if milestone_number is not None and (
            not isinstance(milestone_number, int) or isinstance(milestone_number, bool) or milestone_number <= 0
        ):
            raise ToolExecutionError(f"numéro de milestone invalide: {milestone_number!r}")
        data: dict = {"title": title, "body": body or "", "labels": normalized_labels}
        if milestone_number is not None:
            data["milestone"] = milestone_number
        resp = self._api_post(f"/repos/{owner}/{repo}/issues", data)
        return IssueInfo(
            number=resp["number"],
            title=resp["title"],
            state=resp["state"],
            html_url=resp["html_url"],
            user=resp["user"]["login"],
            labels=[label["name"] for label in resp.get("labels", [])],
            created_at=resp["created_at"],
            updated_at=resp["updated_at"],
            body=resp.get("body"),
        )

    def close_issue(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        expected_labels: Iterable[str],
        body_marker: str,
    ) -> IssueInfo:
        """Ferme une issue non-PR après validation de ses labels et marqueur.

        ``expected_labels`` est un sous-ensemble obligatoire (comparaison
        insensible à la casse) : des labels humains supplémentaires ne rendent pas
        le cleanup inopérant. Une issue conforme déjà fermée est un succès
        idempotent. Le endpoint ``issues`` expose aussi les PR ; elles sont donc
        explicitement refusées avant toute mutation.
        """
        validate_ref(owner, "owner")
        validate_ref(repo, "repo")
        if not isinstance(issue_number, int) or isinstance(issue_number, bool) or issue_number <= 0:
            raise ToolExecutionError(f"numéro d'issue invalide: {issue_number!r}")
        try:
            labels_guard = (expected_labels,) if isinstance(expected_labels, str) else tuple(expected_labels)
        except TypeError as exc:
            raise ToolExecutionError("expected_labels invalide") from exc
        if not labels_guard:
            raise ToolExecutionError("expected_labels doit contenir au moins un label de cleanup")
        if any(not isinstance(label, str) or not label.strip() for label in labels_guard):
            raise ToolExecutionError("expected_labels contient un label invalide")
        if not isinstance(body_marker, str) or not body_marker:
            raise ToolExecutionError("garde de fermeture issue absente: body_marker")
        endpoint = f"/repos/{owner}/{repo}/issues/{issue_number}"

        def read_and_verify() -> tuple[dict, IssueInfo]:
            payload = self._api_get(endpoint)
            if not isinstance(payload, dict) or payload.get("number") != issue_number:
                raise ToolExecutionError(f"identité de l'issue #{issue_number} non confirmée par GitHub")
            if "pull_request" in payload:
                raise ToolExecutionError(f"#{issue_number} est une pull request — refus de close_issue")
            state = payload.get("state")
            if state not in {"open", "closed"}:
                raise ToolExecutionError(f"état inattendu pour l'issue #{issue_number}: {state!r}")
            actual_labels = {
                str(label.get("name") or "").strip().casefold()
                for label in payload.get("labels", [])
                if isinstance(label, dict)
            }
            required_labels = {label.strip().casefold() for label in labels_guard}
            missing = required_labels - actual_labels
            if missing:
                raise ToolExecutionError(
                    f"labels de cleanup absents de l'issue #{issue_number}: {', '.join(sorted(missing))}"
                )
            if body_marker not in (payload.get("body") or ""):
                raise ToolExecutionError(f"marqueur de cleanup absent du corps de l'issue #{issue_number}")
            return payload, IssueInfo(
                number=payload["number"],
                title=payload["title"],
                state=state,
                html_url=payload["html_url"],
                user=payload["user"]["login"],
                labels=[label["name"] for label in payload.get("labels", [])],
                created_at=payload["created_at"],
                updated_at=payload["updated_at"],
                body=payload.get("body"),
            )

        _, current = read_and_verify()
        if current.state == "closed":
            return current
        self._request_json("PATCH", endpoint, json_data={"state": "closed"})
        _, confirmed = read_and_verify()
        if confirmed.state != "closed":
            raise ToolExecutionError(f"fermeture de l'issue #{issue_number} non confirmée par GitHub")
        return confirmed
