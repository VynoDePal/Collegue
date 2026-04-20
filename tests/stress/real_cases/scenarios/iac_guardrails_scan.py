"""Real-world scenarios for iac_guardrails_scan."""
from __future__ import annotations

from tests.stress.real_cases import fixture, has_rule, tool_content

TOOL_NAME = "iac_guardrails_scan"


SCENARIOS = [
    {
        "id": "iac-01-bad-terraform",
        "description": "Terraform with S3 public ACL + SSH 0.0.0.0/0 + RDS public",
        "arguments": {
            "files": [{"path": "main.tf", "content": fixture("terraform", "bad.tf")}],
        },
        "llm_dependent": False,
        "assertions": [
            ("Détecte TF-002 (S3 public ACL)",
             lambda r: has_rule(r, "TF-002")),
            ("Détecte TF-004 (SSH port open to world)",
             lambda r: has_rule(r, "TF-004")),
            ("Détecte TF-003 (RDS publicly accessible)",
             lambda r: has_rule(r, "TF-003")),
            ("security_score dégradé (< 0.5)",
             lambda r: (tool_content(r).get("security_score") if tool_content(r).get("security_score") is not None else 1.0) < 0.5),
            ("passed=false",
             lambda r: tool_content(r).get("passed") is False),
        ],
    },
    {
        "id": "iac-02-good-terraform",
        "description": "Clean Terraform (private S3, encrypted RDS, restricted SG)",
        "arguments": {
            "files": [{"path": "main.tf", "content": fixture("terraform", "good.tf")}],
        },
        "llm_dependent": False,
        "assertions": [
            ("0 finding critical",
             lambda r: all(f.get("severity") != "critical"
                            for f in (tool_content(r).get("findings") or []))),
            ("security_score ≥ 0.7",
             lambda r: (tool_content(r).get("security_score") if tool_content(r).get("security_score") is not None else 0.0) >= 0.7),
        ],
    },
    {
        "id": "iac-03-k8s-privileged",
        "description": "K8s deployment with privileged: true",
        "arguments": {
            "files": [{"path": "deployment.yaml", "content": fixture("k8s", "privileged.yaml")}],
        },
        "llm_dependent": False,
        "assertions": [
            ("≥ 1 finding non vide",
             lambda r: len(tool_content(r).get("findings") or []) >= 1),
        ],
    },
    {
        "id": "iac-04-dockerfile-bad",
        "description": "Dockerfile with USER root + ADD http://",
        "arguments": {
            "files": [{"path": "Dockerfile", "content": fixture("dockerfile", "bad.Dockerfile")}],
        },
        "llm_dependent": False,
        "assertions": [
            ("≥ 1 finding non vide",
             lambda r: len(tool_content(r).get("findings") or []) >= 1),
        ],
    },
]
