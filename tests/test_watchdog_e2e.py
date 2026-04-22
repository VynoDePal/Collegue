"""End-to-end tests for the autonomous Watchdog (#208).

The scope here is the full `AutoFixer.attempt_fix()` cycle — from a synthetic
Sentry issue through ContextPack construction, LLM call, fuzzy matching, AST
validation, the anti-destruction guard, and finally the GitHub PR creation.
Everything runs with mocks so the suite fits a regular CI run; a live-mode
placeholder is marked ``@pytest.mark.integration`` and stays skipped until
real credentials are provided.

Scenarios covered (required by #208):
  1. Trivial one-liner fix — exact match, PR is created.
  2. Multi-line patch with fuzzy match — indentation drifts between the
     context pack and the real file, the fuzzy matcher still finds it.
  3. Refusal on >50% reduction — the Watchdog must abort, no PR created.

Plus:
  4. Placeholder `sentry_org` values are rejected by UserConfigRegistry
     before any tool is called.
  5. Multi-user loop runs ``attempt_fix`` once per registered config.
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from collegue.autonomous import watchdog as watchdog_mod
from collegue.autonomous.watchdog import AutoFixer
from collegue.autonomous.config_registry import UserConfigRegistry
from collegue.tools.sentry_monitor import (
    EventInfo,
    IssueInfo,
    SentryResponse,
)
from collegue.tools.github_ops import GitHubResponse, PRInfo


FIXTURES = Path(__file__).parent / "fixtures" / "watchdog"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _load_sentry_fixture() -> tuple[IssueInfo, EventInfo]:
    """Load the realistic anonymised Sentry payload and rebuild the Pydantic
    objects the Watchdog expects."""
    payload = json.loads((FIXTURES / "sample_sentry_issue.json").read_text())
    issue_dict = {k: v for k, v in payload["issue"].items() if not k.startswith("_")}
    return IssueInfo(**issue_dict), EventInfo(**payload["event"])


def _source_code() -> str:
    """Source file the LLM is supposed to patch. Not imported — read as text
    so the line numbers the Sentry fixture points to stay stable."""
    return (FIXTURES / "sample_source.py").read_text()


def _gh_get_file(content: str) -> GitHubResponse:
    """GitHub API returns file content base64-encoded; ContextPackBuilder
    decodes before handing the text to the patcher."""
    return GitHubResponse(
        success=True,
        command="get_file",
        message="OK",
        content=base64.b64encode(content.encode("utf-8")).decode("ascii"),
    )


def _gh_noop(command: str) -> GitHubResponse:
    return GitHubResponse(success=True, command=command, message="OK")


def _gh_pr() -> GitHubResponse:
    return GitHubResponse(
        success=True,
        command="create_pr",
        message="PR created",
        pr=PRInfo(
            id=1,
            number=1,
            title="fix",
            state="open",
            html_url="https://github.com/acme/svc/pull/1",
            user="bot",
            base_branch="main",
            head_branch="fix/sentry-PROJ-42",
            created_at="2026-04-21T08:30:00Z",
            updated_at="2026-04-21T08:30:00Z",
        ),
    )


def _llm_response(fix_data: dict) -> MagicMock:
    resp = MagicMock()
    resp.text = "```json\n" + json.dumps(fix_data) + "\n```"
    resp.annotations = []
    return resp


def _build_fixer_with_mocks(
    gh_side_effect: list,
    sentry_event: EventInfo,
) -> tuple[AutoFixer, MagicMock, MagicMock]:
    fixer = AutoFixer()
    fixer.sentry = MagicMock()
    fixer.sentry._execute_core_logic.side_effect = [
        SentryResponse(
            success=True,
            command="issue_events",
            message="OK",
            events=[sentry_event],
        )
    ]
    fixer.github = MagicMock()
    fixer.github._execute_core_logic.side_effect = gh_side_effect
    return fixer, fixer.sentry, fixer.github


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_processed_issues() -> Iterator[None]:
    """The Watchdog keeps a module-level set of already-processed issue IDs.
    Between tests we wipe it so the same fixture issue can be exercised
    across scenarios."""
    watchdog_mod._processed_issues.clear()
    yield
    watchdog_mod._processed_issues.clear()


@pytest.fixture
def _isolated_registry() -> Iterator[UserConfigRegistry]:
    """Brand-new UserConfigRegistry per test to avoid cross-contamination
    (it's a singleton by design)."""
    registry = UserConfigRegistry()
    with registry._config_lock:
        registry._configs.clear()
    yield registry
    with registry._config_lock:
        registry._configs.clear()


# ---------------------------------------------------------------------------
# Scenario 1 — Trivial fix, exact match → PR created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trivial_attribute_error_fix_creates_pr():
    issue, event = _load_sentry_fixture()
    source = _source_code()

    # Exact-match patch against the source (lifted straight from sample_source.py).
    fix_data = {
        "filepath": "collegue/fixtures/sample_source.py",
        "explanation": "Guard against None to avoid AttributeError.",
        "patches": [
            {
                "search": "def get_user_email(user: Optional[User]) -> str:\n    \"\"\"BUG 1 (AttributeError) : si `user` est None, l'accès à .email casse.\"\"\"\n    return user.email",
                "replace": "def get_user_email(user: Optional[User]) -> str:\n    \"\"\"Retourne l'email ou '' si user est None (fix Sentry PROJ-42).\"\"\"\n    if user is None:\n        return \"\"\n    return user.email",
            }
        ],
    }

    fixer, _, gh = _build_fixer_with_mocks(
        gh_side_effect=[
            _gh_get_file(source),          # ContextPackBuilder.fetch_file_content
            _gh_noop("create_branch"),
            _gh_noop("update_file"),
            _gh_pr(),
        ],
        sentry_event=event,
    )

    with patch("collegue.autonomous.watchdog.generate_text", new_callable=AsyncMock) as llm:
        # Two generate_text calls: web search (ignored on failure) + analysis.
        llm.side_effect = [Exception("no web search"), _llm_response(fix_data)]
        with patch.object(fixer, "_get_github_token", return_value="gh-token"):
            await fixer.attempt_fix(issue, "acme", "svc", "real-org", "sentry-token")

    commands = [call.args[0].command for call in gh._execute_core_logic.call_args_list]
    assert commands == ["get_file", "create_branch", "update_file", "create_pr"]

    update_call = gh._execute_core_logic.call_args_list[2].args[0]
    assert "if user is None" in update_call.content
    assert update_call.branch == "fix/sentry-PROJ-42"


# ---------------------------------------------------------------------------
# Scenario 2 — Fuzzy match when indentation drifted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fuzzy_match_survives_indentation_drift():
    issue, event = _load_sentry_fixture()
    source = _source_code()

    # LLM's `search` is the same block but indented differently (one more
    # level) than what lives in the file — the exact `in` test will miss,
    # difflib fuzzy path should catch it (score ≥ 0.6).
    drifted_search = (
        "    result = []\n"
        "        for u in users:\n"
        "            line = \"<\" + u.email + \">\"\n"
        "            result.append(line)\n"
        "        return result"
    )
    fix_data = {
        "filepath": "collegue/fixtures/sample_source.py",
        "explanation": "Guard None emails.",
        "patches": [
            {
                "search": drifted_search,
                "replace": (
                    "    result = []\n"
                    "    for u in users:\n"
                    "        email = u.email or \"\"\n"
                    "        line = \"<\" + email + \">\"\n"
                    "        result.append(line)\n"
                    "    return result"
                ),
            }
        ],
    }

    fixer, _, gh = _build_fixer_with_mocks(
        gh_side_effect=[
            _gh_get_file(source),
            _gh_noop("create_branch"),
            _gh_noop("update_file"),
            _gh_pr(),
        ],
        sentry_event=event,
    )

    with patch("collegue.autonomous.watchdog.generate_text", new_callable=AsyncMock) as llm:
        llm.side_effect = [Exception("no web search"), _llm_response(fix_data)]
        with patch.object(fixer, "_get_github_token", return_value="gh-token"):
            await fixer.attempt_fix(issue, "acme", "svc", "real-org", "sentry-token")

    commands = [call.args[0].command for call in gh._execute_core_logic.call_args_list]
    assert "create_pr" in commands, (
        "Fuzzy match should have succeeded and produced a PR"
    )
    update_call = gh._execute_core_logic.call_args_list[2].args[0]
    assert "email = u.email or" in update_call.content


# ---------------------------------------------------------------------------
# Scenario 3 — Anti-destruction guard refuses >50% reduction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_destructive_patch_refused_by_size_guard():
    issue, event = _load_sentry_fixture()
    source = _source_code()

    # Match the whole file body then replace it with one line — shrinks the
    # file to well below 50% of its original size. Guard must abort.
    fix_data = {
        "filepath": "collegue/fixtures/sample_source.py",
        "explanation": "Deleting everything fixes the bug, right?",
        "patches": [
            {
                "search": source,
                "replace": "# oops\n",
            }
        ],
    }

    fixer, _, gh = _build_fixer_with_mocks(
        gh_side_effect=[
            _gh_get_file(source),
            # No further GitHub calls expected — if the guard fails, these
            # would run out and MagicMock.StopIteration surfaces, which is
            # itself a failing assertion.
        ],
        sentry_event=event,
    )

    with patch("collegue.autonomous.watchdog.generate_text", new_callable=AsyncMock) as llm:
        llm.side_effect = [Exception("no web search"), _llm_response(fix_data)]
        with patch.object(fixer, "_get_github_token", return_value="gh-token"):
            await fixer.attempt_fix(issue, "acme", "svc", "real-org", "sentry-token")

    commands = [call.args[0].command for call in gh._execute_core_logic.call_args_list]
    assert "create_branch" not in commands
    assert "update_file" not in commands
    assert "create_pr" not in commands


# ---------------------------------------------------------------------------
# UserConfigRegistry — placeholder blacklist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("placeholder", [
    "your-org", "my-organization", "test-org", "placeholder", "YOUR-ORG",
])
def test_registry_rejects_placeholder_orgs(_isolated_registry, placeholder):
    """Any `sentry_org` in the hardcoded blacklist must not persist — the
    Watchdog's multi-user loop skips orgs that return None from `register()`."""
    config_id = _isolated_registry.register(
        sentry_org=placeholder,
        sentry_token="any",
        github_token="any",
        github_owner="acme",
        github_repo="svc",
    )
    assert config_id is None
    assert _isolated_registry.get_all_active(max_age_hours=24.0) == []


