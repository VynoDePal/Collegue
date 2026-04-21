"""pytest configuration for the Collegue test suite.

The ``_KNOWN_FAILURES`` set was introduced to unblock CI (#212) while the
32 pre-existing test failures exposed by the new pipeline were triaged in
#218. All of them have now been fixed, so the set is empty.

The mechanism stays in place as a pattern: if a future regression temporarily
breaks a test that cannot be fixed immediately, add its nodeid here with a
follow-up issue reference, then remove it once the fix lands. Keep the set
as close to empty as possible — every entry is a silenced red flag.
"""
from __future__ import annotations

import pytest

_KNOWN_FAILURES: frozenset[str] = frozenset()

_SKIP_REASON = "pre-existing failure — document in a tracking issue before adding"


def pytest_collection_modifyitems(config, items):
    if not _KNOWN_FAILURES:
        return
    for item in items:
        if item.nodeid in _KNOWN_FAILURES:
            item.add_marker(pytest.mark.skip(reason=_SKIP_REASON))
