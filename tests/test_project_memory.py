"""Tests pour ProjectMemory — mémoire persistante inter-sessions."""

import json
import tempfile
import time

import pytest

from collegue.core.project_memory import (
    DEFAULT_TTL,
    MemoryEntry,
    ProjectMemory,
    get_project_memory,
    reset_project_memory,
)


@pytest.fixture
def memory(tmp_path):
    """Crée une ProjectMemory avec un répertoire temporaire."""
    return ProjectMemory(memory_dir=str(tmp_path / "memory"))


@pytest.fixture(autouse=True)
def reset_singleton():
    """Réinitialise le singleton entre les tests."""
    reset_project_memory()
    yield
    reset_project_memory()


class TestMemoryEntry:
    def test_create_entry(self):
        entry = MemoryEntry(
            expert="code_review",
            entry_type="issue_found",
            category="security",
            title="SQL injection détectée",
            data={"severity": "critical"},
        )
        assert entry.expert == "code_review"
        assert entry.entry_type == "issue_found"
        assert entry.timestamp > 0

    def test_entry_not_expired_when_no_ttl(self):
        entry = MemoryEntry(
            expert="test",
            entry_type="project_profile",
            category="info",
            title="Test",
            data={},
            ttl_seconds=None,
        )
        assert not entry.is_expired()

    def test_entry_expired_when_ttl_passed(self):
        entry = MemoryEntry(
            expert="test",
            entry_type="issue_found",
            category="test",
            title="Old issue",
            data={},
            timestamp=time.time() - 1000,
            ttl_seconds=100,
        )
        assert entry.is_expired()

    def test_entry_not_expired_when_ttl_not_passed(self):
        entry = MemoryEntry(
            expert="test",
            entry_type="issue_found",
            category="test",
            title="Fresh issue",
            data={},
            ttl_seconds=86400,
        )
        assert not entry.is_expired()

    def test_to_dict(self):
        entry = MemoryEntry(
            expert="code_review",
            entry_type="pattern_learned",
            category="architecture",
            title="Singleton detected",
            data={"file": "app.py"},
            score=0.8,
        )
        d = entry.to_dict()
        assert d["expert"] == "code_review"
        assert d["score"] == 0.8

    def test_from_dict(self):
        d = {
            "expert": "perf",
            "entry_type": "issue_found",
            "category": "cpu",
            "title": "O(n²) loop",
            "data": {},
            "timestamp": 123456.0,
            "score": 0.3,
        }
        entry = MemoryEntry.from_dict(d)
        assert entry.expert == "perf"
        assert entry.timestamp == 123456.0


class TestProjectMemoryStore:
    def test_store_entry(self, memory):
        entry = memory.store(
            expert="code_review",
            entry_type="issue_found",
            category="naming",
            title="Variable name too short",
            data={"line": 5},
        )
        assert entry.expert == "code_review"
        assert len(memory) == 1

    def test_store_multiple_entries(self, memory):
        for i in range(5):
            memory.store(
                expert="code_review",
                entry_type="issue_found",
                category="test",
                title=f"Issue {i}",
                data={},
            )
        assert len(memory) == 5

    def test_store_with_default_ttl(self, memory):
        entry = memory.store(
            expert="test",
            entry_type="pattern_learned",
            category="test",
            title="Test pattern",
            data={},
        )
        assert entry.ttl_seconds == DEFAULT_TTL["pattern_learned"]

    def test_store_with_custom_ttl(self, memory):
        entry = memory.store(
            expert="test",
            entry_type="issue_found",
            category="test",
            title="Custom TTL",
            data={},
            ttl_seconds=3600,
        )
        assert entry.ttl_seconds == 3600


