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
    assert command.startswith("COLUMNS=220 python -m pytest"), f"commande de test inattendue: {command!r}"


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
    assert command == "COLUMNS=220 python -m pytest -q"


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


# --- gate frontend en sous-répertoire (#457, layout monorepo) ----------------------


def test_frontend_dirs_detects_root_and_first_level(tmp_path):
    """#457 : la racine d'abord (comportement #438), puis les sous-répertoires de
    1er niveau — node_modules et répertoires cachés ignorés, tri déterministe."""
    from collegue.executor.quality_gate import _frontend_dirs

    _write_pkg(tmp_path, scripts={})
    for sub in ("frontend", "web", "node_modules", ".hidden"):
        (tmp_path / sub).mkdir()
        _write_pkg(tmp_path / sub, scripts={})
    (tmp_path / "backend").mkdir()  # sans package.json → ignoré
    assert _frontend_dirs(str(tmp_path)) == [".", "frontend", "web"]


def test_frontend_dirs_empty_without_any_package_json(tmp_path):
    from collegue.executor.quality_gate import _frontend_dirs

    (tmp_path / "backend").mkdir()
    assert _frontend_dirs(str(tmp_path)) == []


def test_frontend_gate_command_subdir_runs_inside_it(tmp_path):
    """#457 : pour un package.json en sous-répertoire, la commande `cd` dedans et
    lit SES scripts/tsconfig (pas ceux de la racine)."""
    from collegue.executor import frontend_gate_command

    front = tmp_path / "frontend"
    front.mkdir()
    _write_pkg(front, scripts={}, dev_deps={"typescript": "^5"})
    (front / "tsconfig.json").write_text("{}", encoding="utf-8")
    command = frontend_gate_command(str(tmp_path), subdir="frontend")
    assert command is not None
    assert "cd -- frontend && " in command
    assert "tsc --noEmit" in command  # tsconfig du SOUS-répertoire détecté
    assert command.index("cd -- frontend") < command.index("npm ci")

    # Pas de package.json dans le sous-répertoire → None.
    assert frontend_gate_command(str(tmp_path), subdir="autre") is None


async def test_gate_runs_frontend_pass_for_subdir_package_json(tmp_path):
    """#457 (cas réel FacNor v3) : package.json livré dans frontend/ SEULEMENT →
    la passe npm doit tourner quand même (42 erreurs TS mergées tout gate vert)."""
    front = tmp_path / "frontend"
    front.mkdir()
    _write_pkg(front, scripts={"build": "vite build"})
    sandbox = _green()
    await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    _ws, command = sandbox.calls[0]
    assert "cd -- frontend && " in command
    assert "npm run build" in command
    assert command.index("pytest") < command.index("npm run build")
    assert ") && " in command  # fail-closed : l'échec npm rend le gate rouge


async def test_gate_runs_one_frontend_pass_per_detected_dir(tmp_path):
    """#457 : racine + sous-répertoire → une passe npm PAR répertoire détecté."""
    _write_pkg(tmp_path, scripts={"build": "vite build"})
    front = tmp_path / "frontend"
    front.mkdir()
    _write_pkg(front, scripts={"build": "tsc && vite build"})
    sandbox = _green()
    await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    _ws, command = sandbox.calls[0]
    assert command.count("npm run build") == 2
    assert "cd -- frontend && " in command


# --- pytest exit 5 : « aucun test collecté » (#463) --------------------------------


def test_tolerate_exit5_shell_semantics():
    """Le wrapper EXÉCUTÉ : exit 5 → 0 (+ note), exit 1 → 1, exit 0 → 0."""
    import subprocess

    from collegue.executor.quality_gate import _EXIT5_NOTE, _tolerate_pytest_exit5

    def run(inner):
        return subprocess.run(["sh", "-c", _tolerate_pytest_exit5(inner)], capture_output=True, text=True)

    five = run("exit 5")
    assert five.returncode == 0
    assert _EXIT5_NOTE in five.stdout
    assert run("exit 1").returncode == 1
    ok = run("echo vert")
    assert ok.returncode == 0 and _EXIT5_NOTE not in ok.stdout


async def test_gate_tolerates_exit5_when_frontend_pass_covers(tmp_path):
    """#463 (cas réel FacNor v3) : tâche frontend sans test pytest → exit 5
    toléré (la passe npm couvre), enchaînement préservé."""
    front = tmp_path / "frontend"
    front.mkdir()
    _write_pkg(front, scripts={"build": "vite build"})
    sandbox = _green()
    await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    _ws, command = sandbox.calls[0]
    assert "_rc=$?" in command and "-eq 5" in command  # tolérance présente
    assert command.index("-eq 5") < command.index("npm")  # ... AVANT la passe npm
    assert "cd -- frontend && " in command


async def test_gate_exit5_still_fails_without_frontend(tmp_path):
    """Fail-closed conservé : sans passe frontend, exit 5 reste un échec
    (un projet Python sans AUCUN test ne passe pas le gate en silence)."""
    sandbox = _FakeSandbox(SandboxResult(exit_code=5, stdout="no tests ran", stderr=""))
    report = await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    _ws, command = sandbox.calls[0]
    assert "-eq 5" not in command  # pas de tolérance hors couverture frontend
    assert report.tests_passed is False
    assert report.passed is False


