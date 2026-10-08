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
url=""; want_code=0; method=GET; accept_ok=0; body=""; max_time=""
echo "$*" >> "$STATE/curl.calls"
while [ $# -gt 0 ]; do
  case "$1" in
    --max-time) max_time="$2"; shift ;;
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
    if [ "${FAKE_CURL_STALL:-0}" = 1 ]; then
      # Connexion locale bloquée : seul --max-time (code 28) peut libérer l'appelant.
      if [ -n "$max_time" ]; then sleep 0.2; exit 28; fi
      sleep 300; exit 28
    fi
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

    def start(self, **env: str | None) -> subprocess.Popen:
        """Lance l'entrypoint ; une valeur ``None`` retire la variable (valeur par défaut du script)."""
        handle = self.log.open("w", encoding="utf-8")
        merged = {**self.env, **env}
        merged = {key: value for key, value in merged.items() if value is not None}
        return subprocess.Popen(
            ["sh", str(ENTRYPOINT)],
            cwd=self.root,
            env=merged,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,  # groupe de processus dédié : permet de prouver qu'aucun fils ne survit
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
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # tout le groupe : aucun fils orphelin
        except ProcessLookupError:
            pass
        proc.wait()
        raise AssertionError(f"l'entrypoint ne s'est pas terminé dans le délai imparti ({timeout}s)")


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


# --- Paramètres de readiness : validés AVANT de démarrer le moindre processus ------------------
#
# Défaut d'origine : `HEALTH_READY_ATTEMPTS=abc` faisait échouer `[ ... -ge abc ]` à l'intérieur
# d'un `if`, ce que `set -e` ne rattrape pas : boucle infinie (health jamais prêt) avec un
# compteur « attempt N/abc ». Aucun délai du harnais ne doit compter comme un succès ici : `_finish`
# FAIT ÉCHOUER le test si le script ne se termine pas seul.

INVALID_COUNTERS = [
    "abc",
    "0",
    "-1",
    "+5",
    "1.5",
    "1e3",
    "0x10",
    "007",
    " 5",
    "5 ",
    "5;true",
    "1000000",  # première valeur hors plage (max 999999)
    "99999999999999999999",  # déborde un entier 64 bits
    "9223372036854775808",
    "-9223372036854775809",
]
INVALID_INTERVALS = [
    "abc",
    "0",
    "0.0",
    "0.000",
    "-1",
    "+1",
    "1e1",
    ".5",
    "5.",
    "1,5",
    "inf",
    "nan",
    "1.2345",  # plus de 3 décimales
    "3601",  # juste au-dessus du maximum
    "3600.001",
    "9999",
    "10000",  # plus de 4 chiffres
    "99999999999999999999",
    " 1",
    "1 ",
]
COUNTER_CASES = [
    ("HEALTH_READY_ATTEMPTS", {"FAKE_HEALTH_MODE": "never-ready"}),
    ("MCP_READY_ATTEMPTS", {"FAKE_MCP_MODE": "hang"}),
]


def _assert_refused_before_any_process(harness: Harness, proc: subprocess.Popen, variable: str, value: str) -> None:
    started = time.monotonic()
    code = _finish(proc, timeout=10)  # un délai dépassé = échec du test, jamais un succès
    out = harness.output()

    assert code == 2, f"{variable}={value!r}: code {code}\n{out}"
    assert time.monotonic() - started < 5
    assert variable in out and "ERROR" in out
    assert not (harness.state / "health.pid").exists(), "aucun processus ne doit démarrer"
    assert not (harness.state / "fastmcp.args").exists()
    assert not (harness.state / "curl.calls").exists(), "aucune sonde avant la validation"
    assert BANNER not in out
    assert "Starting health server" not in out


@pytest.mark.parametrize("value", INVALID_COUNTERS)
@pytest.mark.parametrize(("variable", "mode"), COUNTER_CASES)
def test_invalid_attempt_counters_are_refused_before_starting_anything(
    harness: Harness, variable: str, mode: dict[str, str], value: str
) -> None:
    proc = harness.start(**{variable: value, "READY_POLL_INTERVAL": "0.01", **mode})

    _assert_refused_before_any_process(harness, proc, variable, value)


@pytest.mark.parametrize("value", INVALID_INTERVALS)
def test_invalid_poll_interval_is_refused_before_starting_anything(harness: Harness, value: str) -> None:
    proc = harness.start(READY_POLL_INTERVAL=value)

    _assert_refused_before_any_process(harness, proc, "READY_POLL_INTERVAL", value)


def test_invalid_value_is_reported_with_the_expected_format(harness: Harness) -> None:
    proc = harness.start(MCP_READY_ATTEMPTS="abc")

    assert _finish(proc, timeout=10) == 2
    assert "MCP_READY_ATTEMPTS" in harness.output()
    assert "'abc'" in harness.output()
    assert "999999" in harness.output()  # borne indiquée à l'opérateur


@pytest.mark.parametrize(
    "overrides",
    [
        {"HEALTH_READY_ATTEMPTS": "1", "MCP_READY_ATTEMPTS": "1", "READY_POLL_INTERVAL": "0.001"},
        {"HEALTH_READY_ATTEMPTS": "999999", "MCP_READY_ATTEMPTS": "999999", "READY_POLL_INTERVAL": "0.05"},
        {"HEALTH_READY_ATTEMPTS": "30", "MCP_READY_ATTEMPTS": "120", "READY_POLL_INTERVAL": "0.5"},
        {"HEALTH_READY_ATTEMPTS": "5", "MCP_READY_ATTEMPTS": "5", "READY_POLL_INTERVAL": "2.5"},
    ],
    ids=["minimum", "maximum-counters", "container-defaults-values", "decimal-interval"],
)
def test_valid_parameters_are_accepted_and_the_service_starts(harness: Harness, overrides: dict[str, str]) -> None:
    # Services déjà prêts au premier essai : valide aussi la borne basse (1 tentative).
    (harness.state / "health_up").touch()
    (harness.state / "mcp_up").touch()
    proc = harness.start(**overrides)

    harness.wait_for_output(BANNER)
    assert proc.poll() is None
    proc.send_signal(signal.SIGTERM)

    assert _finish(proc, timeout=20) == 0, harness.output()
    harness.assert_no_survivors()


@pytest.mark.parametrize("interval", ["3600", "3600.000", "60"])
def test_sigterm_is_prompt_even_with_a_long_poll_interval(harness: Harness, interval: str) -> None:
    """`docker stop` n'attend que 10 s : un long intervalle ne doit pas retarder l'arrêt propre."""

    (harness.state / "health_up").touch()
    (harness.state / "mcp_up").touch()
    proc = harness.start(READY_POLL_INTERVAL=interval)
    harness.wait_for_output(BANNER)
    time.sleep(0.3)  # laisse la boucle de surveillance entrer dans son attente

    started = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    code = _finish(proc, timeout=8)  # délai dépassé = échec du test

    assert code == 0, harness.output()
    assert time.monotonic() - started < 6
    harness.assert_no_survivors()
    assert not _group_alive(proc.pid), "aucun processus (dont le sleep d'attente) ne doit survivre"


def _group_alive(pgid: int) -> bool:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)  # ne rien laisser derrière soi
    except ProcessLookupError:
        pass
    return True


