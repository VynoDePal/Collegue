"""Worker OpenHands raccordé au courtier W5 : allocation, session par allocation, consolidation, clé absente.

Le « conteneur » est simulé AU SEUL point où Docker interviendrait : ``run_command`` d'un sous-type de ``DockerSandbox`` appelle
le courtier EXACTEMENT comme le fait le conteneur (vrai SDK ``openai`` → relais loopback embarqué → socket Unix monté), après
avoir construit l'argv ``docker run`` réel (réseau none, montage unique en lecture seule). Aucun Docker, aucun réseau externe,
aucune vraie clé : la « clé Google » ci-dessous est une chaîne factice dont on prouve l'absence PARTOUT sauf dans le fournisseur.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import openai
import pytest
from w5_broker_support import FakeUpstream, google_response, http_error

from collegue.broker import BrokerConfig
from collegue.broker.runtime import BrokerRuntime
from collegue.core.llm.budget_guard import bind_budget
from collegue.executor.agent import IssueSpec
from collegue.executor.openhands_sdk_agent import OHSdkAgent
from collegue.executor.runner import _run_agent_under_budget
from collegue.executor.worker_budget import allocate_worker, settle_worker, worker_allocation
from collegue.sandbox.executor import DockerSandbox, SandboxRefused, SandboxResult, SandboxUnavailable
from collegue.state import BudgetRefused, ProjectStateManager

REPO = Path(__file__).resolve().parents[1]
GOOGLE_KEY = "AIzaFAKE-w5-provider-key-0001"
spec = importlib.util.spec_from_file_location("oh_broker_relay", REPO / "collegue" / "executor" / "oh_broker_relay.py")
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


class ContainerSandbox(DockerSandbox):
    """``DockerSandbox`` réel (argv, montages, garde W1) dont SEUL le lancement du conteneur est remplacé."""

    behaviour = None  # callable(client: openai.OpenAI, derived_sandbox) -> int exit code
    launched = None  # derniers argv construits

    def run_command(self, cmd, workspace, *, timeout=None):
        argv = self._build_run_argv(cmd if isinstance(cmd, list) else ["sh", "-c", cmd], workspace, name="fake")
        type(self).launched = {"argv": argv, "sandbox": self, "timeout": timeout}
        env = {**self.env}
        secrets = {
            k: (v.get_secret_value() if hasattr(v, "get_secret_value") else v) for k, v in self._env_secrets.items()
        }
        server, port = relay.start(f"{self.broker_socket_dir}/broker.sock")
        try:
            client = openai.OpenAI(
                base_url=f"http://127.0.0.1:{port}/v1", api_key=secrets["LLM_API_KEY"], max_retries=0, timeout=20
            )
            code = type(self).behaviour(client, env)
        finally:
            server.shutdown()
            server.server_close()
        return SandboxResult(
            exit_code=code, stdout='OH_RUNNER_DONE\n[collegue-usage] {"prompt_tokens": 9999999}', stderr=""
        )


@pytest.fixture
def manager(tmp_path):
    return ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'w5.db'}", create=True)


SETTINGS = dict(
    LLM_PROVIDER="gemini",
    LLM_MODEL="gemma-4-31b-it",
    LLM_API_KEY=GOOGLE_KEY,
    LLM_TRANSPORT="budget_broker",
    CODER_SUBSCRIPTION=False,
    COLLEGUE_RUN_DEADLINE_SECONDS=0.0,
    BUDGET_WORKER_SHARE=0.5,
)


def make_settings(**extra):
    return SimpleNamespace(**{**SETTINGS, **extra})


class Env:
    def __init__(
        self, manager, tmp_path, *, upstream=None, settings=None, sandbox=None, config=None, max_tokens=250_000
    ):
        self.settings = settings or make_settings()
        self.upstream = upstream or FakeUpstream()
        self.runtime = BrokerRuntime(
            upstream=self.upstream,
            config=config or BrokerConfig(),
            run_root=str(tmp_path / "run"),
            provider_keys=lambda: (GOOGLE_KEY,),
        )
        self.sandbox = sandbox or ContainerSandbox(
            allow_root=True, network="none", env={"LLM_MODEL": "openai/gemma-4-31b-it"}, workspace_root=str(tmp_path)
        )
        self.agent = OHSdkAgent(self.sandbox, settings_obj=self.settings, broker=self.runtime)
        self.manager = manager
        pid = manager.create_project(name="w5")
        self.ledger = manager.budget_ledger
        self.scope = self.ledger.scope_for_project(pid, max_cost_usd=2.0, max_tokens=max_tokens).scope_key
        self.db = str(tmp_path / "w5.db")
        self.workspace = tmp_path / "ws"
        self.workspace.mkdir()

    def binding(self):
        return bind_budget(self.ledger, self.scope, settings=self.settings)

    def run(self):
        with self.binding():
            return _run_agent_under_budget(self.agent, str(self.workspace), IssueSpec(number=1, title="t", body="b"))


@pytest.fixture
def env(manager, tmp_path):
    ContainerSandbox.behaviour = None
    return Env(manager, tmp_path)


def ask(client, model="openai/gemma-4-31b-it", n=1):
    for _ in range(n):
        client.chat.completions.create(model=model, messages=[{"role": "user", "content": "x"}], max_tokens=64)
    return 0


# ── reproduction de l'état ACTUEL (mode direct) puis capacité du mode courtier ──────────────────────────────


def test_baseline_direct_mode_exposes_the_provider_key_to_the_sandbox_and_is_refused_under_strict_caps(
    manager, tmp_path
):
    from collegue.pilot import runtime as pilot_runtime

    direct = make_settings(LLM_TRANSPORT="direct")
    assert pilot_runtime._coder_sandbox_secrets(direct) == {
        "LLM_API_KEY": GOOGLE_KEY
    }  # la clé Google entre dans le conteneur
    agent = OHSdkAgent(ContainerSandbox(allow_root=True), settings_obj=direct)  # historique : garde in-runner
    ledger = manager.budget_ledger
    scope = ledger.scope_for_project(manager.create_project(name="d"), max_cost_usd=2.0, max_tokens=250_000).scope_key
    with bind_budget(ledger, scope, settings=direct) as binding:
        with pytest.raises(BudgetRefused) as caught:
            allocate_worker(binding, agent=agent)
    assert caught.value.code == "unbounded_transport"  # clé facturable + plafond strict : transport non borné


def test_broker_mode_gives_the_container_no_key_and_no_network(manager, tmp_path):
    from collegue.pilot import runtime as pilot_runtime

    kwargs = pilot_runtime._coder_sandbox_kwargs(make_settings())
    assert (
        kwargs["env_secrets"] == {} and kwargs["network"] == "none" and kwargs["dns"] == () if "dns" in kwargs else True
    )
    assert GOOGLE_KEY not in json.dumps(kwargs, default=str)
    assert (
        kwargs["env"]["LLM_MODEL"] == "openai/gemma-4-31b-it"
        and kwargs["env"]["OH_FALLBACK_MODELS"] == "openai/gemma-4-26b-a4b-it"
    )
    assert "LLM_BASE_URL" not in kwargs["env"] and "LLM_API_KEY" not in kwargs["env"]


def test_the_strict_allocation_is_accepted_only_on_the_proof_of_the_real_transport(env):
    with env.binding() as binding:
        alloc = allocate_worker(binding, agent=env.agent)
    assert alloc.max_tokens > 0 and alloc.strict
    assert env.agent.budget_enforcement == "broker" and env.agent.transport == "budget_broker"


def test_a_string_alone_is_not_a_capability(env):
    class Claims:
        budget_enforcement = "broker"

        def model_chain(self):
            return ["gemini/gemma-4-31b-it"]

    with env.binding() as binding:
        with pytest.raises(BudgetRefused) as caught:
            allocate_worker(binding, agent=Claims())
    assert caught.value.code == "unbounded_transport" and "aucune preuve de transport" in str(caught.value)


@pytest.mark.parametrize(
    "sandbox_kwargs",
    [{"network": "bridge"}, {"network": "host"}],
)
def test_a_sandbox_with_a_network_cannot_carry_the_broker_capability(manager, tmp_path, sandbox_kwargs):
    sandbox = ContainerSandbox(allow_root=True, **sandbox_kwargs)
    built = Env(manager, tmp_path, sandbox=sandbox)
    with built.binding() as binding:
        with pytest.raises(BudgetRefused) as caught:
            allocate_worker(binding, agent=built.agent)
    assert "network_none" in str(caught.value) or "sandbox_accepts_broker_mount" in str(caught.value)


def test_a_provider_key_in_the_sandbox_environment_makes_the_proof_fail(manager, tmp_path):
    sandbox = ContainerSandbox(allow_root=True, network="none", env={"MY_SETTING": GOOGLE_KEY})  # clé sous un autre nom
    built = Env(manager, tmp_path, sandbox=sandbox)
    proof = built.agent.broker_transport_proof()
    assert not proof.ok and any("provider_key_absent_from_sandbox" in f for f in proof.failures)
    assert GOOGLE_KEY not in json.dumps(proof.to_dict())  # la preuve ne contient jamais le secret


def test_a_double_that_is_not_a_docker_sandbox_cannot_carry_the_capability(manager, tmp_path):
    built = Env(manager, tmp_path, sandbox=SimpleNamespace(run_command=lambda *a, **k: None))
    proof = built.agent.broker_transport_proof()
    assert not proof.ok and "sandbox_supports_broker" in proof.failures[0]


# ── flux complet : session par allocation, consolidation unique ──────────────────────────────────────────────


def test_a_worker_run_is_accounted_by_the_broker_alone_and_consolidated_once(env):
    ContainerSandbox.behaviour = lambda client, e: ask(client, n=2)

    result = env.run()

    assert result.usage_source == "broker" and result.usage_status == "reported"
    assert (result.prompt_tokens, result.completion_tokens) == (20, 10)  # 2 appels × (10 entrée, 5 sortie)
    assert result.total_tokens == 30  # PAS les 9 999 999 tokens que le journal de l'agent prétendait
    project = env.ledger.snapshot(env.scope)
    assert (project.consumed_tokens, project.reserved_tokens, project.unknown_tokens) == (
        30,
        0,
        0,
    ) and not project.blocked
    assert len(env.upstream.generate_calls) == 2
    # sandbox lancé : sans réseau, socket unique en lecture seule, jeton de session par référence, aucune clé fournisseur
    argv = ContainerSandbox.launched["argv"]
    joined = " ".join(argv)
    assert argv[argv.index("--network") + 1] == "none" and ":/run/collegue-broker:ro" in joined
    assert GOOGLE_KEY not in joined and "cbk_" not in joined  # le jeton lui-même n'est pas dans l'argv non plus
    assert ContainerSandbox.launched["timeout"] is None or ContainerSandbox.launched["timeout"] > 0


def test_the_coder_may_use_the_fallback_model_and_nothing_else(env):
    seen = []

    def behaviour(client, e):
        ask(client, model="openai/gemma-4-26b-a4b-it")
        for bad in ("openai/gemini-2.5-flash", "gpt-5.4"):
            try:
                ask(client, model=bad)
            except openai.APIStatusError as exc:
                seen.append(exc.status_code)
        return 0

    ContainerSandbox.behaviour = behaviour
    result = env.run()
    assert seen == [403, 403] and result.usage_status == "reported"
    assert [c["url_model"] for c in env.upstream.generate_calls] == ["gemma-4-26b-a4b-it"]


def test_nothing_the_worker_submits_widens_the_session(env):
    refused = []

    def behaviour(client, e):
        for extra in ({"role": "reviewer"}, {"scope": "project:1"}, {"base_url": "http://evil"}):
            try:
                client.chat.completions.create(
                    model="gemma-4-31b-it", messages=[{"role": "user", "content": "x"}], max_tokens=8, extra_body=extra
                )
            except openai.APIStatusError as exc:
                refused.append(exc.status_code)
        return 0

    ContainerSandbox.behaviour = behaviour
    env.run()
    assert refused == [400, 400, 400] and env.upstream.generate_calls == []


def test_the_worker_can_never_exceed_its_allocation(manager, tmp_path):
    built = Env(
        manager,
        tmp_path,
        max_tokens=2_000,
        upstream=FakeUpstream(count=100, response=google_response(prompt=100, candidates=60)),
    )
    codes = []

    def behaviour(client, e):
        for _ in range(100):  # bien plus que l'allocation
            try:
                ask(client)
            except openai.APIStatusError as exc:
                codes.append(exc.status_code)
                break
        return 0

    ContainerSandbox.behaviour = behaviour
    result = built.run()
    project = built.ledger.snapshot(built.scope)
    assert codes == [403] and not project.blocked  # refus de budget propre, jamais un dépassement
    allocation = 1_000  # 50 % du solde de 2 000 tokens
    assert 0 < project.consumed_tokens == result.total_tokens <= allocation and project.reserved_tokens == 0
    assert project.used_tokens <= 2_000
    assert len(built.upstream.generate_calls) == project.consumed_tokens // 160  # 100 entrée + 60 sortie par appel


def test_the_provider_key_never_appears_anywhere_a_sandbox_or_a_log_can_see(env, caplog, tmp_path):
    import logging

    caplog.set_level(logging.DEBUG)
    ContainerSandbox.behaviour = lambda client, e: ask(client)
    env.run()

    haystack = [
        json.dumps(ContainerSandbox.launched["argv"]),
        repr(ContainerSandbox.launched["sandbox"].env),
        repr(vars(ContainerSandbox.launched["sandbox"])),
        caplog.text,
        repr(env.agent.broker_transport_proof().to_dict()),
        repr(env.runtime),
    ]
    import sqlite3

    with sqlite3.connect(env.db) as conn:
        for table in (
            "broker_sessions",
            "broker_attempts",
            "broker_clocks",
            "budget_scopes",
            "budget_reservations",
            "budget_events",
        ):
            haystack.append(
                json.dumps([list(map(str, row)) for row in conn.execute(f"SELECT * FROM {table}").fetchall()])
            )
    assert all(GOOGLE_KEY not in blob for blob in haystack)
    assert not any("cbk_" in blob for blob in haystack[:2] + haystack[3:4])  # le jeton hashé seulement en base (sha256)


# ── interruptions ────────────────────────────────────────────────────────────────────────────────────────────


def test_a_worker_that_never_launched_leaves_the_parent_reservation_to_the_caller_release(env):
    def behaviour(client, e):
        raise SandboxUnavailable("docker absent")

    ContainerSandbox.behaviour = behaviour
    with pytest.raises(SandboxUnavailable):
        env.run()
    project = env.ledger.snapshot(env.scope)
    assert (project.reserved_tokens, project.consumed_tokens, project.unknown_tokens) == (
        0,
        0,
        0,
    ) and not project.blocked
    session = env.runtime.service_for(env.ledger).store.open_sessions()
    assert session == []  # la session est fermée, le socket supprimé


def test_a_worker_interrupted_midway_is_unknown_never_zero(env):
    def behaviour(client, e):
        ask(client)
        raise KeyboardInterrupt

    ContainerSandbox.behaviour = behaviour
    with pytest.raises(KeyboardInterrupt):
        env.run()
    project = env.ledger.snapshot(env.scope)
    assert project.blocked and project.unknown_tokens > 0  # réserve parent conservée, projet bloqué


def test_an_ambiguous_child_failure_blocks_the_whole_project_immediately(env):
    env.upstream.generate_error = http_error(503)
    states = []

    def behaviour(client, e):
        try:
            ask(client)
        except openai.APIStatusError as exc:
            states.append(exc.status_code)
        states.append(env.ledger.snapshot(env.scope).blocked)  # avant même la fin du worker
        return 0

    ContainerSandbox.behaviour = behaviour
    result = env.run()

    assert states == [502, True]
    assert result.usage_status == "incomplete" and result.usage_source == "broker"
    project = env.ledger.snapshot(env.scope)
    assert project.blocked and project.unknown_tokens > 0 and project.consumed_tokens == 0
    with env.binding() as binding:
        with pytest.raises(BudgetRefused):
            allocate_worker(binding, agent=env.agent)  # plus aucun worker


def test_a_successful_exit_without_any_broker_usage_is_a_proven_zero_not_a_guess(env):
    ContainerSandbox.behaviour = lambda client, e: 0  # le worker n'a jamais appelé le modèle
    result = env.run()
    assert result.usage_status == "reported" and result.total_tokens == 0
    project = env.ledger.snapshot(env.scope)
    assert (project.consumed_tokens, project.reserved_tokens) == (0, 0) and not project.blocked


def test_the_broker_agent_refuses_to_run_without_a_bound_registry(env):
    ContainerSandbox.behaviour = lambda client, e: 0
    with pytest.raises(BudgetRefused) as caught:
        env.agent.implement_issue(str(env.workspace), IssueSpec(number=1, title="t"))
    assert "aucune allocation" in str(caught.value) and env.upstream.count_calls == []


def test_settle_worker_is_a_no_op_after_the_brokers_consolidation_and_never_counts_twice(env):
    ContainerSandbox.behaviour = lambda client, e: ask(client)
    with env.binding() as binding:
        alloc = allocate_worker(binding, agent=env.agent)
        with worker_allocation(alloc):
            result = env.agent.implement_issue(str(env.workspace), IssueSpec(number=1, title="t"))
        settle_worker(binding, alloc, result)
        settle_worker(binding, alloc, result)
    assert env.ledger.snapshot(env.scope).consumed_tokens == 15


def test_the_runner_script_command_carries_no_allocation_arguments_in_broker_mode(env):
    with env.binding() as binding:
        alloc = allocate_worker(binding, agent=env.agent)
        with worker_allocation(alloc):
            command = env.agent.build_command(IssueSpec(number=1, title="t"))
    assert "--budget-usd" not in command and "--budget-tokens" not in command and "--prices" not in command
    assert env.agent.runner_model_chain() == ["openai/gemma-4-31b-it", "openai/gemma-4-26b-a4b-it"]
    assert env.agent.model_chain() == [
        "gemini/gemma-4-31b-it",
        "gemini/gemma-4-26b-a4b-it",
    ]  # la destination reste Google


async def test_the_pilot_repairs_interrupted_broker_state_before_launching_and_is_a_no_op_otherwise(manager, tmp_path):
    from collegue.broker.runtime import install_runtime_for_tests
    from collegue.pilot.runtime import _recover_broker_state

    built = Env(manager, tmp_path)
    service = built.runtime.service_for(built.ledger)
    parent = built.ledger.reserve(
        built.scope, micro_usd=1, tokens=5000, kind="worker", role="coder", transport="worker"
    )
    session = service.open_session(
        parent_scope_key=built.scope, parent_reservation_id=parent.reservation_id, role="coder"
    )
    install_runtime_for_tests(built.runtime)
    try:
        assert await _recover_broker_state(make_settings(LLM_TRANSPORT="direct"), manager) == 0  # hors courtier : rien
        assert service.store.get_session(session.session_id).state == "open"
        await _recover_broker_state(built.settings, manager)  # courtier : la session orpheline est fermée et consolidée
    finally:
        install_runtime_for_tests(None)
    assert service.store.get_session(session.session_id).state == "closed"
    assert built.ledger.get_reservation(parent.reservation_id).state == "committed"
