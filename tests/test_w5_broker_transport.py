"""Transport du courtier W5 : serveur de socket Unix par session, relais loopback, montage du sandbox.

Chaîne RÉELLE (aucun mock de transport) : vrai SDK ``openai`` → relais TCP loopback (``oh_broker_relay``, le fichier embarqué
dans l'image) → vrai socket Unix → ``BrokerSocketServer`` → ``BrokerService`` → faux fournisseur compteur d'émissions.
Aucun réseau externe, aucune clé fournisseur.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import socket
import stat
import sys
from pathlib import Path

import openai
import pytest
from w5_broker_contract import open_worker, service_for
from w5_broker_support import FakeUpstream, chat_request, google_response

from collegue.broker.server import BrokerSocketServer
from collegue.sandbox.executor import DockerSandbox, SandboxRefused
from collegue.state import ProjectStateManager

REPO = Path(__file__).resolve().parents[1]


def _load_relay():
    spec = importlib.util.spec_from_file_location(
        "oh_broker_relay", REPO / "collegue" / "executor" / "oh_broker_relay.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


relay_module = _load_relay()


@pytest.fixture
def manager(tmp_path):
    return ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'w5.db'}", create=True)


class Stack:
    def __init__(self, manager, tmp_path, upstream=None):
        self.service, self.upstream, self.ledger, self.scope_key, self.parent_rid = service_for(manager, upstream)
        self.session = open_worker(self.service, self.scope_key, self.parent_rid)
        self.server = BrokerSocketServer(self.service, self.session.session_id, run_root=str(tmp_path / "run")).start()
        self.relay, self.port = relay_module.start(self.server.socket_path)
        self.base_url = f"http://127.0.0.1:{self.port}/v1"

    def client(self, token=None):
        return openai.OpenAI(base_url=self.base_url, api_key=token or self.session.token, max_retries=0, timeout=20)

    def raw(self, data: bytes, *, via_relay=True) -> bytes:
        if via_relay:
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        else:
            sock = socket.socket(socket.AF_UNIX)
            sock.settimeout(10)
            sock.connect(self.server.socket_path)
        try:
            sock.sendall(data)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            sock.close()

    def close(self):
        self.relay.shutdown()
        self.relay.server_close()
        self.server.stop()


@pytest.fixture
def stack(manager, tmp_path):
    made = []

    def build(upstream=None):
        built = Stack(manager, tmp_path, upstream)
        made.append(built)
        return built

    yield build
    for built in made:
        built.close()


def status_of(response: bytes) -> int:
    return int(response.split(b" ", 2)[1])


def body_of(response: bytes) -> dict:
    return json.loads(response.split(b"\r\n\r\n", 1)[1])


def post(stack, body: dict, *, token=None, headers=None, path="/v1/chat/completions") -> bytes:
    payload = json.dumps(body).encode()
    lines = [f"POST {path} HTTP/1.1", "Host: x", f"Content-Length: {len(payload)}", "Content-Type: application/json"]
    if token is not False:
        lines.append(f"Authorization: Bearer {token or stack.session.token}")
    lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
    return stack.raw(("\r\n".join(lines) + "\r\n\r\n").encode() + payload)


# ── vrai SDK de bout en bout ─────────────────────────────────────────────────────────────────────────────────


def test_the_real_openai_sdk_talks_to_the_broker_through_the_relay_and_the_unix_socket(stack):
    built = stack()
    completion = built.client().chat.completions.create(
        model="openai/gemma-4-31b-it", messages=[{"role": "user", "content": "salut"}], max_tokens=32
    )
    assert completion.choices[0].message.content == "ok"
    assert completion.usage.total_tokens == 15 and completion.model == "gemma-4-31b-it"
    assert len(built.upstream.generate_calls) == 1
    assert (
        built.upstream.generate_calls[0]["body"]["model"] == "models/gemma-4-31b-it"
    )  # Google natif, pas du Chat Completions
    assert built.ledger.snapshot(built.session.scope_key).consumed_tokens == 15


def test_the_sdk_tool_calling_round_trip_reaches_google_natively(stack):
    parts = [{"functionCall": {"name": "run", "args": {"cmd": "ls"}}}]
    built = stack(FakeUpstream(response=google_response(parts=parts, finish="STOP")))
    tools = [
        {
            "type": "function",
            "function": {
                "name": "run",
                "description": "d",
                "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
            },
        }
    ]
    completion = built.client().chat.completions.create(
        model="gemma-4-31b-it",
        messages=[{"role": "user", "content": "x"}],
        tools=tools,
        tool_choice="auto",
        max_tokens=32,
    )
    call = completion.choices[0].message.tool_calls[0]
    assert (call.function.name, json.loads(call.function.arguments), completion.choices[0].finish_reason) == (
        "run",
        {"cmd": "ls"},
        "tool_calls",
    )
    sent = built.upstream.generate_calls[0]["body"]
    assert sent["tools"][0]["functionDeclarations"][0]["name"] == "run" and sent["toolConfig"] == {
        "functionCallingConfig": {"mode": "AUTO"}
    }


def test_a_refusal_is_a_clean_openai_style_error_the_sdk_does_not_retry_into_a_storm(stack):
    built = stack()
    with pytest.raises(openai.APIStatusError) as caught:
        built.client().chat.completions.create(
            model="gpt-5.4", messages=[{"role": "user", "content": "x"}], max_tokens=8
        )
    assert caught.value.status_code == 403 and "model_not_allowed" in str(caught.value.body)
    assert built.upstream.count_calls == []


# ── surface : une route, aucun en-tête relayé ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "request_line",
    [
        "GET / HTTP/1.1",
        "GET /v1/models HTTP/1.1",
        "POST /v1/completions HTTP/1.1",
        "POST /v1/chat/completions/ HTTP/1.1",
        "POST /v1/chat/completions?x=1 HTTP/1.1",
        "POST /admin HTTP/1.1",
        "POST /health HTTP/1.1",
        "DELETE /v1/chat/completions HTTP/1.1",
        "CONNECT evil.example:443 HTTP/1.1",
        "POST http://evil.example/v1/chat/completions HTTP/1.1",
    ],
)
def test_only_one_route_exists_everything_else_is_404(stack, request_line):
    built = stack()
    response = built.raw(
        f"{request_line}\r\nHost: x\r\nContent-Length: 0\r\nAuthorization: Bearer {built.session.token}\r\n\r\n".encode()
    )
    assert status_of(response) == 404 and built.upstream.count_calls == []


def test_authentication_is_the_session_token_only(stack, manager):
    built = stack()
    body = chat_request()
    assert status_of(post(built, body, token=False)) == 401
    assert status_of(post(built, body, token="cbk_faux")) == 401
    assert (
        status_of(post(built, body, headers={"x-goog-api-key": "AIza-client-key"}, token=False)) == 401
    )  # pas une clé fournisseur
    other = open_worker(
        built.service,
        built.scope_key,
        built.ledger.reserve(
            built.scope_key, micro_usd=1, tokens=1000, kind="worker", transport="worker"
        ).reservation_id,
    )
    assert status_of(post(built, body, token=other.token)) == 401  # le jeton d'une AUTRE session n'ouvre pas celle-ci
    assert built.upstream.count_calls == []
    assert status_of(post(built, body)) == 200


def test_client_supplied_headers_proxies_and_hosts_change_nothing(stack):
    built = stack()
    response = post(
        built,
        chat_request(),
        headers={
            "X-Goog-Api-Key": "AIza-client-key",
            "X-Forwarded-For": "10.0.0.1",
            "X-Forwarded-Host": "evil.example",
            "Proxy-Authorization": "Basic eA==",
            "X-Original-Host": "evil.example",
            "X-Collegue-Scope": "project:1",
            "X-Role": "reviewer",
            "Location": "http://evil.example",
        },
    )
    assert status_of(response) == 200
    assert b"Location" not in response.split(b"\r\n\r\n")[0] and b"evil.example" not in response
    sent = built.upstream.generate_calls[0]["body"]
    assert json.dumps(sent).count("AIza") == 0 and "evil" not in json.dumps(sent)


@pytest.mark.parametrize(
    "head, expected",
    [
        ("POST /v1/chat/completions HTTP/1.1\r\nTransfer-Encoding: chunked\r\nAuthorization: Bearer x\r\n\r\n", 400),
        ("POST /v1/chat/completions HTTP/1.1\r\nAuthorization: Bearer x\r\n\r\n", 411),
        ("POST /v1/chat/completions HTTP/1.1\r\nContent-Length: 99999999\r\nAuthorization: Bearer x\r\n\r\n", 413),
        ("POST /v1/chat/completions HTTP/1.1\r\nContent-Length: abc\r\nAuthorization: Bearer x\r\n\r\n", 411),
        (
            "POST /v1/chat/completions HTTP/1.1\r\nExpect: 100-continue\r\nContent-Length: 2\r\nAuthorization: Bearer x\r\n\r\n",
            400,
        ),
        (
            "POST /v1/chat/completions HTTP/1.1\r\nContent-Length: 2\r\nContent-Length: 2\r\nAuthorization: Bearer x\r\n\r\n",
            400,
        ),
        ("BAD\r\n\r\n", 400),
    ],
)
def test_malformed_http_is_refused_without_reaching_the_service(stack, head, expected):
    built = stack()
    assert status_of(built.raw(head.encode())) == expected and built.upstream.count_calls == []


def test_an_invalid_idempotency_key_is_refused_and_a_valid_one_replays_without_a_second_generation(stack):
    built = stack()
    assert status_of(post(built, chat_request(), headers={"Idempotency-Key": "bad key!"})) == 400
    first, second = (
        post(built, chat_request(), headers={"Idempotency-Key": "k-1"}),
        post(built, chat_request(), headers={"Idempotency-Key": "k-1"}),
    )
    assert status_of(first) == status_of(second) == 200 and body_of(first) == body_of(second)
    assert len(built.upstream.generate_calls) == 1


def test_the_relay_and_the_socket_expose_the_same_surface_and_the_error_never_carries_a_secret(stack):
    built = stack()
    direct = built.raw(b"GET /v1/models HTTP/1.1\r\nHost: x\r\n\r\n", via_relay=False)
    relayed = built.raw(b"GET /v1/models HTTP/1.1\r\nHost: x\r\n\r\n")
    assert status_of(direct) == status_of(relayed) == 404
    assert built.session.token.encode() not in direct + relayed


# ── répertoire du socket ─────────────────────────────────────────────────────────────────────────────────────


def test_the_socket_directory_is_private_holds_only_the_socket_and_disappears_on_stop(manager, tmp_path):
    service, _, ledger, scope_key, parent_rid = service_for(manager)
    session = open_worker(service, scope_key, parent_rid)
    server = BrokerSocketServer(service, session.session_id, run_root=str(tmp_path / "run")).start()
    directory = Path(server.directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700 and os.listdir(directory) == ["broker.sock"]
    assert (
        stat.S_ISSOCK(os.lstat(server.socket_path).st_mode)
        and stat.S_IMODE(os.lstat(server.socket_path).st_mode) == 0o600
    )
    server.stop()
    assert not directory.exists()


# ── montage du sandbox ───────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def socket_dir(tmp_path):
    directory = tmp_path / "broker"
    directory.mkdir()
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(directory / "broker.sock"))
    yield directory
    sock.close()


def broker_sandbox(socket_dir, **kw):
    return DockerSandbox(allow_root=True, **kw).with_broker(
        str(socket_dir),
        env={"LLM_BASE_URL": "http://127.0.0.1:8765/v1"},
        env_secrets={"LLM_API_KEY": "cbk_session_token"},
    )


def test_the_broker_mount_is_read_only_isolated_and_carries_the_token_only_by_reference(socket_dir, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    argv = broker_sandbox(socket_dir)._build_run_argv(["true"], str(workspace), name="n")
    joined = " ".join(argv)
    assert argv[argv.index("--network") + 1] == "none"
    assert (
        f"{socket_dir.resolve()}:/run/collegue-broker:ro" in joined
        and "COLLEGUE_BROKER_SOCKET=/run/collegue-broker/broker.sock" in joined
    )
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert len(mounts) == 2 and mounts[1].endswith(":/workspace")  # ni registre, ni autre session, ni fichier hôte
    assert "cbk_session_token" not in joined and "LLM_API_KEY" in argv and argv[argv.index("LLM_API_KEY") - 1] == "-e"
    assert "--env-file" not in argv and "--privileged" not in argv and "host" not in argv[argv.index("--network") + 1]
    assert "cbk_session_token" not in repr(vars(broker_sandbox(socket_dir)).get("env", {}))


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"network": "bridge"}, "sans réseau"),
        ({"network": "host"}, "sans réseau"),
        ({"env_passthrough": ("FOO",)}, "env_passthrough"),
        ({"subscription_auth_dir": "/tmp"}, "exclusifs"),
        ({"env": {"GEMINI_API_KEY": "x"}}, "interdite"),
        ({"env": {"google_api_key": "x"}}, "interdite"),
        ({"env": {"HTTPS_PROXY": "http://proxy:3128"}}, "interdite"),
        ({"env": {"OPENAI_BASE_URL": "https://evil.example"}}, "interdite"),
    ],
)
def test_a_broker_worker_refuses_network_environment_passthrough_provider_keys_and_proxies(
    socket_dir, kwargs, fragment
):
    with pytest.raises(SandboxRefused, match=fragment):
        broker_sandbox(socket_dir, **kwargs)


def test_the_socket_directory_must_hold_exactly_the_socket(socket_dir, tmp_path):
    (socket_dir / "extra.txt").write_text("x")
    with pytest.raises(SandboxRefused, match="ne doit contenir que"):
        broker_sandbox(socket_dir)
    (socket_dir / "extra.txt").unlink()
    link = tmp_path / "link"
    link.symlink_to(socket_dir)
    with pytest.raises(SandboxRefused, match="ordinaire"):
        broker_sandbox(link)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SandboxRefused, match="socket absent"):
        broker_sandbox(empty)
    regular = tmp_path / "regular"
    regular.mkdir()
    (regular / "broker.sock").write_text("pas un socket")
    with pytest.raises(SandboxRefused, match="ne doit contenir que"):
        broker_sandbox(regular)


def test_the_w1_git_control_guard_still_applies_to_the_socket_directory(tmp_path):
    from collegue.sandbox.executor import GIT_CONTROL_MARKER

    control = tmp_path / "ws.control"
    control.mkdir()
    (control / GIT_CONTROL_MARKER).write_text("x")
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(control / "broker.sock"))
    try:
        with pytest.raises(SandboxRefused, match="Git"):
            broker_sandbox(control)
    finally:
        sock.close()


def test_the_mount_is_revalidated_at_every_launch(socket_dir, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sandbox = broker_sandbox(socket_dir)
    (socket_dir / "broker.sock").unlink()
    with pytest.raises(SandboxRefused, match="socket absent"):
        sandbox._build_run_argv(["true"], str(workspace), name="n")


def test_the_default_sandbox_argv_is_unchanged_without_a_broker(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    argv = DockerSandbox(allow_root=True)._build_run_argv(["true"], str(workspace), name="n")
    assert "/run/collegue-broker" not in " ".join(argv) and "COLLEGUE_BROKER_SOCKET" not in " ".join(argv)


# ── inventaire statique des sous-processus ───────────────────────────────────────────────────────────────────


def _calls(path: Path, names: set):
    found = []
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = ast.unparse(node.func)
            if target in names or target.split(".")[-1] in {"Popen", "system", "popen"}:
                found.append((node.lineno, target))
    return found


def test_the_broker_package_and_the_relay_spawn_no_subprocess():
    forbidden = {
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_output",
        "os.system",
        "os.popen",
        "os.execv",
        "os.spawnv",
    }
    for path in [
        *sorted((REPO / "collegue" / "broker").glob("*.py")),
        REPO / "collegue" / "executor" / "oh_broker_relay.py",
    ]:
        assert _calls(path, forbidden) == [], f"sous-processus inattendu dans {path.name}"


def test_the_sandbox_subprocess_call_sites_are_exactly_the_inventoried_ones():
    sites = _calls(REPO / "collegue" / "sandbox" / "executor.py", {"subprocess.run"})
    assert [target for _, target in sites] == ["subprocess.run"] * 3  # docker run (worker), docker kill, docker version


def test_a_socket_path_too_long_for_af_unix_is_an_explicit_refusal(manager, tmp_path):
    service, _, ledger, scope_key, parent_rid = service_for(manager)
    session = open_worker(service, scope_key, parent_rid)
    deep = tmp_path / ("d" * 40) / ("e" * 40) / ("f" * 40)
    with pytest.raises(RuntimeError, match="trop long"):
        BrokerSocketServer(service, session.session_id, run_root=str(deep)).start()
    assert not any(deep.parent.parent.parent.rglob("cbk-*"))  # rien ne reste sur le disque