async def test_gate_exit5_tolerance_applies_to_installability_collect(tmp_path):
    """#463 : la collecte de la passe d'installabilité (#439) renvoie aussi
    exit 5 sans test pytest — même tolérance quand le front couvre."""
    front = tmp_path / "frontend"
    front.mkdir()
    _write_pkg(front, scripts={"build": "vite build"})
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    sandbox = _green()
    await run_quality_gate(
        str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer(), check_installability=True
    )
    _ws, command = sandbox.calls[0]
    assert command.count("-eq 5") == 2  # pytest principal + collecte installabilité


# --- smoke run (#458) ---------------------------------------------------------------


def _write_fastapi_app(tmp_path, rel="main.py"):
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")


def test_detect_asgi_app_finds_fastapi_entrypoints(tmp_path):
    from collegue.executor.quality_gate import _detect_asgi_app

    assert _detect_asgi_app(str(tmp_path)) is None  # rien à détecter
    _write_fastapi_app(tmp_path, "app/main.py")
    assert _detect_asgi_app(str(tmp_path)) == "app.main:app"
    _write_fastapi_app(tmp_path, "main.py")
    assert _detect_asgi_app(str(tmp_path)) == "main:app"  # racine prioritaire

    # Un main.py SANS FastAPI n'est pas une app détectable.
    (tmp_path / "main.py").write_text("print('cli')\n", encoding="utf-8")
    (tmp_path / "app" / "main.py").write_text("print('cli')\n", encoding="utf-8")
    assert _detect_asgi_app(str(tmp_path)) is None


def test_smoke_run_command_none_without_app_nor_command(tmp_path):
    from collegue.executor import smoke_run_command

    assert smoke_run_command(str(tmp_path)) is None


def test_smoke_run_command_autodetects_fastapi(tmp_path):
    from collegue.executor import smoke_run_command

    _write_fastapi_app(tmp_path)
    command = smoke_run_command(str(tmp_path))
    assert command is not None
    assert "python -m uvicorn main:app" in command
    assert "127.0.0.1" in command and "8765" in command
    assert command.startswith("python - <<'COLLEGUE_SMOKE_458'")


def test_smoke_run_command_explicit_command_and_paths(tmp_path):
    # Commande explicite : pas besoin d'app détectable ; les chemins sont embarqués.
    from collegue.executor import smoke_run_command

    command = smoke_run_command(str(tmp_path), command="python serve.py", paths=("/health", "/factures/"))
    assert command is not None
    assert "'python serve.py'" in command
    assert "/health" in command and "/factures/" in command


def test_smoke_probe_script_is_valid_python(tmp_path):
    """La sonde embarquée doit COMPILER (une SyntaxError dans le template rendrait
    la passe rouge en prod pour une mauvaise raison)."""
    from collegue.executor.quality_gate import _smoke_probe_script

    script = _smoke_probe_script('uvicorn "x":app', ("/", "/a b"), 30.0)
    compile(script, "<smoke>", "exec")


def test_smoke_probe_script_normalizes_leading_slash():
    # Sans `/` de tête, urlopen("…:8765health") lèverait à chaque tentative et
    # brûlerait tout le délai en silence (faux « sans réponse »).
    from collegue.executor.quality_gate import _smoke_probe_script

    script = _smoke_probe_script("python serve.py", ("health", "/ok"), 5.0)
    assert "'/health'" in script and "'/ok'" in script


def _free_port():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run_probe(command, timeout=10.0, port=None, paths=("/",)):
    import subprocess
    import sys

    from collegue.executor.quality_gate import _smoke_probe_script

    script = _smoke_probe_script(command, paths, timeout, port=port or _free_port())
    return subprocess.run([sys.executable, "-"], input=script, text=True, capture_output=True, timeout=60)


def test_smoke_probe_green_against_real_server():
    """La sonde EXÉCUTÉE (pas seulement compilée) : un serveur HTTP réel qui
    répond sur / → exit 0. Port libre pour ne pas dépendre de l'état local —
    en prod la sonde tourne sur 8765 dans son conteneur isolé."""
    import sys

    port = _free_port()
    proc = _run_probe(f"{sys.executable} -m http.server {port} --bind 127.0.0.1", port=port)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "-> 200" in proc.stdout


def test_smoke_probe_red_when_server_exits():
    """Un « serveur » qui se termine aussitôt → rouge, avec le diagnostic
    « doit rester au premier plan » (et pas un simple silence)."""
    import sys

    proc = _run_probe(f"{sys.executable} -c 'pass'", timeout=5.0)
    assert proc.returncode == 1
    assert "premier plan" in proc.stdout


async def test_gate_smoke_run_appended_last(tmp_path):
    """#458 : la passe smoke-run est enchaînée fail-closed EN DERNIER (le heredoc
    clôt la commande), après les autres passes."""
    _write_fastapi_app(tmp_path)
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    sandbox = _green()
    await run_quality_gate(
        str(tmp_path),
        DIFF,
        ctx=None,
        sandbox=sandbox,
        reviewer=FakeReviewer(),
        smoke_run=True,
        check_installability=True,
    )
    _ws, command = sandbox.calls[0]
    assert "smoke run" in command
    assert "python -m uvicorn main:app" in command
    assert command.index("installabilité") < command.index("smoke run")
    assert command.rstrip().endswith("COLLEGUE_SMOKE_458")


