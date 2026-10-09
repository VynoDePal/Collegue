"""Échéance GLOBALE persistée appliquée au PROCESSUS du worker — pas seulement au refus de nouvelles émissions.

Le « docker » est un vrai exécutable (script Python) appelé par le VRAI ``DockerSandbox.run_command`` (vrai ``subprocess.run``,
vraie auto-limite ``timeout --signal=TERM`` de la commande interne, vrai délai hôte). Il interprète l'argv ``docker run`` (montages,
``-e`` par référence, secrets dans l'environnement du sous-process), exécute la commande interne sur l'hôte avec le socket du
courtier traduit, et journalise ce qu'il a reçu. Le « worker » est un script qui appelle le courtier (vrai SDK ``openai`` → relais →
socket Unix) puis dort / calcule / reste bloqué en vol. Aucun réseau externe, aucun Docker, aucune clé.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from w5_broker_support import FakeUpstream

from collegue.broker import BrokerConfig
from collegue.broker.runtime import BrokerRuntime
from collegue.core.llm.budget_guard import bind_budget
from collegue.executor.agent import IssueSpec
from collegue.executor.openhands_sdk_agent import OHSdkAgent
from collegue.executor.runner import _run_agent_under_budget
from collegue.executor.worker_budget import allocate_worker
from collegue.sandbox.executor import DockerSandbox, SandboxRefused
from collegue.state import BudgetRefused, ProjectStateManager

REPO = Path(__file__).resolve().parents[1]

FAKE_DOCKER = textwrap.dedent(
    """\
    #!{python}
    import json, os, subprocess, sys
    argv = sys.argv[1:]
    log = os.environ["FAKE_DOCKER_LOG"]
    def record(entry):
        with open(log, "a") as handle:
            handle.write(json.dumps(entry) + "\\n")
    if argv[0] == "version":
        sys.exit(0)
    if argv[0] == "kill":
        record({{"kill": argv[1:]}})
        sys.exit(0)
    assert argv[0] == "run", argv
    mounts, env, i = [], {{}}, 1
    value_flags = {{"--name", "--network", "--cap-drop", "--security-opt", "--pids-limit", "--memory", "--cpus",
                    "--stop-timeout", "--dns", "--tmpfs", "--user", "-v", "-e", "-w"}}
    image = None
    while i < len(argv):
        token = argv[i]
        if token in ("--rm", "--read-only"):
            i += 1
        elif token == "-v":
            host, container = argv[i + 1].split(":")[:2]
            mounts.append((host, container)); i += 2
        elif token == "-e":
            name, sep, value = argv[i + 1].partition("=")
            env[name] = value if sep else os.environ[name]   # `-e NAME` : valeur héritée du process docker (secret par référence)
            i += 2
        elif token == "-w":
            workdir = argv[i + 1]; image = argv[i + 2]; inner = argv[i + 3:]; break
        elif token in value_flags:
            i += 2
        else:
            raise SystemExit("option docker inconnue : " + token)
    def host_path(value):
        for host, container in mounts:
            if value.startswith(container):
                return host + value[len(container):]
        return value
    child_env = {{k: host_path(v) for k, v in env.items()}}
    child_env["PATH"] = os.environ.get("PATH", "")
    cwd = next((h for h, c in mounts if c == "/workspace"), None)
    record({{"run": argv, "inner": inner, "env_names": sorted(env), "image": image}})
    sys.exit(subprocess.call(inner, env=child_env, cwd=cwd))
    """
)

WORKER = textwrap.dedent(
    """\
    import importlib.util, os, sys, time
    import openai
    spec = importlib.util.spec_from_file_location("oh_broker_relay", os.environ["W5_RELAY_PATH"])
    relay = importlib.util.module_from_spec(spec); spec.loader.exec_module(relay)
    _server, port = relay.start(os.environ["COLLEGUE_BROKER_SOCKET"])
    client = openai.OpenAI(base_url=f"http://127.0.0.1:{port}/v1", api_key=os.environ["LLM_API_KEY"], max_retries=0, timeout=120)
    behaviour = os.environ.get("W5_BEHAVIOUR", "quick")
    def call():
        client.chat.completions.create(model="openai/gemma-4-31b-it", messages=[{"role": "user", "content": "x"}], max_tokens=64)
    if behaviour == "quick":
        call()
    elif behaviour == "sleep_after_call":
        call(); time.sleep(120)
    elif behaviour == "compute_after_call":
        call()
        while True:
            pass
    elif behaviour == "blocked_in_flight":
        call()   # le fournisseur ne répond jamais : requête émise, worker bloqué
    elif behaviour == "sleep_only":
        time.sleep(120)
    """
)


@pytest.fixture
def manager(tmp_path):
    return ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'w5.db'}", create=True)


class Rig:
    def __init__(self, manager, tmp_path, monkeypatch, *, behaviour, deadline=3, upstream=None, close_wait=0.5):
        docker = tmp_path / "fake-docker"
        docker.write_text(FAKE_DOCKER.format(python=sys.executable))
        docker.chmod(0o755)
        worker = tmp_path / "worker.py"
        worker.write_text(WORKER)
        self.log = tmp_path / "docker.log"
        monkeypatch.setenv("FAKE_DOCKER_LOG", str(self.log))
        self.settings = SimpleNamespace(
            LLM_PROVIDER="gemini",
            LLM_MODEL="gemma-4-31b-it",
            LLM_API_KEY="AIzaFAKE-w5-deadline-key-0007",
            LLM_TRANSPORT="budget_broker",
            CODER_SUBSCRIPTION=False,
            COLLEGUE_RUN_DEADLINE_SECONDS=0.0,
            BUDGET_WORKER_SHARE=0.5,
        )
        self.upstream = upstream or FakeUpstream()
        self.runtime = BrokerRuntime(
            upstream=self.upstream,
            config=BrokerConfig(global_deadline_seconds=deadline, close_wait_seconds=close_wait),
            run_root=str(tmp_path / "run"),
        )
        self.sandbox = DockerSandbox(
            docker_bin=str(docker),
            allow_root=True,
            network="none",
            workspace_root=str(tmp_path),
            env={
                "W5_BEHAVIOUR": behaviour,
                "W5_RELAY_PATH": str(REPO / "collegue" / "executor" / "oh_broker_relay.py"),
            },
        )
        self.agent = OHSdkAgent(
            self.sandbox,
            settings_obj=self.settings,
            broker=self.runtime,
            python_bin=sys.executable,
            runner_path=str(worker),
        )
        pid = manager.create_project(name="deadline")
        self.ledger = manager.budget_ledger
        self.scope = self.ledger.scope_for_project(pid, max_cost_usd=2.0, max_tokens=250_000).scope_key
        self.service = self.runtime.service_for(self.ledger)
        self.workspace = tmp_path / "ws"
        self.workspace.mkdir()

    def binding(self, *, window=3600):
        """Un NOUVEAU run : fenêtre locale (ici très tardive) sans rapport avec l'horloge persistée."""
        return bind_budget(
            self.ledger,
            self.scope,
            settings=self.settings,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=window),
        )

    def run(self, **kw):
        with self.binding(**kw):
            return _run_agent_under_budget(self.agent, str(self.workspace), IssueSpec(number=1, title="t", body="b"))

    def docker_calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []


