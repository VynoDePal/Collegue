"""Lanceur et juge des oracles d'acceptation (vague 3) : un verdict n'est jamais « exit 0 ».

Le lanceur pytest isolé de la vague 2 ne renvoyait qu'un code de sortie : tous les tests sautés, un contrat
partiellement sauté ou un ``xfail`` sortaient à 0 et passaient. Il émet désormais un **rapport complet**, produit
par un plugin pytest DANS le conteneur, que l'hôte juge phase par phase :

- une assertion n'est « liée au contrat » que levée dans la phase ``call`` d'un test RÉELLEMENT exécuté
  (``AssertionError`` du corps du test) ; une erreur de collecte/d'import/de setup ne compte pas ;
- ``skip``, ``xfail`` (échoué ou réussi), zéro test, erreur de collecte, arrêt prématuré sans rapport complet
  (sortie du process, délai, sortie tronquée) rendent le run **invalide**, jamais « vert » ni « rouge par assertion » ;
- le rapport porte un nonce choisi par l'hôte pour ce run (une ligne de sortie du projet ne peut pas en forger un
  par hasard). Limite assumée : du code malveillant exécuté DANS le même interpréteur que pytest pourrait forger le
  statut de sortie ET le rapport ; l'isolation d'un oracle contre le code qu'il teste n'est pas fournie.

Les protections du lanceur précédent sont conservées : ``pytest`` importé sous ``python -I`` AVANT l'ajout des
chemins du projet, fichiers d'oracle aléatoires créés exclusivement dans le tmpfs, ``--noconftest``, configuration et
plugins du projet neutralisés (``-c /dev/null``, ``PYTEST_DISABLE_PLUGIN_AUTOLOAD``).
"""

from __future__ import annotations

import base64
import json
import secrets
import shlex
from typing import Any, Dict, List, Optional, Sequence, Tuple

from collegue.executor.delivery_proof import OracleRun

ORACLE_MARKER = "@@COLLEGUE-ORACLE-REPORT"
ORACLE_END = "@@END@@"
MAX_REPORT_ITEMS = 5000

STATUS_GREEN = "green"
STATUS_RED_ASSERTION = "red-assertion"
STATUS_INVALID = "invalid"

_LAUNCHER = """\
import base64
import json
import os
import site
import sys
import tempfile

import pytest

ENTRIES = {entries!r}
NONCE = {nonce!r}
MARKER = {marker!r}
END = {end!r}
WORKSPACE = {workspace!r}
TMP = {tmp!r}
LIMIT = {limit!r}

user_site = site.getusersitepackages()
project_paths = [path for path in (WORKSPACE, WORKSPACE + "/src", user_site) if path]
# pytest est déjà importé depuis l'image sous -I : on peut maintenant préfixer les dépendances installées
# --user et le projet sans permettre un pytest.py local.
sys.path[:0] = [path for path in project_paths if path not in sys.path]
os.chdir(WORKSPACE)


class Recorder:
    def __init__(self):
        self.items = []
        self.collect_errors = []
        self.collected = {{}}
        self.finished = False
        self.exitstatus = None
        self.labels = {{}}

    def label_of(self, nodeid):
        name = os.path.basename(str(nodeid).split("::", 1)[0])
        return self.labels.get(name)

    def pytest_collectreport(self, report):
        if report.failed:
            label = self.label_of(report.nodeid)
            if label is not None:
                self.collect_errors.append({{"f": label, "e": str(report.longrepr)[-300:]}})

    def pytest_collection_modifyitems(self, session, config, items):
        for item in items:
            label = self.label_of(item.nodeid)
            if label is not None:
                self.collected[label] = self.collected.get(label, 0) + 1

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        label = self.label_of(item.nodeid)
        if label is None or len(self.items) >= LIMIT:
            return
        excinfo = call.excinfo
        self.items.append({{
            "f": label,
            "n": item.nodeid.split("::", 1)[-1],
            "w": report.when,
            "o": report.outcome,
            "s": bool(report.skipped),
            "x": hasattr(report, "wasxfail"),
            "a": bool(excinfo is not None and excinfo.errisinstance(AssertionError)),
            "t": excinfo.typename if excinfo is not None else None,
        }})

    def pytest_sessionfinish(self, session, exitstatus):
        self.finished = True
        self.exitstatus = int(exitstatus)


recorder = Recorder()
paths = []
exit_code = 1
try:
    for label, payload in ENTRIES:
        fd, path = tempfile.mkstemp(prefix="collegue_acceptance_", suffix=".py", dir=TMP)
        with os.fdopen(fd, "wb") as handle:
            handle.write(base64.b64decode(payload))
        recorder.labels[os.path.basename(path)] = label
        paths.append(path)
    exit_code = pytest.main(
        [
            "--noconftest",
            "-c", "/dev/null",
            "--rootdir=" + TMP,
            "-p", "no:cacheprovider",
            "-q",
            "--tb=short",
            "--show-capture=no",  # sortie bornée : le rapport (émis en dernier) ne doit pas être noyé/tronqué
            *paths,
        ],
        plugins=[recorder],
    )
finally:
    for path in paths:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
report = {{
    "nonce": NONCE,
    "finished": recorder.finished,
    "exitstatus": recorder.exitstatus,
    "collected": recorder.collected,
    "items": recorder.items,
    "collect_errors": recorder.collect_errors,
}}
sys.stdout.write("\\n" + MARKER + ":" + NONCE + "@@" + json.dumps(report) + END + "\\n")
sys.stdout.flush()
raise SystemExit(int(exit_code))
"""