async def test_gate_smoke_run_default_off_and_skipped_without_app(tmp_path):
    _write_fastapi_app(tmp_path)
    sandbox = _green()
    await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    _ws, command = sandbox.calls[0]
    assert "smoke" not in command  # opt-in

    bare = tmp_path / "bare"
    bare.mkdir()
    sandbox2 = _green()
    await run_quality_gate(str(bare), DIFF, ctx=None, sandbox=sandbox2, reviewer=FakeReviewer(), smoke_run=True)
    _ws, command2 = sandbox2.calls[0]
    assert "smoke" not in command2  # aucune app détectable → passe skippée


# --- installabilité du livrable (#439) ---------------------------------------------


async def test_deps_install_failure_is_surfaced_as_signal(tmp_path):
    """#439 : l'échec d'installation des deps déclarées (toléré, #414) devient un
    SIGNAL structuré — un vert obtenu grâce aux paquets de l'IMAGE n'a pas la
    même valeur qu'un vert installable partout."""
    from collegue.sandbox import SandboxResult

    (tmp_path / "requirements.txt").write_text("paquet-inexistant\n", encoding="utf-8")
    out = "[gate] installation des dépendances en échec — tests lancés quand même (#414)\n2 passed"
    sandbox = _FakeSandbox(SandboxResult(exit_code=0, stdout=out, stderr=""))
    report = await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    assert report.passed is True  # toléré par défaut (sandbox sans réseau, #414)
    assert report.deps_install_failed is True  # …mais le signal SURVIT
    markdown = report.to_markdown()
    assert "installation des dépendances déclarées EN ÉCHEC" in markdown
    assert "installabilité non prouvée" in markdown


async def test_deps_install_ok_has_no_signal(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    report = await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer())
    assert report.deps_install_failed is False
    assert "EN ÉCHEC" not in report.to_markdown()


async def test_require_deps_install_makes_install_blocking(tmp_path):
    # Mode strict (#439) : plus de `|| echo` — l'install conditionne les tests
    # (`&&`), son échec rend le gate rouge au lieu d'un vert trompeur.
    (tmp_path / "requirements.txt").write_text("x\n", encoding="utf-8")
    sandbox = _green()
    await run_quality_gate(
        str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer(), require_deps_install=True
    )
    _ws, command = sandbox.calls[0]
    assert "|| echo" not in command
    assert ") && COLUMNS=220 python -m pytest" in command


def test_installability_command_has_network_retries(tmp_path):
    """#461 : la passe dépend de PyPI — pip ré-essaie (--retries/--timeout)
    avant que le moteur ne voie un gate rouge (une micro-coupure réseau coûtait
    une tentative fonctionnelle entière)."""
    from collegue.executor import installability_command

    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    command = installability_command(str(tmp_path))
    assert command.count("--retries 5") == 2  # requirements + pytest
    assert command.count("--timeout 30") == 2


async def test_gate_pytest_commands_force_wide_columns(tmp_path):
    """#478 : en non-tty, pytest borne son short summary à COLUMNS (80 par
    défaut) et tronquait le nom du paquet manquant du feedback — toutes les
    invocations pytest du gate forcent une largeur large."""
    from collegue.executor import installability_command

    sandbox = _green()
    await run_quality_gate("/ws", DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    _ws, command = sandbox.calls[0]
    assert "COLUMNS=220 python -m pytest -q" in command

    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    install = installability_command(str(tmp_path))
    assert "COLUMNS=220 /tmp/.gate_venv/bin/python -m pytest --collect-only -q" in install


def test_installability_command_requires_requirements(tmp_path):
    from collegue.executor import installability_command

    assert installability_command(str(tmp_path)) is None
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    command = installability_command(str(tmp_path))
    # venv NU → install depuis les requirements du projet → collecte (imports).
    assert "python -m venv --clear /tmp/.gate_venv" in command
    assert "-r requirements.txt" in command
    assert "pytest --collect-only" in command
    assert " && " in command  # fail-closed à chaque étape


async def test_check_installability_appends_nude_venv_pass(tmp_path):
    """#439 : la passe d'installabilité tourne dans un venv VIERGE — une dep
    manquante du requirements.txt (masquée par l'image sandbox, ex.
    email-validator sur FacNor v2) fait enfin échouer le gate."""
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    sandbox = _green()
    await run_quality_gate(
        str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer(), check_installability=True
    )
    _ws, command = sandbox.calls[0]
    assert "installabilité" in command  # bannière
    assert "/tmp/.gate_venv" in command
    assert command.index("pytest -q") < command.index("--collect-only")  # après la passe normale

    # …et opt-out par défaut (réseau PyPI requis).
    sandbox2 = _green()
    await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox2, reviewer=FakeReviewer())
    assert ".gate_venv" not in sandbox2.calls[0][1]


# --- adéquation diff↔issue (#437) --------------------------------------------------


def test_issue_expects_code_heuristic():
    from collegue.executor import issue_expects_code

    # Fail-closed : une feature attend du code…
    assert issue_expects_code(IssueSpec(number=1, title="Implémentation du service d'export PDF")) is True
    # …un livrable explicitement non-code, non.
    assert issue_expects_code(IssueSpec(number=2, title="Documentation de l'API")) is False
    assert issue_expects_code(IssueSpec(number=3, title="Mise à jour de la configuration CI")) is False


