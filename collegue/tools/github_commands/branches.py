"""
Branch Commands for GitHub Operations.

Handles branch listing, creation, and commit operations.
"""

import re
import time
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

from pydantic import BaseModel

from ..base import ToolExecutionError
from ..clients import GitHubClient
from ._helpers import validate_ref

# Branches refusées par défaut à la suppression (garde-fou contre la perte de la base).
PROTECTED_BRANCHES = ("main", "master")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_DELETE_CONFIRM_BACKOFF_SECONDS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)


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


class BranchDetails(BaseModel):
    """Branche telle que décrite par ``GET /branches/{b}`` (``protected`` : protection classique OU ruleset)."""

    name: str
    commit_sha: str
    protected: bool = False


class RequiredCheckSpec(BaseModel):
    """Un check exigé par une protection ; ``app_id`` absent = n'importe quelle source."""

    context: str
    app_id: Optional[int] = None


class ClassicProtection(BaseModel):
    """Protection de branche classique, réduite à ce qui conditionne une fusion automatique."""

    has_required_status_checks: bool = False
    strict: bool = False
    required_checks: List[RequiredCheckSpec] = []
    enforce_admins: bool = False


class BranchRule(BaseModel):
    """Règle d'un ruleset applicable à une branche (``GET /rules/branches/{b}``)."""

    type: str
    ruleset_id: Optional[int] = None
    ruleset_source_type: Optional[str] = None
    parameters: Dict[str, Any] = {}


class RulesetInfo(BaseModel):
    """Ruleset : mode d'application et possibilité de contournement PAR L'ACTEUR AUTHENTIFIÉ."""

    id: int
    name: str = ""
    target: str = ""
    enforcement: str = ""
    # "always" | "pull_requests_only" | "exempt" | "never" | None (champ absent : inconnu)
    current_user_can_bypass: Optional[str] = None


