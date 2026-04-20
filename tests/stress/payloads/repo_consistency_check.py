"""Stress payloads for repo_consistency_check."""
TOOL_NAME = "repo_consistency_check"

_huge_file = "import os\n" + ("x = 1\n" * 100_000)
_circular_a = "from b import thing\n\ndef f():\n    return thing()\n"
_circular_b = "from a import f\n\ndef thing():\n    return f()\n"
_bad_diff = "just some random text\nnot a diff"

PAYLOADS = [
    {"description": "files=[] (min_length)", "arguments": {"files": []}},
    {"description": "min_confidence=-1 hors borne",
     "arguments": {"files": [{"path": "a.py", "content": "import os\n"}], "min_confidence": -1}},
    {"description": "min_confidence=150 hors borne",
     "arguments": {"files": [{"path": "a.py", "content": "import os\n"}], "min_confidence": 150}},
    {"description": "checks inconnu",
     "arguments": {"files": [{"path": "a.py", "content": "import os\n"}],
                   "checks": ["unknown_check"]}},
    {"description": "Fichier Python 100k lignes",
     "arguments": {"files": [{"path": "big.py", "content": _huge_file}]}},
    {"description": "Diff malforme (pas ---/+++ headers)",
     "arguments": {"files": [{"path": "a.py", "content": "import os\n"}], "diff": _bad_diff}},
    {"description": "mode=deep avec imports cycliques",
     "arguments": {
         "files": [
             {"path": "a.py", "content": _circular_a},
             {"path": "b.py", "content": _circular_b},
         ],
         "mode": "deep",
         "language": "python",
     }},
    {"description": "500 fichiers Python",
     "arguments": {"files": [
         {"path": f"f{i}.py", "content": f"import os\nx{i} = {i}\n"} for i in range(500)
     ]}},
    {"description": "language=auto + fichiers multiples langages",
     "arguments": {"files": [
         {"path": "x.py", "content": "import os\n"},
         {"path": "y.js", "content": "const x = require('fs');\n"},
         {"path": "z.ts", "content": "interface X { a: string }\n"},
         {"path": "w.php", "content": "<?php echo 'hi';"},
     ], "language": "auto"}},
    {"description": "analysis_depth=deep tres gros fichier",
     "arguments": {
         "files": [{"path": "big.py", "content": _huge_file}],
         "analysis_depth": "deep",
     }},
]
