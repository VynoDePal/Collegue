"""Cycle de vie de ``entrypoint.sh`` en mode HTTP : verdict fidèle, jamais « prêt » à tort.

Exigences vérifiées (vague 1, lot B) :

- un échec de ``fastmcp run`` (dont OAuth fail-closed) sort en NON-ZÉRO, avec le code exact ;
  le trap/cleanup ne le convertit jamais en 0 ;
- le health server seul ne suffit pas : « All services started successfully! » n'est affiché que
  quand le MCP répond réellement (2xx, ou 401/403 d'un endpoint protégé par OAuth), sinon sortie non nulle
  après une attente bornée ;
- aucun processus fils ne survit à la sortie (le cleanup tue MCP ET health server).

Le script est exécuté tel quel (``sh entrypoint.sh``) avec de faux ``python3`` (health server),
``fastmcp`` et ``curl`` placés en tête du PATH : aucun réseau, aucun vrai serveur.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "entrypoint.sh"
BANNER = "All services started successfully!"

_FAKE_HEALTH = r"""#!/bin/sh
# Faux `python3 /app/collegue/health_server.py`
echo $$ > "$STATE/health.pid"
case "${FAKE_HEALTH_MODE:-ok}" in
  crash-immediately) exit "${FAKE_HEALTH_EXIT_CODE:-5}" ;;
  never-ready) ;;
  crash-later) touch "$STATE/health_up"; sleep "${FAKE_CRASH_AFTER:-1}"; exit 6 ;;
  *) touch "$STATE/health_up" ;;
esac
trap 'exit 0' TERM INT
while :; do sleep 0.1; done
"""

_FAKE_FASTMCP = r"""#!/bin/sh
# Faux `fastmcp run ...`
echo "$@" >> "$STATE/fastmcp.args"
echo $$ > "$STATE/mcp.pid"
case "${FAKE_MCP_MODE:-serve}" in
  exit) exit "${FAKE_MCP_EXIT_CODE:-1}" ;;
  hang) ;;
  serve-then-exit) touch "$STATE/mcp_up"; sleep "${FAKE_CRASH_AFTER:-1}"; exit "${FAKE_MCP_EXIT_CODE:-3}" ;;
  *) touch "$STATE/mcp_up" ;;
esac
trap 'exit 0' TERM INT
while :; do sleep 0.1; done
"""

# Faux curl : health -> 0 quand le faux health server est « up » ; MCP -> code HTTP configurable.
# Le faux MCP vérifie le contrat de la requête comme le vrai : sans le header Accept attendu il
# répond 406, sans initialize complet 400, hors du chemin /mcp/ 404, hors POST 405.
_FAKE_CURL = r"""#!/bin/sh
url=""; want_code=0; method=GET; accept_ok=0; body=""
while [ $# -gt 0 ]; do
  case "$1" in
    -w) want_code=1 ;;
    -X) method="$2"; shift ;;
    -H) case "$2" in
          [Aa]ccept:*application/json*text/event-stream*) accept_ok=1 ;;
        esac
        shift ;;
    -d) body="$2"; shift ;;
    http://*) url="$1" ;;
  esac
  shift
done
case "$url" in
  *:4122/*)
    [ -f "$STATE/health_up" ] || exit 7
    echo '{"status":"ok"}'; exit 0 ;;
  *:4121/*)
    if [ ! -f "$STATE/mcp_up" ]; then
      [ "$want_code" = 1 ] && printf '000'
      exit 7
    fi
    code=$(cat "$STATE/mcp_code" 2>/dev/null || echo 200)
    case "$url" in
      */mcp/) ;;
      *) code=404 ;;
    esac
    [ "$method" = POST ] || code=405
    [ "$accept_ok" = 1 ] || code=406
    case "$body" in
      *'"method":"initialize"'*'"protocolVersion"'*'"clientInfo"'*) ;;
      *) code=400 ;;
    esac
    if [ "$want_code" = 1 ]; then printf '%s' "$code"; exit 0; fi
    echo '{"jsonrpc":"2.0","id":1,"result":{}}'; exit 0 ;;
  *) echo "faux curl: URL inattendue: $*" >&2; exit 99 ;;