def new_nonce() -> str:
    return secrets.token_hex(16)


def oracle_pytest_command(
    entries: Sequence[Tuple[str, str]],
    nonce: str,
    *,
    workspace_dir: str = "/workspace",
    tmp_dir: str = "/tmp",
    wide_columns: str = "COLUMNS=200",
) -> str:
    """Commande pytest isolée pour ``entries`` = ``[(étiquette, source)]`` (un fichier tmpfs par source)."""
    encoded = tuple((str(label), base64.b64encode(source.encode("utf-8")).decode("ascii")) for label, source in entries)
    launcher = _LAUNCHER.format(
        entries=encoded,
        nonce=nonce,
        marker=ORACLE_MARKER,
        end=ORACLE_END,
        workspace=workspace_dir,
        tmp=tmp_dir,
        limit=MAX_REPORT_ITEMS,
    )
    return (
        f"{wide_columns} PYTHONPATH= PYTEST_ADDOPTS= PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTEST_PLUGINS= "
        f"python -I -c {shlex.quote(launcher)}"
    )


def parse_oracle_report(output: str, nonce: str) -> Optional[Dict[str, Any]]:
    """Le rapport portant EXACTEMENT ``nonce`` dans la sortie, ou ``None`` (absent, multiple, tronqué, illisible).

    Un seul rapport par nonce est légitime : le lanceur en émet exactement un. Plusieurs occurrences rendent la sortie
    non fiable (un code testé aurait pu en imprimer une copie) : ``None``, jamais « le dernier gagne ».
    """
    needle = f"{ORACLE_MARKER}:{nonce}@@"
    text = output or ""
    if text.count(needle) != 1:
        return None
    tail = text[text.find(needle) + len(needle) :]
    end = tail.find(ORACLE_END)
    if end < 0:
        return None
    try:
        report = json.loads(tail[:end])
    except ValueError:
        return None
    if not isinstance(report, dict) or report.get("nonce") != nonce or not isinstance(report.get("items"), list):
        return None
    return report


def collect_oracle_report(
    output: str, nonce: str, *, exit_code: Optional[int] = None, timed_out: bool = False
) -> Tuple[Optional[Dict[str, Any]], str]:
    """``(rapport, problème)`` : le rapport exploitable, sinon le motif PRÉCIS pour lequel il ne l'est pas.

    Contrôles : délai, absence (dont sortie tronquée par le sandbox), rapports multiples pour un même nonce, rapport
    illisible, et CONTRADICTION entre le code de sortie réel du process et le statut que le rapport déclare.
    """
    if timed_out:
        return None, "délai dépassé pendant l'exécution de l'oracle"
    text = output or ""
    count = text.count(f"{ORACLE_MARKER}:{nonce}@@")
    if count == 0:
        if "sortie tronquée" in text:
            return None, "sortie tronquée par le sandbox avant le rapport d'oracle"
        return None, "rapport d'oracle absent ou incomplet (sortie du process, délai ou sortie tronquée)"
    if count > 1:
        return None, f"{count} rapports pour le même nonce : sortie non fiable"
    report = parse_oracle_report(text, nonce)
    if report is None:
        return None, "rapport d'oracle tronqué ou illisible"
    declared = report.get("exitstatus")
    if exit_code is not None and declared is not None and int(exit_code) != int(declared):
        return None, f"code de sortie du process ({exit_code}) contradictoire avec le statut du rapport ({declared})"
    return report, ""


