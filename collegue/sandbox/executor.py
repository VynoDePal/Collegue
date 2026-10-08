"""Exécuteur de code en sandbox Docker isolé (C8, brief §6).

Garde-fou critique : le code généré n'est **jamais** exécuté sur l'hôte. Toute
commande tourne dans un conteneur Docker éphémère, durci, qui ne monte que le
workspace du projet — plus, OPT-IN et validés, un cache pip et les creds d'abonnement —
pas le reste du système de fichiers hôte. Aucun de ces montages n'expose jamais un
répertoire de contrôle Git (``git_control_exposure``).

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

import errno
import math
import os
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from typing import List, Mapping, Optional, Tuple, Union

DEFAULT_SANDBOX_IMAGE = "collegue-sandbox:latest"
# #496 : point de montage du cache pip persistant dans le conteneur.
SANDBOX_PIP_CACHE_MOUNT = "/tmp/.pip_cache"
# Sous-chemin (relatif au HOME du worker) où OpenHands lit/écrit ses creds
# d'abonnement (Codex/ChatGPT, subscription_login). La cible RÉELLE du montage est
# dérivée du HOME effectif du conteneur (``env['HOME']``), pas codée en dur : les
# creds NE peuvent PAS vivre sous /tmp (le ``--tmpfs /tmp`` les masquerait), d'où
# le fail-loud si HOME pointe sous /tmp (cf. _build_run_argv).
SANDBOX_OPENHANDS_AUTH_SUBPATH = ".openhands"

# Marqueur posé dans le répertoire de contrôle Git d'un workspace (frontière Git,
# ``collegue.executor.git_boundary``). Ce répertoire — hooks/config/refs/index,
# référence de base HEAD — est l'AUTORITÉ des opérations Git hôte ; il ne doit
# JAMAIS être monté dans un conteneur (l'agent et les tests y écriraient). Le
# sandbox refuse donc tout montage qui l'expose, à toute profondeur (cf. git_control_exposure).
GIT_CONTROL_MARKER = ".collegue-git-control"
# Le répertoire de contrôle d'un workspace ``/x/workspace`` est ``/x/workspace.control``.
GIT_CONTROL_SUFFIX = ".control"

# Bornes du parcours qui vérifie qu'un montage n'expose aucun répertoire de contrôle (voir
# ``git_control_exposure``). Au-delà, le montage est REFUSÉ (fail-closed), jamais autorisé.
# DIRS borne à la fois les répertoires parcourus ET la file en attente (une file qui grossit
# sans borne serait un parcours sans limite) ; ENTRIES borne chaque entrée itérée, fichiers compris.
GIT_CONTROL_SCAN_MAX_DIRS = 250_000
GIT_CONTROL_SCAN_MAX_ENTRIES = 1_000_000
GIT_CONTROL_SCAN_MAX_DEPTH = 64

# Code de sortie conventionnel pour un dépassement de délai (cf. coreutils timeout).
TIMEOUT_EXIT_CODE = 124
# Auto-limitation du conteneur sous allocation (secondes) : délai entre TERM et KILL, et marge hôte.
SELF_LIMIT_KILL_AFTER = 15
SELF_LIMIT_HOST_MARGIN = 30

# Préfixe de la note ajoutée à stderr quand le conteneur est tué au timeout —
# consommé par le moteur (#461 : classification infra ; #464 : usage perdu).
TIMEOUT_NOTE = "[sandbox] délai dépassé après"


class SandboxUnavailable(RuntimeError):
    """Docker indisponible, ou refus de s'exécuter (ex. en root)."""


class SandboxRefused(ValueError):
    """Le sandbox a REFUSÉ la commande AVANT tout lancement (montage/chemin invalide).

    Sous-classe de ``ValueError`` (compatibilité) : distingue un refus de validation — aucune
    dépense possible, la réservation budgétaire peut être libérée — d'une ``ValueError`` quelconque
    levée plus tard par l'appelant.
    """


def git_control_reason(path: str) -> Optional[str]:
    """Raison pour laquelle ``path`` n'est PAS un checkout de confiance, ``None`` sinon.

    Un workspace géré (répertoire de contrôle frère) a pu être monté en RW et exécuté :
    il est écrit par du code non fiable ; un répertoire de contrôle est l'autorité de la
    frontière Git. Ni l'un ni l'autre ne doit servir de source de clone ni recevoir de
    commande git hôte hors de la frontière (``collegue.executor.git_boundary``).
    Pure : deux ``lexists``, aucune lecture git.
    """
    workspace = os.path.abspath(os.fspath(path))
    if os.path.lexists(os.path.join(workspace, GIT_CONTROL_MARKER)):
        return "répertoire de contrôle Git (autorité de la frontière, jamais une source ni un cwd)"
    if os.path.lexists(os.path.join(workspace + GIT_CONTROL_SUFFIX, GIT_CONTROL_MARKER)):
        return "workspace géré : écrit par du code non fiable, git hôte réservé à la frontière"
    return None


