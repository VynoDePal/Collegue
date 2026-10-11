"""Tests hors ligne de la sonde minimale du jeton de la fixture (C65) : aucun accès réseau, aucun vrai jeton."""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fixture_token_diagnostic", ROOT / "scripts" / "fixture_token_diagnostic.py"
)
assert SPEC and SPEC.loader
diag = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = diag
SPEC.loader.exec_module(diag)

TOKEN = "github_pat_FAKE0123456789abcdefghijKLMNOP_0123456789"
WORKFLOW = ROOT / ".github" / "workflows" / "fixture-token-diagnostic.yml"
REQUEST_ID = "A1B2:3C4D:5E6F7A:8B9C0D:6543210F"


class FakeApi:
    """Transport factice : table ``chemin -> (statut, en-têtes, corps JSON ou octets)`` ; mémorise les appels."""

    def __init__(self, routes: Optional[Dict[str, Any]] = None, default: Any = None):
        self.routes = routes or {}
        self.default = default
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float):
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout})
        path = urllib.parse.urlsplit(url).path
        route = self.routes.get(path, self.default)
        if route is None:
            route = (404, {}, {"message": "Not Found"})
        if isinstance(route, diag.Reply):
            return route
        status, headers_out, body = route
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return diag.Reply(status, {k.lower(): v for k, v in headers_out.items()}, raw)


def rights_headers(remaining: int = 4979, **extra: str) -> Dict[str, str]:
    return {
        "X-GitHub-Request-Id": REQUEST_ID,
        "X-Accepted-GitHub-Permissions": "administration=read",
        "X-RateLimit-Limit": "5000",
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": "1790000000",
        "X-RateLimit-Resource": "core",
        **extra,
    }


def healthy_routes() -> Dict[str, Any]:
    repo = f"/repos/{diag.REPOSITORY}"
    return {
        "/user": (
            200,
            rights_headers(),
            {"login": "fixture-bot", "id": 7, "type": "User", "email": "secret@example.org"},
        ),
        repo: (
            200,
            rights_headers(),
            {"id": diag.REPOSITORY_ID, "full_name": diag.REPOSITORY, "private": True, "permissions": {"admin": True}},
        ),
        f"{repo}/collaborators/fixture-bot/permission": (
            200,
            rights_headers(),
            {"permission": "admin", "role_name": "admin"},
        ),
        f"{repo}/rules/branches/{diag.BOOTSTRAP_BRANCH}": (
            200,
            rights_headers(),
            [
                {
                    "type": "required_status_checks",
                    "ruleset_id": 24793056,
                    "parameters": {"required_status_checks": [{"context": "x"}]},
                }
            ],
        ),
        f"{repo}/rules/branches/{diag.ABSENT_BRANCH}": (200, rights_headers(), []),
        f"{repo}/rulesets/18840666": (
            200,
            rights_headers(),
            {"id": 18840666, "name": "seed", "enforcement": "active", "target": "branch", "rules": []},
        ),
        f"{repo}/rulesets/24793056": (
            200,
            rights_headers(),
            {"id": 24793056, "name": "bases", "enforcement": "active", "target": "branch", "rules": []},
        ),
        "/rate_limit": (
            200,
            rights_headers(),
            {"resources": {"core": {"limit": 5000, "remaining": 4979, "reset": 1790000000}}},
        ),
    }


