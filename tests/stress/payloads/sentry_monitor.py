"""Stress payloads for sentry_monitor (#206).

Goals (see issue #206):
- Pydantic validation on `command` + whitelist of 10 commands.
- Slug validation in clients/sentry.py (path-traversal / special chars
  rejected on organization, project, issue_id).
- Plausible requests without SENTRY_AUTH_TOKEN → TOOL-ERR, not CRASH-500.
"""
TOOL_NAME = "sentry_monitor"

_long_str = "a" * 10_000
_path_traversal = "../../etc/passwd"
_unicode = "\U0001f4a3" * 500 + "‮" * 50

PAYLOADS = [
    # --- Validation ---
    {"description": "No command", "arguments": {}},
    {"description": "Empty command", "arguments": {"command": ""}},
    {"description": "Non-whitelisted command", "arguments": {"command": "delete_project"}},
    {"description": "Command case mangling", "arguments": {"command": "LIST_PROJECTS"}},
    {"description": "get_issue without issue_id", "arguments": {
        "command": "get_issue", "organization": "my-org"}},
    {"description": "issue_events with issue_id as int", "arguments": {
        "command": "issue_events", "organization": "o", "issue_id": 12345}},

    # --- Adversarial: slug validation (path traversal / special chars) ---
    {"description": "Path traversal in organization", "arguments": {
        "command": "list_projects", "organization": _path_traversal}},
    {"description": "Slash in organization slug", "arguments": {
        "command": "list_projects", "organization": "org/project"}},
    {"description": "Shell metachars in project", "arguments": {
        "command": "project_stats", "organization": "o", "project": "p; rm -rf /"}},
    {"description": "Null byte in issue_id", "arguments": {
        "command": "get_issue", "organization": "o", "issue_id": "abc\x00def"}},
    {"description": "URL-encoded traversal", "arguments": {
        "command": "list_projects", "organization": "%2e%2e%2f%2e%2e%2f"}},
    {"description": "Unicode RTL override in project", "arguments": {
        "command": "project_stats", "organization": "o", "project": "a‮evil"}},

    # --- Boundaries ---
    {"description": "Extremely long organization", "arguments": {
        "command": "list_projects", "organization": _long_str}},
    {"description": "Unicode-bomb issue_id", "arguments": {
        "command": "get_issue", "organization": "o", "issue_id": _unicode}},

    # --- Plausible (no token → TOOL-ERR expected) ---
    {"description": "list_projects plausible org", "arguments": {
        "command": "list_projects", "organization": "sentry"}},
    {"description": "list_issues plausible query", "arguments": {
        "command": "list_issues", "organization": "sentry", "project": "sentry", "query": "is:unresolved"}},
]
