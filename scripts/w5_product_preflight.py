#!/usr/bin/env python3
"""Harnais du préflight PRODUIT distant, sans modèle (propriété C, vague 5, C69).

Il exécute les VRAIES entrées publiques du produit — ``python -m collegue.pilot.w4_business preflight --stage static|full`` — sous le jeton de
lecture de la fixture, puis constitue des artefacts inspectables et une provenance. Il ne REMPLACE aucun contrôle du produit (P01 à P12 sont ceux de
``collegue.pilot.w4_business`` / ``w5_business``), ne fabrique aucun fait de lancement (``GITHUB_EVENT_NAME``, ``GITHUB_RUN_ATTEMPT`` et la
confirmation viennent du déclenchement RÉEL du workflow) et ne peut appeler ni ``run``, ni ``claim``, ni ``activate``, ni ``cleanup`` : la seule
action du produit qu'il sait construire est ``preflight`` (liste fermée, testée).

Deux sous-commandes :

* ``stage --stage static|full`` : lance le préflight du produit (le code de sortie du produit est conservé : 0 validé, 1 échec, 3 validation
  incomplète), enregistre ce code (``diag/rc-<stage>.json``), puis passe le scanner de confiance ``scripts/w5_leak_scan.py`` (jeton de fixture
  cherché dans le contenu ET les noms) qui construit le SEUL ensemble publiable ``publish/<stage>``. Le code retourné est celui du produit s'il
  est non nul, sinon celui du scanner. Les sorties du produit et du scanner sont relues puis EXPURGÉES du jeton avant d'être affichées ;
* ``provenance`` : relit les rapports et codes enregistrés (aucun jeton nécessaire) et écrit ``diag/provenance.json`` : SHA testé, arbres du produit
  et du harnais, empreinte du manifeste, image, commandes et codes réels, étapes réellement jouées, verdict. Seuls des champs TYPÉS sont recopiés
  (énumérations fermées, entiers, booléens, identifiants validés) : jamais un détail libre d'étape, jamais un message. Code : 0 validé, 1 échec,
  3 incomplet (l'absence du préflight complet, d'un code, d'un rapport sain ou d'une image est « incomplet », jamais « validé »).

Défense en profondeur : le harnais REFUSE (code 2, avant toute émission) si une variable de clé de modèle est présente (le produit le refuse aussi, en
P03), si le jeton de fixture est absent ou trop court, ou si l'identifiant de campagne n'a pas la forme attendue. Ce préflight ne prouve NI BUILD,
NI R04/R05, NI la campagne : il établit que le produit, tel que publié, franchit ses contrôles réels contre GitHub sans émettre.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PRODUCT_MODULE = "collegue.pilot.w4_business"
PRODUCT_ACTION = "preflight"  # la SEULE action du produit que ce harnais construit
STAGES = ("static", "full")
TOKEN_ENV = "GITHUB_TOKEN"
MIN_TOKEN_LENGTH = 8
CAMPAIGN_ID = re.compile(r"[a-z0-9][a-z0-9-]{2,39}")
FORBIDDEN_ENV = re.compile(
    r"(LLM_API_KEY(_[A-Z]+)?|GEMINI_API_KEY|GOOGLE_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|W5_GOOGLE[A-Z_]*)"
)

SCHEMA = "w5-product-preflight-provenance/1"
SCOPE = (
    "préflight PRODUIT (statique puis complet) sous le jeton de lecture de la fixture, sans clé de modèle : ne prouve NI BUILD, "
    "NI R04/R05, NI la campagne ; une campagne exige un ordre distinct"
)
# Énumérations FERMÉES du produit (garde-fou : tests/test_w5_ci_product_preflight.py les compare aux constantes de w4_business).
STEP_STATES = ("succeeded", "not_executed", "budget_stop", "failed", "incomplete_validation")
VERDICTS = ("validated", "failed", "budget_stop", "incomplete_validation")
EXIT_CODES = {"validated": 0, "failed": 1, "incomplete_validation": 3, "budget_stop": 4}
REPORT_SCHEMA = "w4-business-report/1"
STEP_ID = re.compile(r"P\d{2}-[a-z0-9-]{1,40}")
SHA = re.compile(r"[0-9a-f]{40}")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
HARNESS_FILES = (
    "scripts/w5_product_preflight.py",
    "scripts/w5_leak_scan.py",
    "scripts/w5_workflow_env.py",
    ".github/workflows/w5-product-preflight.yml",
)
PRODUCT_TREES = ("collegue", "locks", "docker/sandbox")
#: Contrôles que le préflight du produit DÉCLARE (P01 à P12), dans les deux étapes. Liste fermée, complète et unique : une omission, un doublon, un
#: contrôle inconnu ou un drapeau ``required`` falsifié interdit le verdict « validé » (garde-fou : les tests la comparent à ``run_preflight``).
EXPECTED_STEPS = (
    "P01-launch-context",
    "P02-environment",
    "P03-secret-scope",
    "P04-fixture-identity",
    "P05-role-routes",
    "P06-worker-capacity",
    "P07-base-protection",
    "P08-oracle-environment",
    "P09-gemma-models",
    "P10-budget-broker",
    "P11-bootstrap",
    "P12-campaign-identity",
)
OPTIONAL_IN_STATIC = "P08-oracle-environment"  # image du gate : construite seulement après l'étape statique (facultative et non jouée alors)
FILE_NAME = re.compile(r"[A-Za-z0-9._-]{1,80}")
EXCEPTION_LINE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*\.)*[A-Z][A-Za-z0-9_]*(Error|Exception|Exit|Interrupt)(:|$)")
MANIFEST = "tests/fixtures/w5-business/manifest.v2.json"


def _load_scanner() -> Any:
    spec = importlib.util.spec_from_file_location("w5_leak_scan_harness", SCRIPTS / "w5_leak_scan.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def redact(text: str, token: str) -> str:
    """``text`` sans AUCUNE forme usuelle du jeton (brute, URL, JSON, base64 à trois alignements)."""
    scanner = _load_scanner()
    for form in sorted(scanner.variants(token).values(), key=len, reverse=True):
        text = text.replace(form.decode("ascii", "ignore"), "[REDACTED]")
    return text


def layout(root: Path) -> Dict[str, Path]:
    return {
        "report": root / "report",
        "publish": root / "publish",
        "quarantine": root / "quarantine",
        "diag": root / "diag",
    }


def product_argv(stage: str, campaign_id: str, output: Path, human: Path) -> List[str]:
    """Commande EXACTE du produit : ``preflight`` seulement, aucune autre action possible."""
    return [
        sys.executable,
        "-m",
        PRODUCT_MODULE,
        PRODUCT_ACTION,
        "--stage",
        stage,
        "--campaign-id",
        campaign_id,
        "--output",
        str(output),
        "--human",
        str(human),
    ]


def scanner_argv(stage: str, root: Path) -> List[str]:
    paths = layout(root)
    return [
        sys.executable,
        str(SCRIPTS / "w5_leak_scan.py"),
        "--env",
        TOKEN_ENV,
        "--quarantine",
        str(paths["quarantine"] / stage),
        "--stage",
        str(paths["publish"] / stage),
        "--report",
        str(paths["diag"] / f"leak-scan-{stage}.json"),
        "--source",
        f"report={paths['report'] / stage}",
    ]


def safe_text(text: str, token: str, limit: int = 500) -> str:
    """Texte court sans AUCUNE forme du jeton : expurgation AVANT troncature (un jeton coupé à la borne ne laisserait qu'un préfixe sensible)."""
    return redact(text, token)[:limit]


def exception_class(stderr: Optional[str]) -> Optional[str]:
    """Nom de CLASSE de la dernière exception d'un traceback (jamais son message) ; ``None`` si aucune ligne reconnaissable."""
    for line in reversed((stderr or "").strip().splitlines()[-5:]):
        if EXCEPTION_LINE.match(line.strip()):
            return line.strip().split(":", 1)[0]
    return None


def summary_lines(stage: str, root: Path, product_rc: int, scan_rc: int, stderr_class: Optional[str]) -> List[str]:
    """Journal MINIMAL et typé de l'étape, dérivé des rapports enregistrés : jamais la sortie brute du produit ni du scanner
    (le rapport complet, scanné puis déposé, est la source de détail)."""
    outcome = stage_outcome(stage, root)
    lines = [
        f"préflight {stage} : code produit {product_rc}, code scanner {scan_rc}, scanner {outcome['leak_scan'] or 'sans verdict'}"
    ]
    report = outcome["report"]
    if report is None:
        suffix = f" (classe d'exception : {stderr_class})" if stderr_class else ""
        lines.append(f"aucun rapport machine lisible{suffix}")
        return lines
    lines.append(f"verdict produit : {report['verdict']} (code {report['exit_code']})")
    lines += [f"  {item['id']} : {item['state']}" for item in report["steps"]]
    return lines


def run_stage(
    stage: str,
    *,
    root: Path,
    campaign_id: str,
    env: Mapping[str, str],
    runner: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
) -> int:
    if stage not in STAGES:
        print(f"w5_product_preflight: étape inconnue {stage!r}", file=sys.stderr)
        return 2
    if not CAMPAIGN_ID.fullmatch(campaign_id):
        print("w5_product_preflight: identifiant de campagne invalide : aucune commande lancée", file=sys.stderr)
        return 2
    present = sorted(name for name, value in env.items() if value and FORBIDDEN_ENV.fullmatch(name))
    if present:
        print(
            f"w5_product_preflight: variable(s) de clé de modèle présente(s) ({', '.join(present)}) : aucune commande lancée",
            file=sys.stderr,
        )
        return 2
    token = env.get(TOKEN_ENV, "")
    if len(token) < MIN_TOKEN_LENGTH:
        print(f"w5_product_preflight: ${TOKEN_ENV} absent ou trop court : aucune commande lancée", file=sys.stderr)
        return 2
    root = (
        root.resolve()
    )  # chemins ABSOLUS : le cwd des sous-processus est la racine du dépôt, jamais celui de l'appelant
    paths = layout(root)
    for name in ("report", "publish", "quarantine", "diag"):
        (paths[name]).mkdir(parents=True, exist_ok=True)
    report_dir = paths["report"] / stage
    report_dir.mkdir(parents=True, exist_ok=True)
    output, human = report_dir / f"preflight-{stage}.json", report_dir / f"preflight-{stage}.txt"
    argv = product_argv(stage, campaign_id, output, human)
    done = runner(argv, env=dict(env), cwd=str(ROOT), capture_output=True, text=True, check=False)
    stderr_class = exception_class(done.stderr)
    product_rc = int(done.returncode)
    (paths["diag"] / f"rc-{stage}.json").write_text(
        json.dumps({"stage": stage, "action": PRODUCT_ACTION, "rc": product_rc}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    scan = runner(scanner_argv(stage, root), env=dict(env), cwd=str(ROOT), capture_output=True, text=True, check=False)
    scan_rc = int(scan.returncode)
    for line in summary_lines(stage, root, product_rc, scan_rc, stderr_class):
        print(safe_text(line, token))
    if product_rc != 0:
        return product_rc  # garde rouge ou validation incomplète du produit : code conservé tel quel
    if not output.exists():
        print("w5_product_preflight: le produit a rendu 0 sans rapport machine : rouge", file=sys.stderr)
        return 1
    return scan_rc


# ── provenance ──────────────────────────────────────────────────────────────────────────────────────────────────────────


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10**9 else None


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def typed_report(data: Optional[Dict[str, Any]], stage: str) -> Optional[Dict[str, Any]]:
    """Champs TYPÉS d'un rapport du produit (aucun détail libre) ; ``None`` si le rapport n'est pas celui d'un préflight de cette étape."""
    if not data or data.get("schema") != REPORT_SCHEMA or data.get("kind") != "preflight":
        return None
    facts = data.get("facts") if isinstance(data.get("facts"), dict) else {}
    if facts.get("stage") != stage:
        return None
    steps, dropped, unexpected = [], 0, 0
    raw_steps = data.get("steps") if isinstance(data.get("steps"), list) else []
    for item in raw_steps:
        if not (
            isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and STEP_ID.fullmatch(item["id"])
            and item.get("state") in STEP_STATES
        ):
            dropped += 1  # élément illisible : compté, jamais ignoré en silence (la conformité l'exige à 0)
        elif item["id"] not in EXPECTED_STEPS:
            unexpected += 1  # contrôle inconnu : compté, son identifiant n'est jamais recopié dans la provenance
        else:
            steps.append({"id": item["id"], "state": item["state"], "required": item.get("required") is True})
    verdict = data.get("verdict") if data.get("verdict") in VERDICTS else None
    return {
        "verdict": verdict,
        "exit_code": _int(data.get("exit_code")),
        "steps": steps,
        "dropped_steps": dropped,
        "unexpected_steps": unexpected,
        "played": [s["id"] for s in steps if s["state"] != "not_executed"],
        "llm_calls_emitted": _int(facts.get("llm_calls_emitted")),
        "billable_actions_emitted": _int(facts.get("billable_actions_emitted")),
    }


def step_problems(stage: str, report: Mapping[str, Any]) -> List[str]:
    """Écarts entre les contrôles du rapport et la liste ATTENDUE (vide = conforme). N'accorde AUCUNE confiance à une omission ni à un drapeau
    ``required`` : P08 est facultatif et non joué seulement à l'étape statique, obligatoire à l'étape complète ; tout autre contrôle est obligatoire."""
    problems: List[str] = []
    steps = report.get("steps") or []
    ids = [item["id"] for item in steps]
    for step_id in EXPECTED_STEPS:
        if step_id not in ids:
            problems.append(f"missing:{step_id}")
        elif ids.count(step_id) > 1:
            problems.append(f"duplicate:{step_id}")
    if report.get("unexpected_steps", 0):
        problems.append("unexpected_step")
    if report.get("dropped_steps", 0):
        problems.append("malformed_steps")
    for item in steps:
        if item["id"] not in EXPECTED_STEPS:
            continue
        optional = stage == "static" and item["id"] == OPTIONAL_IN_STATIC
        if item["required"] is optional:
            problems.append(f"required_flag:{item['id']}")
        if optional and item["state"] != "not_executed":
            problems.append(f"optional_step_played:{item['id']}")
    return problems


def infer_stage(outcome: Mapping[str, Any]) -> str:
    """Étape d'un résultat enregistré : champ ``stage``, sinon noms des fichiers publiés ; inconnue ⇒ « full » (la plus exigeante)."""
    stage = outcome.get("stage")
    if stage in STAGES:
        return str(stage)
    names = " ".join(outcome.get("published_files") or [])
    found = [candidate for candidate in STAGES if f"preflight-{candidate}." in names]
    return found[0] if len(found) == 1 else "full"


def stage_outcome(stage: str, root: Path) -> Dict[str, Any]:
    paths = layout(root)
    rc_record = read_json(paths["diag"] / f"rc-{stage}.json")
    scan = read_json(paths["diag"] / f"leak-scan-{stage}.json")
    report = typed_report(read_json(paths["report"] / stage / f"preflight-{stage}.json"), stage)
    rc = _int(rc_record.get("rc")) if rc_record and rc_record.get("stage") == stage else None
    scan_verdict = scan.get("verdict") if scan and scan.get("verdict") in ("clean", "leak", "incomplete") else None
    published = (
        sorted(
            p.name
            for p in (paths["publish"] / stage / "report").glob("*")
            if p.is_file() and FILE_NAME.fullmatch(p.name)
        )
        if (paths["publish"] / stage / "report").is_dir()
        else []
    )
    return {
        "stage": stage,
        "executed": rc is not None,
        "product_rc": rc,
        "leak_scan": scan_verdict,
        "published_files": published,
        "report": report,
    }


def stage_status(outcome: Mapping[str, Any], stage: Optional[str] = None) -> str:
    """``validated`` seulement si TOUT est établi : code 0, rapport sain validé, liste des contrôles COMPLÈTE et conforme à l'étape, étapes requises
    réussies, zéro émission, scanner propre. ``stage`` absent : déduit du résultat enregistré (``infer_stage``)."""
    report = outcome["report"]
    stage = stage or infer_stage(outcome)
    if not outcome["executed"]:
        return "incomplete_validation"
    if report is None:
        return "failed" if outcome["product_rc"] not in (None, 0) else "incomplete_validation"
    if report["verdict"] == "failed" or (outcome["product_rc"] not in (None, 0) and report["verdict"] == "validated"):
        return "failed"
    required_ok = (
        bool(report["steps"])
        and not step_problems(stage, report)
        and all(s["state"] == "succeeded" for s in report["steps"] if s["required"])
    )
    clean = (
        outcome["product_rc"] == 0
        and report["verdict"] == "validated"
        and report["exit_code"] == 0
        and required_ok
        and report["llm_calls_emitted"] == 0
        and report["billable_actions_emitted"] == 0
        and outcome["leak_scan"] == "clean"
    )
    if clean:
        return "validated"
    if report["verdict"] in ("incomplete_validation", "budget_stop"):
        return report["verdict"]
    # « validated » annoncé mais preuve non établie (contrôle absent, doublon, drapeau falsifié, étape requise non réussie…) : jamais validé
    return "incomplete_validation" if report["verdict"] == "validated" and outcome["product_rc"] == 0 else "failed"


def git_value(*args: str, cwd: Path = ROOT) -> Optional[str]:
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    value = done.stdout.strip()
    return value if done.returncode == 0 and SHA.fullmatch(value) else None


def build_provenance(
    root: Path,
    env: Mapping[str, str],
    *,
    image_id: Optional[str],
    tree: Callable[[str], Optional[str]] = lambda spec: git_value("rev-parse", spec),
) -> Dict[str, Any]:
    outcomes = {stage: stage_outcome(stage, root) for stage in STAGES}
    statuses = {stage: stage_status(outcomes[stage], stage) for stage in STAGES}
    sha = env.get("GITHUB_SHA", "")
    manifest = ROOT / MANIFEST
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.is_file() else None
    launch = {
        "event": env.get("GITHUB_EVENT_NAME") or None,
        "run_id": env.get("GITHUB_RUN_ID") or None,
        "run_attempt": env.get("GITHUB_RUN_ATTEMPT") or None,
        "ref": env.get("GITHUB_REF") or None,
        "repository": env.get("GITHUB_REPOSITORY") or None,
    }
    image_ok = bool(image_id and IMAGE_ID.fullmatch(image_id))
    reasons: List[str] = []
    if not SHA.fullmatch(sha):
        reasons.append("sha_absent")
    if launch["event"] != "workflow_dispatch" or launch["run_attempt"] != "1":
        reasons.append("launch_context_not_first_dispatch")
    if manifest_sha is None:
        reasons.append("manifest_absent")
    if not image_ok:
        reasons.append("image_id_absent")
    trees = {path: tree(f"HEAD:{path}") for path in PRODUCT_TREES}
    blobs = {path: tree(f"HEAD:{path}") for path in HARNESS_FILES}
    trees = {path: (value if value and SHA.fullmatch(value) else None) for path, value in trees.items()}
    blobs = {path: (value if value and SHA.fullmatch(value) else None) for path, value in blobs.items()}
    if any(value is None for value in trees.values()):
        reasons.append("product_tree_absent")  # le contenu produit testé n'est pas prouvé : jamais « validé »
    if any(value is None for value in blobs.values()):
        reasons.append("harness_blob_absent")
    for stage in STAGES:
        if statuses[stage] != "validated":
            reasons.append(f"{stage}:{statuses[stage]}")
        report = outcomes[stage]["report"]
        if report is not None:
            reasons += [f"{stage}:{problem}" for problem in step_problems(stage, report)]
    if any(statuses[s] == "failed" for s in STAGES):
        verdict = "failed"
    elif reasons:
        verdict = "incomplete_validation"
    else:
        verdict = "validated"
    emitted = {
        "llm_calls_emitted": sum((o["report"] or {}).get("llm_calls_emitted") or 0 for o in outcomes.values()),
        "billable_actions_emitted": sum(
            (o["report"] or {}).get("billable_actions_emitted") or 0 for o in outcomes.values()
        ),
    }
    return {
        "schema": SCHEMA,
        "scope": SCOPE,
        "verdict": verdict,
        "reasons": reasons,
        "partial": not outcomes["full"]["executed"],
        "run": launch,
        "product": {"sha": sha or None, "trees": trees},
        "harness": {"sha": sha or None, "blobs": blobs},
        "manifest": {"path": MANIFEST, "sha256": manifest_sha},
        "image": {"name": env.get("SANDBOX_IMAGE") or None, "id": image_id if image_ok else None},
        "commands": [
            {
                "stage": stage,
                "argv": [PRODUCT_MODULE, PRODUCT_ACTION, "--stage", stage],
                "rc": outcomes[stage]["product_rc"],
            }
            for stage in STAGES
            if outcomes[stage]["executed"]
        ],
        "stages": {stage: {**outcomes[stage], "status": statuses[stage]} for stage in STAGES},
        "played_steps": {stage: (outcomes[stage]["report"] or {}).get("played", []) for stage in STAGES},
        **emitted,
    }


def write_provenance(root: Path, env: Mapping[str, str], image_id: Optional[str], output: Path) -> int:
    record = build_provenance(root, env, image_id=image_id)
    output.write_text(json.dumps(record, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"verdict": record["verdict"], "reasons": record["reasons"], "partial": record["partial"]}, sort_keys=True
        )
    )
    return EXIT_CODES[record["verdict"]]


def main(argv: Optional[Sequence[str]] = None, *, environ: Optional[Mapping[str, str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Harnais du préflight produit distant (sans modèle).")
    sub = parser.add_subparsers(dest="command", required=True)
    stage = sub.add_parser("stage", help="lance le préflight du produit pour une étape, puis le scanner de fuite")
    stage.add_argument("--stage", choices=STAGES, required=True)
    stage.add_argument("--root", type=Path, required=True)
    stage.add_argument("--campaign-id", required=True)
    prov = sub.add_parser("provenance", help="écrit la provenance typée à partir des rapports et codes enregistrés")
    prov.add_argument("--root", type=Path, required=True)
    prov.add_argument("--image-id", default="")
    prov.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    if args.command == "stage":
        return run_stage(args.stage, root=args.root, campaign_id=args.campaign_id, env=env)
    return write_provenance(args.root, env, args.image_id or None, args.output)


if __name__ == "__main__":
    sys.exit(main())
