"""Exécution de commandes pour l'exécuteur (E2, epic #362).

Deux backends partagent une même interface (:class:`CommandRunner`) — un
``run_command(cmd, workspace) -> SandboxResult`` :

- :class:`~collegue.sandbox.executor.DockerSandbox` (C8) : exécution **isolée**,
  pour le code non fiable (agent OpenHands, suite de tests) et, en
  ``integration``, la plomberie git sur un dépôt potentiellement hostile.
- :class:`LocalCommandRunner` : exécution **locale** (hôte), **sans isolation**,
  réservée à la CI sans Docker pour de la **plomberie git sur un dépôt de
  confiance / fixture**. Jamais pour exécuter du code non fiable.

``DockerSandbox`` satisfait déjà ce protocole — on réutilise donc le même
:class:`SandboxResult` ici, et l'exécuteur (E2+) est paramétré par le runner.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import List, Optional, Protocol, Union, runtime_checkable

from collegue.sandbox.executor import TIMEOUT_EXIT_CODE, SandboxResult

# Code de sortie conventionnel quand le binaire est introuvable (cf. shell 127).
COMMAND_NOT_FOUND_EXIT_CODE = 127


@runtime_checkable
class CommandRunner(Protocol):
    """Tout objet capable d'exécuter une commande dans un workspace.

    Contrat identique à ``DockerSandbox.run_command`` afin que le sandbox durci et
    le runner local soient interchangeables.
    """

    def run_command(self, cmd: Union[str, List[str]], workspace: str) -> SandboxResult: ...


class LocalCommandRunner:
    """Exécute des commandes EN LOCAL (hôte), sans aucune isolation.

    Destiné à la **plomberie git** (clone, diff…) sur un dépôt de confiance en CI,
    là où Docker n'est pas disponible. **Ne jamais** y exécuter du code non fiable
    (agent, tests) : utiliser le :class:`DockerSandbox`.

    La sortie est **bornée** (écrite sur disque puis relue avec un plafond), comme
    le sandbox, pour qu'une sortie pathologique ne fasse pas exploser la mémoire.
    """

    def __init__(self, *, timeout: float = 120.0, max_output_bytes: int = 10 * 1024 * 1024):
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes

    def _read_capped(self, path: str) -> str:
        with open(path, "rb") as handle:
            data = handle.read(self.max_output_bytes + 1)
        text = data[: self.max_output_bytes].decode("utf-8", errors="replace")
        if len(data) > self.max_output_bytes:
            text += f"\n[local] sortie tronquée à {self.max_output_bytes} octets"
        return text

    def run_command(self, cmd: Union[str, List[str]], workspace: str) -> SandboxResult:
        argv = ["sh", "-c", cmd] if isinstance(cmd, str) else list(cmd)
        out_f = tempfile.NamedTemporaryFile(prefix="local-out-", delete=False)
        err_f = tempfile.NamedTemporaryFile(prefix="local-err-", delete=False)
        out_path, err_path = out_f.name, err_f.name
        timed_out = False
        try:
            try:
                proc = subprocess.run(argv, cwd=workspace, stdout=out_f, stderr=err_f, timeout=self.timeout)
                exit_code: Optional[int] = proc.returncode
            except subprocess.TimeoutExpired:
                exit_code = TIMEOUT_EXIT_CODE
                timed_out = True
            except FileNotFoundError:
                exit_code = COMMAND_NOT_FOUND_EXIT_CODE
            finally:
                out_f.close()
                err_f.close()

            stdout = self._read_capped(out_path)
            stderr = self._read_capped(err_path)
            if exit_code == COMMAND_NOT_FOUND_EXIT_CODE:
                stderr = (stderr + f"\n[local] binaire introuvable: {argv[0]}").strip()
            if timed_out:
                stderr += f"\n[local] délai dépassé après {self.timeout:g}s"
            return SandboxResult(exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=timed_out)
        finally:
            for path in (out_path, err_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
