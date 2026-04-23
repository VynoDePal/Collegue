"""Regression tests for #233 — test_generation and code_documentation must
route their prompt construction through ``BaseTool.prepare_prompt`` (which
reaches the EnhancedPromptEngine + its templates + A/B bandit) and must
feed telemetry back via ``track_last_prompt_performance``.

Pre-#233, both tools called ``self._engine.build_prompt(...)`` directly,
bypassing the whole template system. The tests here verify :

- The tool reaches ``prepare_prompt`` when a ``prompt_engine`` is present.
- The tool reaches its fallback ``_build_prompt`` when no engine is
  configured (preserves offline test behaviour).
- After ``ctx.sample``, ``track_performance`` is called on the engine
  with a plausible template_id / version pair.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from collegue.tools.code_documentation import DocumentationRequest, DocumentationTool
from collegue.tools.test_generation import TestGenerationRequest, TestGenerationTool


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeVersion:
    id: str
    version: str
    content: str


class _FakeVersionManager:
    def __init__(self, template_id: str, version: _FakeVersion):
        self._template_id = template_id
        self._version = version

    def get_all_versions(self, template_id: str) -> List[_FakeVersion]:
        if template_id == self._template_id:
            return [self._version]
        return []


class _FakeTemplate:
    def __init__(self, tid: str, name: str):
        self.id = tid
        self.name = name


def _make_fake_engine(tool_name: str, rendered_prompt: str):
    """Return a real :class:`EnhancedPromptEngine` subclass instance with
    the three methods that :meth:`BaseTool.prepare_prompt` touches stubbed
    out. Subclassing (vs MagicMock(spec=...)) is what makes
    ``isinstance(engine, EnhancedPromptEngine)`` return True in base.py.
    """
    from collegue.prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine

    class _FakeEngine(EnhancedPromptEngine):
        # Skip the real __init__ so we don't touch the filesystem.
        def __init__(self) -> None:  # noqa: D401 — intentional no-op
            self._tool_name = tool_name
            self._rendered = rendered_prompt
            self._template = _FakeTemplate(
                f"tpl-{tool_name}", f"{tool_name}_default"
            )
            self._version = _FakeVersion(
                id=f"ver-{tool_name}",
                version="1.0.0",
                content=rendered_prompt,
            )
            self.version_manager = _FakeVersionManager(
                self._template.id, self._version
            )
            self.track_performance = MagicMock()

        async def get_optimized_prompt(
            self,
            tool_name: str,
            context: Dict[str, Any],
            language: Optional[str] = None,
            version: Optional[str] = None,
        ):
            return self._rendered, self._version.id

        def get_templates_by_category(self, category: str):
            if self._tool_name in category:
                return [self._template]
            return []

    return _FakeEngine()


# ---------------------------------------------------------------------------
# test_generation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_generation_routes_through_prepare_prompt():
    tool = TestGenerationTool()
    engine = _make_fake_engine("test_generation", "YAML_RENDERED_PROMPT")
    tool.prompt_engine = engine

    ctx = MagicMock()
    ctx.info = AsyncMock()
    ctx.warning = AsyncMock()
    ctx.error = AsyncMock()
    ctx.sample = AsyncMock()
    ctx.report_progress = AsyncMock()
    sample_result = MagicMock()
    sample_result.text = "def test_foo():\n    assert True"
    ctx.sample.return_value = sample_result

    request = TestGenerationRequest(code="def foo(): pass", language="python")
    response = await tool.execute_async(request, ctx=ctx)

    # The prompt sent to ctx.sample must be what the engine rendered, not
    # the hardcoded French prompt.
    ctx.sample.assert_awaited_once()
    sent_prompt = ctx.sample.await_args.kwargs["messages"]
    assert sent_prompt == "YAML_RENDERED_PROMPT"
    # System prompt should NOT be re-injected when the template owns the role.
    assert "system_prompt" not in ctx.sample.await_args.kwargs

    # Telemetry must have been fed back with the right template/version.
    engine.track_performance.assert_called_once()
    call_kwargs = engine.track_performance.call_args.kwargs
    assert call_kwargs["template_id"] == "tpl-test_generation"
    assert call_kwargs["version"] == "1.0.0"
    assert call_kwargs["success"] is True
    assert call_kwargs["execution_time"] >= 0
    assert response.test_code == sample_result.text


def test_test_generation_fallback_when_no_engine():
    """Without a prompt_engine, the tool must delegate to ``_build_prompt``
    (which wraps the engine's hardcoded prompt) instead of crashing."""
    tool = TestGenerationTool()
    assert tool.prompt_engine is None  # default

    request = TestGenerationRequest(code="def foo(): return 1", language="python")
    prompt = tool._build_prompt(request)
    # Hardcoded FR prompt preserved for offline fallback.
    assert "Génère des tests unitaires" in prompt
    assert "def foo()" in prompt


# ---------------------------------------------------------------------------
# code_documentation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_code_documentation_routes_through_prepare_prompt():
    tool = DocumentationTool()
    engine = _make_fake_engine("documentation", "DOC_YAML_PROMPT")
    tool.prompt_engine = engine

    ctx = MagicMock()
    ctx.info = AsyncMock()
    ctx.warning = AsyncMock()
    ctx.error = AsyncMock()
    ctx.sample = AsyncMock()
    ctx.report_progress = AsyncMock()
    sample_result = MagicMock()
    sample_result.text = "# foo\n\nDoes something."
    ctx.sample.return_value = sample_result

    request = DocumentationRequest(
        code="def foo():\n    return 1",
        language="python",
        doc_format="markdown",
    )
    response = await tool.execute_async(request, ctx=ctx)

    ctx.sample.assert_awaited_once()
    sent_prompt = ctx.sample.await_args.kwargs["messages"]
    assert sent_prompt == "DOC_YAML_PROMPT"
    assert "system_prompt" not in ctx.sample.await_args.kwargs

    engine.track_performance.assert_called_once()
    call_kwargs = engine.track_performance.call_args.kwargs
    assert call_kwargs["template_id"] == "tpl-documentation"
    assert call_kwargs["version"] == "1.0.0"
    assert call_kwargs["success"] is True


def test_code_documentation_fallback_when_no_engine():
    tool = DocumentationTool()
    assert tool.prompt_engine is None

    request = DocumentationRequest(
        code="def foo(): return 1",
        language="python",
        doc_format="markdown",
    )
    prompt = tool._build_prompt(request)
    # Fallback must include the code and be non-empty.
    assert "def foo" in prompt
    assert len(prompt) > 50


@pytest.mark.asyncio
async def test_code_documentation_rendered_prompt_has_no_ghost_placeholders():
    """Regression for #244 — the old ``_enrich_context_with_elements`` wrote
    to a ``context`` field that didn't exist on ``DocumentationRequest``,
    and ``default.yaml`` referenced a non-existent ``{format}`` placeholder.
    Both reached the LLM as literal strings. With the real engine + template
    this test locks in that neither placeholder survives rendering.
    """
    from collegue.prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine

    engine = EnhancedPromptEngine()
    tool = DocumentationTool()
    tool.prompt_engine = engine

    ctx = MagicMock()
    ctx.info = AsyncMock()
    ctx.warning = AsyncMock()
    ctx.error = AsyncMock()
    ctx.report_progress = AsyncMock()
    captured = {}

    async def fake_sample(**kwargs):
        captured.update(kwargs)
        result = MagicMock()
        result.text = "# Stub"
        return result

    ctx.sample = fake_sample

    request = DocumentationRequest(
        code="def foo(): return 1",
        language="python",
        doc_format="markdown",
    )
    await tool.execute_async(request, ctx=ctx)

    prompt = captured["messages"]
    # Ghost placeholders that used to leak into the prompt:
    assert "{context}" not in prompt
    assert "{format}" not in prompt
    assert "{audience}" not in prompt
    assert "{depth_level}" not in prompt
    # ``focus_on`` defaults to None — if the template ever re-adds it unguarded
    # we'd see "None" as a literal word in a sentence. Pin against that.
    assert " None " not in prompt
    # Sanity: the real template's opening line is there.
    assert "You generate developer-facing documentation" in prompt


# ---------------------------------------------------------------------------
# BaseTool tracking plumbing
# ---------------------------------------------------------------------------


def test_track_last_prompt_noop_when_ids_missing():
    """The tracking helper must silently no-op when ``prepare_prompt`` fell
    back (no engine configured). Used by tools that route everything through
    ``track_last_prompt_performance`` unconditionally."""
    tool = TestGenerationTool()  # no engine
    tool.prompt_engine = MagicMock()
    tool.prompt_engine.track_performance = MagicMock()
    # Simulate no successful prepare_prompt call ever.
    tool._last_prompt_template_id = None
    tool._last_prompt_version = None

    tool.track_last_prompt_performance(
        execution_time=0.1, tokens_used=42, success=True
    )
    tool.prompt_engine.track_performance.assert_not_called()
