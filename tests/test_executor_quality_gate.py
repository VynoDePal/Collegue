"""Tests E3 (#365) : gate qualité (tests sandbox + revue experte), fail-closed.

Sandbox et reviewer mockés (pas de Docker, pas de LLM) ; les variantes réelles
sont couvertes en ``integration``.
"""

import pytest

from collegue.executor import (
    ExpertReviewer,
    FakeReviewer,
    IssueSpec,
    QualityReport,
    Reviewer,
    ReviewFindingLite,
    outcome_from_review,
    run_quality_gate,
)
from collegue.sandbox import SandboxResult, SandboxUnavailable
from collegue.tools.code_review.models import CodeReviewResponse, ReviewFinding
from collegue.tools.quotas import BudgetExceeded

ISSUE = IssueSpec(number=5, title="T")
DIFF = "diff --git a/x.py b/x.py\n+print('x')\n"


class _FakeSandbox:
    def __init__(self, result=None, *, raises=None):
        self._result = result
        self._raises = raises
        self.calls = []

    def run_tests(self, workspace, command="pytest -q"):
        self.calls.append((workspace, command))
        if self._raises is not None:
            raise self._raises
        return self._result


def _green():
    return _FakeSandbox(SandboxResult(exit_code=0, stdout="2 passed", stderr=""))


def _red():
    return _FakeSandbox(SandboxResult(exit_code=1, stdout="", stderr="1 failed"))


# --- verdict combiné ------------------------------------------------------------


async def test_passed_when_tests_green_and_review_ok():
    report = await run_quality_gate("/ws", DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer())
    assert report.tests_passed is True
    assert report.review_blocking is False
    assert report.review_error is None
    assert report.passed is True


async def test_failed_when_tests_red():
    report = await run_quality_gate("/ws", DIFF, ctx=None, sandbox=_red(), reviewer=FakeReviewer())
    assert report.tests_passed is False
    assert report.passed is False  # peu importe la revue


async def test_failed_when_review_blocks():
    report = await run_quality_gate("/ws", DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer(blocking=True))
    assert report.tests_passed is True
    assert report.review_blocking is True
    assert report.passed is False


async def test_default_test_command_invokes_pytest_as_module():
    """Régression #413 : le gate lance `python -m pytest` (PAS le script `pytest`).

    Le script `pytest` n'ajoute pas le répertoire de travail à ``sys.path`` ; un projet
    en layout ``src/``/``app/`` qui importe par package échouerait alors à la collecte.
    ``python -m pytest`` ajoute le cwd (la racine du workspace) → imports résolus.
    """
    sandbox = _green()
    await run_quality_gate("/ws", DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    assert sandbox.calls, "le gate doit exécuter les tests"
    _ws, command = sandbox.calls[0]
    assert command.startswith("python -m pytest"), f"commande de test inattendue: {command!r}"


# --- installation des deps du projet (#414) --------------------------------------


async def test_gate_installs_project_deps_before_tests(tmp_path):
    """#414 : le conteneur de tests est éphémère → les deps déclarées du projet
    sont installées AVANT pytest, dans la MÊME commande (même conteneur — un run
    séparé perdrait l'install, `pip --user` écrivant sous /tmp tmpfs)."""
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    sandbox = _green()
    await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    _ws, command = sandbox.calls[0]
    assert "pip install --user --no-cache-dir -q -r requirements.txt" in command
    assert command.index("pip install") < command.index("python -m pytest")


async def test_gate_installs_editable_project_when_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    sandbox = _green()
    await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    _ws, command = sandbox.calls[0]
    assert "pip install --user --no-cache-dir -q -e ." in command


async def test_gate_no_install_when_no_deps_declared(tmp_path):
    sandbox = _green()
    await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    _ws, command = sandbox.calls[0]
    assert command == "python -m pytest -q"


async def test_gate_install_failure_is_tolerated_in_command(tmp_path):
    # L'échec d'install ne court-circuite pas les tests : chaque étape est
    # encapsulée en `(... || echo ...)` — les tests tournent quand même et la
    # cause reste visible dans la sortie du gate.
    (tmp_path / "requirements.txt").write_text("x\n", encoding="utf-8")
    sandbox = _green()
    await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    _ws, command = sandbox.calls[0]
    assert "|| echo" in command


async def test_gate_install_deps_opt_out(tmp_path):
    (tmp_path / "requirements.txt").write_text("x\n", encoding="utf-8")
    sandbox = _green()
    await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer(), install_deps=False)
    _ws, command = sandbox.calls[0]
    assert "pip install" not in command


# --- gate frontend (#438) ---------------------------------------------------------


def _write_pkg(tmp_path, scripts=None, dev_deps=None):
    import json as _json

    payload = {"name": "front", "scripts": scripts or {}}
    if dev_deps:
        payload["devDependencies"] = dev_deps
    (tmp_path / "package.json").write_text(_json.dumps(payload), encoding="utf-8")


