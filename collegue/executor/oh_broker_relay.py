"""Relais local du worker : ``127.0.0.1:<port>`` → socket Unix du courtier monté dans le conteneur.

Fichier AUTONOME (stdlib seule, aucun import ``collegue``) : il est copié dans l'image à côté du runner
(``COPY collegue/executor/oh_broker_relay.py /opt/oh_broker_relay.py``). Le conteneur du codeur n'a AUCUN réseau
(``--network none``) ; les clients HTTP/SDK du worker parlent à ce relais en loopback, et le relais ne fait qu'une chose :
recopier les octets vers le socket Unix du courtier (monté en lecture seule, répertoire ne contenant que ``broker.sock``).

Ce n'est pas un proxy : il n'interprète ni ne modifie rien (le courtier valide route, jeton, en-têtes et corps), il n'écoute
que sur la boucle locale, ne se connecte QU'AU socket configuré (jamais à une adresse fournie par la requête), borne le volume
client → courtier et le temps d'inactivité. Le jeton de session que le client y présente n'est pas une clé fournisseur.
"""

from __future__ import annotations

import os
import socket
import socketserver
import threading
from typing import Optional, Tuple

DEFAULT_SOCKET = "/run/collegue-broker/broker.sock"
LOOPBACK = "127.0.0.1"
MAX_CLIENT_BYTES = 512 * 1024 + 32 * 1024  # un peu plus que le plafond de requête du courtier (il tranche)
IDLE_SECONDS = 600.0
_CHUNK = 64 * 1024


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client: socket.socket = self.request
        client.settimeout(IDLE_SECONDS)
        broker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        broker.settimeout(IDLE_SECONDS)
        try:
            broker.connect(self.server.socket_path)  # type: ignore[attr-defined]
        except OSError:
            broker.close()
            return
        done = threading.Event()

        def upstream() -> None:
            sent = 0
            try:
                while not done.is_set():
                    chunk = client.recv(_CHUNK)
                    if not chunk:
                        break
                    sent += len(chunk)
                    if sent > MAX_CLIENT_BYTES:
                        break
                    broker.sendall(chunk)
                try:
                    broker.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
            except OSError:
                pass

        pump = threading.Thread(target=upstream, daemon=True)
        pump.start()
        try:
            while True:
                chunk = broker.recv(_CHUNK)
                if not chunk:
                    break
                client.sendall(chunk)
        except OSError:
            pass
        finally:
            done.set()
            for sock in (client, broker):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            broker.close()


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], socket_path: str):
        self.socket_path = socket_path
        super().__init__(address, _Handler)


def start(socket_path: Optional[str] = None, port: int = 0) -> Tuple[_Server, int]:
    """Démarre le relais en thread daemon ; renvoie ``(serveur, port)`` (``port=0`` : port libre choisi par le noyau)."""
    path = socket_path or os.environ.get("COLLEGUE_BROKER_SOCKET") or DEFAULT_SOCKET
    server = _Server((LOOPBACK, int(port)), path)
    threading.Thread(target=server.serve_forever, name="broker-relay", daemon=True).start()
    return server, int(server.server_address[1])


def main() -> int:  # pragma: no cover - utilisé seulement en exécution directe
    server, port = start(port=int(os.environ.get("COLLEGUE_BROKER_RELAY_PORT", "0")))
    print(f"oh_broker_relay: {LOOPBACK}:{port} -> {server.socket_path}", flush=True)
    threading.Event().wait()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
