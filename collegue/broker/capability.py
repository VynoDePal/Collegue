"""Preuve de capacité du transport courtier — établie sur l'objet RÉELLEMENT instancié, jamais déclarée par une chaîne.

Un agent qui affiche ``budget_enforcement = "broker"`` n'est pas pour autant admis : l'allocation du worker rappelle
:func:`prove_worker_transport` sur son sandbox réel et refuse si une seule vérification échoue. La preuve :

* construit l'argv ``docker run`` EXACT que le sandbox utiliserait (``_build_run_argv``, pur, sans Docker) pour un sandbox
  raccordé à un socket de session — réel (un vrai socket Unix est lié dans un répertoire privé) ;
* y contrôle : réseau ``none``, montage du socket unique et en lecture seule, aucun autre montage hôte que le workspace
  (et, optionnel, le cache pip), aucune variable de clé fournisseur / de proxy, aucune valeur égale à une clé fournisseur
  connue, aucun ``--env-file``, aucun privilège ni partage d'espace de noms hôte ;
* n'exige NI clé NI inférence : les clés éventuellement fournies ne servent qu'à vérifier qu'elles sont ABSENTES du sandbox.

La preuve ne contient jamais de secret (les valeurs ne sont ni stockées ni affichées).
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
from dataclasses import dataclass
from typing import List, Optional, Tuple

TRANSPORT = "budget_broker"
_ALLOWED_MOUNT_TARGETS = ("/workspace", "/run/collegue-broker", "/tmp/.pip_cache")
_FORBIDDEN_FLAGS = ("--env-file", "--privileged", "--cap-add", "--add-host", "--device", "--volumes-from")
_FORBIDDEN_NAMESPACES = ("--pid", "--ipc", "--uts", "--userns")


@dataclass(frozen=True)
class TransportCheck:
    """Une vérification. ``required=False`` : information (ex. présence d'une clé), qui n'entre pas dans ``ok``."""

    name: str
    ok: bool
    detail: str = ""
    required: bool = True


@dataclass(frozen=True)
class TransportProof:
    """Résultat d'un contrôle de transport ; ``ok`` seulement si TOUTES les vérifications passent."""

    transport: str
    checks: Tuple[TransportCheck, ...]

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(check.ok for check in self.checks if check.required)

    @property
    def failures(self) -> List[str]:
        return [f"{c.name}: {c.detail}" if c.detail else c.name for c in self.checks if c.required and not c.ok]

    def to_dict(self) -> dict:
        return {
            "transport": self.transport,
            "ok": self.ok,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail, "required": c.required} for c in self.checks],
        }


def _mounts(argv: List[str]) -> List[str]:
    return [argv[i + 1] for i, token in enumerate(argv[:-1]) if token == "-v"]


def _env_pairs(argv: List[str]) -> List[Tuple[str, Optional[str]]]:
    pairs: List[Tuple[str, Optional[str]]] = []
    for i, token in enumerate(argv[:-1]):
        if token == "-e":
            name, sep, value = argv[i + 1].partition("=")
            pairs.append((name, value if sep else None))
    return pairs


def prove_worker_transport(
    sandbox: object, *, provider_keys: Tuple[str, ...] = (), session_dir: Optional[str] = None
) -> TransportProof:
    """Prouve que ``sandbox`` (un ``DockerSandbox`` réel) raccordé au courtier isole bien le worker.

    ``session_dir`` : répertoire du socket d'une session RÉELLE (contrôle de lancement). Absent : un socket de sonde est lié
    dans un répertoire privé jetable (préflight / allocation, avant toute session).
    """
    checks: List[TransportCheck] = []

    def add(name: str, ok: bool, detail: str = "") -> bool:
        checks.append(TransportCheck(name, bool(ok), "" if ok else detail))
        return bool(ok)

    attach = getattr(sandbox, "with_broker", None)
    if not add(
        "sandbox_supports_broker",
        callable(attach),
        f"{type(sandbox).__name__} n'a pas with_broker : raccordement impossible",
    ):
        return TransportProof(TRANSPORT, tuple(checks))
    probe_dir = None
    probe_ws = tempfile.mkdtemp(prefix="cbw-")
    sock = None
    try:
        directory = session_dir
        if directory is None:
            probe_dir = tempfile.mkdtemp(prefix="cbp-")
            sock = socket.socket(socket.AF_UNIX)
            sock.bind(os.path.join(probe_dir, "broker.sock"))
            directory = probe_dir
        try:
            attached = attach(directory)
            argv = attached._build_run_argv(["true"], probe_ws, name="collegue-broker-probe")
        except Exception as exc:  # noqa: BLE001 - SandboxRefused (réseau, env, montage…) : la capacité n'est pas établie
            add("sandbox_accepts_broker_mount", False, f"{type(exc).__name__}: {exc}")
            return TransportProof(TRANSPORT, tuple(checks))
        add("sandbox_accepts_broker_mount", True)

        index = argv.index("--network") if "--network" in argv else -1
        add("network_none", index >= 0 and argv[index + 1] == "none", "le worker doit tourner avec --network none")

        mounts = _mounts(argv)
        targets = [m.split(":")[1] if m.count(":") >= 1 else m for m in mounts]
        broker_mounts = [m for m in mounts if ":/run/collegue-broker" in m]
        add(
            "single_readonly_socket_mount",
            len(broker_mounts) == 1 and broker_mounts[0].endswith(":ro"),
            f"montage(s) du courtier : {len(broker_mounts)}",
        )
        add(
            "no_other_host_mount",
            all(t in _ALLOWED_MOUNT_TARGETS for t in targets),
            f"cibles de montage inattendues : {[t for t in targets if t not in _ALLOWED_MOUNT_TARGETS]}",
        )
        flags = [t for t in argv if t.split("=")[0] in _FORBIDDEN_FLAGS or t.split("=")[0] in _FORBIDDEN_NAMESPACES]
        add("no_privilege_envfile_or_host_namespace", not flags, f"options interdites : {flags}")

        pairs = _env_pairs(argv)
        names = {name.upper() for name, _ in pairs}
        bad_names = sorted(
            names
            & {"GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
        )
        add("no_provider_key_or_proxy_variable", not bad_names, f"variables interdites : {bad_names}")

        secret_values = []
        for value in getattr(attached, "_env_secrets", {}).values():
            reveal = getattr(value, "get_secret_value", None)
            secret_values.append(str(reveal() if callable(reveal) else value))
        explicit = [value for _, value in pairs if value]
        leaked = False
        for key in (k for k in provider_keys if k):
            if any(key in token for token in argv) or key in secret_values or key in explicit:
                leaked = True
        add(
            "provider_key_absent_from_sandbox",
            not leaked,
            "la clé fournisseur figure dans l'argv ou l'environnement du sandbox",
        )
        add("passthrough_empty", not getattr(attached, "env_passthrough", ()), "env_passthrough non vide")
        return TransportProof(TRANSPORT, tuple(checks))
    finally:
        if sock is not None:
            sock.close()
        for path in (probe_dir, probe_ws):
            if path:
                shutil.rmtree(path, ignore_errors=True)