esac
"""


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.app_dir = tmp_path / "app"
        (self.app_dir / "collegue").mkdir(parents=True)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for name, body in (("python3", _FAKE_HEALTH), ("fastmcp", _FAKE_FASTMCP), ("curl", _FAKE_CURL)):
            path = bin_dir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "STATE": str(self.state),
            "COLLEGUE_APP_DIR": str(self.app_dir),
            "READY_POLL_INTERVAL": "0.1",
            "HEALTH_READY_ATTEMPTS": "20",
            "MCP_READY_ATTEMPTS": "20",
            "FASTMCP_LOG_LEVEL": "INFO",
        }
        self.env.pop("MCP_TRANSPORT", None)
        self.log = tmp_path / "entrypoint.out"

    def start(self, **env: str) -> subprocess.Popen:
        handle = self.log.open("w", encoding="utf-8")
        return subprocess.Popen(
            ["sh", str(ENTRYPOINT)],
            cwd=self.root,
            env={**self.env, **env},
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def output(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def wait_for_output(self, needle: str, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.output():
                return
            time.sleep(0.05)
        raise AssertionError(f"'{needle}' absent après {timeout}s. Sortie:\n{self.output()}")

    def pid(self, name: str) -> int | None:
        path = self.state / name
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8").strip()
        return int(text) if text else None

    def assert_no_survivors(self) -> None:
        for name in ("health.pid", "mcp.pid"):
            pid = self.pid(name)
            if pid is None:
                continue
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _alive(pid):
                time.sleep(0.05)
            assert not _alive(pid), f"{name} ({pid}) survit à l'entrypoint. Sortie:\n{self.output()}"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def harness(tmp_path: Path):
    h = Harness(tmp_path)
    yield h
    for name in ("health.pid", "mcp.pid"):  # filet de sécurité : ne jamais laisser de fils
        pid = h.pid(name)
        if pid and _alive(pid):
            os.kill(pid, signal.SIGKILL)


def _finish(proc: subprocess.Popen, timeout: float = 20.0) -> int:
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise AssertionError("l'entrypoint ne s'est pas terminé dans le délai imparti")


def test_ready_only_after_health_and_mcp_answer_then_sigterm_is_clean(harness: Harness) -> None:
    proc = harness.start()
    harness.wait_for_output(BANNER)

    out = harness.output()
    assert out.index("Health server is ready!") < out.index("MCP server is ready!") < out.index(BANNER)
    assert out.count(BANNER) == 1
    assert proc.poll() is None  # le service reste en vie tant qu'on ne l'arrête pas

    proc.send_signal(signal.SIGTERM)

    assert _finish(proc) == 0  # arrêt demandé = arrêt propre
    harness.assert_no_survivors()


@pytest.mark.parametrize("mcp_exit", [1, 2, 17])
def test_fastmcp_failure_exits_with_the_exact_nonzero_code(harness: Harness, mcp_exit: int) -> None:
    """Cas OAuth fail-closed : `fastmcp run` sort en erreur dès l'import de l'application."""

    proc = harness.start(FAKE_MCP_MODE="exit", FAKE_MCP_EXIT_CODE=str(mcp_exit))

    assert _finish(proc) == mcp_exit, harness.output()
    out = harness.output()
    assert BANNER not in out
    assert "exited before becoming ready" in out
    harness.assert_no_survivors()  # le health server ne reste pas seul en vie


def test_fastmcp_exiting_zero_before_ready_is_still_a_failure(harness: Harness) -> None:
    proc = harness.start(FAKE_MCP_MODE="exit", FAKE_MCP_EXIT_CODE="0")

    assert _finish(proc) != 0, harness.output()
    assert BANNER not in harness.output()
    harness.assert_no_survivors()


def test_health_server_alone_never_declares_the_container_ready(harness: Harness) -> None:
    """MCP vivant mais jamais à l'écoute : sortie non nulle après attente bornée."""

    started = time.monotonic()
    proc = harness.start(FAKE_MCP_MODE="hang", MCP_READY_ATTEMPTS="10")

    assert _finish(proc) != 0, harness.output()
    assert time.monotonic() - started < 15
    out = harness.output()
    assert "Health server is ready!" in out
    assert BANNER not in out
    assert "not ready after" in out
    harness.assert_no_survivors()


def test_mcp_answering_5xx_is_not_ready(harness: Harness) -> None:
    (harness.state / "mcp_code").write_text("503", encoding="utf-8")

    proc = harness.start(MCP_READY_ATTEMPTS="10")

    assert _finish(proc) != 0, harness.output()
    assert BANNER not in harness.output()
    harness.assert_no_survivors()


@pytest.mark.parametrize("code", ["200", "202", "401", "403"])
def test_mcp_answering_2xx_or_auth_status_is_ready(harness: Harness, code: str) -> None:
    """401/403 : réponse attendue d'un MCP protégé par OAuth, le service est bien à l'écoute."""

    (harness.state / "mcp_code").write_text(code, encoding="utf-8")
    proc = harness.start()

    harness.wait_for_output(BANNER)
    proc.send_signal(signal.SIGTERM)

    assert _finish(proc) == 0
    harness.assert_no_survivors()


