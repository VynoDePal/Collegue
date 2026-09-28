"""OAuth fail-closed au démarrage de ``collegue.app``.

Défaut d'origine : une exception du constructeur ``JWTVerifier`` ou l'absence de
l'import était journalisée, puis FastMCP démarrait avec ``auth=None`` — donc SANS
authentification alors que l'opérateur avait demandé ``OAUTH_ENABLED=true``.

Ces tests démarrent réellement ``collegue.app`` dans un sous-processus (le module
lit ``settings`` à l'import) et observent le code de sortie ainsi que l'état
d'authentification de l'application. Aucun appel réseau : le lifespan n'est jamais
exécuté et le JWKS n'est jamais interrogé (une requête sans jeton est refusée
avant toute vérification de signature).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[1]
RESULT_MARKER = "__APP_RESULT__"

# Piloté par argv[1] : « inject » simule une panne du chemin JWT AVANT l'import de
# l'application, « probe » envoie une requête MCP sans jeton pour observer si
# l'authentification est réellement appliquée.
_DRIVER = r"""
import json
import sys

inject = sys.argv[1]
probe = sys.argv[2] == "probe"

if inject == "jwt-import-absent":
    # Une entrée None dans sys.modules fait échouer `from ... import ...` en ImportError.
    sys.modules["fastmcp.server.auth.providers.jwt"] = None
elif inject == "jwt-ctor-error":
    import fastmcp.server.auth.providers.jwt as jwt_module

    def _boom(self, *args, **kwargs):
        raise RuntimeError("panne simulée du constructeur JWTVerifier")

    jwt_module.JWTVerifier.__init__ = _boom

import collegue.app as collegue_app

auth = collegue_app.app.auth
result = {"auth": None if auth is None else type(auth).__name__, "status": None}

if probe:
    from starlette.testclient import TestClient

    web = collegue_app.app.http_app(path="/mcp/")
    client = TestClient(web, raise_server_exceptions=False)
    response = client.post(
        "/mcp/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "oauth-test", "version": "0"},
            },
        },
        headers={"Accept": "application/json, text/event-stream"},
    )
    result["status"] = response.status_code
    result["www_authenticate"] = response.headers.get("www-authenticate")

print("__APP_RESULT__" + json.dumps(result))
"""


@pytest.fixture(scope="module")
def rsa_public_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )


def _run_app(*, oauth_env: dict[str, str] | None = None, inject: str = "none", probe: bool = False):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OAUTH_", "PILOT_")) and key not in {"HOST", "PORT"}
    }
    env["PYTHONPATH"] = str(ROOT)
    env.update(oauth_env or {})
    completed = subprocess.run(
        [sys.executable, "-c", _DRIVER, inject, "probe" if probe else "import"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    result = None
    for line in completed.stdout.splitlines():
        if line.startswith(RESULT_MARKER):
            result = json.loads(line[len(RESULT_MARKER) :])
    return completed, result


def _jwks_env() -> dict[str, str]:
    return {
        "OAUTH_ENABLED": "true",
        "OAUTH_JWKS_URI": "https://idp.example.invalid/realms/test/protocol/openid-connect/certs",
        "OAUTH_ISSUER": "https://idp.example.invalid/realms/test",
        "OAUTH_AUDIENCE": "collegue",
    }


def test_local_mode_starts_explicitly_without_auth() -> None:
    completed, result = _run_app(probe=True)

    assert completed.returncode == 0, completed.stderr[-2000:]
    assert result is not None
    assert result["auth"] is None
    # Sans OAuth, aucune barrière d'authentification : la requête n'est pas rejetée en 401.
    assert result["status"] not in (401, 403)


def test_oauth_jwks_mode_starts_and_rejects_requests_without_bearer() -> None:
    completed, result = _run_app(oauth_env=_jwks_env(), probe=True)

    assert completed.returncode == 0, completed.stderr[-2000:]
    assert result is not None
    assert result["auth"] == "JWTVerifier"
    assert result["status"] == 401
    assert "bearer" in (result["www_authenticate"] or "").lower()


def test_oauth_public_key_mode_starts_and_rejects_requests_without_bearer(rsa_public_pem: str) -> None:
    oauth_env = {
        "OAUTH_ENABLED": "true",
        "OAUTH_PUBLIC_KEY": rsa_public_pem,
        "OAUTH_ISSUER": "https://idp.example.invalid/realms/test",
    }
    completed, result = _run_app(oauth_env=oauth_env, probe=True)

    assert completed.returncode == 0, completed.stderr[-2000:]
    assert result is not None
    assert result["auth"] == "JWTVerifier"
    assert result["status"] == 401


def test_oauth_enabled_but_jwt_constructor_fails_blocks_startup() -> None:
    completed, result = _run_app(oauth_env=_jwks_env(), inject="jwt-ctor-error")

    assert result is None, "l'application ne doit jamais atteindre l'état « démarré » sans l'auth demandée"
    assert completed.returncode != 0
    assert "OAuthConfigurationError" in completed.stderr
    assert "OAUTH_ENABLED" in completed.stderr


def test_oauth_enabled_but_jwt_verifier_import_absent_blocks_startup() -> None:
    completed, result = _run_app(oauth_env=_jwks_env(), inject="jwt-import-absent")

    assert result is None, "l'application ne doit jamais atteindre l'état « démarré » sans l'auth demandée"
    assert completed.returncode != 0
    assert "OAuthConfigurationError" in completed.stderr
    assert "OAUTH_ENABLED" in completed.stderr


def test_oauth_enabled_without_key_material_blocks_startup() -> None:
    completed, result = _run_app(
        oauth_env={"OAUTH_ENABLED": "true", "OAUTH_ISSUER": "https://idp.example.invalid/realms/test"}
    )

    assert result is None
    assert completed.returncode != 0
    assert "OAUTH_JWKS_URI" in completed.stderr


def test_jwt_failures_do_not_affect_local_mode() -> None:
    """Sans OAUTH_ENABLED, ni l'import ni le constructeur JWT ne sont sollicités."""

    for inject in ("jwt-import-absent", "jwt-ctor-error"):
        completed, result = _run_app(inject=inject)

        assert completed.returncode == 0, (inject, completed.stderr[-2000:])
        assert result is not None
        assert result["auth"] is None