def test_unset_parameters_use_the_documented_defaults(harness: Harness) -> None:
    proc = harness.start(READY_POLL_INTERVAL=None, HEALTH_READY_ATTEMPTS=None, MCP_READY_ATTEMPTS=None)

    harness.wait_for_output(BANNER, timeout=20)
    proc.send_signal(signal.SIGTERM)

    assert _finish(proc, timeout=20) == 0
    harness.assert_no_survivors()


def test_empty_parameters_count_as_unset_and_use_the_defaults(harness: Harness) -> None:
    proc = harness.start(READY_POLL_INTERVAL="", HEALTH_READY_ATTEMPTS="", MCP_READY_ATTEMPTS="")

    harness.wait_for_output(BANNER, timeout=20)
    proc.send_signal(signal.SIGTERM)

    assert _finish(proc, timeout=20) == 0
    harness.assert_no_survivors()


def test_default_attempt_counts_are_30_for_health_and_120_for_mcp(harness: Harness) -> None:
    """Les défauts sont ceux documentés : visibles dans les messages d'attente."""

    proc = harness.start(
        READY_POLL_INTERVAL="0.01",
        HEALTH_READY_ATTEMPTS=None,
        MCP_READY_ATTEMPTS=None,
        FAKE_HEALTH_MODE="never-ready",
    )

    assert _finish(proc, timeout=20) != 0
    assert "attempt 29/30" in harness.output()
    assert "not ready after 30 attempts" in harness.output()


