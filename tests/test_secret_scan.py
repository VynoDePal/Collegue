"""
Tests unitaires pour l'outil Secret Scan refactorisé.
"""
import pytest
from unittest.mock import MagicMock

from collegue.tools.secret_scan import (
    SecretScanTool,
    SecretScanRequest,
    SecretFinding
)
from collegue.tools.secret_scan.engine import SecretDetectionEngine


class TestSecretDetectionEngine:
    """Tests pour le moteur de détection."""

    @pytest.fixture
    def engine(self):
        return SecretDetectionEngine(logger=None)

    def test_mask_secret_short(self, engine):
        """Test le masquage d'un secret court."""
        masked = engine.mask_secret("abc", visible_chars=2)
        assert masked == "***"

    def test_mask_secret_long(self, engine):
        """Test le masquage d'un secret long."""
        secret = "sk-1234567890abcdef"
        masked = engine.mask_secret(secret, visible_chars=4)
        assert masked.startswith("sk-1")
        assert masked.endswith("cdef")
        assert "*" in masked

    def test_get_recommendation_aws(self, engine):
        """Test la recommandation pour AWS."""
        rec = engine.get_recommendation("aws_access_key")
        assert "AWS Secrets Manager" in rec

    def test_get_recommendation_github(self, engine):
        """Test la recommandation pour GitHub."""
        rec = engine.get_recommendation("github_token")
        assert "github.com/settings/tokens" in rec

    def test_get_recommendation_default(self, engine):
        """Test la recommandation par défaut."""
        rec = engine.get_recommendation("unknown_secret_type")
        assert "variables d'environnement" in rec

    def test_scan_content_aws_key(self, engine):
        """Test la détection d'une clé AWS."""
        content = "aws_key = 'AKIAIOSFODNN7EXAMPLE'"
        findings = engine.scan_content(content, "test.py", "low")
        assert len(findings) > 0
        assert any(f.type == "aws_access_key" for f in findings)

    def test_scan_content_openai_key(self, engine):
        """Test la détection d'une clé OpenAI."""
        # La clé doit faire exactement 48 caractères après "sk-"
        content = "api_key = 'sk-1234567890abcdefghijklmnopqrstuvwxyz1234567890abcd'"
        findings = engine.scan_content(content, "test.py", "low")
        assert len(findings) > 0
        assert any(f.type == "openai_api_key" for f in findings)

    def test_scan_content_no_secrets(self, engine):
        """Test le scan de contenu sans secrets."""
        content = "print('Hello World')"
        findings = engine.scan_content(content, "test.py", "low")
        assert len(findings) == 0

    def test_scan_content_severity_filter(self, engine):
        """Test le filtrage par sévérité."""
        content = "password = 'mysecretpassword123'"
        # Seuil low -> devrait trouver
        findings_low = engine.scan_content(content, "test.py", "low")
        # Seuil critical -> ne devrait pas trouver (password est medium)
        findings_critical = engine.scan_content(content, "test.py", "critical")
        assert len(findings_low) > 0
        assert len(findings_critical) == 0

    def test_should_scan_file_by_extension(self, engine):
        """Test la sélection des fichiers par extension."""
        assert engine.should_scan_file("test.py", [], []) is True
        assert engine.should_scan_file("test.js", [], []) is True
        assert engine.should_scan_file("test.exe", [], []) is False

    def test_should_scan_file_exclude_pattern(self, engine):
        """Test l'exclusion par pattern."""
        assert engine.should_scan_file("node_modules/package.json", [], ["node_modules"]) is False
        assert engine.should_scan_file("src/main.py", [], ["node_modules"]) is True

    def test_should_scan_file_include_pattern(self, engine):
        """Test l'inclusion par pattern."""
        assert engine.should_scan_file("src/test.py", ["*.py"], []) is True
        assert engine.should_scan_file("src/test.js", ["*.py"], []) is False

    def test_deduplicate_findings(self, engine):
        """Test la déduplication des findings."""
        findings = [
            SecretFinding(type="aws", severity="high", file="f.py", line=1, column=0,
                         match="AKIA...", rule="AWS", recommendation="Fix"),
            SecretFinding(type="aws", severity="high", file="f.py", line=1, column=0,
                         match="AKIA...", rule="AWS", recommendation="Fix"),
            SecretFinding(type="github", severity="high", file="f.py", line=2, column=0,
                         match="ghp...", rule="GitHub", recommendation="Fix"),
        ]
        deduped = engine.deduplicate_findings(findings)
        assert len(deduped) == 2


