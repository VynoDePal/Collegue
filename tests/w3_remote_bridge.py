"""Pont de test de la vague 3 (propriété C) : un VRAI dépôt Git distant derrière la frontière REST des VRAIS clients GitHub.

Deux doubles existaient séparément :

* ``github_fakes.FakeRemote`` (lot A) : un dépôt Git bare réel — blobs, arbres, commits calculés par ``git`` — avec la sémantique de
  la Contents API, mais sans protections, checks ni fusion ;
* ``github_fake_server.FakeGitHubServer`` (lot B) : la sémantique REST de la fusion (checks requis, règle « à jour », rulesets,
  commit de fusion), sur un graphe de commits synthétique.

Ce pont les compose SANS réécrire ni l'un ni l'autre : ``BridgeServer`` est un ``FakeGitHubServer`` dont les commits, les
branches et les commits de fusion vivent dans le dépôt bare de ``FakeRemote`` (mêmes SHA, mêmes arbres, mêmes parents ; aucun
rehachage en libellé), et dont la frontière REST accepte en plus les routes de PUBLICATION (``git/refs``, ``contents``, ``pulls``).
Les clients utilisés sont les clients de production (``BranchCommands``, ``FileCommands``, ``PRCommands``), détournés
au seul niveau de leur transport HTTP (``_request_json``).

Conséquence : le commit de fusion produit par le serveur est un VRAI objet Git du dépôt distant, que la resynchronisation
(``git fetch origin`` + ``reset --hard``) et ``verify_local_sync`` lisent réellement depuis le checkout de l'opérateur
(``attach_operator_checkout``). Aucun booléen « preuve verte », aucun ``verify_fn`` factice.
"""

from __future__ import annotations

import base64
import re
import subprocess
from collections.abc import MutableMapping
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional
from urllib.parse import parse_qsl

from github_fake_server import FIVE_CHECKS, GITHUB_ACTIONS_APP, OWNER, REPO, FakeGitHubServer, HttpError
from github_fakes import _AUTHOR_ENV, FakeRemote, git

from collegue.executor.pr import PrClients
from collegue.tools.github_commands import BranchCommands, FileCommands, PRCommands

_PREFIX = f"/repos/{OWNER}/{REPO}"


class _CommitStore(MutableMapping):
    """Commits LUS dans le dépôt Git distant (``git cat-file``) : aucune copie, aucun SHA recalculé."""

    def __init__(self, remote: FakeRemote):
        self._remote = remote

    def __getitem__(self, sha: str) -> Dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{40}", str(sha)):
            raise KeyError(sha)
        try:
            body = self._remote._git("cat-file", "-p", sha)
        except subprocess.CalledProcessError:
            raise KeyError(sha) from None
        header, _, message = body.partition("\n\n")
        tree, parents = None, []
        for line in header.splitlines():
            if line.startswith("tree "):
                tree = line.split()[1]
            elif line.startswith("parent "):
                parents.append(line.split()[1])
        return {"sha": sha, "parents": parents, "tree": tree, "message": message.strip() or "-"}

    def __setitem__(self, sha, value):  # les commits ne s'écrivent que par ``BridgeServer.commit`` (objets réels)
        raise TypeError("commits Git réels : écrire par BridgeServer.commit()")

    def __delitem__(self, sha):
        raise TypeError("commits Git immuables")

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0


class _RefStore(MutableMapping):
    """Branches = ``refs/heads/*`` du dépôt Git distant."""

    def __init__(self, remote: FakeRemote):
        self._remote = remote

    def __getitem__(self, name: str) -> str:
        sha = self._remote.branch_sha(name)
        if sha is None:
            raise KeyError(name)
        return sha

    def __setitem__(self, name: str, sha: str) -> None:
        self._remote._git("update-ref", f"refs/heads/{name}", sha)

    def __delitem__(self, name: str) -> None:
        self._remote._git("update-ref", "-d", f"refs/heads/{name}")

    def __iter__(self) -> Iterator[str]:
        out = self._remote._git("for-each-ref", "--format=%(refname:short)", "refs/heads")
        return iter([line for line in out.splitlines() if line])

    def __len__(self) -> int:
        return len(list(iter(self)))


