"""Tests #465 : logging honnête de ``_execute_with_retry`` (clients API).

Un 404 attendu (sondage d'existence) court-circuite au 1er essai : le message
final doit porter le compteur RÉEL (« after 1 attempt(s) », pas « after 4
attempts ») et partir en ``debug`` — sur un run de plusieurs heures, des
centaines de fausses « erreurs après 4 tentatives » noyaient les vraies pannes
(run FacNor v3 : jusqu'à 17 lignes error par PR ouverte).
"""

import pytest

from collegue.tools.clients.base import APIClient, APIError


class _Client(APIClient):
    """Client minimal : pas de réseau, l'opération est injectée par le test."""

    def __init__(self):
        super().__init__(base_url="https://api.example.test", max_retries=3, retry_delay=0.0)


class _HTTPError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _always_raise(exc):
    def _op():
        raise exc

    return _op


def test_expected_404_logs_real_attempt_count_at_debug(caplog):
    client = _Client()
    with caplog.at_level("DEBUG", logger=client.logger.name):
        with pytest.raises(APIError) as excinfo:
            client._execute_with_retry(_always_raise(_HTTPError(404, "Ressource introuvable")), "GET contents/x")
    # Compteur RÉEL : le 404 n'est jamais retenté → 1 tentative, pas 4.
    assert "failed after 1 attempt(s)" in str(excinfo.value)
    assert excinfo.value.status_code == 404
    records = [r for r in caplog.records if "failed after" in r.message]
    assert records and all(r.levelname == "DEBUG" for r in records)


def test_exhausted_retries_log_error_with_real_count(caplog):
    client = _Client()
    with caplog.at_level("DEBUG", logger=client.logger.name):
        with pytest.raises(APIError) as excinfo:
            client._execute_with_retry(_always_raise(_HTTPError(503, "Service Unavailable")), "GET ref")
    # 503 retenté jusqu'à épuisement : max_retries + 1 tentatives, en ERROR.
    assert "failed after 4 attempt(s)" in str(excinfo.value)
    assert excinfo.value.status_code == 503
    records = [r for r in caplog.records if "failed after" in r.message]
    assert records and all(r.levelname == "ERROR" for r in records)


def test_non_retryable_non_404_stays_error(caplog):
    client = _Client()
    with caplog.at_level("DEBUG", logger=client.logger.name):
        with pytest.raises(APIError) as excinfo:
            client._execute_with_retry(_always_raise(_HTTPError(422, "Validation Failed")), "PUT contents/x")
    # 422 : ni retenté ni attendu — vraie erreur, compteur réel.
    assert "failed after 1 attempt(s)" in str(excinfo.value)
    records = [r for r in caplog.records if "failed after" in r.message]
    assert records and all(r.levelname == "ERROR" for r in records)


def test_success_after_transient_failure_no_final_log(caplog):
    client = _Client()
    calls = {"n": 0}

    def _op():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _HTTPError(503, "blip")
        return "ok"

    with caplog.at_level("DEBUG", logger=client.logger.name):
        assert client._execute_with_retry(_op, "GET ref") == "ok"
    assert not [r for r in caplog.records if "failed after" in r.message]


def test_tool_execution_error_404_logs_at_debug(caplog):
    """#505 : un ToolExecutionError porteur de status_code=404 (cas GitHubClient,
    qui convertit le 404 HTTP en exception métier) part en debug, plus en error —
    le rétrogradage #465 atteint enfin ce chemin (~60 lignes de bruit au run v5)."""
    from collegue.tools.base import ToolExecutionError

    client = _Client()
    with caplog.at_level("DEBUG", logger=client.logger.name):
        with pytest.raises(APIError):
            client._execute_with_retry(
                _always_raise(ToolExecutionError("Ressource introuvable: contents/x", status_code=404)),
                "GET contents/x",
            )
    records = [r for r in caplog.records if "failed after" in r.message]
    assert records and all(r.levelname == "DEBUG" for r in records)


def test_tool_execution_error_401_stays_error(caplog):
    """401 reste une vraie erreur (console) — auth invalide, pas du bruit attendu."""
    from collegue.tools.base import ToolExecutionError

    client = _Client()
    with caplog.at_level("DEBUG", logger=client.logger.name):
        with pytest.raises(APIError):
            client._execute_with_retry(
                _always_raise(ToolExecutionError("Token invalide", status_code=401)),
                "GET user",
            )
    records = [r for r in caplog.records if "failed after" in r.message]
    assert records and all(r.levelname == "ERROR" for r in records)


def test_tool_execution_error_default_status_is_zero():
    """Compat signature : message seul → status_code défaut 0 (sous-classes incluses)."""
    from collegue.tools.base import ToolExecutionError, ToolQuotaError, ToolRateLimitError

    assert ToolExecutionError("x").status_code == 0
    assert ToolRateLimitError("x").status_code == 0
    assert ToolQuotaError("x").status_code == 0
