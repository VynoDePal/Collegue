"""Real-world scenarios for code_refactoring (LLM)."""
from __future__ import annotations

from tests.stress.real_cases import tool_content

TOOL_NAME = "code_refactoring"


BLOATED_CODE = """\
def compute(x):
    if x == 1:
        return 1
    if x == 2:
        return 2
    if x == 3:
        return 3
    if x == 4:
        return 4
    return 0
"""


SCENARIOS = [
    {
        "id": "cr-01-simplify",
        "description": "Simplify a function with 4 identical if-return blocks",
        "arguments": {
            "code": BLOATED_CODE,
            "language": "python",
            "refactoring_type": "simplify",
        },
        "llm_dependent": True,
        "assertions": [
            ("Output refactored_code non vide",
             lambda r: len(tool_content(r).get("refactored_code") or "") > 5),
            ("≥ 1 change ou code plus court",
             lambda r: (len(tool_content(r).get("changes") or []) >= 1
                         or len(tool_content(r).get("refactored_code") or "")
                            < len(BLOATED_CODE))),
        ],
    },
    {
        "id": "cr-02-clean",
        "description": "Clean refactoring on a minor mess",
        "arguments": {
            "code": "import os\n\ndef hi():  print('hi')   # trailing\n",
            "language": "python",
            "refactoring_type": "clean",
        },
        "llm_dependent": True,
        "assertions": [
            ("Output non vide",
             lambda r: len(tool_content(r).get("refactored_code") or "") > 5),
        ],
    },
]
