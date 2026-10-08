"""Double COMPLET de GitHub pour les tests de livraison (vague 3) : un VRAI dépôt Git distant, sans réseau.

Le dépôt « distant » est un dépôt Git bare réel : blobs, arbres et commits sont calculés par ``git`` lui-même (plomberie
``hash-object``/``update-index``/``write-tree``/``commit-tree``), indépendamment du code de production. Les clients
exposent exactement les méthodes que la livraison consomme (``ensure_branch``, ``get_branch_sha``, ``get_git_commit``,
``update_file``, ``delete_file``, ``find_pr_by_head``, ``create_pr``, ``get_pr``) avec la sémantique de la Contents API :
une écriture = un commit de plus sur la branche, mode ``100644`` pour un fichier neuf (le mode d'un fichier existant est conservé).

Les crochets (``on_write``, ``on_create_pr``, ``on_get_pr``) permettent d'injecter une dérive AU MOMENT de l'appel
distant (déplacement de la base, tête qui change pendant la publication), pas seulement avant la dernière lecture.
Un refus dû à un mock incomplet (``AttributeError``) n'est donc jamais confondu avec un refus sécurisé.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional

from collegue.executor.pr import PrClients

_AUTHOR_ENV = {
    "GIT_AUTHOR_NAME": "remote",
    "GIT_AUTHOR_EMAIL": "remote@example.invalid",
    "GIT_COMMITTER_NAME": "remote",
    "GIT_COMMITTER_EMAIL": "remote@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def git(cwd, *args, env=None, input=None, strip=True):
    full_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    full_env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"})
    full_env.update(env or {})
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=str(cwd),
        env=full_env,
        input=input,
        capture_output=True,
        text=isinstance(input, str) or input is None,
        check=True,
    )
    if isinstance(proc.stdout, str):
        return proc.stdout.strip() if strip else proc.stdout
    return proc.stdout


def make_source_repo(path, files: Optional[Dict[str, str]] = None, *, gitignore: Optional[str] = None) -> str:
    """Dépôt Git source (``main``) avec un commit de base ; renvoie son chemin."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    for name, content in (files or {"README.md": "# base\n"}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if gitignore is not None:
        (path / ".gitignore").write_text(gitignore, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "-c", "user.name=fixture", "-c", "user.email=f@e.invalid", "commit", "-q", "-m", "base", env=_AUTHOR_ENV)
    return str(path)


class FakeRemote:
    """Dépôt distant réel (bare) cloné depuis ``source`` ; sert de backend aux trois clients factices."""

    def __init__(self, root, source: str, *, base: str = "main"):
        self.dir = Path(root) / "remote.git"
        git(Path(root), "clone", "-q", "--bare", str(source), str(self.dir))
        self.base = base
        if self.branch_sha(base) is None:  # source initialisée avec une autre branche par défaut
            self._git("update-ref", f"refs/heads/{base}", self._git("rev-parse", "HEAD"))
        self.calls: List[str] = []
        self.writes: List[str] = []
        self.prs: Dict[int, SimpleNamespace] = {}
        self.lost_writes: set = set()
        self.on_write: Optional[Callable[["FakeRemote", str], None]] = None
        self.on_create_pr: Optional[Callable[["FakeRemote"], None]] = None
        self.on_get_pr: Optional[Callable[["FakeRemote", int], None]] = None
        self.hide_pr_head_sha = False
        self.next_pr = 101

    # -- plomberie ------------------------------------------------------------------------------
    def _git(self, *args, env=None, input=None, strip=True):
        return git(self.dir, *args, env={"GIT_DIR": str(self.dir), **(env or {})}, input=input, strip=strip)

    def branch_sha(self, branch: str) -> Optional[str]:
        try:
            return self._git("rev-parse", "--verify", "-q", f"refs/heads/{branch}")
        except subprocess.CalledProcessError:
            return None

    def tree_of(self, sha: str) -> str:
        return self._git("rev-parse", f"{sha}^{{tree}}")

    def files_at(self, ref: str) -> Dict[str, str]:
        out = self._git("ls-tree", "-r", "-z", "--full-tree", ref)
        files = {}
        for record in out.split("\0"):
            if not record:
                continue
            meta, _, path = record.partition("\t")
            files[path] = self._git("cat-file", "blob", meta.split(" ")[2], strip=False)
        return files

    def _commit_change(self, branch: str, path: str, content: Optional[str], message: str) -> str:
        tip = self.branch_sha(branch)
        if tip is None:
            raise KeyError(branch)
        index = str(self.dir / f"index-{os.getpid()}")
        env = {"GIT_INDEX_FILE": index}
        try:
            self._git("read-tree", tip, env=env)
            if content is None:
                self._git("update-index", "--index-info", input=f"0 {'0' * 40}\t{path}\n", env=env)
            else:
                blob = self._git("hash-object", "-w", "--stdin", input=content, env=env)
                # Contents API : un fichier NEUF est écrit en 100644 ; une mise à jour conserve le mode existant.
                mode = "100644"
                existing = self._git("ls-tree", tip, "--", path)
                if existing:
                    mode = existing.split(" ", 1)[0]
                self._git("update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}", env=env)
            tree = self._git("write-tree", env=env)
        finally:
            if os.path.exists(index):
                os.unlink(index)
        commit = self._git("commit-tree", tree, "-p", tip, "-m", message, env=_AUTHOR_ENV)
        self._git("update-ref", f"refs/heads/{branch}", commit)
        return commit

    def advance_base(self, path: str = "moved-on.txt", content: str = "la base a bougé\n") -> str:
        """Déplace la base distante (nouveau commit sur ``main``) — simule un autre merge."""
        return self._commit_change(self.base, path, content, "autre merge")

    # -- clients --------------------------------------------------------------------------------
    def clients(self) -> PrClients:
        return PrClients(branches=_Branches(self), files=_Files(self), prs=_Prs(self))


