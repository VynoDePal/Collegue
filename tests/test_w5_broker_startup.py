"""Reprise du courtier au démarrage du VRAI serveur MCP (hors pilote) — sous-processus, vrai ``collegue.app``, faux fournisseur.

Un état abandonné par un processus disparu (émission en vol d'un producteur hors worker) est réparé AVANT la première dépense du
processus redémarré : l'émission devient inconnue et bloque le projet, un propriétaire vivant est conservé, aucune échéance n'est
ouverte, rien n'est demandé à Google. Le transport direct reste inchangé.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from w5_broker_contract import setup
from w5_broker_review_contract import _crashed_in_process_emission, _owner_row
from w5_broker_support import FakeUpstream

from collegue.broker import BrokerConfig, BrokerService
from collegue.state import ProjectStateManager

REPO = Path(__file__).resolve().parents[1]

CHILD = textwrap.dedent(
    """
    import asyncio, importlib, json, os, sys
    from w5_broker_support import FakeUpstream, chat_request
    from collegue.broker import BrokerConfig
    from collegue.broker.runtime import BrokerRuntime, install_runtime_for_tests

    upstream = FakeUpstream()
    install_runtime_for_tests(BrokerRuntime(upstream=upstream, config=BrokerConfig(), run_root=os.environ["W5_RUN"]))
    module = importlib.import_module("collegue.app")
    from fastmcp import Client
    from collegue.state import ProjectStateManager

    report = {"startup_error": None, "after": None}

    async def main():
        try:
            async with Client(module.app, timeout=60):
                pass
        except BaseException as exc:
            report["startup_error"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
            return
        if not os.environ["W5_SCOPE"]:
            return  # état vierge : rien à inspecter
        manager = ProjectStateManager.from_url(os.environ["STATE_DATABASE_URL"])
        ledger = manager.budget_ledger
        from collegue.broker import BrokerService

        service = BrokerService(ledger, upstream, config=BrokerConfig())
        scope = os.environ["W5_SCOPE"]
        report["states"] = {a: service.store.get_attempt(a).state for a in os.environ["W5_ATTEMPTS"].split(",")}
        report["blocked"] = ledger.snapshot(scope).blocked
        report["clock"] = service.persisted_deadline(scope) is not None
        try:
            await service.sampling_completion(scope, "qa", chat_request())
            report["next_call"] = "emitted"
        except BaseException as exc:
            report["next_call"] = type(exc).__name__
        report["upstream"] = {"count": len(upstream.count_calls), "generate": len(upstream.generate_calls)}

    asyncio.run(main())
    print("W5_REPORT:" + json.dumps(report))
    """
)

BROKER_ENV = {
    "LLM_TRANSPORT": "budget_broker",
    "LLM_PROVIDER": "gemini",
    "LLM_MODEL": "gemma-4-31b-it",
    "LLM_API_KEY": "AIzaFAKE-w5-startup-key-0010",
}


def run_server(tmp_path, url, env_extra, *, scope="", attempts=""):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "PYTHONPATH": f"{REPO}{os.pathsep}{REPO / 'tests'}",
        "PYTHONDONTWRITEBYTECODE": "1",
        "COLLEGUE_HOME": str(tmp_path / "home"),
        "STATE_DATABASE_URL": url,
        "OAUTH_ENABLED": "false",
        "WATCHDOG_ENABLED": "false",
        "FASTMCP_CHECK_FOR_UPDATES": "off",
        "W5_RUN": str(tmp_path / "run"),
        "W5_SCOPE": scope,
        "W5_ATTEMPTS": attempts,
        **env_extra,
    }
    proc = subprocess.run(
        [sys.executable, "-c", CHILD], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("W5_REPORT:")]
    assert proc.returncode == 0 and len(lines) == 1, (proc.returncode, proc.stdout[-1500:], proc.stderr[-2500:])
    return json.loads(lines[0].removeprefix("W5_REPORT:"))


def seed(tmp_path):
    """État laissé par un processus disparu ET par un propriétaire vivant (ce processus de test)."""
    url = f"sqlite:///{tmp_path / 'state.sqlite3'}"
    manager = ProjectStateManager.from_url(url, create=True)
    ledger, scope, _ = setup(manager)
    service = BrokerService(ledger, FakeUpstream(), config=BrokerConfig())
    now = datetime.now(timezone.utc)
    _owner_row(ledger, "own_dead", host=socket.gethostname(), pid=2**22 + 12345, ticks=1, heartbeat=now)
    dead, dead_rid = _crashed_in_process_emission(service, ledger, scope, owner_id="own_dead", name="dead")
    live_owner = BrokerService(ledger, FakeUpstream(), config=BrokerConfig())
    live_owner._ensure_owner()  # ce processus de test : vivant pour le serveur redémarré (même hôte, même pid, même date de démarrage)
    live, _ = _crashed_in_process_emission(live_owner, ledger, scope, owner_id=live_owner.owner_id, name="live")
    return url, scope, dead.attempt_id, live.attempt_id


def test_a_restarted_server_repairs_abandoned_producers_before_its_first_spend(tmp_path):
    url, scope, dead, live = seed(tmp_path)
    report = run_server(tmp_path, url, BROKER_ENV, scope=scope, attempts=f"{dead},{live}")
    assert report["startup_error"] is None
    assert report["states"] == {
        dead: "unknown",
        live: "emitting",
    }  # abandonné ⇒ inconnu ; propriétaire vivant ⇒ conservé
    assert report["blocked"] is True  # poursuite stricte refusée sur inconnue établie
    assert report["next_call"] == "BrokerBlocked"  # aucune nouvelle dépense
    assert report["upstream"] == {"count": 0, "generate": 0}  # rien demandé à Google, ni au démarrage ni ensuite
    assert report["clock"] is False  # le démarrage n'ouvre ni ne renouvelle l'échéance globale


def test_the_direct_transport_is_untouched_by_the_startup_recovery(tmp_path):
    url, scope, dead, live = seed(tmp_path)
    env = {"LLM_PROVIDER": "gemini", "LLM_MODEL": "gemma-4-31b-it", "LLM_API_KEY": "k"}
    report = run_server(tmp_path, url, env, scope=scope, attempts=f"{dead},{live}")
    assert report["startup_error"] is None and report["states"] == {dead: "emitting", live: "emitting"}
    assert report["blocked"] is False


def test_a_never_migrated_state_has_nothing_to_repair_and_the_server_starts(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.sqlite3'}"
    ProjectStateManager.from_url(url, create=False)  # engine seul : aucune table
    report = run_server(tmp_path, url, BROKER_ENV)
    assert report["startup_error"] is None


def test_an_unreadable_state_refuses_the_startup_instead_of_serving_on_a_doubtful_ledger(tmp_path):
    report = run_server(tmp_path, f"sqlite:///{tmp_path / 'absent' / 'state.sqlite3'}", BROKER_ENV)
    assert report["startup_error"] is not None
    assert (
        "OperationalError" in report["startup_error"]["type"]
        or "OperationalError" in report["startup_error"]["message"]
    )