def by_key(report: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {entry["key"]: entry for entry in report["endpoints"]}


# ── lecture nominale et liste fermée ───────────────────────────────────────────────────────────────────────────────────────


def test_nominal_run_reads_closed_list_with_get_only():
    api = FakeApi(healthy_routes())
    report = diag.run(TOKEN, api)
    assert report["verdict"] == "all_reads_answered" and report["failures"] == []
    assert "diagnostic des droits" in report["purpose"]
    paths = [urllib.parse.urlsplit(c["url"]).path for c in api.calls]
    repo = f"/repos/{diag.REPOSITORY}"
    assert paths == [
        "/user",
        repo,
        f"{repo}/collaborators/fixture-bot/permission",
        f"{repo}/branches/main/protection",
        f"{repo}/branches/{diag.BOOTSTRAP_BRANCH}/protection",
        f"{repo}/branches/{diag.ABSENT_BRANCH}/protection",
        f"{repo}/rules/branches/{diag.BOOTSTRAP_BRANCH}",
        f"{repo}/rules/branches/{diag.ABSENT_BRANCH}",
        f"{repo}/rulesets/18840666",
        f"{repo}/rulesets/24793056",
        "/rate_limit",
    ]
    assert all(c["url"].startswith("https://api.github.com/") and c["timeout"] <= 15 for c in api.calls)
    assert all(e["method"] == "GET" for e in report["endpoints"])
    assert by_key(report)["repository"]["data"]["id_matches_expected"] is True
    assert by_key(report)["user"]["data"] == {
        "login": "fixture-bot",
        "id": 7,
        "type": "User",
    }  # l'e-mail n'est pas recopié


def test_no_endpoint_for_collaborator_without_valid_login():
    routes = healthy_routes()
    routes["/user"] = (200, rights_headers(), {"login": "../../evil?x=1#", "id": 7})
    api = FakeApi(routes)
    report = diag.run(TOKEN, api)
    assert report["login"] is None
    assert not any("/collaborators/" in c["url"] for c in api.calls)
    assert "collaborator_permission" not in by_key(report)


def test_build_url_refuses_foreign_or_ambiguous_destinations():
    for bad in ("//evil.example/x", "/a/../b", "/a?b=1", "/a#b", "https://evil.example/x", "/a@evil", "x"):
        with pytest.raises(ValueError):
            diag.build_url(bad)
    assert diag.build_url("/user") == "https://api.github.com/user"


# ── 403 : droits vs quota ──────────────────────────────────────────────────────────────────────────────────────────────────


def test_403_permission_keeps_rights_header_and_is_not_rate_limit():
    routes = healthy_routes()
    path = f"/repos/{diag.REPOSITORY}/branches/{diag.BOOTSTRAP_BRANCH}/protection"
    routes[path] = (
        403,
        rights_headers(remaining=4979),
        {
            "message": "Resource not accessible by personal access token",
            "documentation_url": "https://docs.github.com/rest/branches/branch-protection#get-branch-protection",
        },
    )
    report = diag.run(TOKEN, FakeApi(routes))
    entry = by_key(report)["protection_bootstrap_v2"]
    assert entry["status"] == 403 and entry["outcome"] == "failure"
    assert entry["message_class"] == "resource_not_accessible_by_personal_access_token"
    assert entry["x_accepted_github_permissions"] == "administration=read"
    assert entry["x_github_request_id"] == REQUEST_ID
    assert entry["x_ratelimit_remaining"] == 4979 and entry["primary_rate_limit_exhausted"] is False
    assert entry["documentation_url"].startswith("https://docs.github.com/")
    assert report["verdict"] == "failures" and report["failures"] == ["protection_bootstrap_v2"]
    # les autres lectures continuent malgré le 403
    assert by_key(report)["ruleset_24793056"]["outcome"] == "ok"


def test_403_primary_quota_exhausted_is_distinguished():
    routes = healthy_routes()
    path = f"/repos/{diag.REPOSITORY}/branches/main/protection"
    routes[path] = (
        403,
        rights_headers(remaining=0, **{"Retry-After": "60"}),
        {"message": "API rate limit exceeded for user ID 7."},
    )
    entry = by_key(diag.run(TOKEN, FakeApi(routes)))["protection_main"]
    assert entry["primary_rate_limit_exhausted"] is True and entry["message_class"] == "primary_rate_limit"
    assert entry["retry_after"] == 60 and entry["x_ratelimit_remaining"] == 0


def test_secondary_rate_limit_is_classified_not_primary():
    routes = healthy_routes()
    routes["/user"] = (
        403,
        rights_headers(remaining=4000, **{"Retry-After": "30"}),
        {"message": "You have exceeded a secondary rate limit."},
    )
    entry = by_key(diag.run(TOKEN, FakeApi(routes)))["user"]
    assert entry["message_class"] == "secondary_rate_limit" and entry["primary_rate_limit_exhausted"] is False


def test_unknown_message_is_unclassified_never_copied():
    routes = healthy_routes()
    routes["/user"] = (403, rights_headers(), {"message": "texte arbitraire SECRET-VALUE"})
    entry = by_key(diag.run(TOKEN, FakeApi(routes)))["user"]
    assert entry["message_class"] == "unclassified"
    assert "SECRET-VALUE" not in json.dumps(entry)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_401_403_and_5xx_are_failures_with_nonzero_exit(status, tmp_path):
    routes = healthy_routes()
    routes["/rate_limit"] = (status, rights_headers(), {"message": "Requires authentication"})
    out = tmp_path / "r.json"
    rc = diag.main(["--output", str(out)], environ={diag.TOKEN_ENV: TOKEN}, transport=FakeApi(routes))
    assert rc == 1
    assert json.loads(out.read_text())["verdict"] == "failures"  # le rapport est écrit malgré l'échec


# ── 404 attendu ────────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_classic_protection_404_is_expected_for_ruleset_only_repo(tmp_path):
    api = FakeApi(healthy_routes())  # défaut : 404 Not Found sur les trois /protection
    out = tmp_path / "r.json"
    assert diag.main(["--output", str(out)], environ={diag.TOKEN_ENV: TOKEN}, transport=api) == 0
    report = json.loads(out.read_text())
    for key in ("protection_main", "protection_bootstrap_v2", "protection_absent_branch"):
        assert by_key(report)[key]["outcome"] == "expected_not_found"
    assert report["failures"] == []


def test_404_on_non_protection_endpoint_is_failure():
    routes = healthy_routes()
    del routes[f"/repos/{diag.REPOSITORY}"]
    report = diag.run(TOKEN, FakeApi(routes))
    assert by_key(report)["repository"]["outcome"] == "failure" and "repository" in report["failures"]


# ── jeton absent, redirections, proxys, exceptions ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [None, "", "court", "a b c d e f g h i j k l m n o p q r s t"])
def test_absent_or_malformed_token_refused_before_any_request(value, tmp_path, capsys):
    api = FakeApi(healthy_routes())
    environ = {} if value is None else {diag.TOKEN_ENV: value}
    out = tmp_path / "r.json"
    assert diag.main(["--output", str(out)], environ=environ, transport=api) == 2
    assert api.calls == [] and not out.exists()
    assert value is None or not value or value not in capsys.readouterr().err


def test_redirect_is_refused_not_followed():
    routes = healthy_routes()
    routes["/user"] = (302, {"Location": "https://evil.example/steal"}, b"")
    report = diag.run(TOKEN, FakeApi(routes))
    entry = by_key(report)["user"]
    assert entry["outcome"] == "redirect_refused" and "evil.example" not in json.dumps(report)
    assert "user" in report["failures"]


def test_default_opener_refuses_redirects_and_ignores_proxies(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:3128")
    opener = diag.build_opener()
    # ProxyHandler({}) remplace le défaut : aucun handler de proxy actif (donc HTTPS_PROXY est ignoré)
    assert not [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler) and h.proxies]
    assert not [
        h
        for h in opener.handlers
        if any(name.endswith("_open") and name.startswith(("http_proxy", "https_proxy")) for name in dir(h))
    ]
    redirect = next(h for h in opener.handlers if isinstance(h, diag._NoRedirect))
    assert redirect.redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None


def test_default_transport_returns_redirect_as_status_without_following(monkeypatch):
    class Opener:
        def open(self, request, timeout):  # noqa: ARG002
            raise urllib.error.HTTPError(request.full_url, 302, "Found", {"Location": "https://evil.example/"}, None)

    monkeypatch.setattr(diag, "build_opener", lambda: Opener())
    reply = diag.default_transport("https://api.github.com/user", {"Authorization": f"Bearer {TOKEN}"}, 1.0)
    assert reply.status == 302 and reply.body == b""


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TimeoutError(f"timed out {TOKEN}"), "timeout"),
        (urllib.error.URLError(ConnectionRefusedError(f"refused {TOKEN}")), "connection_refused"),
        (RuntimeError(f"Authorization: Bearer {TOKEN}"), "network_error"),
    ],
)
def test_network_exceptions_are_classified_without_their_text(exc, expected, monkeypatch, tmp_path, capsys):
    class Opener:
        def open(self, request, timeout):  # noqa: ARG002
            raise exc

    monkeypatch.setattr(diag, "build_opener", lambda: Opener())
    out = tmp_path / "r.json"
    assert diag.main(["--output", str(out)], environ={diag.TOKEN_ENV: TOKEN}) == 1
    captured = capsys.readouterr()
    text = out.read_text() + captured.out + captured.err
    assert TOKEN not in text and "Authorization" not in text
    assert by_key(json.loads(out.read_text()))["user"]["error_class"] == expected


