#!/usr/bin/env python3
"""CLI de la paire JSON W5 : ``preflight`` → ``claim`` → ``run`` → ``finalize`` (propriété A ; le workflow de C les appelle).

Contrat arbitré par le manager (``briefs/w5-json-pair-cli-ruling.md``) : arguments communs ``--root`` et ``--campaign-id`` ;
le contexte GitHub RÉEL est lu dans l'environnement à chaque étape et comparé à la preuve (aucun argument ne permet de le
fabriquer) ; seuls ``--expected-sha`` et ``--confirm`` sont des affirmations du déclencheur, contrôlées. Aucune étape ne reçoit
plus d'un secret : ``preflight`` et ``finalize`` aucun, ``claim`` le seul jeton de la fixture, ``run`` la seule clé ``LLM_API_KEY``.

Sorties : une ligne fermée sur stdout, jamais de corps, de clé ni de texte d'exception. Codes : 0 observations requises obtenues,
1 échec observé, 3 incomplet ou refus (2 est réservé aux erreurs d'arguments d'argparse).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # exécution directe : ``python scripts/w5_json_pair.py``
    sys.path.insert(0, str(ROOT))

from collegue.broker import json_pair  # noqa: E402
from collegue.broker.json_pair import (  # noqa: E402
    CONFIRMATION,
    ENVELOPE,
    EXIT_FAILED,
    EXIT_INCOMPLETE,
    EXIT_OBSERVED,
    MODEL,
    REQUEST_SHA256,
    VARIANT_NAMES,
)

__all__ = [
    "CONFIRMATION",
    "ENVELOPE",
    "EXIT_FAILED",
    "EXIT_INCOMPLETE",
    "EXIT_OBSERVED",
    "FILES",
    "MODEL",
    "REQUEST_SHA256",
    "VARIANT_NAMES",
    "main",
]

PREFLIGHT_SCHEMA = "w5-json-pair-preflight/1"
CLAIM_SCHEMA = "w5-json-pair-claim/1"
PROVENANCE_SCHEMA = "w5-json-pair-provenance/1"
REPOSITORY = "vynodepal/collegue"  # comparé sans casse à ``GITHUB_REPOSITORY``
REQUIRED_REF = "refs/heads/main"
REQUIRED_EVENT = "workflow_dispatch"
SHA = re.compile(r"[0-9a-f]{40}")
KEY_ENV = "LLM_API_KEY"
TOKEN_ENV = "GITHUB_TOKEN"
MIN_SECRET_LENGTH = 8
#: Un nom « porte un secret » dès qu'un de ses segments est une clé d'API, un secret, un mot de passe, un jeton ou un PAT :
#: clés de fournisseur, clés par rôle, jetons GitHub ET tous leurs alias. Les métadonnées normales du runner (``GITHUB_RUN_ID``,
#: ``GITHUB_SHA``, ``RUNNER_*``, ``MAX_TOKENS``…) n'ont aucun de ces segments ; les jetons internes d'Actions sont énumérés.
SECRET_NAME = re.compile(r"(.*_)?(API_KEY|APIKEY|SECRET|PASSWORD|PRIVATE_KEY|TOKEN|PAT)(_.*)?")
RUNNER_INTERNAL = frozenset({"ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL"})
TOKEN_ALIAS = re.compile(r"(.*_)?(GITHUB|GH|FIXTURE|PERSONAL_ACCESS)(_.*)?(TOKEN|PAT)(_.*)?")
FALSE_VALUE = "false"  # seule preuve d'un nightly inactif
ACTIVE_VALUES = frozenset({"1", "true", "yes", "on"})
GITHUB_ENV = (
    "GITHUB_EVENT_NAME",
    "GITHUB_REF",
    "GITHUB_SHA",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_REPOSITORY",
)
HTTP_TIMEOUT_SECONDS = 180.0

#: Disposition sous ``--root`` (le workflow crée ``proof/ home/ report/`` vides ; le CLI les crée sinon, sans jamais rien supprimer).
FILES = {
    "preflight": "proof/preflight.json",
    "claim": "proof/claim.json",
    "database": "home/{campaign_id}.sqlite3",
    "report_json": "report/json-pair.json",
    "report_text": "report/json-pair.txt",
    "provenance": "report/provenance.json",
}


class Refused(Exception):
    """Refus d'une étape : ``code`` fermé (jamais de texte libre)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ── utilitaires ───────────────────────────────────────────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(data: Mapping[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sealed(data: Dict[str, Any]) -> Dict[str, Any]:
    body = {key: value for key, value in data.items() if key != "proof_sha256"}
    return {**body, "proof_sha256": hashlib.sha256(_canonical(body)).hexdigest()}


