#!/usr/bin/env python3
"""Bilan CI du nightly d'intégration : verdict structuré, jamais vert par vacuité ou par skip.

Deux sous-commandes (bibliothèque standard uniquement) :

``junit REPORT.xml [--pytest-outcome OUTCOME]``
    Lit le rapport JUnit XML écrit par ``pytest --junitxml`` (source structurée : aucun
    parsing du texte de pytest, dont le wording n'est pas un contrat). Échoue (code 1) si :
    le rapport est absent/illisible/vide, un test a échoué ou est en erreur, aucun test n'a
    été réellement exécuté (tout skippé), ou l'étape pytest n'a pas fini en ``success``.
    Les tests skippés sont listés avec leur raison : ils n'ont pas été exécutés et ne
    constituent pas une preuve.

``e2e --result RESULT``
    Publie le statut du job « produit E2E » (``needs.product-e2e.result``). ``skipped`` reste
    un code 0 (opt-in) mais est annoncé comme NON EXÉCUTÉ, sans preuve du cycle produit.
    ``failure``, ``cancelled`` ou une valeur inconnue échouent (code 1).

Les annotations ``::error::`` / ``::warning::`` vont sur stdout ; un résumé Markdown est
ajouté à ``$GITHUB_STEP_SUMMARY`` quand la variable est définie.
"""

from __future__ import annotations

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

NO_REASON = "(sans raison indiquée)"
MAX_LISTED_REASONS = 20


class ReportError(Exception):
    """Le rapport JUnit est absent, illisible ou ne contient aucun test."""


@dataclass
class Report:
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    skip_reasons: Counter[str] = field(default_factory=Counter)
    problems: list[str] = field(default_factory=list)  # identifiants des tests en échec/erreur

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped

    @property
    def executed(self) -> int:
        return self.passed + self.failed + self.errors


def parse_junit(path: Path) -> Report:
    if not path.is_file():
        raise ReportError(f"rapport JUnit introuvable: {path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ReportError(f"rapport JUnit illisible ({path}): {exc}") from exc
    # pytest n'émet jamais de DTD : refuser toute déclaration DOCTYPE/ENTITY écarte les
    # attaques par entités (XXE, billion laughs) sans dépendre de defusedxml.
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ReportError(f"rapport JUnit refusé (DTD/entités interdites): {path}")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ReportError(f"rapport JUnit illisible ({path}): {exc}") from exc

    report = Report()
    for case in root.iter("testcase"):
        ident = "::".join(part for part in (case.get("classname"), case.get("name")) if part) or "(sans nom)"
        tags = {child.tag for child in case}
        if "error" in tags:
            report.errors += 1
            report.problems.append(ident)
        elif "failure" in tags:
            report.failed += 1
            report.problems.append(ident)
        elif "skipped" in tags:
            report.skipped += 1
            node = case.find("skipped")
            reason = ((node.get("message") or (node.text or "")).strip() if node is not None else "") or NO_REASON
            report.skip_reasons[reason] += 1
        else:
            report.passed += 1
    if report.total == 0:
        raise ReportError(f"rapport JUnit sans aucun test: {path}")
    return report


def _emit_summary(lines: list[str]) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def _bilan_junit(report_path: Path, pytest_outcome: str | None) -> int:
    problems: list[str] = []
    summary = ["## Nightly d'intégration — bilan", ""]

    try:
        report = parse_junit(report_path)
    except ReportError as exc:
        problems.append(str(exc))
        report = None

    if report is not None:
        print(
            f"Bilan integration : {report.passed} passed, {report.failed} failed, "
            f"{report.errors} errors, {report.skipped} skipped (total {report.total})"
        )
        summary += [
            "| Résultat | Nombre |",
            "|---|---|",
            f"| passed | {report.passed} |",
            f"| failed | {report.failed} |",
            f"| error | {report.errors} |",
            f"| skipped (NON exécutés) | {report.skipped} |",
            "",
        ]
        if report.failed or report.errors:
            problems.append(
                f"{report.failed} test(s) en échec et {report.errors} en erreur : " + ", ".join(report.problems[:10])
            )
        if report.executed == 0:
            problems.append("aucun test integration exécuté (tous skippés) — vérifier services et secrets")
        if report.skipped:
            print(f"::warning::{report.skipped} test(s) skippé(s) : non exécutés, donc aucune preuve pour ceux-ci.")
            summary.append("### Tests skippés — non exécutés, aucune preuve")
            for reason, count in report.skip_reasons.most_common(MAX_LISTED_REASONS):
                summary.append(f"- {count} × {reason}")
            summary.append("")

    if pytest_outcome not in (None, "", "success"):
        problems.append(f"l'étape pytest a fini en '{pytest_outcome}' (attendu: success)")

    summary.append(
        "> Ce job n'exerce PAS le cycle produit plan → run → PR → cleanup : voir le job « Statut du produit E2E »."
    )
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        summary += ["", "### ❌ Verdict : ÉCHEC", *[f"- {problem}" for problem in problems]]
        _emit_summary(summary)
        return 1

    summary += ["", "### Verdict : les tests exécutés ont réussi"]
    _emit_summary(summary)
    print("Verdict : les tests exécutés ont réussi.")
    return 0


E2E_STATUSES: dict[str, tuple[int, str, str]] = {
    "success": (0, "notice", "✅ Cycle produit **exécuté** et réussi (job `product-e2e`)."),
    "skipped": (
        0,
        "warning",
        "⚠️ **NON EXÉCUTÉ** — le job `product-e2e` a été ignoré (opt-in : la variable "
        "`INTEGRATION_E2E_ENABLED` n'est pas `true`). Ce nightly, même vert, ne prouve **PAS** le cycle "
        "produit plan → run → PR → cleanup.",
    ),
    "failure": (1, "error", "❌ **échec** du cycle produit (job `product-e2e`)."),
    "cancelled": (1, "error", "❌ cycle produit **annulé** (job `product-e2e`) : aucune preuve."),
}


def _bilan_e2e(result: str) -> int:
    code, level, message = E2E_STATUSES.get(
        result,
        (1, "error", f"❌ statut **inconnu** du job `product-e2e` : '{result}' — traité comme un échec."),
    )
    print(f"::{level}::{message.replace('**', '').replace('`', '')}")
    _emit_summary(["## Statut du produit E2E — plan → run → PR → cleanup", "", message])
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    junit = sub.add_parser("junit", help="bilan d'un rapport JUnit XML")
    junit.add_argument("report", type=Path)
    junit.add_argument("--pytest-outcome", default=None, help="outcome de l'étape pytest (steps.<id>.outcome)")

    e2e = sub.add_parser("e2e", help="statut explicite du job produit E2E")
    e2e.add_argument("--result", default="", help="needs.product-e2e.result")

    args = parser.parse_args(argv)
    if args.command == "junit":
        return _bilan_junit(args.report, args.pytest_outcome)
    return _bilan_e2e(args.result)


if __name__ == "__main__":
    sys.exit(main())
