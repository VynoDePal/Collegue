"""Stress payloads for dependency_guard. Offline mode (no OSV / no registry)."""
TOOL_NAME = "dependency_guard"

_OFF = {"check_vulnerabilities": False, "check_existence": False}


def _req(content: str, language: str = "python", **extra) -> dict:
    return {"content": content, "language": language, **_OFF, **extra}


_big_requirements = "\n".join(f"package{i}==1.{i}.0" for i in range(10_000))
_json_bomb = '{"dependencies": {' + ",".join(f'"p{i}":"^1.0.0"' for i in range(50_000)) + '}}'
_package_json_no_deps = '{"name": "x", "version": "1.0.0"}'
_pkg_bad_names = "\n".join([
    "../etc/passwd==1.0.0",
    "normal_pkg==1.0.0",
    "\x00evil==1.0.0",
    "'; drop table;--==1.0.0",
])
_pkg_bad_versions = "\n".join([
    "flask>=abc",
    "django==1.2.3.4.5.6.7.8",
    "pytest~!malformed",
])

PAYLOADS = [
    {"description": "content vide", "arguments": _req("")},
    {"description": "language non supporte (rust)", "arguments": _req("tokio = \"1\"", language="rust")},
    {"description": "content 10MB x * 10_000_000", "arguments": _req("x" * 10_000_000)},
    {"description": "JSON bomb 50k deps (JS)",
     "arguments": _req(_json_bomb, language="javascript")},
    {"description": "10 000 packages Python requirements",
     "arguments": _req(_big_requirements)},
    {"description": "package.json sans cle dependencies",
     "arguments": _req(_package_json_no_deps, language="javascript")},
    {"description": "allowlist/blocklist 10k entries each",
     "arguments": _req("flask==2.0.0", allowlist=[f"pkg{i}" for i in range(10_000)],
                       blocklist=[f"evil{i}" for i in range(10_000)])},
    {"description": "Noms packages avec ../ et null bytes",
     "arguments": _req(_pkg_bad_names)},
    {"description": "Versions malformees (bornes ou format)",
     "arguments": _req(_pkg_bad_versions)},
    {"description": "language vide",
     "arguments": _req("flask==2.0", language="")},
]
