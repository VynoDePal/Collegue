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


class PRNotMergeableError(ToolExecutionError):
    """La PR est en CONFLIT avec sa base : le merge est impossible en l'état (#434).

    Erreur **typée** (sous-classe de :class:`ToolExecutionError`) pour que les
    appelants (auto-merge H2, orchestrations) puissent brancher une stratégie de
    réparation — close + redo de la tâche (cf. ``requeue_task_for_redo`` du pilote),
    update-branch… — au lieu de parser un 405 générique.
    """

    def __init__(self, pr_number: int, mergeable_state: str, message: str):
        super().__init__(message)
        self.pr_number = pr_number
        self.mergeable_state = mergeable_state


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
    # #442 : une PR ``closed`` peut l'être par MERGE ou par simple fermeture —
    # la réconciliation GitHub→état a besoin de distinguer les deux.
    merged: bool = False
    # Contexte autoritatif requis par l'auto-merge Phase 5. Les endpoints de liste
    # ne garantissent pas les statistiques, d'où des défauts conservateurs.
    head_sha: Optional[str] = None
    base_sha: Optional[str] = None
    merge_commit_sha: Optional[str] = None
    additions: Optional[int] = None
    deletions: Optional[int] = None
    changed_files: Optional[int] = None
    body: Optional[str] = None


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


class PRFilesSnapshot(BaseModel):
    """Liste paginée des fichiers d'une PR et preuve de complétude."""

    files: List[FileChange] = []
    complete: bool = False
    expected_count: int = 0


