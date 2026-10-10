#!/usr/bin/env python3
"""Contrôle CI SANS MODÈLE ni clé du transport W5, dans l'image qui embarque le VRAI SDK OpenHands (propriété C, vague 5).

Exécuté par le job « Docker build » dans un conteneur **sans réseau** (``--network none``) et **sans aucun secret hôte** : le script
est transmis sur l'entrée standard (``python - < scripts/ci_w5_broker_transport.py``), donc aucun montage. Aucun appel LLM, aucun
fournisseur : le « courtier » ci-dessous est un FAUX serveur HTTP local sur socket Unix, qui joue le rôle du courtier budgétaire
côté hôte (le vrai courtier, ses budgets et son fournisseur sont éprouvés par les tests de A et par la preuve d'intégration hôte).

Ce que le script établit :

1. **isolation** : la seule interface réseau est la boucle locale, aucune connexion externe n'aboutit, aucune variable d'environnement
   ne ressemble à une clé ou à un secret (noms seulement, jamais de valeur) ;
2. **chemin réel** : le VRAI ``openhands.sdk.LLM`` (celui du codeur) appelle ``http://127.0.0.1:<port>/v1``, le relais embarqué
   ``/opt/oh_broker_relay.py`` recopie les octets vers le socket Unix, le faux courtier reçoit UNE requête ``POST /v1/chat/completions``
   avec le jeton de session factice (et lui seul) et répond au format OpenAI avec ``usage`` ; le SDK relit réponse et usage ;
3. **contenu de l'image** : chaque entrée du verrou embarqué ``/opt/locks/sandbox-broker.txt`` (marqueurs évalués) est installée EXACTEMENT à
   la version verrouillée, et l'application web legacy (``openhands-ai``) ainsi que ``python-jose`` / ``ecdsa`` / ``passlib`` sont ABSENTES ;
4. **le relais n'est pas un proxy** : une requête vers une autre route, ou vers une URL absolue, n'atteint que le faux courtier (404,
   aucune sortie) ; un corps client au-delà du plafond du relais est coupé avant le courtier.

Sortie : un rapport JSON sur stdout (aucune clé) ; code 0 si tout est vérifié, 1 sinon.
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import importlib
import importlib.util
import json
import os
import re
import socket
import socketserver
import sys
import tempfile
import threading
from typing import Any, Dict, List, Optional

DEFAULT_RELAY = "/opt/oh_broker_relay.py"
DEFAULT_LOCK = "/opt/locks/sandbox-broker.txt"
FORBIDDEN_DISTRIBUTIONS = ("openhands-ai", "python-jose", "ecdsa", "passlib")
SESSION_TOKEN = "w5-ci-session-token-0123456789abcdef"  # FACTICE : n'ouvre rien, jamais une clé fournisseur
MODEL = "gemma-4-31b-it"
ANSWER = "pong"
ROUTE = "/v1/chat/completions"
SECRET_LIKE = re.compile(r"(API_?KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)", re.I)
EXTERNAL_PROBES = (("192.0.2.1", 80), ("1.1.1.1", 443), ("8.8.8.8", 53))  # TEST-NET-1 puis résolveurs publics


class Failures:
    def __init__(self) -> None:
        self.items: List[str] = []

    def check(self, condition: bool, message: str) -> bool:
        if not condition:
            self.items.append(message)
        return bool(condition)


# ── faux courtier (socket Unix) ───────────────────────────────────────────────────────────────────────────────────────────


class FakeBroker:
    """Répond ``POST /v1/chat/completions`` au format OpenAI ; tout le reste est 404. Consigne ce qu'il reçoit."""

    def __init__(self, socket_path: str, token: str = SESSION_TOKEN):
        self.socket_path = socket_path
        self.token = token
        self.requests: List[Dict[str, Any]] = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # pas de journal : le client d'un socket Unix n'a pas d'adresse
                return

            def address_string(self) -> str:
                return "unix"

            def _send(self, status: int, payload: Dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                try:
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):  # client coupé (corps trop gros rejeté par le relais)
                    pass
                self.close_connection = True

            def do_POST(self) -> None:  # noqa: N802 - API de http.server
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                record: Dict[str, Any] = {
                    "method": "POST",
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "bytes": len(raw),
                }
                owner.requests.append(record)
                if self.path != ROUTE:
                    self._send(404, {"error": {"message": "route inconnue", "type": "not_found"}})
                    return
                if self.headers.get("Authorization") != f"Bearer {owner.token}":
                    self._send(401, {"error": {"message": "jeton de session inconnu", "type": "auth"}})
                    return
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    self._send(400, {"error": {"message": "JSON invalide", "type": "invalid_request"}})
                    return
                record["model"] = body.get("model")
                record["messages"] = len(body.get("messages") or [])
                record["fields"] = (
                    sorted(str(key) for key in body) if isinstance(body, dict) else []
                )  # NOMS seulement, jamais de contenu
                self._send(
                    200,
                    {
                        "id": "chatcmpl-w5-ci",
                        "object": "chat.completion",
                        "created": 1,
                        "model": body.get("model") or MODEL,
                        "choices": [
                            {"index": 0, "message": {"role": "assistant", "content": ANSWER}, "finish_reason": "stop"}
                        ],
                        "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
                    },
                )

            def do_GET(self) -> None:  # noqa: N802
                owner.requests.append({"method": "GET", "path": self.path, "authorization": None, "bytes": 0})
                self._send(404, {"error": {"message": "route inconnue", "type": "not_found"}})

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.server = Server(socket_path, Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name="fake-broker", daemon=True)

    def start(self) -> "FakeBroker":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# ── contrôles ─────────────────────────────────────────────────────────────────────────────────────────────────────────────


