"""Faux serveur GitHub REST (en mémoire) pour exercer les VRAIS clients ``PRCommands`` / ``BranchCommands``.

Les clients réels sont branchés sur ce serveur en remplaçant leur frontière HTTP (``_api_get`` / ``_api_put``) :
pagination, parsing, mapping d'erreurs, corps des requêtes ET la politique de fusion sont donc ceux de la
production. Le serveur reproduit les sémantiques GitHub qui comptent pour la course sur la base :

- ``PUT /pulls/N/merge`` : refuse (409) si ``sha`` ne correspond pas à la tête ; applique les checks requis et la
  règle « branche à jour » (strict) des protections classiques / rulesets APPLICABLES à l'acteur (un acteur
  pouvant contourner n'est pas contraint, comme sur GitHub) ; produit le commit de fusion (squash/merge).
- ``before_merge`` / ``after_merge`` : crochets déclenchés AU MOMENT de l'appel de fusion (course réelle : la
  base bouge entre la dernière lecture du client et l'évaluation serveur ; crash après succès distant).
- ``calls`` : journal exact des appels émis (méthode, chemin, paramètres/corps).
- ``fail(...)`` : injection d'erreurs HTTP (403, 404, 500…) par route.

Aucun réseau, aucune écriture sur un dépôt réel.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Callable, Dict, List, Optional

from collegue.tools.base import ToolExecutionError
from collegue.tools.github_commands import BranchCommands, PRCommands

OWNER, REPO = "fixture", "fixture"
RUFF, PYTEST311, PYTEST312, AUDIT, DOCKER = (
    "Ruff",
    "Pytest (Python 3.11)",
    "Pytest (Python 3.12)",
    "Dependency audit",
    "Docker build",
)
FIVE_CHECKS = (RUFF, PYTEST311, PYTEST312, AUDIT, DOCKER)
GITHUB_ACTIONS_APP = 15368


def sha_of(label: str) -> str:
    return hashlib.sha1(label.encode()).hexdigest()


_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def tree_sha_of(tree: str) -> str:
    """SHA d'un arbre : un identifiant Git COMPLET (40 hex, ex. vrai arbre d'un dépôt de test) est conservé tel quel ;
    seul un libellé symbolique (``"tree-task-1"``) est haché. Permet à un pont vers de vrais dépôts Git de fournir de
    vrais SHA sans qu'ils soient re-hachés."""
    return tree if _FULL_SHA.fullmatch(tree) else sha_of(tree)


class HttpError(ToolExecutionError):
    pass