def _file_sha256(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _path(root: Path, key: str, campaign_id: str = "") -> Path:
    return root / FILES[key].format(campaign_id=campaign_id)


def _write(path: Path, text: str) -> None:
    """Écriture atomique (fichier frère puis renommage) en 0600 ; aucun fichier partiel ne reste."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    _write(path, json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n")


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _github_context(env: Mapping[str, str]) -> Dict[str, Any]:
    """Contexte du runner, lu dans l'environnement (jamais dans un argument)."""
    run_id = str(env.get("GITHUB_RUN_ID", "") or "")
    return {
        "event": str(env.get("GITHUB_EVENT_NAME", "") or ""),
        "ref": str(env.get("GITHUB_REF", "") or ""),
        "sha": str(env.get("GITHUB_SHA", "") or ""),
        "run_id": int(run_id) if run_id.isdigit() else None,
        "run_attempt": str(env.get("GITHUB_RUN_ATTEMPT", "") or ""),
        "repository": str(env.get("GITHUB_REPOSITORY", "") or ""),
    }


def _check_context(env: Mapping[str, str], expected_sha: Optional[str]) -> Dict[str, Any]:
    context = _github_context(env)
    if context["event"] != REQUIRED_EVENT:
        raise Refused("bad_event")
    if context["ref"] != REQUIRED_REF:
        raise Refused("bad_ref")
    if context["repository"].lower() != REPOSITORY:
        raise Refused("bad_repository")
    if not SHA.fullmatch(context["sha"]) or (expected_sha is not None and context["sha"] != expected_sha):
        raise Refused("sha_mismatch")
    if context["run_attempt"] != "1":
        raise Refused("run_attempt_not_one")
    if context["run_id"] is None:
        raise Refused("bad_run_id")
    _check_nightly(env)
    return context


def _forbid_secrets(env: Mapping[str, str], *, allow: Sequence[str] = ()) -> None:
    """Chaque étape n'admet que son propre secret (``allow``) : toute autre clé, tout jeton et tout alias non vide est refusé."""
    for name, value in env.items():
        if not value or name in allow or name in RUNNER_INTERNAL or not SECRET_NAME.fullmatch(name):
            continue
        if name == KEY_ENV:
            raise Refused("key_present")
        if TOKEN_ALIAS.fullmatch(name):
            raise Refused("fixture_token_present")
        raise Refused("forbidden_env")


def _check_nightly(env: Mapping[str, str]) -> None:
    """Le nightly est inactif SEULEMENT si la valeur est explicitement ``false`` : absent, vide ou ambigu ne prouve rien."""
    value = str(env.get("INTEGRATION_E2E_ENABLED", "") or "")
    if value.strip().lower() in ACTIVE_VALUES:
        raise Refused("nightly_active")
    if value != FALSE_VALUE:
        raise Refused("nightly_unestablished")


def _layout(root: Path, campaign_id: str) -> None:
    if not json_pair.CAMPAIGN_ID.fullmatch(campaign_id or ""):
        raise Refused("invalid_campaign_id")
    for name in ("proof", "home", "report"):
        (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)


# ── preuves ───────────────────────────────────────────────────────────────────────────────────────────────────────────


def _load_proof(
    root: Path,
    key: str,
    schema: str,
    campaign_id: str,
    env: Mapping[str, str],
    preflight: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Relit une preuve : schéma, scellé, campagne ET contexte réel du runner doivent correspondre à ce qu'elle a lié.

    Une preuve de revendication doit de plus viser la référence EXACTE de la campagne, la vraie fixture et le préflight utilisé."""
    data = _read_json(_path(root, key, campaign_id))
    if data is None:
        raise Refused(f"{key}_proof_missing")
    if data.get("schema") != schema or data.get("proof_sha256") != _sealed(data)["proof_sha256"]:
        raise Refused(f"{key}_proof_invalid")
    if data.get("campaign_id") != campaign_id:
        raise Refused(f"{key}_proof_campaign")
    github = data.get("github")
    current = _github_context(env)
    if key == "preflight":
        if data.get("status") != "ready" or github != current or data.get("expected_sha") != current["sha"]:
            raise Refused("preflight_proof_context")
        return data
    from collegue.pilot.w4_business import FIXTURE_REPOSITORY
    from collegue.pilot.w5_business import claim_ref

    if data.get("created") is not True or data.get("durable") is not True:
        raise Refused("claim_proof_not_created")
    if github != current or any(data.get(name) != current[name] for name in ("run_id", "run_attempt", "sha")):
        raise Refused("claim_proof_context")
    if data.get("repository") != current["repository"]:
        raise Refused("claim_proof_context")
    if data.get("ref") != claim_ref(campaign_id) or data.get("fixture_repository") != FIXTURE_REPOSITORY:
        raise Refused("claim_proof_target")
    if preflight is None or data.get("preflight_proof_sha256") != preflight.get("proof_sha256"):
        raise Refused("claim_proof_preflight")
    return data


def cmd_preflight(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    root = Path(args.root)
    _layout(root, args.campaign_id)
    _forbid_secrets(env)
    if not SHA.fullmatch(args.expected_sha or ""):
        raise Refused("bad_expected_sha")
    context = _check_context(env, args.expected_sha)
    if args.confirm != CONFIRMATION:
        raise Refused("bad_confirmation")
    if json_pair.request_fingerprints() != REQUEST_SHA256 or json_pair.MODEL != "gemma-4-31b-it":
        raise Refused("payload_drift")
    first, second = json_pair.payloads()
    native = dict(second)
    if native.pop("response_format", None) != {"type": "json_object"} or native != first:
        raise Refused("payload_drift")
    if set(first) != {"model", "max_tokens", "messages"} or first["max_tokens"] != 256:
        raise Refused("payload_drift")
    database = _path(root, "database", args.campaign_id)
    if (
        database.exists()
        and json_pair.read_report(database=database, campaign_id=args.campaign_id)["reason"] != "not_started"
    ):
        raise Refused("registry_not_fresh")
    proof = _sealed(
        {
            "schema": PREFLIGHT_SCHEMA,
            "status": "ready",
            "campaign_id": args.campaign_id,
            "github": context,
            "expected_sha": args.expected_sha,
            "model": MODEL,
            "variants": list(VARIANT_NAMES),
            "request_sha256": list(REQUEST_SHA256),
            "envelope": dict(ENVELOPE),
            "checks": [
                "context",
                "expected_sha",
                "run_attempt",
                "nightly_inactive",
                "no_secret",
                "confirmation",
                "payloads",
                "fresh_registry",
            ],
            "created_utc": _now(),
        }
    )
    _write_json(_path(root, "preflight"), proof)
    print("preflight: ok")
    return EXIT_OBSERVED


def _clients(token: str) -> Any:
    from collegue.pilot.w4_business import _fixture_clients

    return _fixture_clients(token)


def cmd_claim(
    args: argparse.Namespace, env: Mapping[str, str], clients_factory: Optional[Callable[[str], Any]] = None
) -> int:
    root = Path(args.root)
    _layout(root, args.campaign_id)
    _forbid_secrets(env, allow=(TOKEN_ENV,))
    token = str(env.get(TOKEN_ENV, "") or "")
    if len(token) < MIN_SECRET_LENGTH:
        raise Refused("fixture_token_missing")
    context = _check_context(env, None)
    preflight = _load_proof(root, "preflight", PREFLIGHT_SCHEMA, args.campaign_id, env)
    from collegue.pilot.w4_business import FIXTURE_REPOSITORY, CampaignReport, IncompleteValidation
    from collegue.pilot.w5_business import claim_campaign_identity, claim_ref

    ref = claim_ref(args.campaign_id)
    report = CampaignReport("json-pair-claim", args.campaign_id, secrets=(token,))
    try:
        # Création EXCLUSIVE ; une erreur GitHub n'est jamais lue comme « libre » ; la référence n'est jamais supprimée.
        claim_campaign_identity((clients_factory or _clients)(token), args.campaign_id, report)
    except IncompleteValidation as exc:
        raise Refused("claim_exists" if "consommé" in str(exc) else "claim_unavailable") from None
    except Exception:  # noqa: BLE001 - toute autre panne est un refus de lancer, sans texte libre
        raise Refused("claim_unavailable") from None
    proof = _sealed(
        {
            "schema": CLAIM_SCHEMA,
            "campaign_id": args.campaign_id,
            "ref": ref,
            "fixture_repository": FIXTURE_REPOSITORY,
            "github": context,
            "repository": context["repository"],
            "run_id": context["run_id"],
            "run_attempt": context["run_attempt"],
            "sha": context["sha"],
            "created": True,
            "durable": True,
            "preflight_proof_sha256": preflight["proof_sha256"],
            "created_utc": _now(),
        }
    )
    _write_json(_path(root, "claim"), proof)
    print("claim: ok")
    return EXIT_OBSERVED


# ── rapports ──────────────────────────────────────────────────────────────────────────────────────────────────────────

_STATE_LABELS = {
    "not_started": "non démarrée",
    "in_progress": "en cours",
    "observed": "observée (JSON valide)",
    "parsing_failed": "réponse reçue, JSON invalide ou vide",
    "rejected": "refus HTTP explicite",
    "count_failed": "comptage en échec",
    "refused": "refus local ou émission non partie",
    "unknown": "issue inconnue (usage conservé comme inconnu)",
    "bound_violation": "borne démentie",
    "interrupted": "interrompue",
    "skipped": "non jouée",
}


def render_text(report: Mapping[str, Any]) -> str:
    """Rendu humain dérivé UNIQUEMENT du dict machine (codes fermés) ; ne conclut jamais sur la qualification métier."""
    lines = [
        f"Paire JSON — campagne {report['campaign_id']}",
        f"statut : {report['status']} (raison : {report['reason']})",
    ]
    for variant in report["variants"]:
        valid = {True: "oui", False: "non", None: "n/a"}[variant["json_valid"]]
        lines.append(
            f"- {variant['name']} : {_STATE_LABELS.get(variant['state'], variant['state'])}"
            f" ; json_valid={valid} ; finish_reason={variant['finish_reason'] or 'n/a'}"
        )
    budget = report["budget"]
    lines.append(
        "budget (registre) : consommé={consumed_tokens} inconnu={unknown_tokens} réservé={reserved_tokens} "
        "bloqué={blocked} échéance={deadline_at}".format(**budget)
    )
    emissions = report["emissions"]
    lines.append(
        "émissions ({basis}) : admises={admitted} réglées={settled} libérées={released} inconnues={unknown}".format(
            **emissions
        )
    )
    lines.append("validation métier : non exécutée. Cette paire n'établit aucune qualification de Gemma 4 hébergé.")
    return "\n".join(lines) + "\n"


def _write_reports(root: Path, campaign_id: str, report: Mapping[str, Any], env: Mapping[str, str]) -> None:
    database = _path(root, "database", campaign_id)
    _write_json(_path(root, "report_json"), report)
    _write(_path(root, "report_text"), render_text(report))
    preflight = _read_json(_path(root, "preflight")) or {}
    claim = _read_json(_path(root, "claim")) or {}
    provenance = {
        "schema": PROVENANCE_SCHEMA,
        "campaign_id": campaign_id,
        "github": _github_context(env),
        "expected_sha": preflight.get("expected_sha"),
        "model": MODEL,
        "request_sha256": list(REQUEST_SHA256),
        "envelope": dict(ENVELOPE),
        "preflight_proof_sha256": preflight.get("proof_sha256"),
        "claim_proof_sha256": claim.get("proof_sha256"),
        "claim_ref": claim.get("ref"),
        "status": report["status"],
        "reason": report["reason"],
        "report_sha256": _file_sha256(_path(root, "report_json")),
        "database_sha256": _file_sha256(database),
        "business_validation": "not_executed",
        "created_utc": _now(),
    }
    _write_json(_path(root, "provenance"), provenance)


def _emit(report: Mapping[str, Any]) -> int:
    code = json_pair.report_exit_code(dict(report))
    print(f"status={report['status']} reason={report['reason']} exit={code}")
    return code


def cmd_finalize(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    root = Path(args.root)
    _layout(root, args.campaign_id)
    _forbid_secrets(env)
    database = _path(root, "database", args.campaign_id)
    report = json_pair.reconcile_pair(database=database, campaign_id=args.campaign_id)  # n'émet jamais
    _write_reports(root, args.campaign_id, report, env)
    return _emit(report)


# ── run ───────────────────────────────────────────────────────────────────────────────────────────────────────────────


def _google_upstream(key: str) -> Any:
    from collegue.broker.upstream import GoogleUpstream

    return GoogleUpstream(lambda: key, timeout=HTTP_TIMEOUT_SECONDS)


def cmd_run(
    args: argparse.Namespace, env: Mapping[str, str], upstream_factory: Optional[Callable[[str], Any]] = None
) -> int:
    root = Path(args.root)
    _layout(root, args.campaign_id)
    _forbid_secrets(env, allow=(KEY_ENV,))
    key = str(env.get(KEY_ENV, "") or "")
    if len(key) < MIN_SECRET_LENGTH:
        raise Refused("key_missing")
    _check_context(env, None)
    preflight = _load_proof(root, "preflight", PREFLIGHT_SCHEMA, args.campaign_id, env)
    _load_proof(root, "claim", CLAIM_SCHEMA, args.campaign_id, env, preflight)
    database = _path(root, "database", args.campaign_id)
    if json_pair.read_report(database=database, campaign_id=args.campaign_id)["reason"] != "not_started":
        # Registre déjà démarré (ou étranger) : jamais de reprise. On réconcilie, on rapporte, on refuse de poursuivre.
        report = json_pair.reconcile_pair(database=database, campaign_id=args.campaign_id)
        _write_reports(root, args.campaign_id, report, env)
        print("run: refused resume_refused")
        return EXIT_INCOMPLETE
    upstream = (upstream_factory or _google_upstream)(key)

    async def _main() -> dict:
        task = asyncio.ensure_future(
            json_pair.run_pair(database=database, campaign_id=args.campaign_id, upstream=upstream)
        )
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, task.cancel)  # SIGTERM : aucune nouvelle émission, journal lisible
        try:
            return await task
        except asyncio.CancelledError:
            return json_pair.reconcile_pair(database=database, campaign_id=args.campaign_id)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)

    report = asyncio.run(_main())
    try:
        _write_reports(root, args.campaign_id, report, env)
    except OSError:
        pass  # ``finalize`` réécrit de toute façon ; le code de sortie reste celui du rapport
    return _emit(report)


# ── entrée ────────────────────────────────────────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Paire JSON W5 : preflight, claim, run, finalize.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "claim", "run", "finalize"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--campaign-id", required=True)
        if name == "preflight":
            command.add_argument("--expected-sha", required=True)
            command.add_argument("--confirm", required=True)
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    clients_factory: Optional[Callable[[str], Any]] = None,
    upstream_factory: Optional[Callable[[str], Any]] = None,
) -> int:
    args = build_parser().parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        if args.command == "preflight":
            return cmd_preflight(args, env)
        if args.command == "claim":
            return cmd_claim(args, env, clients_factory)
        if args.command == "run":
            return cmd_run(args, env, upstream_factory)
        return cmd_finalize(args, env)
    except Refused as refused:
        print(f"{args.command}: refused {refused.code}")
        return EXIT_INCOMPLETE
    except json_pair.JsonPairRefused as refused:
        print(f"{args.command}: refused {refused.code}")
        return EXIT_INCOMPLETE
    except Exception as exc:  # noqa: BLE001 - jamais de trace libre : le NOM de classe seulement
        name = type(exc).__name__ if re.fullmatch(r"[A-Za-z0-9_]{1,60}", type(exc).__name__) else "Exception"
        print(f"{args.command}: refused internal_error {name}")
        return EXIT_INCOMPLETE


if __name__ == "__main__":
    sys.exit(main())