# Seules ces erreurs ÉTABLISSENT une absence (le chemin n'existe pas, ou un composant n'est pas un
# répertoire). Toute autre (EACCES, EPERM, EIO, ELOOP, ETIMEDOUT…) empêche d'exclure un contrôle.
_ABSENCE_ERRNOS = (errno.ENOENT, errno.ENOTDIR)


def _lstat_or_none(path: str):
    """``os.lstat`` ; ``None`` UNIQUEMENT si l'absence est établie. Toute autre erreur est propagée.

    ``os.path.lexists`` / ``isdir`` ne conviennent pas ici : ils transforment ``PermissionError`` en
    « absent », ce qui ferait passer pour inexistant un contrôle sous un parent non traversable alors
    que le démon Docker, lui, résout le bind avec ses propres privilèges.
    """
    try:
        return os.lstat(path)
    except OSError as exc:
        if exc.errno in _ABSENCE_ERRNOS:
            return None
        raise


def _resolve_mount_path(path: str) -> tuple:
    """``(chemin canonique, existe)`` d'un chemin de montage, ou ``OSError`` si indécidable.

    Résolution STRICTE des alias/liens (``realpath(strict=True)``) du plus long préfixe existant, puis
    ajout du reste inexistant (workspace ou cache « à créer »). Une absence n'est admise que si
    ``lstat`` l'établit (ENOENT/ENOTDIR) ; un lien pendant (cible absente, donc susceptible de devenir un
    contrôle), une boucle de liens ou toute erreur d'accès propagent l'erreur ⇒ refus.
    """
    current = os.path.abspath(os.fspath(path))
    remainder: list = []
    while True:
        try:
            resolved = os.path.realpath(current, strict=True)
            break
        except OSError as exc:
            if exc.errno not in _ABSENCE_ERRNOS:
                raise
        if _lstat_or_none(current) is not None:
            raise OSError(errno.ELOOP, "lien symbolique pendant (cible absente)", current)
        parent = os.path.dirname(current)
        if parent == current:
            raise OSError(errno.ENOENT, "racine introuvable", current)
        remainder.append(os.path.basename(current))
        current = parent
    for name in reversed(remainder):
        resolved = os.path.join(resolved, name)
    return resolved, not remainder


def git_control_exposure(path: str) -> Optional[str]:
    """Raison pour laquelle monter ``path`` exposerait un répertoire de contrôle Git, ou ``None``.

    Garantie (valable pour TOUT bind mount : workspace, cache pip, creds d'abonnement, et le
    montage d'abonnement de ``sampling_ctx``) — un chemin est accepté seulement si, après
    canonicalisation (résolution stricte : alias, liens, ``..``, chemin relatif), AUCUN répertoire
    portant le marqueur de contrôle n'est :

    * ``path`` lui-même, ni l'un de ses ancêtres (monter l'intérieur d'un contrôle en exposerait
      une partie) ;
    * contenu dans ``path`` à une profondeur quelconque ≤ ``GIT_CONTROL_SCAN_MAX_DEPTH``.

    Le parcours est itératif, ne suit AUCUN lien symbolique (ni boucle, ni lien vers l'hôte), et
    borné de trois façons : répertoires parcourus ET file en attente ≤ ``GIT_CONTROL_SCAN_MAX_DIRS``,
    entrées itérées (fichiers compris, comptées à mesure) ≤ ``GIT_CONTROL_SCAN_MAX_ENTRIES``,
    profondeur ≤ ``GIT_CONTROL_SCAN_MAX_DEPTH``.

    **Une erreur n'est jamais une absence.** Seules ENOENT/ENOTDIR, établies par ``lstat``, rendent
    un chemin « à créer » (accepté : rien à exposer). Toute autre erreur de résolution, de ``lstat``
    ou de lecture — ``EACCES`` d'un parent non traversable (que le démon Docker, plus privilégié,
    franchirait), ``ELOOP``, lien pendant, ``EIO``… — ainsi que tout dépassement de borne ⇒ REFUS
    (« vérification impossible »). L'état vit sur le disque (marqueur) : le contrôle reste valable
    après reprise du processus. Un marqueur planté dans un arbre le fait refuser (faux positif
    assumé) et ne peut jamais autoriser quoi que ce soit.

    AUCUNE dispense pour un workspace « géré » : être apparié à SON contrôle frère ne prouve pas
    l'absence d'AUTRES contrôles dans l'arbre (``prepare_workspace(dest_root=<ws>/nested)`` en
    place un sous le montage). Le coût (≈ 0,06 s pour 30 000 répertoires) ne justifie pas une
    exception ; en contrepartie un arbre piégé par l'agent est refusé, fail-closed.
    """
    try:
        real, exists = _resolve_mount_path(path)
        current = real
        while True:  # soi-même et tous les ancêtres
            if _lstat_or_none(os.path.join(current, GIT_CONTROL_MARKER)) is not None:
                return f"{current} est (ou contient en ancêtre) un répertoire de contrôle Git"
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        if not exists or not stat.S_ISDIR(os.lstat(real).st_mode):
            return None  # absent (à créer, établi par lstat) ou fichier : rien à parcourir
        stack = [(real, 0)]
        visited = 0
        entries_seen = 0
        while stack:
            directory, depth = stack.pop()
            visited += 1
            if visited > GIT_CONTROL_SCAN_MAX_DIRS:
                return f"vérification impossible : plus de {GIT_CONTROL_SCAN_MAX_DIRS} répertoires sous {real}"
            if depth > GIT_CONTROL_SCAN_MAX_DEPTH:
                return f"vérification impossible : profondeur > {GIT_CONTROL_SCAN_MAX_DEPTH} sous {real}"
            with os.scandir(directory) as entries:
                for entry in entries:
                    entries_seen += 1
                    if entries_seen > GIT_CONTROL_SCAN_MAX_ENTRIES:
                        return f"vérification impossible : plus de {GIT_CONTROL_SCAN_MAX_ENTRIES} entrées sous {real}"
                    if entry.name == GIT_CONTROL_MARKER:
                        return f"{directory} est un répertoire de contrôle Git contenu dans le montage"
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((entry.path, depth + 1))
                        if len(stack) > GIT_CONTROL_SCAN_MAX_DIRS:
                            return (
                                f"vérification impossible : plus de {GIT_CONTROL_SCAN_MAX_DIRS} "
                                f"répertoires en attente sous {real}"
                            )
        return None
    except (OSError, ValueError) as exc:
        return f"vérification impossible ({type(exc).__name__}: {exc})"


