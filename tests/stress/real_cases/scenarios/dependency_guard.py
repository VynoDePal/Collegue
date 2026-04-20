"""Real-world scenarios for dependency_guard (offline)."""
from __future__ import annotations

from tests.stress.real_cases import fixture, tool_content

TOOL_NAME = "dependency_guard"
_OFF = {"check_vulnerabilities": False, "check_existence": False}


SCENARIOS = [
    {
        "id": "dg-01-project-requirements",
        "description": "Parse the project's own requirements.txt",
        "arguments": {
            "content": open("requirements.txt", encoding="utf-8").read(),
            "language": "python",
            **_OFF,
        },
        "llm_dependent": False,
        "assertions": [
            ("≥ 15 dépendances détectées",
             lambda r: (tool_content(r).get("total_dependencies") or 0) >= 15),
            ("valid=true",
             lambda r: tool_content(r).get("valid") is True),
        ],
    },
    {
        "id": "dg-02-typosquat-blocklist",
        "description": "Typosquat urllib-3 detected via blocklist",
        "arguments": {
            "content": fixture("requirements_vuln.txt"),
            "language": "python",
            "blocklist": ["urllib-3", "python-dateutils"],
            **_OFF,
        },
        "llm_dependent": False,
        "assertions": [
            ("≥ 1 issue détectée",
             lambda r: len(tool_content(r).get("issues") or []) >= 1),
            ("total_dependencies ≥ 5",
             lambda r: (tool_content(r).get("total_dependencies") or 0) >= 5),
        ],
    },
    {
        "id": "dg-03-package-json",
        "description": "Parse package.json with deps + devDependencies",
        "arguments": {
            "content": fixture("package_sample.json"),
            "language": "javascript",
            **_OFF,
        },
        "llm_dependent": False,
        "assertions": [
            ("≥ 3 dépendances (express, lodash, axios) détectées",
             lambda r: (tool_content(r).get("total_dependencies") or 0) >= 3),
        ],
    },
]
