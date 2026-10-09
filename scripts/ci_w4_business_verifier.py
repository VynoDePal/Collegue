#!/usr/bin/env python3
"""Preuve CI SANS MODÈLE ET SANS RÉSEAU du vérificateur métier public de la vague 4, dans l'image construite (propriété C).

Exécuté par le job « Docker build » APRÈS la construction de l'image OpenHands, sur l'hôte du runner (pas dans un conteneur) : il
appelle le chemin PUBLIC de B, ``collegue.pilot.w4_business.verify_business_checkout``, **runner omis** et avec l'image construite
passée explicitement. C'est donc le vrai ``docker_verifier_command`` (montages validés par la garde W1, réseau none, racine en
lecture seule, UID de l'appelant, ``/scratch``) et le vrai superviseur ``timeout(1)`` du conteneur qui sont éprouvés. Ce script ne
construit aucun montage, n'exécute aucun livrable sur l'hôte, n'utilise aucune clé et n'émet aucun appel de modèle.

Cas (tous obligatoires ; un cas non exécuté échoue le job, jamais un saut ni un succès par défaut) :

* ``pass_reference`` — fixture de confiance (``tests/w4_business_fixture.py``, étape 3) : base SQLite vierge, migration,
  création et lecture d'un audit, redémarrage, PDF lu par un vrai lecteur. Observation ``passed``, TOUTES les assertions métier
  connues présentes et vraies, aucune absente ni fausse ;
* ``hostile_ignores_alarm`` — même fixture dont l'import annule ``SIGALRM``, l'ignore, puis dort 600 s. Le vrai conteneur doit
  être arrêté par le superviseur EXTERNE au code livré (``timeout`` : code 124), l'import doit avoir été atteint (marqueur sur
  stderr), le résultat est ``incomplete`` et la durée observée est bornée (échéance, pas fin normale du témoin) ;
* ``hostile_ignores_term`` — idem mais le témoin ignore aussi ``SIGTERM`` : le superviseur doit passer au ``KILL`` après
  ``WATCHDOG_KILL_AFTER`` secondes (code 137).

**Portée de la preuve d'échéance.** Pour établir que c'est le superviseur du conteneur (et non la relève du client hôte) qui arrête
le témoin, la marge de la relève hôte (``HOST_KILL_MARGIN``, 2 s en production) est allongée À 60 s pendant ces deux cas, par
affectation de l'attribut du module de B ; le chemin produit n'est pas modifié. En contrepartie la marge de production de 2 s n'est
pas éprouvée ici, et le crash réel de l'hôte (conteneur orphelin) n'est pas provoqué : seule l'indépendance du superviseur
vis-à-vis du code livré l'est.

Sorties : ``<report-dir>/report.json`` (cas, commandes sans le script en ligne, codes de retour réels, versions utiles, aucun
secret), écrit MÊME sur échec ; code de sortie 0 seulement si tous les cas obligatoires sont passés.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "w4_business_fixture.py"
BUSINESS_MODULE = "collegue.pilot.w4_business"
SCHEMA = "w4-business-verifier-proof/1"

CASE_PASS = "pass_reference"
CASE_ALARM = "hostile_ignores_alarm"
CASE_TERM = "hostile_ignores_term"
REQUIRED_CASES = (CASE_PASS, CASE_ALARM, CASE_TERM)

#: Assertions métier connues de la vérification (phases d'écriture puis de relecture) : toutes doivent être présentes ET vraies.
EXPECTED_CHECKS = (
    "write:database_really_empty",
    "write:migration_succeeds",
    "write:schema_tables",
    "write:audit_created",
    "write:audit_read_back_identical",
    "write:findings_preserved",
    "write:unknown_audit_is_404",
    "write:rows_persisted",
    "write:pdf_served",
    "write:pdf_text_has_the_persisted_audit_data",
    "write:pdf_names_the_audit",
    "write:legal_notice_present",
    "reread:audit_survives_restart",
    "reread:pdf_served",
    "reread:pdf_text_has_the_persisted_audit_data",
    "reread:pdf_names_the_audit",
    "reread:legal_notice_present",
)

HOSTILE_MARKER = "W4-HOSTILE-IMPORT-REACHED"
HOSTILE_LIMIT_SECONDS = 8.0  # durée accordée au témoin (le témoin dort 600 s : seule l'échéance peut l'arrêter)
HOSTILE_SLEEP_SECONDS = 600
#: Relève hôte de secours pendant les cas hostiles (production : ``HOST_KILL_MARGIN`` = 2 s) — voir « Portée » ci-dessus.
HOST_RELIEF_MARGIN = 60.0
#: Marge de scrutation du démarrage/arrêt Docker au-dessus de l'échéance, sans jamais approcher la fin normale du témoin.
TIMING_SLACK_SECONDS = 20.0
DEADLINE_RC_TERM, DEADLINE_RC_KILL = 124, 137

_HOSTILE_COMMON = (
    "import signal, sys, time\n"
    "signal.alarm(0)\n"
    "signal.signal(signal.SIGALRM, signal.SIG_IGN)\n"
    "{extra}"
    f"sys.stderr.write({HOSTILE_MARKER!r} + '\\n'); sys.stderr.flush()\n"
    f"time.sleep({HOSTILE_SLEEP_SECONDS})\n"
)
HOSTILE_ALARM = _HOSTILE_COMMON.format(extra="")
HOSTILE_TERM = _HOSTILE_COMMON.format(extra="signal.signal(signal.SIGTERM, signal.SIG_IGN)\n")

_DOCKER_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")
_SECRETISH = re.compile(r"(?i)(key|token|secret|password|credential)")


#: Horloge de mesure des durées (remplaçable par les tests du script ; la production lit l'horloge monotone du système).
_clock: Callable[[], float] = time.monotonic


class ProofError(Exception):
    """Un prérequis de la preuve est absent (image, module, fixture) : la preuve n'est PAS établie."""


