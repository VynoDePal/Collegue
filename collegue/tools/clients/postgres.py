"""
PostgreSQL client for database operations.

Provides read-only access to PostgreSQL databases with safe query execution.
"""
import os
from typing import Any, Dict, List, Optional

from .base import APIResponse, APIError


class PostgresClient:
    """
    Client for PostgreSQL database operations.
    
    Provides safe, read-only access for introspection and queries.
    Uses psycopg2 or asyncpg if available.
    """

    def __init__(
        self,
        connection_string: Optional[str] = None,
        host: Optional[str] = None,
        port: int = 5432,
        database: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        schema: str = "public"
    ):
        # Build connection string from parts if not provided
        if connection_string:
            self.connection_string = connection_string
        else:
            # Get from env or build
            conn_str = os.environ.get('POSTGRES_URL')
            if conn_str:
                self.connection_string = conn_str
            else:
                self.connection_string = self._build_connection_string(
                    host, port, database, username, password
                )

        self.schema = schema
        self._connection = None
        self._driver = None

        # Detect available driver
        try:
            import psycopg2
            self._driver = 'psycopg2'
        except ImportError:
            try:
                import asyncpg
                self._driver = 'asyncpg'
            except ImportError:
                self._driver = None

    def _build_connection_string(
        self,
        host: Optional[str],
        port: int,
        database: Optional[str],
        username: Optional[str],
        password: Optional[str]
    ) -> str:
        """Build connection string from components."""
        host = host or os.environ.get('POSTGRES_HOST', 'localhost')
        database = database or os.environ.get('POSTGRES_DB', 'postgres')
        username = username or os.environ.get('POSTGRES_USER', 'postgres')
        password = password or os.environ.get('POSTGRES_PASSWORD', '')

        if password:
            return f"postgresql://{username}:{password}@{host}:{port}/{database}"
        return f"postgresql://{username}@{host}:{port}/{database}"

    def _get_connection(self):
        """Get or create database connection."""
        if self._driver == 'psycopg2':
            import psycopg2
            return psycopg2.connect(self.connection_string)
        else:
            raise APIError("No PostgreSQL driver available. Install psycopg2 or asyncpg.")

    def list_schemas(self) -> APIResponse:
        """List all schemas in the database."""
        query = """
            SELECT schema_name 
            FROM information_schema.schemata 
            WHERE schema_name NOT LIKE 'pg_%' 
            AND schema_name != 'information_schema'
            ORDER BY schema_name
        """
        return self._execute_query(query)

    def list_tables(self, schema_name: Optional[str] = None) -> APIResponse:
        """List all tables in a schema."""
        schema = schema_name or self.schema

        query = """
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = %s 
            AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """
        return self._execute_query(query, (schema,))

    def describe_table(
        self,
        table_name: str,
        schema_name: Optional[str] = None
    ) -> APIResponse:
        """Get column information for a table."""
        schema = schema_name or self.schema

        query = """
            SELECT 
                column_name,
                data_type,
                is_nullable,
                column_default,
                character_maximum_length
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
        """
        return self._execute_query(query, (schema, table_name))

    def get_indexes(self, table_name: str, schema_name: Optional[str] = None) -> APIResponse:
        """Get indexes for a table."""
        schema = schema_name or self.schema

        query = """
            SELECT 
                indexname,
                indexdef
            FROM pg_indexes
            WHERE schemaname = %s AND tablename = %s
        """
        return self._execute_query(query, (schema, table_name))

    def get_foreign_keys(
        self,
        table_name: str,
        schema_name: Optional[str] = None
    ) -> APIResponse:
        """Get foreign key constraints for a table."""
        schema = schema_name or self.schema

        query = """
            SELECT
                kcu.column_name,
                ccu.table_name AS foreign_table_name,
                ccu.column_name AS foreign_column_name
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
                ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.constraint_column_usage AS ccu
                ON ccu.constraint_name = tc.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY'
            AND tc.table_schema = %s
            AND tc.table_name = %s
        """
        return self._execute_query(query, (schema, table_name))

    def get_table_stats(self, table_name: str, schema_name: Optional[str] = None) -> APIResponse:
        """Get row count and size stats for a table."""
        schema = schema_name or self.schema

        query = """
            SELECT 
                relname AS table_name,
                n_live_tup AS row_count,
                pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size
            FROM pg_stat_user_tables st
            JOIN pg_class c ON c.relname = st.relname
            WHERE st.schemaname = %s AND st.relname = %s
        """
        return self._execute_query(query, (schema, table_name))

    def sample_data(
        self,
        table_name: str,
        schema_name: Optional[str] = None,
        limit: int = 100
    ) -> APIResponse:
        """Get sample rows from a table (safe query)."""
        schema = schema_name or self.schema

        # Validate table name to prevent injection
        if not self._is_valid_identifier(table_name):
            return APIResponse(
                success=False,
                error_message=f"Invalid table name: {table_name}"
            )

        if not self._is_valid_identifier(schema):
            return APIResponse(
                success=False,
                error_message=f"Invalid schema name: {schema}"
            )

        # Use parameterized LIMIT only
        query = f'SELECT * FROM "{schema}"."{table_name}" LIMIT %s'
        return self._execute_query(query, (limit,))

    def execute_query(self, query: str, limit: int = 1000) -> APIResponse:
        """
        Execute a read-only SELECT query.
        
        Only SELECT queries are allowed for safety.
        """
        # Safety check - only allow SELECT
        normalized = query.strip().upper()
        if not normalized.startswith('SELECT'):
            return APIResponse(
                success=False,
                error_message="Only SELECT queries are allowed for safety"
            )

        # Apply limit if not present
        if 'LIMIT' not in normalized:
            query = query.rstrip(';') + f' LIMIT {limit}'

        return self._execute_query(query)

    def _execute_query(
        self,
        query: str,
        params: Optional[tuple] = None
    ) -> APIResponse:
        """Execute a query and return results."""
        if self._driver is None:
            return APIResponse(
                success=False,
                error_message="No PostgreSQL driver available. Install psycopg2."
            )

        try:
            conn = self._get_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(query, params)

                    if cur.description:
                        columns = [desc[0] for desc in cur.description]
                        rows = cur.fetchall()
                        data = [dict(zip(columns, row)) for row in rows]
                    else:
                        data = []

                    return APIResponse(
                        success=True,
                        data=data,
                        status_code=200
                    )
            finally:
                conn.close()

        except Exception as e:
            return APIResponse(
                success=False,
                error_message=str(e)
            )

    def _is_valid_identifier(self, name: str) -> bool:
        """Check if a string is a valid SQL identifier."""
        import re
        # Only allow alphanumeric and underscore
        return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name))
