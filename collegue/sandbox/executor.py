"""Exécuteur de code en sandbox Docker isolé (C8, brief §6).

Garde-fou critique : le code généré n'est **jamais** exécuté sur l'hôte. Toute
commande tourne dans un conteneur Docker éphémère, durci, qui ne monte **que** le
workspace du projet — pas le reste du système de fichiers hôte.

Durcissement appliqué :
- réseau coupé (``--network none``), capabilities droppées (``--cap-drop ALL``),
  ``no-new-privileges``, root FS en lecture seule (``--read-only`` + ``/tmp`` tmpfs) ;
- **refus de tourner en root** (sinon le code non fiable s'exécuterait en uid 0) ;
- **validation du workspace** : la racine du FS / un chemin avec ``:`` sont refusés ;
  ``workspace_root`` confine les workspaces autorisés (recommandé en Phase 3) ;
- **conteneur nommé + kill au timeout** : un ``docker run`` qui dépasse le délai ne
  laisse pas de conteneur orphelin (tuer le client ne tue pas le conteneur) ;
- **sortie bornée** : stdout/stderr vont sur disque puis sont relus avec un plafond,
  pour qu'une commande hostile ne fasse pas exploser la mémoire du parent.

Architecture testable : la **construction** de la commande ``docker run`` est pure
(testée sans Docker) ; l'**exécution** est testée par mock de subprocess en CI et
vérifiée pour de vrai par les tests ``integration`` (isolation FS hôte + persistance).

Durcissement différé (Phase 3, à la mise en service) : profil seccomp explicite,
user-namespace remap, quota de taille sur le workspace, image ``collegue-sandbox``
pinnée par digest + Node via NodeSource + scan d'image. Voir aussi le piège DinD :
si l'exécuteur appelle un daemon Docker distant/hôte, les chemins ``workspace`` sont
résolus par CE daemon — ``workspace_root`` doit alors pointer un chemin commun.

Module **isolé** : non câblé au runtime (l'intégration OpenHands comme worker est
différée en Phase 2 ; le pilote Phase 3 câblera cet exécuteur).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from typing import List, Mapping, Optional, Tuple, Union

DEFAULT_SANDBOX_IMAGE = "collegue-sandbox:latest"

# Code de sortie conventionnel pour un dépassement de délai (cf. coreutils timeout).
TIMEOUT_EXIT_CODE = 124

# Préfixe de la note ajoutée à stderr quand le conteneur est tué au timeout —
# consommé par le moteur (#461 : classification infra ; #464 : usage perdu).
TIMEOUT_NOTE = "[sandbox] délai dépassé après"


class SandboxUnavailable(RuntimeError):
    """Docker indisponible, ou refus de s'exécuter (ex. en root)."""