class TestProjectMemoryRecall:
    def test_recall_by_expert(self, memory):
        memory.store(expert="code_review", entry_type="issue_found", category="a", title="CR issue", data={})
        memory.store(expert="perf", entry_type="issue_found", category="b", title="Perf issue", data={})

        results = memory.recall(expert="code_review")
        assert len(results) == 1
        assert results[0].title == "CR issue"

    def test_recall_by_type(self, memory):
        memory.store(expert="test", entry_type="pattern_learned", category="a", title="Pattern", data={})
        memory.store(expert="test", entry_type="issue_found", category="b", title="Issue", data={})

        results = memory.recall(entry_type="pattern_learned")
        assert len(results) == 1
        assert results[0].title == "Pattern"

    def test_recall_by_category(self, memory):
        memory.store(expert="test", entry_type="issue_found", category="security", title="Security 1", data={})
        memory.store(expert="test", entry_type="issue_found", category="naming", title="Naming 1", data={})

        results = memory.recall(category="security")
        assert len(results) == 1

    def test_recall_by_language(self, memory):
        memory.store(expert="test", entry_type="issue_found", category="a", title="Py", data={}, language="python")
        memory.store(expert="test", entry_type="issue_found", category="b", title="JS", data={}, language="javascript")

        results = memory.recall(language="python")
        assert len(results) == 1
        assert results[0].title == "Py"

    def test_recall_with_limit(self, memory):
        for i in range(20):
            memory.store(
                expert="test", entry_type="issue_found", category="a", title=f"Issue {i}", data={}, score=float(i)
            )

        results = memory.recall(limit=5)
        assert len(results) == 5

    def test_recall_sorted_by_score(self, memory):
        memory.store(expert="test", entry_type="issue_found", category="a", title="Low", data={}, score=0.1)
        memory.store(expert="test", entry_type="issue_found", category="a", title="High", data={}, score=0.9)
        memory.store(expert="test", entry_type="issue_found", category="a", title="Mid", data={}, score=0.5)

        results = memory.recall()
        assert results[0].title == "High"
        assert results[1].title == "Mid"
        assert results[2].title == "Low"

    def test_recall_excludes_expired(self, memory):
        memory.store(expert="test", entry_type="issue_found", category="a", title="Old", data={}, ttl_seconds=0.001)
        time.sleep(0.01)
        memory.store(expert="test", entry_type="issue_found", category="a", title="New", data={})

        results = memory.recall()
        assert len(results) == 1
        assert results[0].title == "New"

    def test_recall_all_filters_combined(self, memory):
        memory.store(
            expert="code_review",
            entry_type="issue_found",
            category="security",
            title="Match",
            data={},
            language="python",
        )
        memory.store(
            expert="code_review",
            entry_type="pattern_learned",
            category="security",
            title="Wrong type",
            data={},
            language="python",
        )

        results = memory.recall(expert="code_review", entry_type="issue_found", category="security", language="python")
        assert len(results) == 1
        assert results[0].title == "Match"


class TestProjectMemorySearch:
    def test_search_by_title(self, memory):
        memory.store(expert="test", entry_type="issue_found", category="a", title="SQL injection risk", data={})
        memory.store(expert="test", entry_type="issue_found", category="a", title="Unused import", data={})

        results = memory.search("SQL injection")
        assert len(results) == 1
        assert results[0].title == "SQL injection risk"

    def test_search_by_category(self, memory):
        memory.store(expert="test", entry_type="issue_found", category="security", title="Issue 1", data={})
        memory.store(expert="test", entry_type="issue_found", category="naming", title="Issue 2", data={})

        results = memory.search("security")
        assert len(results) == 1

    def test_search_no_results(self, memory):
        memory.store(expert="test", entry_type="issue_found", category="a", title="Something", data={})
        results = memory.search("nonexistent term xyz")
        assert len(results) == 0


class TestProjectMemoryPersistence:
    def test_save_and_load(self, tmp_path):
        memory_dir = str(tmp_path / "mem")
        memory1 = ProjectMemory(memory_dir=memory_dir)
        memory1.store(expert="test", entry_type="issue_found", category="a", title="Persistent", data={"key": "val"})
        memory1.save()

        # Charger depuis le même répertoire
        memory2 = ProjectMemory(memory_dir=memory_dir)
        assert len(memory2) == 1
        results = memory2.recall()
        assert results[0].title == "Persistent"
        assert results[0].data == {"key": "val"}

    def test_load_empty_dir(self, tmp_path):
        memory = ProjectMemory(memory_dir=str(tmp_path / "empty"))
        assert len(memory) == 0

    def test_load_corrupted_file(self, tmp_path):
        memory_dir = tmp_path / "corrupt"
        memory_dir.mkdir(parents=True)
        (memory_dir / "project_memory.json").write_text("not valid json")

        memory = ProjectMemory(memory_dir=str(memory_dir))
        assert len(memory) == 0


