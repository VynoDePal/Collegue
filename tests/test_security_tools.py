"""
Tests unitaires pour les outils de qualité et sécurité (T14)

- secret_scan
- dependency_guard
"""
import os
import sys
import unittest
import tempfile
import shutil


parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, parent_dir)

from collegue.tools.secret_scan import SecretScanTool, SecretScanRequest, SecretScanResponse, SecretFinding
from collegue.tools.dependency_guard import DependencyGuardTool, DependencyGuardRequest, DependencyGuardResponse


class TestSecretScanTool(unittest.TestCase):
    """Tests pour l'outil secret_scan."""

    def setUp(self):
        self.tool = SecretScanTool()

    def test_tool_metadata(self):
        self.assertEqual(self.tool.get_name(), "secret_scan")
        self.assertIn("python", self.tool.get_supported_languages())
        self.assertFalse(self.tool.is_long_running())
        print("✅ Métadonnées secret_scan correctes")

    def test_detect_aws_key(self):
        code = '''
config = {
    "aws_access_key": "AKIAIOSFODNN7EXAMPLE",
    "region": "us-east-1"
}
'''
        findings = self.tool._scan_content(code)
        self.assertGreater(len(findings), 0)
        self.assertTrue(any('aws' in f.type.lower() for f in findings))
        print("✅ Détection clé AWS")

    def test_detect_openai_key(self):
        code = 'api_key = "sk-1234567890abcdef1234567890abcdef1234567890abcdef"'
        findings = self.tool._scan_content(code)
        self.assertGreater(len(findings), 0)
        self.assertTrue(any('openai' in f.type.lower() for f in findings))
        print("✅ Détection clé OpenAI")

    def test_detect_github_token(self):
        code = 'GITHUB_TOKEN = "ghp_1234567890abcdef1234567890abcdef1234"'
        findings = self.tool._scan_content(code)
        self.assertGreater(len(findings), 0)
        self.assertTrue(any('github' in f.type.lower() for f in findings))
        print("✅ Détection token GitHub")

    def test_detect_private_key(self):
        code = '''
-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEA2Z3qX2BTLS4e0...
-----END RSA PRIVATE KEY-----
'''
        findings = self.tool._scan_content(code)
        self.assertGreater(len(findings), 0)
        self.assertTrue(any('private_key' in f.type.lower() for f in findings))
        self.assertEqual(findings[0].severity, 'critical')
        print("✅ Détection clé privée RSA")

    def test_detect_password_in_url(self):
        code = 'db_url = "postgres://user:supersecret123@localhost/db"'
        findings = self.tool._scan_content(code)
        self.assertGreater(len(findings), 0)
        print("✅ Détection password dans URL")

    def test_clean_code(self):
        code = '''
import os

def get_api_key():
    return os.environ.get("API_KEY")

class Config:
    DEBUG = True
    DATABASE_URL = os.getenv("DATABASE_URL")
'''
        findings = self.tool._scan_content(code)
        critical = [f for f in findings if f.severity == 'critical']
        self.assertEqual(len(critical), 0)
        print("✅ Code propre sans faux positifs critiques")

    def test_mask_secret(self):
        secret = "sk-1234567890abcdef"
        masked = self.tool._mask_secret(secret)
        self.assertIn("****", masked)
        self.assertTrue(masked.startswith("sk-1"))
        self.assertTrue(masked.endswith("cdef"))
        print("✅ Masquage des secrets")

    def test_scan_file(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write('API_KEY = "sk-1234567890abcdef1234567890abcdef1234567890abcdef"\n')
            f.flush()

            try:
                findings = self.tool._scan_file(f.name, 'low', 1024*1024)
                self.assertGreater(len(findings), 0)
            finally:
                os.unlink(f.name)

        print("✅ Scan de fichier")

    def test_full_scan_content(self):
        request = SecretScanRequest(
            target='api_key = "AKIAIOSFODNN7EXAMPLE"',
            scan_type='content'
        )

        response = self.tool._execute_core_logic(request)
        self.assertIsInstance(response, SecretScanResponse)
        self.assertFalse(response.clean)
        self.assertGreater(response.total_findings, 0)
        print("✅ Scan complet de contenu")


class TestDependencyGuardTool(unittest.TestCase):
    """Tests pour l'outil dependency_guard."""

    def setUp(self):
        self.tool = DependencyGuardTool()

    def test_tool_metadata(self):
        self.assertEqual(self.tool.get_name(), "dependency_guard")
        self.assertIn("python", self.tool.get_supported_languages())
        self.assertTrue(self.tool.is_long_running())
        print("✅ Métadonnées dependency_guard correctes")

    def test_parse_requirements_txt(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write('''
# Dépendances principales
django==4.2.0
requests>=2.28.0
flask[async]
numpy
# Commentaire
-e git+https://github.com/user/repo.git
''')
            f.flush()

            try:
                deps = self.tool._parse_requirements_txt(f.name)
                dep_names = [d['name'] for d in deps]

                self.assertIn('django', dep_names)
                self.assertIn('requests', dep_names)
                self.assertIn('flask', dep_names)
                self.assertIn('numpy', dep_names)

                django_dep = next(d for d in deps if d['name'] == 'django')
                self.assertEqual(django_dep['version'], '==4.2.0')
            finally:
                os.unlink(f.name)

        print("✅ Parsing requirements.txt")

    def test_parse_package_json(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write('''{
    "name": "test-project",
    "dependencies": {
        "express": "^4.18.0",
        "lodash": "~4.17.21"
    },
    "devDependencies": {
        "jest": "^29.0.0"
    }
}''')
            f.flush()

            try:
                deps = self.tool._parse_package_json(f.name)
                dep_names = [d['name'] for d in deps]

                self.assertIn('express', dep_names)
                self.assertIn('lodash', dep_names)
                self.assertIn('jest', dep_names)
            finally:
                os.unlink(f.name)

        print("✅ Parsing package.json")

    def test_detect_deprecated_packages(self):
        request = DependencyGuardRequest(
            content='pycrypto==2.6.1\nnose==1.3.7',
            language='python',
            check_existence=False,
            check_vulnerabilities=False
        )

        response = self.tool._execute_core_logic(request)

        deprecated_issues = [i for i in response.issues if i.issue_type == 'deprecated']
        deprecated_packages = [i.package for i in deprecated_issues]

        self.assertIn('pycrypto', deprecated_packages)
        self.assertIn('nose', deprecated_packages)

        print("✅ Détection packages dépréciés")

    def test_blocklist_check(self):
        request = DependencyGuardRequest(
            content='django==4.0\nflask==2.0',
            language='python',
            check_existence=False,
            check_vulnerabilities=False,
            blocklist=['flask']
        )

        response = self.tool._execute_core_logic(request)

        blocked_issues = [i for i in response.issues if i.issue_type == 'blocked']
        self.assertEqual(len(blocked_issues), 1)
        self.assertEqual(blocked_issues[0].package, 'flask')

        print("✅ Vérification blocklist")

    def test_allowlist_check(self):
        request = DependencyGuardRequest(
            content='django==4.0\nflask==2.0\nrequests==2.28',
            language='python',
            check_existence=False,
            check_vulnerabilities=False,
            allowlist=['django', 'requests']
        )

        response = self.tool._execute_core_logic(request)

        not_allowed = [i for i in response.issues if i.issue_type == 'not_allowed']
        self.assertEqual(len(not_allowed), 1)
        self.assertEqual(not_allowed[0].package, 'flask')

        print("✅ Vérification allowlist")

    def test_malicious_package_detection(self):

        request = DependencyGuardRequest(
            content='request==1.0',
            language='python',
            check_existence=False,
            check_vulnerabilities=False
        )

        response = self.tool._execute_core_logic(request)

        malicious_issues = [i for i in response.issues if i.issue_type == 'malicious']
        self.assertGreater(len(malicious_issues), 0)
        self.assertEqual(malicious_issues[0].severity, 'critical')

        print("✅ Détection packages malveillants")


class TestToolsIntegration(unittest.TestCase):
    """Tests d'intégration des outils de sécurité."""

    def test_all_tools_registered(self):
        from collegue.tools import get_registry

        registry = get_registry()
        tool_names = registry.list_tools()

        self.assertIn('SecretScanTool', tool_names)
        self.assertIn('DependencyGuardTool', tool_names)

        print("✅ Tous les outils de sécurité sont enregistrés")

    def test_tools_inherit_base_tool(self):
        from collegue.tools.base import BaseTool

        self.assertIsInstance(SecretScanTool(), BaseTool)
        self.assertIsInstance(DependencyGuardTool(), BaseTool)

        print("✅ Tous les outils héritent de BaseTool")


class TestTestGenerationValidation(unittest.TestCase):
    """Tests pour l'intégration test_generation + run_tests."""

    def test_validation_request_fields(self):
        from collegue.tools.test_generation import TestGenerationRequest, TestValidationResult

        request = TestGenerationRequest(
            code="def add(a, b): return a + b",
            language="python",
            validate_tests=True,
            working_dir="/tmp"
        )

        self.assertTrue(request.validate_tests)
        self.assertEqual(request.working_dir, "/tmp")
        print("✅ Champs validate_tests et working_dir présents")

    def test_validation_result_model(self):
        from collegue.tools.test_generation import TestValidationResult

        result = TestValidationResult(
            validated=True,
            success=True,
            total=5,
            passed=5,
            failed=0,
            errors=0,
            duration=1.5
        )

        self.assertTrue(result.validated)
        self.assertTrue(result.success)
        self.assertEqual(result.total, 5)
        print("✅ Modèle TestValidationResult valide")

    def test_response_includes_validation(self):
        from collegue.tools.test_generation import TestGenerationResponse, TestValidationResult

        validation = TestValidationResult(
            validated=True,
            success=True,
            total=3,
            passed=3,
            failed=0,
            errors=0,
            duration=0.5
        )

        response = TestGenerationResponse(
            test_code="def test_add(): assert add(1, 2) == 3",
            language="python",
            framework="pytest",
            estimated_coverage=0.8,
            tested_elements=[{"type": "function", "name": "add"}],
            validation_result=validation
        )

        self.assertIsNotNone(response.validation_result)
        self.assertTrue(response.validation_result.success)
        print("✅ TestGenerationResponse inclut validation_result")


if __name__ == '__main__':
    print("=" * 60)
    print("Tests des outils de Qualité et Sécurité (T14)")
    print("=" * 60)

    unittest.main(verbosity=2)
