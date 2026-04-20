"""Real-world scenarios for repo_consistency_check."""
from __future__ import annotations

from tests.stress.real_cases import fixture, tool_content

TOOL_NAME = "repo_consistency_check"


def _issue_kinds(resp) -> set[str]:
    return {i.get("kind") for i in (tool_content(resp).get("issues") or [])
            if isinstance(i, dict)}


SCENARIOS = [
    {
        "id": "rcc-01-unused-imports",
        "description": "Dead code fixture: 3 unused imports + 1 unused private fn",
        "arguments": {
            "files": [{"path": "dead.py", "content": fixture("python", "dead_code.py")}],
            "language": "python",
            "mode": "fast",
            "checks": ["unused_imports", "dead_code"],
        },
        "llm_dependent": False,
        "assertions": [
            ("≥ 3 imports inutilisés détectés",
             lambda r: sum(1 for i in (tool_content(r).get("issues") or [])
                            if isinstance(i, dict) and i.get("kind") == "unused_import") >= 3),
            ("Lines 2, 3, 4 présentes dans les findings",
             lambda r: {i.get("line") for i in (tool_content(r).get("issues") or [])}
                        .issuperset({2, 3, 4})),
        ],
    },
    {
        "id": "rcc-02-clean",
        "description": "Clean Python file, all imports used",
        "arguments": {
            "files": [{"path": "clean.py", "content": fixture("python", "clean.py")}],
            "language": "python",
            "mode": "fast",
            "checks": ["unused_imports", "dead_code"],
        },
        "llm_dependent": False,
        "assertions": [
            ("0 issue unused_import",
             lambda r: "unused_import" not in _issue_kinds(r)),
            ("valid=true",
             lambda r: tool_content(r).get("valid") is True),
        ],
    },
]
