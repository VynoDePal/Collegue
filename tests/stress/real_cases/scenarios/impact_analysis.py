"""Real-world scenarios for impact_analysis."""
from __future__ import annotations

from tests.stress.real_cases import tool_content

TOOL_NAME = "impact_analysis"


def _search_terms(resp) -> set[str]:
    return {q.get("query", "").lower()
            for q in (tool_content(resp).get("search_queries") or [])
            if isinstance(q, dict)}


SCENARIOS = [
    {
        "id": "imp-01-rename-add",
        "description": "Rename identifier 'add' → expect search_queries + non-empty summary",
        # Use the exact "rename X to Y" form that IDENTIFIER_PATTERNS recognises (no filler word).
        "arguments": {
            "change_intent": "rename add to sum",
            "files": [
                {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
                {"path": "api.py", "content": "from calc import add\nresult = add(1, 2)\n"},
                {"path": "report.py", "content": "from calc import add as _a\nprint(_a(3, 4))\n"},
            ],
        },
        "llm_dependent": False,
        "assertions": [
            ("Au moins une search_query générée",
             lambda r: len(tool_content(r).get("search_queries") or []) >= 1),
            ("change_summary non vide",
             lambda r: bool(tool_content(r).get("change_summary"))),
        ],
    },
    {
        "id": "imp-02-with-diff",
        "description": "Patch analysis with real diff block",
        "arguments": {
            "change_intent": "Fix edge case in divide_by when divisor is zero",
            "files": [{"path": "math.py", "content": "def divide_by(a, b):\n    return a / b\n"}],
            "diff": (
                "--- a/math.py\n"
                "+++ b/math.py\n"
                "@@ -1,2 +1,4 @@\n"
                " def divide_by(a, b):\n"
                "+    if b == 0:\n"
                "+        raise ValueError('b cannot be 0')\n"
                "     return a / b\n"
            ),
        },
        "llm_dependent": False,
        "assertions": [
            ("change_summary non vide",
             lambda r: bool(tool_content(r).get("change_summary"))),
            ("Structure exploitable (4 champs présents)",
             lambda r: all(k in tool_content(r)
                            for k in ("impacted_files", "risk_notes",
                                       "search_queries", "tests_to_run"))),
        ],
    },
]