class BridgeServer(FakeGitHubServer):
    """``FakeGitHubServer`` (sémantique REST de B) adossé au dépôt Git bare de ``FakeRemote`` (A)."""

    def __init__(self, remote: FakeRemote):
        self._real_objects = False
        super().__init__(base_branch=remote.base)
        self.remote = remote
        self.commits = _CommitStore(remote)  # type: ignore[assignment]
        self.branches = _RefStore(remote)  # type: ignore[assignment]
        self._real_objects = True
        self.prs = {}
        self.next_pr = 101
        self.auto_green = True  # CI simulée : les cinq checks passent dès l'ouverture de la PR
        self.on_write: Optional[Callable[["BridgeServer", str], None]] = None
        self.published_calls: List[tuple] = []
        self.protected_direct_writes: set = (
            set()
        )  # branches où GitHub refuse toute écriture directe (ruleset « PR obligatoire »)
        self.truncate_trees = False  # réponses d'arbre « tronquées » (la lecture ne prouve alors rien)

    # ── objets Git réels ───────────────────────────────────────────────────────
    def commit(self, parents: List[str], *, tree: str, message: str = "c") -> str:
        if not self._real_objects:
            return super().commit(parents, tree=tree, message=message)
        args = ["commit-tree", tree]
        for parent in parents:
            args += ["-p", parent]
        args += ["-m", message or "c"]
        return self.remote._git(*args, env=_AUTHOR_ENV)

    # ── clients de production, détournés au niveau du transport ────────────────
    def clients(self) -> PrClients:  # type: ignore[override]
        prs, branches, files = PRCommands(token=None), BranchCommands(token=None), FileCommands(token=None)
        for client in (prs, branches, files):
            client._request_json = self.request  # type: ignore[method-assign]
        return PrClients(branches=branches, files=files, prs=prs)

    def request(self, method: str, endpoint: str, *, params=None, json_data=None) -> Any:
        path, _, query = endpoint.partition("?")
        merged = dict(parse_qsl(query))
        merged.update(params or {})
        data = dict(json_data or {})
        if method == "GET":
            return self._get(path, merged)
        if method == "PUT":
            return self._put(path, data)
        if method == "POST":
            return self._post(path, data)
        if method == "DELETE":
            return self._delete(path, data)
        if method == "PATCH":
            return self._patch(path, data)
        raise AssertionError(f"méthode non simulée: {method} {endpoint}")

    # ── GET ────────────────────────────────────────────────────────────────────
    def _get(self, path: str, params: Dict[str, Any]) -> Any:
        m = re.fullmatch(rf"{_PREFIX}/contents/(.+)", path)
        if m:
            self.calls.append(("GET", path, dict(params)))
            self._maybe_fail("GET", path)
            return self._read_file(m.group(1), params.get("ref") or self.base_branch)
        m = re.fullmatch(rf"{_PREFIX}/git/trees/([0-9a-f]{{40}})", path)
        if m:  # arbre Git RÉEL de premier niveau (barrière d'intégrité des contrôles ``.github/``)
            self.calls.append(("GET", path, dict(params)))
            self._maybe_fail("GET", path)
            recursive = ["-r"] if str(params.get("recursive", "")).lower() in {"1", "true"} else []
            rows = self.remote._git("ls-tree", "-z", *recursive, m.group(1), strip=False).split("\0")
            entries = []
            for row in rows:
                if not row:
                    continue
                meta, _, name = row.partition("\t")
                mode, kind, sha = meta.split(" ")
                entries.append({"path": name, "mode": mode, "type": kind, "sha": sha})
            return {"tree": entries, "truncated": bool(getattr(self, "truncate_trees", False))}
        if path == _PREFIX:
            self.calls.append(("GET", path, dict(params)))
            return {"default_branch": self.base_branch}
        self._refresh_open_prs()
        return self.api_get(path, params)

    def _read_file(self, rel: str, ref: str) -> Dict[str, Any]:
        sha = self.remote.branch_sha(ref)
        if sha is None:
            raise HttpError("Not Found", status_code=404)
        row = self.remote._git("ls-tree", sha, "--", rel)
        if not row:
            raise HttpError("Not Found", status_code=404)
        meta = row.split("\t")[0].split(" ")
        blob = meta[2]
        raw = self.remote._git("cat-file", "blob", blob, strip=False)
        return {
            "type": "file",
            "sha": blob,
            "size": len(raw),
            "content": base64.b64encode(raw.encode("utf-8")).decode("ascii"),
            "html_url": f"https://github.test/{OWNER}/{REPO}/blob/{ref}/{rel}",
        }

    # ── PUT / POST / DELETE (publication) ─────────────────────────────────────
    def _put(self, path: str, data: Dict[str, Any]) -> Any:
        m = re.fullmatch(rf"{_PREFIX}/contents/(.+)", path)
        if not m:
            self._refresh_open_prs()
            return self.api_put(path, data)
        self.calls.append(("PUT", path, {k: v for k, v in data.items() if k != "content"}))
        self._maybe_fail("PUT", path)
        rel, branch = m.group(1), data.get("branch") or self.base_branch
        if branch in self.protected_direct_writes:  # texte du refus réel d'un ruleset (observé sur la fixture protégée)
            raise HttpError(
                "GH013: Changes must be made through a pull request. Required status check Fixture tests is expected.",
                status_code=409,
            )
        current = None
        try:
            current = self._read_file(rel, branch)
        except HttpError:
            pass
        if current is not None and data.get("sha") != current["sha"]:
            raise HttpError("sha does not match", status_code=409)
        content = base64.b64decode(data["content"]).decode("utf-8")
        commit = self.remote._commit_change(branch, rel, content, data.get("message", "write"))
        self.remote.writes.append(rel)
        if self.on_write is not None:
            self.on_write(self, rel)
        return {"content": {"path": rel}, "commit": {"sha": commit}}

    def _patch(self, path: str, data: Dict[str, Any]) -> Any:
        m = re.fullmatch(rf"{_PREFIX}/pulls/(\d+)", path)
        if not m or int(m.group(1)) not in self.prs:
            raise AssertionError(f"route PATCH non simulée: {path}")
        self.calls.append(("PATCH", path, dict(data)))
        self._maybe_fail("PATCH", path)
        pr = self.prs[int(m.group(1))]
        if data.get("state") == "closed" and not pr["merged"]:  # fermeture sans fusion (nettoyage de la campagne)
            pr["state"] = "closed"
        return {k: v for k, v in pr.items() if k != "files"}

    def _delete(self, path: str, data: Dict[str, Any]) -> Any:
        ref = re.fullmatch(rf"{_PREFIX}/git/refs/heads/(.+)", path)
        if ref:  # suppression d'une branche (nettoyage de la campagne) ; 404 si absente
            self.calls.append(("DELETE", path, dict(data)))
            self._maybe_fail("DELETE", path)
            if ref.group(1) not in self.branches:
                raise HttpError("Not Found", status_code=404)
            del self.branches[ref.group(1)]
            return {}
        m = re.fullmatch(rf"{_PREFIX}/contents/(.+)", path)
        if not m:
            raise AssertionError(f"route DELETE non simulée: {path}")
        self.calls.append(("DELETE", path, dict(data)))
        self._maybe_fail("DELETE", path)
        rel, branch = m.group(1), data.get("branch") or self.base_branch
        current = self._read_file(rel, branch)
        if data.get("sha") != current["sha"]:
            raise HttpError("sha does not match", status_code=409)
        commit = self.remote._commit_change(branch, rel, None, data.get("message", "delete"))
        self.remote.writes.append(f"-{rel}")
        if self.on_write is not None:
            self.on_write(self, f"-{rel}")
        return {"commit": {"sha": commit}}

    def _post(self, path: str, data: Dict[str, Any]) -> Any:
        self.calls.append(("POST", path, dict(data)))
        self._maybe_fail("POST", path)
        if path == f"{_PREFIX}/git/refs":
            name = str(data["ref"]).removeprefix("refs/heads/")
            if name in self.branches:
                raise HttpError("Reference already exists", status_code=422)
            self.branches[name] = data["sha"]
            return {"ref": data["ref"], "object": {"sha": data["sha"], "type": "commit"}}
        if path == f"{_PREFIX}/pulls":
            return self._create_pr(data)
        raise AssertionError(f"route POST non simulée: {path}")

    # ── PR ───────────────────────────────────────────────────────────────────
    def _create_pr(self, data: Dict[str, Any]) -> Dict[str, Any]:
        head_ref = str(data["head"]).split(":", 1)[-1]
        base_ref = str(data["base"])
        number = self.next_pr
        self.next_pr += 1
        head_sha, base_sha = self.branches[head_ref], self.branches[base_ref]
        pr = {
            "number": number,
            "title": data.get("title", ""),
            "state": "open",
            "html_url": f"https://github.test/{OWNER}/{REPO}/pull/{number}",
            "user": {"login": self.actor_login or "collegue-bot"},
            "base": {"ref": base_ref, "sha": base_sha},
            "head": {"ref": head_ref, "sha": head_sha},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "labels": [],
            "draft": False,
            "merged": False,
            "merge_commit_sha": None,
            "body": data.get("body") or "",
            "mergeable": True,
            "mergeable_state": "clean",
        }
        files = self._pr_files(base_sha, head_sha)
        pr["files"] = files
        pr["additions"] = sum(f["additions"] for f in files)
        pr["deletions"] = sum(f["deletions"] for f in files)
        pr["changed_files"] = len(files)
        self.prs[number] = pr
        # ``FakeRemote.prs`` : même numérotation pour les tests qui lisent le double de A.
        self.remote.prs[number] = SimpleNamespace(head=head_ref, base=base_ref, title=pr["title"], body=pr["body"])
        self.remote.next_pr = max(self.remote.next_pr, number + 1)
        if self.auto_green:
            self.set_checks(head_sha, {name: "success" for name in FIVE_CHECKS}, app_id=GITHUB_ACTIONS_APP)
        return {k: v for k, v in pr.items() if k != "files"}

    def _pr_files(self, base_sha: str, head_sha: str) -> List[Dict[str, Any]]:
        out = self.remote._git("diff", "--numstat", "--no-renames", base_sha, head_sha)
        status = dict(
            line.split("\t")[::-1]
            for line in self.remote._git("diff", "--name-status", "--no-renames", base_sha, head_sha).splitlines()
            if line
        )
        files = []
        for line in out.splitlines():
            add, dele, name = line.split("\t", 2)
            files.append(
                {
                    "filename": name,
                    "status": {"A": "added", "M": "modified", "D": "removed"}.get(status.get(name, "M"), "modified"),
                    "additions": 0 if add == "-" else int(add),
                    "deletions": 0 if dele == "-" else int(dele),
                }
            )
        return files

    def _refresh_open_prs(self) -> None:
        """Une PR ouverte suit sa branche de tête et sa branche de base (comme GitHub)."""
        for pr in self.prs.values():
            if pr["state"] != "open":
                continue
            for side in ("head", "base"):
                sha = self.remote.branch_sha(pr[side]["ref"])
                if sha is not None:
                    pr[side]["sha"] = sha

    # ── aides de scénario ─────────────────────────────────────────────────────
    def attach_operator_checkout(self, source: str) -> None:
        """Le checkout de l'opérateur (``repo_source``) suit ce dépôt distant via ``origin`` (resynchronisation RÉELLE)."""
        git(source, "remote", "add", "origin", str(self.remote.dir))
        git(source, "fetch", "-q", "origin")

    def break_origin(self, source: str) -> str:
        """Rend ``origin`` injoignable (la resynchronisation échoue VRAIMENT) ; renvoie l'URL à restaurer."""
        url = git(source, "remote", "get-url", "origin")
        git(source, "remote", "set-url", "origin", str(self.remote.dir) + ".absent")
        return url

    def restore_origin(self, source: str, url: str) -> None:
        git(source, "remote", "set-url", "origin", url)

    def green(self, number: int, **overrides: str) -> None:
        states = {name: "success" for name in FIVE_CHECKS}
        states.update(overrides)
        self.set_checks(self.prs[number]["head"]["sha"], states)

    def protect_direct_writes(self, *branches: str) -> None:
        """Ces branches refusent les écritures directes (Contents) comme le ruleset des bases de campagne ; seule une PR y fusionne."""
        self.protected_direct_writes.update(branches)

    def required_names(self) -> List[str]:
        return list(FIVE_CHECKS)

    def protect(self, *, strict: bool = True, enforce_admins: bool = True) -> None:
        self.protect_classic(strict=strict, enforce_admins=enforce_admins)

    def merged_pr_numbers(self) -> List[int]:
        return [n for n, pr in self.prs.items() if pr["merged"]]

    def merge_bodies(self) -> List[Dict[str, Any]]:
        return [c[2] for c in self.merge_calls()]

    def write_remote_file(self, branch: str, path: str, content: str, message: str = "écriture hors moteur") -> str:
        """Commit réel poussé sur ``branch`` hors du moteur (autre contributeur / push tardif)."""
        return self.remote._commit_change(branch, path, content, message)

    def merge_out_of_band(self, number: int, *, method: str = "squash") -> Dict[str, Any]:
        """Fusion par un humain / un autre outil : passe par la même sémantique serveur, SANS le moteur."""
        return self.api_put(
            f"{_PREFIX}/pulls/{number}/merge", {"merge_method": method, "sha": self.prs[number]["head"]["sha"]}
        )


def make_bridge(
    root, source: str, *, base: str = "main", operator_checkout: bool = True, protect: bool = True
) -> BridgeServer:
    """Dépôt distant réel + serveur REST ; ``source`` devient le checkout opérateur suivant ``origin``."""
    remote = FakeRemote(root, source, base=base)
    server = BridgeServer(remote)
    if protect:
        server.protect()
    if operator_checkout:
        server.attach_operator_checkout(source)
    return server
