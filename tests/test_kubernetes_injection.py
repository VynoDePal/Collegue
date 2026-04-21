import pytest
from collegue.tools.clients.kubernetes import KubernetesClient

def test_kubernetes_client_command_injection():
    client = KubernetesClient()
    
    # Validation stricte des arguments - ne doit pas lever d'erreur pour une commande valide
    try:
        client._run_kubectl(["get", "pods"])
    except FileNotFoundError:
        # Expected if kubectl is not installed, which is fine for this test
        pass
    except Exception as e:
        if "kubectl not found" not in str(e):
            raise
    
    # Injection via caractère dangereux
    with pytest.raises(ValueError, match=r"Dangerous character .* detected in argument"):
        client._run_kubectl(["get", "pods; rm -rf /"])
        
    with pytest.raises(ValueError, match=r"Dangerous character .* detected in argument"):
        client._run_kubectl(["get", "pods", "--namespace=default&echo 'PWNED'"])
        
    with pytest.raises(ValueError, match="Command arguments must be strings"):
        client._run_kubectl(["get", 123])