class TestProjectMemoryPrune:
    def test_prune_expired(self, memory):
        memory.store(expert="test", entry_type="issue_found", category="a", title="Old", data={}, ttl_seconds=0.001)
        time.sleep(0.01)
        memory.store(expert="test", entry_type="issue_found", category="a", title="New", data={})

        pruned = memory.prune()
        assert pruned == 1
        assert len(memory) == 1

    def test_prune_over_limit(self, tmp_path):
        memory = ProjectMemory(memory_dir=str(tmp_path / "mem"), max_total=5)
        for i in range(10):
            memory.store(expert="test", entry_type="issue_found", category="a", title=f"Item {i}", data={})

        assert len(memory) <= 5

    def test_per_expert_limit_enforced_during_store(self, tmp_path):
        """Store triggers pruning when a single expert exceeds MAX_ENTRIES_PER_EXPERT."""
        from collegue.core.project_memory import MAX_ENTRIES_PER_EXPERT

        memory = ProjectMemory(memory_dir=str(tmp_path / "mem"))

        for i in range(MAX_ENTRIES_PER_EXPERT + 50):
            memory.store(
                expert="same_expert",
                entry_type="issue_found",
                category="a",
                title=f"Item {i}",
                data={"i": i},
                auto_save=False,
            )

        assert len(memory) <= MAX_ENTRIES_PER_EXPERT


class TestProjectMemoryContext:
    def test_get_context_for_empty(self, memory):
        context = memory.get_context_for("nonexistent_expert")
        assert context == {}

    def test_get_context_for_with_data(self, memory):
        memory.store(expert="code_review", entry_type="pattern_learned", category="a", title="Singleton", data={})
        memory.store(
            expert="code_review",
            entry_type="issue_found",
            category="security",
            title="SQL injection",
            data={"desc": "bad"},
        )
        memory.store(expert="code_review", entry_type="fix_applied", category="b", title="Fixed XSS", data={})
        memory.store(
            expert="code_review",
            entry_type="project_profile",
            category="info",
            title="Profile",
            data={"framework": "Django"},
        )

        context = memory.get_context_for("code_review")
        assert "known_patterns" in context
        assert "Singleton" in context["known_patterns"]
        assert "known_issues" in context
        assert "past_fixes" in context
        assert "project_profile" in context
        assert context["project_profile"]["framework"] == "Django"


class TestProjectMemoryStats:
    def test_export_stats(self, memory):
        memory.store(expert="code_review", entry_type="issue_found", category="a", title="Issue 1", data={})
        memory.store(expert="perf", entry_type="pattern_learned", category="b", title="Pattern 1", data={})

        stats = memory.export_stats()
        assert stats["total_entries"] == 2
        assert stats["active_entries"] == 2
        assert stats["by_expert"]["code_review"] == 1
        assert stats["by_expert"]["perf"] == 1
        assert stats["by_type"]["issue_found"] == 1
        assert stats["by_type"]["pattern_learned"] == 1


class TestProjectMemoryClear:
    def test_clear(self, memory):
        memory.store(expert="test", entry_type="issue_found", category="a", title="Delete me", data={})
        assert len(memory) == 1
        memory.clear()
        assert len(memory) == 0


class TestProjectMemorySingleton:
    def test_singleton(self):
        m1 = get_project_memory()
        m2 = get_project_memory()
        assert m1 is m2

    def test_reset_singleton(self):
        m1 = get_project_memory()
        reset_project_memory()
        m2 = get_project_memory()
        assert m1 is not m2