def test_frontend_gate_command_none_without_package_json(tmp_path):
    from collegue.executor import frontend_gate_command

    assert frontend_gate_command(str(tmp_path)) is None


def test_frontend_gate_command_install_build_and_real_tests(tmp_path):
    from collegue.executor import frontend_gate_command

    _write_pkg(tmp_path, scripts={"build": "tsc && vite build", "test": "vitest run"})
    command = frontend_gate_command(str(tmp_path))
    assert "npm ci" in command and "npm install" in command  # install + repli
    assert "npm run build" in command
    assert "npm test" in command
    assert "CI=true" in command  # neutralise les modes watch
    assert "NPM_CONFIG_CACHE=/tmp/.npm" in command  # rootfs read-only (#414)
    # fail-closed : chaque étape conditionne la suivante
    assert command.index("npm ci") < command.index("npm run build") < command.index("npm test")
    assert "&&" in command


def test_frontend_gate_command_skips_npm_default_test_stub(tmp_path):
    # Le stub `npm init` (« no test specified… exit 1 ») ferait échouer TOUT
    # projet front sans tests : il n'est jamais lancé.
    from collegue.executor import frontend_gate_command

    _write_pkg(tmp_path, scripts={"build": "vite build", "test": 'echo "Error: no test specified" && exit 1'})
    command = frontend_gate_command(str(tmp_path))
    assert "npm test" not in command


def test_frontend_gate_command_typecheck_fallback_without_build_script(tmp_path):
    # Pas de script build mais TypeScript déclaré + tsconfig → `tsc --noEmit`
    # (le bug InvoiceForm.tsx aurait été bloqué dès la première PR fautive).
    from collegue.executor import frontend_gate_command

    _write_pkg(tmp_path, scripts={}, dev_deps={"typescript": "^5"})
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    command = frontend_gate_command(str(tmp_path))
    assert "tsc --noEmit" in command

    # …mais pas de type-check si TypeScript n'est pas déclaré (rien à vérifier).
    (tmp_path / "tsconfig.json").unlink()
    assert "tsc" not in frontend_gate_command(str(tmp_path))


def test_frontend_gate_command_tolerates_invalid_package_json(tmp_path):
    # JSON invalide : on garde l'install (npm signalera lui-même l'erreur) —
    # fail-closed plutôt qu'un gate silencieusement vert.
    from collegue.executor import frontend_gate_command

    (tmp_path / "package.json").write_text("{pas du json", encoding="utf-8")
    command = frontend_gate_command(str(tmp_path))
    assert command is not None and "npm ci" in command


async def test_gate_chains_frontend_after_pytest(tmp_path):
    """#438 : avec un package.json, la passe frontend est enchaînée fail-closed
    (`&&`) après pytest, dans la MÊME commande (même conteneur) — sans elle,
    « tests verts » signifiait « tests *Python* verts » même sur un diff front."""
    _write_pkg(tmp_path, scripts={"build": "vite build"})
    sandbox = _green()
    await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    _ws, command = sandbox.calls[0]
    assert "python -m pytest" in command and "npm run build" in command
    assert command.index("pytest") < command.index("npm run build")
    assert ") && " in command  # la passe front rend le gate ROUGE si elle échoue


async def test_gate_frontend_opt_out(tmp_path):
    _write_pkg(tmp_path, scripts={"build": "vite build"})
    sandbox = _green()
    await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer(), frontend_gate=False)
    _ws, command = sandbox.calls[0]
    assert "npm" not in command


# --- fail-closed ----------------------------------------------------------------


async def test_reviewer_exception_is_fail_closed():
    reviewer = FakeReviewer(raises=RuntimeError("LLM indisponible"))
    report = await run_quality_gate("/ws", DIFF, ctx=None, sandbox=_green(), reviewer=reviewer)
    assert report.review_error is not None
    assert report.review_blocking is True  # fail-closed
    assert report.passed is False