def test_global_deadline_skips_remaining_requests():
    ticks = iter([0.0] + [diag.GLOBAL_BUDGET + 1] * 50)
    api = FakeApi(healthy_routes())
    report = diag.run(TOKEN, api, clock=lambda: next(ticks))
    assert api.calls == [] and all(e["outcome"] == "skipped_global_deadline" for e in report["endpoints"])
    assert report["verdict"] == "failures"


# ── fuite du jeton / données arbitraires ───────────────────────────────────────────────────────────────────────────────────


def test_malicious_echo_of_token_and_arbitrary_data_is_never_published(tmp_path, capsys):
    evil = healthy_routes()
    evil["/user"] = (
        200,
        {
            **rights_headers(),
            "X-Accepted-GitHub-Permissions": f"echo {TOKEN}",
            "X-GitHub-Request-Id": f"{TOKEN}",
            "X-Evil": TOKEN,
        },
        {"login": "fixture-bot", "id": 7, "type": "User", "bio": TOKEN, "message": TOKEN},
    )
    evil[f"/repos/{diag.REPOSITORY}"] = (
        200,
        rights_headers(),
        {
            "id": diag.REPOSITORY_ID,
            "full_name": f"VynoDePal/{TOKEN}",
            "description": TOKEN,
            "default_branch": f"{TOKEN}",
        },
    )
    evil[f"/repos/{diag.REPOSITORY}/rulesets/24793056"] = (
        200,
        rights_headers(),
        {
            "id": 24793056,
            "name": TOKEN,
            "enforcement": TOKEN,
            "conditions": {"ref_name": {"include": [TOKEN, "refs/heads/collegue-business/**"]}},
            "rules": [],
        },
    )
    evil["/rate_limit"] = (
        403,
        rights_headers(),
        {"message": TOKEN, "documentation_url": f"https://docs.github.com.evil.example/{TOKEN}"},
    )
    out = tmp_path / "r.json"
    rc = diag.main(["--output", str(out)], environ={diag.TOKEN_ENV: TOKEN}, transport=FakeApi(evil))
    assert rc == 1
    captured = capsys.readouterr()
    published = out.read_text() + captured.out + captured.err
    assert TOKEN not in published
    report = json.loads(out.read_text())
    assert "documentation_url" not in by_key(report)["rate_limit"]
    for key in ("user", "repository", "ruleset_24793056", "rate_limit"):
        entry = by_key(report)[key]
        assert entry["token_echo_detected"] is True and "data" not in entry and entry["outcome"] == "failure"