@pytest.mark.parametrize(("variable", "mode"), COUNTER_CASES)
def test_never_ready_services_terminate_by_themselves_with_small_valid_counters(
    harness: Harness, variable: str, mode: dict[str, str]
) -> None:
    started = time.monotonic()
    proc = harness.start(**{variable: "3", "READY_POLL_INTERVAL": "0.05", **mode})

    assert _finish(proc, timeout=15) == 1, harness.output()
    assert time.monotonic() - started < 10
    assert f"not ready after 3 attempts" in harness.output()
    assert BANNER not in harness.output()
    harness.assert_no_survivors()


def test_health_probe_is_bounded_by_max_time(harness: Harness) -> None:
    proc = harness.start(HEALTH_READY_ATTEMPTS="2", READY_POLL_INTERVAL="0.01", FAKE_HEALTH_MODE="never-ready")

    _finish(proc, timeout=15)

    health_calls = [
        line for line in (harness.state / "curl.calls").read_text(encoding="utf-8").splitlines() if ":4122/" in line
    ]
    assert health_calls
    for line in health_calls:
        tokens = line.split()
        assert "--max-time" in tokens, line
        assert 1 <= float(tokens[tokens.index("--max-time") + 1]) <= 5, line


def test_a_stalled_health_connection_cannot_hold_the_startup_forever(harness: Harness) -> None:
    """Sans --max-time, une seule connexion locale bloquée rendait le nombre FINI de tentatives inopérant."""

    started = time.monotonic()
    proc = harness.start(HEALTH_READY_ATTEMPTS="3", READY_POLL_INTERVAL="0.05", FAKE_CURL_STALL="1")

    code = _finish(proc, timeout=30)  # le fake curl bloque 300 s sans --max-time : le test échouerait ici

    assert code == 1, harness.output()
    assert time.monotonic() - started < 25
    assert "not ready after 3 attempts" in harness.output()
    harness.assert_no_survivors()


def test_mcp_probe_is_bounded_by_max_time(harness: Harness) -> None:
    proc = harness.start(MCP_READY_ATTEMPTS="2", READY_POLL_INTERVAL="0.01", FAKE_MCP_MODE="hang")

    _finish(proc, timeout=15)

    mcp_calls = [
        line for line in (harness.state / "curl.calls").read_text(encoding="utf-8").splitlines() if ":4121/" in line
    ]
    assert mcp_calls
    for line in mcp_calls:
        assert "--max-time" in line.split(), line


@pytest.mark.parametrize(
    "bad",
    [
        {"READY_POLL_INTERVAL": "abc", "HEALTH_READY_ATTEMPTS": "abc", "MCP_READY_ATTEMPTS": "-1"},
        {"READY_POLL_INTERVAL": "0", "HEALTH_READY_ATTEMPTS": "0"},
    ],
)
def test_stdio_mode_does_not_depend_on_readiness_parameters(harness: Harness, bad: dict[str, str]) -> None:
    proc = harness.start(MCP_TRANSPORT="stdio", FAKE_MCP_MODE="exit", FAKE_MCP_EXIT_CODE="7", **bad)

    assert _finish(proc, timeout=10) == 7, harness.output()
    assert "--transport stdio" in (harness.state / "fastmcp.args").read_text(encoding="utf-8")
    assert not (harness.state / "health.pid").exists()


def test_mcp_ready_subcommand_does_not_depend_on_readiness_parameters(harness: Harness) -> None:
    (harness.state / "mcp_up").touch()
    harness.env.update(READY_POLL_INTERVAL="abc", HEALTH_READY_ATTEMPTS="abc", MCP_READY_ATTEMPTS="abc")

    completed = _run_subcommand(harness, "mcp-ready")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not (harness.state / "health.pid").exists()
