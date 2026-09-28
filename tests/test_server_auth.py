"""Tests unitaires de ``collegue.core.server_auth`` (OAuth fail-closed, sans importer l'app)."""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace

import pytest

from collegue.core import server_auth
from collegue.core.server_auth import OAuthConfigurationError, build_auth_provider, is_loopback_host

JWT_MODULE = "fastmcp.server.auth.providers.jwt"


def _cfg(**overrides) -> SimpleNamespace:
    values = {
        "OAUTH_ENABLED": True,
        "OAUTH_JWKS_URI": "https://idp.example.invalid/jwks",
        "OAUTH_PUBLIC_KEY": None,
        "OAUTH_ISSUER": "https://idp.example.invalid/realm",
        "OAUTH_AUDIENCE": "collegue",
        "HOST": "127.0.0.1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _RecordingVerifier:
    calls: list[dict] = []

    def __init__(self, **kwargs) -> None:
        type(self).calls.append(kwargs)


@pytest.fixture
def recording_verifier(monkeypatch: pytest.MonkeyPatch):
    import fastmcp.server.auth.providers.jwt as jwt_module

    _RecordingVerifier.calls = []
    monkeypatch.setattr(jwt_module, "JWTVerifier", _RecordingVerifier)
    return _RecordingVerifier


def test_disabled_oauth_is_explicit_local_mode_without_auth(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=server_auth.logger.name):
        provider = build_auth_provider(_cfg(OAUTH_ENABLED=False))

    assert provider is None
    assert "mode local explicite" in caplog.text
    assert "SANS authentification" in caplog.text


def test_disabled_oauth_never_touches_the_jwt_machinery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, JWT_MODULE, None)  # tout import échouerait

    assert build_auth_provider(_cfg(OAUTH_ENABLED=False)) is None


def test_jwks_configuration_builds_the_verifier_with_issuer_and_audience(recording_verifier) -> None:
    provider = build_auth_provider(_cfg())

    assert isinstance(provider, recording_verifier)
    assert recording_verifier.calls == [
        {
            "jwks_uri": "https://idp.example.invalid/jwks",
            "issuer": "https://idp.example.invalid/realm",
            "audience": "collegue",
        }
    ]


