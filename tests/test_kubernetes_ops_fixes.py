import pytest
from unittest.mock import patch, MagicMock
from collegue.tools.kubernetes_ops import KubernetesOpsTool, KubernetesRequest
from collegue.tools.clients.base import APIResponse

@pytest.fixture
def mock_k8s_client():
    with patch('collegue.tools.kubernetes_ops.KubernetesOpsTool._get_kubernetes_client') as mock:
        client = MagicMock()
        mock.return_value = client
        yield client

def test_list_pods_success(mock_k8s_client):
    tool = KubernetesOpsTool()
    
    mock_k8s_client.list_pods.return_value = APIResponse(
        success=True,
        data=[{
            'metadata': {
                'name': 'test-pod',
                'namespace': 'default',
                'creation_timestamp': '2024-01-01T00:00:00Z',
                'labels': {'app': 'test'}
            },
            'status': {
                'phase': 'Running',
                'pod_ip': '10.0.0.1',
                'container_statuses': [
                    {'name': 'container1', 'ready': True, 'restart_count': 0}
                ]
            },
            'spec': {
                'node_name': 'node-1'
            }
        }]
    )
    
    request = KubernetesRequest(command='list_pods', namespace='default')
    response = tool.execute(request)
    
    assert response.success
    assert len(response.pods) == 1
    assert response.pods[0].name == 'test-pod'
    assert response.pods[0].status == 'Running'

def test_get_pod_logs_success(mock_k8s_client):
    tool = KubernetesOpsTool()
    
    mock_k8s_client.get_pod_logs.return_value = APIResponse(
        success=True,
        data="Log line 1\nLog line 2\nLog line 3"
    )
    
    request = KubernetesRequest(command='pod_logs', name='test-pod', namespace='default', tail_lines=3)
    response = tool.execute(request)
    
    assert response.success
    assert response.logs == "Log line 1\nLog line 2\nLog line 3"

def test_get_node_success(mock_k8s_client):
    tool = KubernetesOpsTool()
    
    mock_k8s_client.list_nodes.return_value = APIResponse(
        success=True,
        data=[{
            'metadata': {
                'name': 'test-node',
                'creation_timestamp': '2024-01-01T00:00:00Z',
                'labels': {
                    'node-role.kubernetes.io/master': 'true'
                }
            },
            'status': {
                'conditions': [
                    {'type': 'Ready', 'status': 'True'}
                ],
                'node_info': {
                    'kubelet_version': 'v1.28.0',
                    'os_image': 'Ubuntu 22.04'
                },
                'capacity': {
                    'cpu': '4',
                    'memory': '16Gi',
                    'pods': '110'
                }
            }
        }]
    )
    
    request = KubernetesRequest(command='get_node', name='test-node')
    response = tool.execute(request)
    
    assert response.success
    assert response.node is not None
    assert response.node.name == 'test-node'
    assert response.node.status == 'Ready'