@pytest.mark.parametrize(
    "form",
    [
        lambda t: t,
        lambda t: urllib.parse.quote(t, safe=""),
        lambda t: base64.b64encode(t.encode()).decode(),
        lambda t: base64.b64encode(f"x-access-token:{t}".encode()).decode(),
        lambda t: base64.urlsafe_b64encode(f"Bearer {t}".encode()).decode().rstrip("="),
    ],
)
def test_report_containing_token_in_any_usual_form_is_refused(form):
    leaked = {"endpoints": [], "failures": [], "verdict": "x", "note": form(TOKEN)}
    assert diag.finalize(leaked, TOKEN) == (None, [])


def test_report_refused_leaves_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(diag, "run", lambda *a, **k: {"endpoints": [], "failures": [], "verdict": TOKEN})
    out = tmp_path / "r.json"
    assert diag.main(["--output", str(out)], environ={diag.TOKEN_ENV: TOKEN}) == 2 and not out.exists()


def test_authorization_header_is_sent_but_never_reported():
    api = FakeApi(healthy_routes())
    report = diag.run(TOKEN, api)
    assert all(c["headers"]["Authorization"] == f"Bearer {TOKEN}" for c in api.calls)
    text, lines = diag.finalize(report, TOKEN)
    assert text and TOKEN not in text and not any("Authorization" in line or TOKEN in line for line in lines)
    assert "diagnostic des droits seulement" in lines[-1]


