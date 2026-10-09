"""Serveur de socket Unix d'UNE session de worker — ce n'est PAS un proxy HTTP général.

Surface exposée au conteneur (qui n'a aucun réseau) : un répertoire ne contenant QUE ``broker.sock``. Ce serveur :

* est lié à UNE session côté serveur : le jeton présenté n'ouvre que celle-là (aucune autre session, aucun registre) ;
* n'implémente qu'une route, ``POST /v1/chat/completions`` ; tout le reste est 404 (ni administration, ni liste, ni santé) ;
* lit le corps avec ``Content-Length`` borné (``Transfer-Encoding`` et ``Expect`` refusés), une requête par connexion
  (``Connection: close``), des délais de lecture bornés ;
* ne relaie AUCUN en-tête du client : seuls ``Authorization`` (jeton de session), ``Content-Length``, ``Content-Type`` et
  ``Idempotency-Key`` (clé de rejeu, charset borné) sont lus ;
* renvoie les erreurs au format OpenAI, sans secret ; aucune redirection n'est jamais émise.

Il tourne dans un thread dédié avec sa propre boucle asyncio (le worker est lancé par un appel bloquant côté hôte).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import tempfile
import threading
from pathlib import Path
from typing import Optional

from collegue.broker.errors import BrokerAuthError, BrokerError, BrokerRequestRefused
from collegue.broker.policy import MAX_REQUEST_BYTES
from collegue.broker.service import BrokerService

SOCKET_NAME = "broker.sock"
ROUTE = "/v1/chat/completions"
MAX_HEADER_BYTES = 16 * 1024
MAX_HEADERS = 64
READ_TIMEOUT = 30.0
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9._:\-]{1,96}$")


def _response(status: int, payload: dict) -> bytes:
    reason = {
        200: "OK",
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        408: "Request Timeout",
        409: "Conflict",
        411: "Length Required",
        413: "Payload Too Large",
        429: "Too Many Requests",
        500: "Internal Server Error",
        502: "Bad Gateway",
    }.get(status, "Error")
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    head = (
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\nCache-Control: no-store\r\n\r\n"
    ).encode("ascii")
    return head + body


class BrokerSocketServer:
    """Socket Unix d'une session. ``start()`` crée le répertoire privé et le socket ; ``stop()`` les supprime."""

    def __init__(self, service: BrokerService, session_id: str, *, run_root: Optional[str] = None):
        self._service = service
        self._session_id = session_id
        self._run_root = run_root
        self._dir: Optional[Path] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[asyncio.AbstractServer] = None
        self._ready = threading.Event()
        self._failure: Optional[BaseException] = None
        self.requests_handled = 0

    # ── cycle de vie ─────────────────────────────────────────────────────────────────────────────────────

    @property
    def directory(self) -> str:
        if self._dir is None:
            raise RuntimeError("serveur non démarré")
        return str(self._dir)

    @property
    def socket_path(self) -> str:
        return str(Path(self.directory) / SOCKET_NAME)

    def start(self) -> "BrokerSocketServer":
        """Démarre le serveur ; le répertoire (0700, hors de tout workspace) ne contient que le socket (0600)."""
        root = self._run_root or tempfile.gettempdir()
        os.makedirs(root, mode=0o700, exist_ok=True)
        self._dir = Path(tempfile.mkdtemp(prefix="cbk-", dir=root))
        os.chmod(self._dir, 0o700)
        self._thread = threading.Thread(target=self._run, name=f"broker-{self._session_id}", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=15) or self._failure is not None:
            self.stop()
            raise RuntimeError(f"serveur de socket non démarré : {self._failure!r}")
        mode = os.lstat(self.socket_path).st_mode
        if not stat.S_ISSOCK(mode):
            self.stop()
            raise RuntimeError("le point d'accès du courtier n'est pas un socket")
        os.chmod(self.socket_path, 0o600)
        return self

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._server = loop.run_until_complete(asyncio.start_unix_server(self._handle, path=self.socket_path))
        except BaseException as exc:  # noqa: BLE001
            self._failure = exc
            self._ready.set()
            loop.close()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            self._server.close()
            loop.run_until_complete(self._server.wait_closed())
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def stop(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is not None and thread is not None and thread.is_alive():
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=15)
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def __enter__(self) -> "BrokerSocketServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ── protocole ────────────────────────────────────────────────────────────────────────────────────────

    async def _read_request(self, reader: asyncio.StreamReader):
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=READ_TIMEOUT)
        if len(raw) > MAX_HEADER_BYTES:
            raise BrokerRequestRefused("en-têtes trop gros", code="headers_too_large", status=400)
        lines = raw.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            raise BrokerRequestRefused("requête HTTP invalide", code="bad_request", status=400)
        headers = {}
        for line in lines[1:]:
            if not line:
                continue
            if ":" not in line or len(headers) >= MAX_HEADERS:
                raise BrokerRequestRefused("en-tête invalide", code="bad_request", status=400)
            name, value = line.split(":", 1)
            key = name.strip().lower()
            if key in headers:
                raise BrokerRequestRefused(f"en-tête dupliqué : {key}", code="bad_request", status=400)
            headers[key] = value.strip()
        return parts[0], parts[1], headers

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            status, payload = await self._dispatch(reader)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            status, payload = (
                408,
                {"error": {"message": "requête incomplète", "type": "collegue_broker_error", "code": "timeout"}},
            )
        except BrokerError as exc:
            status, payload = exc.status, exc.to_openai_error()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - jamais de trace ni de secret vers le client
            status = 500
            payload = {
                "error": {
                    "message": f"erreur interne du courtier ({type(exc).__name__})",
                    "type": "collegue_broker_error",
                    "code": "internal",
                }
            }
        try:
            writer.write(_response(status, payload))
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def _dispatch(self, reader: asyncio.StreamReader):
        method, target, headers = await self._read_request(reader)
        self.requests_handled += 1
        if method != "POST" or target != ROUTE:
            raise BrokerRequestRefused("route inconnue", code="not_found", status=404)
        if "transfer-encoding" in headers or "expect" in headers:
            raise BrokerRequestRefused("Transfer-Encoding / Expect non pris en charge", code="bad_request", status=400)
        length_text = headers.get("content-length", "")
        if not length_text.isdigit():
            raise BrokerRequestRefused("Content-Length requis", code="length_required", status=411)
        length = int(length_text)
        if length > MAX_REQUEST_BYTES:
            raise BrokerRequestRefused(
                f"corps de requête trop gros ({length} > {MAX_REQUEST_BYTES})", code="payload_too_large", status=413
            )
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("bearer ") or len(auth) < 8:
            raise BrokerAuthError("jeton de session requis (Authorization: Bearer …)")
        token = auth[7:].strip()
        body = await asyncio.wait_for(reader.readexactly(length), timeout=READ_TIMEOUT)
        key = headers.get("idempotency-key")
        if key is not None and not _IDEMPOTENCY.match(key):
            raise BrokerRequestRefused("Idempotency-Key invalide", code="bad_request", status=400)
        completion = await self._service.chat_completion(self._session_id, token, body, request_id=key)
        return 200, completion