class CompareInfo(BaseModel):
    """Relation d'ascendance entre deux commits (``GET /compare/{base}...{head}``)."""

    status: str
    ahead_by: int = 0
    behind_by: int = 0
    merge_base_sha: Optional[str] = None


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

    # ── préconditions serveur d'une fusion (protections, rulesets, acteur) ────────────────────
    # Lectures SEULES. Toute erreur autre que « 404 attendu » se propage : une vérification
    # inaccessible n'est jamais une absence de protection (fail-closed chez l'appelant).

    def get_branch(self, owner: str, repo: str, branch: str) -> BranchDetails:
        validate_ref(owner, "owner")
        validate_ref(repo, "repo")
        self._validate_branch_name(branch)
        data = self._api_get(f"/repos/{owner}/{repo}/branches/{quote(branch, safe='/')}")
        if not isinstance(data, dict):
            raise ToolExecutionError(f"branche {branch!r}: réponse malformée")
        sha = self._validate_full_sha((data.get("commit") or {}).get("sha"), "SHA de la branche")
        return BranchDetails(
            name=str(data.get("name") or branch), commit_sha=sha, protected=bool(data.get("protected"))
        )

    def get_branch_protection(self, owner: str, repo: str, branch: str) -> Optional[ClassicProtection]:
        """Protection classique de ``branch``, ou ``None`` si GitHub répond 404.

        Un 404 signifie « branche non protégée » OU « protection non visible par ce jeton » : l'appelant ne
        doit pas en déduire qu'aucune protection n'existe, seulement qu'il n'en lit aucune. 401/403/5xx se
        propagent.
        """
        validate_ref(owner, "owner")
        validate_ref(repo, "repo")
        self._validate_branch_name(branch)
        try:
            data = self._api_get(f"/repos/{owner}/{repo}/branches/{quote(branch, safe='/')}/protection")
        except ToolExecutionError as exc:
            if getattr(exc, "status_code", 0) == 404:
                return None
            raise
        if not isinstance(data, dict):
            raise ToolExecutionError("protection de branche: réponse malformée")
        status_checks = data.get("required_status_checks")
        if status_checks is not None and not isinstance(status_checks, dict):
            raise ToolExecutionError("protection de branche: required_status_checks malformé")
        specs: List[RequiredCheckSpec] = []
        if isinstance(status_checks, dict):
            checks = status_checks.get("checks")
            if isinstance(checks, list) and checks:
                for item in checks:
                    if not isinstance(item, dict) or not item.get("context"):
                        raise ToolExecutionError("protection de branche: check requis malformé")
                    app_id = item.get("app_id")
                    specs.append(
                        RequiredCheckSpec(
                            context=str(item["context"]),
                            app_id=app_id
                            if isinstance(app_id, int) and not isinstance(app_id, bool) and app_id >= 0
                            else None,
                        )
                    )
            else:  # ancien format : contextes sans application attendue
                for context in status_checks.get("contexts") or []:
                    specs.append(RequiredCheckSpec(context=str(context), app_id=None))
        enforce = data.get("enforce_admins")
        return ClassicProtection(
            has_required_status_checks=isinstance(status_checks, dict),
            strict=bool(status_checks.get("strict")) if isinstance(status_checks, dict) else False,
            required_checks=specs,
            enforce_admins=bool(enforce.get("enabled")) if isinstance(enforce, dict) else False,
        )

    def get_branch_rules(self, owner: str, repo: str, branch: str, *, max_pages: int = 20) -> List[BranchRule]:
        """Règles de rulesets ACTIFS applicables à ``branch``, toutes pages lues (échec si tronqué)."""
        validate_ref(owner, "owner")
        validate_ref(repo, "repo")
        self._validate_branch_name(branch)
        rules: List[BranchRule] = []
        page_size = 100
        for page in range(1, max_pages + 1):
            data = self._api_get(
                f"/repos/{owner}/{repo}/rules/branches/{quote(branch, safe='/')}",
                {"per_page": page_size, "page": page},
            )
            if not isinstance(data, list):
                raise ToolExecutionError("rules/branches: réponse malformée")
            for item in data:
                if not isinstance(item, dict) or not item.get("type"):
                    raise ToolExecutionError("rules/branches: règle malformée")
                params = item.get("parameters")
                rules.append(
                    BranchRule(
                        type=str(item["type"]),
                        ruleset_id=item.get("ruleset_id") if isinstance(item.get("ruleset_id"), int) else None,
                        ruleset_source_type=item.get("ruleset_source_type"),
                        parameters=params if isinstance(params, dict) else {},
                    )
                )
            if len(data) < page_size:
                return rules
        raise ToolExecutionError(f"rules/branches: plus de {max_pages} pages — liste potentiellement tronquée")

    def get_ruleset(self, owner: str, repo: str, ruleset_id: int) -> RulesetInfo:
        validate_ref(owner, "owner")
        validate_ref(repo, "repo")
        if not isinstance(ruleset_id, int) or isinstance(ruleset_id, bool) or ruleset_id <= 0:
            raise ToolExecutionError(f"identifiant de ruleset invalide: {ruleset_id!r}")
        data = self._api_get(f"/repos/{owner}/{repo}/rulesets/{ruleset_id}")
        if not isinstance(data, dict) or data.get("id") != ruleset_id:
            raise ToolExecutionError(f"ruleset {ruleset_id}: réponse malformée")
        bypass = data.get("current_user_can_bypass")
        return RulesetInfo(
            id=ruleset_id,
            name=str(data.get("name") or ""),
            target=str(data.get("target") or ""),
            enforcement=str(data.get("enforcement") or "").strip().lower(),
            current_user_can_bypass=str(bypass).strip().lower() if isinstance(bypass, str) else None,
        )

    def get_authenticated_login(self) -> str:
        """Login de l'acteur du jeton. Un jeton d'application (403) lève : l'acteur n'est alors pas établi."""
        data = self._api_get("/user")
        login = data.get("login") if isinstance(data, dict) else None
        if not isinstance(login, str) or not login.strip():
            raise ToolExecutionError("identité de l'acteur GitHub indisponible")
        return login.strip()

    def get_collaborator_role(self, owner: str, repo: str, login: str) -> str:
        """Rôle effectif de ``login`` sur le dépôt (``admin``, ``maintain``, ``write``, ``triage``, ``read``…)."""
        validate_ref(owner, "owner")
        validate_ref(repo, "repo")
        validate_ref(login, "login")
        data = self._api_get(f"/repos/{owner}/{repo}/collaborators/{login}/permission")
        role = (data or {}).get("role_name") or (data or {}).get("permission") if isinstance(data, dict) else None
        if not isinstance(role, str) or not role.strip():
            raise ToolExecutionError(f"rôle de {login} sur {owner}/{repo} indisponible")
        return role.strip().lower()

    def compare_commits(self, owner: str, repo: str, base_sha: str, head_sha: str) -> CompareInfo:
        """Ascendance de ``head_sha`` par rapport à ``base_sha`` (``ahead`` = contient la base)."""
        validate_ref(owner, "owner")
        validate_ref(repo, "repo")
        base_sha = self._validate_full_sha(base_sha, "SHA de base")
        head_sha = self._validate_full_sha(head_sha, "SHA de tête")
        data = self._api_get(f"/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}")
        if not isinstance(data, dict) or not isinstance(data.get("status"), str):
            raise ToolExecutionError("comparaison de commits malformée")
        merge_base = (data.get("merge_base_commit") or {}).get("sha")
        return CompareInfo(
            status=data["status"].strip().lower(),
            ahead_by=int(data.get("ahead_by") or 0),
            behind_by=int(data.get("behind_by") or 0),
            merge_base_sha=merge_base if isinstance(merge_base, str) and _SHA_RE.fullmatch(merge_base) else None,
        )

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

    def get_git_tree(self, owner: str, repo: str, tree_sha: str, recursive: bool = False) -> Dict[str, Any]:
        """Arbre Git RÉEL (``GET /git/trees/{sha}``), validé : ``{"tree": [...], "truncated": bool}``. Une réponse malformée
        lève ; la troncature est RAPPORTÉE (l'appelant décide : les gardes de fusion et de socle la traitent en refus)."""
        validate_ref(owner, "owner")
        validate_ref(repo, "repo")
        tree_sha = self._validate_full_sha(tree_sha, "SHA du tree")
        data = self._api_get(f"/repos/{owner}/{repo}/git/trees/{tree_sha}", {"recursive": "1"} if recursive else None)
        if not isinstance(data, dict) or not isinstance(data.get("tree"), list):
            raise ToolExecutionError("arbre Git malformé")
        for entry in data["tree"]:
            if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in ("path", "type", "sha")):
                raise ToolExecutionError("entrée d'arbre Git malformée")
        return {"tree": data["tree"], "truncated": bool(data.get("truncated"))}

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
        delete_error: Optional[ToolExecutionError] = None
        try:
            self._request_json("DELETE", f"/repos/{owner}/{repo}/git/refs/heads/{branch}")
        except ToolExecutionError as exc:
            # Une réponse DELETE peut être perdue après application côté GitHub.
            # Ne jamais réémettre la mutation : la même confirmation bornée
            # tranche sur l'état autoritatif de la ref.
            delete_error = exc

        # Un DELETE 2xx ne suffit pas comme preuve pour une opération destructive.
        # GitHub peut toutefois servir brièvement l'ancien SHA après suppression :
        # lui seul est retryable. Un autre SHA prouve une course et bloque aussitôt.
        for attempt in range(len(_DELETE_CONFIRM_BACKOFF_SECONDS) + 1):
            remaining_sha = self._branch_sha_or_none(owner, repo, branch)
            if remaining_sha is None:
                return True
            if remaining_sha != current_sha:
                raise ToolExecutionError(
                    f"suppression de la branche {branch!r} ambiguë: tête déplacée de {current_sha} vers {remaining_sha}"
                )
            if attempt < len(_DELETE_CONFIRM_BACKOFF_SECONDS):
                time.sleep(_DELETE_CONFIRM_BACKOFF_SECONDS[attempt])

        if delete_error is not None:
            raise delete_error
        raise ToolExecutionError(f"suppression de la branche {branch!r} non confirmée: tête distante {current_sha}")
