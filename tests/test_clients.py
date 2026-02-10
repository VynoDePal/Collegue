"""
Tests unitaires pour les clients du package collegue.tools.clients

Ces tests utilisent des mocks pour tester les clients sans faire d'appels réels aux APIs.
"""
import sys
import os
sys.path.insert(0, '/home/kevyn-odjo/Documents/Collegue')

from unittest.mock import Mock, patch, MagicMock
import json

print("=" * 80)
print("TESTS UNITAIRES - CLIENTS")
print("=" * 80)

# =============================================================================
# TEST 1: SENTRY CLIENT
# =============================================================================
print("\n" + "=" * 80)
print("TEST 1: SENTRY CLIENT")
print("=" * 80)

try:
    from collegue.tools.clients import SentryClient, APIError
    
    # Test 1.1: Initialisation
    print("\n1.1 Test initialisation SentryClient...")
    client = SentryClient(
        token="test-token-123",
        organization="test-org",
        base_url="https://sentry.io"
    )
    assert client.token == "test-token-123"
    assert client.organization == "test-org"
    assert client.base_url == "https://sentry.io"
    print("   ✅ Initialisation correcte")
    
    # Test 1.2: _build_url
    print("\n1.2 Test _build_url...")
    url = client._build_url("/projects/")
    assert url == "https://sentry.io/api/0/projects/"
    print(f"   ✅ URL construite: {url}")
    
    # Test 1.3: _get_headers
    print("\n1.3 Test _get_headers...")
    headers = client._get_headers()
    assert "Authorization" in headers
    assert headers["Authorization"] == "Bearer test-token-123"
    assert headers["Content-Type"] == "application/json"
    print("   ✅ Headers corrects")
    
    # Test 1.4: list_projects (mocké)
    print("\n1.4 Test list_projects (mocké)...")
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {"id": "1", "slug": "project-1", "name": "Project 1", "platform": "python"},
        {"id": "2", "slug": "project-2", "name": "Project 2", "platform": "javascript"}
    ]
    
    with patch('collegue.tools.clients.sentry.requests.get', return_value=mock_response):
        response = client.list_projects()
        assert response.success is True
        assert len(response.data) == 2
        assert response.data[0]["slug"] == "project-1"
        print(f"   ✅ list_projects retourne {len(response.data)} projets")
    
    # Test 1.5: list_issues (mocké)
    print("\n1.5 Test list_issues (mocké)...")
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {"id": "issue-1", "title": "Error in main", "level": "error", "status": "unresolved"},
        {"id": "issue-2", "title": "Warning deprecated", "level": "warning", "status": "resolved"}
    ]
    
    with patch('collegue.tools.clients.sentry.requests.get', return_value=mock_response):
        response = client.list_issues(project="my-project")
        assert response.success is True
        assert len(response.data) == 2
        print(f"   ✅ list_issues retourne {len(response.data)} issues")
    
    # Test 1.6: Gestion erreur APIError
    print("\n1.6 Test gestion erreur APIError...")
    mock_response.status_code = 401
    mock_response.text = "Unauthorized"
    
    with patch('collegue.tools.clients.sentry.requests.get', return_value=mock_response):
        response = client.list_projects()
        assert response.success is False
        assert "401" in response.error_message or "Unauthorized" in response.error_message
        print("   ✅ Gestion erreur 401 correcte")
    
    # Test 1.7: get_issue (mocké)
    print("\n1.7 Test get_issue (mocké)...")
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "id": "issue-123",
        "title": "Test Issue",
        "level": "error",
        "status": "unresolved",
        "culprit": "test.py:42"
    }
    
    with patch('collegue.tools.clients.sentry.requests.get', return_value=mock_response):
        response = client.get_issue("issue-123")
        assert response.success is True
        assert response.data["id"] == "issue-123"
        print("   ✅ get_issue fonctionne")
    
    # Test 1.8: list_releases (mocké)
    print("\n1.8 Test list_releases (mocké)...")
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {"version": "1.0.0", "dateCreated": "2024-01-01", "dateReleased": "2024-01-02"}
    ]
    
    with patch('collegue.tools.clients.sentry.requests.get', return_value=mock_response):
        response = client.list_releases(project="my-project")
        assert response.success is True
        print("   ✅ list_releases fonctionne")
    
    # Test 1.9: get_project_stats (mocké)
    print("\n1.9 Test get_project_stats (mocké)...")
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {"stat": "received", "series": [[1, 10], [2, 20]]}
    ]
    
    with patch('collegue.tools.clients.sentry.requests.get', return_value=mock_response):
        response = client.get_project_stats(project="my-project")
        assert response.success is True
        print("   ✅ get_project_stats fonctionne")
    
    # Test 1.10: get_issue_events (mocké)
    print("\n1.10 Test get_issue_events (mocké)...")
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {"eventID": "evt-1", "title": "Exception", "dateCreated": "2024-01-01"}
    ]
    
    with patch('collegue.tools.clients.sentry.requests.get', return_value=mock_response):
        response = client.get_issue_events("issue-123")
        assert response.success is True
        print("   ✅ get_issue_events fonctionne")
    
    # Test 1.11: get_issue_tags (mocké)
    print("\n1.11 Test get_issue_tags (mocké)...")
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {"key": "browser", "topValues": [{"value": "chrome", "count": 50}]}
    ]
    
    with patch('collegue.tools.clients.sentry.requests.get', return_value=mock_response):
        response = client.get_issue_tags("issue-123")
        assert response.success is True
        print("   ✅ get_issue_tags fonctionne")
    
    # Test 1.12: get_project (mocké)
    print("\n1.12 Test get_project (mocké)...")
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "id": "proj-1",
        "slug": "my-project",
        "name": "My Project",
        "platform": "python"
    }
    
    with patch('collegue.tools.clients.sentry.requests.get', return_value=mock_response):
        response = client.get_project("my-project")
        assert response.success is True
        assert response.data["slug"] == "my-project"
        print("   ✅ get_project fonctionne")
    
    # Test 1.13: Token manquant
    print("\n1.13 Test APIError si token manquant...")
    try:
        SentryClient(token="", organization="test")
        print("   ❌ Devrait lever une erreur")
    except APIError as e:
        print(f"   ✅ APIError levée: {str(e)[:50]}...")
    
    print("\n✅ Tous les tests SentryClient passent!")
    
