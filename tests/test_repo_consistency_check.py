"""
Tests unitaires pour l'outil Repo Consistency Check refactorisé.
"""
import pytest
from unittest.mock import MagicMock, patch

from collegue.tools.repo_consistency_check import (
    RepoConsistencyCheckTool,
    ConsistencyCheckRequest,
    ConsistencyCheckResponse
)
from collegue.tools.repo_consistency_check.engine import ConsistencyAnalysisEngine
from collegue.tools.repo_consistency_check.models import ConsistencyFile
from collegue.core.shared import FileInput, ConsistencyIssue


class TestConsistencyAnalysisEngine:
    """Tests pour le moteur d'analyse."""

    @pytest.fixture
    def engine(self):
        return ConsistencyAnalysisEngine(logger=None)

    def test_normalize_line(self, engine):
        """Test la normalisation des lignes."""
        assert engine.normalize_line("code // comment") == "code "
        assert engine.normalize_line("code # comment") == "code "
        assert engine.normalize_line("  code  ") == "code"

    def test_get_code_blocks(self, engine):
        """Test l'extraction des blocs de code."""
        content = "line1\nline2\nline3\nline4\nline5"
        blocks = engine.get_code_blocks(content, block_size=3)
        assert len(blocks) > 0
        # Vérifier qu'un bloc contient les bonnes lignes
        for block_hash, (line_num, code) in blocks.items():
            assert line_num > 0
            assert len(code) > 0

    def test_analyze_duplication(self, engine):
        """Test la détection de duplication."""
        files = [
            FileInput(path="file1.py", content="def foo():\n    return 1\n    \ndef bar():\n    return 2"),
            FileInput(path="file2.py", content="def baz():\n    return 1\n    \ndef qux():\n    return 2")
        ]
        issues = engine.analyze_duplication(files, min_lines=2)
        # Devrait détecter la duplication de "return 1" et "return 2"
        assert len(issues) > 0

    def test_extract_defined_symbols_python(self, engine):
        """Test l'extraction des symboles Python."""
        file = FileInput(
            path="test.py",
            content="def my_func():\n    pass\n\nclass MyClass:\n    pass\n\nMY_VAR = 1"
        )
        symbols = engine.extract_defined_symbols(file)
        assert 'my_func' in symbols
        assert 'MyClass' in symbols
        assert 'MY_VAR' in symbols

    def test_calculate_refactoring_score_no_issues(self, engine):
        """Test le calcul du score sans issues."""
        score, priority = engine.calculate_refactoring_score([])
        assert score == 0.0
        assert priority == "none"

    def test_calculate_refactoring_score_with_issues(self, engine):
        """Test le calcul du score avec issues."""
        issues = [
            ConsistencyIssue(kind="unused_var", severity="medium", path="f.py", line=1,
                           message="unused", confidence=80, suggested_fix="remove", engine="test"),
            ConsistencyIssue(kind="dead_code", severity="high", path="f.py", line=2,
                           message="dead", confidence=90, suggested_fix="remove", engine="test"),
            ConsistencyIssue(kind="unused_import", severity="medium", path="f.py", line=3,
                           message="import", confidence=85, suggested_fix="remove", engine="test"),
            ConsistencyIssue(kind="dead_code", severity="high", path="f.py", line=4,
                           message="dead2", confidence=90, suggested_fix="remove", engine="test"),
            ConsistencyIssue(kind="duplication", severity="low", path="f.py", line=5,
                           message="dup", confidence=70, suggested_fix="extract", engine="test"),
        ]
        score, priority = engine.calculate_refactoring_score(issues)
        assert score > 0.0
        assert priority in ["suggested", "recommended", "critical"]

    def test_build_analysis_summary_no_issues(self, engine):
        """Test le résumé sans issues."""
        summary = engine.build_analysis_summary([], 3, {'high': 0, 'medium': 0, 'low': 0, 'info': 0})
        assert "Aucune incohérence" in summary
        assert "3 fichier(s)" in summary

    def test_build_analysis_summary_with_issues(self, engine):
        """Test le résumé avec issues."""
        issues = [MagicMock()]
        severity_counts = {'high': 1, 'medium': 2, 'low': 3, 'info': 0}
        summary = engine.build_analysis_summary(issues, 2, severity_counts)
        assert "incohérence(s)" in summary
        assert "Haute(1)" in summary


class TestRepoConsistencyCheckTool:
    """Tests pour le Tool principal."""

    @pytest.fixture
    def tool(self):
        return RepoConsistencyCheckTool(app_state={})

    def test_tool_metadata(self, tool):
        """Test les métadonnées du tool."""
        assert tool.tool_name == "repo_consistency_check"
        assert "analysis" in tool.tags
        assert "python" in tool.supported_languages

    def test_scan_unused_imports_python(self, tool):
        """Test la détection d'imports non utilisés en Python."""
        files = [
            ConsistencyFile(path="test.py", content="import os\nimport sys\nprint('hello')")
        ]
        request = ConsistencyCheckRequest(
            files=files,
            language="python",
            checks=["unused_imports"]
        )
        response = tool._execute_core_logic(request)
        
        # os et sys sont importés mais pas utilisés
        assert response.files_analyzed == 1
        assert len(response.issues) >= 1
        assert any("unused" in i.kind.lower() or "import" in i.kind.lower() 
                  for i in response.issues)

    def test_scan_no_issues(self, tool):
        """Test le scan sans problèmes."""
        files = [
            ConsistencyFile(path="test.py", content="print('hello')")
        ]
        request = ConsistencyCheckRequest(
            files=files,
            language="python",
            checks=["unused_imports"]
        )
        response = tool._execute_core_logic(request)
        
        assert response.valid is True
        assert response.summary['total'] == 0

    def test_scan_multiple_files(self, tool):
        """Test le scan de plusieurs fichiers."""
        files = [
            ConsistencyFile(path="file1.py", content="x = 1"),
            ConsistencyFile(path="file2.py", content="y = 2")
        ]
        request = ConsistencyCheckRequest(
            files=files,
            language="python"
        )
        response = tool._execute_core_logic(request)
        
        assert response.files_analyzed == 2

    def test_mode_validation(self, tool):
        """Test la validation du mode."""
        files = [ConsistencyFile(path="test.py", content="pass")]
        request = ConsistencyCheckRequest(files=files, language="python", mode="deep")
        assert request.mode == "deep"
        
        with pytest.raises(ValueError):
            ConsistencyCheckRequest(files=files, language="python", mode="invalid")


class TestConsistencyCheckRequest:
    """Tests pour le modèle ConsistencyCheckRequest."""

    def test_request_validation_checks(self):
        """Test la validation des checks."""
        files = [ConsistencyFile(path="test.py", content="pass")]
        request = ConsistencyCheckRequest(
            files=files,
            language="python",
            checks=["unused_imports", "dead_code"]
        )
        assert "unused_imports" in request.checks
        
        with pytest.raises(ValueError):
            ConsistencyCheckRequest(
                files=files,
                language="python",
                checks=["invalid_check"]
            )

    def test_request_language_auto(self):
        """Test la détection auto du langage."""
        files = [ConsistencyFile(path="test.py", content="pass")]
        request = ConsistencyCheckRequest(files=files, language="auto")
        assert request.language == "auto"