def timeout_seconds_of(call) -> int:
    inner = call["inner"]
    assert inner[:2] == ["timeout", "--signal=TERM"] and inner[2].startswith("--kill-after=")
    return int(inner[3])


def test_the_worker_process_is_stopped_at_the_persisted_deadline_even_when_it_sleeps_after_its_last_call(
    manager, tmp_path, monkeypatch
):
    rig = Rig(manager, tmp_path, monkeypatch, behaviour="sleep_after_call", deadline=3)
    rig.service.open_clock(rig.scope)  # le temps global a commencé AVANT ce run (planification / canaris)
    persisted = rig.service.persisted_deadline(rig.scope)

    started = time.monotonic()
    result = rig.run(window=3600)  # fenêtre locale d'un nouveau run : une heure
    elapsed = time.monotonic() - started

    (call,) = rig.docker_calls()
    assert 1 <= timeout_seconds_of(call) <= 3  # délai du conteneur ≤ échéance persistée, pas 3600
    assert (
        elapsed < 20 and not result.success
    )  # le processus a été ARRÊTÉ (il dormait 120 s) : exit 124 / délai dépassé
    assert "délai dépassé" in result.logs
    assert datetime.now(timezone.utc) >= persisted - timedelta(seconds=1)
    # l'appel fait avant l'arrêt est réglé UNE fois ; la session est close et consolidée ; rien n'est resté réservé
    project = rig.ledger.snapshot(rig.scope)
    assert (project.consumed_tokens, project.reserved_tokens, project.unknown_tokens) == (
        15,
        0,
        0,
    ) and not project.blocked
    assert rig.service.store.open_sessions() == [] and len(rig.upstream.generate_calls) == 1