# ── frontière Docker de l'orchestrateur (lectures seules : version, image, conteneurs résiduels) ─────────────────────────────


def docker_env() -> Dict[str, str]:
    return {name: os.environ[name] for name in _DOCKER_ENV_ALLOWLIST if name in os.environ}


def default_docker(args: Sequence[str], timeout: float = 120.0) -> "subprocess.CompletedProcess":
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, env=docker_env(), check=False
    )


DockerFn = Callable[..., "subprocess.CompletedProcess"]


def host_facts(docker: DockerFn, image: str) -> Dict[str, Any]:
    """Versions et identité utiles, SANS secret ; échoue si l'image est absente ou illisible (jamais un succès par défaut)."""
    inspected = docker(["image", "inspect", image, "--format", "{{.Id}}"])
    if inspected.returncode != 0 or not (inspected.stdout or "").strip():
        raise ProofError(f"image {image!r} absente ou illisible (docker image inspect rc={inspected.returncode})")
    version = docker(["version", "--format", "{{.Client.Version}} / {{.Server.Version}}"])
    tools = docker(
        [
            "run", "--rm", "--pull", "never", "--network", "none", "--read-only", "--cap-drop", "ALL", image,
            "sh", "-c", "python --version 2>&1; timeout --version 2>&1 | head -1",
        ]
    )  # fmt: skip
    if tools.returncode != 0:
        raise ProofError(f"outillage de l'image illisible (rc={tools.returncode}) : {(tools.stderr or '')[-200:]}")
    lines = (tools.stdout or "").strip().splitlines()
    return {
        "image": image,
        "image_id": inspected.stdout.strip(),
        "docker_client_server": (version.stdout or "").strip() if version.returncode == 0 else "inconnu",
        "image_python": lines[0] if lines else "",
        "image_timeout": lines[1] if len(lines) > 1 else "",
        "orchestrator_python": platform.python_version(),
        "uid": os.getuid() if hasattr(os, "getuid") else None,
    }


# ── fixture et module de B ────────────────────────────────────────────────────────────────────────────────────────────────


