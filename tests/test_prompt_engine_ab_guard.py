"""Behaviour tests for ``EnhancedPromptEngine._select_version``.

Originally written under #232 to guard the ε-greedy bandit against faking
randomness over identical clones. Under #240 the bandit was removed and
selection became fully deterministic — these tests now pin the simpler
invariant: selection is stable across calls and across theoretical
`ab_testing_enabled` toggles (which no longer exist, but historical
fixture setups might still touch the attribute).
"""
from __future__ import annotations

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


def test_selection_is_deterministic_across_calls(isolated_engine):
    """N calls to ``_select_version`` return the same PromptVersion.

    Under #240 the bandit was removed, so this invariant is trivial
    for clones AND for real variants — there's no exploration. Pin
    both to prove the method never returns anything else under
    unchanging state.
    """
    _write_yaml_seed(isolated_engine["yaml_root"], "toolA", "default", "Hello {code}")
    engine = isolated_engine["factory"]()
    template_id = next(iter(engine.library.templates.keys()))

    # Even if someone authored 9 more clones, selection must stay stable.
    _add_clone_versions(engine, template_id, "Hello {code}", n=9)
    assert len(engine.version_manager.get_all_versions(template_id)) >= 10

    chosen_ids = {
        engine._select_version(template_id).id
        for _ in range(100)
    }
    assert len(chosen_ids) == 1, (
        f"Expected selection to be deterministic; got {len(chosen_ids)} distinct picks"
    )


def test_selection_deterministic_even_with_distinct_variants(isolated_engine):
    """Two genuinely-different versions → selection still stable.

    This locks the #240 decision: without a learning signal, we don't
    explore. A future feature that re-adds exploration should delete
    this test along with its rationale.
    """
    _write_yaml_seed(isolated_engine["yaml_root"], "toolA", "default", "Variant A: analyse {code}")
    engine = isolated_engine["factory"]()

    template_id = next(iter(engine.library.templates.keys()))
    engine.version_manager.create_version(
        template_id=template_id,
        content="Variant B: rewrite {code}",
        variables=[],
        version="2.0.0",
    )

    contents_seen = {
        engine._select_version(template_id).content
        for _ in range(50)
    }
    assert len(contents_seen) == 1, (
        f"Expected exactly one variant to be returned across 50 calls "
        f"(bandit removed under #240); saw {contents_seen}"
    )


def test_startup_log_reports_templates_count(isolated_engine, caplog):
    """The startup INFO log must emit a ``templates loaded`` line with the count."""
    _write_yaml_seed(isolated_engine["yaml_root"], "toolA", "default", "Hello {code}")
    _write_yaml_seed(isolated_engine["yaml_root"], "toolB", "default", "World {code}")

    import logging
    with caplog.at_level(logging.INFO):
        isolated_engine["factory"]()

    loaded_logs = [r for r in caplog.records if "Prompt templates loaded" in r.message]
    assert loaded_logs, "Expected one INFO log announcing template count"
    msg = loaded_logs[0].message
    assert "2" in msg, msg
    assert "deterministic" in msg.lower(), msg
