"""Logique du contrôle CI du transport W5 (``scripts/ci_w5_broker_transport.py``), sans Docker ni SDK réel.

Le script s'exécute dans l'image (vrai SDK, ``--network none``) ; ici on éprouve SA logique avec un relais de remplacement qui respecte
l'interface publiée par A (``start(socket_path, port) -> (serveur, port)``, ``MAX_CLIENT_BYTES``) et un faux module ``openhands.sdk``.
Chaque contrôle doit passer sur un relais sain ET échouer sur un relais fautif : un contrôle qui ne peut pas échouer ne prouve rien.
Le relais RÉEL de A et le vrai SDK ne sont éprouvés que par la CI distante (image construite) : voir ``docs/consolidation/w5-integration.md``.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import re
import socket
import socketserver
import sys
import threading
import types
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci_w5_broker_transport.py"


@pytest.fixture(scope="module")
def proof():
    spec = importlib.util.spec_from_file_location("ci_w5_broker_transport_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STAND_IN_RELAY = """
import socket, socketserver, threading

MAX_CLIENT_BYTES = {limit}
FORWARD_ALL = {forward_all}


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(5)
        broker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        broker.connect(self.server.socket_path)
        received = b""
        try:
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                received += chunk
                if not FORWARD_ALL and len(received) > MAX_CLIENT_BYTES:
                    return
                broker.sendall(chunk)
                if b"\\r\\n\\r\\n" in received and len(received) >= _expected(received):
                    break
            broker.shutdown(socket.SHUT_WR)
            while True:
                data = broker.recv(65536)
                if not data:
                    break
                client.sendall(data)
        except OSError:
            pass
        finally:
            broker.close()


def _expected(raw):
    head, _, _ = raw.partition(b"\\r\\n\\r\\n")
    length = 0
    for line in head.split(b"\\r\\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    return len(head) + 4 + length


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, socket_path):
        self.socket_path = socket_path
        super().__init__(address, _Handler)


def start(socket_path=None, port=0):
    server = _Server(({host!r}, int(port)), socket_path)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])