class FakeGitHubServer:
    def __init__(self, *, base_branch: str = "main"):
        self.base_branch = base_branch
        self.commits: Dict[str, Dict[str, Any]] = {}
        self.branches: Dict[str, str] = {}
        self.prs: Dict[int, Dict[str, Any]] = {}
        self.check_runs: Dict[str, List[Dict[str, Any]]] = {}
        self.statuses: Dict[str, List[Dict[str, Any]]] = {}
        self.actions_jobs: Dict[
            int, Dict[str, Any]
        ] = {}  # jobs Actions RÉELS (un check publié par l'API des checks n'en est pas un)
        self.actions_runs: Dict[int, Dict[str, Any]] = {}
        self._check_run_id = 7_000_000
        self.calls: List[tuple] = []
        self._counter = 0
        self.failures: List[tuple] = []
        # configuration serveur
        self.actor_login: Optional[str] = "collegue-bot"
        self.actor_role: str = "write"
        self.classic: Optional[Dict[str, Any]] = None  # corps de GET protection, None -> 404
        self.branch_protected_flag: Optional[bool] = None
        self.rules: List[Dict[str, Any]] = []
        self.rulesets: Dict[int, Dict[str, Any]] = {}
        self.page_cap = 100
        self.get_hooks: List[list] = []  # [regex, callable(server), times]
        self.before_merge: Optional[Callable[["FakeGitHubServer"], None]] = None
        self.after_merge: Optional[Callable[["FakeGitHubServer", Dict[str, Any]], None]] = None
        self.merge_block_message: Optional[str] = None
        self.ignore_strict = False  # serveur qui NE fait PAS respecter la règle « à jour » (précondition trompée)
        # dépôt de départ : un commit racine sur la base
        root = self.commit([], tree="tree-root", message="root")
        self.branches[base_branch] = root

    # ── construction du dépôt ───────────────────────────────────────────────────
    def commit(self, parents: List[str], *, tree: str, message: str = "c") -> str:
        self._counter += 1
        sha = sha_of(f"{self._counter}:{tree}:{','.join(parents)}:{message}")
        self.commits[sha] = {"sha": sha, "parents": list(parents), "tree": tree, "message": message}
        return sha

    @property
    def base_tip(self) -> str:
        return self.branches[self.base_branch]

    def advance_base(self, *, tree: str, message: str = "other work") -> str:
        """Un autre contributeur fait avancer la base."""
        sha = self.commit([self.base_tip], tree=tree, message=message)
        self.branches[self.base_branch] = sha
        return sha

    def open_pr(
        self,
        number: int,
        *,
        head_ref: str,
        tree: str,
        files: Optional[List[Dict[str, Any]]] = None,
        parent: Optional[str] = None,
        checks: Optional[Dict[str, str]] = "all-green",  # type: ignore[assignment]
        app_id: int = GITHUB_ACTIONS_APP,
    ) -> Dict[str, Any]:
        parent = parent or self.base_tip
        head = self.commit([parent], tree=tree, message=f"work {head_ref}")
        self.branches[head_ref] = head
        pr = {
            "number": number,
            "title": f"PR {number}",
            "state": "open",
            "html_url": f"https://github.test/{OWNER}/{REPO}/pull/{number}",
            "user": {"login": "collegue-bot"},
            "base": {"ref": self.base_branch, "sha": parent},
            "head": {"ref": head_ref, "sha": head},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "labels": [],
            "draft": False,
            "merged": False,
            "merge_commit_sha": None,
            "additions": 3,
            "deletions": 0,
            "changed_files": len(files or [{}]),
            "body": "",
            "mergeable": True,
            "mergeable_state": "clean",
            "files": files or [{"filename": "src/app.py", "status": "added", "additions": 3, "deletions": 0}],
        }
        self.prs[number] = pr
        if checks == "all-green":
            self.set_checks(head, {name: "success" for name in FIVE_CHECKS}, app_id=app_id)
        elif checks:
            self.set_checks(head, checks, app_id=app_id)
        return pr

    def set_checks(self, sha: str, states: Dict[str, str], *, app_id: Optional[int] = GITHUB_ACTIONS_APP) -> None:
        runs = []
        for name, state in states.items():
            self._check_run_id += 1
            if state in {"pending", "queued", "in_progress"}:
                runs.append(
                    {
                        "id": self._check_run_id,
                        "name": name,
                        "status": state if state != "pending" else "in_progress",
                        "conclusion": None,
                        "app": {"id": app_id},
                    }
                )
            else:
                runs.append(
                    {
                        "id": self._check_run_id,
                        "name": name,
                        "status": "completed",
                        "conclusion": state,
                        "app": {"id": app_id},
                    }
                )
        self.check_runs[sha] = runs

    def register_actions_job(
        self,
        check_run_id: int,
        *,
        head_sha: str,
        name: str,
        run_id: int = 900,
        workflow_path: str = ".github/workflows/fixture-tests.yml",
        event: str = "pull_request",
        conclusion: Optional[str] = "success",
        run_head_sha: Optional[str] = None,
        repository: Optional[str] = None,
        head_repository: Optional[str] = None,
        run_conclusion: Optional[str] = "success",
    ) -> None:
        """Enregistre le job Actions et l'exécution qui correspondent à un check-run (provenance d'un VRAI job)."""
        full = f"{OWNER}/{REPO}"
        self.actions_jobs[check_run_id] = {
            "id": check_run_id,
            "run_id": run_id,
            "name": name,
            "head_sha": head_sha,
            "status": "completed" if conclusion else "in_progress",
            "conclusion": conclusion,
        }
        self.actions_runs[run_id] = {
            "id": run_id,
            "path": workflow_path,
            "event": event,
            "head_sha": run_head_sha or head_sha,
            "status": "completed" if run_conclusion else "in_progress",
            "conclusion": run_conclusion,
            "repository": {"full_name": repository or full},
            "head_repository": {"full_name": head_repository or full},
        }

    def push_to_pr_head(self, number: int, *, tree: str) -> str:
        pr = self.prs[number]
        new = self.commit([pr["head"]["sha"]], tree=tree, message="late push")
        pr["head"]["sha"] = new
        self.branches[pr["head"]["ref"]] = new
        return new

    # ── protections ─────────────────────────────────────────────────────────────
    def protect_classic(
        self,
        *,
        checks=FIVE_CHECKS,
        strict: bool = True,
        enforce_admins: bool = True,
        app_id: Optional[int] = GITHUB_ACTIONS_APP,
        legacy_contexts_only: bool = False,
    ) -> None:
        rs: Dict[str, Any] = {"strict": strict, "contexts": list(checks)}
        if not legacy_contexts_only:
            rs["checks"] = [{"context": c, "app_id": app_id if app_id is not None else -1} for c in checks]
        self.classic = {"required_status_checks": rs, "enforce_admins": {"enabled": enforce_admins}}
        self.branch_protected_flag = True

    def add_ruleset(
        self,
        ruleset_id: int,
        *,
        checks=FIVE_CHECKS,
        strict: bool = True,
        enforcement: str = "active",
        can_bypass: Optional[str] = "never",
        integration_id: Optional[int] = GITHUB_ACTIONS_APP,
        listed: Optional[bool] = None,
        extra_rule_types: tuple = (),
    ) -> None:
        params = {
            "strict_required_status_checks_policy": strict,
            "do_not_enforce_on_create": False,
            "required_status_checks": [
                ({"context": c, "integration_id": integration_id} if integration_id is not None else {"context": c})
                for c in checks
            ],
        }
        detail: Dict[str, Any] = {
            "id": ruleset_id,
            "name": f"rs{ruleset_id}",
            "target": "branch",
            "enforcement": enforcement,
        }
        if can_bypass is not None:
            detail["current_user_can_bypass"] = can_bypass
        self.rulesets[ruleset_id] = detail
        if listed is None:
            listed = enforcement == "active"
        if listed:
            self.rules.append(
                {
                    "type": "required_status_checks",
                    "ruleset_id": ruleset_id,
                    "ruleset_source_type": "Repository",
                    "parameters": params,
                }
            )
            for extra in extra_rule_types:
                self.rules.append(
                    {"type": extra, "ruleset_id": ruleset_id, "ruleset_source_type": "Repository", "parameters": {}}
                )

    def on_get(self, pattern: str, fn: Callable[["FakeGitHubServer"], None], *, nth: int = 1) -> None:
        """Exécute ``fn`` AVANT de répondre au ``nth``-ième GET dont le chemin correspond (course entre deux lectures)."""
        self.get_hooks.append([re.compile(pattern), fn, nth])

    def fail(self, method: str, pattern: str, status: int = 500, *, times: Optional[int] = None) -> None:
        self.failures.append([method, re.compile(pattern), status, times])

    # ── fusion côté serveur (sémantique GitHub) ───────────────────────────────────
    def _ancestors(self, sha: str) -> set:
        seen, todo = set(), [sha]
        while todo:
            cur = todo.pop()
            if cur in seen:
                continue
            seen.add(cur)
            todo.extend(self.commits[cur]["parents"])
        return seen

    def _classic_binds_actor(self) -> bool:
        if not self.classic:
            return False
        return not (self.actor_role == "admin" and not self.classic["enforce_admins"]["enabled"])

    def _enforced_strict(self) -> bool:
        if self.classic and self.classic["required_status_checks"].get("strict") and self._classic_binds_actor():
            return True
        for rule in self.rules:
            if rule["type"] != "required_status_checks":
                continue
            detail = self.rulesets.get(rule["ruleset_id"], {})
            if detail.get("enforcement") != "active":
                continue
            if detail.get("current_user_can_bypass", "never") in {"always", "pull_requests_only", "exempt"}:
                continue
            if rule["parameters"].get("strict_required_status_checks_policy"):
                return True
        return False

    def _enforced_required_checks(self) -> List[str]:
        names: List[str] = []
        if self.classic and self._classic_binds_actor():
            names += list(self.classic["required_status_checks"].get("contexts", []))
        for rule in self.rules:
            detail = self.rulesets.get(rule["ruleset_id"], {})
            if (
                rule["type"] == "required_status_checks"
                and detail.get("enforcement") == "active"
                and detail.get("current_user_can_bypass", "never") == "never"
            ):
                names += [c["context"] for c in rule["parameters"]["required_status_checks"]]
        return names

    def _merge(self, number: int, body: Dict[str, Any]) -> Dict[str, Any]:
        if self.before_merge is not None:
            hook, self.before_merge = self.before_merge, None
            hook(self)
        pr = self.prs[number]
        if self.merge_block_message:
            raise HttpError(self.merge_block_message, status_code=405)
        if pr["state"] != "open":
            raise HttpError("Pull Request is not mergeable", status_code=405)
        head = pr["head"]["sha"]
        if body.get("sha") and body["sha"] != head:
            raise HttpError("Head branch was modified. Review and try the merge again.", status_code=409)
        tip = self.base_tip
        for name in self._enforced_required_checks():
            runs = [r for r in self.check_runs.get(head, []) if r["name"] == name]
            if not runs or any(r["conclusion"] != "success" for r in runs):
                raise HttpError(f"Required status check '{name}' is expected.", status_code=405)
        if self._enforced_strict() and not self.ignore_strict and tip not in self._ancestors(head):
            raise HttpError("Head branch is not up to date with the base branch.", status_code=405)
        head_tree = self.commits[head]["tree"]
        if tip in self._ancestors(head):
            tree = head_tree
        else:  # fusion non protégée : contenu différent de celui testé
            tree = f"merged({self.commits[tip]['tree']}+{head_tree})"
        method = body.get("merge_method", "merge")
        parents = [tip] if method == "squash" else [tip, head]
        merge_sha = self.commit(parents, tree=tree, message=f"merge PR {number}")
        self.branches[self.base_branch] = merge_sha
        pr.update(state="closed", merged=True, merge_commit_sha=merge_sha)
        result = {"merged": True, "sha": merge_sha, "message": "Pull Request successfully merged"}
        if self.after_merge is not None:
            hook, self.after_merge = self.after_merge, None
            hook(self, result)
        return result

    # ── routage REST ──────────────────────────────────────────────────────────────
    def _maybe_fail(self, method: str, path: str) -> None:
        for entry in list(self.failures):
            m, pattern, status, times = entry
            if m == method and pattern.search(path):
                if times is not None:
                    entry[3] = times - 1
                    if entry[3] <= 0:
                        self.failures.remove(entry)
                raise HttpError(f"HTTP {status} {path}", status_code=status)

    def api_get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        params = dict(params or {})
        self.calls.append(("GET", endpoint, params))
        self._maybe_fail("GET", endpoint)
        for hook in list(self.get_hooks):
            if hook[0].search(endpoint):
                hook[2] -= 1
                if hook[2] <= 0:
                    self.get_hooks.remove(hook)
                    hook[1](self)
        prefix = f"/repos/{OWNER}/{REPO}"
        if endpoint == "/user":
            if self.actor_login is None:
                raise HttpError("Resource not accessible by integration", status_code=403)
            return {"login": self.actor_login, "type": "User"}
        if endpoint.startswith(f"{prefix}/collaborators/") and endpoint.endswith("/permission"):
            return {"permission": "admin" if self.actor_role == "admin" else "write", "role_name": self.actor_role}
        m = re.fullmatch(rf"{prefix}/pulls/(\d+)", endpoint)
        if m:
            pr = self.prs.get(int(m.group(1)))
            if pr is None:
                raise HttpError("Not Found", status_code=404)
            return {k: v for k, v in pr.items() if k != "files"}
        if endpoint == f"{prefix}/pulls":
            head = params.get("head", "").split(":", 1)[-1] if params.get("head") else None
            matches = [
                p
                for p in self.prs.values()
                if (
                    head is None or p["head"]["ref"] == head
                )  # sans filtre ``head`` : liste des PR (filtre ``base`` facultatif)
                and (not params.get("base") or p["base"]["ref"] == params["base"])
                and (params.get("state") in (None, "all") or p["state"] == params.get("state", "open"))
            ]
            return [{k: v for k, v in p.items() if k != "files"} for p in matches]
        m = re.fullmatch(rf"{prefix}/pulls/(\d+)/files", endpoint)
        if m:
            files = self.prs[int(m.group(1))]["files"]
            page, per = int(params.get("page", 1)), int(params.get("per_page", 30))
            return files[(page - 1) * per : page * per]
        m = re.fullmatch(rf"{prefix}/git/ref/heads/(.+)", endpoint)
        if m:
            name = m.group(1)
            if name not in self.branches:
                raise HttpError("Not Found", status_code=404)
            return {"ref": f"refs/heads/{name}", "object": {"sha": self.branches[name], "type": "commit"}}
        m = re.fullmatch(rf"{prefix}/git/commits/([0-9a-f]{{40}})", endpoint)
        if m:
            c = self.commits.get(m.group(1))
            if c is None:
                raise HttpError("Not Found", status_code=404)
            return {
                "sha": c["sha"],
                "tree": {"sha": tree_sha_of(c["tree"])},
                "parents": [{"sha": p} for p in c["parents"]],
                "message": c["message"],
            }
        m = re.fullmatch(rf"{prefix}/compare/([0-9a-f]{{40}})\.\.\.([0-9a-f]{{40}})", endpoint)
        if m:
            base_sha, head_sha = m.groups()
            head_anc, base_anc = self._ancestors(head_sha), self._ancestors(base_sha)
            if base_sha in head_anc:
                status = "ahead" if head_sha != base_sha else "identical"
            elif head_sha in base_anc:
                status = "behind"
            else:
                status = "diverged"
            common = [s for s in head_anc & base_anc]
            merge_base = base_sha if base_sha in head_anc else (common[0] if common else None)
            return {
                "status": status,
                "ahead_by": len(head_anc - base_anc),
                "behind_by": len(base_anc - head_anc),
                "merge_base_commit": {"sha": merge_base} if merge_base else None,
            }
        m = re.fullmatch(rf"{prefix}/branches/(.+)/protection", endpoint)
        if m:
            if self.classic is None:
                raise HttpError("Branch not protected", status_code=404)
            return self.classic
        m = re.fullmatch(rf"{prefix}/branches/(.+)", endpoint)
        if m:
            name = m.group(1)
            if name not in self.branches:
                raise HttpError("Not Found", status_code=404)
            protected = (
                self.branch_protected_flag
                if self.branch_protected_flag is not None
                else bool(self.classic or self.rules)
            )
            return {"name": name, "commit": {"sha": self.branches[name]}, "protected": protected}
        m = re.fullmatch(rf"{prefix}/rules/branches/(.+)", endpoint)
        if m:
            page, per = int(params.get("page", 1)), min(int(params.get("per_page", 30)), self.page_cap)
            return self.rules[(page - 1) * per : page * per]
        m = re.fullmatch(rf"{prefix}/rulesets/(\d+)", endpoint)
        if m:
            detail = self.rulesets.get(int(m.group(1)))
            if detail is None:
                raise HttpError("Not Found", status_code=404)
            return detail
        m = re.fullmatch(rf"{prefix}/git/trees/([0-9a-f]{{40}})", endpoint)
        if m:  # arbre de premier niveau : par défaut sans ``.github`` (barrière d'intégrité des contrôles, W5) ; ``trees`` le surcharge
            return getattr(self, "trees", {}).get(m.group(1), {"tree": [], "truncated": False})
        m = re.fullmatch(rf"{prefix}/commits/([0-9a-f]{{40}})/check-runs", endpoint)
        if m:
            runs = self.check_runs.get(m.group(1), [])
            page, per = int(params.get("page", 1)), min(int(params.get("per_page", 30)), self.page_cap)
            return {"total_count": len(runs), "check_runs": runs[(page - 1) * per : page * per]}
        m = re.fullmatch(rf"{prefix}/commits/([0-9a-f]{{40}})/statuses", endpoint)
        if m:
            sts = self.statuses.get(m.group(1), [])
            page, per = int(params.get("page", 1)), min(int(params.get("per_page", 30)), self.page_cap)
            return sts[(page - 1) * per : page * per]
        m = re.fullmatch(rf"{prefix}/actions/jobs/(\d+)", endpoint)
        if m:  # un identifiant qui n'est pas un job (check publié par l'API des checks) répond 404, comme GitHub
            job = self.actions_jobs.get(int(m.group(1)))
            if job is None:
                raise HttpError("Not Found", status_code=404)
            return job
        m = re.fullmatch(rf"{prefix}/actions/runs/(\d+)", endpoint)
        if m:
            run = self.actions_runs.get(int(m.group(1)))
            if run is None:
                raise HttpError("Not Found", status_code=404)
            return run
        raise AssertionError(f"route GET non simulée: {endpoint}")

    def api_put(self, endpoint: str, data: Dict[str, Any]) -> Any:
        self.calls.append(("PUT", endpoint, dict(data)))
        self._maybe_fail("PUT", endpoint)
        m = re.fullmatch(rf"/repos/{OWNER}/{REPO}/pulls/(\d+)/merge", endpoint)
        if m:
            return self._merge(int(m.group(1)), data)
        raise AssertionError(f"route PUT non simulée: {endpoint}")

    # ── clients réels branchés ─────────────────────────────────────────────────────
    def clients(self):
        prs = PRCommands(token=None)
        branches = BranchCommands(token=None)
        for client in (prs, branches):
            client._api_get = self.api_get
            client._api_put = self.api_put
        return _Clients(prs=prs, branches=branches)

    def merge_calls(self) -> List[tuple]:
        return [c for c in self.calls if c[0] == "PUT" and c[1].endswith("/merge")]


