"""Politique de repli de bout en bout (A27) : VRAI ``oh_runner.main()`` → VRAI relais → socket → VRAI courtier → faux fournisseur.

Seul le SDK OpenHands est remplacé par un double STRICT (mêmes champs que 1.19.1) dont ``Conversation.run`` émet par un VRAI client
``openai`` en lui appliquant les ``num_retries`` que le runner a posés : si le runner laissait des retries au SDK, ils se verraient
ici dans le nombre d'émissions du fournisseur. Aucun réseau, aucune clé.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import openai
import pytest
from w5_broker_fallback_contract import FALLBACK, PRIMARY, ModelGatedUpstream
from w5_broker_support import http_error

from collegue.broker import BrokerConfig
from collegue.broker.runtime import BrokerRuntime, install_runtime_for_tests
from collegue.broker.server import BrokerSocketServer
from collegue.state import ProjectStateManager


@pytest.fixture
def rig(tmp_path, monkeypatch):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'fb.db'}", create=True)
    upstream = ModelGatedUpstream()
    runtime = BrokerRuntime(upstream=upstream, config=BrokerConfig(), run_root=str(tmp_path / "run"))
    install_runtime_for_tests(runtime)
    ledger = manager.budget_ledger
    scope = ledger.scope_for_project(manager.create_project(name="fb"), max_cost_usd=2.0, max_tokens=250_000).scope_key
    parent = ledger.reserve(scope, micro_usd=1000, tokens=100_000, kind="worker", role="coder", transport="worker")
    service = runtime.service_for(ledger)
    session = service.open_session(parent_scope_key=scope, parent_reservation_id=parent.reservation_id, role="coder")
    server = BrokerSocketServer(service, session.session_id, run_root=str(runtime.run_root)).start()
    yield SimpleNamespace(
        upstream=upstream, ledger=ledger, scope=scope, session=session, server=server, monkeypatch=monkeypatch
    )
    upstream.release.set()
    server.stop()
    install_runtime_for_tests(None)


def run_runner(rig, *, client_timeout=20):
    from test_w4_routing_sdk import StrictLLM

    from collegue.executor import oh_runner

    StrictLLM.instances, StrictLLM.login_calls = [], []
    failures = []

    class Conversation:
        def __init__(self, **_kw):
            pass

        def send_message(self, _task):
            return None

        def run(self):
            llm = StrictLLM.instances[-1]
            client = openai.OpenAI(
                base_url=llm.base_url,
                api_key=llm.api_key,
                max_retries=llm.kwargs["num_retries"],  # ce que le SDK réel appliquerait
                timeout=client_timeout,
            )
            try:
                client.chat.completions.create(
                    model=llm.model.split("/", 1)[1],
                    messages=[{"role": "user", "content": "x"}],
                    max_tokens=llm.max_output_tokens,
                )
            except Exception as exc:
                failures.append(exc)
                raise

    sdk = types.ModuleType("openhands.sdk")
    sdk.LLM, sdk.Conversation = StrictLLM, Conversation
    default = types.ModuleType("openhands.tools.preset.default")
    default.get_default_agent = lambda *, llm, cli_mode: llm
    mp = rig.monkeypatch
    mp.setitem(sys.modules, "openhands.sdk", sdk)
    mp.setitem(sys.modules, "openhands.tools.preset.default", default)
    for name in ("LLM_API_KEY", "GEMINI_API_KEY", "LLM_BASE_URL", "LLM_SUBSCRIPTION"):
        mp.delenv(name, raising=False)
    mp.setenv("COLLEGUE_BROKER_SOCKET", rig.server.socket_path)
    mp.setenv("LLM_API_KEY", rig.session.token)
    mp.setenv("LLM_MODEL", f"openai/{PRIMARY}")
    mp.setenv("OH_FALLBACK_MODELS", f"openai/{FALLBACK}")
    mp.setenv("OH_MAX_OUTPUT_TOKENS", "128")
    mp.setenv("OH_NUM_RETRIES", "8")  # même une demande explicite de retries n'arme pas ceux du SDK en mode courtier
    mp.setattr(sys, "argv", ["oh_runner", "--task", "t"])
    code = oh_runner.main()
    return SimpleNamespace(code=code, llms=StrictLLM.instances, failures=failures)


def test_the_sdk_never_retries_by_itself_and_a_lost_primary_response_is_not_followed_by_a_fallback(rig):
    """Délai client dépassé pendant que le fournisseur traite encore : ni retry du SDK, ni repli — une seule émission."""
    result = run_runner(rig, client_timeout=0.5)  # le fournisseur ne répond pas (porte fermée)
    assert result.code == 4  # fin sans repli : la consommation du principal n'est pas connue
    assert [llm.kwargs["num_retries"] for llm in result.llms] == [0] and len(result.llms) == 1
    assert rig.upstream.models == [PRIMARY]  # UNE émission : ni retry, ni 26B
    assert isinstance(result.failures[0], openai.APITimeoutError)


def test_a_refusal_established_before_processing_allows_the_fallback_which_the_server_then_authorizes(rig):
    rig.upstream.primary_error = http_error(429)
    rig.upstream.release.set()
    result = run_runner(rig)
    assert result.code == 0
    assert [llm.model for llm in result.llms] == [f"openai/{PRIMARY}", f"openai/{FALLBACK}"]
    assert rig.upstream.models == [PRIMARY, FALLBACK]
    assert all(llm.kwargs["num_retries"] == 0 for llm in result.llms)


def test_a_definitive_broker_refusal_is_not_followed_by_a_fallback(rig):
    other = rig.ledger.reserve(rig.scope, tokens=1, kind="call")
    rig.ledger.mark_unknown(other.reservation_id, reason="un autre rôle a perdu sa réponse")  # projet bloqué
    rig.upstream.release.set()
    result = run_runner(rig)
    assert result.code == 4 and len(result.llms) == 1 and rig.upstream.models == []
    assert "budget_blocked" in str(result.failures[0]) or "bloqu" in str(result.failures[0]).lower()


@pytest.mark.parametrize(
    "exc, verdict",
    [
        (
            type("RateLimitError", (Exception,), {"__module__": "litellm.exceptions"})(
                """{"error":{"code":"upstream_rejected"}}"""
            ),
            "fallback",
        ),
        (
            type("APIError", (Exception,), {"__module__": "openai"})(
                "Error code: 429 - {'error': {'code': 'count_tokens_failed'}}"
            ),
            "fallback",
        ),
        (
            type("APIError", (Exception,), {"__module__": "openai"})(
                "Error code: 403 - {'error': {'code': 'budget_blocked'}}"
            ),
            "stop",
        ),
        (
            type("APIError", (Exception,), {"__module__": "openai"})(
                "Error code: 502 - {'error': {'code': 'upstream_ambiguous'}}"
            ),
            "stop",
        ),
        (type("Timeout", (Exception,), {"__module__": "litellm.exceptions"})("request timed out"), "ambiguous"),
        (type("APIConnectionError", (Exception,), {"__module__": "openai"})("Connection error."), "ambiguous"),
        (RuntimeError("outil en échec"), "fallback"),  # le courtier ne la voit pas : bascule historique
    ],
)
def test_the_runner_classifies_failures_without_inventing_a_verdict(exc, verdict):
    from collegue.executor import oh_runner

    assert oh_runner.broker_failure_verdict(exc) == verdict
