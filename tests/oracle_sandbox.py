"""Sandbox de test qui EXÉCUTE réellement la commande d'oracle (pytest du venv), sans Docker.

La commande produite par ``collegue.executor.oracle`` cible ``/workspace`` et ``/tmp`` du conteneur : ce double
remplace le chemin du workspace par le répertoire réel, lance la commande avec ``sh -c`` en mettant le python des
tests en tête du ``PATH``, et renvoie un vrai :class:`SandboxResult`. Aucun rapport n'est fabriqué : c'est le lanceur
de production qui l'émet, l'hôte qui le juge.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys

from collegue.sandbox import SandboxResult


class LocalOracleSandbox:
    def __init__(self, *, timeout: float = 120.0, fallback=None):
        self.timeout = timeout
        self.commands = []
        self.workspaces = []
        self.fallback = fallback  # sandbox de repli pour les commandes qui ne sont PAS un oracle (pytest du gate…)

    def run_tests(self, workspace, command="pytest -q"):
        self.commands.append(command)
        self.workspaces.append(workspace)
        if "COLLEGUE-ORACLE" not in command and self.fallback is not None:
            return self.fallback.run_tests(workspace, command)
        local = command.replace("/workspace", str(workspace))
        env = dict(os.environ)
        env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
        proc = subprocess.Popen(
            ["sh", "-c", local],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(workspace),
            start_new_session=True,  # groupe propre : un délai ne laisse aucun enfant derrière lui
        )
        try:
            stdout, stderr = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
            return SandboxResult(exit_code=124, stdout=stdout or "", stderr=stderr or "", timed_out=True)
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # filet : jamais de survivant du lanceur
            except ProcessLookupError:
                pass
        return SandboxResult(exit_code=proc.returncode, stdout=stdout, stderr=stderr)