def load_relay(path: str):
    spec = importlib.util.spec_from_file_location("oh_broker_relay_under_test", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"relais non chargeable : {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def interfaces(proc_net_dev: str = "/proc/net/dev") -> List[str]:
    with open(proc_net_dev, encoding="utf-8") as handle:
        lines = handle.read().splitlines()[2:]
    return sorted(line.split(":", 1)[0].strip() for line in lines if ":" in line)


def external_connections(timeout: float = 2.0) -> List[str]:
    """Adresses externes auxquelles une connexion TCP a ABOUTI (doit être vide sous ``--network none``)."""
    reached = []
    for host, port in EXTERNAL_PROBES:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                reached.append(f"{host}:{port}")
        except OSError:
            pass
    return reached


def check_isolation(failures: Failures, environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    env = dict(os.environ if environ is None else environ)
    leaking = sorted(name for name in env if SECRET_LIKE.search(name))
    nets = interfaces()
    reached = external_connections()
    failures.check(not leaking, f"variables ressemblant à une clé dans le conteneur : {leaking}")
    failures.check(nets == ["lo"], f"interfaces réseau autres que la boucle locale : {nets}")
    failures.check(not reached, f"connexion externe aboutie malgré --network none : {reached}")
    return {"secret_like_variables": leaking, "interfaces": nets, "external_connections": reached}


def _http(port: int, method: str, target: str, body: Optional[bytes] = None) -> Dict[str, Any]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        headers = {"Authorization": f"Bearer {SESSION_TOKEN}", "Content-Type": "application/json"}
        conn.request(method, target, body=body, headers=headers)
        response = conn.getresponse()
        return {"status": response.status, "body": response.read()[:2000]}
    except (OSError, http.client.HTTPException) as exc:
        return {"status": None, "error": type(exc).__name__}
    finally:
        conn.close()


def message_text(response: Any) -> str:
    """Texte d'un ``LLMResponse`` du SDK (``response.message.content`` : liste de blocs, texte dans ``.text``)."""
    content = getattr(getattr(response, "message", None), "content", None)
    if isinstance(content, str):
        return content
    return "".join(str(getattr(block, "text", "") or "") for block in (content or []))


#: Champs de premier niveau que le courtier accepte (miroir de ``collegue.broker.translate._ALLOWED_TOP_LEVEL``, plus ses champs inertes) : le script
#: tourne dans l'image, où le produit n'est pas installé ; ``tests/test_w5_ci_transport_proof.py`` garde l'égalité des deux ensembles.
BROKER_ACCEPTED_FIELDS = frozenset(
    {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "response_format",
        "stop",
        "n",
        "stream",
        "seed",
        "parallel_tool_calls",
    }
)


def unaccepted_fields(fields: Any) -> List[str]:
    """Noms de champs d'une requête que le courtier ne reconnaît pas (liste vide = tous acceptés)."""
    return sorted(str(name) for name in fields if name not in BROKER_ACCEPTED_FIELDS)


def check_sdk_through_relay(port: int, broker: FakeBroker, failures: Failures) -> Dict[str, Any]:
    os.environ.setdefault(
        "LITELLM_LOCAL_MODEL_COST_MAP", "True"
    )  # pas de téléchargement de la table de prix (aucun réseau)
    sdk = importlib.import_module("openhands.sdk")
    from pydantic import SecretStr

    llm = sdk.LLM(  # mêmes options explicites que le runner du produit en mode courtier (oh_runner.run_with)
        model=f"openai/{MODEL}",
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key=SecretStr(SESSION_TOKEN),
        usage_id="coder",
        num_retries=0,
        timeout=30,
        max_output_tokens=64,
        reasoning_effort=None,
    )
    before = len(broker.requests)
    response = llm.completion(
        messages=[sdk.Message(role="user", content=[sdk.TextContent(text="ping")])],
    )
    seen = broker.requests[before:]
    failures.check(len(seen) == 1, f"le faux courtier devait recevoir UNE requête du SDK, il en a reçu {len(seen)}")
    request = seen[0] if seen else {}
    failures.check(request.get("path") == ROUTE and request.get("method") == "POST", f"route inattendue : {request}")
    failures.check(
        request.get("authorization") == f"Bearer {SESSION_TOKEN}", "le jeton de session n'est pas celui présenté"
    )
    failures.check(MODEL in str(request.get("model")), f"modèle transmis inattendu : {request.get('model')!r}")
    refused = unaccepted_fields(request.get("fields") or [])
    failures.check(
        not refused,
        f"champ(s) de la requête du SDK que le courtier refuserait (noms seulement) : {refused} ; vus : {request.get('fields')}",
    )
    failures.check(
        message_text(response) == ANSWER, f"réponse du courtier non relue par le SDK : {message_text(response)!r}"
    )
    usage = getattr(getattr(llm, "metrics", None), "accumulated_token_usage", None)
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    failures.check(
        (prompt, completion) == (7, 2), f"usage relu par le SDK ≠ usage du courtier : {(prompt, completion)}"
    )
    return {
        "requests": seen,
        "request_fields": request.get("fields"),
        "usage": [prompt, completion],
        "usage_id": getattr(llm, "usage_id", None),
    }


def check_relay_is_not_a_proxy(relay: Any, port: int, broker: FakeBroker, failures: Failures) -> Dict[str, Any]:
    before = len(broker.requests)
    other_route = _http(port, "POST", "/v1/models", b"{}")
    absolute = _http(port, "GET", "http://example.com/steal")
    failures.check(other_route.get("status") == 404, f"autre route : {other_route}")
    failures.check(absolute.get("status") == 404, f"URL absolue : {absolute}")
    paths = [r["path"] for r in broker.requests[before:]]
    failures.check(paths == ["/v1/models", "http://example.com/steal"], f"chemins vus par le courtier : {paths}")
    before = len(broker.requests)
    oversized = _http(port, "POST", ROUTE, b"x" * (int(relay.MAX_CLIENT_BYTES) + 64 * 1024))
    delivered = [r for r in broker.requests[before:] if r["bytes"] > int(relay.MAX_CLIENT_BYTES)]
    failures.check(oversized.get("status") != 200 and not delivered, f"corps au-delà du plafond transmis : {oversized}")
    return {
        "other_route": other_route["status"],
        "absolute_url": absolute["status"],
        "oversized": oversized.get("status"),
    }


_LOCK_ENTRY = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;\\]+)(?:\s*;\s*(?P<marker>[^\\]*?))?\s*\\?$"
)


