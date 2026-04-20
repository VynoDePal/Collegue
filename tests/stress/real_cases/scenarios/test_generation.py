"""Real-world scenarios for test_generation (LLM)."""
from __future__ import annotations

from tests.stress.real_cases import response_text, tool_content

TOOL_NAME = "test_generation"


SCENARIOS = [
    {
        "id": "tg-01-pure-function-pytest",
        "description": "Generate pytest tests for a pure function",
        "arguments": {
            "code": "def add(a, b):\n    return a + b\n",
            "language": "python",
            "test_framework": "pytest",
        },
        "llm_dependent": True,
        "assertions": [
            ("Output non vide",
             lambda r: len(tool_content(r).get("test_code") or "") > 30),
            ("Contient 'def test_'",
             lambda r: "def test_" in response_text(r)),
            ("Mentionne la fonction 'add'",
             lambda r: "add" in response_text(r)),
        ],
    },
    {
        "id": "tg-02-class-unittest",
        "description": "Generate unittest tests for a class with a save() method",
        "arguments": {
            "code": (
                "class User:\n"
                "    def __init__(self, name):\n"
                "        self.name = name\n"
                "    def save(self):\n"
                "        return True\n"
            ),
            "language": "python",
            "test_framework": "unittest",
        },
        "llm_dependent": True,
        "assertions": [
            ("Output non vide",
             lambda r: len(tool_content(r).get("test_code") or "") > 30),
            ("Mentionne User ou save",
             lambda r: "User" in response_text(r) or "save" in response_text(r)),
        ],
    },
]
