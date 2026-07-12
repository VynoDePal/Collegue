from __future__ import annotations

import hashlib

import pytest

from scripts.patch_openhands_gemma4_terminal import PatchError, patch_source

_SOURCE = b"""from pydantic import Field

class TerminalAction(Action):
    command: str = Field(description="command")
    reset: bool = Field(default=False)

    @property
    def visualize(self) -> Text:
        return Text(self.command)


class TerminalObservation(Observation):
    pass
"""


def _patched_action_type():
    patched = patch_source(
        _SOURCE,
        expected_sha256=hashlib.sha256(_SOURCE).hexdigest(),
    ).decode("utf-8")
    namespace: dict[str, object] = {}
    exec(
        "from pydantic import BaseModel, ConfigDict\n"
        "class Action(BaseModel):\n    model_config = ConfigDict(extra='forbid')\n"
        "class Observation(BaseModel):\n    pass\n"
        "class Text(str):\n    pass\n" + patched,
        namespace,
    )
    return namespace["TerminalAction"]


def test_plural_commands_alias_is_accepted():
    action_type = _patched_action_type()
    assert action_type.model_validate({"commands": "git status"}).command == "git status"


def test_canonical_command_is_unchanged():
    action_type = _patched_action_type()
    assert action_type.model_validate({"command": "echo hi"}).command == "echo hi"


def test_canonical_command_takes_precedence_over_plural_alias():
    action_type = _patched_action_type()
    action = action_type.model_validate({"command": "echo canonical", "commands": "echo ignored"})
    assert action.command == "echo canonical"


def test_unknown_preimage_is_rejected():
    expected = hashlib.sha256(_SOURCE).hexdigest()
    with pytest.raises(PatchError, match="unsupported .* preimage"):
        patch_source(_SOURCE + b"# upstream changed\n", expected_sha256=expected)
