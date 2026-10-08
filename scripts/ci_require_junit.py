#!/usr/bin/env python3
"""Garde CI : un rapport JUnit doit prouver que TOUS les tests attendus ont tourné, sans skip ni échec.

Sert à la preuve PostgreSQL réelle du registre de budget (``tests/test_budget_ledger_postgres.py``) dans le
job ``Pytest`` requis : un service absent, un test sauté ou désélectionné, une collecte tronquée ne doivent
jamais passer pour un succès. Bibliothèque standard uniquement ; le verdict vient de la structure du
rapport (``testcase`` / ``skipped`` / ``failure`` / ``error``), jamais du texte de pytest.

Usage : ``ci_require_junit.py REPORT.xml --min-tests N [--expect-tests M]``

Codes de sortie : 0 conforme · 1 non conforme (rapport absent/illisible, DTD, trop peu de tests, nombre
différent de l'attendu, skip, échec ou erreur) · 2 usage.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


class ReportError(Exception):
    """Le rapport est absent, illisible ou refusé."""


def read_report(path: Path) -> tuple[int, list[str], list[str]]:
    """Retourne ``(tests, skipped, failed)`` ; les deux listes contiennent les identifiants concernés."""
    if not path.is_file():
        raise ReportError(f"rapport JUnit introuvable: {path}")
    data = path.read_bytes()
    # pytest n'émet jamais de DTD : la refuser écarte les attaques par entités sans dépendance externe.
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ReportError(f"rapport JUnit refusé (DTD/entités interdites): {path}")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ReportError(f"rapport JUnit illisible ({path}): {exc}") from exc
    tests = 0
    skipped: list[str] = []
    failed: list[str] = []
    for case in root.iter("testcase"):
        tests += 1
        ident = "::".join(part for part in (case.get("classname"), case.get("name")) if part) or "(sans nom)"
        tags = {child.tag for child in case}
        if "skipped" in tags:
            skipped.append(ident)
        if "failure" in tags or "error" in tags:
            failed.append(ident)
    return tests, skipped, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", type=Path)
    parser.add_argument("--min-tests", type=int, required=True, help="plancher : moins de tests = échec")
    parser.add_argument("--expect-tests", type=int, default=None, help="nombre exact attendu (collecte)")
    args = parser.parse_args(argv)
    if args.min_tests < 1 or (args.expect_tests is not None and args.expect_tests < 1):
        print("::error::--min-tests et --expect-tests doivent valoir au moins 1", file=sys.stderr)
        return 2

    try:
        tests, skipped, failed = read_report(args.report)
    except ReportError as exc:
        print(f"::error::{exc}")
        return 1

    problems: list[str] = []
    if tests < args.min_tests:
        problems.append(f"{tests} test(s) exécuté(s) pour un minimum de {args.min_tests}")
    if args.expect_tests is not None and tests != args.expect_tests:
        problems.append(f"{tests} test(s) dans le rapport pour {args.expect_tests} collecté(s)")
    if skipped:
        problems.append(
            f"{len(skipped)} test(s) sauté(s), une preuve sautée n'est pas une preuve: {', '.join(skipped[:5])}"
        )
    if failed:
        problems.append(f"{len(failed)} test(s) en échec ou en erreur: {', '.join(failed[:5])}")

    print(f"junit {args.report}: tests={tests} skipped={len(skipped)} failed={len(failed)}")
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
