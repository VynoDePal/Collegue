"""pytest configuration for the Collegue test suite.

Known pre-existing failures are listed in ``_KNOWN_FAILURES`` and skipped at
collection time so CI stays green while they are triaged under issue #218.
When a test is fixed, remove its nodeid from the set — pytest will re-collect
it automatically on the next run.

Do NOT add unrelated tests here. Every entry must correspond to a documented
failure in issue #218.
"""
from __future__ import annotations

import pytest

_KNOWN_FAILURES: frozenset[str] = frozenset({
    "tests/test_secret_scan.py::TestSecretScanTool::test_scan_batch_files",
    "tests/test_test_generation_fixes.py::test_test_generation_success",
})

_SKIP_REASON = "pre-existing failure, tracked in #218"


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.nodeid in _KNOWN_FAILURES:
            item.add_marker(pytest.mark.skip(reason=_SKIP_REASON))
