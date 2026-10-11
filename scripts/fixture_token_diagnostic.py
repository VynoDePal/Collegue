#!/usr/bin/env python3
"""Sonde MINIMALE des droits du jeton de la fixture (propriété C, diagnostic temporaire — aucune campagne, aucune génération).

La campagne ``w5-gemma-20261010-dcf645da`` s'est arrêtée au préflight P07 sur
``GET /repos/VynoDePal/collegue-e2e-fixture/branches/collegue-business/<run>/protection`` : 403, « Rate limit GitHub atteint ou permissions
insuffisantes. Restant:4979 ». Le client du produit masque le message, le corps et les en-têtes de GitHub : la cause précise n'est pas établie.
Cette sonde rejoue, avec le MÊME secret (``INTEGRATION_FIXTURE_GITHUB_TOKEN``, passé à ce script par ``FIXTURE_DIAG_TOKEN``), une liste FERMÉE de
lectures ``GET`` et rapporte, pour chacune, UNIQUEMENT des champs d'une liste blanche. Elle ne décide de rien : « diagnostic des droits seulement ».

Garanties de conception :

* destination fixe ``https://api.github.com`` (hôte revérifié avant chaque requête), méthode ``GET`` seulement, chemins d'une liste fermée (le seul
  élément variable est le ``login`` renvoyé par ``/user``, validé par expression régulière) ;
* redirections REFUSÉES (jamais suivies : le jeton ne peut pas partir ailleurs), proxys de l'environnement IGNORÉS, aucun retry, délai court par
  requête et durée globale bornée ; chaque endpoint est tenté même si un précédent a répondu 403 (aucun verdict vert malgré un refus) ;
* le jeton n'est lu que dans ``FIXTURE_DIAG_TOKEN`` : jamais affiché, copié, empreinte, ni son préfixe ; aucun fichier d'identifiants n'est lu, aucune autre
  variable d'environnement n'est parcourue ; l'en-tête ``Authorization`` n'apparaît dans aucune sortie ;
* le rapport ne recopie JAMAIS un corps de réponse ni un message GitHub arbitraire : statut, méthode/chemin fixes, en-têtes d'une liste blanche validés par
  motif, champs typés (booléens, entiers, énumérations fermées, motifs validés), message CLASSÉ parmi des phrases connues ;
  ``documentation_url`` seulement si l'hôte est exactement ``docs.github.com`` ;
* avant toute écriture, le rapport complet est cherché pour le jeton sous ses formes usuelles (brute, URL, base64 seul, ``x-access-token:``, ``Bearer``) : s'il
  y figure, il est REFUSÉ (code 2, aucune donnée écrite) ;
* un 404 de PROTECTION CLASSIQUE n'est pas une erreur (dépôt protégé par rulesets) ; tout autre 4xx/5xx, tout échec réseau ou toute redirection est un échec
  (code 1). Un succès de collecte ne prouve NI le garde produit NI la campagne.

Codes : 0 = toutes les lectures attendues ont répondu ; 1 = au moins un échec (le rapport est écrit) ; 2 = refus (jeton absent ou malformé, rapport contenant le
jeton, argument invalide).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

API_BASE = "https://api.github.com"
API_HOST = "api.github.com"
REPOSITORY = "VynoDePal/collegue-e2e-fixture"
REPOSITORY_ID = 1298596453
BOOTSTRAP_BRANCH = "collegue-business/bootstrap-w5-v2"
ABSENT_BRANCH = "collegue-business/38027369635-1"  # branche de l'ancien run : ABSENTE, jamais créée ici
RULESET_IDS = (18840666, 24793056)
# Lectures utilisées APRÈS P07 (contrôles du socle V2) : identifiants issus de GET indépendants du manager, jamais devinés.
BOOTSTRAP_COMMIT = "ad56c0fa03066b1f6efb3cf6a5aa26cb496372ca"
CHECKS_APP_ID = 15368
WORKFLOW_JOB_ID = 113869164089
WORKFLOW_RUN_ID = 37945085226
WORKFLOW_PATH = ".github/workflows/fixture-tests.yml"
TOKEN_ENV = "FIXTURE_DIAG_TOKEN"
SCHEMA = "w5-fixture-token-diagnostic/1"
REQUEST_TIMEOUT = 10.0
GLOBAL_BUDGET = 90.0
MAX_BODY = 1_000_000

_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_\-.=+/]{20,400}")
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_REQUEST_ID = re.compile(r"[A-F0-9:]{8,80}")
_PERMISSIONS_HEADER = re.compile(r"[A-Za-z_=,;| \-]{0,300}")
_DIGITS = re.compile(r"[0-9]{1,12}")
_PATTERN = re.compile(r"[A-Za-z0-9_./*~\-]{1,120}")
_NAME = re.compile(r"[A-Za-z0-9 ()_.\-]{1,80}")
_BRANCH = re.compile(r"[A-Za-z0-9_./\-]{1,120}")
_FULL_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}")
_DOC_PATH = re.compile(r"/[A-Za-z0-9_./#\-]{0,200}")

USER_TYPES = ("User", "Bot", "Organization")
PERMISSIONS = ("admin", "maintain", "write", "triage", "read", "none")
ROLE_NAMES = ("admin", "maintain", "write", "triage", "read")
RATE_RESOURCES = (
    "core",
    "graphql",
    "search",
    "code_search",
    "integration_manifest",
    "source_import",
    "actions_runner_registration",
)
RULE_TYPES = (
    "creation",
    "update",
    "deletion",
    "required_linear_history",
    "required_signatures",
    "pull_request",
    "required_status_checks",
    "non_fast_forward",
    "required_deployments",
    "merge_queue",
    "copilot_code_review",
    "code_scanning",
    "workflows",
    "commit_message_pattern",
    "branch_name_pattern",
)
CHECK_STATUSES = ("queued", "in_progress", "completed", "waiting", "requested", "pending")
CHECK_CONCLUSIONS = (
    "success",
    "failure",
    "neutral",
    "cancelled",
    "skipped",
    "timed_out",
    "action_required",
    "stale",
    "startup_failure",
)
COMMIT_STATES = ("error", "failure", "pending", "success")
RUN_EVENTS = (
    "push",
    "pull_request",
    "pull_request_target",
    "workflow_dispatch",
    "schedule",
    "workflow_run",
    "merge_group",
)
ENFORCEMENT = ("active", "disabled", "evaluate")
TARGETS = ("branch", "tag", "push")
ACTOR_TYPES = ("Integration", "OrganizationAdmin", "RepositoryRole", "Team", "DeployKey")
BYPASS_MODES = ("always", "pull_request", "exempt")
SOURCE_TYPES = ("Repository", "Organization", "Enterprise")
PROTECTION_KEYS = (
    "required_status_checks",
    "required_pull_request_reviews",
    "enforce_admins",
    "restrictions",
    "required_linear_history",
    "allow_force_pushes",
    "allow_deletions",
    "required_conversation_resolution",
    "required_signatures",
    "lock_branch",
)

#: Phrases connues d'erreur GitHub (minuscules), de la plus précise à la plus générale : le message est CLASSÉ, jamais recopié.
KNOWN_MESSAGES: Tuple[Tuple[str, str], ...] = (
    ("resource not accessible by personal access token", "resource_not_accessible_by_personal_access_token"),
    ("resource not accessible by integration", "resource_not_accessible_by_integration"),
    ("secondary rate limit", "secondary_rate_limit"),
    ("api rate limit exceeded", "primary_rate_limit"),
    ("bad credentials", "bad_credentials"),
    ("requires authentication", "requires_authentication"),
    ("branch not protected", "branch_not_protected"),
    ("must have admin rights", "admin_rights_required"),
    ("saml", "saml_enforcement"),
    ("not found", "not_found"),
    ("forbidden", "forbidden"),
)


# ── transport ───────────────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Reply:
    """Réponse brute ou échec réseau CLASSÉ (jamais le texte de l'exception)."""

    status: Optional[int]
    headers: Mapping[str, str]
    body: bytes
    error: Optional[str] = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse toute redirection : le jeton ne part jamais vers une autre destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401, ARG002 - API d'urllib
        return None


def build_opener() -> urllib.request.OpenerDirector:
    """Opener sans proxy (``ProxyHandler({})`` ignore ``HTTP(S)_PROXY``) et sans redirection."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def classify_exception(exc: BaseException) -> str:
    """Nom de CLASSE seulement : le texte d'une exception pourrait contenir l'URL, l'en-tête ou le jeton."""
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        exc = exc.reason
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, ssl.SSLError):
        return "tls_error"
    if isinstance(exc, socket.gaierror):
        return "dns_error"
    if isinstance(exc, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(exc, (ConnectionError, http.client.HTTPException)):
        return "connection_error"
    return "network_error"


def default_transport(url: str, headers: Mapping[str, str], timeout: float) -> Reply:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with build_opener().open(request, timeout=timeout) as response:  # noqa: S310 - hôte revérifié par l'appelant (https://api.github.com)
            return Reply(response.status, {k.lower(): v for k, v in response.headers.items()}, response.read(MAX_BODY))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(MAX_BODY)
        except Exception:  # noqa: BLE001 - corps illisible : on garde le statut
            body = b""
        return Reply(exc.code, {k.lower(): v for k, v in exc.headers.items()}, body)
    except Exception as exc:  # noqa: BLE001 - classé par nom de classe, jamais par message
        return Reply(None, {}, b"", classify_exception(exc))


# ── extraction à liste blanche ───────────────────────────────────────────────────────────────────────────────────────────────


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10**12 else None


def _bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _enum(value: Any, allowed: Sequence[str]) -> Optional[str]:
    return value if isinstance(value, str) and value in allowed else None


def _match(value: Any, pattern: "re.Pattern[str]") -> Optional[str]:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _drop_none(data: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def safe_headers(headers: Mapping[str, str]) -> Dict[str, Any]:
    """En-têtes d'une liste blanche, chacun VALIDÉ par motif (une valeur hors motif est omise, jamais recopiée)."""
    lower = {str(k).lower(): v for k, v in headers.items()}
    out: Dict[str, Any] = {}
    request_id = _match(lower.get("x-github-request-id"), _REQUEST_ID)
    if request_id:
        out["x_github_request_id"] = request_id
    accepted = lower.get("x-accepted-github-permissions")
    if isinstance(accepted, str) and _PERMISSIONS_HEADER.fullmatch(accepted):
        out["x_accepted_github_permissions"] = accepted
    for name in ("limit", "remaining", "reset"):
        value = lower.get(f"x-ratelimit-{name}")
        if isinstance(value, str) and _DIGITS.fullmatch(value):
            out[f"x_ratelimit_{name}"] = int(value)
    resource = _enum(lower.get("x-ratelimit-resource"), RATE_RESOURCES)
    if resource:
        out["x_ratelimit_resource"] = resource
    elif isinstance(lower.get("x-ratelimit-resource"), str):
        out["x_ratelimit_resource"] = "other"
    retry = lower.get("retry-after")
    if isinstance(retry, str) and _DIGITS.fullmatch(retry):
        out["retry_after"] = int(retry)
    return out


def docs_url(value: Any) -> Optional[str]:
    """``documentation_url`` reconstruite si, et seulement si, l'hôte est exactement ``docs.github.com`` ; ``None`` sinon.

    ``urlsplit`` et ``.port`` lèvent ``ValueError`` (IPv6 mal formée, port non numérique ou hors plage) avec un texte qui CONTIENT l'entrée :
    l'exception est absorbée ici et son texte n'est jamais lu.
    """
    if not isinstance(value, str) or len(value) > 500:
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "docs.github.com"
            or parsed.port is not None
            or parsed.username
        ):
            return None
        path = parsed.path + (f"#{parsed.fragment}" if parsed.fragment else "")
    except ValueError:
        return None
    return f"https://docs.github.com{path}" if _DOC_PATH.fullmatch(path) else None


def classify_message(payload: Any, *, with_documentation_url: bool = True) -> Dict[str, Any]:
    """Classe le message d'erreur parmi des phrases connues ; ``documentation_url`` seulement si l'hôte est ``docs.github.com``."""
    if not isinstance(payload, dict):
        return {}
    out: Dict[str, Any] = {}
    message = payload.get("message")
    if isinstance(message, str):
        lowered = message.lower()
        out["message_class"] = next((label for phrase, label in KNOWN_MESSAGES if phrase in lowered), "unclassified")
    if with_documentation_url and "documentation_url" in payload:
        url = docs_url(payload.get("documentation_url"))
        if url:
            out["documentation_url"] = url
        else:
            out["documentation_url_class"] = "rejected"  # absente de confiance : ni texte ni hôte recopiés
    return out


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]


