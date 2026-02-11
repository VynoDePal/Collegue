"""
Tests pour le module transformers/kubernetes.py
"""
import sys
sys.path.insert(0, '/home/kevyn-odjo/Documents/Collegue')

from collegue.tools.transformers.kubernetes import (
    transform_pods,
    transform_pod_detail,
    transform_deployments,
    transform_services,
    transform_namespaces,
    transform_events,
    transform_nodes,
    transform_configmaps,
    transform_secrets,
    _format_age,
)
from datetime import datetime, timezone


class TestFormatAge:
    """Tests pour _format_age."""

    def test_format_age_seconds(self):
        """Test formatage en secondes."""
        now = datetime.now(timezone.utc)
        result = _format_age(now)
        assert result == "0s"

    def test_format_age_minutes(self):
        """Test formatage en minutes."""
        now = datetime.now(timezone.utc)
        past = datetime.fromtimestamp(now.timestamp() - 120, timezone.utc)
        result = _format_age(past)
        assert result == "2m"

    def test_format_age_hours(self):
        """Test formatage en heures."""
        now = datetime.now(timezone.utc)
        past = datetime.fromtimestamp(now.timestamp() - 7200, timezone.utc)
        result = _format_age(past)
        assert result == "2h"

    def test_format_age_days(self):
        """Test formatage en jours."""
        now = datetime.now(timezone.utc)
        past = datetime.fromtimestamp(now.timestamp() - 172800, timezone.utc)
        result = _format_age(past)
        assert result == "2d"

    def test_format_age_none(self):
        """Test avec valeur None."""
        result = _format_age(None)
        assert result == "N/A"


class TestTransformPods:
    """Tests pour transform_pods."""

    def test_transform_empty_list(self):
        """Test avec liste vide."""
        result = transform_pods([])
        assert result == []

    def test_transform_single_pod(self):
        """Test avec un seul pod."""
        pod_data = [{
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
        result = transform_pods(pod_data)
        assert len(result) == 1
        assert result[0].name == 'test-pod'
        assert result[0].namespace == 'default'
        assert result[0].status == 'Running'
        assert result[0].node == 'node-1'
        assert result[0].ip == '10.0.0.1'


class TestTransformDeployments:
    """Tests pour transform_deployments."""

    def test_transform_empty_list(self):
        """Test avec liste vide."""
        result = transform_deployments([])
        assert result == []

    def test_transform_single_deployment(self):
        """Test avec un seul déploiement."""
        deployment_data = [{
            'metadata': {
                'name': 'test-deployment',
                'namespace': 'default',
                'creation_timestamp': '2024-01-01T00:00:00Z'
            },
            'spec': {
                'replicas': 3,
                'selector': {'match_labels': {'app': 'test'}},
                'strategy': {'type': 'RollingUpdate'}
            },
            'status': {
                'ready_replicas': 2,
                'available_replicas': 2
            }
        }]
        result = transform_deployments(deployment_data)
        assert len(result) == 1
        assert result[0].name == 'test-deployment'
        assert result[0].ready == 2
        assert result[0].available == 2


class TestTransformServices:
    """Tests pour transform_services."""

    def test_transform_empty_list(self):
        """Test avec liste vide."""
        result = transform_services([])
        assert result == []

    def test_transform_single_service(self):
        """Test avec un seul service."""
        service_data = [{
            'metadata': {
                'name': 'test-service',
                'namespace': 'default',
                'creation_timestamp': '2024-01-01T00:00:00Z'
            },
            'spec': {
                'type': 'ClusterIP',
                'cluster_ip': '10.0.0.10',
                'external_ips': ['192.168.1.1'],
                'ports': [{'port': 80}],
                'selector': {'app': 'test'}
            }
        }]
        result = transform_services(service_data)
        assert len(result) == 1
        assert result[0].name == 'test-service'
        assert result[0].type == 'ClusterIP'
        assert result[0].cluster_ip == '10.0.0.10'


class TestTransformNamespaces:
    """Tests pour transform_namespaces."""

    def test_transform_empty_list(self):
        """Test avec liste vide."""
        result = transform_namespaces([])
        assert result == []

    def test_transform_single_namespace(self):
        """Test avec un seul namespace."""
        ns_data = [{
            'metadata': {
                'name': 'test-ns',
                'creationTimestamp': '2024-01-01T00:00:00Z',
                'labels': {'env': 'test'}
            },
            'status': {'phase': 'Active'}
        }]
        result = transform_namespaces(ns_data)
        assert len(result) == 1
        assert result[0].name == 'test-ns'
        assert result[0].status == 'Active'


class TestTransformEvents:
    """Tests pour transform_events."""

    def test_transform_empty_list(self):
        """Test avec liste vide."""
        result = transform_events([])
        assert result == []

    def test_transform_single_event(self):
        """Test avec un seul événement."""
        event_data = [{
            'metadata': {
                'name': 'test-event.123',
                'namespace': 'default',
                'creationTimestamp': '2024-01-01T00:00:00Z'
            },
            'type': 'Warning',
            'reason': 'FailedMount',
            'message': 'Mount failed',
            'source': {'component': 'kubelet'},
            'firstTimestamp': '2024-01-01T00:00:00Z',
            'lastTimestamp': '2024-01-01T00:01:00Z',
            'count': 5,
            'involvedObject': {'name': 'test-pod', 'kind': 'Pod'}
        }]
        result = transform_events(event_data)
        assert len(result) == 1
        assert result[0].name == 'test-event.123'
        assert result[0].type == 'Warning'
        assert result[0].reason == 'FailedMount'
        assert result[0].count == 5


class TestTransformNodes:
    """Tests pour transform_nodes."""

    def test_transform_empty_list(self):
        """Test avec liste vide."""
        result = transform_nodes([])
        assert result == []

    def test_transform_single_node(self):
        """Test avec un seul nœud."""
        node_data = [{
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
        result = transform_nodes(node_data)
        assert len(result) == 1
        assert result[0].name == 'test-node'
        assert result[0].status == 'Ready'
        assert 'master' in result[0].roles
        assert result[0].version == 'v1.28.0'


class TestTransformConfigMaps:
    """Tests pour transform_configmaps."""

    def test_transform_empty_list(self):
        """Test avec liste vide."""
        result = transform_configmaps([])
        assert result == []

    def test_transform_single_configmap(self):
        """Test avec un seul ConfigMap."""
        cm_data = [{
            'metadata': {
                'name': 'test-cm',
                'namespace': 'default',
                'creationTimestamp': '2024-01-01T00:00:00Z'
            },
            'data': {
                'key1': 'value1',
                'key2': 'value2'
            }
        }]
        result = transform_configmaps(cm_data)
        assert len(result) == 1
        assert result[0].name == 'test-cm'
        assert result[0].data_keys == ['key1', 'key2']


class TestTransformSecrets:
    """Tests pour transform_secrets."""

    def test_transform_empty_list(self):
        """Test avec liste vide."""
        result = transform_secrets([])
        assert result == []

    def test_transform_single_secret(self):
        """Test avec un seul Secret."""
        secret_data = [{
            'metadata': {
                'name': 'test-secret',
                'namespace': 'default',
                'creationTimestamp': '2024-01-01T00:00:00Z'
            },
            'type': 'Opaque',
            'data': {
                'password': 'cGFzc3dvcmQ='  # base64
            }
        }]
        result = transform_secrets(secret_data)
        assert len(result) == 1
        assert result[0].name == 'test-secret'
        assert result[0].type == 'Opaque'
        assert result[0].data_keys == ['password']


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])