def lock_entries(text: str) -> List[Dict[str, Optional[str]]]:
    entries = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "--hash")):
            continue
        match = _LOCK_ENTRY.match(line)
        if match:
            entries.append(match.groupdict())
    return entries


def check_image_contents(lock_path: str, failures: Failures, min_entries: int = 100) -> Dict[str, Any]:
    """L'image EST son verrou : chaque entrée applicable installée à la version exacte ; distributions interdites absentes."""
    import importlib.metadata as metadata

    from packaging.markers import Marker

    try:
        with open(lock_path, encoding="utf-8") as handle:
            entries = lock_entries(handle.read())
    except OSError as exc:
        failures.check(False, f"verrou embarqué illisible ({lock_path}) : {type(exc).__name__}")
        return {"lock": lock_path, "entries": 0}
    failures.check(len(entries) >= min_entries, f"verrou embarqué anormalement court : {len(entries)} entrées")
    checked, skipped, wrong, missing = 0, 0, [], []
    for entry in entries:
        marker = entry.get("marker")
        if marker and not Marker(marker.strip()).evaluate():
            skipped += 1
            continue
        try:
            installed = metadata.version(str(entry["name"]))
        except metadata.PackageNotFoundError:
            missing.append(str(entry["name"]))
            continue
        checked += 1
        if installed != entry["version"]:
            wrong.append(f"{entry['name']} {installed} ≠ {entry['version']}")
    failures.check(not missing, f"entrées du verrou non installées : {missing[:10]}")
    failures.check(not wrong, f"versions installées différentes du verrou : {wrong[:10]}")
    present = []
    for name in FORBIDDEN_DISTRIBUTIONS:
        try:
            metadata.version(name)
            present.append(name)
        except metadata.PackageNotFoundError:
            pass
    failures.check(not present, f"distributions interdites présentes dans l'image broker : {present}")
    return {
        "lock": lock_path,
        "entries": len(entries),
        "checked": checked,
        "skipped_by_marker": skipped,
        "forbidden_present": present,
    }


