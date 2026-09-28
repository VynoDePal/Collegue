"""Frontière Git commune des opérations HÔTE sur un workspace non fiable.

**Menace.** L'agent (OpenHands) ET les tests du gate tournent dans un conteneur
qui monte le workspace en lecture-écriture. Un ``.git`` vivant dans ce montage
est donc écrit par du code non fiable : ``core.fsmonitor``, ``core.hooksPath``,
``include``/``includeIf``, filtres ``clean``/``smudge``, ``diff.external``,
``textconv``, hooks (``post-index-change``, ``pre-commit``, ``reference-
transaction``…), redirection ``.git`` (gitfile / symlink / ``core.worktree``),
et falsification de ``HEAD``/index pour masquer un changement. Chacun s'exécute
— ou fausse le diff — dès que l'HÔTE lance ``git add -A`` / ``git diff`` /
``git commit`` / ``git revert`` dans ce répertoire.

**Méthode.** Le workspace de travail n'est jamais une source fiable de
hooks/config/GIT_DIR/base HEAD :

- les métadonnées Git de contrôle (objets, refs, config, index, HEAD = base de
  livraison) vivent dans ``<workspace>.control``, FRÈRE du workspace, jamais
  monté dans un conteneur (le sandbox refuse un montage qui l'inclurait) ;
- toute opération hôte passe par :class:`TrustedGit` : ``GIT_DIR`` = répertoire
  de contrôle et ``GIT_WORK_TREE`` = workspace explicites, environnement
  reconstruit de zéro (pas de config globale/système, pas de ``GIT_*`` hérité),
  et neutralisation forcée en ``-c`` des mécanismes exécutants
  (:data:`HARDENING_CONFIG`) — défense en profondeur si un jour la config de
  contrôle était altérée ;
- le ``.git`` que l'agent voit dans son workspace n'est qu'une COPIE jetable
  (confort de l'agent, compatibilité) : l'hôte ne le lit jamais, et son contenu
  n'influence ni la base, ni l'index, ni le diff ;
- la capture stage dans l'index PRIVÉ du répertoire de contrôle et compare à
  son ``HEAD`` (la base fiable), avec ``--no-ext-diff --no-textconv`` ; un dépôt
  imbriqué (gitlink) est refusé (fail-closed).

:class:`HardenedGitRunner` couvre les clones plats que l'HÔTE vient de créer et
qui ne sont jamais montés (revert, santé de main) : même environnement durci,
liste blanche de config, refus de ``.git`` gitfile/symlink ; et, si le chemin
désigne un workspace géré, il bascule automatiquement sur son répertoire de
contrôle.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
from typing import Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple, Union

from collegue.executor.command import COMMAND_NOT_FOUND_EXIT_CODE
from collegue.sandbox.executor import GIT_CONTROL_MARKER, TIMEOUT_EXIT_CODE, SandboxResult

logger = logging.getLogger(__name__)

# Le répertoire de contrôle d'un workspace ``/x/workspace`` est ``/x/workspace.control``.
CONTROL_SUFFIX = ".control"

DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_OUTPUT_BYTES = 10 * 1024 * 1024
# Le diff capturé est l'objet livré/relu : jamais tronqué en silence (fail-closed).
CAPTURE_MAX_OUTPUT_BYTES = 32 * 1024 * 1024

# Mécanismes git qui EXÉCUTENT du code ou redirigent la lecture, neutralisés en
# ligne de commande (précédence maximale sur tout fichier de config).
HARDENING_CONFIG: Tuple[Tuple[str, str], ...] = (
    ("core.fsmonitor", ""),
    ("core.hooksPath", os.devnull),
    ("core.untrackedCache", "false"),
    ("core.attributesFile", os.devnull),
    ("core.excludesFile", os.devnull),
    ("core.pager", "cat"),
    ("core.editor", "true"),
    ("core.askPass", "true"),
    ("core.sshCommand", "false"),
    ("protocol.ext.allow", "never"),
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
    ("commit.gpgSign", "false"),
    ("tag.gpgSign", "false"),
    ("submodule.recurse", "false"),
)


class WorkspaceError(RuntimeError):
    """Échec de préparation/exploitation du workspace (source invalide, git en erreur,
    frontière Git violée ou invérifiable…)."""


# ── environnement durci ───────────────────────────────────────────────────────────

_home_lock = threading.Lock()
_home_dir: Optional[str] = None


def _empty_home() -> str:
    """HOME vide propre au process : aucune config utilisateur/globale n'est lue."""
    global _home_dir
    with _home_lock:
        if _home_dir is None or not os.path.isdir(_home_dir):
            _home_dir = tempfile.mkdtemp(prefix="collegue-git-home-")
            atexit.register(shutil.rmtree, _home_dir, ignore_errors=True)
        return _home_dir