def load_business_module() -> Any:
    try:
        module = importlib.import_module(BUSINESS_MODULE)
    except Exception as exc:  # noqa: BLE001 - tout échec d'import est une preuve non établie
        raise ProofError(
            f"module {BUSINESS_MODULE} indisponible ({type(exc).__name__}: {exc}) : lot B non intégré ?"
        ) from exc
    for name in ("verify_business_checkout", "run_in_named_container"):
        if not callable(getattr(module, name, None)):
            raise ProofError(f"{BUSINESS_MODULE}.{name} absent : API publique de B attendue")
    return module


def load_fixture(path: Path = FIXTURE_PATH) -> Callable[[int], Dict[str, str]]:
    """``stage_files`` de la fixture de confiance, chargée PAR CHEMIN (``tests/`` n'est pas un paquet)."""
    if not path.is_file():
        raise ProofError(f"fixture de confiance absente : {path}")
    spec = importlib.util.spec_from_file_location("w4_business_fixture_ci", path)
    if spec is None or spec.loader is None:
        raise ProofError(f"fixture illisible : {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stage_files = getattr(module, "stage_files", None)
    if not callable(stage_files):
        raise ProofError("stage_files absent de la fixture")
    return stage_files


def write_tree(files: Dict[str, str], destination: Path) -> str:
    for relative, content in files.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return str(destination)


# ── espion de l'exécution réelle (n'altère pas le chemin produit : il enregistre puis délègue) ────────────────────────────


class ContainerSpy:
    """Enveloppe ``run_in_named_container`` : consigne la commande (script en ligne masqué), le code de retour RÉEL et les
    sorties tronquées, puis délègue à la fonction d'origine sans rien changer."""

    def __init__(self, business: Any):
        self._business = business
        self._original = business.run_in_named_container
        self.calls: List[Dict[str, Any]] = []

    def __enter__(self) -> "ContainerSpy":
        self._business.run_in_named_container = self
        return self

    def __exit__(self, *exc: Any) -> None:
        self._business.run_in_named_container = self._original

    @staticmethod
    def _mask(argv: Sequence[str]) -> List[str]:
        return [f"<script {len(a)} caractères>" if len(a) > 300 else a for a in argv]

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> Any:
        record: Dict[str, Any] = {
            "name": kwargs.get("name"),
            "argv": self._mask(argv),
            "host_timeout": kwargs.get("timeout"),
        }
        self.calls.append(record)
        started = _clock()
        try:
            proc = self._original(argv, **kwargs)
        except BaseException as exc:
            record.update(exception=type(exc).__name__, elapsed=round(_clock() - started, 2))
            raise
        record.update(
            returncode=proc.returncode,
            elapsed=round(_clock() - started, 2),
            stdout_tail=(proc.stdout or "")[-300:],
            stderr_tail=(proc.stderr or "")[-300:],
        )
        return proc


# ── cas ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────


def _case(status: str, detail: str = "", **extra: Any) -> Dict[str, Any]:
    return {"status": status, "detail": detail, **extra}


def _leftover(docker: DockerFn, names: Sequence[str]) -> List[str]:
    left = []
    for name in names:
        if not name:
            continue
        listed = docker(["ps", "-a", "-q", "--filter", f"name=^/{name}$"])
        if listed.returncode != 0 or (listed.stdout or "").strip():
            left.append(name)
    return left


def case_pass(context: Dict[str, Any]) -> Dict[str, Any]:
    business, docker, image = context["business"], context["docker"], context["image"]
    with tempfile.TemporaryDirectory(prefix="w4-ci-pass-") as folder:
        checkout = write_tree(context["stage_files"](3), Path(folder) / "checkout")
        with ContainerSpy(business) as spy:
            started = _clock()
            observation = business.verify_business_checkout(checkout, image=image)
            elapsed = round(_clock() - started, 2)
    problems: List[str] = []
    if observation.status != "passed":
        problems.append(
            f"observation {observation.status!r} (échecs : {list(observation.failed)}) : {observation.detail[:200]}"
        )
    checks = dict(observation.checks)
    absent = [name for name in EXPECTED_CHECKS if name not in checks]
    false = sorted(name for name, ok in checks.items() if not ok)
    if absent:
        problems.append(f"assertions métier absentes : {absent}")
    if false:
        problems.append(f"assertions métier fausses : {false}")
    seen = observation.observations
    if seen.get("write.db_existed_before") is not False:
        problems.append("la base n'était pas vierge")
    if not str(seen.get("write.pdf_reader", "")).startswith("pypdf "):
        problems.append("PDF non lu par pypdf")
    if seen.get("write.pdf_raw_bytes_contain_title") is not False:
        problems.append("le texte du PDF serait lisible par recherche d'octets (lecteur réel non éprouvé)")
    if len(spy.calls) != 2 or any(call.get("returncode") != 0 for call in spy.calls):
        problems.append(f"deux phases conteneur à code 0 attendues, vu {[c.get('returncode') for c in spy.calls]}")
    left = _leftover(docker, [call.get("name") for call in spy.calls])
    if left:
        problems.append(f"conteneur(s) résiduel(s) : {left}")
    return _case(
        "failed" if problems else "passed",
        "; ".join(problems),
        elapsed=elapsed,
        observation_status=observation.status,
        checks=checks,
        containers=spy.calls,
    )


def _case_hostile(context: Dict[str, Any], preamble: str, expected_rc: int, minimum: float) -> Dict[str, Any]:
    business, docker, image = context["business"], context["docker"], context["image"]
    files = dict(context["stage_files"](3))
    files["app/main.py"] = preamble + files["app/main.py"]
    previous_margin = getattr(business, "HOST_KILL_MARGIN", None)
    business.HOST_KILL_MARGIN = (
        HOST_RELIEF_MARGIN  # relève hôte allongée : seul le superviseur du conteneur peut conclure
    )
    try:
        with tempfile.TemporaryDirectory(prefix="w4-ci-hostile-") as folder:
            checkout = write_tree(files, Path(folder) / "checkout")
            with ContainerSpy(business) as spy:
                started = _clock()
                observation = business.verify_business_checkout(checkout, image=image, timeout=HOSTILE_LIMIT_SECONDS)
                elapsed = round(_clock() - started, 2)
    finally:
        if previous_margin is None:
            del business.HOST_KILL_MARGIN
        else:
            business.HOST_KILL_MARGIN = previous_margin
    problems: List[str] = []
    if observation.status != "incomplete":
        problems.append(f"résultat {observation.status!r} au lieu de 'incomplete'")
    if not spy.calls:
        problems.append("aucun conteneur lancé")
    else:
        first = spy.calls[0]
        if "exception" in first:
            problems.append(f"fin par exception hôte ({first['exception']}), pas par le superviseur du conteneur")
        elif first.get("returncode") != expected_rc:
            problems.append(f"code du conteneur {first.get('returncode')!r} au lieu de {expected_rc}")
        if HOSTILE_MARKER not in str(first.get("stderr_tail", "")):
            problems.append("l'import du témoin n'a pas été atteint (marqueur absent de stderr)")
    if not minimum <= elapsed <= minimum + TIMING_SLACK_SECONDS:
        problems.append(
            f"durée {elapsed}s hors de la fenêtre d'échéance [{minimum}, {minimum + TIMING_SLACK_SECONDS}] "
            f"(le témoin dort {HOSTILE_SLEEP_SECONDS}s : seule l'échéance doit l'arrêter)"
        )
    left = _leftover(docker, [call.get("name") for call in spy.calls])
    if left:
        problems.append(f"conteneur(s) résiduel(s) : {left}")
    return _case(
        "failed" if problems else "passed",
        "; ".join(problems),
        elapsed=elapsed,
        observation_status=observation.status,
        observation_detail=observation.detail,
        expected_returncode=expected_rc,
        host_relief_margin=HOST_RELIEF_MARGIN,
        containers=spy.calls,
    )


def case_alarm(context: Dict[str, Any]) -> Dict[str, Any]:
    return _case_hostile(context, HOSTILE_ALARM, DEADLINE_RC_TERM, HOSTILE_LIMIT_SECONDS)


def case_term(context: Dict[str, Any]) -> Dict[str, Any]:
    kill_after = float(getattr(context["business"], "WATCHDOG_KILL_AFTER", 3))
    return _case_hostile(context, HOSTILE_TERM, DEADLINE_RC_KILL, HOSTILE_LIMIT_SECONDS + kill_after - 0.5)


CASES: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    CASE_PASS: case_pass,
    CASE_ALARM: case_alarm,
    CASE_TERM: case_term,
}