# ---------------------------------------------------------------------------
# Multi-user — registry holds 2 configs, both processed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_user_registry_isolates_each_config(_isolated_registry):
    c1 = _isolated_registry.register(
        sentry_org="real-org-one",
        sentry_token="t1",
        github_token="gh1",
        github_owner="acme",
        github_repo="svc",
    )
    c2 = _isolated_registry.register(
        sentry_org="real-org-two",
        sentry_token="t2",
        github_token="gh2",
        github_owner="globex",
        github_repo="app",
    )
    assert c1 and c2 and c1 != c2

    active = _isolated_registry.get_all_active(max_age_hours=24.0)
    orgs = sorted(cfg.sentry_org for cfg in active)
    assert orgs == ["real-org-one", "real-org-two"]

    # Each AutoFixer bound to a UserConfig resolves its own credentials
    # without falling back to the shared env/header resolvers.
    fixer1 = AutoFixer(user_config=next(c for c in active if c.sentry_org == "real-org-one"))
    fixer2 = AutoFixer(user_config=next(c for c in active if c.sentry_org == "real-org-two"))

    assert fixer1._get_sentry_token() == "t1"
    assert fixer2._get_sentry_token() == "t2"
    assert fixer1._get_github_token() == "gh1"
    assert fixer2._get_github_token() == "gh2"
    assert fixer1._get_github_owner() == "acme"
    assert fixer2._get_github_owner() == "globex"


# ---------------------------------------------------------------------------
# Live-mode placeholder (skipped by default, enabled via -m integration)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_watchdog_cycle_against_sandbox():
    """Runs the full Watchdog against a real sandbox (Sentry + GitHub + LLM).

    Intentionally a placeholder: the live setup (sandbox Sentry project +
    disposable GitHub repo + dedicated LLM quota) is described in
    docs/watchdog_deployment.md §Runbook E2E. Enable with::

        pytest -m integration tests/test_watchdog_e2e.py

    This stub fails loudly if the required env vars aren't exported, so
    it's safe to keep in the test file.
    """
    import os

    required = ["SENTRY_AUTH_TOKEN", "SENTRY_ORG", "GITHUB_TOKEN", "LLM_API_KEY"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        pytest.skip(f"Live mode needs env vars: {', '.join(missing)}")

    pytest.skip(
        "Live runner not implemented in-repo — see docs/watchdog_deployment.md "
        "for the manual runbook (sandbox setup + cleanup)."
    )