def hardened_env(extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Environnement git reconstruit de zéro (aucun ``GIT_*`` hérité).

    Ni config système/globale, ni ``GIT_DIR``/``GIT_INDEX_FILE``/``GIT_EXTERNAL_DIFF``
    ou ``GIT_CONFIG_*`` du process appelant, ni prompt interactif, ni pager/éditeur.
    """
    home = _empty_home()
    env = {
        "PATH": os.environ.get("PATH") or os.defpath,
        "HOME": home,
        "XDG_CONFIG_HOME": os.path.join(home, "xdg"),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "true",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_EDITOR": "true",
        "GIT_SEQUENCE_EDITOR": "true",
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        env["TMPDIR"] = tmpdir
    if extra:
        env.update(extra)
    return env


def _config_args() -> List[str]:
    args: List[str] = []
    for key, value in HARDENING_CONFIG:
        args += ["-c", f"{key}={value}"]
    return args


# ── exécution bornée ──────────────────────────────────────────────────────────────


class _Raw(NamedTuple):
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    truncated: bool


def _read_capped(path: str, limit: int) -> Tuple[bytes, bool]:
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    return data[:limit], len(data) > limit


def _run_raw(
    argv: Sequence[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    stdin: Optional[bytes] = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    lock_dir: Optional[str] = None,
) -> _Raw:
    """Lance ``argv`` (jamais un shell) ; sortie sur disque puis relue avec plafond.

    Le process tourne dans son propre groupe : au timeout tout le groupe est tué,
    et un ``index.lock`` orphelin du répertoire de contrôle est retiré (sinon toute
    opération suivante échouerait).
    """
    out_f = tempfile.NamedTemporaryFile(prefix="git-out-", delete=False)
    err_f = tempfile.NamedTemporaryFile(prefix="git-err-", delete=False)
    out_path, err_path = out_f.name, err_f.name
    timed_out = False
    exit_code = 0
    try:
        try:
            proc = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=out_f,
                stderr=err_f,
                start_new_session=True,
            )
            try:
                proc.communicate(input=stdin, timeout=timeout)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = TIMEOUT_EXIT_CODE
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                proc.wait()
                if lock_dir:
                    for lock in ("index.lock", "HEAD.lock"):
                        try:
                            os.unlink(os.path.join(lock_dir, lock))
                        except OSError:
                            pass
        except FileNotFoundError:
            exit_code = COMMAND_NOT_FOUND_EXIT_CODE
        finally:
            out_f.close()
            err_f.close()
        stdout, out_trunc = _read_capped(out_path, max_output_bytes)
        stderr, err_trunc = _read_capped(err_path, max_output_bytes)
        if exit_code == COMMAND_NOT_FOUND_EXIT_CODE:
            stderr = (stderr + f"\n[git-boundary] binaire ou répertoire introuvable: {argv[0]}".encode()).strip()
        if timed_out:
            stderr += f"\n[git-boundary] délai dépassé après {timeout:g}s".encode()
        return _Raw(exit_code, stdout, stderr, timed_out, out_trunc or err_trunc)
    finally:
        for path in (out_path, err_path):
            try:
                os.unlink(path)
            except OSError:
                pass


def _as_result(raw: _Raw) -> SandboxResult:
    stdout = raw.stdout.decode("utf-8", errors="replace")
    stderr = raw.stderr.decode("utf-8", errors="replace")
    if raw.truncated:
        stdout += "\n[git-boundary] sortie tronquée"
    return SandboxResult(exit_code=raw.exit_code, stdout=stdout, stderr=stderr, timed_out=raw.timed_out)


# ── localisation / validation du répertoire de contrôle ───────────────────────────


def control_dir_for(workspace_path: Union[str, os.PathLike]) -> str:
    """Chemin (convention) du répertoire de contrôle d'un workspace."""
    return os.path.abspath(os.fspath(workspace_path)) + CONTROL_SUFFIX


def locate_control(workspace_path: Union[str, os.PathLike]) -> Optional[str]:
    """Répertoire de contrôle VALIDÉ du workspace, ``None`` s'il n'existe pas.

    Fail-closed : un répertoire de contrôle présent mais incohérent (symlink,
    marqueur absent, marqueur apparié à un AUTRE workspace) lève
    :class:`WorkspaceError` — on ne retombe jamais sur le ``.git`` du workspace.
    """
    workspace = os.path.abspath(os.fspath(workspace_path))
    control = workspace + CONTROL_SUFFIX
    if not os.path.lexists(control):
        return None
    marker = os.path.join(control, GIT_CONTROL_MARKER)
    if os.path.islink(control) or not os.path.isdir(control) or os.path.islink(marker):
        raise WorkspaceError(f"répertoire de contrôle Git invalide (symlink/fichier): {control}")
    try:
        with open(marker, encoding="utf-8") as handle:
            recorded = handle.read().strip()
    except OSError as exc:
        raise WorkspaceError(f"répertoire de contrôle Git sans marqueur lisible: {control}") from exc
    if recorded != os.path.realpath(workspace):
        raise WorkspaceError(f"répertoire de contrôle Git apparié à un autre workspace: {control}")
    return control


def _decode_name(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceError(f"nom de fichier non UTF-8 refusé: {raw!r}") from exc


def _remove_entry(path: str) -> None:
    """Supprime un fichier/symlink/répertoire SANS jamais suivre de lien.

    Un répertoire aux permissions retirées (l'agent en est capable) est d'abord rendu
    accessible — jamais à travers un lien symbolique — avant une seconde tentative.
    """
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
        return
    if not os.path.isdir(path):
        return
    try:
        shutil.rmtree(path)
    except OSError:
        for dirpath, dirnames, _files in os.walk(path):  # os.walk ne suit pas les liens
            for name in dirnames:
                child = os.path.join(dirpath, name)
                if not os.path.islink(child):
                    try:
                        os.chmod(child, stat.S_IRWXU)
                    except OSError:
                        pass
        os.chmod(path, stat.S_IRWXU)
        shutil.rmtree(path)


# ── opérations sur un workspace géré ──────────────────────────────────────────────


class TrustedGit:
    """Git HÔTE sur un workspace géré : ``GIT_DIR`` = contrôle, ``GIT_WORK_TREE`` = workspace.

    Le contenu du workspace est LU (hachage des fichiers) mais sa config/ses hooks/
    son ``HEAD``/son index ne sont jamais consultés. La base de livraison est le
    ``HEAD`` du répertoire de contrôle, que seul l'hôte écrit.
    """

    def __init__(
        self,
        git_dir: str,
        work_tree: str,
        *,
        git_bin: str = "git",
        timeout: float = DEFAULT_TIMEOUT,
        max_output_bytes: int = CAPTURE_MAX_OUTPUT_BYTES,
    ):
        self.git_dir = git_dir
        self.work_tree = work_tree
        self.git_bin = git_bin
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes

    @classmethod
    def locate(cls, workspace_path: Union[str, os.PathLike], *, git_bin: str = "git") -> Optional["TrustedGit"]:
        """:class:`TrustedGit` du workspace, ``None`` si non géré (aucun contrôle)."""
        control = locate_control(workspace_path)
        if control is None:
            return None
        return cls(control, os.path.realpath(os.fspath(workspace_path)), git_bin=git_bin)

    # -- exécution -----------------------------------------------------------------

    def _env(self) -> Dict[str, str]:
        return hardened_env({"GIT_DIR": self.git_dir, "GIT_WORK_TREE": self.work_tree})

    def _raw(self, args: Sequence[str], *, stdin: Optional[bytes] = None) -> _Raw:
        argv = [self.git_bin, *_config_args(), *args]
        return _run_raw(
            argv,
            cwd=self.work_tree,
            env=self._env(),
            stdin=stdin,
            timeout=self.timeout,
            max_output_bytes=self.max_output_bytes,
            lock_dir=self.git_dir,
        )

    def run(self, *args: str, stdin: Optional[bytes] = None) -> SandboxResult:
        """Exécute ``git <args>`` ; ne lève pas sur un code de sortie non nul."""
        return _as_result(self._raw(args, stdin=stdin))

    def must(self, *args: str, stdin: Optional[bytes] = None, what: Optional[str] = None) -> str:
        """Comme :meth:`run` mais lève :class:`WorkspaceError` si git échoue."""
        raw = self._raw(args, stdin=stdin)
        if raw.exit_code != 0 or raw.timed_out:
            detail = (raw.stderr or raw.stdout).decode("utf-8", errors="replace").strip()
            raise WorkspaceError(f"{what or 'git ' + ' '.join(args[:2])} a échoué: {detail}")
        return raw.stdout.decode("utf-8", errors="replace")

    # -- lecture -------------------------------------------------------------------

    def head(self) -> str:
        """SHA de la base FIABLE courante (``HEAD`` du répertoire de contrôle)."""
        return self.must("rev-parse", "HEAD", what="lecture de la base fiable").strip()

    # -- capture -------------------------------------------------------------------

    def capture(self, paths: Optional[Sequence[str]] = None) -> Tuple[str, Tuple[str, ...]]:
        """Diff autoritatif ``base fiable → workspace`` et fichiers touchés.

        ``git add -A`` stage le workspace dans l'index PRIVÉ du répertoire de
        contrôle (persistant entre captures : un recapture borné à ``paths`` garde
        l'état stagé par la capture initiale), puis l'index est comparé au ``HEAD``
        de contrôle. ``--no-renames`` : un renommage rapporte ancien ET nouveau chemin
        (sinon la PR ne supprimerait jamais l'ancien fichier). Fail-closed : dépôt
        imbriqué / sous-module (mode 160000), nom non UTF-8, diff tronqué.
        """
        if not os.path.isdir(self.work_tree):
            raise WorkspaceError(f"workspace introuvable: {self.work_tree}")
        add = ["add", "-A"]
        if paths:
            add += ["--", *paths]
        self.must(*add, what="git add")

        listing = self._raw(
            ["diff", "--cached", "--raw", "--no-abbrev", "--no-renames", "--no-ext-diff", "--no-textconv", "-z", "HEAD"]
        )
        if listing.exit_code != 0 or listing.truncated:
            detail = listing.stderr.decode("utf-8", "replace").strip() or "sortie tronquée"
            raise WorkspaceError(f"git diff --raw a échoué: {detail}")
        files = self._parse_raw_listing(listing.stdout)

        patch = self._raw(
            [
                "diff",
                "--cached",
                "--binary",
                "--full-index",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                "HEAD",
            ]
        )
        if patch.exit_code != 0:
            raise WorkspaceError("git diff a échoué: " + patch.stderr.decode("utf-8", "replace").strip())
        if patch.truncated:
            raise WorkspaceError(
                f"diff supérieur à {self.max_output_bytes} octets : livrable invérifiable, capture refusée"
            )
        return patch.stdout.decode("utf-8", errors="replace"), files

    @staticmethod
    def _parse_raw_listing(data: bytes) -> Tuple[str, ...]:
        tokens = data.split(b"\0")
        if tokens and tokens[-1] == b"":
            tokens.pop()
        files: List[str] = []
        index = 0
        while index < len(tokens):
            meta = tokens[index]
            if not meta.startswith(b":"):
                raise WorkspaceError("sortie git diff --raw inattendue")
            fields = meta[1:].split(b" ")
            if len(fields) < 5:
                raise WorkspaceError("sortie git diff --raw inattendue")
            old_mode, new_mode, status = fields[0], fields[1], fields[4][:1]
            count = 2 if status in (b"R", b"C") else 1
            names = tokens[index + 1 : index + 1 + count]
            index += 1 + count
            if len(names) != count:
                raise WorkspaceError("sortie git diff --raw tronquée")
            if b"160000" in (old_mode, new_mode):
                raise WorkspaceError(
                    "dépôt git imbriqué / sous-module refusé (jamais interrogé, jamais livré): "
                    + ", ".join(_decode_name(name) for name in names)
                )
            files.extend(_decode_name(name) for name in names)
        return tuple(files)

    # -- seed / compounding / nettoyage ----------------------------------------------

    def apply_seed(self, diff: str) -> bool:
        """Réapplique ``diff`` en 3-way ; sur échec, restaure un arbre propre (``False``).

        Le patch arrive sur stdin (aucun fichier temporaire) ; ``git apply`` refuse
        de lui-même les chemins ``.git/`` et les traversées.
        """
        payload = (diff if diff.endswith("\n") else diff + "\n").encode("utf-8")
        result = self.run("apply", "-3", "--whitespace=nowarn", "-", stdin=payload)
        if result.ok:
            self.refresh_agent_view()
            return True
        logger.warning(
            "seed_diff inapplicable sur %s (conflit réel avec le main avancé ?)"
            " — la tentative repart du clone vierge : %s",
            self.work_tree,
            (result.stderr or result.stdout or "").strip()[:300],
        )
        # #479 : un 3-way en conflit laisse des marqueurs et des ajouts indexés.
        self.reset_clean()
        return False

    def reset_clean(self) -> None:
        """Restaure le workspace et l'index de contrôle sur la base fiable (best-effort)."""
        self.run("reset", "--hard", "--quiet")
        self.run("clean", "-fdq")

    def commit_all(self, message: str, *, email: str, name: str) -> bool:
        """Stage tout et COMMITE dans le répertoire de contrôle : nouvelle base fiable.

        Sert au compounding (#545) : ``HEAD`` reflète l'état cumulé, donc la capture
        suivante ne contient que les changements du round courant.
        """
        if not self.run("add", "-A").ok:
            return False
        result = self.run(
            "-c",
            f"user.email={email}",
            "-c",
            f"user.name={name}",
            "commit",
            "-q",
            "--no-verify",
            "--no-gpg-sign",
            "-m",
            message,
        )
        if not result.ok:
            return False
        self.refresh_agent_view()
        return True

    def refresh_agent_view(self) -> bool:
        """Recopie le contrôle dans ``<workspace>/.git`` : la vue jetable de l'agent.

        Copie réelle (aucun hardlink, aucun partage d'inode avec le contrôle ni la
        source). Best-effort : l'hôte ne lit jamais cette copie, un échec est sans
        conséquence de sécurité.
        """
        dest = os.path.join(self.work_tree, ".git")
        try:
            _remove_entry(dest)
            shutil.copytree(
                self.git_dir,
                dest,
                symlinks=True,
                ignore=shutil.ignore_patterns(GIT_CONTROL_MARKER, "*.lock"),
            )
            os.chmod(dest, 0o755)
            return True
        except (OSError, shutil.Error) as exc:
            logger.warning("vue git de l'agent non rafraîchie (%s) : %s", dest, exc)
            return False


def create_managed_workspace(
    source: str,
    *,
    parent: str,
    branch: str,
    git_bin: str = "git",
    timeout: float = DEFAULT_TIMEOUT,
) -> Tuple[str, str]:
    """Clone ``source`` vers ``<parent>/workspace`` avec contrôle HORS montage.

    Renvoie ``(chemin du workspace, SHA de base)``. Le clone est fait sans checkout
    dans un répertoire temporaire ; son ``.git`` est DÉPLACÉ en
    ``<workspace>.control`` (jamais monté), le workspace est matérialisé depuis le
    contrôle sur la branche dédiée, puis reçoit une copie jetable de ``.git``.
    """
    workspace = os.path.join(parent, "workspace")
    control = workspace + CONTROL_SUFFIX
    if os.path.lexists(control):
        raise WorkspaceError(f"répertoire de contrôle déjà présent: {control}")
    if os.path.lexists(workspace) and (
        os.path.islink(workspace) or not os.path.isdir(workspace) or os.listdir(workspace)
    ):
        raise WorkspaceError(f"destination du workspace non vide: {workspace}")

    scratch = tempfile.mkdtemp(prefix=".collegue-clone-", dir=parent)
    try:
        template = os.path.join(scratch, "template")
        os.mkdir(template)  # gabarit VIDE : aucun hook copié dans le contrôle
        clone_dir = os.path.join(scratch, "repo")
        cloned = _as_result(
            _run_raw(
                [
                    git_bin,
                    *_config_args(),
                    "clone",
                    "--quiet",
                    "--no-checkout",
                    f"--template={template}",
                    source,
                    clone_dir,
                ],
                cwd=parent,
                env=hardened_env(),
                timeout=timeout,
                max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
            )
        )
        if not cloned.ok:
            raise WorkspaceError(f"git clone a échoué: {cloned.stderr.strip() or cloned.stdout.strip()}")
        os.rename(os.path.join(clone_dir, ".git"), control)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    os.makedirs(workspace, exist_ok=True)
    real_workspace = os.path.realpath(workspace)
    os.chmod(control, 0o700)
    with open(os.path.join(control, GIT_CONTROL_MARKER), "w", encoding="utf-8") as handle:
        handle.write(real_workspace + "\n")

    repo = TrustedGit(control, real_workspace, git_bin=git_bin, timeout=timeout)
    try:
        base_commit = repo.must("rev-parse", "HEAD", what="lecture du commit de base").strip()
    except WorkspaceError as exc:
        raise WorkspaceError(f"impossible de lire le commit de base: {exc}") from exc
    if not base_commit:
        raise WorkspaceError("impossible de lire le commit de base: HEAD vide")
    repo.must("branch", "--quiet", branch, base_commit, what=f"git checkout -b {branch}")
    repo.must("symbolic-ref", "HEAD", f"refs/heads/{branch}", what=f"git checkout -b {branch}")
    repo.must("reset", "--hard", "--quiet", what="matérialisation du workspace")
    repo.refresh_agent_view()
    return workspace, base_commit


# ── runner git durci pour les clones plats créés par l'hôte ───────────────────────

_SAFE_CONFIG_KEY = re.compile(
    r"^(core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode|symlinks)"
    r"|remote\..+\.(url|fetch|pushurl)"
    r"|branch\..+\.(remote|merge|rebase)"
    r"|user\.(name|email))$"
)
_TRANSPORT_HELPER = re.compile(r"^[A-Za-z0-9+.\-]+::")


def plain_git_dir_problem(workspace: str, *, git_bin: str = "git") -> Optional[str]:
    """Raison pour laquelle ``<workspace>/.git`` n'est pas utilisable, ``None`` si sain.

    Réservé aux clones PLATS que l'hôte vient de créer (jamais montés). Refus :
    ``.git`` gitfile/symlink, ``commondir``/``config.worktree`` (redirections), config
    qui sort d'une liste blanche minimale (remote/branch/user/core structurel) ou
    qui contient ``include``/``includeIf``/un transport ``ext::``.
    """
    dot_git = os.path.join(workspace, ".git")
    try:
        mode = os.lstat(dot_git).st_mode
    except FileNotFoundError:
        return None  # pas encore un dépôt (ex. clone en cours) : git échouera de lui-même
    except OSError as exc:
        return f".git illisible: {exc}"
    if not stat.S_ISDIR(mode):
        return ".git n'est pas un répertoire réel (gitfile ou lien symbolique)"
    for redirect in ("commondir", "config.worktree"):
        if os.path.lexists(os.path.join(dot_git, redirect)):
            return f".git/{redirect} présent (redirection de dépôt)"
    config_path = os.path.join(dot_git, "config")
    if os.path.islink(config_path):
        return ".git/config est un lien symbolique"
    if not os.path.isfile(config_path):
        return ".git/config absent"
    raw = _run_raw(
        [git_bin, "config", "--file", config_path, "--no-includes", "--list", "-z"],
        cwd=tempfile.gettempdir(),
        env=hardened_env(),
        timeout=30.0,
        max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
    )
    if raw.exit_code != 0 or raw.truncated:
        return ".git/config illisible"
    for entry in raw.stdout.split(b"\0"):
        if not entry:
            continue
        key, _, value = entry.decode("utf-8", errors="replace").partition("\n")
        if not _SAFE_CONFIG_KEY.match(key):
            return f"clé de configuration non autorisée: {key}"
        if key.startswith("remote.") and _TRANSPORT_HELPER.match(value):
            return f"transport distant non autorisé: {key}"
    return None


class HardenedGitRunner:
    """:class:`~collegue.executor.command.CommandRunner` git-seulement, durci.

    Remplace ``LocalCommandRunner`` pour la plomberie git sur un clone que l'HÔTE a
    créé et qui n'est jamais monté dans un conteneur (revert, santé de ``main``) :

    - env reconstruit de zéro et neutralisation ``-c`` (:data:`HARDENING_CONFIG`) ;
    - workspace GÉRÉ (répertoire de contrôle frère) → ``GIT_DIR``/``GIT_WORK_TREE``
      pointent le contrôle ; le ``.git`` du workspace n'est jamais lu ;
    - sinon clone plat : ``.git`` doit passer :func:`plain_git_dir_problem`, et la
      découverte de dépôt ne remonte jamais au-delà du workspace
      (``GIT_CEILING_DIRECTORIES``).

    N'exécute QUE de l'argv git (une chaîne shell est refusée) et refuse toute
    option globale autre que ``-c`` (``-C``, ``--git-dir``, ``--work-tree``…).
    """

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT, max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES):
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes

    @staticmethod
    def _refused(reason: str) -> SandboxResult:
        return SandboxResult(exit_code=126, stdout="", stderr=f"[git-boundary] refusé: {reason}")

    def run_command(self, cmd: Union[str, List[str]], workspace: str) -> SandboxResult:
        if isinstance(cmd, str):
            raise TypeError("HardenedGitRunner n'exécute que de l'argv git, jamais une chaîne shell")
        argv = list(cmd)
        if not argv:
            return self._refused("commande vide")
        git_bin, rest = argv[0], argv[1:]
        leading: List[str] = []
        index = 0
        while index + 1 < len(rest) and rest[index] == "-c":
            leading += rest[index : index + 2]
            index += 2
        subcommand = rest[index:]
        if not subcommand or subcommand[0].startswith("-"):
            return self._refused(f"option globale git non autorisée: {subcommand[:1]}")

        workspace = os.path.abspath(workspace)
        if not os.path.isdir(workspace):
            return SandboxResult(
                exit_code=COMMAND_NOT_FOUND_EXIT_CODE,
                stdout="",
                stderr=f"[git-boundary] répertoire introuvable: {workspace}",
            )
        extra: Dict[str, str] = {}
        lock_dir: Optional[str] = None
        try:
            control = locate_control(workspace)
        except WorkspaceError as exc:
            return self._refused(str(exc))
        if control is not None:
            extra = {"GIT_DIR": control, "GIT_WORK_TREE": os.path.realpath(workspace)}
            lock_dir = control
        else:
            if subcommand[0] != "clone":
                problem = plain_git_dir_problem(workspace, git_bin=git_bin)
                if problem:
                    return self._refused(problem)
            extra = {"GIT_CEILING_DIRECTORIES": os.path.dirname(workspace)}
        final = [git_bin, *leading, *_config_args(), *subcommand]
        return _as_result(
            _run_raw(
                final,
                cwd=workspace,
                env=hardened_env(extra),
                timeout=self.timeout,
                max_output_bytes=self.max_output_bytes,
                lock_dir=lock_dir,
            )
        )


def default_git_runner() -> HardenedGitRunner:
    """Runner git par défaut pour un clone/workspace créé par l'hôte."""
    return HardenedGitRunner()