"""


def write_relay(tmp_path, *, limit=544 * 1024, forward_all=False, host="127.0.0.1"):
    path = tmp_path / "relay.py"
    path.write_text(STAND_IN_RELAY.format(limit=limit, forward_all=forward_all, host=host), encoding="utf-8")
    return str(path)


class FakeSdk(types.ModuleType):
    """Faux ``openhands.sdk`` : ``LLM.completion`` fait un vrai POST HTTP (loopback) vers ``base_url`` avec le jeton."""

    def __init__(self, behaviour="ok"):
        super().__init__("openhands.sdk")
        sdk = self

        class TextContent:
            def __init__(self, text):
                self.text = text

        class Message:
            def __init__(self, role, content):
                self.role, self.content = role, content

        class Usage:
            prompt_tokens = 7
            completion_tokens = 2

        class Metrics:
            accumulated_token_usage = Usage()

        class LLM:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.usage_id = kwargs["usage_id"]
                self.metrics = (
                    Metrics()
                    if behaviour != "wrong-usage"
                    else types.SimpleNamespace(
                        accumulated_token_usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1)
                    )
                )

            def completion(self, messages):
                host_port = self.kwargs["base_url"].split("//", 1)[1].split("/", 1)[0]
                host, port = host_port.split(":")
                conn = http.client.HTTPConnection(host, int(port), timeout=10)
                token = self.kwargs["api_key"].get_secret_value()
                if behaviour == "bad-token":
                    token = "autre"
                body = json.dumps({"model": self.kwargs["model"].split("/", 1)[1], "messages": [{"role": "user"}]})
                conn.request(
                    "POST",
                    "/v1/chat/completions",
                    body=body,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                )
                data = json.loads(conn.getresponse().read().decode())
                text = data["choices"][0]["message"]["content"]
                return types.SimpleNamespace(message=types.SimpleNamespace(content=[TextContent(text)]))

        self.LLM, self.Message, self.TextContent = LLM, Message, TextContent


@pytest.fixture
def fake_sdk(monkeypatch):
    def install(behaviour="ok"):
        monkeypatch.setitem(sys.modules, "openhands", types.ModuleType("openhands"))
        monkeypatch.setitem(sys.modules, "openhands.sdk", FakeSdk(behaviour))

    return install


@pytest.fixture
def pristine(monkeypatch, proof):
    """Isolation saine simulée : boucle locale seule, aucune sortie, aucune variable sensible (celles de l'hôte de test retirées)."""
    monkeypatch.setattr(proof, "interfaces", lambda *a, **k: ["lo"])
    monkeypatch.setattr(proof, "external_connections", lambda *a, **k: [])
    for name in [n for n in os.environ if proof.SECRET_LIKE.search(n)]:
        monkeypatch.delenv(name)


def short_dir():
    import tempfile

    return tempfile.mkdtemp(prefix="w5c-", dir="/dev/shm" if Path("/dev/shm").is_dir() else None)


def test_the_fake_broker_serves_the_openai_route_with_the_session_token_only(proof):
    tmp = short_dir()
    broker = proof.FakeBroker(f"{tmp}/b.sock").start()
    try:

        def call(target, token=proof.SESSION_TOKEN, method="POST", body=b'{"model": "m", "messages": [1]}'):
            raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            raw.connect(broker.socket_path)
            head = f"{method} {target} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {token}\r\nContent-Length: {len(body)}\r\n\r\n"
            raw.sendall(head.encode() + body)
            data = b""
            while chunk := raw.recv(4096):
                data += chunk
            raw.close()
            return int(data.split(b" ", 2)[1])

        assert call("/v1/chat/completions") == 200
        assert call("/v1/chat/completions", token="autre") == 401
        assert call("/v1/models") == 404
        assert call("/v1/chat/completions", body=b"pas du json") == 400
        assert [r["path"] for r in broker.requests] == ["/v1/chat/completions"] * 2 + ["/v1/models"] + [
            "/v1/chat/completions"
        ]
    finally:
        broker.stop()


def test_isolation_fails_on_a_key_like_variable_an_extra_interface_or_an_external_connection(proof, monkeypatch):
    monkeypatch.setattr(proof, "interfaces", lambda *a, **k: ["lo"])
    monkeypatch.setattr(proof, "external_connections", lambda *a, **k: [])
    ok = proof.Failures()
    proof.check_isolation(ok, {"PATH": "/usr/bin", "HOME": "/home/sandbox"})
    assert ok.items == []
    leaking = proof.Failures()
    proof.check_isolation(leaking, {"GOOGLE_API_KEY": "x", "GITHUB_TOKEN": "y", "LLM_API_KEY_CODER": "z"})
    assert any("GOOGLE_API_KEY" in m and "GITHUB_TOKEN" in m for m in leaking.items)
    monkeypatch.setattr(proof, "interfaces", lambda *a, **k: ["eth0", "lo"])
    assert any("interfaces" in m for m in _isolation(proof))
    monkeypatch.setattr(proof, "interfaces", lambda *a, **k: ["lo"])
    monkeypatch.setattr(proof, "external_connections", lambda *a, **k: ["1.1.1.1:443"])
    assert any("connexion externe" in m for m in _isolation(proof))


def _isolation(proof):
    failures = proof.Failures()
    proof.check_isolation(failures, {})
    return failures.items


def test_the_interface_listing_reads_proc_net_dev_format(proof, tmp_path):
    dev = tmp_path / "dev"
    dev.write_text("Inter-|   Receive\n face |bytes\n    lo: 1 2\n  eth0: 3 4\n", encoding="utf-8")
    assert proof.interfaces(str(dev)) == ["eth0", "lo"]


def test_the_whole_proof_passes_with_a_healthy_relay_and_fails_for_each_fault(proof, tmp_path, fake_sdk, pristine):
    fake_sdk("ok")
    report = proof.run_checks(write_relay(tmp_path))
    assert report["failures"] == [], report
    assert report["sdk"]["usage"] == [7, 2] and report["sdk"]["usage_id"] == "coder"
    assert report["relay"] == {"other_route": 404, "absolute_url": 404, "oversized": report["relay"]["oversized"]}

    fake_sdk("bad-token")
    assert any(
        "jeton" in m or "réponse" in m or "SDK" in m for m in proof.run_checks(write_relay(tmp_path))["failures"]
    )
    fake_sdk("wrong-usage")
    assert any("usage" in m for m in proof.run_checks(write_relay(tmp_path))["failures"])


def test_a_relay_that_forwards_oversized_bodies_or_listens_beyond_loopback_is_rejected(
    proof, tmp_path, fake_sdk, pristine
):
    fake_sdk("ok")
    unbounded = proof.run_checks(write_relay(tmp_path, forward_all=True))
    assert any("plafond" in m for m in unbounded["failures"]), unbounded["failures"]
    wide = proof.run_checks(write_relay(tmp_path, host="0.0.0.0"))
    assert any("boucle locale" in m for m in wide["failures"]), wide["failures"]


def test_a_missing_relay_or_sdk_is_a_failure_not_a_skip(proof, tmp_path, pristine):
    assert any("non chargeable" in m for m in proof.run_checks(str(tmp_path / "absent.py"))["failures"])
    report = proof.run_checks(write_relay(tmp_path))
    assert any("VRAI SDK" in m for m in report["failures"]), "sans SDK installé le contrôle échoue au lieu d'être sauté"


def test_the_script_never_reads_a_credentials_file_nor_opens_a_non_loopback_socket():
    text = SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r"~/|expanduser|\.aws|\.ssh|\.config/|id_rsa|\.netrc", text)
    assert "0.0.0.0" not in text and "AF_INET6" not in text
