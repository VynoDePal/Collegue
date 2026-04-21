"""Stress payloads for github_ops (#206).

Goals (see issue #206):
- Pydantic validation: missing / malformed fields must trigger VALID-OK.
- Adversarial inputs against the command whitelist in
  collegue/core/validators.py and path validation in clients/github.py.
- Boundary: oversized queries, unicode, huge pagination.
- Plausible requests without GITHUB_TOKEN → tool must return TOOL-ERR
  cleanly, never CRASH-500.
"""
TOOL_NAME = "github_ops"

_long_str = "a" * 10_000
_sql_inj = "owner'; DROP TABLE users; --"
_path_traversal = "../../etc/passwd"
_unicode = "\U0001f511" * 500 + "‮" * 50

PAYLOADS = [
    # --- Validation ---
    {"description": "No command field", "arguments": {}},
    {"description": "Empty command string", "arguments": {"command": ""}},
    {"description": "Non-whitelisted command", "arguments": {"command": "delete_repo"}},
    {"description": "Command case mangling", "arguments": {"command": "LIST_REPOS"}},
    {"description": "Command with shell metacharacters", "arguments": {"command": "list_repos; rm -rf /"}},
    {"description": "get_repo without owner", "arguments": {"command": "get_repo", "repo": "collegue"}},
    {"description": "get_repo with int owner", "arguments": {"command": "get_repo", "owner": 12345, "repo": "x"}},
    {"description": "pr_number as string", "arguments": {
        "command": "get_pr", "owner": "a", "repo": "b", "pr_number": "not-a-number"}},

    # --- Adversarial injection ---
    {"description": "Path traversal in repo", "arguments": {
        "command": "get_repo", "owner": "foo", "repo": _path_traversal}},
    {"description": "SQL injection shape in owner", "arguments": {
        "command": "get_repo", "owner": _sql_inj, "repo": "x"}},
    {"description": "Null bytes in path", "arguments": {
        "command": "get_file", "owner": "a", "repo": "b", "path": "src/\x00/etc/passwd"}},
    {"description": "Newline injection in query", "arguments": {
        "command": "search_code", "query": "term\r\nAuthorization: Bearer leaked"}},

    # --- Boundaries ---
    {"description": "Extremely long owner", "arguments": {
        "command": "get_repo", "owner": _long_str, "repo": "r"}},
    {"description": "Unicode-bomb query", "arguments": {
        "command": "search_code", "query": _unicode}},
    {"description": "Negative pr_number", "arguments": {
        "command": "get_pr", "owner": "a", "repo": "b", "pr_number": -42}},
    {"description": "pr_number = 0", "arguments": {
        "command": "get_pr", "owner": "a", "repo": "b", "pr_number": 0}},
    {"description": "pr_number huge", "arguments": {
        "command": "get_pr", "owner": "a", "repo": "b", "pr_number": 10**18}},

    # --- Plausible (no token → expected TOOL-ERR, not CRASH-500) ---
    {"description": "list_repos for plausible org", "arguments": {
        "command": "list_repos", "owner": "github"}},
    {"description": "search_code plausible query", "arguments": {
        "command": "search_code", "query": "FastMCP language:python"}},
    {"description": "get_file on plausible repo", "arguments": {
        "command": "get_file", "owner": "fastapi", "repo": "fastapi", "path": "README.md"}},
]
