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
    
    def test_no_custom_network_defined(self, compose_file):
        """Test qu'aucun réseau custom n'est défini (utilise le réseau par défaut de Coolify)."""
        # Pas de networks section, ou si elle existe, pas de collegue-network
        # car Coolify gère son propre réseau et Caddy utilise {{upstreams}}
        if 'networks' in compose_file:
            assert 'collegue-network' not in compose_file.get('networks', {}), \
                "collegue-network should not be defined - let Coolify manage the network"
    
    def test_services_use_default_network(self, compose_file):
        """Test que les services n'ont pas de réseau explicite (utilisent le réseau par défaut)."""
        for service_name in ['collegue-app', 'nginx', 'keycloak']:
            service = compose_file['services'].get(service_name, {})
            # Les services ne doivent pas avoir de networks explicitement défini
            assert 'networks' not in service, \
                f"{service_name} should not have explicit networks - use Coolify's default network"
    
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
    
    def test_caddy_uses_https(self, compose_file):
        """Test que Caddy gère HTTPS sur le port 443."""
        caddy = compose_file['services'].get('caddy', {})
        ports = caddy.get('ports', [])
        
        # Vérifier que le port 443 est exposé
        port_443_found = False
        for port in ports:
            if isinstance(port, str) and '443' in port:
                port_443_found = True
                break
            elif isinstance(port, dict):
                if str(port.get('published', '')) == '443' or str(port.get('target', '')) == '443':
                    port_443_found = True
                    break
        
        assert port_443_found, "Caddy should expose port 443 for HTTPS"
    
    def test_collegue_app_ports_exposed(self, compose_file):
        """Test que les ports MCP sont exposés directement (contournement Traefik)."""
        collegue_app = compose_file['services']['collegue-app']
        ports = collegue_app.get('ports', [])
        
        # Vérifier que les ports sont exposés
        port_mappings = []
        for port in ports:
            if isinstance(port, str):
                port_mappings.append(port)
            elif isinstance(port, dict):
                port_mappings.append(f"{port.get('published')}:{port.get('target')}")
        
        # Port 4121 (MCP) doit être exposé
        assert any('4121' in p for p in port_mappings), "MCP port 4121 should be exposed"
        # Port 4122 (health) doit être exposé  
        assert any('4122' in p for p in port_mappings), "Health port 4122 should be exposed"
    
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
    
    def test_caddyfile_exists(self):
        """Test que le fichier Caddyfile existe."""
        caddyfile_path = os.path.join(os.path.dirname(__file__), '..', 'Caddyfile')
        assert os.path.exists(caddyfile_path), "Caddyfile should exist"
        
        with open(caddyfile_path, 'r') as f:
            content = f.read()
        
        assert 'beta.collegue.dev' in content, "Caddyfile should contain the domain"
        assert 'collegue-app:4121' in content, "Caddyfile should proxy to collegue-app:4121"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
