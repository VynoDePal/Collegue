"""
Tests unitaires pour l'outil Dependency Guard refactorisé.
"""
import pytest
from unittest.mock import MagicMock, patch
import json

from collegue.tools.dependency_guard import (
    DependencyGuardTool,
    DependencyGuardRequest,
    DependencyIssue
)
from collegue.tools.dependency_guard.engine import DependencyAnalysisEngine


class TestDependencyAnalysisEngine:
    """Tests pour le moteur d'analyse."""

    @pytest.fixture
    def engine(self):
        return DependencyAnalysisEngine(logger=None)

    def test_parse_requirements_txt(self, engine):
        """Test le parsing de requirements.txt."""
        content = """
django>=4.0
requests==2.28.0
flask
# comment
-r other.txt
"""
        deps = engine.parse_requirements_txt(content)
        assert len(deps) == 3
        assert deps[0]['name'] == 'django'
        assert deps[0]['version'] == '>=4.0'
        assert deps[2]['name'] == 'flask'
        assert deps[2]['version'] == '*'

    def test_parse_package_json(self, engine):
        """Test le parsing de package.json."""
        content = json.dumps({
            "dependencies": {"axios": "^1.0.0", "lodash": "^4.17.0"},
            "devDependencies": {"jest": "^29.0.0"}
        })
        deps = engine.parse_package_json(content)
        assert len(deps) == 3
        assert any(d['name'] == 'axios' for d in deps)
        assert any(d['name'] == 'jest' for d in deps)

    def test_parse_composer_json(self, engine):
        """Test le parsing de composer.json."""
        content = json.dumps({
            "require": {"symfony/console": "^6.0", "php": ">=8.1"},
            "require-dev": {"phpunit/phpunit": "^10.0"}
        })
        deps = engine.parse_composer_json(content)
        assert len(deps) == 2  # php est exclu
        assert any(d['name'] == 'symfony/console' for d in deps)

    def test_extract_version(self, engine):
        """Test l'extraction de version."""
        assert engine.extract_version('>=1.2.3') == '1.2.3'
        assert engine.extract_version('==2.0.0') == '2.0.0'
        assert engine.extract_version('^3.0.0-beta') == '3.0.0-beta'
        assert engine.extract_version('*') == ''

    def test_detect_content_type_requirements(self, engine):
        """Test la détection de type pour requirements.txt."""
        content = "django>=4.0\nrequests>=2.28"
        assert engine.detect_content_type(content, 'python') == 'requirements.txt'

    def test_detect_content_type_package_json(self, engine):
        """Test la détection de type pour package.json."""
        content = '{"dependencies": {"axios": "^1.0.0"}}'
        assert engine.detect_content_type(content, 'javascript') == 'package.json'

    def test_detect_content_type_package_lock(self, engine):
        """Test la détection de type pour package-lock.json."""
        content = '{"lockfileVersion": 3, "packages": {}}'
        assert engine.detect_content_type(content, 'javascript') == 'package-lock.json'

    def test_extract_osv_severity(self, engine):
        """Test l'extraction de sévérité OSV."""
        vuln = {'database_specific': {'severity': 'HIGH'}}
        assert engine.extract_osv_severity(vuln) == 'high'
        
        vuln = {'database_specific': {'severity': 'CRITICAL'}}
        assert engine.extract_osv_severity(vuln) == 'critical'