def test_a_cpu_bound_worker_after_the_last_call_is_stopped_too(manager, tmp_path, monkeypatch):
    rig = Rig(manager, tmp_path, monkeypatch, behaviour="compute_after_call", deadline=3)
    rig.service.open_clock(rig.scope)
    started = time.monotonic()
    result = rig.run(window=7200)
    assert time.monotonic() - started < 20 and not result.success
    assert timeout_seconds_of(rig.docker_calls()[0]) <= 3
    assert rig.ledger.snapshot(rig.scope).consumed_tokens == 15


def test_a_request_already_emitted_when_the_deadline_stops_the_worker_stays_reserved_as_unknown(
    manager, tmp_path, monkeypatch
):
    import asyncio

    upstream = FakeUpstream()
    upstream.gate = asyncio.Event()  # le fournisseur ne répond JAMAIS
    rig = Rig(
        manager, tmp_path, monkeypatch, behaviour="blocked_in_flight", deadline=3, upstream=upstream, close_wait=0.3
    )
    rig.service.open_clock(rig.scope)

    result = rig.run(window=3600)

    assert not result.success and result.usage_status == "incomplete" and result.usage_source == "broker"
    project = rig.ledger.snapshot(rig.scope)
    assert project.blocked and project.unknown_tokens > 0 and project.consumed_tokens == 0  # conservée, jamais libérée
    assert len(upstream.generate_calls) == 1  # une seule émission, aucune autre ensuite
    with rig.binding() as binding:
        with pytest.raises(BudgetRefused):
            allocate_worker(binding, agent=rig.agent)


def test_a_cli_resume_gets_a_fresh_local_window_but_never_a_fresh_persisted_deadline(manager, tmp_path, monkeypatch):
    rig = Rig(manager, tmp_path, monkeypatch, behaviour="quick", deadline=6)
    rig.service.open_clock(rig.scope)
    first_deadline = rig.service.persisted_deadline(rig.scope)

    rig.run(window=3600)
    time.sleep(2)
    # « nouveau run_project » : nouveau BudgetBinding à fenêtre tardive, nouveau agent / nouvelle instance de service sur la MÊME base
    resumed_runtime = BrokerRuntime(upstream=rig.upstream, config=rig.runtime.config, run_root=str(tmp_path / "run2"))
    rig.agent = OHSdkAgent(
        rig.sandbox,
        settings_obj=rig.settings,
        broker=resumed_runtime,
        python_bin=sys.executable,
        runner_path=str(tmp_path / "worker.py"),
    )
    rig.run(window=7200)

    first, second = rig.docker_calls()
    assert timeout_seconds_of(first) <= 6 and timeout_seconds_of(second) <= 4  # restant, pas 6 puis 7200
    assert resumed_runtime.service_for(rig.ledger).persisted_deadline(rig.scope) == first_deadline  # horloge inchangée
    assert rig.ledger.snapshot(rig.scope).consumed_tokens == 30


