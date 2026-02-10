"""
Tests intégrés pour les outils MCP Collègue générés par le MCP test_generation
"""
import os
import sys
from unittest.mock import Mock, patch, MagicMock
import json
import pytest
from pathlib import Path

# Test des outils MCP Collègue

class TestMCPTools:
    """Suite de tests pour les outils MCP Collègue."""
    
    def setup_method(self):
        """Setup pour chaque test."""
        self.test_dir = Path("/tmp/test_mcp")
        self.test_dir.mkdir(exist_ok=True)
    
    def test_repo_consistency_check_imports(self):
        """Test le détecteur d'imports inutilisés."""
        # Créer un fichier avec imports inutilisés
        test_file = self.test_dir / "test.py"
        test_file.write_text("""
import os
import sys
import json  # Non utilisé
from pathlib import Path
from datetime import datetime  # Non utilisé

def main():
    print(os.getcwd())
    print(Path.cwd())
    return True
        """)
        
        # Appeler l'outil
        from collegue.tools.repo_consistency_check import RepoConsistencyCheckTool, ConsistencyCheckRequest, FileInput
        
        tool = RepoConsistencyCheckTool()
        request = ConsistencyCheckRequest(
            files=[FileInput(path=str(test_file), content=test_file.read_text())],
            checks=["unused_imports"],
            language="python"
        )
        
        response = tool.execute(request)
        
        # Assertions
        assert response.success is True
        assert len(response.issues) >= 2  # json et datetime non utilisés
        unused_imports = [i for i in response.issues if i.kind == "unused_import"]
        assert len(unused_imports) >= 2
    
    def test_secret_scan_detection(self):
        """Test le détecteur de secrets."""
        # Créer un fichier avec des secrets
        test_file = self.test_dir / "config.py"
        test_file.write_text("""
# Configuration
API_KEY = "sk-1234567890abcdef"
DB_PASSWORD = "my_secret_password"
GITHUB_TOKEN = "ghp_xxxxxxxxxxxxxxxxxxxx"

# Variable normale
DEBUG = True
        """)
        
        from collegue.tools.secret_scan import SecretScanTool, SecretScanRequest, FileContent
        
        tool = SecretScanTool()
        request = SecretScanRequest(
            files=[FileContent(path=str(test_file), content=test_file.read_text())],
            severity_threshold="medium"
        )
        
        response = tool.execute(request)
        
        # Assertions
        assert response.success is True
        assert len(response.secrets) >= 3  # API_KEY, DB_PASSWORD, GITHUB_TOKEN
        
        # Vérifier les types de secrets détectés
        secret_types = [s.type for s in response.secrets]
        assert "api_key" in secret_types or "llm_key" in secret_types
        assert "password" in secret_types
        assert "vcs_token" in secret_types
    
    def test_dependency_guard_vulnerabilities(self):
        """Test le détecteur de vulnérabilités."""
        # Simuler un requirements.txt avec vulnérabilités
        requirements = """
django==3.2.0
requests==2.25.0
urllib3==1.26.0
        """
        
        from collegue.tools.dependency_guard import DependencyGuardTool, DependencyGuardRequest
        
        tool = DependencyGuardTool()
        request = DependencyGuardRequest(
            content=requirements,
            language="python",
            check_vulnerabilities=True
        )
        
        response = tool.execute(request)
        
        # Assertions
        assert response.success is True
        assert response.total_dependencies >= 3
        
        # Vérifier que les CVE sont détectées (peut varier selon la base OSV)
        if response.vulnerabilities > 0:
            assert len(response.issues) > 0
            cve_issues = [i for i in response.issues if "CVE" in i.message]
            assert len(cve_issues) > 0
    
    def test_impact_analysis_scope(self):
        """Test l'analyse d'impact."""
        # Simuler une modification de base de données
        files = [
            {
                "path": "models/user.py",
                "content": """
class User:
    def __init__(self, id, name):
        self.id = id
        self.name = name
        self.email = None  # Nouveau champ
                """
            },
            {
                "path": "services/auth.py",
                "content": """
def authenticate(email, password):
    # Vérifier l'utilisateur en base
    user = find_user_by_email(email)
    return user is not None
                """
            }
        ]
        
        from collegue.tools.impact_analysis import ImpactAnalysisTool, ImpactAnalysisRequest, FileInput
        
        tool = ImpactAnalysisTool()
        request = ImpactAnalysisRequest(
            change_intent="Ajouter le champ email aux utilisateurs",
            files=[FileInput(**f) for f in files],
            analysis_depth="fast"
        )
        
        response = tool.execute(request)
        
        # Assertions
        assert response.success is True
        assert len(response.affected_files) >= 1
        
        # Vérifier que les recommandations sont pertinentes
        assert len(response.recommendations) > 0
        rec_types = [r.lower() for r in response.recommendations]
        assert any("migration" in r or "data" in r for r in rec_types)
    
    def test_code_refactoring_simplify(self):
        """Test le refactoring de simplification."""
        code_complexe = """
def process_items(items):
    result = []
    for i in range(len(items)):
        if items[i] is not None:
            if items[i].get("active") == True:
                result.append(items[i]["value"])
    return result
        """
        
        from collegue.tools.code_refactoring import CodeRefactoringTool, RefactoringRequest
        
        tool = CodeRefactoringTool()
        request = RefactoringRequest(
            code=code_complexe,
            language="python",
            refactoring_type="simplify"
        )
        
        response = tool.execute(request)
        
        # Assertions
        assert response.success is True
        assert response.refactored_code is not None
        assert len(response.refactored_code) > 0
        
        # Le code refactorisé devrait être plus simple
        assert "if" in response.refactored_code  # Toujours présent
        # Vérifier qu'il n'y a plus de conditions imbriquées inutiles
    
    def test_kubernetes_ops_mock(self):
        """Test les opérations Kubernetes avec mock."""
        from unittest.mock import patch
        from collegue.tools.kubernetes_ops import KubernetesOpsTool, KubernetesRequest
        
        tool = KubernetesOpsTool()
        
        # Mock kubectl
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout='{"items": []}',
                stderr=''
            )
            
            request = KubernetesRequest(
                command="list_pods",
                namespace="default"
            )
            
            response = tool.execute(request)
            
            # Assertions
            assert response.success is True
            assert response.command == "list_pods"
            assert "pods" in response.message.lower()
    
    def test_postgres_db_mock(self):
        """Test les opérations PostgreSQL avec mock."""
        from unittest.mock import patch
        from collegue.tools.postgres_db import PostgresDBTool, PostgresRequest
        
        tool = PostgresDBTool()
        
        # Mock psycopg2
        with patch('psycopg2.connect') as mock_connect:
            mock_conn = Mock()
            mock_cursor = Mock()
            mock_cursor.fetchall.return_value = [
                {"table_name": "users", "table_type": "BASE TABLE"}
            ]
            mock_conn.cursor.return_value = mock_cursor
            mock_connect.return_value = mock_conn
            
            request = PostgresRequest(
                command="list_tables",
                connection_string="postgresql://test:test@localhost/test"
            )
            
            response = tool.execute(request)
            
            # Assertions
            assert response.success is True
            assert len(response.tables) >= 1
            assert response.tables[0].name == "users"
    
    def test_sentry_monitor_mock(self):
        """Test le monitoring Sentry avec mock."""
        from unittest.mock import patch
        from collegue.tools.sentry_monitor import SentryMonitorTool, SentryRequest
        
        tool = SentryMonitorTool()
        
        # Mock requests
        with patch('requests.get') as mock_get:
            mock_get.return_value = Mock(
                status_code=200,
                json=lambda: [
                    {"id": "1", "title": "Error 1", "level": "error"},
                    {"id": "2", "title": "Warning", "level": "warning"}
                ]
            )
            
            request = SentryRequest(
                command="list_issues",
                organization="test-org",
                token="test-token"
            )
            
            response = tool.execute(request)
            
            # Assertions
            assert response.success is True
            assert len(response.issues) >= 2
            assert response.issues[0].title == "Error 1"
    
    def test_github_ops_mock(self):
        """Test les opérations GitHub avec mock."""
        from unittest.mock import patch
        from collegue.tools.github_ops import GitHubOpsTool, GitHubRequest
        
        tool = GitHubOpsTool()
        
        # Mock requests
        with patch('requests.get') as mock_get:
            mock_get.return_value = Mock(
                status_code=200,
                json=lambda: [
                    {"name": "repo1", "full_name": "user/repo1", "private": False},
                    {"name": "repo2", "full_name": "user/repo2", "private": True}
                ]
            )
            
            request = GitHubRequest(
                command="list_repos",
                owner="user",
                token="test-token"
            )
            
            response = tool.execute(request)
            
            # Assertions
            assert response.success is True
            assert len(response.repos) >= 2
            assert response.repos[0].name == "repo1"
    
    def test_iac_guardrails_scan(self):
        """Test le scan IaC."""
        # Créer un fichier Terraform avec problèmes
        terraform_file = self.test_dir / "main.tf"
        terraform_file.write_text("""
resource "aws_security_group" "example" {
    name        = "example"
    description = "Example security group"
    
    ingress {
        from_port   = 0
        to_port     = 0
        protocol    = "-1"
        cidr_blocks = ["0.0.0.0/0"]  # Trop permissif
    }
    
    egress {
        from_port   = 0
        to_port     = 0
        protocol    = "-1"
        cidr_blocks = ["0.0.0.0/0"]  # Trop permissif
    }
}
        """)
        
        from collegue.tools.iac_guardrails_scan import IacGuardrailsScanTool, IacGuardrailsRequest, FileInput
        
        tool = IacGuardrailsScanTool()
        request = IacGuardrailsRequest(
            files=[FileInput(path=str(terraform_file), content=terraform_file.read_text())],
            policy_profile="baseline"
        )
        
        response = tool.execute(request)
        
        # Assertions
        assert response.success is True
        assert len(response.issues) >= 1  # Au moins une règle de sécurité
        
        # Vérifier que les problèmes de sécurité sont détectés
        security_issues = [i for i in response.issues if i.severity in ["high", "critical"]]
        assert len(security_issues) >= 1
    
    def teardown_method(self):
        """Nettoyage après chaque test."""
        import shutil
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)