class TestSecretScanTool:
    """Tests pour le Tool principal."""

    @pytest.fixture
    def tool(self):
        return SecretScanTool(app_state={})

    def test_tool_metadata(self, tool):
        """Test les métadonnées du tool."""
        assert tool.tool_name == "secret_scan"
        assert "security" in tool.tags
        assert "python" in tool.supported_languages

    def test_validate_language_any(self, tool):
        """Test que n'importe quel langage est accepté."""
        assert tool.validate_language("python") is True
        assert tool.validate_language("unknown_lang") is True

    def test_scan_batch_files(self, tool):
        """Test le scan batch de fichiers."""
        from collegue.tools.secret_scan.models import SecretScanFile
        # Clé AWS valide (20 caractères après AKIA)
        files = [
            SecretScanFile(path="config.py", content="api_key = 'AKIAIOSFODNN7EXAMPLE'"),
            SecretScanFile(path="clean.py", content="print('hello')")
        ]
        request = SecretScanRequest(files=files, severity_threshold="low")
        response = tool._execute_core_logic(request)
        
        assert response.files_scanned == 2
        assert response.total_findings >= 1  # Au moins la clé AWS
        assert "config.py" in response.files_with_secrets

    def test_scan_content_direct(self, tool):
        """Test le scan de contenu direct."""
        request = SecretScanRequest(
            content="aws_secret = 'AKIAIOSFODNN7EXAMPLE'",
            scan_type="content"
        )
        response = tool._execute_core_logic(request)
        
        assert response.files_scanned == 1
        assert response.total_findings >= 1
        assert response.clean is False

    def test_scan_no_secrets(self, tool):
        """Test le scan sans secrets."""
        request = SecretScanRequest(
            content="print('Hello World')",
            scan_type="content"
        )
        response = tool._execute_core_logic(request)
        
        assert response.clean is True
        assert response.total_findings == 0

    def test_severity_threshold(self, tool):
        """Test le filtrage par sévérité."""
        # Password hardcodé est de sévérité medium
        request = SecretScanRequest(
            content="password = 'mysecret123'",
            scan_type="content",
            severity_threshold="critical"  # Seulement critical
        )
        response = tool._execute_core_logic(request)
        
        # Ne devrait pas détecter car password est medium
        assert response.total_findings == 0


class TestSecretFinding:
    """Tests pour le modèle SecretFinding."""

    def test_finding_creation(self):
        """Test la création d'un finding."""
        finding = SecretFinding(
            type="aws_access_key",
            severity="critical",
            file="config.py",
            line=10,
            column=15,
            match="AKIA****EXAMPLE",
            rule="Clé d'accès AWS",
            recommendation="Utilisez AWS Secrets Manager"
        )
        assert finding.type == "aws_access_key"
        assert finding.severity == "critical"
        assert finding.line == 10


class TestSecretScanRequest:
    """Tests pour le modèle SecretScanRequest."""

    def test_request_validation_scan_type(self):
        """Test la validation du scan_type."""
        with pytest.raises(ValueError):
            SecretScanRequest(target=".", scan_type="invalid")

    def test_request_validation_severity(self):
        """Test la validation de severity_threshold."""
        with pytest.raises(ValueError):
            SecretScanRequest(target=".", severity_threshold="invalid")

    def test_request_post_init_no_source(self):
        """Test la validation qu'aucune source n'est fournie."""
        with pytest.raises(ValueError):
            SecretScanRequest()

    def test_request_valid(self):
        """Test une requête valide."""
        request = SecretScanRequest(target=".", scan_type="directory")
        assert request.scan_type == "directory"
        assert request.severity_threshold == "low"
