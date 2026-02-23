"""
Tests unitaires pour la configuration nginx
"""
import pytest
import re
import os


class TestNginxConfig:
    """Tests pour la configuration nginx."""
    
    @pytest.fixture
    def nginx_conf(self):
        """Charge le fichier nginx.conf."""
        nginx_path = os.path.join(os.path.dirname(__file__), '..', 'docker', 'nginx', 'nginx.conf')
        with open(nginx_path, 'r') as f:
            return f.read()
    
    def test_nginx_config_exists(self):
        """Test que le fichier nginx.conf existe."""
        nginx_path = os.path.join(os.path.dirname(__file__), '..', 'docker', 'nginx', 'nginx.conf')
        assert os.path.exists(nginx_path), "nginx.conf not found"
    
    def test_upstream_mcp_backend_defined(self, nginx_conf):
        """Test que l'upstream mcp_backend est défini."""
        assert 'upstream mcp_backend' in nginx_conf, "mcp_backend upstream not defined"
        assert 'server collegue-app:4121' in nginx_conf, "collegue-app:4121 not in upstream"
    
    def test_upstream_health_backend_defined(self, nginx_conf):
        """Test que l'upstream health_backend est défini."""
        assert 'upstream health_backend' in nginx_conf, "health_backend upstream not defined"
        assert 'server collegue-app:4122' in nginx_conf, "collegue-app:4122 not in upstream"
    
    def test_mcp_location_uses_upstream(self, nginx_conf):
        """Test que le location /mcp/ utilise l'upstream."""
        assert 'location /mcp/' in nginx_conf, "/mcp/ location not found"
        assert 'proxy_pass http://mcp_backend' in nginx_conf, "mcp_backend not used in /mcp/ location"
    
    def test_health_location_uses_upstream(self, nginx_conf):
        """Test que le location /_health utilise l'upstream health."""
        assert 'location /_health' in nginx_conf, "/_health location not found"
        assert 'proxy_pass http://health_backend/_health' in nginx_conf, "health_backend not used"
    
    def test_proxy_next_upstream_configured(self, nginx_conf):
        """Test que proxy_next_upstream est configuré pour les erreurs."""
        assert 'proxy_next_upstream' in nginx_conf, "proxy_next_upstream not configured"
        assert 'http_503' in nginx_conf, "http_503 not in proxy_next_upstream"
    
    def test_resolver_configured(self, nginx_conf):
        """Test que le resolver DNS est configuré."""
        assert 'resolver' in nginx_conf, "resolver not configured"
        assert '127.0.0.11' in nginx_conf, "Docker DNS resolver not configured"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
