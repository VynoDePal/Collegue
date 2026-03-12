"""
Tests de sécurité pour KubernetesClient - Protection contre command injection.
"""
import pytest
from unittest.mock import patch, MagicMock
from collegue.tools.clients.kubernetes import KubernetesClient, KubernetesSecurityError
from collegue.tools.clients.base import APIResponse


class TestCommandInjectionProtection:
    """Tests de protection contre l'injection de commandes."""
    
    def setup_method(self):
        """Setup avant chaque test."""
        self.client = KubernetesClient(namespace="default")
    
    def test_valid_command_succeeds(self):
        """Test qu'une commande valide passe la validation."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='{"items": []}',
                stderr=''
            )
            
            result = self.client._run_kubectl(["get", "pods"])
            
            assert result.success is True
            mock_run.assert_called_once()
    
    def test_command_injection_with_semicolon_blocked(self):
        """Test que l'injection avec ; est bloquée."""
        with pytest.raises(KubernetesSecurityError) as exc_info:
            self.client._run_kubectl(["get", "pods; rm -rf /"])
        
        assert "Dangerous character" in str(exc_info.value)
    
    def test_command_injection_with_ampersand_blocked(self):
        """Test que l'injection avec & est bloquée."""
        with pytest.raises(KubernetesSecurityError) as exc_info:
            self.client._run_kubectl(["get", "pods && cat /etc/passwd"])
        
        assert "Dangerous character" in str(exc_info.value)
    
    def test_command_injection_with_pipe_blocked(self):
        """Test que l'injection avec | est bloquée."""
        with pytest.raises(KubernetesSecurityError) as exc_info:
            self.client._run_kubectl(["get", "pods | cat /etc/passwd"])
        
        assert "Dangerous character" in str(exc_info.value)
    
    def test_command_injection_with_backtick_blocked(self):
        """Test que l'injection avec ` est bloquée."""
        with pytest.raises(KubernetesSecurityError) as exc_info:
            self.client._run_kubectl(["get", "pods `whoami`"])
        
        assert "Dangerous character" in str(exc_info.value)
    
    def test_command_injection_with_dollar_blocked(self):
        """Test que l'injection avec $ est bloquée."""
        with pytest.raises(KubernetesSecurityError) as exc_info:
            self.client._run_kubectl(["get", "pods $(whoami)"])
        
        assert "Dangerous character" in str(exc_info.value)
    
    def test_command_injection_with_newline_blocked(self):
        """Test que l'injection avec \n est bloquée."""
        with pytest.raises(KubernetesSecurityError) as exc_info:
            self.client._run_kubectl(["get", "pods\nrm -rf /"])
        
        assert "Dangerous character" in str(exc_info.value)
    
    def test_non_string_argument_blocked(self):
        """Test que les arguments non-string sont bloqués."""
        with pytest.raises(KubernetesSecurityError) as exc_info:
            self.client._run_kubectl(["get", 123])
        
        assert "must be strings" in str(exc_info.value)
    
    def test_namespace_injection_blocked_in_method(self):
        """Test que l'injection via namespace est bloquée dans les méthodes."""
        malicious_namespace = "default; rm -rf /"
        
        with pytest.raises(KubernetesSecurityError) as exc_info:
            client = KubernetesClient(namespace=malicious_namespace)
            client._run_kubectl(["get", "pods"])
        
        assert "Dangerous character" in str(exc_info.value)
    
    def test_resource_name_injection_blocked(self):
        """Test que l'injection via nom de ressource est bloquée."""
        malicious_name = "pod; cat /etc/passwd"
        
        with pytest.raises(KubernetesSecurityError) as exc_info:
            self.client._run_kubectl(["get", "pod", malicious_name])
        
        assert "Dangerous character" in str(exc_info.value)
    
    def test_label_selector_injection_blocked(self):
        """Test que l'injection via label selector est bloquée."""
        malicious_selector = "app=test; rm -rf /"
        
        with pytest.raises(KubernetesSecurityError) as exc_info:
            self.client._run_kubectl(["get", "pods", "-l", malicious_selector])
        
        assert "Dangerous character" in str(exc_info.value)


class TestNamespaceValidation:
    """Tests de validation du namespace."""
    
    def test_valid_namespace_accepted(self):
        """Test qu'un namespace valide est accepté."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='{"items": []}',
                stderr=''
            )
            
            client = KubernetesClient(namespace="production")
            result = client._run_kubectl(["get", "pods"])
            
            assert result.success is True
    
    def test_namespace_with_dangerous_chars_rejected(self):
        """Test qu'un namespace avec caractères dangereux est rejeté."""
        with pytest.raises(KubernetesSecurityError) as exc_info:
            client = KubernetesClient(namespace="default; echo hacked")
            client._run_kubectl(["get", "pods"])
        
        assert "Dangerous character" in str(exc_info.value)


class TestKubeconfigValidation:
    """Tests de validation du kubeconfig path."""
    
    def test_kubeconfig_path_injection_blocked(self):
        """Test que l'injection via kubeconfig path est bloquée."""
        with pytest.raises(KubernetesSecurityError) as exc_info:
            client = KubernetesClient(kubeconfig="/tmp/config; rm -rf /")
            client._run_kubectl(["get", "pods"])
        
        assert "Dangerous character" in str(exc_info.value)


class TestContextValidation:
    """Tests de validation du context."""
    
    def test_context_injection_blocked(self):
        """Test que l'injection via context est bloquée."""
        with pytest.raises(KubernetesSecurityError) as exc_info:
            client = KubernetesClient(context="prod; cat /etc/passwd")
            client._run_kubectl(["get", "pods"])
        
        assert "Dangerous character" in str(exc_info.value)


class TestMethodLevelProtection:
    """Tests de protection au niveau des méthodes publiques."""
    
    def setup_method(self):
        """Setup avant chaque test."""
        self.client = KubernetesClient(namespace="default")
    
    def test_list_pods_with_malicious_namespace_blocked(self):
        """Test que list_pods bloque les namespaces malveillants."""
        with pytest.raises(KubernetesSecurityError) as exc_info:
            self.client.list_pods(namespace="default; rm -rf /")
        
        assert "Dangerous character" in str(exc_info.value)
    
    def test_get_pod_with_malicious_name_blocked(self):
        """Test que get_pod bloque les noms malveillants."""
        with pytest.raises(KubernetesSecurityError) as exc_info:
            self.client.get_pod(name="nginx; cat /etc/passwd")
        
        assert "Dangerous character" in str(exc_info.value)
    
    def test_get_pod_logs_with_malicious_name_blocked(self):
        """Test que get_pod_logs bloque les noms malveillants."""
        with pytest.raises(KubernetesSecurityError) as exc_info:
            self.client.get_pod_logs(name="nginx; whoami")
        
        assert "Dangerous character" in str(exc_info.value)
    
    def test_describe_resource_with_malicious_name_blocked(self):
        """Test que describe_resource bloque les noms malveillants."""
        with pytest.raises(KubernetesSecurityError) as exc_info:
            self.client.describe_resource(resource_type="pod", name="nginx; id")
        
        assert "Dangerous character" in str(exc_info.value)
    
    def test_describe_resource_with_malicious_type_blocked(self):
        """Test que describe_resource bloque les types malveillants."""
        with pytest.raises(KubernetesSecurityError) as exc_info:
            self.client.describe_resource(resource_type="pod; rm -rf /", name="nginx")
        
        assert "Dangerous character" in str(exc_info.value)