def judge_oracle_run(report: Optional[Dict[str, Any]], label: str, *, phase: str, problem: str = "") -> OracleRun:
    """Juge un oracle (``label``) d'après le rapport COMPLET.

    Le statut renvoyé est celui OBSERVÉ (``green`` / ``red-assertion`` / ``invalid``) ; l'appelant le compare à
    l'attendu (rouge sur la préimage d'un NOUVEAU contrat, vert sur le candidat). ``invalid`` couvre tout ce qui ne prouve rien (rapport absent, collecte, zéro test, skip, xfail,
    erreur hors assertion pour un rouge) avec le motif précis.
    """

    def invalid(reason: str, **counts: int) -> OracleRun:
        return OracleRun(phase=phase, status=STATUS_INVALID, reason=reason, **counts)

    if report is None:
        return invalid(problem or "rapport d'oracle absent ou incomplet (sortie du process, délai ou sortie tronquée)")
    if not report.get("finished") or report.get("exitstatus") not in (0, 1, 2, 5):
        return invalid(f"session pytest non terminée ou en erreur interne (statut {report.get('exitstatus')!r})")
    items = [i for i in report["items"] if i.get("f") == label]
    collection_errors = sum(1 for c in report.get("collect_errors", ()) if c.get("f") == label)
    call = [i for i in items if i.get("w") == "call"]
    phases: Dict[str, set] = {}
    for item in items:
        phases.setdefault(str(item.get("n")), set()).add(item.get("w"))
    skipped = sum(1 for i in items if i.get("s") and not i.get("x"))
    xfailed = sum(1 for i in items if i.get("s") and i.get("x"))
    xpassed = sum(1 for i in items if i.get("x") and i.get("o") == "passed")
    failed_calls = [i for i in call if i.get("o") == "failed"]
    assertion_failures = sum(1 for i in failed_calls if i.get("a"))
    other_failures = sum(1 for i in failed_calls if not i.get("a"))
    non_call_errors = sum(1 for i in items if i.get("w") != "call" and i.get("o") == "failed")
    errors = other_failures + non_call_errors
    passed = sum(1 for i in call if i.get("o") == "passed" and not i.get("x"))
    counts = {
        "executed": len(call),
        "passed": passed,
        "failed": len(failed_calls),
        "assertion_failures": assertion_failures,
        "skipped": skipped,
        "xfailed": xfailed,
        "xpassed": xpassed,
        "collection_errors": collection_errors,
        "errors": errors,
    }
    names = tuple(str(i.get("n")) for i in call)[:50]

    def run(status: str, reason: str = "") -> OracleRun:
        return OracleRun(phase=phase, status=status, reason=reason, tests=names, **counts)

    if collection_errors:
        return run(STATUS_INVALID, "erreur de collecte/d'import de l'oracle (ne prouve aucune assertion)")
    if not call:
        return run(STATUS_INVALID, "aucun test d'oracle exécuté (zéro test ou tous ignorés avant la phase call)")
    if report.get("exitstatus") in (2, 5):
        return run(STATUS_INVALID, f"session pytest interrompue (statut {report.get('exitstatus')})")
    collected = report.get("collected", {})
    collected_count = collected.get(label) if isinstance(collected, dict) else None
    if not isinstance(collected_count, int) or collected_count != len(phases):
        return run(
            STATUS_INVALID,
            f"événements incomplets : {collected_count!r} test(s) collecté(s) mais {len(phases)} avec événements "
            "(session interrompue ou rapport borné)",
        )
    unfinished = sorted(name for name, seen in phases.items() if "teardown" not in seen)
    if unfinished:
        return run(STATUS_INVALID, "cycle de vie incomplet (phase teardown absente) : " + ", ".join(unfinished[:5]))
    if skipped or xfailed or xpassed:
        return run(STATUS_INVALID, "test(s) sauté(s) ou xfail/xpass : un contrat partiellement ignoré ne prouve rien")
    if errors:
        return run(
            STATUS_INVALID,
            "échec hors assertion (setup, import, exception du code testé) : pas une assertion liée au contrat",
        )
    if failed_calls:  # tous les échecs sont des AssertionError levées en phase call d'un test exécuté
        return run(STATUS_RED_ASSERTION)
    return run(STATUS_GREEN)


def run_summary(runs: Sequence[OracleRun]) -> List[str]:
    return [f"{r.phase}:{r.status}" + (f" ({r.reason})" if r.reason else "") for r in runs]
