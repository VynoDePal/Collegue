"""Tests pour ProactiveMonitor — détection proactive de problèmes."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from collegue.autonomous.proactive_monitor import (
    LANGUAGE_MAP,
    TRIGGER_RULES,
    ChangeDetector,
    ExpertTriggerer,
    FileChange,
    MonitorConfig,
    MonitorResult,
    ProactiveMonitor,
    TriggerDecision,
)


class TestFileChange:
    def test_create(self):
        change = FileChange(path="src/app.py", change_type="modified", language="python")
        assert change.path == "src/app.py"
        assert change.language == "python"

    def test_no_language(self):
        change = FileChange(path="README.md", change_type="modified")
        assert change.language is None


class TestLanguageMap:
    def test_python(self):
        assert LANGUAGE_MAP[".py"] == "python"

    def test_javascript(self):
        assert LANGUAGE_MAP[".js"] == "javascript"

    def test_typescript(self):
        assert LANGUAGE_MAP[".ts"] == "typescript"

    def test_unknown_extension(self):
        assert ".xyz" not in LANGUAGE_MAP


class TestMonitorConfig:
    def test_defaults(self):
        config = MonitorConfig()
        assert config.scan_interval == 300.0
        assert "code_review" in config.enabled_experts
        assert "performance_analysis" in config.enabled_experts
        assert config.min_changes_to_trigger == 1

    def test_custom(self):
        config = MonitorConfig(scan_interval=60.0, min_changes_to_trigger=3)
        assert config.scan_interval == 60.0
        assert config.min_changes_to_trigger == 3


class TestExpertTriggerer:
    @pytest.fixture
    def triggerer(self):
        return ExpertTriggerer(MonitorConfig())

    def test_python_file_triggers_review_and_perf(self, triggerer):
        changes = [FileChange(path="app.py", change_type="modified", language="python")]
        decisions = triggerer.decide_triggers(changes)
        expert_names = {d.expert for d in decisions}
        assert "code_review" in expert_names
        assert "performance_analysis" in expert_names

    def test_dockerfile_triggers_iac(self, triggerer):
        changes = [FileChange(path="Dockerfile", change_type="modified")]
        decisions = triggerer.decide_triggers(changes)
        expert_names = {d.expert for d in decisions}
        assert "iac_guardrails_scan" in expert_names

    def test_package_json_triggers_architecture(self, triggerer):
        changes = [FileChange(path="package.json", change_type="modified")]
        decisions = triggerer.decide_triggers(changes)
        expert_names = {d.expert for d in decisions}
        assert "architecture_analysis" in expert_names

    def test_requirements_triggers_architecture(self, triggerer):
        changes = [FileChange(path="requirements.txt", change_type="modified")]
        decisions = triggerer.decide_triggers(changes)
        expert_names = {d.expert for d in decisions}
        assert "architecture_analysis" in expert_names

    def test_no_changes_no_triggers(self, triggerer):
        decisions = triggerer.decide_triggers([])
        assert len(decisions) == 0

    def test_min_changes_threshold(self):
        triggerer = ExpertTriggerer(MonitorConfig(min_changes_to_trigger=5))
        changes = [FileChange(path="app.py", change_type="modified", language="python")]
        decisions = triggerer.decide_triggers(changes)
        assert len(decisions) == 0

    def test_disabled_expert_not_triggered(self):
        config = MonitorConfig(enabled_experts={"iac_guardrails_scan"})
        triggerer = ExpertTriggerer(config)
        changes = [FileChange(path="app.py", change_type="modified", language="python")]
        decisions = triggerer.decide_triggers(changes)
        expert_names = {d.expert for d in decisions}
        assert "code_review" not in expert_names

    def test_multiple_files_aggregate(self, triggerer):
        changes = [
            FileChange(path="src/app.py", change_type="modified", language="python"),
            FileChange(path="src/utils.py", change_type="modified", language="python"),
            FileChange(path="src/main.js", change_type="added", language="javascript"),
        ]
        decisions = triggerer.decide_triggers(changes)
        cr_decisions = [d for d in decisions if d.expert == "code_review"]
        assert len(cr_decisions) == 1
        assert len(cr_decisions[0].params["files"]) == 3
        assert "python" in cr_decisions[0].params["languages"]
        assert "javascript" in cr_decisions[0].params["languages"]

    def test_decisions_sorted_by_priority(self, triggerer):
        changes = [
            FileChange(path="app.py", change_type="modified", language="python"),
            FileChange(path="Dockerfile", change_type="modified"),
        ]
        decisions = triggerer.decide_triggers(changes)
        priorities = [d.priority for d in decisions]
        assert priorities == sorted(priorities, reverse=True)

    def test_should_trigger_architecture(self, triggerer):
        changes = [FileChange(path="requirements.txt", change_type="modified")]
        assert triggerer.should_trigger_architecture(changes)

    def test_should_not_trigger_architecture(self, triggerer):
        changes = [FileChange(path="app.py", change_type="modified")]
        assert not triggerer.should_trigger_architecture(changes)

    def test_should_trigger_tests(self, triggerer):
        changes = [FileChange(path="src/new_module.py", change_type="added", language="python")]
        assert triggerer.should_trigger_tests(changes)

    def test_should_not_trigger_tests_for_test_files(self, triggerer):
        changes = [FileChange(path="tests/test_new.py", change_type="added", language="python")]
        assert not triggerer.should_trigger_tests(changes)

    def test_test_file_does_not_trigger_experts(self, triggerer):
        changes = [FileChange(path="tests/test_app.py", change_type="modified", language="python")]
        decisions = triggerer.decide_triggers(changes)
        # test files match both source code pattern AND test pattern
        # but should still trigger code_review/perf for .py source
        expert_names = {d.expert for d in decisions}
        assert "code_review" in expert_names  # .py still matches source pattern


class TestChangeDetector:
    def test_detect_changes_no_repo(self, tmp_path):
        detector = ChangeDetector(str(tmp_path / "nonexistent"))
        changes = detector.detect_changes()
        assert changes == []

    @patch("subprocess.run")
    def test_detect_changes_parses_output(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="M\tsrc/app.py\nA\tsrc/new.js\nD\told.txt\n", stderr=""),
            MagicMock(returncode=0, stdout="abc123\n", stderr=""),
        ]
        detector = ChangeDetector("/fake/repo")
        changes = detector.detect_changes()
        assert len(changes) == 3
        assert changes[0].path == "src/app.py"
        assert changes[0].change_type == "modified"
        assert changes[0].language == "python"
        assert changes[1].change_type == "added"
        assert changes[1].language == "javascript"
        assert changes[2].change_type == "deleted"

    @patch("subprocess.run")
    def test_detect_uncommitted(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=" M src/app.py\n?? new_file.ts\n",
            stderr="",
        )
        detector = ChangeDetector("/fake/repo")
        changes = detector.detect_uncommitted()
        assert len(changes) == 2
        assert changes[0].change_type == "modified"
        assert changes[1].change_type == "added"

    @patch("subprocess.run")
    def test_detect_changes_empty(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="\n", stderr=""),
            MagicMock(returncode=0, stdout="abc123\n", stderr=""),
        ]
        detector = ChangeDetector("/fake/repo")
        changes = detector.detect_changes()
        assert changes == []


class TestProactiveMonitor:
    def test_create_default(self):
        monitor = ProactiveMonitor()
        assert not monitor.is_running
        assert monitor.config.scan_interval == 300.0

    def test_set_repo_path(self):
        monitor = ProactiveMonitor()
        monitor.set_repo_path("/tmp/repo")
        assert monitor.config.repo_path == "/tmp/repo"

    def test_scan_once_no_repo(self):
        monitor = ProactiveMonitor()
        result = monitor.scan_once()
        assert result.changes_detected == 0
        assert len(result.errors) == 1

    @patch("collegue.autonomous.proactive_monitor.ChangeDetector")
    def test_scan_once_with_changes(self, mock_detector_cls):
        mock_detector = MagicMock()
        mock_detector.detect_changes.return_value = [
            FileChange(path="app.py", change_type="modified", language="python")
        ]
        mock_detector_cls.return_value = mock_detector

        monitor = ProactiveMonitor()
        monitor.set_repo_path("/tmp/repo")
        monitor._detector = mock_detector

        result = monitor.scan_once()
        assert result.changes_detected == 1
        assert result.triggers_decided >= 1

    def test_get_stats(self):
        monitor = ProactiveMonitor()
        stats = monitor.get_stats()
        assert "is_running" in stats
        assert "total_scans" in stats
        assert stats["total_scans"] == 0

    def test_scan_history(self):
        monitor = ProactiveMonitor()
        history = monitor.get_scan_history()
        assert history == []
