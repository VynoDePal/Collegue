"""Stress payloads for iac_guardrails_scan."""
TOOL_NAME = "iac_guardrails_scan"

_100k_yaml = "apiVersion: v1\nkind: Pod\nmetadata:\n  name: p\nspec:\n" + "  ports:\n    - containerPort: 8080\n" * 100_000
_tf_empty = ""
_bad_terraform = 'resource "aws_s3_bucket" "x" {\n  acl = "public-read\n' + "}" * 50  # unclosed quote
_yaml_anchor_bomb = """
a: &a ["x","x","x","x","x","x","x","x","x","x"]
b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a,*a]
c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b,*b]
d: &d [*c,*c,*c,*c,*c,*c,*c,*c,*c,*c]
e: [*d,*d,*d,*d,*d,*d,*d,*d,*d,*d]
"""

PAYLOADS = [
    {"description": "files vide", "arguments": {"files": []}},
    {"description": "Fichier Terraform vide",
     "arguments": {"files": [{"path": "main.tf", "content": _tf_empty}]}},
    {"description": "YAML 100k lignes",
     "arguments": {"files": [{"path": "pod.yaml", "content": _100k_yaml}]}},
    {"description": "YAML anchor recursion bomb",
     "arguments": {"files": [{"path": "bomb.yaml", "content": _yaml_anchor_bomb}]}},
    {"description": "Custom policy ReDoS (a+)+$",
     "arguments": {
         "files": [{"path": "x.tf", "content": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa!"}],
         "custom_policies": [{
             "id": "redos-test",
             "content": r"(a+)+$",
             "language": "regex",
             "severity": "critical",
         }],
     }},
    {"description": "policy_profile invalide",
     "arguments": {"files": [{"path": "x.tf", "content": "resource \"aws_s3_bucket\" \"b\" {}"}],
                   "policy_profile": "unknown"}},
    {"description": "analysis_depth=deep",
     "arguments": {
         "files": [{"path": "main.tf", "content": 'resource "aws_s3_bucket" "b" {\n  acl = "public-read"\n}'}],
         "analysis_depth": "deep",
     }},
    {"description": "Terraform avec quote non fermee",
     "arguments": {"files": [{"path": "bad.tf", "content": _bad_terraform}]}},
    {"description": "50 fichiers melanges",
     "arguments": {
         "files": [{"path": f"f{i}.tf", "content": "resource \"aws_s3_bucket\" \"b\" {}"} for i in range(50)]
     }},
    {"description": "engines invalide",
     "arguments": {
         "files": [{"path": "x.tf", "content": ""}],
         "engines": ["nonexistent-engine"],
     }},
]