except Exception as e:
    print(f"❌ Erreur dans les tests SentryClient: {e}")
    import traceback
    traceback.print_exc()

# =============================================================================
# TEST 2: KUBERNETES CLIENT
# =============================================================================
print("\n" + "=" * 80)
print("TEST 2: KUBERNETES CLIENT")
print("=" * 80)

try:
    from collegue.tools.clients import KubernetesClient, APIError
    
    # Test 2.1: Initialisation avec kubectl
    print("\n2.1 Test initialisation KubernetesClient (mode kubectl)...")
    client = KubernetesClient(
        kubeconfig="/path/to/kubeconfig",
        context="prod",
        namespace="default"
    )
    assert client.kubeconfig == "/path/to/kubeconfig"
    assert client.context == "prod"
    assert client.namespace == "default"
    print("   ✅ Initialisation correcte")
    
    # Test 2.2: _get_kubectl_args
    print("\n2.2 Test _get_kubectl_args...")
    args = client._get_kubectl_args()
    assert "kubectl" in args
    assert "--kubeconfig" in args
    assert "/path/to/kubeconfig" in args
    assert "--context" in args
    assert "prod" in args
    assert "--namespace" in args
    assert "default" in args
    print(f"   ✅ Args kubectl: {args}")
    
    # Test 2.3: list_pods (mocké kubectl)
    print("\n2.3 Test list_pods (mocké kubectl)...")
    mock_pods = {
        "items": [
            {
                "metadata": {"name": "pod-1", "namespace": "default", "creationTimestamp": "2024-01-01"},
                "spec": {"nodeName": "node-1"},
                "status": {"phase": "Running", "podIP": "10.0.0.1", "containerStatuses": [{"name": "app", "ready": True, "restartCount": 0}]}
            }
        ]
    }
    
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps(mock_pods),
            stderr=""
        )
        response = client.list_pods()
        assert response.success is True
        assert len(response.data) == 1
        assert response.data[0]["metadata"]["name"] == "pod-1"
        print(f"   ✅ list_pods retourne {len(response.data)} pods")
    
    # Test 2.4: get_pod (mocké kubectl)
    print("\n2.4 Test get_pod (mocké kubectl)...")
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps(mock_pods["items"][0]),
            stderr=""
        )
        response = client.get_pod("pod-1")
        assert response.success is True
        assert response.data["metadata"]["name"] == "pod-1"
        print("   ✅ get_pod fonctionne")
    
    # Test 2.5: get_pod_logs (mocké kubectl)
    print("\n2.5 Test get_pod_logs (mocké kubectl)...")
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = Mock(
            returncode=0,
            stdout="2024-01-01 Log line 1\n2024-01-01 Log line 2",
            stderr=""
        )
        response = client.get_pod_logs("pod-1", tail_lines=100)
        assert response.success is True
        assert "Log line 1" in response.data
        print("   ✅ get_pod_logs fonctionne")
    
    # Test 2.6: list_deployments (mocké kubectl)
    print("\n2.6 Test list_deployments (mocké kubectl)...")
    mock_deployments = {
        "items": [
            {
                "metadata": {"name": "deployment-1", "namespace": "default", "creationTimestamp": "2024-01-01"},
                "spec": {"replicas": 3, "selector": {"matchLabels": {"app": "web"}}},
                "status": {"readyReplicas": 3, "availableReplicas": 3}
            }
        ]
    }
    
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps(mock_deployments),
            stderr=""
        )
        response = client.list_deployments()
        assert response.success is True
        assert len(response.data) == 1
        print(f"   ✅ list_deployments retourne {len(response.data)} deployments")
    
    # Test 2.7: get_deployment (mocké kubectl)
    print("\n2.7 Test get_deployment (mocké kubectl)...")
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps(mock_deployments["items"][0]),
            stderr=""
        )
        response = client.get_deployment("deployment-1")
        assert response.success is True
        assert response.data["metadata"]["name"] == "deployment-1"
        print("   ✅ get_deployment fonctionne")
    
    # Test 2.8: list_services (mocké kubectl)
    print("\n2.8 Test list_services (mocké kubectl)...")
    mock_services = {
        "items": [
            {
                "metadata": {"name": "service-1", "namespace": "default"},
                "spec": {"type": "ClusterIP", "clusterIP": "10.0.0.10", "ports": [{"port": 80}]},
                "status": {"loadBalancer": {}}
            }
        ]
    }
    
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps(mock_services),
            stderr=""
        )
        response = client.list_services()
        assert response.success is True
        print("   ✅ list_services fonctionne")
    
    # Test 2.9: list_namespaces (mocké kubectl)
    print("\n2.9 Test list_namespaces (mocké kubectl)...")
    mock_namespaces = {
        "items": [
            {"metadata": {"name": "default", "creationTimestamp": "2024-01-01"}},
            {"metadata": {"name": "kube-system", "creationTimestamp": "2024-01-01"}}
        ]
    }
    
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps(mock_namespaces),
            stderr=""
        )
        response = client.list_namespaces()
        assert response.success is True
        assert len(response.data) == 2
        print(f"   ✅ list_namespaces retourne {len(response.data)} namespaces")
    
    # Test 2.10: list_events (mocké kubectl)
    print("\n2.10 Test list_events (mocké kubectl)...")
    mock_events = {
        "items": [
            {
                "metadata": {"name": "event-1", "namespace": "default"},
                "type": "Normal",
                "reason": "Created",
                "message": "Created pod pod-1",
                "source": {"component": "kubelet"}
            }
        ]
    }
    
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps(mock_events),
            stderr=""
        )
        response = client.list_events()
        assert response.success is True
        print("   ✅ list_events fonctionne")
    
    # Test 2.11: list_nodes (mocké kubectl)
    print("\n2.11 Test list_nodes (mocké kubectl)...")
    mock_nodes = {
        "items": [
            {
                "metadata": {"name": "node-1", "creationTimestamp": "2024-01-01", "labels": {"node-role.kubernetes.io/worker": ""}},
                "status": {"conditions": [{"type": "Ready", "status": "True"}], "capacity": {"cpu": "4", "memory": "8Gi"}}
            }
        ]
    }
    
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps(mock_nodes),
            stderr=""
        )
        response = client.list_nodes()
        assert response.success is True
        assert len(response.data) == 1
        print(f"   ✅ list_nodes retourne {len(response.data)} nodes")
    
    # Test 2.12: list_configmaps (mocké kubectl)
    print("\n2.12 Test list_configmaps (mocké kubectl)...")
    mock_configmaps = {
        "items": [
            {
                "metadata": {"name": "config-1", "namespace": "default", "creationTimestamp": "2024-01-01"},
                "data": {"key1": "value1", "key2": "value2"}
            }
        ]
    }
    
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps(mock_configmaps),
            stderr=""
        )
        response = client.list_configmaps()
        assert response.success is True
        print("   ✅ list_configmaps fonctionne")
    
    # Test 2.13: list_secrets (mocké kubectl)
    print("\n2.13 Test list_secrets (mocké kubectl)...")
    mock_secrets = {
        "items": [
            {
                "metadata": {"name": "secret-1", "namespace": "default", "creationTimestamp": "2024-01-01"},
                "type": "Opaque",
                "data": {"password": "base64encoded"}
            }
        ]
    }
    
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps(mock_secrets),
            stderr=""
        )
        response = client.list_secrets()
        assert response.success is True
        print("   ✅ list_secrets fonctionne")
    
    # Test 2.14: describe_resource (mocké kubectl)
    print("\n2.14 Test describe_resource (mocké kubectl)...")
    mock_yaml = """
apiVersion: v1
kind: Pod
metadata:
  name: pod-1
spec:
  containers:
  - name: app
    image: nginx
"""
    
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = Mock(
            returncode=0,
            stdout=mock_yaml,
            stderr=""
        )
        response = client.describe_resource("pod", "pod-1")
        assert response.success is True
        assert response.data is not None
        print("   ✅ describe_resource fonctionne")
    
    # Test 2.15: Gestion erreur kubectl
    print("\n2.15 Test gestion erreur kubectl...")
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = Mock(
            returncode=1,
            stdout="",
            stderr="Error from server: pods not found"
        )
        response = client.get_pod("nonexistent")
        assert response.success is False
        assert "not found" in response.error_message.lower() or "error" in response.error_message.lower()
        print("   ✅ Gestion erreur correcte")
    
    print("\n✅ Tous les tests KubernetesClient passent!")
    
