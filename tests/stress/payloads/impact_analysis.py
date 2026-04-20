"""Stress payloads for impact_analysis."""
TOOL_NAME = "impact_analysis"

_large_diff = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-old\n+new\n" * 50_000
_long_intent = "Rewrite the payment flow. " * 5000  # ~100k chars

PAYLOADS = [
    {"description": "change_intent vide",
     "arguments": {"change_intent": "", "files": [{"path": "a.py", "content": "x=1\n"}]}},
    {"description": "files path traversal",
     "arguments": {"change_intent": "refactor", "files": [{"path": "../../etc/passwd", "content": ""}]}},
    {"description": "change_intent 100k chars",
     "arguments": {"change_intent": _long_intent, "files": [{"path": "a.py", "content": "x=1\n"}]}},
    {"description": "entry_points liste vide",
     "arguments": {"change_intent": "rm fn", "files": [{"path": "a.py", "content": "def f(): pass"}], "entry_points": []}},
    {"description": "assumptions dict 10k cles",
     "arguments": {
         "change_intent": "test", "files": [{"path": "a.py", "content": "x=1\n"}],
         "assumptions": {f"k{i}": "v" for i in range(10_000)},
     }},
    {"description": "confidence_mode invalide (evil)",
     "arguments": {"change_intent": "t", "files": [{"path": "a.py", "content": "x=1\n"}],
                   "confidence_mode": "evil"}},
    {"description": "analysis_depth=deep sur 50 fichiers",
     "arguments": {
         "change_intent": "cross-cutting refactor",
         "files": [{"path": f"f{i}.py", "content": f"def f{i}(): return {i}\n"} for i in range(50)],
         "analysis_depth": "deep",
     }},
    {"description": "diff tres volumineux (5MB)",
     "arguments": {
         "change_intent": "rewrite", "files": [{"path": "a.py", "content": "x=1\n"}],
         "diff": _large_diff,
     }},
    {"description": "files=[] (min_length)",
     "arguments": {"change_intent": "test", "files": []}},
    {"description": "change_intent ANSI escape sequences",
     "arguments": {"change_intent": "\x1b[31mRED\x1b[0m pwned", "files": [{"path": "a.py", "content": "x=1\n"}]}},
]
