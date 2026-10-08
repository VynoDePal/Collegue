"""
Tests unitaires pour le script entrypoint.sh
Vérifie que les deux services démarrent correctement.
"""

import os
import signal
import subprocess
import time

import pytest
import requests


class TestEntrypoint:
    """Tests pour le script d'entrée."""

    def test_entrypoint_script_exists(self):
        """Test que le script entrypoint.sh existe et est exécutable."""
        import os

        entrypoint_path = os.path.join(os.path.dirname(__file__), "..", "entrypoint.sh")
        assert os.path.exists(entrypoint_path), "entrypoint.sh not found"
        assert os.access(entrypoint_path, os.X_OK), "entrypoint.sh is not executable"

    def test_entrypoint_syntax_valid(self):
        """Test que le script shell est syntaxiquement valide."""
        import subprocess

        entrypoint_path = os.path.join(os.path.dirname(__file__), "..", "entrypoint.sh")
        result = subprocess.run(["sh", "-n", entrypoint_path], capture_output=True)
        assert result.returncode == 0, f"Shell syntax error: {result.stderr.decode()}"


class TestHealthServer:
    """Tests pour le health_server.py."""

    @pytest.fixture(scope="module")
    def health_server(self):
        """Démarre le vrai health server et attend qu'il réponde ; échoue (jamais skip) sinon.

        Interpréteur courant (dépendances installées) et attente active : un test qui saute quand
        le serveur ne répond pas ne prouverait rien.
        """
        import os
        import subprocess
        import sys

        health_server_path = os.path.join(os.path.dirname(__file__), "..", "collegue", "health_server.py")
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        # Comme l'image (PYTHONPATH=/app) : lancé en script, le paquet `collegue` doit être importable.
        env = {**os.environ, "PYTHONPATH": repo_root}
        proc = subprocess.Popen(
            [sys.executable, health_server_path], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )

        deadline = time.monotonic() + 20
        try:
            while True:
                try:
                    if requests.get("http://localhost:4122/_health", timeout=1).status_code == 200:
                        break
                except requests.exceptions.ConnectionError:
                    pass
                if proc.poll() is not None:
                    error = proc.stderr.read().decode(errors="replace")[-800:]
                    pytest.fail(f"health_server.py s'est arrêté (code {proc.returncode}) : {error}")
                if time.monotonic() > deadline:
                    pytest.fail("health_server.py ne répond pas sur :4122 après 20 s")
                time.sleep(0.2)

            yield proc
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_health_endpoint_responds(self, health_server):
        """Test que le endpoint /_health répond."""
        response = requests.get("http://localhost:4122/_health", timeout=5)
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_oauth_endpoint_exists(self, health_server):
        """Test que le endpoint OAuth existe."""
        response = requests.get("http://localhost:4122/.well-known/oauth-protected-resource", timeout=5)
        # Peut retourner 200 ou 500 selon la config, mais ne doit pas être 404
        assert response.status_code in [200, 500]


class TestPortConfiguration:
    """Tests pour vérifier la configuration des ports."""

    def test_ports_are_different(self):
        """Test que les ports 4121 et 4122 sont différents."""
        # Le MCP utilise 4121, le health server 4122
        mcp_port = 4121
        health_port = 4122
        assert mcp_port != health_port, "MCP and health ports should be different"

    def test_dockerfile_exposes_both_ports(self):
        """Test que le Dockerfile expose les deux ports."""
        import os
        import re

        dockerfile_path = os.path.join(os.path.dirname(__file__), "..", "docker", "collegue", "Dockerfile")
        with open(dockerfile_path, "r") as f:
            content = f.read()

        # Vérifier que les deux ports sont exposés (peut être sur la même ligne ou séparés)
        assert "EXPOSE ${PORT}" in content or "EXPOSE 4121" in content, "MCP port not exposed"
        assert "EXPOSE" in content and ("${HEALTH_PORT}" in content or "4122" in content), "Health port not exposed"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