async def test_review_blocks_phantom_delivery_diff():
    """#437 : la livraison fantôme exacte du run v2 — issue feature (« export PDF »)
    fermée par +1 ligne de requirements.txt. Le skip neutre #409 devenait la faille :
    diff sans code → revue ignorée → gate vert. Désormais BLOQUANT."""
    req_diff = "diff --git a/requirements.txt b/requirements.txt\n+reportlab\n"
    feature = IssueSpec(number=55, title="Implémentation du service d'export PDF")
    reviewer = ExpertReviewer(tool=None)  # l'outil ne doit JAMAIS être sollicité
    outcome = await reviewer.review(req_diff, ctx=None, issue=feature)
    assert outcome.blocking is True
    assert "fantôme" in outcome.summary
    assert outcome.findings and outcome.findings[0].severity == "critical"


async def test_review_still_neutral_for_expected_non_code_diff():
    # Livrable non-code ATTENDU (docs) : comportement #409 conservé (neutre).
    md_diff = "diff --git a/README.md b/README.md\n+# titre\n"
    docs_issue = IssueSpec(number=9, title="Documentation utilisateur")
    outcome = await ExpertReviewer(tool=None).review(md_diff, ctx=None, issue=docs_issue)
    assert outcome.blocking is False

    # …et sans issue fournie : neutre aussi (comportement historique).
    outcome2 = await ExpertReviewer(tool=None).review(md_diff, ctx=None)
    assert outcome2.blocking is False


async def test_adequacy_checker_blocks_gate_when_not_implemented(tmp_path):
    """#437 : tests verts + revue OK mais le diff n'implémente PAS l'issue →
    gate NON passé, justification visible dans le rapport (et donc la PR)."""
    from collegue.executor import FakeAdequacyChecker

    checker = FakeAdequacyChecker(implemented=False, justification="aucune route PDF, reportlab jamais importé")
    report = await run_quality_gate(
        str(tmp_path), DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer(), issue=ISSUE, adequacy_checker=checker
    )
    assert checker.calls == [ISSUE.number]
    assert report.tests_passed is True
    assert report.adequacy_implemented is False
    assert report.passed is False  # fail-closed
    markdown = report.to_markdown()
    assert "Adéquation à l'issue" in markdown and "NON conforme" in markdown
    assert "aucune route PDF" in markdown


async def test_adequacy_checker_pass_keeps_gate_green(tmp_path):
    from collegue.executor import FakeAdequacyChecker

    checker = FakeAdequacyChecker(implemented=True, justification="service livré et testé")
    report = await run_quality_gate(
        str(tmp_path), DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer(), issue=ISSUE, adequacy_checker=checker
    )
    assert report.passed is True
    assert report.adequacy_implemented is True


async def test_adequacy_checker_not_called_when_gate_already_red(tmp_path):
    # Économie LLM : un gate déjà rouge n'appelle pas le contrôle d'adéquation.
    from collegue.executor import FakeAdequacyChecker

    checker = FakeAdequacyChecker(implemented=True)
    report = await run_quality_gate(
        str(tmp_path), DIFF, ctx=None, sandbox=_red(), reviewer=FakeReviewer(), issue=ISSUE, adequacy_checker=checker
    )
    assert checker.calls == []
    assert report.adequacy_implemented is None
    assert report.passed is False


async def test_adequacy_checker_exception_is_fail_closed(tmp_path):
    from collegue.executor import FakeAdequacyChecker

    checker = FakeAdequacyChecker(raises=RuntimeError("LLM indisponible"))
    report = await run_quality_gate(
        str(tmp_path), DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer(), issue=ISSUE, adequacy_checker=checker
    )
    assert report.adequacy_error is not None
    assert report.passed is False
    assert "indisponible" in report.to_markdown()


def test_parse_adequacy_is_fail_closed():
    from collegue.executor.quality_gate import _parse_adequacy

    ok = _parse_adequacy('{"implemented": true, "justification": "service livré"}')
    assert ok.implemented is True and "livré" in ok.justification
    fenced = _parse_adequacy('```json\n{"implemented": false, "justification": "feature absente"}\n```')
    assert fenced.implemented is False
    assert _parse_adequacy("je ne sais pas").implemented is False  # illisible ⇒ non conforme
    assert _parse_adequacy("").implemented is False


async def test_llm_adequacy_checker_round_trip():
    from collegue.executor import LLMAdequacyChecker

    prompts = []

    async def fake_sample(prompt, system_prompt):
        prompts.append((prompt, system_prompt))
        return '{"implemented": false, "justification": "le diff ne contient que requirements.txt"}'

    checker = LLMAdequacyChecker(fake_sample)
    outcome = await checker.check("diff --git a/requirements.txt b/requirements.txt\n+x\n", ISSUE, ctx=None)
    assert outcome.implemented is False
    prompt, system = prompts[0]
    assert "Diff livré" in prompt and ISSUE.title in prompt
    assert "JSON" in system


# --- signal « aucun test touché » (#437) --------------------------------------------


def test_tests_touched_detection():
    from collegue.executor import tests_touched

    assert tests_touched("diff --git a/tests/test_api.py b/tests/test_api.py\n+x\n") is True
    assert tests_touched("diff --git a/src/InvoiceForm.test.tsx b/src/InvoiceForm.test.tsx\n+x\n") is True
    assert tests_touched("diff --git a/src/form.spec.ts b/src/form.spec.ts\n+x\n") is True
    assert tests_touched("diff --git a/app/main.py b/app/main.py\n+x\n") is False
    assert tests_touched("") is False


