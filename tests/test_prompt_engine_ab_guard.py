"""Regression tests for #232 — the ε-greedy bandit in EnhancedPromptEngine
must *not* introduce randomness when every version attached to a template
has identical content.

Before #232, ``_select_version_ab_testing`` happily picked one of 132
UUID-distinct-but-content-identical clones per exploration tick. No signal
could ever be learned because every outcome attached to a different UUID
despite being produced by the same prompt. The fix keys the bandit gate
on ``len({v.content for v in versions})`` instead of ``len(versions)``.
"""
from __future__ import annotations

import random
from pathlib import Path

import pytest
import yaml

from collegue.prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine


def _write_yaml_seed(tools_root: Path, tool: str, name: str, template_body: str) -> Path:
    tool_dir = tools_root / tool
    tool_dir.mkdir(parents=True, exist_ok=True)
    yaml_file = tool_dir / f"{name}.yaml"
    yaml_file.write_text(
        yaml.safe_dump(
            {
                "name": f"{tool}_{name}",
                "description": f"test seed for {tool}/{name}",
                "template": template_body,
                "variables": [
                    {"name": "code", "description": "x", "type": "string", "required": True},
                ],
            }
        ),
        encoding="utf-8",
    )
    return yaml_file


@pytest.fixture
def isolated_engine(tmp_path):
    """Build an EnhancedPromptEngine in a clean tmp_path."""
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "templates").mkdir()
    yaml_root = tmp_path / "yaml_seeds"

    def _factory() -> EnhancedPromptEngine:
        if not yaml_root.exists():
            yaml_root.mkdir()
        return EnhancedPromptEngine(
            templates_dir=str(yaml_root),
            storage_dir=str(storage),
        )

    return {"factory": _factory, "yaml_root": yaml_root}


def _add_clone_versions(engine: EnhancedPromptEngine, template_id: str, content: str, n: int) -> None:
    """Append N versions with identical content to simulate the pre-#231 state."""
    for _ in range(n):
        engine.version_manager.create_version(
            template_id=template_id,
            content=content,
            variables=[],
            version="1.0.0",
        )


def test_ab_testing_skips_when_all_clones(isolated_engine):
    """10 versions with identical content → ``_select_version_ab_testing``
    must return the same PromptVersion on every call. No randomness over
    clones — otherwise the bandit wastes CPU on coin flips that cannot
    change the outcome."""
    _write_yaml_seed(isolated_engine["yaml_root"], "toolA", "default", "Hello {code}")
    engine = isolated_engine["factory"]()
    template_id = next(iter(engine.library.templates.keys()))

    # The YAML loader already added 1 version; pad with 9 identical clones.
    _add_clone_versions(engine, template_id, "Hello {code}", n=9)
    assert len(engine.version_manager.get_all_versions(template_id)) >= 10

    # Force deterministic randomness to make the assertion crisp.
    random.seed(42)
    chosen_ids = {
        engine._select_version_ab_testing(template_id).id
        for _ in range(100)
    }
    assert len(chosen_ids) == 1, (
        f"Expected the bandit to return the SAME version on all 100 calls "
        f"when versions are content-identical clones; got {len(chosen_ids)} distinct picks"
    )


def test_ab_testing_active_when_variants_differ(isolated_engine):
    """2 versions with distinct content → over 200 calls with a moderate
    exploration_rate the bandit must visit **both** versions at least once.
    Proves the guard doesn't inadvertently freeze the policy when real
    variants exist."""
    _write_yaml_seed(isolated_engine["yaml_root"], "toolA", "default", "Variant A: analyse {code}")
    engine = isolated_engine["factory"]()
    engine.exploration_rate = 0.5  # force the explore branch half the time

    template_id = next(iter(engine.library.templates.keys()))
    # Append a SECOND version with truly different content.
    engine.version_manager.create_version(
        template_id=template_id,
        content="Variant B: rewrite {code}",
        variables=[],
        version="2.0.0",
    )
    assert {v.content for v in engine.version_manager.get_all_versions(template_id)} == {
        "Variant A: analyse {code}",
        "Variant B: rewrite {code}",
    }

    random.seed(0)
    contents_seen = set()
    for _ in range(200):
        v = engine._select_version_ab_testing(template_id)
        contents_seen.add(v.content)

    assert contents_seen == {
        "Variant A: analyse {code}",
        "Variant B: rewrite {code}",
    }, (
        f"Expected the bandit to visit BOTH variants over 200 calls with "
        f"exploration_rate=0.5; only saw {contents_seen}"
    )


def test_guard_falls_back_to_best_version_when_clones(isolated_engine):
    """When all versions are clones, the guard should return
    ``get_best_version()`` if available, not random.choice. Makes the
    behaviour stable even if ab_testing_enabled flips between runs."""
    _write_yaml_seed(isolated_engine["yaml_root"], "toolA", "default", "Hello {code}")
    engine = isolated_engine["factory"]()
    template_id = next(iter(engine.library.templates.keys()))
    _add_clone_versions(engine, template_id, "Hello {code}", n=4)

    # Toggle ab_testing_enabled off and on; the result must be identical.
    engine.ab_testing_enabled = True
    pick_with_ab = engine._select_version_ab_testing(template_id)
    engine.ab_testing_enabled = False
    pick_without_ab = engine._select_version_ab_testing(template_id)

    assert pick_with_ab.content == pick_without_ab.content, (
        "When versions are clones, the returned content must be stable "
        "regardless of ab_testing_enabled"
    )


def test_startup_log_reports_zero_active_when_all_clones(isolated_engine, caplog):
    """The INFO log at startup must correctly count templates by variant
    status — 0 active, N single-variant when no variants exist."""
    _write_yaml_seed(isolated_engine["yaml_root"], "toolA", "default", "Hello {code}")
    _write_yaml_seed(isolated_engine["yaml_root"], "toolB", "default", "World {code}")

    import logging
    with caplog.at_level(logging.INFO):
        isolated_engine["factory"]()

    status_logs = [r for r in caplog.records if "A/B testing status" in r.message]
    assert status_logs, "Expected one INFO log announcing A/B readiness"
    msg = status_logs[0].message
    assert "0 template(s) with ≥ 2 real variants" in msg, msg
    assert "2 with a single variant" in msg, msg