async def test_tests_not_runnable_is_fail_closed():
    sandbox = _FakeSandbox(raises=SandboxUnavailable("docker absent"))
    report = await run_quality_gate("/ws", DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    assert report.tests_passed is False
    assert "non exécutables" in report.test_output
    assert report.passed is False


async def test_budget_exception_propagates_through_gate():
    # BudgetExceeded est une BaseException : le `except Exception` du gate NE doit
    # PAS l'avaler (cf. C4) ; elle remonte pour stopper la boucle.
    reviewer = FakeReviewer(raises=BudgetExceeded("cost", 10.0, 5.0))
    with pytest.raises(BudgetExceeded):
        await run_quality_gate("/ws", DIFF, ctx=None, sandbox=_green(), reviewer=reviewer)


# --- to_markdown : anti-injection ----------------------------------------------


def test_to_markdown_neutralizes_fence_and_heading_injection():
    report = QualityReport(
        tests_passed=True,
        test_exit_code=0,
        test_output="ok\n## pas une section",
        review_summary="```python\n## fausse section injectée",
        review_findings=(ReviewFindingLite(category="security", severity="critical", title="x\n```\n# evil"),),
        review_blocking=True,
        passed=False,
    )
    md = report.to_markdown()
    # Le texte de revue/tests ne peut pas démarrer une nouvelle section.
    assert "\n## fausse section" not in md
    assert "\n# evil" not in md
    # Les fences hostiles sont neutralisées (remplacées).
    assert "ʼʼʼ" in md
    # La structure de fences reste celle qu'on émet (4 lignes : 2 ouvrantes
    # ```text + 2 fermantes ```), aucune fence injectée par le contenu hostile.
    fences = [line for line in md.splitlines() if line.strip() in ("```", "```text")]
    assert len(fences) == 4
    # Le contenu reste présent (juste désarmé) + verdict affiché.
    assert "fausse section injectée" in md
    assert "❌ NON PASSÉ" in md


# --- outcome_from_review (mapping pur) ------------------------------------------


def _response(score, findings=()):
    return CodeReviewResponse(quality_score=score, findings=list(findings), summary="résumé", language="python")


def test_outcome_blocking_below_quality_threshold():
    outcome = outcome_from_review(_response(0.3))
    assert outcome.blocking is True


def test_outcome_not_blocking_above_threshold():
    outcome = outcome_from_review(_response(0.9))
    assert outcome.blocking is False
    assert outcome.quality_score == 0.9


def test_outcome_blocking_on_critical_finding_even_if_score_high():
    crit = ReviewFinding(category="security", severity="critical", title="RCE", description="d")
    outcome = outcome_from_review(_response(0.95, [crit]))
    assert outcome.blocking is True
    assert outcome.findings[0].severity == "critical"


def test_outcome_blocking_on_error_finding_even_if_score_high():
    # Un finding `error` sur un gros diff (score ~0.96) doit bloquer : sinon le gate
    # serait laxiste (le score normalisé par la taille noierait l'erreur).
    err = ReviewFinding(category="security", severity="error", title="injection", description="d")
    outcome = outcome_from_review(_response(0.96, [err]))
    assert outcome.blocking is True


def test_outcome_not_blocking_on_warning_only():
    warn = ReviewFinding(category="naming", severity="warning", title="nom", description="d")
    outcome = outcome_from_review(_response(0.9, [warn]))
    assert outcome.blocking is False


# --- ExpertReviewer : mapping via l'outil (tool factice) ------------------------


class _FakeTool:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def execute_async(self, request, ctx=None):
        self.calls.append((request, ctx))
        return self._response


async def test_expert_reviewer_maps_tool_response():
    crit = ReviewFinding(category="security", severity="critical", title="RCE", description="d")
    tool = _FakeTool(_response(0.4, [crit]))
    reviewer = ExpertReviewer(tool=tool)
    outcome = await reviewer.review(DIFF, ctx=None, issue=ISSUE)
    assert outcome.blocking is True
    assert outcome.summary == "résumé"
    # La requête a bien porté le diff et le contexte de l'issue.
    request = tool.calls[0][0]
    assert request.code == DIFF
    assert request.context == ISSUE.to_prompt()
    # Langage détecté depuis l'extension du diff (#409), pas codé en dur.
    assert request.language == "python"


async def test_review_skipped_for_unsupported_language_diff():
    """#409 : un diff sans fichier de code supporté (SQL seul) → revue IGNORÉE et
    NON bloquante ; l'outil n'est PAS appelé (plus de faux 0.00 sur du non-code)."""
    sql_diff = "diff --git a/schema.sql b/schema.sql\n+CREATE TABLE t (id INT);\n"
    tool = _FakeTool(_response(0.0))  # bloquerait s'il était appelé
    reviewer = ExpertReviewer(tool=tool)
    outcome = await reviewer.review(sql_diff, ctx=None, issue=ISSUE)
    assert outcome.blocking is False
    assert tool.calls == []  # outil non sollicité


async def test_review_detects_language_from_diff():
    """#409 : le langage envoyé à code_review est détecté depuis les extensions du diff."""
    ts_diff = "diff --git a/app/x.ts b/app/x.ts\n+export const a = 1;\n"
    tool = _FakeTool(_response(0.9))
    reviewer = ExpertReviewer(tool=tool)
    await reviewer.review(ts_diff, ctx=None, issue=ISSUE)
    assert tool.calls[0][0].language == "typescript"


# --- protocole ------------------------------------------------------------------


def test_reviewer_protocol_runtime_checkable():
    assert isinstance(FakeReviewer(), Reviewer)
    assert isinstance(ExpertReviewer(), Reviewer)
    assert not isinstance(object(), Reviewer)