def test_after_the_deadline_no_worker_is_allocated_whatever_the_new_runs_window(manager, tmp_path, monkeypatch):
    rig = Rig(manager, tmp_path, monkeypatch, behaviour="quick", deadline=1)
    rig.service.open_clock(rig.scope)
    time.sleep(1.5)
    with rig.binding(window=7200) as binding:
        with pytest.raises(BudgetRefused) as caught:
            allocate_worker(binding, agent=rig.agent)
    assert caught.value.code == "deadline" and rig.docker_calls() == []
    # nettoyage / collecte sans génération : la fermeture et la lecture restent possibles après l'échéance
    assert rig.service.store.open_sessions() == [] and rig.ledger.snapshot(rig.scope).reserved_tokens == 0


def test_an_unopened_clock_is_opened_at_the_real_launch_and_bounds_the_container(manager, tmp_path, monkeypatch):
    rig = Rig(manager, tmp_path, monkeypatch, behaviour="sleep_only", deadline=3)
    assert rig.service.persisted_deadline(rig.scope) is None  # rien n'a encore touché le fournisseur

    started = time.monotonic()
    rig.run(window=7200)

    assert time.monotonic() - started < 20
    assert rig.service.persisted_deadline(rig.scope) is not None  # ouverte au lancement réel du worker
    assert timeout_seconds_of(rig.docker_calls()[0]) <= 3


def test_a_deadline_passed_between_allocation_and_launch_launches_nothing_and_leaves_the_parent_to_the_caller(
    manager, tmp_path, monkeypatch
):
    rig = Rig(manager, tmp_path, monkeypatch, behaviour="quick", deadline=3)
    with rig.binding() as binding:
        alloc = allocate_worker(binding, agent=rig.agent)  # la clock n'est pas ouverte : l'allocation passe
        rig.service.store.ensure_clock(
            rig.scope, 1, datetime.now(timezone.utc) - timedelta(seconds=60)
        )  # puis elle est dépassée
        with pytest.raises(SandboxRefused):
            with rig.runtime.attach_worker(ledger=rig.ledger, allocation=alloc, role="coder", sandbox=rig.sandbox):
                pytest.fail("le worker ne doit pas être lancé")
        parent = rig.ledger.get_reservation(alloc.reservation_id)
    assert parent.state == "reserved" and rig.docker_calls() == [] and rig.service.store.open_sessions() == []


def test_the_effective_container_timeout_comes_from_the_persisted_clock_not_from_the_allocation(
    manager, tmp_path, monkeypatch
):
    """Garde-fou de cohérence : le délai effectif est ``min(allocation, échéance persistée − maintenant)`` calculé AU LANCEMENT."""
    rig = Rig(manager, tmp_path, monkeypatch, behaviour="quick", deadline=1000)
    rig.service.store.ensure_clock(
        rig.scope, 1000, datetime.now(timezone.utc) - timedelta(seconds=940)
    )  # il en reste ~60 s
    with rig.binding(window=36_000) as binding:
        alloc = allocate_worker(binding, agent=rig.agent)
        assert alloc.runtime_seconds is not None and alloc.runtime_seconds <= 61  # déjà au niveau de l'allocation
        with rig.runtime.attach_worker(
            ledger=rig.ledger, allocation=alloc, role="coder", sandbox=rig.sandbox
        ) as attached:
            assert 0 < attached.timeout_seconds <= 61
            assert attached.session.deadline_at is not None
            assert attached.session.deadline_at <= rig.service.persisted_deadline(rig.scope) + timedelta(seconds=1)