@dataclass
class SandboxResult:
    """Résultat d'une exécution en sandbox."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class DockerSandbox:
    """Exécute des commandes dans un conteneur Docker isolé et durci.

    Isolation (AC#1 « ne peut pas lire le FS hôte ») : seul ``workspace`` est monté
    (sur ``/workspace``) ; aucun autre chemin hôte n'est exposé. Persistance (AC#2) :
    ``workspace`` est un répertoire réel réutilisé d'une exécution à l'autre.
    """

    def __init__(
        self,
        image: str = DEFAULT_SANDBOX_IMAGE,
        *,
        network: str = "none",
        dns: Optional[Tuple[str, ...]] = None,
        memory: str = "512m",
        cpus: str = "1.0",
        pids_limit: int = 256,
        timeout: float = 120.0,
        max_output_bytes: int = 10 * 1024 * 1024,
        workspace_root: Optional[str] = None,
        allow_root: bool = False,
        docker_bin: str = "docker",
        env: Optional[Mapping[str, str]] = None,
        env_passthrough: Tuple[str, ...] = (),
        read_only: bool = True,
    ):
        self.image = image
        self.network = network
        # Résolveurs DNS explicites (#485) : le résolveur Docker par défaut
        # produit des « Temporary failure in name resolution » en rafale sur
        # les passes réseau du gate (#414/#439) — tentatives brûlées sur de
        # l'infra. Vide (défaut) = comportement Docker inchangé. Une adresse
        # invalide fait échouer `docker run` (stderr visible dans le gate).
        self.dns = tuple(dns or ())
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes
        self.workspace_root = workspace_root
        self.allow_root = allow_root
        self.docker_bin = docker_bin
        # Injection d'environnement (ex. worker OpenHands) :
        # - ``env`` : couples explicites (-e K=V) — pour les valeurs NON secrètes.
        # - ``env_passthrough`` : noms passés par référence (-e NAME, sans valeur) —
        #   docker hérite la valeur de l'environnement du process appelant, donc le
        #   secret (ex. LLM_API_KEY) n'apparaît JAMAIS dans l'argv (ni dans `ps`).
        # ``read_only`` : root FS en lecture seule (durci) ; désactivable pour les
        #   workers qui écrivent hors workspace/tmp (au prix d'un durcissement moindre).
        self.env = dict(env) if env else {}
        self.env_passthrough = tuple(env_passthrough)
        self.read_only = bool(read_only)

    # ── validation / construction (pur, testable sans Docker) ─────────────────────

    def _validate_workspace(self, workspace: str) -> str:
        """Résout et valide le workspace. Lève ``ValueError`` si dangereux.

        Refuse : la racine du FS, un chemin contenant ``:`` (délimiteur ``-v``), et —
        si ``workspace_root`` est défini — tout chemin hors de cette racine
        (``realpath`` pour déjouer les symlinks).
        """
        ws = os.path.realpath(os.path.abspath(workspace))
        if ":" in ws:
            raise ValueError(f"workspace invalide (contient ':'): {ws}")
        if ws == os.path.sep:
            raise ValueError("workspace invalide : la racine du FS ne peut pas être montée")
        if self.workspace_root is not None:
            root = os.path.realpath(os.path.abspath(self.workspace_root))
            if os.path.commonpath([ws, root]) != root:
                raise ValueError(f"workspace hors du répertoire autorisé {root}: {ws}")
        return ws

    def _build_run_argv(self, cmd: Union[str, List[str]], workspace: str, name: Optional[str] = None) -> List[str]:
        """Construit l'argv ``docker run`` durci. Pure (testable sans Docker)."""
        ws = os.path.abspath(workspace)
        inner = ["sh", "-c", cmd] if isinstance(cmd, str) else list(cmd)
        argv = [self.docker_bin, "run", "--rm"]
        if name:
            argv += ["--name", name]
        argv += [
            "--network",
            self.network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.pids_limit),
            "--memory",
            self.memory,
            "--cpus",
            self.cpus,
            "--stop-timeout",
            "5",
        ]
        # #485 : résolveurs DNS explicites — uniquement si configurés (argv par
        # défaut strictement inchangé).
        for server in self.dns:
            argv += ["--dns", server]
        if self.read_only:
            argv += ["--read-only"]  # root FS en lecture seule
        argv += [
            "--tmpfs",
            # Scratch éphémère écrivable. ``exec`` EXPLICITE (#454) : les défauts
            # docker (`noexec,nosuid,nodev`) rendent inimportable tout module natif
            # (.so) installé sous /tmp — or ``HOME=/tmp`` y envoie `pip --user`
            # (#414) et la passe d'installabilité y crée son venv (#439) :
            # `failed to map segment from shared object`, gate rouge structurel.
            # ``noexec`` n'est PAS une frontière de sécurité ici : ce conteneur
            # exécute déjà délibérément le code du projet (pytest, npm). On garde
            # ``nosuid``/``nodev``.
            "/tmp:exec,nosuid,nodev",
            "-e",
            "HOME=/tmp",
        ]
        # Injection d'environnement (ordre déterministe : passthrough triés, puis
        # couples explicites triés) — pour le worker OpenHands (clé API par réf.,
        # modèle/flags en clair). Vide par défaut → argv identique au comportement
        # historique (tests inchangés).
        for name in sorted(self.env_passthrough):
            argv += ["-e", name]
        for key in sorted(self.env):
            argv += ["-e", f"{key}={self.env[key]}"]
        # Exécuter en tant qu'utilisateur hôte non-root : le workspace (dir hôte)
        # reste écrivable/persisté, et le conteneur ne tourne jamais en root
        # (run_command refuse uid 0 sauf allow_root explicite).
        if hasattr(os, "getuid"):
            argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
        argv += [
            "-v",
            f"{ws}:/workspace",  # SEUL chemin hôte monté
            "-w",
            "/workspace",
            self.image,
            *inner,
        ]
        return argv

    # ── exécution ─────────────────────────────────────────────────────────────────

    def _read_capped(self, path: str) -> str:
        """Relit un fichier de sortie avec un plafond (évite l'OOM du parent)."""
        with open(path, "rb") as f:
            data = f.read(self.max_output_bytes + 1)
        text = data[: self.max_output_bytes].decode("utf-8", errors="replace")
        if len(data) > self.max_output_bytes:
            text += f"\n[sandbox] sortie tronquée à {self.max_output_bytes} octets"
        return text

    def _kill_container(self, name: str) -> None:
        """Tue un conteneur par nom (best-effort) — pour ne pas laisser d'orphelin."""
        try:
            subprocess.run([self.docker_bin, "kill", name], capture_output=True, timeout=15)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

    def run_command(self, cmd: Union[str, List[str]], workspace: str) -> SandboxResult:
        """Exécute ``cmd`` dans le sandbox, workspace monté sur ``/workspace``.

        ``cmd`` peut être une chaîne (``sh -c`` dans le conteneur) ou un argv (liste).
        Lève :class:`SandboxUnavailable` si Docker est absent ou si l'on tourne en
        root sans ``allow_root``. Lève ``ValueError`` si le workspace est invalide.
        """
        if not self.allow_root and hasattr(os, "getuid") and os.getuid() == 0:
            raise SandboxUnavailable(
                "refus de lancer le sandbox en root (le code non fiable tournerait en uid 0) ; "
                "configurer allow_root=True en connaissance de cause."
            )
        ws = self._validate_workspace(workspace)
        os.makedirs(ws, exist_ok=True)
        name = f"collegue-sbx-{uuid.uuid4().hex[:12]}"
        argv = self._build_run_argv(cmd, ws, name=name)

        out_f = tempfile.NamedTemporaryFile(prefix="sbx-out-", delete=False)
        err_f = tempfile.NamedTemporaryFile(prefix="sbx-err-", delete=False)
        out_path, err_path = out_f.name, err_f.name
        timed_out = False
        try:
            try:
                proc = subprocess.run(argv, stdout=out_f, stderr=err_f, timeout=self.timeout)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                # Tuer le client ne tue pas le conteneur → on le tue par nom.
                self._kill_container(name)
                exit_code = TIMEOUT_EXIT_CODE
                timed_out = True
            except FileNotFoundError as exc:
                raise SandboxUnavailable(f"Binaire Docker introuvable: {self.docker_bin}") from exc
            finally:
                out_f.close()
                err_f.close()

            stdout = self._read_capped(out_path)
            stderr = self._read_capped(err_path)
            if timed_out:
                stderr += f"\n{TIMEOUT_NOTE} {self.timeout:g}s"
            return SandboxResult(exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=timed_out)
        finally:
            for path in (out_path, err_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def run_tests(self, workspace: str, command: Union[str, List[str]] = "pytest -q") -> SandboxResult:
        """Lance la suite de tests d'un projet dans le sandbox (défaut : ``pytest -q``)."""
        return self.run_command(command, workspace)

    def is_available(self) -> bool:
        """True si le binaire Docker répond (``docker version``)."""
        try:
            proc = subprocess.run(
                [self.docker_bin, "version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False
        return proc.returncode == 0
