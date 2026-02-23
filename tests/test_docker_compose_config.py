"""
Tests unitaires pour la configuration Docker Compose.
Vérifie que le kc-provisioner est désactivé et que le healthcheck est correct.
"""
import pytest
import yaml
import os


class TestDockerComposeConfig:
    """Tests pour la configuration docker-compose.yml."""
    
    @pytest.fixture
    def compose_file(self):
        """Charge le fichier docker-compose.yml."""
        compose_path = os.path.join(os.path.dirname(__file__), '..', 'docker-compose.yml')
        with open(compose_path, 'r') as f:
            return yaml.safe_load(f)
    
    def test_docker_compose_syntax_is_valid(self, compose_file):
        """Test que le fichier docker-compose.yml est syntaxiquement valide."""
        assert compose_file is not None
        assert 'services' in compose_file
        assert 'collegue-app' in compose_file['services']
    
    def test_network_is_defined(self, compose_file):
        """Test que le réseau collegue-network est défini."""
        assert 'networks' in compose_file, "Networks section not found"
        assert 'collegue-network' in compose_file['networks'], "collegue-network not defined"
        network = compose_file['networks']['collegue-network']
        assert network.get('driver') == 'bridge', "Network should use bridge driver"
    
    def test_healthcheck_port_is_correct(self, compose_file):
        """Test que le healthcheck pointe sur le bon port (4122 pour health_server)."""
        collegue_app = compose_file['services']['collegue-app']
        healthcheck = collegue_app.get('healthcheck', {})
        test_command = healthcheck.get('test', [])
        
        # Vérifier que la commande healthcheck existe
        assert test_command, "Healthcheck test command not found"
        
        # Trouver l'URL dans la commande
        healthcheck_url = None
        for item in test_command:
            if isinstance(item, str) and 'http://localhost' in item:
                healthcheck_url = item
                break
        
        assert healthcheck_url is not None, "Healthcheck URL not found in test command"
        # Le health_server écoute sur 4122 (voir entrypoint.sh et health_server.py)
        assert ':4122/' in healthcheck_url, f"Healthcheck should use port 4122, found: {healthcheck_url}"
    
    def test_kc_provisioner_is_disabled(self, compose_file):
        """Test que le service kc-provisioner est désactivé/commenté."""
        services = compose_file.get('services', {})
        
        # Le service ne devrait pas être présent ou être commenté (donc pas parsé par YAML)
        assert 'kc-provisioner' not in services, \
            "kc-provisioner service should be disabled (commented out)"
    
    def test_keycloak_service_still_present(self, compose_file):
        """Test que Keycloak est toujours présent (au cas où on veut l'utiliser plus tard)."""
        services = compose_file.get('services', {})
        assert 'keycloak' in services, "Keycloak service should still be present"
        
        keycloak = services['keycloak']
        assert keycloak.get('image', '').startswith('quay.io/keycloak/'), \
            "Keycloak should use the correct image"
    
    def test_nginx_depends_on_app(self, compose_file):
        """Test que nginx dépend bien de collegue-app."""
        nginx = compose_file['services'].get('nginx', {})
        depends_on = nginx.get('depends_on', {})
        
        assert 'collegue-app' in depends_on, \
            "nginx should depend on collegue-app"
        
        # Vérifier que la condition est bien sur le healthcheck
        if isinstance(depends_on, dict):
            condition = depends_on.get('collegue-app', {}).get('condition', '')
            assert condition == 'service_healthy', \
                f"nginx should wait for collegue-app to be healthy, got: {condition}"
    
    def test_collegue_app_uses_network(self, compose_file):
        """Test que collegue-app utilise le réseau collegue-network."""
        collegue_app = compose_file['services']['collegue-app']
        networks = collegue_app.get('networks', [])
        assert 'collegue-network' in networks, "collegue-app should use collegue-network"
    
    def test_nginx_uses_network(self, compose_file):
        """Test que nginx utilise le réseau collegue-network."""
        nginx = compose_file['services'].get('nginx', {})
        networks = nginx.get('networks', [])
        assert 'collegue-network' in networks, "nginx should use collegue-network"
    
    def test_collegue_app_environment_variables(self, compose_file):
        """Test que les variables d'environnement essentielles sont présentes."""
        collegue_app = compose_file['services']['collegue-app']
        env = collegue_app.get('environment', {})
        
        required_env = ['PORT', 'MCP_TRANSPORT', 'MCP_HOST', 'PYTHONUNBUFFERED']
        for var in required_env:
            assert var in env, f"Required environment variable {var} not found"
        
        # Vérifier que le port est bien 4121 (peut être int ou string selon le parsing YAML)
        port_value = env.get('PORT')
        assert str(port_value) == '4121', f"PORT should be 4121, got: {port_value} (type: {type(port_value)})"


class TestDockerComposeHealthcheckIntegration:
    """Tests d'intégration pour le healthcheck."""
    
    def test_healthcheck_matches_app_port(self):
        """Test que le port du healthcheck correspond au health_server (port 4122)."""
        compose_path = os.path.join(os.path.dirname(__file__), '..', 'docker-compose.yml')
        
        with open(compose_path, 'r') as f:
            content = f.read()
        
        # Extraire le port du healthcheck
        import re
        healthcheck_match = re.search(r'http://localhost:(\d+)/_health', content)
        assert healthcheck_match, "Could not find healthcheck URL pattern"
        
        healthcheck_port = int(healthcheck_match.group(1))
        
        # Le healthcheck doit être sur 4122 (health_server), pas 4121 (MCP server)
        assert healthcheck_port == 4122, \
            f"Healthcheck port {healthcheck_port} should be 4122 (health_server)"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