def run_proof(
    image: str,
    *,
    docker: DockerFn = default_docker,
    business: Any = None,
    stage_files: Optional[Callable[[int], Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Exécute TOUS les cas obligatoires ; jamais d'exception vers l'appelant, jamais de cas manquant lu comme un succès."""
    report: Dict[str, Any] = {
        "schema": SCHEMA,
        "image": image,
        "ok": False,
        "scope": [
            "chemin public verify_business_checkout, runner omis, image construite explicite",
            "aucun modèle, aucun réseau applicatif, aucune clé ; conteneurs lancés par B en --network none",
            f"relève hôte allongée à {HOST_RELIEF_MARGIN:g}s pendant les cas hostiles (production : 2s) pour isoler le superviseur",
            "crash réel de l'hôte et conteneur orphelin non provoqués",
        ],
        "host": {},
        "cases": {name: _case("not_executed", "cas non exécuté") for name in REQUIRED_CASES},
        "failures": [],
    }
    try:
        report["host"] = host_facts(docker, image)
        business = business or load_business_module()
        stage_files = stage_files or load_fixture()
    except ProofError as exc:
        report["failures"].append(str(exc))
        return report
    except Exception as exc:  # noqa: BLE001
        report["failures"].append(f"préparation impossible ({type(exc).__name__}: {exc})")
        return report
    context = {"business": business, "docker": docker, "image": image, "stage_files": stage_files}
    for name in REQUIRED_CASES:
        try:
            report["cases"][name] = CASES[name](context)
        except Exception as exc:  # noqa: BLE001 - une exception est un cas ÉCHOUÉ, jamais un succès ni une interruption muette
            report["cases"][name] = _case("failed", f"exception {type(exc).__name__}: {exc}")
    for name in REQUIRED_CASES:
        outcome = report["cases"][name]
        if outcome["status"] != "passed":
            report["failures"].append(f"{name}: {outcome['status']} — {outcome.get('detail', '')}")
    report["ok"] = not report["failures"] and all(report["cases"][n]["status"] == "passed" for n in REQUIRED_CASES)
    return report


def scrub(report: Dict[str, Any]) -> Dict[str, Any]:
    """Défense en profondeur : aucune variable d'environnement de nom sensible ne figure dans le rapport."""
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, default=str)
    for name, value in os.environ.items():
        if value and len(value) >= 6 and _SECRETISH.search(name):
            text = text.replace(value, "[REDACTED]")
    return json.loads(text)


def write_report(report: Dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "report.json"
    path.write_text(json.dumps(scrub(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--image", required=True, help="image OpenHands construite par le job (tag explicite)")
    parser.add_argument("--report-dir", default="w4-verifier-report")
    args = parser.parse_args(argv)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        report = run_proof(args.image)
    except BaseException as exc:  # noqa: BLE001 - le rapport est écrit même sur interruption, puis l'échec est propagé
        report = {
            "schema": SCHEMA,
            "image": args.image,
            "ok": False,
            "failures": [f"interruption {type(exc).__name__}"],
        }
        write_report(report, Path(args.report_dir))
        raise
    path = write_report(report, Path(args.report_dir))
    for name in REQUIRED_CASES:
        outcome = report["cases"][name]
        print(f"{name:24s} {outcome['status']:13s} {outcome.get('detail', '')[:200]}")
    for failure in report["failures"]:
        print(f"ECART: {failure}", file=sys.stderr)
    print(f"rapport : {path} — {'OK' if report['ok'] else 'ÉCHEC'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