@pytest.mark.parametrize("code", ["400", "404", "405", "406", "408", "429", "500", "502", "503"])
def test_mcp_answering_wrong_path_or_contract_or_error_is_not_ready(harness: Harness, code: str) -> None:
    """404/405/406 signalent un mauvais chemin ou contrat ; 5xx une erreur serveur : jamais « prêt »."""

    (harness.state / "mcp_code").write_text(code, encoding="utf-8")

    proc = harness.start(MCP_READY_ATTEMPTS="5")

    assert _finish(proc) != 0, harness.output()
    assert BANNER not in harness.output()
    assert "not ready after" in harness.output()
    harness.assert_no_survivors()


def test_health_server_never_ready_blocks_startup_before_launching_mcp(harness: Harness) -> None:
    proc = harness.start(FAKE_HEALTH_MODE="never-ready", HEALTH_READY_ATTEMPTS="10")

    assert _finish(proc) != 0, harness.output()
    assert not (harness.state / "fastmcp.args").exists(), "le MCP ne doit pas démarrer sans health server"
    assert BANNER not in harness.output()
    harness.assert_no_survivors()


def test_health_server_crash_at_startup_propagates_its_code(harness: Harness) -> None:
    proc = harness.start(FAKE_HEALTH_MODE="crash-immediately", FAKE_HEALTH_EXIT_CODE="5")

    assert _finish(proc) == 5, harness.output()
    assert not (harness.state / "fastmcp.args").exists()


def test_mcp_crash_after_ready_propagates_the_exit_code_and_stops_the_health_server(harness: Harness) -> None:
    proc = harness.start(FAKE_MCP_MODE="serve-then-exit", FAKE_MCP_EXIT_CODE="3", FAKE_CRASH_AFTER="1")

    assert _finish(proc) == 3, harness.output()
    out = harness.output()
    assert BANNER in out  # il avait bien été prêt
    assert "MCP server exited with code 3" in out
    harness.assert_no_survivors()


def test_health_server_dying_while_serving_fails_the_container(harness: Harness) -> None:
    proc = harness.start(FAKE_HEALTH_MODE="crash-later", FAKE_CRASH_AFTER="1")

    assert _finish(proc) != 0, harness.output()
    assert "health server exited" in harness.output().lower()
    harness.assert_no_survivors()


def test_sigterm_during_startup_stops_everything_cleanly(harness: Harness) -> None:
    proc = harness.start(FAKE_MCP_MODE="hang", MCP_READY_ATTEMPTS="600", READY_POLL_INTERVAL="0.2")
    harness.wait_for_output("MCP server not ready yet")

    proc.send_signal(signal.SIGTERM)

    assert _finish(proc) == 0, harness.output()
    harness.assert_no_survivors()


def test_stdio_mode_execs_fastmcp_and_propagates_its_status(harness: Harness) -> None:
    proc = harness.start(MCP_TRANSPORT="stdio", FAKE_MCP_MODE="exit", FAKE_MCP_EXIT_CODE="7")

    assert _finish(proc) == 7, harness.output()
    args = (harness.state / "fastmcp.args").read_text(encoding="utf-8")
    assert "--transport stdio" in args
    assert not (harness.state / "health.pid").exists(), "pas de health server en mode stdio"


def _run_subcommand(harness: Harness, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(ENTRYPOINT), *args],
        cwd=harness.root,
        env=harness.env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    ("code", "expected_rc"),
    [("200", 0), ("202", 0), ("401", 0), ("403", 0)]
    + [(c, 1) for c in ("400", "404", "405", "406", "408", "429", "500", "502", "503")],
)
def test_mcp_ready_subcommand_is_the_compose_healthcheck_criterion(
    harness: Harness, code: str, expected_rc: int
) -> None:
    """`entrypoint.sh mcp-ready` : même critère que l'attente de démarrage, réutilisé par le healthcheck."""

    (harness.state / "mcp_up").touch()
    (harness.state / "mcp_code").write_text(code, encoding="utf-8")

    completed = _run_subcommand(harness, "mcp-ready")

    assert completed.returncode == expected_rc, completed.stdout + completed.stderr


def test_mcp_ready_subcommand_fails_when_nothing_listens_and_starts_nothing(harness: Harness) -> None:
    completed = _run_subcommand(harness, "mcp-ready")

    assert completed.returncode == 1
    assert not (harness.state / "fastmcp.args").exists()
    assert not (harness.state / "health.pid").exists()
