"""
Tests unitaires générés pour PostgresClient avec pytest et mocks

Couverture cible: 0.9
Framework: pytest
Mocks: unittest.mock
"""
import pytest
import os
from unittest.mock import MagicMock, patch
from typing import Any

# Assuming the module is named postgres_client.py
from collegue.tools.clients.postgres import PostgresClient, APIResponse, APIError


@pytest.fixture
def mock_psycopg2():
    with patch("psycopg2.connect") as mock_connect:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        yield mock_connect, mock_conn, mock_cursor


@pytest.fixture
def client():
    return PostgresClient(
        host="localhost",
        port=5432,
        database="testdb",
        username="user",
        password="password"
    )


class TestPostgresClientInit:
    def test_init_with_params(self):
        client = PostgresClient(host="h", port=1234, database="d", username="u", password="p")
        assert client.connection_string == "postgresql://u:p@h:1234/d"

    def test_init_with_connection_string(self):
        conn_str = "postgresql://custom:str@remote:5432/db"
        client = PostgresClient(connection_string=conn_str)
        assert client.connection_string == conn_str

    def test_init_from_env_url(self):
        with patch.dict(os.environ, {"POSTGRES_URL": "postgresql://env:env@host:5432/db"}):
            client = PostgresClient()
            assert client.connection_string == "postgresql://env:env@host:5432/db"

    def test_init_from_env_parts(self):
        env_vars = {
            "POSTGRES_HOST": "env_host",
            "POSTGRES_DB": "env_db",
            "POSTGRES_USER": "env_user",
            "POSTGRES_PASSWORD": "env_password"
        }
        with patch.dict(os.environ, env_vars):
            client = PostgresClient(port=9999)
            assert client.connection_string == "postgresql://env_user:env_password@env_host:9999/env_db"

    def test_driver_detection_psycopg2(self):
        with patch("builtins.__import__", side_effect=lambda name, *args, **kwargs: MagicMock() if name == "psycopg2" else exec("raise ImportError")):
            client = PostgresClient()
            assert client._driver == "psycopg2"

    def test_driver_detection_asyncpg(self):
        def side_effect(name, *args, **kwargs):
            if name == "psycopg2": raise ImportError
            if name == "asyncpg": return MagicMock()
            raise ImportError
        
        with patch("builtins.__import__", side_effect=side_effect):
            client = PostgresClient()
            assert client._driver == "asyncpg"


class TestPostgresClientSafety:
    def test_is_valid_identifier(self, client):
        assert client._is_valid_identifier("users") is True
        assert client._is_valid_identifier("user_profile_123") is True
        assert client._is_valid_identifier("123user") is False
        assert client._is_valid_identifier("user; DROP TABLE") is False
        assert client._is_valid_identifier("schema.table") is False
        assert client._is_valid_identifier("") is False

    def test_execute_query_only_select(self, client):
        response = client.execute_query("DELETE FROM users")
        assert response.success is False
        assert "Only SELECT queries are allowed" in response.error_message

    def test_execute_query_adds_limit(self, mock_psycopg2, client):
        _, _, mock_cursor = mock_psycopg2
        mock_cursor.description = [("col",)]
        mock_cursor.fetchall.return_value = []
        
        client.execute_query("SELECT * FROM users")
        mock_cursor.execute.assert_called_with("SELECT * FROM users LIMIT 1000", None)

    def test_execute_query_respects_existing_limit(self, mock_psycopg2, client):
        _, _, mock_cursor = mock_psycopg2
        mock_cursor.description = [("col",)]
        client.execute_query("SELECT * FROM users LIMIT 10")
        mock_cursor.execute.assert_called_with("SELECT * FROM users LIMIT 10", None)


class TestPostgresClientOperations:
    def test_list_schemas(self, mock_psycopg2, client):
        _, _, mock_cursor = mock_psycopg2
        mock_cursor.description = [("schema_name",)]
        mock_cursor.fetchall.return_value = [("public",), ("auth",)]
        
        res = client.list_schemas()
        assert res.success is True
        assert res.data == [{"schema_name": "public"}, {"schema_name": "auth"}]

    def test_list_tables(self, mock_psycopg2, client):
        _, _, mock_cursor = mock_psycopg2
        mock_cursor.description = [("table_name",)]
        
        client.list_tables(schema_name="private")
        args, _ = mock_cursor.execute.call_args
        assert "WHERE table_schema = %s" in args[0]
        assert args[1] == ("private",)

    def test_describe_table(self, mock_psycopg2, client):
        _, _, mock_cursor = mock_psycopg2
        mock_cursor.description = [("column_name",), ("data_type",)]
        
        client.describe_table("users")
        args, _ = mock_cursor.execute.call_args
        assert "WHERE table_schema = %s AND table_name = %s" in args[0]
        assert args[1] == ("public", "users")

    def test_sample_data_valid(self, mock_psycopg2, client):
        _, _, mock_cursor = mock_psycopg2
        mock_cursor.description = [("id",)]
        
        res = client.sample_data("users", limit=50)
        assert res.success is True
        mock_cursor.execute.assert_called_with('SELECT * FROM "public"."users" LIMIT %s', (50,))

    def test_sample_data_injection_prevention(self, client):
        res = client.sample_data("users; DROP TABLE users")
        assert res.success is False
        assert "Invalid table name" in res.error_message

    def test_get_indexes(self, mock_psycopg2, client):
        _, _, mock_cursor = mock_psycopg2
        mock_cursor.description = [("indexname",)]
        client.get_indexes("users")
        assert mock_cursor.execute.called

    def test_get_foreign_keys(self, mock_psycopg2, client):
        _, _, mock_cursor = mock_psycopg2
        mock_cursor.description = [("column_name",)]
        client.get_foreign_keys("orders")
        assert mock_cursor.execute.called

    def test_get_table_stats(self, mock_psycopg2, client):
        _, _, mock_cursor = mock_psycopg2
        mock_cursor.description = [("table_name",), ("row_count",)]
        client.get_table_stats("users")
        assert mock_cursor.execute.called


class TestPostgresClientErrorHandling:
    def test_no_driver_error(self):
        client = PostgresClient()
        client._driver = None
        res = client.list_schemas()
        assert res.success is False
        assert "No PostgreSQL driver available" in res.error_message

    def test_connection_failure(self, mock_psycopg2, client):
        mock_connect, _, _ = mock_psycopg2
        mock_connect.side_effect = Exception("Connection timeout")
        
        res = client.list_schemas()
        assert res.success is False
        assert "Connection timeout" in res.error_message

    def test_query_execution_error(self, mock_psycopg2, client):
        _, _, mock_cursor = mock_psycopg2
        mock_cursor.execute.side_effect = Exception("Syntax error near SELECT")
        
        res = client.execute_query("SELECT * FROM invalid_table")
        assert res.success is False
        assert "Syntax error" in res.error_message

    def test_get_connection_unsupported_driver(self):
        client = PostgresClient()
        client._driver = 'asyncpg'  # asyncpg is detected but _get_connection only handles psycopg2
        with pytest.raises(APIError, match="No PostgreSQL driver available. Install psycopg2"):
            client._get_connection()

    def test_execute_query_no_results(self, mock_psycopg2, client):
        _, _, mock_cursor = mock_psycopg2
        mock_cursor.description = None # Simulate non-SELECT or no-result metadata
        
        res = client._execute_query("COMMIT")
        assert res.success is True
        assert res.data == []
