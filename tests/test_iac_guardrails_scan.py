"""
Tests unitaires pour l'outil IaC Guardrails Scan refactorisé.
"""
import pytest
from unittest.mock import MagicMock, patch

from collegue.tools.iac_guardrails_scan import (
    IacGuardrailsScanTool,
    IacGuardrailsRequest,
    IacFinding,
    CustomPolicy
)
from collegue.tools.iac_guardrails_scan.engine import IacAnalysisEngine
from collegue.core.shared import FileInput


class TestIacAnalysisEngine:
    """Tests pour le moteur d'analyse."""

    @pytest.fixture
    def engine(self):
        return IacAnalysisEngine(
            k8s_rules={'baseline': [], 'strict': []},
            tf_rules={'baseline': [], 'strict': []},
            dockerfile_rules={'baseline': [], 'strict': []},
            logger=None
        )

    def test_detect_file_type_terraform(self, engine):
        assert engine.detect_file_type('main.tf', 'resource "aws_s3"') == 'terraform'
        assert engine.detect_file_type('main.tf.json', '{}') == 'terraform'

    def test_detect_file_type_kubernetes(self, engine):
        content = 'apiVersion: apps/v1\nkind: Deployment'
        assert engine.detect_file_type('deployment.yaml', content) == 'kubernetes'

    def test_detect_file_type_dockerfile(self, engine):
        assert engine.detect_file_type('Dockerfile', 'FROM ubuntu') == 'dockerfile'
        assert engine.detect_file_type('docker/Dockerfile', 'FROM alpine') == 'dockerfile'

    def test_detect_file_type_unknown(self, engine):
        assert engine.detect_file_type('readme.txt', 'Hello') == 'unknown'

    def test_calculate_security_scores_no_findings(self, engine):
        scores = engine.calculate_security_scores([])
        assert scores == (1.0, 1.0, 'low')

    def test_calculate_security_scores_with_critical(self, engine):
        findings = [
            IacFinding(
                rule_id='TEST-001', severity='critical', path='test.yaml', line=1,
                title='Test', description='Test desc', remediation='Fix it', engine='test'
            )
        ]
        security, compliance, risk = engine.calculate_security_scores(findings)
        assert security < 1.0
        assert risk == 'critical'

    def test_deduplicate_findings(self, engine):
        findings = [
            IacFinding(rule_id='R1', severity='high', path='f.yaml', line=1, title='T', description='D', remediation='R', engine='E'),
            IacFinding(rule_id='R1', severity='high', path='f.yaml', line=1, title='T', description='D', remediation='R', engine='E'),
            IacFinding(rule_id='R2', severity='high', path='f.yaml', line=2, title='T', description='D', remediation='R', engine='E'),
        ]
        deduped = engine.deduplicate_findings(findings)
        assert len(deduped) == 2

    def test_generate_sarif(self, engine):
        findings = [
            IacFinding(
                rule_id='TEST-001', severity='high', path='test.yaml', line=5,
                title='Test Issue', description='Test description',
                remediation='Fix it', references=['https://example.com'], engine='test'
            )
        ]
        sarif = engine.generate_sarif(findings, 1)
        assert '$schema' in sarif
        assert 'runs' in sarif
        assert len(sarif['runs'][0]['results']) == 1


class TestIacGuardrailsScanTool:
    """Tests pour le Tool principal."""

    @pytest.fixture
    def tool(self):
        return IacGuardrailsScanTool(app_state={})

    def test_tool_metadata(self, tool):
        assert tool.tool_name == "iac_guardrails_scan"
        assert "security" in tool.tags
        assert "terraform" in tool.supported_languages

    def test_scan_empty_files(self, tool):
        request = IacGuardrailsRequest(
            files=[FileInput(path='test.yaml', content='')],
            policy_profile='baseline'
        )
        response = tool._execute_core_logic(request)
        assert response.passed is True
        assert response.files_scanned == 1

    def test_scan_kubernetes_host_network(self, tool):
        """Test que le scanner détecte hostNetwork (comportement réel du scanner)."""
        content = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: test
spec:
  template:
    spec:
      hostNetwork: true
      containers:
      - name: app
        image: nginx
"""
        request = IacGuardrailsRequest(
            files=[FileInput(path='deployment.yaml', content=content)],
            policy_profile='baseline'
        )
        response = tool._execute_core_logic(request)
        assert not response.passed  # Should detect hostNetwork
        assert any('hostNetwork' in f.description or 'K8S-002' in f.rule_id 
                   for f in response.findings)

    def test_scan_dockerfile_root_user(self, tool):
        content = "FROM ubuntu:latest\nRUN apt-get update"
        request = IacGuardrailsRequest(
            files=[FileInput(path='Dockerfile', content=content)],
            policy_profile='baseline'
        )
        response = tool._execute_core_logic(request)
        assert not response.passed
        assert any(f.rule_id == 'DOCKER-001' for f in response.findings)

    def test_custom_regex_policy(self, tool):
        content = "FROM ubuntu:latest"
        request = IacGuardrailsRequest(
            files=[FileInput(path='Dockerfile', content=content)],
            policy_profile='baseline',
            custom_policies=[
                CustomPolicy(
                    id='CUSTOM-001',
                    content='ubuntu',
                    language='regex',
                    severity='medium',
                    description='Policy custom: Ubuntu interdit'
                )
            ]
        )
        response = tool._execute_core_logic(request)
        assert any(f.rule_id == 'CUSTOM-001' for f in response.findings)

    def test_sarif_output(self, tool):
        content = "FROM ubuntu:latest"
        request = IacGuardrailsRequest(
            files=[FileInput(path='Dockerfile', content=content)],
            policy_profile='baseline',
            output_format='sarif'
        )
        response = tool._execute_core_logic(request)
        assert response.sarif is not None
        assert response.sarif['version'] == '2.1.0'

    def test_remediation_actions_generation(self, tool):
        findings = [
            IacFinding(
                rule_id='TEST-001', severity='critical', path='test.yaml', line=1,
                title='Critical Issue', description='Critical desc',
                remediation='Fix immediately', engine='test'
            )
        ]
        files = [FileInput(path='test.yaml', content='apiVersion: v1')]
        actions = tool._generate_remediation_actions(findings, files, 0.5)
        assert len(actions) > 0
        assert actions[0].priority == 'critical'


class TestIacFinding:
    """Tests pour le modèle IacFinding."""

    def test_finding_creation(self):
        finding = IacFinding(
            rule_id='TEST-001',
            severity='high',
            path='test.yaml',
            line=10,
            title='Test Title',
            description='Test Description',
            remediation='Fix it',
            references=['https://example.com'],
            engine='test-engine'
        )
        assert finding.rule_id == 'TEST-001'
        assert finding.severity == 'high'
        assert finding.line == 10


class TestCustomPolicy:
    """Tests pour le modèle CustomPolicy."""

    def test_policy_validation(self):
        policy = CustomPolicy(
            id='CUSTOM-001',
            content='password.*=.*".+"',
            language='regex',
            severity='high',
            description='Detect hardcoded passwords'
        )
        assert policy.id == 'CUSTOM-001'
        assert policy.language == 'regex'

    def test_policy_default_values(self):
        policy = CustomPolicy(
            id='CUSTOM-002',
            content='pattern'
        )
        assert policy.language == 'yaml-rules'
        assert policy.severity == 'medium'