class TestDependencyGuardTool:
    """Tests pour le Tool principal."""

    @pytest.fixture
    def tool(self):
        return DependencyGuardTool(app_state={})

    def test_tool_metadata(self, tool):
        """Test les métadonnées du tool."""
        assert tool.tool_name == "dependency_guard"
        assert "security" in tool.tags
        assert "python" in tool.supported_languages

    def test_scan_no_issues(self, tool):
        """Test le scan sans problèmes (mock)."""
        with patch.object(tool._engine, 'check_package_existence') as mock_check:
            mock_check.return_value = {'exists': True, 'latest_version': '1.0.0'}
            
            request = DependencyGuardRequest(
                content="django>=4.0\nrequests>=2.28",
                language="python",
                check_vulnerabilities=False
            )
            response = tool._execute_core_logic(request)
            
            assert response.total_dependencies == 2
            assert response.valid is True

    def test_scan_malicious_package(self, tool):
        """Test la détection de package malveillant."""
        with patch.object(tool._engine, 'check_package_existence') as mock_check:
            mock_check.return_value = {'exists': True}
            
            request = DependencyGuardRequest(
                content="request>=2.28",  # Package malveillant
                language="python",
                check_vulnerabilities=False
            )
            response = tool._execute_core_logic(request)
            
            assert any(i.issue_type == 'malicious' for i in response.issues)
            assert response.critical >= 1

    def test_scan_deprecated_package(self, tool):
        """Test la détection de package déprécié."""
        with patch.object(tool._engine, 'check_package_existence') as mock_check:
            mock_check.return_value = {'exists': True}
            
            request = DependencyGuardRequest(
                content="pycrypto>=2.6",  # Déprécié
                language="python",
                check_vulnerabilities=False
            )
            response = tool._execute_core_logic(request)
            
            assert any(i.issue_type == 'deprecated' for i in response.issues)

    def test_scan_blocklist(self, tool):
        """Test la blocklist."""
        with patch.object(tool._engine, 'check_package_existence') as mock_check:
            mock_check.return_value = {'exists': True}

            request = DependencyGuardRequest(
                content="suspicious-package>=1.0",
                language="python",
                check_vulnerabilities=False,
                blocklist=["suspicious-package"]
            )
            response = tool._execute_core_logic(request)

            assert any(i.issue_type == 'blocked' for i in response.issues)

    def test_scan_allowlist(self, tool):
        """Test que l'allowlist signale tout package hors liste."""
        request = DependencyGuardRequest(
            content="django==4.0\nflask==2.0\nrequests==2.28",
            language="python",
            check_existence=False,
            check_vulnerabilities=False,
            allowlist=["django", "requests"],
        )
        response = tool._execute_core_logic(request)

        not_allowed = [i for i in response.issues if i.issue_type == "not_allowed"]
        assert len(not_allowed) == 1
        assert not_allowed[0].package == "flask"

    def test_scan_nonexistent_package(self, tool):
        """Test la détection de package inexistant."""
        with patch.object(tool._engine, 'check_package_existence') as mock_check:
            mock_check.return_value = {'exists': False}
            
            request = DependencyGuardRequest(
                content="fake-package-12345>=1.0",
                language="python",
                check_vulnerabilities=False
            )
            response = tool._execute_core_logic(request)
            
            assert any(i.issue_type == 'not_found' for i in response.issues)


class TestDependencyIssue:
    """Tests pour le modèle DependencyIssue."""

    def test_issue_creation(self):
        """Test la création d'un issue."""
        issue = DependencyIssue(
            package="django",
            version="4.0.0",
            issue_type="vulnerable",
            severity="high",
            message="Vulnérabilité XSS",
            recommendation="Mettre à jour",
            cve_ids=["CVE-2023-12345"]
        )
        assert issue.package == "django"
        assert issue.severity == "high"
        assert issue.cve_ids == ["CVE-2023-12345"]


class TestDependencyGuardRequest:
    """Tests pour le modèle DependencyGuardRequest."""

    def test_request_language_validation_typescript(self):
        """Test la validation du langage TypeScript."""
        request = DependencyGuardRequest(content="{}", language="typescript")
        assert request.language == "javascript"

    def test_request_language_validation_js(self):
        """Test la validation du langage JS."""
        request = DependencyGuardRequest(content="{}", language="JS")
        assert request.language == "javascript"

    def test_request_language_validation_invalid(self):
        """Test la validation d'un langage invalide."""
        with pytest.raises(ValueError):
            DependencyGuardRequest(content="{}", language="ruby")
