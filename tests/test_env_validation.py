import pytest
from pydantic import ValidationError
from collegue.config import Settings

def test_sentry_dsn_validation():
    # Valid DSN
    settings = Settings(SENTRY_DSN="https://abcdef@o1234.ingest.sentry.io/5678", OAUTH_ENABLED=False)
    assert settings.SENTRY_DSN.startswith("http")
    
    # Invalid DSN
    with pytest.raises(ValidationError) as exc:
        Settings(SENTRY_DSN="invalid_dsn_string", OAUTH_ENABLED=False)
    assert "SENTRY_DSN configuré semble invalide" in str(exc.value)

def test_oauth_validation():
    # Valid OAuth config
    settings = Settings(
        OAUTH_ENABLED=True,
        OAUTH_ISSUER="http://localhost:8080/realms/master",
        OAUTH_JWKS_URI="http://localhost:8080/realms/master/protocol/openid-connect/certs"
    )
    assert settings.OAUTH_ENABLED is True
    
    # Invalid OAuth: Missing JWKS and Public Key
    with pytest.raises(ValidationError) as exc:
        Settings(
            OAUTH_ENABLED=True,
            OAUTH_ISSUER="http://localhost:8080/realms/master"
        )
    assert "OAUTH_ENABLED est true mais ni OAUTH_JWKS_URI ni OAUTH_PUBLIC_KEY" in str(exc.value)
    
    # Invalid OAuth: Missing Issuer
    with pytest.raises(ValidationError) as exc:
        Settings(
            OAUTH_ENABLED=True,
            OAUTH_JWKS_URI="http://localhost/certs"
        )
    assert "OAUTH_ISSUER est requis" in str(exc.value)

    # Invalid OAuth: Bad JWKS URI format
    with pytest.raises(ValidationError) as exc:
        Settings(
            OAUTH_ENABLED=True,
            OAUTH_ISSUER="http://localhost/issuer",
            OAUTH_JWKS_URI="not_a_url"
        )
    assert "OAUTH_JWKS_URI doit être une URL HTTP/HTTPS valide" in str(exc.value)
