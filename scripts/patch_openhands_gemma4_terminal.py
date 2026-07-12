"""Patch the terminal schema bundled with OpenHands AI 1.7.0 for Gemma 4.

This is deliberately a build-time, fail-closed compatibility patch.  It may only
touch the exact ``openhands-tools==1.19.1`` source pulled by ``openhands-ai==1.7.0``.
When OpenHands changes that source, the image build must fail and this patch must
be reviewed instead of being applied to an unknown preimage.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import os
import sys
import tempfile
from pathlib import Path

SUPPORTED_VERSIONS = {
    "openhands-ai": "1.7.0",
    "openhands-sdk": "1.19.1",
    "openhands-tools": "1.19.1",
}
EXPECTED_SOURCE_SHA256 = "63db877aa4fe6e8306f845604b7bc8dc05ba06af00fc8196b4054fb7f13223d1"
TARGET_RELATIVE_PATH = Path("openhands/tools/terminal/definition.py")

_IMPORT_PREIMAGE = "from pydantic import Field\n"
_IMPORT_POSTIMAGE = "from pydantic import Field, model_validator\n"
_ACTION_START = "class TerminalAction(Action):\n"
_ACTION_END = "\n\nclass TerminalObservation(Observation):\n"
_VALIDATOR_ANCHOR = "    @property\n    def visualize(self) -> Text:\n"
_VALIDATOR_BLOCK = """    @model_validator(mode="before")
    @classmethod
    def _normalize_gemma_terminal_arguments(cls, data: object) -> object:
        # The schema remains canonical. Only normalize Gemma 4's known plural
        # argument. If both keys exist, discard the plural alias so strict
        # extra-field validation still succeeds and the canonical value wins.
        if not isinstance(data, dict) or "commands" not in data:
            return data
        normalized = dict(data)
        normalized.setdefault("command", normalized["commands"])
        normalized.pop("commands", None)
        return normalized

"""


class PatchError(RuntimeError):
    """Raised when a compatibility patch invariant is not satisfied."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def patch_source(source: bytes, *, expected_sha256: str = EXPECTED_SOURCE_SHA256) -> bytes:
    """Return the patched source, refusing every unreviewed preimage."""

    actual_sha256 = _sha256(source)
    if actual_sha256 != expected_sha256:
        raise PatchError(
            "unsupported OpenHands terminal definition preimage: "
            f"expected sha256={expected_sha256}, got sha256={actual_sha256}"
        )

    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError("OpenHands terminal definition is not UTF-8") from exc

    if text.count(_IMPORT_PREIMAGE) != 1:
        raise PatchError("expected exactly one Pydantic Field import")
    if _IMPORT_POSTIMAGE in text or _VALIDATOR_BLOCK in text:
        raise PatchError("Gemma 4 terminal compatibility patch is already present")
    if text.count(_ACTION_START) != 1 or text.count(_ACTION_END) != 1:
        raise PatchError("TerminalAction class boundaries are ambiguous")

    before_action, action_and_after = text.split(_ACTION_START, 1)
    action_body, after_action = action_and_after.split(_ACTION_END, 1)
    if action_body.count(_VALIDATOR_ANCHOR) != 1:
        raise PatchError("TerminalAction validator insertion point is ambiguous")

    action_body = action_body.replace(
        _VALIDATOR_ANCHOR,
        _VALIDATOR_BLOCK + _VALIDATOR_ANCHOR,
        1,
    )
    patched = (
        before_action.replace(_IMPORT_PREIMAGE, _IMPORT_POSTIMAGE, 1)
        + _ACTION_START
        + action_body
        + _ACTION_END
        + after_action
    ).encode("utf-8")

    if _sha256(patched) == actual_sha256:
        raise PatchError("compatibility patch produced no source change")
    return patched


def _distribution(name: str) -> importlib.metadata.Distribution:
    try:
        distribution = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise PatchError(f"required distribution is missing: {name}") from exc
    expected_version = SUPPORTED_VERSIONS[name]
    if distribution.version != expected_version:
        raise PatchError(f"unsupported {name} version: expected {expected_version}, got {distribution.version}")
    return distribution


def locate_target() -> Path:
    """Resolve the file owned by the exact pinned OpenHands distributions."""

    _distribution("openhands-ai")
    _distribution("openhands-sdk")
    tools_distribution = _distribution("openhands-tools")
    target = Path(tools_distribution.locate_file(TARGET_RELATIVE_PATH)).resolve()
    if not target.is_file():
        raise PatchError(f"OpenHands terminal definition is missing: {target}")
    return target


def _atomic_write(path: Path, content: bytes) -> None:
    mode = path.stat().st_mode
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def verify_runtime_contract() -> None:
    """Import the patched class and prove all three compatibility semantics."""

    importlib.invalidate_caches()
    module = importlib.import_module("openhands.tools.terminal.definition")
    action_type = module.TerminalAction

    alias = action_type.model_validate({"commands": "git status"})
    canonical = action_type.model_validate({"command": "echo canonical"})
    priority = action_type.model_validate({"command": "echo canonical", "commands": "echo ignored"})
    if alias.command != "git status":
        raise PatchError("postcheck failed: plural commands alias was not remapped")
    if canonical.command != "echo canonical":
        raise PatchError("postcheck failed: canonical command changed")
    if priority.command != "echo canonical":
        raise PatchError("postcheck failed: canonical command lost precedence")


def main() -> int:
    try:
        target = locate_target()
        patched = patch_source(target.read_bytes())
        _atomic_write(target, patched)
        if target.read_bytes() != patched:
            raise PatchError("post-write verification failed")
        verify_runtime_contract()
    except PatchError as exc:
        print(f"OpenHands Gemma 4 compatibility patch refused: {exc}", file=sys.stderr)
        return 1

    print(f"OpenHands Gemma 4 terminal compatibility patch applied and verified ({target})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
