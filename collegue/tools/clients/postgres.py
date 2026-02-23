"""
PostgreSQL client for database operations.

Provides read-only access to PostgreSQL databases with safe query execution.
"""
import os
from typing import Optional

from .base import APIResponse, APIError
from ...core.auth import resolve_postgres_url


class PostgresClient:

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
        if connection_string:
            self.connection_string = connection_string
        else:
            conn_str = resolve_postgres_url(
                None,
                'x-postgres-url',
                'x-collegue-postgres-url',
            )
            if conn_str:
                self.connection_string = conn_str
            else:
                self.connection_string = self._build_connection_string(
                    host, port, database, username, password
                )

        self.schema = schema
        self._connection = None
        self._driver = None

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
        host = host or os.environ.get('POSTGRES_HOST', 'localhost')
        database = database or os.environ.get('POSTGRES_DB', 'postgres')
        username = username or os.environ.get('POSTGRES_USER', 'postgres')
        password = password or os.environ.get('POSTGRES_PASSWORD', '')

        if password:
            return f"postgresql://{username}:{password}@{host}:{port}/{database}"
        return f"postgresql://{username}@{host}:{port}/{database}"

    def _get_connection(self):
        if self._driver == 'psycopg2':
            import psycopg2
            return psycopg2.connect(self.connection_string)
        else:
            raise APIError("No PostgreSQL driver available. Install psycopg2 or asyncpg.")

    def list_schemas(self) -> APIResponse:
        query = """
            SELECT schema_name 
            FROM information_schema.schemata 
            WHERE schema_name NOT LIKE 'pg_%' 
            AND schema_name != 'information_schema'
            ORDER BY schema_name
        """
        return self._execute_query(query)

    def list_tables(self, schema_name: Optional[str] = None) -> APIResponse:
        schema = schema_name or self.schema

        query = """
            SELECT
                t.table_name,
                (SELECT reltuples::bigint FROM pg_class WHERE oid = (quote_ident(t.table_schema) || '.' || quote_ident(t.table_name))::regclass) as row_count,
                pg_size_pretty(pg_total_relation_size(quote_ident(t.table_schema) || '.' || quote_ident(t.table_name))) as total_size
            FROM information_schema.tables t
            WHERE t.table_schema = %s 
            AND t.table_type IN ('BASE TABLE', 'VIEW')
            ORDER BY t.table_name
        """
        return self._execute_query(query, (schema,))

    def describe_table(
        self,
        table_name: str,
        schema_name: Optional[str] = None
    ) -> APIResponse:
        schema = schema_name or self.schema

        query = """
            SELECT
                c.column_name,
                c.data_type,
                c.is_nullable,
                c.column_default,
                CASE WHEN pk.column_name IS NOT NULL THEN true ELSE false END as is_pk,
                CASE WHEN fk.column_name IS NOT NULL THEN true ELSE false END as is_fk,
                fk.foreign_table_schema || '.' || fk.foreign_table_name || '(' || fk.foreign_column_name || ')' as references
            FROM information_schema.columns c
            LEFT JOIN (
                SELECT ku.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage ku ON tc.constraint_name = ku.constraint_name
                WHERE tc.table_schema = %s AND tc.table_name = %s AND tc.constraint_type = 'PRIMARY KEY'
            ) pk ON c.column_name = pk.column_name
            LEFT JOIN (
                SELECT
                    kcu.column_name,
                    ccu.table_schema as foreign_table_schema,
                    ccu.table_name as foreign_table_name,
                    ccu.column_name as foreign_column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
                JOIN information_schema.constraint_column_usage ccu ON tc.constraint_name = ccu.constraint_name
                WHERE tc.table_schema = %s AND tc.table_name = %s AND tc.constraint_type = 'FOREIGN KEY'
            ) fk ON c.column_name = fk.column_name
            WHERE c.table_schema = %s AND c.table_name = %s
            ORDER BY c.ordinal_position
        """
        return self._execute_query(query, (schema, table_name, schema, table_name, schema, table_name))

    def get_indexes(self, table_name: str, schema_name: Optional[str] = None) -> APIResponse:
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
        schema = schema_name or self.schema

        query = """
            SELECT
                pg_size_pretty(pg_total_relation_size(c.oid)) as total_size,
                pg_size_pretty(pg_table_size(c.oid)) as table_size,
                pg_size_pretty(pg_indexes_size(c.oid)) as indexes_size,
                s.n_live_tup as live_rows,
                s.n_dead_tup as dead_rows,
                s.last_vacuum,
                s.last_autovacuum,
                s.last_analyze,
                s.last_autoanalyze
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
            WHERE n.nspname = %s AND c.relname = %s
        """
        return self._execute_query(query, (schema, table_name))

    def sample_data(
        self,
        table_name: str,
        schema_name: Optional[str] = None,
        limit: int = 100
    ) -> APIResponse:
        schema = schema_name or self.schema

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

        query = f'SELECT * FROM "{schema}"."{table_name}" LIMIT %s'
        return self._execute_query(query, (limit,))

    def execute_query(self, query: str, limit: int = 1000) -> APIResponse:
        normalized = query.strip().upper()
        if not normalized.startswith('SELECT'):
            return APIResponse(
                success=False,
                error_message="Only SELECT queries are allowed for safety"
            )

        if 'LIMIT' not in normalized:
            query = query.rstrip(';') + f' LIMIT {limit}'

        return self._execute_query(query)

    def _execute_query(
        self,
        query: str,
        params: Optional[tuple] = None
    ) -> APIResponse:
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
        import re
        return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name))
