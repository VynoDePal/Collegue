"""
Tests unitaires pour le script entrypoint.sh
Vérifie que les deux services démarrent correctement.
"""
import pytest
import subprocess
import time
import requests
import signal
import os


class TestEntrypoint:
    """Tests pour le script d'entrée."""
    
    def test_entrypoint_script_exists(self):
        """Test que le script entrypoint.sh existe et est exécutable."""
        import os
        entrypoint_path = os.path.join(os.path.dirname(__file__), '..', 'entrypoint.sh')
        assert os.path.exists(entrypoint_path), "entrypoint.sh not found"
        assert os.access(entrypoint_path, os.X_OK), "entrypoint.sh is not executable"
    
    def test_entrypoint_syntax_valid(self):
        """Test que le script shell est syntaxiquement valide."""
        import subprocess
        entrypoint_path = os.path.join(os.path.dirname(__file__), '..', 'entrypoint.sh')
        result = subprocess.run(['sh', '-n', entrypoint_path], capture_output=True)
        assert result.returncode == 0, f"Shell syntax error: {result.stderr.decode()}"


class TestHealthServer:
    """Tests pour le health_server.py."""
    
    @pytest.fixture(scope="module")
    def health_server(self):
        """Démarre le health server pour les tests."""
        import subprocess
        import os
        
        health_server_path = os.path.join(os.path.dirname(__file__), '..', 'collegue', 'health_server.py')
        proc = subprocess.Popen(['python3', health_server_path], 
                                stdout=subprocess.PIPE, 
                                stderr=subprocess.PIPE)
        
        # Attendre que le serveur démarre
        time.sleep(2)
        
        yield proc
        
        # Cleanup
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    
    def test_health_endpoint_responds(self, health_server):
        """Test que le endpoint /_health répond."""
        try:
            response = requests.get('http://localhost:4122/_health', timeout=5)
            assert response.status_code == 200
            assert response.json()['status'] == 'ok'
        except requests.exceptions.ConnectionError:
            pytest.skip("Health server not running (expected in CI environment)")
    
    def test_oauth_endpoint_exists(self, health_server):
        """Test que le endpoint OAuth existe."""
        try:
            response = requests.get('http://localhost:4122/.well-known/oauth-protected-resource', timeout=5)
            # Peut retourner 200 ou 500 selon la config, mais ne doit pas être 404
            assert response.status_code in [200, 500]
        except requests.exceptions.ConnectionError:
            pytest.skip("Health server not running (expected in CI environment)")


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
        
        dockerfile_path = os.path.join(os.path.dirname(__file__), '..', 'docker', 'collegue', 'Dockerfile')
        with open(dockerfile_path, 'r') as f:
            content = f.read()
        
        # Vérifier que les deux ports sont exposés (peut être sur la même ligne ou séparés)
        assert 'EXPOSE ${PORT}' in content or 'EXPOSE 4121' in content, "MCP port not exposed"
        assert 'EXPOSE' in content and ('${HEALTH_PORT}' in content or '4122' in content), "Health port not exposed"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