if __name__ == "__main__":
    # Exécuter tous les tests
    import sys
    
    test_suite = TestMCPTools()
    test_suite.setup_method()
    
    try:
        test_suite.test_repo_consistency_check_imports()
        print("✅ test_repo_consistency_check_imports")
    except Exception as e:
        print(f"❌ test_repo_consistency_check_imports: {e}")
    
    try:
        test_suite.test_secret_scan_detection()
        print("✅ test_secret_scan_detection")
    except Exception as e:
        print(f"❌ test_secret_scan_detection: {e}")
    
    try:
        test_suite.test_dependency_guard_vulnerabilities()
        print("✅ test_dependency_guard_vulnerabilities")
    except Exception as e:
        print(f"❌ test_dependency_guard_vulnerabilities: {e}")
    
    try:
        test_suite.test_impact_analysis_scope()
        print("✅ test_impact_analysis_scope")
    except Exception as e:
        print(f"❌ test_impact_analysis_scope: {e}")
    
    try:
        test_suite.test_code_refactoring_simplify()
        print("✅ test_code_refactoring_simplify")
    except Exception as e:
        print(f"❌ test_code_refactoring_simplify: {e}")
    
    try:
        test_suite.test_kubernetes_ops_mock()
        print("✅ test_kubernetes_ops_mock")
    except Exception as e:
        print(f"❌ test_kubernetes_ops_mock: {e}")
    
    try:
        test_suite.test_postgres_db_mock()
        print("✅ test_postgres_db_mock")
    except Exception as e:
        print(f"❌ test_postgres_db_mock: {e}")
    
    try:
        test_suite.test_sentry_monitor_mock()
        print("✅ test_sentry_monitor_mock")
    except Exception as e:
        print(f"❌ test_sentry_monitor_mock: {e}")
    
    try:
        test_suite.test_github_ops_mock()
        print("✅ test_github_ops_mock")
    except Exception as e:
        print(f"❌ test_github_ops_mock: {e}")
    
    try:
        test_suite.test_iac_guardrails_scan()
        print("✅ test_iac_guardrails_scan")
    except Exception as e:
        print(f"❌ test_iac_guardrails_scan: {e}")
    
    test_suite.teardown_method()
    print("\n✅ Tous les tests MCP terminés!")
