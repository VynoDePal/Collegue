"""Stress payloads for postgres_db (#206).

Goals (see issue #206):
- Pydantic validation on `command` + whitelist of 8 commands.
- SELECT-only enforcement (postgres_db.py::validate_query_safety) —
  every write / DDL keyword must be refused at the request layer.
- Plausible SELECTs without POSTGRES_URL → TOOL-ERR, not CRASH-500.
"""
TOOL_NAME = "postgres_db"

_long_str = "t" * 10_000
_giant_query = "SELECT " + ", ".join(f"col{i}" for i in range(5000)) + " FROM t"

PAYLOADS = [
    # --- Validation ---
    {"description": "No command", "arguments": {}},
    {"description": "Empty command", "arguments": {"command": ""}},
    {"description": "Non-whitelisted command", "arguments": {"command": "truncate_table"}},
    {"description": "describe_table without table_name", "arguments": {"command": "describe_table"}},
    {"description": "limit over max", "arguments": {
        "command": "sample_data", "table_name": "users", "limit": 999_999}},
    {"description": "limit negative", "arguments": {
        "command": "sample_data", "table_name": "users", "limit": -1}},

    # --- SELECT-only guard (must be refused) ---
    {"description": "INSERT in query", "arguments": {
        "command": "query", "query": "INSERT INTO users VALUES (1)"}},
    {"description": "UPDATE in query", "arguments": {
        "command": "query", "query": "UPDATE users SET role='admin'"}},
    {"description": "DELETE in query", "arguments": {
        "command": "query", "query": "DELETE FROM users WHERE 1=1"}},
    {"description": "DROP in query", "arguments": {
        "command": "query", "query": "DROP TABLE users"}},
    {"description": "TRUNCATE in query", "arguments": {
        "command": "query", "query": "TRUNCATE users CASCADE"}},
    {"description": "ALTER in query", "arguments": {
        "command": "query", "query": "ALTER TABLE users ADD pwned text"}},
    {"description": "GRANT in query", "arguments": {
        "command": "query", "query": "GRANT ALL ON users TO public"}},
    {"description": "Whitespace trick: leading newline then DELETE", "arguments": {
        "command": "query", "query": "\n\nDELETE FROM users"}},
    {"description": "Comment trick: /*+hint*/ DROP", "arguments": {
        "command": "query", "query": "/*! DROP */ TABLE users"}},
    {"description": "Multi-statement: SELECT; DROP", "arguments": {
        "command": "query", "query": "SELECT 1; DROP TABLE users"}},

    # --- Adversarial: SQL injection in table_name (must hit identifier validator) ---
    {"description": "SQL injection in table_name", "arguments": {
        "command": "sample_data", "table_name": "users; DROP TABLE users"}},
    {"description": "Schema-qualified in table_name", "arguments": {
        "command": "sample_data", "table_name": "public.users"}},
    {"description": "Quote injection in table_name", "arguments": {
        "command": "describe_table", "table_name": 'users" OR "1"="1'}},
    {"description": "Null byte in table_name", "arguments": {
        "command": "describe_table", "table_name": "users\x00admin"}},

    # --- Boundaries ---
    {"description": "Extremely long table_name", "arguments": {
        "command": "describe_table", "table_name": _long_str}},
    {"description": "Giant SELECT (5000 columns)", "arguments": {
        "command": "query", "query": _giant_query}},

    # --- Plausible SELECT without POSTGRES_URL → TOOL-ERR ---
    {"description": "Plausible SELECT count", "arguments": {
        "command": "query", "query": "SELECT count(*) FROM users"}},
    {"description": "list_tables plausible schema", "arguments": {
        "command": "list_tables", "schema_name": "public"}},
]
