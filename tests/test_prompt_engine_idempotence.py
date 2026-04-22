"""Regression tests for #231 — EnhancedPromptEngine must load YAML seeds
idempotently across repeated instantiations.

Before the fix, each call to ``EnhancedPromptEngine()`` rewrote fresh
UUID-keyed JSON copies of the same YAML templates to disk. The audit
found 2112 JSONs (132× × 16 names) accumulated across server restarts.
These tests guard against that regression by spinning the engine up
several times against a throwaway storage dir and asserting the on-disk
count stays equal to the number of YAML seeds.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from collegue.prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine


def _write_yaml_seed(tools_root: Path, tool: str, name: str, template: str) -> Path:
    tool_dir = tools_root / tool
    tool_dir.mkdir(parents=True, exist_ok=True)
    yaml_file = tool_dir / f"{name}.yaml"
    yaml_file.write_text(
        yaml.safe_dump(
            {
                "name": f"{tool}_{name}",
                "description": f"test seed for {tool}/{name}",
                "template": template,
                "variables": [
                    {"name": "code", "description": "x", "type": "string", "required": True},
                ],
                "tags": [tool],
            }
        ),
        encoding="utf-8",
    )
    return yaml_file


def _json_count(storage_dir: Path) -> int:
    return len(list((storage_dir / "templates").glob("*.json")))


@pytest.fixture
def isolated_engine(tmp_path, monkeypatch):
    """Build a throwaway EnhancedPromptEngine rooted in tmp_path.

    We patch the YAML seed directory so the engine only sees what the
    test put there — avoids contamination from the repo's real templates.
    """
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "templates").mkdir()

    yaml_root = tmp_path / "yaml_seeds"

    def _factory(seeds_present: bool = True) -> EnhancedPromptEngine:
        if seeds_present and not yaml_root.exists():
            yaml_root.mkdir(parents=True)
        engine = EnhancedPromptEngine(
            templates_dir=str(yaml_root),
            storage_dir=str(storage),
        )
        return engine

    return {
        "factory": _factory,
        "storage": storage,
        "yaml_root": yaml_root,
    }


def test_repeated_init_does_not_duplicate_templates(isolated_engine):
    """Three consecutive EnhancedPromptEngine() calls must end with exactly
    one JSON per YAML seed on disk, not 3× per seed."""
    factory = isolated_engine["factory"]
    storage = isolated_engine["storage"]
    yaml_root = isolated_engine["yaml_root"]

    _write_yaml_seed(yaml_root, "toolA", "default", "Hello {code}")
    _write_yaml_seed(yaml_root, "toolB", "default", "Explain {code}")

    for _ in range(3):
        factory()

    assert _json_count(storage) == 2, (
        f"Expected exactly 2 JSONs on disk (one per YAML seed) after 3 inits; "
        f"got {_json_count(storage)}"
    )


def test_yaml_content_change_updates_existing_template_in_place(isolated_engine):
    """When a YAML's content is edited between runs, the engine must update
    the existing template (same UUID) rather than create a new one."""
    factory = isolated_engine["factory"]
    storage = isolated_engine["storage"]
    yaml_root = isolated_engine["yaml_root"]

    seed = _write_yaml_seed(yaml_root, "toolA", "default", "Version 1 prompt for {code}")
    engine_v1 = factory()
    before = list(engine_v1.library.templates.values())
    assert len(before) == 1
    original_id = before[0].id
    assert "Version 1" in before[0].template

    # Edit the YAML on disk.
    data = yaml.safe_load(seed.read_text())
    data["template"] = "Version 2 rewritten prompt for {code}"
    seed.write_text(yaml.safe_dump(data), encoding="utf-8")

    engine_v2 = factory()
    after = list(engine_v2.library.templates.values())
    # Still one template, same UUID, updated content.
    assert len(after) == 1, f"Expected 1 template, got {len(after)}"
    assert after[0].id == original_id, (
        "UUID must be preserved on content-drift update — otherwise any "
        "accumulated performance_score would be lost on every restart"
    )
    assert "Version 2" in after[0].template
    assert _json_count(storage) == 1


def test_versions_not_duplicated_on_reload(isolated_engine):
    """versions.json must not grow on repeated inits of the same YAML."""
    factory = isolated_engine["factory"]
    yaml_root = isolated_engine["yaml_root"]

    _write_yaml_seed(yaml_root, "toolA", "default", "Hello {code}")

    for _ in range(4):
        engine = factory()

    # The single template should have exactly one version (the initial one).
    template_ids = list(engine.library.templates.keys())
    assert len(template_ids) == 1
    versions = engine.version_manager.get_all_versions(template_ids[0])
    assert len(versions) == 1, (
        f"Expected 1 PromptVersion, got {len(versions)} — the version manager "
        f"is not idempotent across reloads"
    )


def test_missing_name_is_skipped_not_fatal(isolated_engine, caplog):
    """A malformed YAML (no `name:` field) must be skipped with a warning,
    not crash the engine startup."""
    factory = isolated_engine["factory"]
    yaml_root = isolated_engine["yaml_root"]
    yaml_root.mkdir(parents=True, exist_ok=True)
    broken = yaml_root / "tool_broken" / "nameless.yaml"
    broken.parent.mkdir()
    broken.write_text(
        yaml.safe_dump({"description": "I forgot my name", "template": "hi"}),
        encoding="utf-8",
    )
    _write_yaml_seed(yaml_root, "toolA", "default", "Valid {code}")

    import logging
    with caplog.at_level(logging.WARNING):
        engine = factory()

    # Only the well-formed YAML produced a template.
    assert len(engine.library.templates) == 1
    assert any("no 'name' key" in rec.message for rec in caplog.records)
