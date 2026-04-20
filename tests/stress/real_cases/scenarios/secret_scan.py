"""Real-world scenarios for secret_scan."""
from __future__ import annotations

from tests.stress.real_cases import fixture, findings_of, tool_content

TOOL_NAME = "secret_scan"


def _types(resp) -> set[str]:
    return {f.get("type") for f in findings_of(resp) if isinstance(f, dict)}


SCENARIOS = [
    {
        "id": "ss-01-multi-secrets",
        "description": "Python module with AWS + OpenAI + DB URL + GitHub token",
        "arguments": {
            "content": fixture("python", "with_secrets.py"),
            "scan_type": "content",
        },
        "llm_dependent": False,
        "assertions": [
            ("≥ 4 findings détectés (la fixture contient 4 types de secrets)",
             lambda r: len(findings_of(r)) >= 4),
            ("AWS access key détectée",
             lambda r: any("aws" in (t or "").lower() for t in _types(r))),
            ("OpenAI-style key détectée (type spécifique OU via env_secret/password)",
             lambda r: any(t and ("openai" in t.lower() or "api_key" in t.lower()
                                   or "bearer" in t.lower() or "env_secret" in t.lower()
                                   or "password" in t.lower())
                            for t in _types(r))),
            ("GitHub token (ghp_) détecté",
             lambda r: any("github" in (t or "").lower() for t in _types(r))),
            ("Postgres URI détecté",
             lambda r: any(("postgres" in (t or "").lower() or "password_in_url" in (t or "").lower())
                            for t in _types(r))),
        ],
    },
    {
        "id": "ss-02-clean-file",
        "description": "Clean Python file, no secrets expected",
        "arguments": {
            "content": fixture("python", "clean.py"),
            "scan_type": "content",
        },
        "llm_dependent": False,
        "assertions": [
            ("Aucun finding critical/high (pas de faux positif)",
             lambda r: all(f.get("severity") not in ("critical", "high")
                            for f in findings_of(r))),
            ("Réponse structurée avec champ clean / total_findings",
             lambda r: "total_findings" in tool_content(r)),
        ],
    },
    {
        "id": "ss-03-env-style-payload",
        "description": ".env-style content with DATABASE_URL",
        "arguments": {
            "content": (
                "DATABASE_URL=postgres://admin:hunter2@db.internal:5432/prod\n"
                "REDIS_URL=redis://:p4ssw0rd@redis.internal:6379/0\n"
            ),
            "scan_type": "content",
        },
        "llm_dependent": False,
        "assertions": [
            ("≥ 1 finding",
             lambda r: len(findings_of(r)) >= 1),
        ],
    },
]