def extract_user(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    return _drop_none(
        {
            "login": _match(data.get("login"), _LOGIN),
            "id": _int(data.get("id")),
            "type": _enum(data.get("type"), USER_TYPES),
        }
    )


def extract_repository(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    permissions = data.get("permissions")
    typed = (
        _drop_none({k: _bool(permissions.get(k)) for k in ("admin", "maintain", "push", "triage", "pull")})
        if isinstance(permissions, dict)
        else {}
    )
    full_name = _match(data.get("full_name"), _FULL_NAME)
    return _drop_none(
        {
            "id": _int(data.get("id")),
            "id_matches_expected": _int(data.get("id")) == REPOSITORY_ID if _int(data.get("id")) is not None else None,
            "full_name": full_name,
            "full_name_matches_expected": full_name == REPOSITORY if full_name else None,
            "private": _bool(data.get("private")),
            "archived": _bool(data.get("archived")),
            "default_branch": _match(data.get("default_branch"), _BRANCH),
            "permissions": typed or None,
        }
    )


def extract_collaborator(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    return _drop_none(
        {
            "permission": _enum(data.get("permission"), PERMISSIONS),
            "role_name": _enum(data.get("role_name"), ROLE_NAMES),
        }
    )


def extract_protection(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    checks = data.get("required_status_checks")
    enforce = data.get("enforce_admins")
    return _drop_none(
        {
            "protections_present": sorted(key for key in data if key in PROTECTION_KEYS) or None,
            "enforce_admins": _bool(enforce.get("enabled")) if isinstance(enforce, dict) else None,
            "required_checks_strict": _bool(checks.get("strict")) if isinstance(checks, dict) else None,
        }
    )


def _rule_params(rule: Mapping[str, Any]) -> Dict[str, Any]:
    params = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
    kind = rule.get("type")
    if kind == "required_status_checks":
        checks = params.get("required_status_checks")
        return _drop_none(
            {
                "strict": _bool(params.get("strict_required_status_checks_policy")),
                "do_not_enforce_on_create": _bool(params.get("do_not_enforce_on_create")),
                "required_checks_count": len(checks) if isinstance(checks, list) else None,
            }
        )
    if kind == "pull_request":
        return _drop_none(
            {
                "required_approving_review_count": _int(params.get("required_approving_review_count")),
                "require_code_owner_review": _bool(params.get("require_code_owner_review")),
            }
        )
    return {}


def extract_rules(data: Any) -> Dict[str, Any]:
    """Règles actives d'une branche (``GET /rules/branches/...``) : types fermés + paramètres typés ; le reste est compté, jamais recopié."""
    if not isinstance(data, list):
        return {}
    rules, others = [], 0
    for item in data[:50]:
        if not isinstance(item, dict):
            others += 1
            continue
        kind = _enum(item.get("type"), RULE_TYPES)
        if kind is None:
            others += 1
            continue
        rules.append(_drop_none({"type": kind, "ruleset_id": _int(item.get("ruleset_id")), **_rule_params(item)}))
    return {"rules": rules, "other_rule_count": others}


def extract_ruleset(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    conditions = data.get("conditions") if isinstance(data.get("conditions"), dict) else {}
    ref_name = conditions.get("ref_name") if isinstance(conditions.get("ref_name"), dict) else {}

    def patterns(key: str) -> Optional[List[str]]:
        values = ref_name.get(key)
        if not isinstance(values, list):
            return None
        return [v for v in values[:5] if _match(v, _PATTERN)]

    bypass = data.get("bypass_actors")
    bypass_typed = (
        [
            _drop_none(
                {
                    "actor_type": _enum(b.get("actor_type"), ACTOR_TYPES),
                    "bypass_mode": _enum(b.get("bypass_mode"), BYPASS_MODES),
                }
            )
            for b in bypass[:5]
            if isinstance(b, dict)
        ]
        if isinstance(bypass, list)
        else None
    )
    rules = data.get("rules") if isinstance(data.get("rules"), list) else []
    typed_rules = extract_rules(rules)
    return _drop_none(
        {
            "id": _int(data.get("id")),
            "name": _match(data.get("name"), _NAME),
            "target": _enum(data.get("target"), TARGETS),
            "enforcement": _enum(data.get("enforcement"), ENFORCEMENT),
            "source_type": _enum(data.get("source_type"), SOURCE_TYPES),
            "ref_name_include": patterns("include"),
            "ref_name_exclude": patterns("exclude"),
            "bypass_actor_count": len(bypass) if isinstance(bypass, list) else None,
            "bypass_actors": bypass_typed,
            "rules": typed_rules.get("rules"),
            "other_rule_count": typed_rules.get("other_rule_count"),
            "configuration_fingerprint": _fingerprint(
                {"rules": rules, "conditions": conditions, "enforcement": data.get("enforcement"), "bypass": bypass}
            ),
        }
    )


def extract_rate_limit(data: Any) -> Dict[str, Any]:
    resources = data.get("resources") if isinstance(data, dict) and isinstance(data.get("resources"), dict) else {}
    out: Dict[str, Any] = {}
    for name in RATE_RESOURCES:
        item = resources.get(name)
        if isinstance(item, dict):
            typed = _drop_none({k: _int(item.get(k)) for k in ("limit", "remaining", "reset")})
            if typed:
                out[name] = typed
    return {"resources": out} if out else {}


def _sha_is_expected(value: Any) -> Optional[bool]:
    return value == BOOTSTRAP_COMMIT if isinstance(value, str) else None


def _nested_id(data: Mapping[str, Any], key: str) -> Optional[int]:
    item = data.get(key)
    return _int(item.get("id")) if isinstance(item, dict) else None


def extract_check_runs(data: Any) -> Dict[str, Any]:
    """Checks d'un commit : compteurs, énumérations fermées, ``app_id`` numérique, SHA attendu en booléen (aucun nom libre)."""
    if not isinstance(data, dict):
        return {}
    items = data.get("check_runs") if isinstance(data.get("check_runs"), list) else None
    runs = (
        [
            _drop_none(
                {
                    "id": _int(item.get("id")),
                    "status": _enum(item.get("status"), CHECK_STATUSES),
                    "conclusion": _enum(item.get("conclusion"), CHECK_CONCLUSIONS),
                    "app_id": _nested_id(item, "app"),
                    "app_id_is_expected": _nested_id(item, "app") == CHECKS_APP_ID
                    if _nested_id(item, "app") is not None
                    else None,
                    "head_sha_is_expected": _sha_is_expected(item.get("head_sha")),
                }
            )
            for item in items[:20]
            if isinstance(item, dict)
        ]
        if items is not None
        else None
    )
    return _drop_none(
        {
            "total_count": _int(data.get("total_count")),
            "listed_count": len(items) if items is not None else None,
            "check_runs": runs,
        }
    )


def extract_commit_statuses(data: Any) -> Dict[str, Any]:
    """Statuts commit (liste) : longueur et états de l'énumération fermée seulement."""
    if not isinstance(data, list):
        return {}
    states = [_enum(item.get("state"), COMMIT_STATES) for item in data[:20] if isinstance(item, dict)]
    return {"count": len(data), "states": [state for state in states if state]}


def extract_workflow_job(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    run_id = _int(data.get("run_id"))
    return _drop_none(
        {
            "id": _int(data.get("id")),
            "run_id": run_id,
            "run_id_is_expected": run_id == WORKFLOW_RUN_ID if run_id is not None else None,
            "status": _enum(data.get("status"), CHECK_STATUSES),
            "conclusion": _enum(data.get("conclusion"), CHECK_CONCLUSIONS),
            "head_sha_is_expected": _sha_is_expected(data.get("head_sha")),
        }
    )


def extract_workflow_run(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    path = data.get("path")
    repository_id = _nested_id(data, "repository")
    return _drop_none(
        {
            "id": _int(data.get("id")),
            "run_attempt": _int(data.get("run_attempt")),
            "status": _enum(data.get("status"), CHECK_STATUSES),
            "conclusion": _enum(data.get("conclusion"), CHECK_CONCLUSIONS),
            "event": _enum(data.get("event"), RUN_EVENTS),
            "head_sha_is_expected": _sha_is_expected(data.get("head_sha")),
            "path_is_expected": path == WORKFLOW_PATH if isinstance(path, str) else None,
            "repository_id": repository_id,
            "repository_id_is_expected": repository_id == REPOSITORY_ID if repository_id is not None else None,
        }
    )


# ── plan ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Endpoint:
    key: str
    path: str
    extractor: Callable[[Any], Dict[str, Any]]
    expected_404: bool = False  # protection classique d'un dépôt protégé par rulesets


def plan(login: Optional[str]) -> List[Endpoint]:
    """Liste FERMÉE des lectures (15 avec login valide, 14 sinon). L'entrée collaborateur n'existe que si ``login`` est un login valide (renvoyé par ``/user``)."""
    repo = f"/repos/{REPOSITORY}"
    steps = [
        Endpoint("user", "/user", extract_user),
        Endpoint("repository", repo, extract_repository),
    ]
    if login is not None and _LOGIN.fullmatch(login):
        steps.append(
            Endpoint("collaborator_permission", f"{repo}/collaborators/{login}/permission", extract_collaborator)
        )
    steps += [
        Endpoint("protection_main", f"{repo}/branches/main/protection", extract_protection, expected_404=True),
        Endpoint(
            "protection_bootstrap_v2",
            f"{repo}/branches/{BOOTSTRAP_BRANCH}/protection",
            extract_protection,
            expected_404=True,
        ),
        Endpoint(
            "protection_absent_branch",
            f"{repo}/branches/{ABSENT_BRANCH}/protection",
            extract_protection,
            expected_404=True,
        ),
        Endpoint("rules_bootstrap_v2", f"{repo}/rules/branches/{BOOTSTRAP_BRANCH}", extract_rules),
        Endpoint("rules_absent_branch", f"{repo}/rules/branches/{ABSENT_BRANCH}", extract_rules),
    ]
    steps += [Endpoint(f"ruleset_{rid}", f"{repo}/rulesets/{rid}", extract_ruleset) for rid in RULESET_IDS]
    steps += [
        Endpoint("commit_check_runs", f"{repo}/commits/{BOOTSTRAP_COMMIT}/check-runs", extract_check_runs),
        Endpoint("commit_statuses", f"{repo}/commits/{BOOTSTRAP_COMMIT}/statuses", extract_commit_statuses),
        Endpoint("workflow_job", f"{repo}/actions/jobs/{WORKFLOW_JOB_ID}", extract_workflow_job),
        Endpoint("workflow_run", f"{repo}/actions/runs/{WORKFLOW_RUN_ID}", extract_workflow_run),
    ]
    steps.append(Endpoint("rate_limit", "/rate_limit", extract_rate_limit))
    return steps


def build_url(path: str) -> str:
    """URL de l'API officielle UNIQUEMENT : chemin absolu sans ``//``, ``..``, ``@``, ``?``, ``#`` ni caractère hors liste."""
    if not re.fullmatch(r"/[A-Za-z0-9_./\-]{1,300}", path) or "//" in path or ".." in path:
        raise ValueError("chemin hors de la liste fermée")
    url = API_BASE + path
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != API_HOST
        or parsed.port is not None
        or parsed.username
        or parsed.query
    ):
        raise ValueError("destination hors de https://api.github.com")
    return url


# ── exécution ───────────────────────────────────────────────────────────────────────────────────────────────────────────────


def payload_contains_token(payload: Any, token: str) -> bool:
    """Cherche le jeton dans les DONNÉES DÉCODÉES (clés et chaînes) : un corps échappé en ``\\uXXXX`` échappe au filtre sur les octets bruts.

    Parcours itératif borné ; un document trop gros pour être balayé est traité comme suspect (jamais extrait).
    """
    stack, seen = [payload], 0
    while stack:
        item = stack.pop()
        seen += 1
        if seen > 200_000:
            return True
        if isinstance(item, str):
            if contains_token(item, token):
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def rate_limit_state(status: int, remaining: Optional[int], message_class: Optional[str]) -> Optional[str]:
    """État de limite de débit d'une réponse 403/429 ; ``None`` pour les autres statuts.

    ``remaining == 0`` = quota primaire épuisé ; ``4979`` = non épuisé ; absent ou invalide = ``unknown`` (jamais présenté comme « non épuisé »).
    """
    if status not in (403, 429):
        return None
    if remaining == 0:
        return "primary_exhausted"
    if message_class == "secondary_rate_limit":
        return "secondary"
    if message_class == "primary_rate_limit":
        return "primary_message"
    if status == 429:
        return "too_many_requests"
    return "unknown" if remaining is None else "not_exhausted"


RATE_LIMIT_ESTABLISHED = ("primary_exhausted", "secondary", "primary_message", "too_many_requests")


def probe_endpoint(
    endpoint: Endpoint, token: str, transport: Callable[[str, Mapping[str, str], float], Reply]
) -> Dict[str, Any]:
    url = build_url(endpoint.path)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "collegue-w5-fixture-token-diagnostic",
    }
    entry: Dict[str, Any] = {"key": endpoint.key, "method": "GET", "path": endpoint.path, "status": None}
    try:
        reply = transport(url, headers, REQUEST_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - classé par nom de classe, jamais par message
        entry.update(outcome="network_failure", error_class=classify_exception(exc))
        return entry
    entry["status"] = reply.status
    if reply.error:
        entry.update(outcome="network_failure", error_class=reply.error)
        return entry
    if any(contains_token(f"{key}: {value}", token) for key, value in reply.headers.items()):
        entry.update(outcome="failure", token_echo_detected=True)  # en-têtes suspects : rien n'en est extrait
        return entry
    entry.update(safe_headers(reply.headers))
    try:
        return _interpret(entry, endpoint, reply, token)
    except Exception as exc:  # noqa: BLE001 - une réponse mal formée ne doit ni lever, ni empêcher les autres lectures
        for key in ("data", "message_class", "documentation_url", "documentation_url_class", "body_class"):
            entry.pop(key, None)
        entry.update(outcome="failure", extraction_error=type(exc).__name__)  # nom de classe seulement, jamais le texte
        return entry


def _interpret(entry: Dict[str, Any], endpoint: Endpoint, reply: Reply, token: str) -> Dict[str, Any]:
    raw = reply.body.decode("utf-8", "replace")
    echoed = contains_token(raw, token)
    payload: Any = None
    try:
        payload = json.loads(raw) if reply.body else None
    except ValueError:
        entry["body_class"] = "not_json"
    if not echoed and payload_contains_token(payload, token):
        echoed = True  # le jeton n'apparaît qu'après décodage (échappements \\uXXXX)
    status = int(reply.status or 0)
    if (
        echoed
    ):  # corps qui renvoie le jeton : aucune donnée ni URL extraite, seule la classe du message (étiquette fixe)
        entry["token_echo_detected"] = True
        payload_view = classify_message(payload, with_documentation_url=False) if isinstance(payload, dict) else {}
        entry.update(outcome="failure", **payload_view)
    elif 200 <= status < 300:
        entry["outcome"] = "ok"
        entry["data"] = endpoint.extractor(payload)
    elif status == 404 and endpoint.expected_404:
        entry["outcome"] = "expected_not_found"
        entry.update(classify_message(payload))
    elif 300 <= status < 400:
        entry["outcome"] = "redirect_refused"  # ni Location ni corps : jamais suivie
    else:
        entry["outcome"] = "failure"
        entry.update(classify_message(payload))
    state = rate_limit_state(status, entry.get("x_ratelimit_remaining"), entry.get("message_class"))
    if state is not None:
        entry["rate_limit_state"] = state
        if isinstance(entry.get("x_ratelimit_remaining"), int):
            entry["primary_rate_limit_exhausted"] = entry["x_ratelimit_remaining"] == 0
    return entry


def token_forms(token: str) -> List[str]:
    """Formes usuelles sous lesquelles le jeton pourrait fuir dans un rapport."""
    raw = token.encode("utf-8")
    forms = {token, urllib.parse.quote(token, safe=""), urllib.parse.quote_plus(token), json.dumps(token)[1:-1]}
    for prefix in (b"", b"x-access-token:", b"Bearer ", b"token ", b"Basic "):
        encoded = prefix + raw
        forms.add(base64.b64encode(encoded).decode("ascii"))
        forms.add(base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("="))
        forms.add(base64.b64encode(encoded).decode("ascii").rstrip("="))
    return [form for form in forms if form]


def contains_token(text: str, token: str) -> bool:
    return any(form in text for form in token_forms(token))


def skipped(endpoint: Endpoint, outcome: str) -> Dict[str, Any]:
    return {"key": endpoint.key, "method": "GET", "path": endpoint.path, "status": None, "outcome": outcome}


def run(
    token: str,
    transport: Callable[[str, Mapping[str, str], float], Reply] = default_transport,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    started = clock()
    entries: List[Dict[str, Any]] = []
    login: Optional[str] = None
    stopped_after: Optional[str] = None
    # « /user » d'abord : son login valide débloque la lecture des permissions du collaborateur
    pending = plan(None)[:1]
    done = 0
    while done < len(pending) and stopped_after is None:
        endpoint = pending[done]
        done += 1
        if clock() - started >= GLOBAL_BUDGET:
            entries.append(skipped(endpoint, "skipped_global_deadline"))
            continue
        entries.append(probe_endpoint(endpoint, token, transport))
        if entries[-1].get("rate_limit_state") in RATE_LIMIT_ESTABLISHED:
            stopped_after = endpoint.key  # limite de débit établie : plus aucune émission, ni retry ni attente
        if endpoint.key == "user":
            candidate = (entries[-1].get("data") or {}).get("login")
            login = candidate if isinstance(candidate, str) and _LOGIN.fullmatch(candidate) else None
            pending = pending + plan(login)[1:]
    if stopped_after is not None:
        executed = {e["key"] for e in entries}
        entries += [skipped(e, "not_executed_rate_limited") for e in plan(login) if e.key not in executed]
    failures = [e["key"] for e in entries if e["outcome"] not in ("ok", "expected_not_found")]
    return {
        "schema": SCHEMA,
        "purpose": "diagnostic des droits du jeton de la fixture SEULEMENT : ne prouve ni le garde produit ni la campagne",
        "repository": REPOSITORY,
        "repository_id_expected": REPOSITORY_ID,
        "login": login,
        "endpoints": entries,
        "failures": failures,
        "stopped_after_rate_limit": stopped_after,
        "verdict": "all_reads_answered" if not failures else "failures",
    }


def summary_lines(report: Mapping[str, Any]) -> List[str]:
    """Lignes de journal DÉRIVÉES du rapport filtré (jamais d'une réponse brute)."""
    lines = []
    for entry in report["endpoints"]:
        extra = [
            f"{key}={entry[key]}"
            for key in (
                "message_class",
                "error_class",
                "x_ratelimit_remaining",
                "primary_rate_limit_exhausted",
                "rate_limit_state",
                "token_echo_detected",
                "extraction_error",
                "x_accepted_github_permissions",
            )
            if key in entry
        ]
        lines.append(f"{entry['key']}: GET {entry['path']} -> {entry['status']} {entry['outcome']} " + " ".join(extra))
    if report.get("stopped_after_rate_limit"):
        lines.append(
            f"arrêt après limite de débit établie sur {report['stopped_after_rate_limit']} : lectures restantes non exécutées"
        )
    lines.append(
        f"verdict: {report['verdict']} (échecs : {report['failures'] or 'aucun'}) — diagnostic des droits seulement"
    )
    return lines


def finalize(report: Mapping[str, Any], token: str) -> Tuple[Optional[str], List[str]]:
    """``(JSON du rapport, lignes de journal)`` ou ``(None, [])`` si le jeton figure, sous une forme usuelle, dans l'une ou l'autre sortie."""
    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    lines = summary_lines(report)
    if contains_token(text, token) or any(contains_token(line, token) for line in lines):
        return None, []
    return text, lines


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    transport: Callable[[str, Mapping[str, str], float], Reply] = default_transport,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    parser = argparse.ArgumentParser(description="Sonde minimale des droits du jeton de la fixture (lecture seule).")
    parser.add_argument("--output", type=Path, required=True, help="rapport JSON filtré (liste blanche)")
    args = parser.parse_args(argv)
    token = (os.environ if environ is None else environ).get(TOKEN_ENV, "")
    if not token or not _TOKEN_SHAPE.fullmatch(token):
        print("fixture_token_diagnostic: jeton absent ou malformé : aucune requête émise (code 2)", file=sys.stderr)
        return 2
    try:
        report = run(token, transport, clock)
        text, lines = finalize(report, token)
    except Exception as exc:  # noqa: BLE001 - jamais de traceback brut : le texte d'une exception peut contenir une entrée hostile
        print(
            f"fixture_token_diagnostic: erreur interne ({type(exc).__name__}) : aucun rapport écrit (code 2)",
            file=sys.stderr,
        )
        return 2
    if text is None:
        print(
            "fixture_token_diagnostic: le rapport contenait le jeton sous une forme usuelle : REFUSÉ, rien n'est écrit (code 2)",
            file=sys.stderr,
        )
        return 2
    args.output.write_text(text, encoding="utf-8")
    for line in lines:
        print(line)
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    sys.exit(main())
