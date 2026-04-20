"""Stress payloads for secret_scan."""
TOOL_NAME = "secret_scan"

_big_text = "hello world\n" * 500_000  # ~6MB
_many_keys = "\n".join(f"AKIA{i:016d}XXXXXXXX" for i in range(1000))
_redos = "a" * 50_000 + "!"
_unicode = ("\U0001f511" * 1000) + ("\u202e" * 100) + ("a\u0301" * 1000)
_null_bytes = ("\x00\x01\x02" * 3000) + "password=hunter2"

PAYLOADS = [
    {"description": "Aucun champ (target/content/files absents)", "arguments": {}},
    {"description": "content vide", "arguments": {"content": "", "scan_type": "content"}},
    {"description": "Null bytes + control chars", "arguments": {"content": _null_bytes, "scan_type": "content"}},
    {"description": "Big content ~6MB", "arguments": {"content": _big_text, "scan_type": "content"}},
    {"description": "1000 cles fictives AKIA", "arguments": {"content": _many_keys, "scan_type": "content"}},
    {"description": "ReDoS candidate (a*50000+!)", "arguments": {"content": _redos, "scan_type": "content"}},
    {"description": "Unicode emojis + RTL + combining", "arguments": {"content": _unicode, "scan_type": "content"}},
    {"description": "scan_type=batch mais files vide", "arguments": {"scan_type": "batch", "files": []}},
    {"description": "files=200 fichiers inexistants", "arguments": {
        "scan_type": "batch",
        "files": [f"/tmp/nonexistent/{i}.txt" for i in range(200)],
    }},
    {"description": "severity_threshold invalide", "arguments": {
        "content": "api_key=abc", "scan_type": "content", "severity_threshold": "super-high"}},
    {"description": "max_file_size hors bornes", "arguments": {
        "content": "x", "scan_type": "content", "max_file_size": 10_000_000_000}},
]