def run_checks(relay_path: str, lock_path: Optional[str] = DEFAULT_LOCK) -> Dict[str, Any]:
    failures = Failures()
    report: Dict[str, Any] = {"relay_path": relay_path}
    report["isolation"] = check_isolation(failures)
    if lock_path:
        report["image_contents"] = check_image_contents(lock_path, failures)
    try:
        relay = load_relay(relay_path)
    except Exception as exc:  # noqa: BLE001
        failures.check(False, f"relais embarqué non chargeable ({relay_path}) : {type(exc).__name__}: {exc}")
        report["failures"] = failures.items
        return report
    with tempfile.TemporaryDirectory(prefix="w5-broker-") as tmp:
        os.chmod(tmp, 0o700)
        broker = FakeBroker(os.path.join(tmp, "broker.sock")).start()
        server = None
        try:
            server, port = relay.start(socket_path=broker.socket_path, port=0)
            report["relay_port_is_loopback"] = server.server_address[0] == "127.0.0.1"
            failures.check(
                report["relay_port_is_loopback"],
                f"le relais n'écoute pas sur la boucle locale : {server.server_address}",
            )
            try:
                report["sdk"] = check_sdk_through_relay(port, broker, failures)
            except Exception as exc:  # noqa: BLE001
                failures.check(False, f"appel du VRAI SDK par le relais en échec : {type(exc).__name__}: {exc}")
            report["relay"] = check_relay_is_not_a_proxy(relay, port, broker, failures)
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            broker.stop()
    report["failures"] = failures.items
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--relay", default=DEFAULT_RELAY)
    parser.add_argument("--lock", default=DEFAULT_LOCK, help="verrou embarqué ('' pour ne pas contrôler le contenu)")
    args = parser.parse_args(argv)
    report = run_checks(args.relay, args.lock or None)
    report["ok"] = not report["failures"]
    print(json.dumps(report, indent=1, ensure_ascii=False, sort_keys=True, default=str))
    for message in report["failures"]:
        print(f"ECART: {message}", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