def test_public_key_configuration_builds_the_verifier(recording_verifier) -> None:
    provider = build_auth_provider(_cfg(OAUTH_JWKS_URI=None, OAUTH_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----"))

    assert isinstance(provider, recording_verifier)
    assert recording_verifier.calls[0]["public_key"] == "-----BEGIN PUBLIC KEY-----"
    assert "jwks_uri" not in recording_verifier.calls[0]


def test_jwks_takes_precedence_over_public_key(recording_verifier) -> None:
    build_auth_provider(_cfg(OAUTH_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----"))

    assert set(recording_verifier.calls[0]) == {"jwks_uri", "issuer", "audience"}


def test_constructor_failure_blocks_startup_and_keeps_the_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    import fastmcp.server.auth.providers.jwt as jwt_module

    class Exploding:
        def __init__(self, **kwargs) -> None:
            raise ValueError("clé illisible")

    monkeypatch.setattr(jwt_module, "JWTVerifier", Exploding)

    with pytest.raises(OAuthConfigurationError) as excinfo:
        build_auth_provider(_cfg())

    assert "OAUTH_ENABLED=true" in str(excinfo.value)
    assert "clé illisible" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ValueError)


def test_missing_jwt_verifier_import_blocks_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, JWT_MODULE, None)

    with pytest.raises(OAuthConfigurationError) as excinfo:
        build_auth_provider(_cfg())

    assert "JWTVerifier" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ImportError)


def test_missing_key_material_blocks_startup(recording_verifier) -> None:
    with pytest.raises(OAuthConfigurationError, match="OAUTH_JWKS_URI"):
        build_auth_provider(_cfg(OAUTH_JWKS_URI=None, OAUTH_PUBLIC_KEY=None))

    assert recording_verifier.calls == []


def test_missing_issuer_blocks_startup(recording_verifier) -> None:
    """Défense en profondeur : sans issuer, n'importe quel émetteur signant avec la clé serait accepté."""

    with pytest.raises(OAuthConfigurationError, match="OAUTH_ISSUER"):
        build_auth_provider(_cfg(OAUTH_ISSUER=None))

    assert recording_verifier.calls == []


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_blank_key_material_counts_as_missing(recording_verifier, blank: str) -> None:
    with pytest.raises(OAuthConfigurationError, match="OAUTH_JWKS_URI"):
        build_auth_provider(_cfg(OAUTH_JWKS_URI=blank, OAUTH_PUBLIC_KEY=blank))

    assert recording_verifier.calls == []


def test_blank_jwks_falls_back_to_a_real_public_key(recording_verifier) -> None:
    build_auth_provider(_cfg(OAUTH_JWKS_URI="  ", OAUTH_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----"))

    assert set(recording_verifier.calls[0]) == {"public_key", "issuer", "audience"}


def test_blank_issuer_blocks_startup(recording_verifier) -> None:
    with pytest.raises(OAuthConfigurationError, match="OAUTH_ISSUER"):
        build_auth_provider(_cfg(OAUTH_ISSUER="   "))

    assert recording_verifier.calls == []


def test_configuration_error_is_a_runtime_error() -> None:
    assert issubclass(OAuthConfigurationError, RuntimeError)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.1.2.3", "::1", "[::1]", "localhost", "LOCALHOST"])
def test_loopback_hosts(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "10.0.0.5", "example.com", "", None])
def test_non_loopback_hosts(host) -> None:
    assert is_loopback_host(host) is False


def test_remote_host_without_oauth_warns_loudly(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=server_auth.logger.name):
        assert build_auth_provider(_cfg(OAUTH_ENABLED=False, HOST="0.0.0.0")) is None

    assert "0.0.0.0" in caplog.text
    assert "OAUTH_ENABLED=true" in caplog.text


def test_remote_publish_host_without_oauth_warns_loudly(caplog: pytest.LogCaptureFixture) -> None:
    """Dans Docker, HOST reste interne : c'est l'adresse de PUBLICATION qui expose le service."""

    with caplog.at_level(logging.WARNING, logger=server_auth.logger.name):
        assert build_auth_provider(_cfg(OAUTH_ENABLED=False, COLLEGUE_PUBLISH_HOST="0.0.0.0")) is None

    assert "COLLEGUE_PUBLISH_HOST=0.0.0.0" in caplog.text
    assert "OAUTH_ENABLED=true" in caplog.text


def test_remote_exposure_with_oauth_enabled_does_not_warn(recording_verifier, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=server_auth.logger.name):
        build_auth_provider(_cfg(HOST="0.0.0.0", COLLEGUE_PUBLISH_HOST="0.0.0.0"))

    assert caplog.records == []


def test_loopback_host_without_oauth_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=server_auth.logger.name):
        build_auth_provider(_cfg(OAUTH_ENABLED=False, HOST="127.0.0.1", COLLEGUE_PUBLISH_HOST="127.0.0.1"))

    assert caplog.records == []


def test_default_host_is_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    from collegue.config import Settings

    monkeypatch.delenv("HOST", raising=False)

    assert Settings(_env_file=None).HOST == "127.0.0.1"


def test_explicit_host_override_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    from collegue.config import Settings

    monkeypatch.setenv("HOST", "0.0.0.0")

    assert Settings(_env_file=None).HOST == "0.0.0.0"


def test_publish_host_setting_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from collegue.config import Settings

    monkeypatch.delenv("COLLEGUE_PUBLISH_HOST", raising=False)
    assert Settings(_env_file=None).COLLEGUE_PUBLISH_HOST is None

    monkeypatch.setenv("COLLEGUE_PUBLISH_HOST", "0.0.0.0")
    assert Settings(_env_file=None).COLLEGUE_PUBLISH_HOST == "0.0.0.0"