def _refuse_if_git_control_exposed(path: str, label: str) -> None:
    reason = git_control_exposure(path)
    if reason is not None:
        raise SandboxRefused(
            f"{label} refusé : montage exposant les métadonnées Git de contrôle ({reason}) — "
            "monter uniquement le répertoire de travail ou un répertoire ordinaire, jamais un ancêtre "
            "ni le répertoire de contrôle"
        )


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

    Isolation (AC#1 « ne peut pas lire le FS hôte ») : ``workspace`` est monté (sur
    ``/workspace``) ; aucun autre chemin hôte n'est exposé — sauf, OPT-IN, un cache pip
    sur ``/tmp/.pip_cache`` si ``pip_cache_dir`` est fourni (#496) et les creds
    d'abonnement si ``subscription_auth_dir`` l'est. Les TROIS sources de montage passent
    par ``git_control_exposure`` : aucune n'expose le contrôle Git, à aucune profondeur.
    Persistance (AC#2) : ``workspace`` est un répertoire réel réutilisé d'une
    exécution à l'autre.
    """

    def __init__(
        self,
        image: str = DEFAULT_SANDBOX_IMAGE,
        *,
        network: str = "none",
        dns: Optional[Tuple[str, ...]] = None,
        pip_cache_dir: Optional[str] = None,
        subscription_auth_dir: Optional[str] = None,
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
        # Cache pip persistant opt-in (#496, partie 2 de #485). Monté sur
        # /tmp/.pip_cache + PIP_CACHE_DIR : les passes réseau du gate (#414/#439)
        # ne retéléchargent plus tout PyPI à chaque run (aléas DNS/timeout
        # réduits, temps mur amorti — jusqu'à 4 passes avec la remédiation #481).
        # Vide (défaut) = aucun montage, argv inchangé. Validé comme un -v.
        self.pip_cache_dir = pip_cache_dir or None
        # Creds d'abonnement (Codex via ChatGPT, subscription_login) montées en
        # LECTURE-ÉCRITURE (OpenHands rafraîchit le token et le réécrit → persistance
        # hors-run). Opt-in : vide (défaut) = aucun montage, argv inchangé. Validé
        # comme un -v (realpath + refus ':'/racine) ; hors confinement workspace_root.
        self.subscription_auth_dir = subscription_auth_dir or None
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
            raise SandboxRefused(f"workspace invalide (contient ':'): {ws}")
        if ws == os.path.sep:
            raise SandboxRefused("workspace invalide : la racine du FS ne peut pas être montée")
        if self.workspace_root is not None:
            root = os.path.realpath(os.path.abspath(self.workspace_root))
            if os.path.commonpath([ws, root]) != root:
                raise SandboxRefused(f"workspace hors du répertoire autorisé {root}: {ws}")
        _refuse_if_git_control_exposed(workspace, "workspace")  # chemin BRUT : un lien pendant doit rester visible
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
        # #496 : cache pip persistant — 2e montage hôte opt-in (cf. docstring
        # d'isolation). Le bind sur /tmp/.pip_cache MASQUE le tmpfs à ce
        # sous-chemin → les pages du cache ne comptent pas dans --memory. Validé
        # (realpath + refus ':'/racine) car il devient un -v ; le confinement
        # workspace_root NE s'applique PAS (le cache vit hors du workspace).
        if self.pip_cache_dir:
            cache = os.path.realpath(os.path.abspath(self.pip_cache_dir))
            if ":" in cache or cache == os.path.sep:
                raise SandboxRefused(f"pip_cache_dir invalide (':' ou racine): {cache}")
            _refuse_if_git_control_exposed(self.pip_cache_dir, "pip_cache_dir")
            argv += ["-v", f"{cache}:{SANDBOX_PIP_CACHE_MOUNT}", "-e", f"PIP_CACHE_DIR={SANDBOX_PIP_CACHE_MOUNT}"]
        # Creds d'abonnement (Codex/ChatGPT) : montage RW, cible fixe (HOME du worker).
        # Même validation que le cache pip ; hors confinement workspace_root (les creds
        # vivent hors du workspace). RW car OpenHands réécrit le token rafraîchi.
        if self.subscription_auth_dir:
            auth = os.path.realpath(os.path.abspath(self.subscription_auth_dir))
            if ":" in auth or auth == os.path.sep:
                raise SandboxRefused(f"subscription_auth_dir invalide (':' ou racine): {auth}")
            _refuse_if_git_control_exposed(self.subscription_auth_dir, "subscription_auth_dir")
            # Cible = $HOME/.openhands du worker. HOME effectif = override ``env['HOME']``
            # (sinon le défaut /tmp injecté plus bas). Les creds NE peuvent PAS vivre sous
            # /tmp (le ``--tmpfs /tmp`` masquerait le bind) → fail-loud, sinon le montage
            # serait inerte silencieusement (le coder ne verrait jamais l'auth abo).
            home = (self.env.get("HOME") or "/tmp").rstrip("/")
            if home == "/tmp" or home.startswith("/tmp/"):
                raise SandboxRefused(
                    "subscription_auth_dir exige env['HOME'] hors /tmp "
                    "(le tmpfs /tmp masquerait le montage des creds d'abonnement)"
                )
            argv += ["-v", f"{auth}:{home}/{SANDBOX_OPENHANDS_AUTH_SUBPATH}"]
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
            f"{ws}:/workspace",  # le workspace ; les montages OPT-IN (cache pip, creds) sont ajoutés plus haut
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

    def run_command(
        self, cmd: Union[str, List[str]], workspace: str, *, timeout: Optional[float] = None
    ) -> SandboxResult:
        """Exécute ``cmd`` dans le sandbox, workspace monté sur ``/workspace``.

        ``timeout`` (secondes, optionnel) remplace ``self.timeout`` pour CET appel — l'échéance d'une
        allocation budgétaire (vague 2). Au dépassement, le conteneur est tué PAR NOM : tuer le client
        ``docker`` ne tue pas le conteneur, qui continuerait à dépenser en arrière-plan.

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
        if timeout is not None and timeout > 0:
            # Échéance d'allocation : le conteneur S'AUTO-LIMITE (coreutils ``timeout`` : TERM puis KILL),
            # indépendamment du client ``docker`` — si le process hôte meurt, le conteneur ne dépense pas
            # indéfiniment en arrière-plan. Le délai hôte, un peu plus long, n'est qu'un filet.
            inner = ["sh", "-c", cmd] if isinstance(cmd, str) else list(cmd)
            cmd = ["timeout", "--signal=TERM", f"--kill-after={SELF_LIMIT_KILL_AFTER}", str(math.ceil(timeout)), *inner]
            effective_timeout = float(timeout) + SELF_LIMIT_KILL_AFTER + SELF_LIMIT_HOST_MARGIN
        else:
            effective_timeout = self.timeout
        argv = self._build_run_argv(cmd, ws, name=name)

        out_f = tempfile.NamedTemporaryFile(prefix="sbx-out-", delete=False)
        err_f = tempfile.NamedTemporaryFile(prefix="sbx-err-", delete=False)
        out_path, err_path = out_f.name, err_f.name
        timed_out = False
        try:
            try:
                proc = subprocess.run(argv, stdout=out_f, stderr=err_f, timeout=effective_timeout)
                exit_code = proc.returncode
                if timeout and exit_code in (TIMEOUT_EXIT_CODE, 137):  # auto-limite du conteneur atteinte
                    timed_out = True
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
                stderr += f"\n{TIMEOUT_NOTE} {(timeout if timeout else effective_timeout):g}s"
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
