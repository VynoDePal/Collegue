"""Real-world scenarios for code_documentation (LLM)."""
from __future__ import annotations

from tests.stress.real_cases import fixture, response_text, tool_content

TOOL_NAME = "code_documentation"


SCENARIOS = [
    {
        "id": "cd-01-calculator-class",
        "description": "Document the Calculator class with add/subtract/reset",
        "arguments": {
            "code": fixture("python", "calculator.py"),
            "language": "python",
            "doc_format": "markdown",
        },
        "llm_dependent": True,
        "assertions": [
            ("Documentation non vide",
             lambda r: len(tool_content(r).get("documentation") or "") > 100),
            ("Mentionne 'Calculator'",
             lambda r: "Calculator" in response_text(r)),
            ("Mentionne au moins 2 méthodes sur (add, subtract, reset)",
             lambda r: sum(1 for m in ("add", "subtract", "reset")
                            if m in response_text(r)) >= 2),
        ],
    },
    {
        "id": "cd-02-js-module",
        "description": "Document a JS module with 2 named exports",
        "arguments": {
            "code": fixture("javascript", "app.js"),
            "language": "javascript",
            "doc_format": "markdown",
        },
        "llm_dependent": True,
        "assertions": [
            ("Documentation non vide",
             lambda r: len(tool_content(r).get("documentation") or "") > 80),
            ("Mentionne formatName ou computeDiscount",
             lambda r: ("formatName" in response_text(r)
                         or "computeDiscount" in response_text(r))),
        ],
    },
]