class CommitChecks(BaseModel):
    """État agrégé des check-runs et commit statuses d'un SHA."""

    states: List[str] = []
    names: List[str] = []
    complete: bool = False


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
    def list_prs(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        limit: int = 30,
        *,
        base: Optional[str] = None,
    ) -> List[PRInfo]:
        params = {"state": state, "per_page": limit, "sort": "updated", "direction": "desc"}
        if base:
            params["base"] = base
        data = self._api_get(f"/repos/{owner}/{repo}/pulls", params)

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
                head_sha=(pr.get("head") or {}).get("sha"),
                base_sha=(pr.get("base") or {}).get("sha"),
                merge_commit_sha=pr.get("merge_commit_sha"),
                additions=pr.get("additions"),
                deletions=pr.get("deletions"),
                changed_files=pr.get("changed_files"),
                body=pr.get("body"),
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
            # L'endpoint de liste n'a pas ``merged`` (bool) mais ``merged_at``.
            merged=bool(pr.get("merged") or pr.get("merged_at")),
            head_sha=(pr.get("head") or {}).get("sha"),
            base_sha=(pr.get("base") or {}).get("sha"),
            merge_commit_sha=pr.get("merge_commit_sha"),
            additions=pr.get("additions"),
            deletions=pr.get("deletions"),
            changed_files=pr.get("changed_files"),
            body=pr.get("body"),
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
            merged=bool(data.get("merged") or data.get("merged_at")),
            head_sha=(data.get("head") or {}).get("sha"),
            base_sha=(data.get("base") or {}).get("sha"),
            merge_commit_sha=data.get("merge_commit_sha"),
            additions=data.get("additions"),
            deletions=data.get("deletions"),
            changed_files=data.get("changed_files"),
            body=data.get("body"),
        )

    def close_pr(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        expected_head_sha: str,
        expected_head_branch: str,
        expected_base_branch: str,
        body_marker: str,
    ) -> PRInfo:
        """Ferme une PR non mergée après validation stricte de son identité.

        Les quatre gardes sont obligatoires : le cleanup ne doit jamais fermer une
        PR retrouvée seulement par son numéro. Une PR déjà fermée et conforme est
        un succès idempotent ; une PR mergée est toujours refusée. Après le PATCH,
        une nouvelle lecture confirme l'état et rejoue toutes les gardes.
        """
        validate_ref(owner, "owner")
        validate_ref(repo, "repo")
        if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
            raise ToolExecutionError(f"numéro de PR invalide: {pr_number!r}")
        guards = {
            "expected_head_sha": expected_head_sha,
            "expected_head_branch": expected_head_branch,
            "expected_base_branch": expected_base_branch,
            "body_marker": body_marker,
        }
        for name, value in guards.items():
            if not isinstance(value, str) or not value:
                raise ToolExecutionError(f"garde de fermeture PR absente: {name}")

        def verify(info: PRInfo) -> None:
            if info.number != pr_number:
                raise ToolExecutionError(
                    f"GitHub a retourné la PR #{info.number} au lieu de la PR #{pr_number} — refus"
                )
            if info.merged:
                raise ToolExecutionError(f"PR #{pr_number} déjà mergée — refus de la fermer par cleanup")
            if info.head_sha != expected_head_sha:
                raise ToolExecutionError(
                    f"SHA head inattendu pour la PR #{pr_number}: attendu {expected_head_sha}, vu {info.head_sha}"
                )
            if info.head_branch != expected_head_branch:
                raise ToolExecutionError(
                    f"branche head inattendue pour la PR #{pr_number}: "
                    f"attendu {expected_head_branch}, vu {info.head_branch}"
                )
            if info.base_branch != expected_base_branch:
                raise ToolExecutionError(
                    f"branche base inattendue pour la PR #{pr_number}: "
                    f"attendu {expected_base_branch}, vu {info.base_branch}"
                )
            if body_marker not in (info.body or ""):
                raise ToolExecutionError(f"marqueur de cleanup absent du corps de la PR #{pr_number}")
            if info.state not in {"open", "closed"}:
                raise ToolExecutionError(f"état inattendu pour la PR #{pr_number}: {info.state!r}")

        current = self.get_pr(owner, repo, pr_number)
        verify(current)
        if current.state == "closed":
            return current

        self._request_json(
            "PATCH",
            f"/repos/{owner}/{repo}/pulls/{pr_number}",
            json_data={"state": "closed"},
        )
        confirmed = self.get_pr(owner, repo, pr_number)
        verify(confirmed)
        if confirmed.state != "closed":
            raise ToolExecutionError(f"fermeture de la PR #{pr_number} non confirmée par GitHub")
        return confirmed

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

    def get_pr_files_snapshot(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        expected_count: Optional[int] = None,
        page_size: int = 100,
        max_pages: int = 30,
    ) -> PRFilesSnapshot:
        """Récupère *tous* les fichiers d'une PR ou marque le résultat incomplet.

        L'endpoint GitHub est paginé et plafonné à 100 éléments. L'auto-merge ne
        doit jamais juger un diff sur sa seule première page : la complétude est
        prouvée en comparant le nombre reçu à ``changed_files`` de la PR.
        """
        page_size = min(100, max(1, int(page_size)))
        max_pages = max(1, int(max_pages))
        if expected_count is None:
            expected_count = self.get_pr(owner, repo, pr_number).changed_files
        try:
            expected = int(expected_count)
        except (TypeError, ValueError):
            return PRFilesSnapshot(files=[], complete=False, expected_count=0)
        if expected < 0:
            return PRFilesSnapshot(files=[], complete=False, expected_count=expected)
        if expected == 0:
            return PRFilesSnapshot(files=[], complete=True, expected_count=0)

        files: List[FileChange] = []
        for page in range(1, max_pages + 1):
            data = self._api_get(
                f"/repos/{owner}/{repo}/pulls/{pr_number}/files",
                {"per_page": page_size, "page": page},
            )
            if not isinstance(data, list):
                return PRFilesSnapshot(files=files, complete=False, expected_count=expected)
            files.extend(
                FileChange(
                    filename=f["filename"],
                    status=f["status"],
                    additions=f.get("additions", 0),
                    deletions=f.get("deletions", 0),
                    changes=f.get("changes", 0),
                    patch=f.get("patch", "")[:2000] if f.get("patch") else None,
                )
                for f in data
            )
            if len(data) < page_size or len(files) >= expected:
                break
        names = [item.filename for item in files]
        stats_valid = all(item.additions >= 0 and item.deletions >= 0 and item.changes >= 0 for item in files)
        complete = len(files) == expected and len(set(names)) == len(names) and stats_valid
        return PRFilesSnapshot(files=files, complete=complete, expected_count=expected)

    def get_commit_checks(
        self,
        owner: str,
        repo: str,
        head_sha: str,
        *,
        page_size: int = 100,
        max_pages: int = 10,
    ) -> CommitChecks:
        """Agrège check-runs et commit statuses, avec pagination fail-closed.

        Un check non terminé devient ``pending``. Une réponse partielle ou
        malformée rend ``complete=False`` ; l'appelant doit alors refuser le merge.
        """
        page_size = min(100, max(1, int(page_size)))
        max_pages = max(1, int(max_pages))
        states: List[str] = []
        names: List[str] = []

        seen_runs = 0
        total_runs: Optional[int] = None
        checks_complete = False
        for page in range(1, max_pages + 1):
            payload = self._api_get(
                f"/repos/{owner}/{repo}/commits/{head_sha}/check-runs",
                {"filter": "latest", "per_page": page_size, "page": page},
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("check_runs"), list):
                break
            if total_runs is None:
                try:
                    total_runs = int(payload.get("total_count", len(payload["check_runs"])))
                except (TypeError, ValueError):
                    break
                if total_runs < 0:
                    break
            batch = payload["check_runs"]
            for check in batch:
                if not isinstance(check, dict):
                    return CommitChecks(states=states, names=names, complete=False)
                names.append(str(check.get("name") or "check-run"))
                status = str(check.get("status") or "").strip().lower()
                conclusion = str(check.get("conclusion") or "").strip().lower()
                states.append(conclusion if status == "completed" and conclusion else "pending")
            seen_runs += len(batch)
            if seen_runs >= total_runs:
                checks_complete = seen_runs == total_runs
                break
            if len(batch) < page_size:
                break

        statuses_complete = False
        seen_contexts = set()
        for page in range(1, max_pages + 1):
            payload = self._api_get(
                f"/repos/{owner}/{repo}/commits/{head_sha}/statuses",
                {"per_page": page_size, "page": page},
            )
            if not isinstance(payload, list):
                break
            batch = payload
            for status in batch:
                if not isinstance(status, dict):
                    return CommitChecks(states=states, names=names, complete=False)
                context = str(status.get("context") or "commit-status")
                # L'API renvoie du plus récent au plus ancien : ne conserver que
                # le verdict le plus récent de chaque contexte legacy.
                if context in seen_contexts:
                    continue
                seen_contexts.add(context)
                names.append(context)
                states.append(str(status.get("state") or "pending").strip().lower())
            if len(batch) < page_size:
                statuses_complete = True
                break

        return CommitChecks(
            states=states,
            names=names,
            complete=bool(checks_complete and statuses_complete),
        )

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
            head_sha=(resp.get("head") or {}).get("sha"),
            base_sha=(resp.get("base") or {}).get("sha"),
            merge_commit_sha=resp.get("merge_commit_sha"),
            additions=resp.get("additions"),
            deletions=resp.get("deletions"),
            changed_files=resp.get("changed_files"),
            body=resp.get("body"),
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
        expected_base_branch: Optional[str] = None,
        expected_base_sha: Optional[str] = None,
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
        head_sha = (current.get("head") or {}).get("sha")
        if expected_head_sha and head_sha != expected_head_sha:
            raise ToolExecutionError(
                f"la tête de la PR #{pr_number} a bougé (attendu {expected_head_sha}, vu {head_sha}) — "
                "refus (anti-course)"
            )
        current_base = current.get("base") or {}
        if expected_base_branch and current_base.get("ref") != expected_base_branch:
            raise ToolExecutionError(
                f"la base de la PR #{pr_number} a changé (attendu {expected_base_branch}, "
                f"vu {current_base.get('ref')}) — refus"
            )
        if expected_base_sha and current_base.get("sha") != expected_base_sha:
            raise ToolExecutionError(
                f"le SHA de base de la PR #{pr_number} a bougé (attendu {expected_base_sha}, "
                f"vu {current_base.get('sha')}) — refus"
            )
        if current.get("merged"):
            merge_sha = current.get("merge_commit_sha")
            if not merge_sha:
                raise ToolExecutionError(f"PR #{pr_number} déjà mergée sans SHA vérifiable — arrêt fail-closed")
            return MergeResult(merged=True, sha=merge_sha, message="PR déjà mergée", already_merged=True)
        if current.get("state") == "closed":
            raise ToolExecutionError(f"PR #{pr_number} fermée sans merge — refus de merger")
        # #434 : détecter le CONFLIT avant le PUT. GitHub expose ``mergeable``
        # (False = conflit confirmé ; None = calcul en cours → on tente, le PUT
        # tranchera) et ``mergeable_state`` (``dirty`` = conflit). Les autres états
        # (behind/blocked/unstable) ne sont PAS des conflits : on laisse l'API
        # décider (un ``behind`` merge très bien).
        mergeable_state = str(current.get("mergeable_state") or "").lower()
        if current.get("mergeable") is False or mergeable_state == "dirty":
            raise PRNotMergeableError(
                pr_number,
                mergeable_state or "dirty",
                f"PR #{pr_number} en conflit avec sa base (mergeable_state="
                f"{mergeable_state or 'dirty'}) — merge impossible en l'état : "
                "rebase/update-branch ou close+redo de la tâche requis",
            )

        body: dict = {"merge_method": method}
        if commit_title:
            body["commit_title"] = commit_title
        if commit_message:
            body["commit_message"] = commit_message
        if expected_head_sha:
            body["sha"] = expected_head_sha

        try:
            resp = self._api_put(f"/repos/{owner}/{repo}/pulls/{pr_number}/merge", body) or {}
        except PRNotMergeableError:
            raise
        except ToolExecutionError as exc:
            # ``mergeable`` peut être None/périmé au GET : GitHub répond alors
            # 405 « Pull Request is not mergeable » au PUT. On re-type l'erreur
            # pour offrir le même canal de réparation que la détection amont.
            text = str(exc)
            if "405" in text or "not mergeable" in text.lower():
                raise PRNotMergeableError(
                    pr_number,
                    "dirty",
                    f"PR #{pr_number} refusée au merge par GitHub (405 non mergeable) — "
                    "conflit probable avec la base : rebase/update-branch ou close+redo requis",
                ) from exc
            raise
        # Un merge réussi renvoie toujours ``{"merged": true, "sha": ...}`` ; l'absence
        # de confirmation = échec (défaut False), jamais un succès supposé (fail-closed).
        merged = bool(resp.get("merged", False))
        merge_sha = resp.get("sha")
        if merged and not merge_sha:
            raise ToolExecutionError(f"merge de la PR #{pr_number} confirmé sans SHA — arrêt fail-closed")
        return MergeResult(merged=merged, sha=merge_sha, message=resp.get("message", ""))
