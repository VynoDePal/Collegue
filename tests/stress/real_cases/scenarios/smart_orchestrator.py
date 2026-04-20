"""Real-world scenarios for smart_orchestrator (LLM)."""
from __future__ import annotations

from tests.stress.real_cases import tool_content, tools_used

TOOL_NAME = "smart_orchestrator"


SCENARIOS = [
    {
        "id": "orch-01-doc-task",
        "description": "Plain documentation task → should invoke code_documentation",
        "arguments": {
            "query": (
                "Génère la documentation markdown pour ce code Python:\n"
                "```python\n"
                "def add(a, b):\n"
                "    return a + b\n"
                "```"
            ),
        },
        "llm_dependent": True,
        "assertions": [
            ("Plan exécuté (tools_used non vide)",
             lambda r: len(tools_used(r)) >= 1),
            ("code_documentation invoqué",
             lambda r: "code_documentation" in tools_used(r)),
        ],
    },
    {
        "id": "orch-02-security-pipeline",
        "description": "Multi-step: scan secrets then refactor → ≥2 tools in plan",
        "arguments": {
            "query": (
                "Scanne ce code pour des secrets puis refactore-le proprement:\n"
                "```python\n"
                "API_KEY = 'AKIAIOSFODNN7EXAMPLE'\n"
                "def do():\n"
                "    print(API_KEY)\n"
                "```"
            ),
        },
        "llm_dependent": True,
        "assertions": [
            ("≥ 2 tools dans le plan",
             lambda r: len(set(tools_used(r))) >= 2),
            ("secret_scan invoqué",
             lambda r: "secret_scan" in tools_used(r)),
        ],
    },
]