async def test_require_test_changes_blocks_codeless_coverage(tmp_path):
    report = await run_quality_gate(
        str(tmp_path), DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer(), require_test_changes=True
    )
    assert report.tests_touched is False
    assert report.passed is False
    assert "aucun fichier de test touché" in report.to_markdown()

    # Par défaut : simple signal, pas bloquant.
    report2 = await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer())
    assert report2.passed is True
    assert "aucun fichier de test touché" in report2.to_markdown()


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
    NON bloquante ; l'outil n'est PAS appelé (plus de faux 0.00 sur du non-code).
    (Depuis #437 : ne vaut que si l'issue n'attend PAS de code — ici données/seed.)"""
    sql_diff = "diff --git a/schema.sql b/schema.sql\n+CREATE TABLE t (id INT);\n"
    tool = _FakeTool(_response(0.0))  # bloquerait s'il était appelé
    reviewer = ExpertReviewer(tool=tool)
    data_issue = IssueSpec(number=5, title="Seed des données de démonstration")
    outcome = await reviewer.review(sql_diff, ctx=None, issue=data_issue)
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


# --- remédiation déterministe des dépendances manquantes (#481) ---------------------


class _SeqSandbox:
    """Rend les résultats en séquence (le dernier se répète) — la remédiation
    relance la même commande dans le même gate."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def run_tests(self, workspace, command="pytest -q"):
        self.calls.append((workspace, command))
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]


def _missing(module):
    return SandboxResult(
        exit_code=2,
        stdout=f"E   ModuleNotFoundError: No module named '{module}'\nERROR tests/test_x.py\n",
        stderr="",
    )


def test_installability_command_continues_on_collection_errors(tmp_path):
    """#481 : la collecte d'installabilité rapporte les erreurs de TOUS les
    fichiers au lieu de s'interrompre (le flag ne couvre pas les chaînes
    d'imports — c'est le rôle de la boucle de remédiation)."""
    from collegue.executor import installability_command

    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    assert "--continue-on-collection-errors" in installability_command(str(tmp_path))


def test_missing_modules_parsed_and_deduplicated():
    from collegue.executor.quality_gate import missing_modules

    output = (
        "E   ModuleNotFoundError: No module named 'httpx'\n"
        "E   ModuleNotFoundError: No module named 'email_validator'\n"
        "E   ModuleNotFoundError: No module named 'multipart'\n"
        "E   ModuleNotFoundError: No module named 'httpx'\n"
        "E   ModuleNotFoundError: No module named 'a.b'\n"
    )
    assert missing_modules(output) == ["httpx", "email_validator", "multipart", "a"]
    assert missing_modules("") == []
    assert missing_modules("FAILED tests/test_x.py - assert 1 == 2") == []


def test_requirement_for_module_table_and_heuristic():
    from collegue.executor.quality_gate import requirement_for_module

    assert requirement_for_module("jose") == "python-jose[cryptography]"
    assert requirement_for_module("email_validator") == "email-validator"
    assert requirement_for_module("multipart") == "python-multipart"
    assert requirement_for_module("httpx") == "httpx"
    assert requirement_for_module("mon_module") == "mon-module"


async def test_remediation_fixes_three_missing_packages_in_one_gate(tmp_path):
    """LE test de l'issue #481 : 3 paquets manquants identifiés et réparés en UN
    cycle (zéro LLM) — le run v4 brûlait un cycle LLM complet PAR paquet."""
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    red = SandboxResult(
        exit_code=2,
        stdout=(
            "E   ModuleNotFoundError: No module named 'httpx'\n"
            "E   ModuleNotFoundError: No module named 'email_validator'\n"
            "E   ModuleNotFoundError: No module named 'multipart'\n"
        ),
        stderr="",
    )
    green = SandboxResult(exit_code=0, stdout="3 passed", stderr="")
    sandbox = _SeqSandbox([red, green])
    report = await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    assert report.passed is True
    assert report.requirements_added == ("httpx", "email-validator", "python-multipart")
    content = (tmp_path / "requirements.txt").read_text(encoding="utf-8")
    assert all(pkg in content for pkg in ("fastapi", "httpx", "email-validator", "python-multipart"))
    assert len(sandbox.calls) == 2  # un seul aller-retour de remédiation


async def test_remediation_serial_import_chain_bounded(tmp_path):
    """Cas FacNor exact : une chaîne d'imports ne révèle qu'un module manquant
    par passage — la boucle bornée les égrène dans le même gate. Et un module
    qui reste manquant après ajout (paquet cassé) ne boucle pas : fail-closed."""
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    green = SandboxResult(exit_code=0, stdout="3 passed", stderr="")
    sandbox = _SeqSandbox([_missing("httpx"), _missing("email_validator"), _missing("multipart"), green])
    report = await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    assert report.passed is True
    assert report.requirements_added == ("httpx", "email-validator", "python-multipart")
    assert len(sandbox.calls) == 4

    # Toujours le même module manquant malgré l'ajout → un seul re-essai, gate
    # rouge (le paquet ajouté reste visible dans le diff pour le cycle suivant).
    (tmp_path / "requirements2").mkdir()
    (tmp_path / "requirements2" / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    stuck = _SeqSandbox([_missing("httpx")])
    report = await run_quality_gate(
        str(tmp_path / "requirements2"), DIFF, ctx=None, sandbox=stuck, reviewer=FakeReviewer()
    )
    assert report.passed is False
    assert report.requirements_added == ("httpx",)
    assert len(stuck.calls) == 2


async def test_remediation_skips_local_and_stdlib_modules(tmp_path):
    """Garde-fous : un module LOCAL du projet (layout app/ ou src/) ou stdlib ne
    devient jamais une dépendance PyPI (dependency confusion)."""
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pkg.py").write_text("")
    red = SandboxResult(
        exit_code=2,
        stdout=(
            "E   ModuleNotFoundError: No module named 'app'\n"
            "E   ModuleNotFoundError: No module named 'pkg'\n"
            "E   ModuleNotFoundError: No module named 'json'\n"
        ),
        stderr="",
    )
    sandbox = _SeqSandbox([red])
    report = await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    assert report.requirements_added == ()
    assert len(sandbox.calls) == 1
    assert (tmp_path / "requirements.txt").read_text(encoding="utf-8") == "fastapi\n"


async def test_remediation_skips_flat_import_inside_package(tmp_path):
    """Revue #481 : ``app/main.py`` qui fait ``import utils`` pour ``app/utils.py``
    lève le même ModuleNotFoundError qu'un paquet manquant — installer `utils`
    depuis PyPI serait une dependency confusion (le gate pourrait même verdir
    sur la sémantique d'un paquet étranger)."""
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("import utils\n")
    (tmp_path / "app" / "utils.py").write_text("")
    sandbox = _SeqSandbox([_missing("utils")])
    report = await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    assert report.requirements_added == ()
    assert len(sandbox.calls) == 1


async def test_remediation_skips_unreadable_requirements(tmp_path):
    """Revue #481 : un requirements.txt non-UTF8 n'est ni remédié ni corrompu —
    et surtout ne détruit pas le diagnostic du gate (cycle LLM normal)."""
    (tmp_path / "requirements.txt").write_bytes(b"# d\xe9pendances\nfastapi\n")  # latin-1
    sandbox = _SeqSandbox([_missing("httpx")])
    report = await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    assert report.requirements_added == ()
    assert "ModuleNotFoundError" in report.test_output  # diagnostic réel préservé
    assert (tmp_path / "requirements.txt").read_bytes() == b"# d\xe9pendances\nfastapi\n"


async def test_remediation_requires_requirements_txt(tmp_path):
    """Sans requirements.txt, la remédiation ne crée pas le contrat d'install."""
    sandbox = _SeqSandbox([_missing("httpx")])
    report = await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    assert report.requirements_added == ()
    assert len(sandbox.calls) == 1


async def test_remediation_never_duplicates_declared_package(tmp_path):
    """Un paquet déjà déclaré (pin, extras) n'est jamais dupliqué — s'il manque
    quand même, la cause est ailleurs (pin cassé) : cycle LLM normal."""
    (tmp_path / "requirements.txt").write_text("httpx==0.27.0\npython-jose[cryptography]\n", encoding="utf-8")
    red = SandboxResult(
        exit_code=2,
        stdout=("E   ModuleNotFoundError: No module named 'httpx'\nE   ModuleNotFoundError: No module named 'jose'\n"),
        stderr="",
    )
    sandbox = _SeqSandbox([red])
    report = await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    assert report.requirements_added == ()
    assert len(sandbox.calls) == 1


async def test_remediation_opt_out(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    sandbox = _SeqSandbox([_missing("httpx")])
    report = await run_quality_gate(
        str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer(), fix_missing_requirements=False
    )
    assert report.requirements_added == ()
    assert len(sandbox.calls) == 1
    assert (tmp_path / "requirements.txt").read_text(encoding="utf-8") == "fastapi\n"


def test_to_markdown_mentions_added_requirements():
    report = QualityReport(
        tests_passed=True,
        test_exit_code=0,
        test_output="ok",
        review_summary="",
        review_findings=(),
        review_blocking=False,
        passed=True,
        requirements_added=("httpx", "python-multipart"),
    )
    markdown = report.to_markdown()
    assert "ajoutées automatiquement" in markdown
    assert "`httpx`" in markdown and "`python-multipart`" in markdown
    report_clean = QualityReport(
        tests_passed=True,
        test_exit_code=0,
        test_output="ok",
        review_summary="",
        review_findings=(),
        review_blocking=False,
        passed=True,
    )
    assert "ajoutées automatiquement" not in report_clean.to_markdown()


# --- garde append-only requirements.txt (#482) --------------------------------------


REQ_REMOVAL_DIFF = (
    "diff --git a/requirements.txt b/requirements.txt\n"
    "--- a/requirements.txt\n"
    "+++ b/requirements.txt\n"
    "@@ -1,4 +1,2 @@\n"
    " fastapi\n"
    "-python-jose[cryptography]\n"
    "-passlib[bcrypt]\n"
    "+httpx\n"
)


def test_removed_requirement_lines_net_removals():
    """#482 : seules les dépendances qui DISPARAISSENT comptent — pin changé,
    réordonnancement, commentaires, options et autres fichiers sont ignorés."""
    from collegue.executor import removed_requirement_lines

    assert removed_requirement_lines(REQ_REMOVAL_DIFF) == ("python-jose[cryptography]", "passlib[bcrypt]")
    # Changement de pin : le NOM survit → pas une suppression.
    pin = (
        "diff --git a/requirements.txt b/requirements.txt\n"
        "--- a/requirements.txt\n+++ b/requirements.txt\n"
        "-fastapi==0.100\n+fastapi==0.110\n"
    )
    assert removed_requirement_lines(pin) == ()
    # Commentaires et lignes vides n'ont pas de nom.
    noise = (
        "diff --git a/requirements.txt b/requirements.txt\n"
        "--- a/requirements.txt\n+++ b/requirements.txt\n"
        "-# anciennes deps\n-\n"
    )
    assert removed_requirement_lines(noise) == ()
    # Un fichier python qui perd un import n'est PAS requirements.txt.
    code = "diff --git a/app/x.py b/app/x.py\n--- a/app/x.py\n+++ b/app/x.py\n-import jose\n"
    assert removed_requirement_lines(code) == ()
    assert removed_requirement_lines("") == ()
    # Monorepo (#457) : backend/requirements.txt compte aussi (basename).
    nested = (
        "diff --git a/backend/requirements.txt b/backend/requirements.txt\n"
        "--- a/backend/requirements.txt\n+++ b/backend/requirements.txt\n"
        "-python-jose[cryptography]\n"
    )
    assert removed_requirement_lines(nested) == ("python-jose[cryptography]",)


async def test_requirements_removal_blocks_gate_with_named_lines():
    """#482 (le symptôme exact) : 12 tests verts n'achètent plus le gate quand
    des lignes de requirements de la base ont été perdues — gate rouge avec les
    lignes NOMMÉES dans le rapport."""
    report = await run_quality_gate(
        "/ws", REQ_REMOVAL_DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer(), issue=ISSUE
    )
    assert report.tests_passed is True
    assert report.passed is False
    assert report.requirements_removed == ("python-jose[cryptography]", "passlib[bcrypt]")
    markdown = report.to_markdown()
    assert "append-only" in markdown
    assert "python-jose[cryptography]" in markdown


async def test_requirements_removal_allowed_when_issue_names_package():
    """« Sans que l'issue le demande » : si l'issue nomme le paquet, la
    suppression est légitime."""
    issue = IssueSpec(number=7, title="Retirer les dépendances python-jose et passlib devenues inutiles")
    report = await run_quality_gate(
        "/ws", REQ_REMOVAL_DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer(), issue=issue
    )
    assert report.requirements_removed == ()
    assert report.passed is True


async def test_requirements_guard_opt_out():
    report = await run_quality_gate(
        "/ws",
        REQ_REMOVAL_DIFF,
        ctx=None,
        sandbox=_green(),
        reviewer=FakeReviewer(),
        issue=ISSUE,
        requirements_guard=False,
    )
    assert report.requirements_removed == ()
    assert report.passed is True


async def test_requirements_removal_skips_adequacy_call():
    """Économie LLM : la garde rouge saute l'appel d'adéquation (#437), comme
    tout gate déjà rouge."""

    class _CountingChecker:
        def __init__(self):
            self.calls = 0

        async def check(self, diff, issue, ctx):
            self.calls += 1
            raise AssertionError("ne doit pas être appelé")

    checker = _CountingChecker()
    report = await run_quality_gate(
        "/ws",
        REQ_REMOVAL_DIFF,
        ctx=None,
        sandbox=_green(),
        reviewer=FakeReviewer(),
        issue=ISSUE,
        adequacy_checker=checker,
    )
    assert report.passed is False
    assert checker.calls == 0


def test_removed_requirement_lines_flags_extras_downgrade():
    """Revue #482 : perdre un extra (`passlib[bcrypt]` → `passlib`) prive le venv
    nu de la dépendance réelle — invisible à la collecte #439 (backends lazy) :
    c'est une suppression."""
    from collegue.executor import removed_requirement_lines

    downgrade = (
        "diff --git a/requirements.txt b/requirements.txt\n"
        "--- a/requirements.txt\n+++ b/requirements.txt\n"
        "-passlib[bcrypt]\n+passlib\n"
    )
    assert removed_requirement_lines(downgrade) == ("passlib[bcrypt]",)
    # Pin changé, extras conservés → pas une suppression.
    pin = (
        "diff --git a/requirements.txt b/requirements.txt\n"
        "--- a/requirements.txt\n+++ b/requirements.txt\n"
        "-passlib[bcrypt]==1.7.4\n+passlib[bcrypt]==1.7.5\n"
    )
    assert removed_requirement_lines(pin) == ()


def test_removed_requirement_lines_flags_option_lines():
    """Revue #482 : perdre `-r base.txt` jette tout un fichier de dépendances —
    les lignes d'option comptent ; l'issue peut les justifier en nommant le
    fichier."""
    from collegue.executor import removed_requirement_lines, unjustified_requirement_removals

    option = (
        "diff --git a/requirements.txt b/requirements.txt\n"
        "--- a/requirements.txt\n+++ b/requirements.txt\n"
        "--r base.txt\n+httpx\n"
    )
    assert removed_requirement_lines(option) == ("-r base.txt",)
    issue = IssueSpec(number=7, title="Fusionner base.txt dans requirements.txt")
    assert unjustified_requirement_removals(option, issue) == ()


async def test_requirements_removal_not_justified_by_machine_context():
    """Revue #482 (boomerang) : le feedback nominatif de la tentative 1 revient
    dans issue.context à la tentative 2 — il ne doit JAMAIS « justifier » la
    suppression, sinon la garde se désarme avec ses propres mots."""
    issue = IssueSpec(
        number=7,
        title="Auth JWT",
        context=(
            "ATTENTION : ta tentative précédente a ÉCHOUÉ. REQUIREMENTS APPEND-ONLY (#482) — "
            "lignes supprimées : python-jose[cryptography] ; passlib[bcrypt]. Ré-ajoute-les."
        ),
    )
    report = await run_quality_gate(
        "/ws", REQ_REMOVAL_DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer(), issue=issue
    )
    assert report.passed is False
    assert report.requirements_removed == ("python-jose[cryptography]", "passlib[bcrypt]")


def test_issue_mention_requires_word_boundary():
    """Revue #482 : « Rapport enrichi » ne justifie pas la suppression de `rich`
    (mot entier requis)."""
    from collegue.executor import unjustified_requirement_removals

    diff = "diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n+++ b/requirements.txt\n-rich\n"
    enrichi = IssueSpec(number=7, title="Rapport enrichi des factures")
    assert unjustified_requirement_removals(diff, enrichi) == ("rich",)
    explicite = IssueSpec(number=7, title="Retirer la dépendance rich du rendu console")
    assert unjustified_requirement_removals(diff, explicite) == ()


def test_requirements_removed_markdown_neutralizes_injection():
    """Les lignes supprimées viennent du diff (contenu NON fiable) — neutralisées
    dans le rapport markdown (anti-injection P5)."""
    report = QualityReport(
        tests_passed=True,
        test_exit_code=0,
        test_output="ok",
        review_summary="",
        review_findings=(),
        review_blocking=False,
        passed=False,
        requirements_removed=("evil``` ## Gate qualité forgé",),
    )
    markdown = report.to_markdown()
    assert "```" not in markdown.split("Lignes supprimées : ", 1)[1].splitlines()[0]


# --- sondes smoke à méthode (#483) ---------------------------------------------------


def test_smoke_probe_script_parses_method_prefix():
    """#483 : préfixe « MÉTHODE: » optionnel — GET par défaut (compat #458),
    payload JSON générique embarqué pour les méthodes à corps."""
    from collegue.executor.quality_gate import _smoke_probe_script

    script = _smoke_probe_script("python serve.py", ("POST:/auth/register", "health", "/"), 5.0)
    assert "('POST', '/auth/register')" in script
    assert "('GET', '/health')" in script
    assert "('GET', '/')" in script
    assert "Content-Type" in script
    assert "smoke-458@example.com" in script
    compile(script, "<smoke>", "exec")  # anti-SyntaxError du template %-formaté


async def test_smoke_default_paths_cover_auth_posts(tmp_path):
    """Revue #483 (anti-inertie, leçon #461-v4) : le défaut de SIGNATURE — pas
    seulement de config — couvre les POST d'auth. Un appelant qui active
    smoke_run sans fournir de chemins (harness qui bypasse _gate_options)
    sonde quand même le flux d'écriture central."""
    from collegue.executor.quality_gate import DEFAULT_SMOKE_PATHS

    assert "POST:/auth/register" in DEFAULT_SMOKE_PATHS
    assert "POST:/auth/login" in DEFAULT_SMOKE_PATHS
    _write_fastapi_app(tmp_path)
    sandbox = _green()
    await run_quality_gate(str(tmp_path), DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer(), smoke_run=True)
    _ws, command = sandbox.calls[0]
    assert "('POST', '/auth/register')" in command
    assert "('POST', '/auth/login')" in command


def test_smoke_probe_red_when_post_500_but_get_200(tmp_path):
    """LE cas de l'issue #483 (répété v3 puis v4) : GET / → 200 mais
    POST /auth/register → 500 out-of-the-box — le smoke doit être ROUGE."""
    import sys

    port = _free_port()
    server = tmp_path / "serve.py"
    server.write_text(
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
        "    def do_POST(self):\n"
        "        # simule le crash passlib/bcrypt à l'exécution du handler\n"
        "        self.send_response(500); self.end_headers()\n"
        "    def log_message(self, *a): pass\n"
        f"HTTPServer(('127.0.0.1', {port}), H).serve_forever()\n",
        encoding="utf-8",
    )
    result = _run_probe(f"{sys.executable} {server}", port=port, paths=("/", "POST:/auth/register"))
    assert result.returncode == 1
    assert "GET / -> 200" in result.stdout
    assert "POST /auth/register -> 500" in result.stdout


def test_smoke_probe_green_when_post_4xx(tmp_path):
    """#483 : un 4xx sur POST (validation du payload, modèle strict) reste
    toléré — même sémantique que les GET (#458) : l'app A répondu."""
    import sys

    port = _free_port()
    server = tmp_path / "serve.py"
    server.write_text(
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
        "    def do_POST(self):\n"
        "        self.send_response(422); self.end_headers()\n"
        "    def log_message(self, *a): pass\n"
        f"HTTPServer(('127.0.0.1', {port}), H).serve_forever()\n",
        encoding="utf-8",
    )
    result = _run_probe(f"{sys.executable} {server}", port=port, paths=("/", "POST:/auth/register"))
    assert result.returncode == 0
    assert "POST /auth/register -> 422" in result.stdout
