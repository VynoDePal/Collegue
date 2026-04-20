"""Module with unused imports and dead code for repo_consistency_check testing."""
import os  # unused
import json  # unused
from collections import OrderedDict  # unused

ACTIVE_COUNT = 0


def used_helper(x: int) -> int:
    return x * 2


def _dead_internal(x: int) -> int:
    # Never called anywhere in the module.
    return x + 999


def main() -> int:
    return used_helper(21)