class _Branches:
    def __init__(self, remote: FakeRemote):
        self.remote = remote
        self.created: List[str] = []

    def ensure_branch(self, owner, repo, branch, from_branch=None):
        self.remote.calls.append("ensure_branch")
        self.created.append(branch)
        if self.remote.branch_sha(branch) is None:
            self.remote._git(
                "update-ref", f"refs/heads/{branch}", self.remote.branch_sha(from_branch or self.remote.base)
            )
        return SimpleNamespace(name=branch, commit_sha=self.remote.branch_sha(branch))

    def get_branch_sha(self, owner, repo, branch):
        self.remote.calls.append("get_branch_sha")
        sha = self.remote.branch_sha(branch)
        if sha is None:
            raise KeyError(f"branche absente: {branch}")
        return sha

    def get_git_commit(self, owner, repo, sha):
        self.remote.calls.append("get_git_commit")
        body = self.remote._git("cat-file", "-p", sha)
        header, _, message = body.partition("\n\n")
        tree, parents = None, []
        for line in header.splitlines():
            if line.startswith("tree "):
                tree = line.split()[1]
            elif line.startswith("parent "):
                parents.append(line.split()[1])
        return SimpleNamespace(sha=sha, tree_sha=tree, parents=parents, message=message.strip() or "-")


class _Files:
    def __init__(self, remote: FakeRemote):
        self.remote = remote
        self.updated: List[str] = []
        self.deleted: List[str] = []

    def update_file(self, owner, repo, path, message, content, branch=None):
        self.remote.calls.append("update_file")
        self.updated.append(path)
        if path not in self.remote.lost_writes:
            self.remote._commit_change(branch, path, content, message)
        self.remote.writes.append(path)
        if self.remote.on_write:
            self.remote.on_write(self.remote, path)
        return {}

    def delete_file(self, owner, repo, path, message, branch=None):
        self.remote.calls.append("delete_file")
        self.deleted.append(path)
        if path not in self.remote.lost_writes:
            self.remote._commit_change(branch, path, None, message)
        self.remote.writes.append(f"-{path}")
        if self.remote.on_write:
            self.remote.on_write(self.remote, f"-{path}")
        return {}


class _Prs:
    def __init__(self, remote: FakeRemote):
        self.remote = remote
        self.created: List[dict] = []

    def _info(self, number: int, *, with_sha: bool):
        pr = self.remote.prs[number]
        head_sha = self.remote.branch_sha(pr.head)
        return SimpleNamespace(
            number=number,
            html_url=f"https://example.invalid/pull/{number}",
            head_branch=pr.head,
            base_branch=pr.base,
            head_sha=head_sha if with_sha and not self.remote.hide_pr_head_sha else None,
            base_sha=self.remote.branch_sha(pr.base),
            state="open",
        )

    def find_pr_by_head(self, owner, repo, head, base=None, state="open"):
        self.remote.calls.append("find_pr_by_head")
        for number, pr in self.remote.prs.items():
            if pr.head == head and (base is None or pr.base == base):
                return self._info(number, with_sha=True)
        return None

    def create_pr(self, owner, repo, title, head, base, body=None):
        self.remote.calls.append("create_pr")
        self.created.append({"title": title, "head": head, "base": base, "body": body})
        if self.remote.on_create_pr:
            self.remote.on_create_pr(self.remote)
        number = self.remote.next_pr
        self.remote.next_pr += 1
        self.remote.prs[number] = SimpleNamespace(head=head, base=base, title=title, body=body)
        return SimpleNamespace(number=number, html_url=f"https://example.invalid/pull/{number}", head_branch=head)

    def get_pr(self, owner, repo, number):
        self.remote.calls.append("get_pr")
        if self.remote.on_get_pr:
            self.remote.on_get_pr(self.remote, number)
        return self._info(number, with_sha=True)