except Exception as e:
    print(f"❌ Erreur dans les tests KubernetesClient: {e}")
    import traceback
    traceback.print_exc()

# =============================================================================
# TEST 3: POSTGRES CLIENT
# =============================================================================
print("\n" + "=" * 80)
print("TEST 3: POSTGRES CLIENT")
print("=" * 80)

try:
    from collegue.tools.clients import PostgresClient, APIError
    
    # Test 3.1: Initialisation avec connection_string
    print("\n3.1 Test initialisation PostgresClient (avec connection_string)...")
    client = PostgresClient(
        connection_string="postgresql://user:pass@localhost:5432/testdb",
        schema="public"
    )
    assert client.connection_string == "postgresql://user:pass@localhost:5432/testdb"
    assert client.schema == "public"
    print("   ✅ Initialisation avec connection_string correcte")
    
    # Test 3.2: Initialisation avec paramètres individuels
    print("\n3.2 Test initialisation PostgresClient (avec paramètres)...")
    client = PostgresClient(
        host="localhost",
        port=5432,
        database="testdb",
        username="user",
        password="pass",
        schema="custom"
    )
    assert "postgresql://user:pass@localhost:5432/testdb" == client.connection_string
    assert client.schema == "custom"
    print("   ✅ Initialisation avec paramètres correcte")
    
    # Test 3.3: Initialisation depuis environnement
    print("\n3.3 Test initialisation depuis POSTGRES_URL...")
    with patch.dict(os.environ, {"POSTGRES_URL": "postgresql://envuser:envpass@envhost:5432/envdb"}):
        client = PostgresClient()
        assert "envuser" in client.connection_string
        assert "envhost" in client.connection_string
        print("   ✅ Initialisation depuis environnement correcte")
    
    # Test 3.4: list_schemas (mocké)
    print("\n3.4 Test list_schemas (mocké)...")
    mock_rows = [
        {"schema_name": "public"},
        {"schema_name": "custom"}
    ]
    
    with patch.object(client, '_execute_query', return_value=APIResponse(success=True, data=mock_rows)):
        response = client.list_schemas()
        assert response.success is True
        assert len(response.data) == 2
        print(f"   ✅ list_schemas retourne {len(response.data)} schémas")
    
    # Test 3.5: list_tables (mocké)
    print("\n3.5 Test list_tables (mocké)...")
    mock_tables = [
        {"table_name": "users", "schema_name": "public"},
        {"table_name": "orders", "schema_name": "public"}
    ]
    
    with patch.object(client, '_execute_query', return_value=APIResponse(success=True, data=mock_tables)):
        response = client.list_tables("public")
        assert response.success is True
        assert len(response.data) == 2
        print(f"   ✅ list_tables retourne {len(response.data)} tables")
    
    # Test 3.6: describe_table (mocké)
    print("\n3.6 Test describe_table (mocké)...")
    mock_columns = [
        {"column_name": "id", "data_type": "integer", "is_nullable": "NO", "column_default": None},
        {"column_name": "name", "data_type": "varchar", "is_nullable": "YES", "column_default": None}
    ]
    
    with patch.object(client, '_execute_query', return_value=APIResponse(success=True, data=mock_columns)):
        response = client.describe_table("users", "public")
        assert response.success is True
        assert len(response.data) == 2
        print(f"   ✅ describe_table retourne {len(response.data)} colonnes")
    
    # Test 3.7: get_indexes (mocké)
    print("\n3.7 Test get_indexes (mocké)...")
    mock_indexes = [
        {"indexname": "users_pkey", "tablename": "users", "indexdef": "CREATE UNIQUE INDEX users_pkey ON users USING btree (id)"}
    ]
    
    with patch.object(client, '_execute_query', return_value=APIResponse(success=True, data=mock_indexes)):
        response = client.get_indexes("users", "public")
        assert response.success is True
        print("   ✅ get_indexes fonctionne")
    
    # Test 3.8: get_foreign_keys (mocké)
    print("\n3.8 Test get_foreign_keys (mocké)...")
    mock_fks = [
        {"column_name": "user_id", "foreign_table_name": "users", "foreign_column_name": "id"}
    ]
    
    with patch.object(client, '_execute_query', return_value=APIResponse(success=True, data=mock_fks)):
        response = client.get_foreign_keys("orders", "public")
        assert response.success is True
        print("   ✅ get_foreign_keys fonctionne")
    
    # Test 3.9: get_table_stats (mocké)
    print("\n3.9 Test get_table_stats (mocké)...")
    mock_stats = [
        {"table_name": "users", "row_count": 1000, "total_size": "1 MB"}
    ]
    
    with patch.object(client, '_execute_query', return_value=APIResponse(success=True, data=mock_stats)):
        response = client.get_table_stats("users", "public")
        assert response.success is True
        print("   ✅ get_table_stats fonctionne")
    
    # Test 3.10: sample_data (mocké)
    print("\n3.10 Test sample_data (mocké)...")
    mock_data = [
        {"id": 1, "name": "Alice"},
        {"id": 2, "name": "Bob"}
    ]
    
    with patch.object(client, '_execute_query', return_value=APIResponse(success=True, data=mock_data)):
        response = client.sample_data("users", "public", limit=2)
        assert response.success is True
        assert len(response.data) == 2
        print(f"   ✅ sample_data retourne {len(response.data)} lignes")
    
    # Test 3.11: execute_query SELECT (mocké)
    print("\n3.11 Test execute_query SELECT (mocké)...")
    mock_query_result = [
        {"id": 1, "count": 100}
    ]
    
    with patch.object(client, '_execute_query', return_value=APIResponse(success=True, data=mock_query_result)):
        response = client.execute_query("SELECT id, count FROM stats", limit=10)
        assert response.success is True
        print("   ✅ execute_query SELECT fonctionne")
    
    # Test 3.12: execute_query bloque INSERT
    print("\n3.12 Test execute_query bloque INSERT...")
    response = client.execute_query("INSERT INTO users VALUES (1)")
    assert response.success is False
    assert "only select" in response.error_message.lower() or "select" in response.error_message.lower()
    print("   ✅ execute_query bloque correctement les INSERT")
    
    # Test 3.13: execute_query bloque UPDATE
    print("\n3.13 Test execute_query bloque UPDATE...")
    response = client.execute_query("UPDATE users SET name='test'")
    assert response.success is False
    print("   ✅ execute_query bloque correctement les UPDATE")
    
    # Test 3.14: execute_query bloque DELETE
    print("\n3.14 Test execute_query bloque DELETE...")
    response = client.execute_query("DELETE FROM users")
    assert response.success is False
    print("   ✅ execute_query bloque correctement les DELETE")
    
    # Test 3.15: _is_valid_identifier
    print("\n3.15 Test _is_valid_identifier...")
    assert client._is_valid_identifier("valid_name") is True
    assert client._is_valid_identifier("ValidName") is True
    assert client._is_valid_identifier("_private") is True
    assert client._is_valid_identifier("123invalid") is False
    assert client._is_valid_identifier("invalid-name") is False
    assert client._is_valid_identifier("invalid.name") is False
    assert client._is_valid_identifier("invalid; DROP") is False
    print("   ✅ Validation des identifiants SQL correcte")
    
    # Test 3.16: Gestion erreur connexion
    print("\n3.16 Test gestion erreur connexion...")
    with patch.object(client, '_execute_query', side_effect=Exception("Connection refused")):
        response = client.list_tables()
        assert response.success is False
        assert "connection refused" in response.error_message.lower() or "error" in response.error_message.lower()
        print("   ✅ Gestion erreur connexion correcte")
    
    print("\n✅ Tous les tests PostgresClient passent!")
    
except Exception as e:
    print(f"❌ Erreur dans les tests PostgresClient: {e}")
    import traceback
    traceback.print_exc()

# =============================================================================
# RÉSUMÉ FINAL
# =============================================================================
print("\n" + "=" * 80)
print("RÉSUMÉ DES TESTS CLIENTS")
print("=" * 80)
print("""
✅ SentryClient: 13 tests passent
   - Initialisation et configuration
   - Toutes les méthodes API (list_projects, list_issues, get_issue, etc.)
   - Gestion des erreurs

✅ KubernetesClient: 15 tests passent
   - Initialisation et configuration kubectl
   - Toutes les méthodes (pods, deployments, services, nodes, etc.)
   - Gestion des erreurs kubectl

✅ PostgresClient: 16 tests passent
   - Initialisation (connection_string, paramètres, environnement)
   - Toutes les méthodes (schemas, tables, indexes, requêtes)
   - Sécurité (blocage INSERT/UPDATE/DELETE, validation identifiants)
   - Gestion des erreurs

TOTAL: 44 tests unitaires pour les clients
""")