class _Clients:
    def __init__(self, *, prs, branches):
        self.prs = prs
        self.branches = branches
        self.files = None


# ── preuve de livraison (double du contrat A : load_delivery_proof / DeliveryProofError) ───────────────


class FakeDeliveryProofError(RuntimeError):
    """Double de ``collegue.executor.delivery_proof.DeliveryProofError`` (refus explicite de la preuve)."""


class FakeProof:
    """Attributs en lecture seule du contrat public (``w3-interface.md``)."""

    def __init__(self, **fields):
        defaults = dict(verdicts=(), oracles=(), passed=True, phase="build")
        defaults.update(fields)
        object.__setattr__(self, "_f", defaults)

    def __getattr__(self, name):
        try:
            return object.__getattribute__(self, "_f")[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        raise AttributeError("preuve immuable")


class ProofStore:
    def __init__(self):
        self.proofs = {}
        self.requests = []

    def add(self, server: FakeGitHubServer, project_id: int, pr_number: int, /, **overrides) -> FakeProof:
        pr = server.prs[pr_number]
        head = pr["head"]["sha"]
        base = pr["base"]["sha"]
        tree_label = server.commits[head]["tree"]
        fields = dict(
            owner=OWNER,
            repo=REPO,
            project_id=project_id,
            pr_number=pr_number,
            head_sha=head,
            base_sha=base,
            tree_sha=tree_sha_of(tree_label),
        )
        fields.update(overrides)
        fields.setdefault(
            "proof_id",
            hashlib.sha256(f"{fields['project_id']}:{fields['pr_number']}:{fields['head_sha']}".encode()).hexdigest(),
        )
        proof = FakeProof(**fields)
        self.proofs[(project_id, fields["owner"], fields["repo"], pr_number, fields["head_sha"])] = proof
        return proof

    def loader(self, manager, project_id, *, owner, repo, pr_number, head_sha):
        self.requests.append((project_id, owner, repo, pr_number, head_sha))
        try:
            return self.proofs[(project_id, owner, repo, pr_number, head_sha)]
        except KeyError:
            raise FakeDeliveryProofError(f"aucune preuve durable pour PR #{pr_number} tête {head_sha[:12]}") from None
