"""Regression test for issue #209: every refactoring subtype must have a
dedicated prompt template in `collegue/prompts/templates/tools/`.

Before this fix, `smart_orchestrator`'s refactoring invocations hit the
EnhancedPromptEngine with `tool_name="refactoring_simplify"` (and similar),
which logged ``WARNING:tools.RefactoringTool:Erreur lors de la préparation
du prompt optimisé: Aucun template trouvé pour l'outil refactoring_simplify``
on every call and degraded the generated code quality via the fallback path.

This suite enforces two contracts:
  1. Every refactoring subtype declared by the tool has at least one YAML
     template (directory + loadable file).
  2. The EnhancedPromptEngine can resolve that template end-to-end through
     ``get_optimized_prompt(tool_name=f"refactoring_{subtype}", ...)``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from collegue.prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine
from collegue.tools.refactoring.tool import RefactoringTool


TEMPLATES_DIR = Path("collegue/prompts/templates/tools")


def _supported_subtypes() -> list[str]:
    """Read the canonical list of refactoring subtypes from the tool itself."""
    return RefactoringTool().get_supported_refactoring_types()


@pytest.mark.parametrize("subtype", _supported_subtypes())
def test_refactoring_subtype_has_template_directory(subtype: str):
    """Each declared subtype (rename, extract, simplify, clean, optimize,
    modernize) needs its own template subdirectory with at least one YAML."""
    tool_dir = TEMPLATES_DIR / f"refactoring_{subtype}"
    assert tool_dir.is_dir(), (
        f"Missing template directory: {tool_dir}. "
        f"Create {tool_dir}/default.yaml"
    )
    yamls = list(tool_dir.glob("*.yaml"))
    assert yamls, (
        f"No YAML templates in {tool_dir}. Expected at least "
        f"refactoring_{subtype}/default.yaml"
    )


@pytest.mark.parametrize("subtype", _supported_subtypes())
def test_prompt_engine_resolves_refactoring_subtype(subtype: str):
    """EnhancedPromptEngine.get_optimized_prompt must return a non-empty
    prompt for every refactoring_<subtype>, proving that the category
    `tool/refactoring_<subtype>` is registered and formatable."""
    engine = EnhancedPromptEngine()

    tool_name = f"refactoring_{subtype}"
    context = {
        "code": "def f():\n    return 1\n",
        "language": "python",
        "parameters": "{}",
        "context": "",
    }

    prompt, version = asyncio.new_event_loop().run_until_complete(
        engine.get_optimized_prompt(tool_name=tool_name, context=context)
    )

    assert prompt, f"Empty prompt returned for {tool_name}"
    # The refactoring verb for the subtype should appear somewhere in the
    # prompt, confirming the template is subtype-specific rather than the
    # generic fallback.
    assert subtype in prompt.lower() or subtype[:5] in prompt.lower(), (
        f"Prompt for {tool_name} does not mention the subtype: "
        f"{prompt[:200]!r}"
    )
    assert version, "Version id missing"


def test_all_refactoring_template_yamls_are_valid():
    """Every refactoring_<subtype>/*.yaml file must parse cleanly and expose
    the minimum fields the engine consumes (``name`` and ``template``)."""
    import yaml

    for subtype_dir in TEMPLATES_DIR.glob("refactoring_*"):
        if not subtype_dir.is_dir():
            continue
        for yaml_file in subtype_dir.glob("*.yaml"):
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            assert isinstance(data, dict), f"{yaml_file} is not a mapping"
            assert data.get("name"), f"{yaml_file} missing 'name'"
            assert data.get("template"), f"{yaml_file} missing 'template'"