def test_ruleset_fingerprint_is_stable_and_config_sensitive():
    base = {"id": 1, "enforcement": "active", "rules": [{"type": "deletion"}], "conditions": {}}
    other = {**base, "rules": [{"type": "update"}]}
    assert (
        diag.extract_ruleset(base)["configuration_fingerprint"]
        == diag.extract_ruleset(dict(base))["configuration_fingerprint"]
    )
    assert (
        diag.extract_ruleset(base)["configuration_fingerprint"]
        != diag.extract_ruleset(other)["configuration_fingerprint"]
    )


# ── contrat du workflow ────────────────────────────────────────────────────────────────────────────────────────────────────


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_triggers_only_on_push_of_exact_branch_with_paths():
    text = workflow_text()
    on_block = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert re.findall(r"^  (\w+):", on_block, re.M) == ["push"]
    assert "      - codex/w5-fixture-token-diagnostic-c\n" in on_block and "*" not in on_block.split("paths:")[0]
    paths = re.findall(r"^      - (\S+)$", on_block.split("paths:", 1)[1], re.M)
    assert paths == ["scripts/fixture_token_diagnostic.py", ".github/workflows/fixture-token-diagnostic.yml"]
    for forbidden in (
        "pull_request",
        "schedule",
        "workflow_dispatch",
        "workflow_run",
        "repository_dispatch",
        "pull_request_target",
    ):
        assert not re.search(rf"^\s*{forbidden}\s*:", text, re.M)


def test_workflow_least_privilege_and_bounded():
    text = workflow_text()
    assert re.search(r"^permissions:\n  contents: read\n", text, re.M)
    assert text.count("permissions:") == 1 and "write" not in re.sub(r"#.*", "", text)
    assert "timeout-minutes: 3" in text
    assert "persist-credentials: false" in text
    assert "docker" not in text.lower() and "pip " not in text and "setup-python" not in text


def test_workflow_secret_reaches_only_the_probe_step_and_no_model_key():
    text = workflow_text()
    assert text.count("secrets.") == 1 and "secrets.INTEGRATION_FIXTURE_GITHUB_TOKEN" in text
    steps = re.split(r"^      - ", text.split("    steps:\n", 1)[1], flags=re.M)[1:]
    holders = [s for s in steps if "secrets." in s]
    assert len(holders) == 1 and holders[0].startswith("name: Probe fixture token rights")
    assert "uses:" not in holders[0]  # le secret n'est jamais confié à du code tiers
    for forbidden in ("GOOGLE", "GEMINI", "OPENAI", "ANTHROPIC", "API_KEY", "GITHUB_TOKEN:", "github.token"):
        assert forbidden not in text
    uploads = [s for s in steps if "upload-artifact" in s]
    assert len(uploads) == 1 and "fixture-token-diagnostic.json" in uploads[0] and ".log" not in uploads[0]
    assert "secrets." not in uploads[0]


def test_probe_script_is_stdlib_get_only_and_reads_no_credential_file():
    source = (ROOT / "scripts" / "fixture_token_diagnostic.py").read_text(encoding="utf-8")
    imports = set(re.findall(r"^(?:from|import) (\w+)", source, re.M))
    assert imports <= {
        "__future__",
        "argparse",
        "base64",
        "hashlib",
        "http",
        "json",
        "os",
        "re",
        "socket",
        "ssl",
        "sys",
        "time",
        "urllib",
        "dataclasses",
        "pathlib",
        "typing",
    }
    assert 'method="GET"' in source and not re.search(r'method="(POST|PUT|PATCH|DELETE)"', source)
    assert "os.environ" in source and "environ.items" not in source and "os.environ.copy" not in source
    assert "netrc" not in source and ".git-credentials" not in source and "getenv" not in source
