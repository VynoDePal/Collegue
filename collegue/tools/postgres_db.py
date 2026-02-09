"""
PostgreSQL Database Tool - Inspection et requêtes de bases de données PostgreSQL

Permet à Collègue d'inspecter le schéma, vérifier les données et debugger les requêtes SQL.
"""
import logging
import re
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, Field, field_validator
from .base import BaseTool, ToolExecutionError

try:
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False


class PostgresRequest(BaseModel):
    """Modèle de requête pour les opérations PostgreSQL.

    PARAMÈTRES REQUIS PAR COMMANDE:
    - list_schemas: aucun paramètre requis
    - list_tables: schema_name (défaut: 'public')
    - describe_table: table_name
    - indexes: table_name
    - foreign_keys: table_name
    - table_stats: table_name
    - sample_data: table_name
    - query: query (requête SQL SELECT uniquement)
    """
    command: str = Field(
        ...,
        description="Commande PostgreSQL. describe_table/indexes/sample_data nécessitent table_name. query nécessite le paramètre query. Commandes: list_schemas, list_tables, describe_table, query, table_stats, indexes, foreign_keys, sample_data"
    )
    connection_string: Optional[str] = Field(
        None,
        description="URI PostgreSQL (utilise automatiquement POSTGRES_URL de l'environnement si non fourni)"
    )
    table_name: Optional[str] = Field(
        None,
        description="REQUIS pour describe_table, indexes, foreign_keys, table_stats, sample_data. Nom de la table à inspecter"
    )
    schema_name: Optional[str] = Field("public", description="Schéma PostgreSQL (défaut: 'public')")
    query: Optional[str] = Field(
        None,
        description="REQUIS pour command='query'. Requête SQL SELECT uniquement (INSERT/UPDATE/DELETE interdits)"
    )
    limit: int = Field(100, description="Nombre max de lignes retournées (1-1000)", ge=1, le=1000)

    @field_validator('command')
    @classmethod
    def validate_command(cls, v: str) -> str:
        valid = ['list_tables', 'describe_table', 'query', 'list_schemas',
                 'table_stats', 'indexes', 'foreign_keys', 'sample_data']
        if v not in valid:
            raise ValueError(f"Commande invalide. Valides: {valid}")
        return v

    @field_validator('query')
    @classmethod
    def validate_query_safety(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v

        dangerous = ['INSERT', 'UPDATE', 'DELETE', 'DROP', 'TRUNCATE', 'ALTER', 'CREATE', 'GRANT', 'REVOKE']
        upper_query = v.upper().strip()
        for keyword in dangerous:
            if re.match(rf'^\s*{keyword}\b', upper_query):
                raise ValueError(f"Opération '{keyword}' interdite. Lecture seule.")
        return v


class TableInfo(BaseModel):
    """Information sur une table."""
    name: str
    schema_name: str
    type: str = "table"
    row_count: Optional[int] = None
    size: Optional[str] = None


class ColumnInfo(BaseModel):
    """Information sur une colonne."""
    name: str
    type: str
    nullable: bool
    default: Optional[str] = None
    is_primary_key: bool = False
    is_foreign_key: bool = False
    references: Optional[str] = None


class IndexInfo(BaseModel):
    """Information sur un index."""
    name: str
    table: str
    columns: List[str]
    is_unique: bool
    is_primary: bool
    type: str = "btree"


class ForeignKeyInfo(BaseModel):
    """Information sur une clé étrangère."""
    name: str
    table: str
    column: str
    references_table: str
    references_column: str


class QueryResult(BaseModel):
    """Résultat d'une requête."""
    columns: List[str]
    rows: List[Dict[str, Any]]
    row_count: int
    truncated: bool = False


class PostgresResponse(BaseModel):
    """Modèle de réponse pour les opérations PostgreSQL."""
    success: bool
    command: str
    message: str
    tables: Optional[List[TableInfo]] = None
    columns: Optional[List[ColumnInfo]] = None
    indexes: Optional[List[IndexInfo]] = None
    foreign_keys: Optional[List[ForeignKeyInfo]] = None
    query_result: Optional[QueryResult] = None
    schemas: Optional[List[str]] = None
    stats: Optional[Dict[str, Any]] = None


class PostgresDBTool(BaseTool):
    """
    Outil d'inspection de bases de données PostgreSQL.

    Fonctionnalités:
    - Lister les tables et schémas
    - Décrire la structure des tables (colonnes, types, contraintes)
    - Exécuter des requêtes SELECT (lecture seule)
    - Afficher les index et clés étrangères
    - Statistiques sur les tables
    - Échantillonner les données
    """

    def get_name(self) -> str:
        return "postgres_db"

    def get_description(self) -> str:
        return "Inspecte et interroge les bases de données PostgreSQL (schéma, tables, requêtes lecture seule)"

    def get_request_model(self) -> Type[BaseModel]:
        return PostgresRequest

    def get_response_model(self) -> Type[BaseModel]:
        return PostgresResponse

    def get_supported_languages(self) -> List[str]:
        return ["sql"]

    def _get_connection(self, connection_string: Optional[str] = None):
        """Obtient une connexion PostgreSQL."""
        if not HAS_PSYCOPG2:
            raise ToolExecutionError(
                "psycopg2 non installé. Installez avec: pip install psycopg2-binary"
            )

        import os
        conn_str = connection_string or os.environ.get('POSTGRES_URL') or os.environ.get('DATABASE_URL')

        if not conn_str:
            raise ToolExecutionError(
                "Aucune connexion PostgreSQL configurée. "
                "Fournissez connection_string ou définissez POSTGRES_URL."
            )

        try:
            return psycopg2.connect(conn_str, cursor_factory=RealDictCursor)
        except Exception as e:
            raise ToolExecutionError(f"Erreur de connexion PostgreSQL: {e}")

    def _list_schemas(self, conn) -> List[str]:
        """Liste les schémas de la base."""
        with conn.cursor() as cur:
            cur.execute("""
                SELECT schema_name
                FROM information_schema.schemata
                WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
                ORDER BY schema_name
            """)
            return [row['schema_name'] for row in cur.fetchall()]

    def _list_tables(self, conn, schema: str = "public") -> List[TableInfo]:
        """Liste les tables d'un schéma."""
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    t.table_name,
                    t.table_schema,
                    t.table_type,
                    pg_total_relation_size(quote_ident(t.table_schema) || '.' || quote_ident(t.table_name)) as size_bytes,
                    (SELECT reltuples::bigint FROM pg_class WHERE oid = (quote_ident(t.table_schema) || '.' || quote_ident(t.table_name))::regclass) as row_estimate
                FROM information_schema.tables t
                WHERE t.table_schema = %s
                AND t.table_type IN ('BASE TABLE', 'VIEW')
                ORDER BY t.table_name
            """, (schema,))

            tables = []
            for row in cur.fetchall():
                size_bytes = row.get('size_bytes', 0) or 0
                if size_bytes >= 1024 * 1024 * 1024:
                    size_str = f"{size_bytes / (1024*1024*1024):.1f} GB"
                elif size_bytes >= 1024 * 1024:
                    size_str = f"{size_bytes / (1024*1024):.1f} MB"
                elif size_bytes >= 1024:
                    size_str = f"{size_bytes / 1024:.1f} KB"
                else:
                    size_str = f"{size_bytes} B"

                tables.append(TableInfo(
                    name=row['table_name'],
                    schema_name=row['table_schema'],
                    type="view" if row['table_type'] == 'VIEW' else "table",
                    row_count=int(row['row_estimate']) if row.get('row_estimate') else None,
                    size=size_str
                ))
            return tables

    def _describe_table(self, conn, table_name: str, schema: str = "public") -> List[ColumnInfo]:
        """Décrit la structure d'une table."""
        with conn.cursor() as cur:

            cur.execute("""
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
            """, (schema, table_name, schema, table_name, schema, table_name))

            columns = []
            for row in cur.fetchall():
                columns.append(ColumnInfo(
                    name=row['column_name'],
                    type=row['data_type'],
                    nullable=row['is_nullable'] == 'YES',
                    default=row['column_default'],
                    is_primary_key=row['is_pk'],
                    is_foreign_key=row['is_fk'],
                    references=row['references'] if row['is_fk'] else None
                ))

            if not columns:
                raise ToolExecutionError(f"Table '{schema}.{table_name}' introuvable")

            return columns

    def _get_indexes(self, conn, table_name: str, schema: str = "public") -> List[IndexInfo]:
        """Récupère les index d'une table."""
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    i.relname as index_name,
                    t.relname as table_name,
                    array_agg(a.attname ORDER BY k.n) as columns,
                    ix.indisunique as is_unique,
                    ix.indisprimary as is_primary,
                    am.amname as index_type
                FROM pg_index ix
                JOIN pg_class i ON i.oid = ix.indexrelid
                JOIN pg_class t ON t.oid = ix.indrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                JOIN pg_am am ON am.oid = i.relam
                CROSS JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, n)
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
                WHERE n.nspname = %s AND t.relname = %s
                GROUP BY i.relname, t.relname, ix.indisunique, ix.indisprimary, am.amname
                ORDER BY i.relname
            """, (schema, table_name))

            return [IndexInfo(
                name=row['index_name'],
                table=row['table_name'],
                columns=row['columns'],
                is_unique=row['is_unique'],
                is_primary=row['is_primary'],
                type=row['index_type']
            ) for row in cur.fetchall()]

    def _get_foreign_keys(self, conn, table_name: str, schema: str = "public") -> List[ForeignKeyInfo]:
        """Récupère les clés étrangères d'une table."""
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    tc.constraint_name,
                    tc.table_name,
                    kcu.column_name,
                    ccu.table_name AS foreign_table_name,
                    ccu.column_name AS foreign_column_name
                FROM information_schema.table_constraints AS tc
                JOIN information_schema.key_column_usage AS kcu ON tc.constraint_name = kcu.constraint_name
                JOIN information_schema.constraint_column_usage AS ccu ON ccu.constraint_name = tc.constraint_name
                WHERE tc.constraint_type = 'FOREIGN KEY'
                AND tc.table_schema = %s AND tc.table_name = %s
            """, (schema, table_name))

            return [ForeignKeyInfo(
                name=row['constraint_name'],
                table=row['table_name'],
                column=row['column_name'],
                references_table=row['foreign_table_name'],
                references_column=row['foreign_column_name']
            ) for row in cur.fetchall()]

    def _get_table_stats(self, conn, table_name: str, schema: str = "public") -> Dict[str, Any]:
        """Récupère les statistiques d'une table."""
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    pg_total_relation_size(c.oid) as total_size,
                    pg_table_size(c.oid) as table_size,
                    pg_indexes_size(c.oid) as indexes_size,
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
            """, (schema, table_name))

            row = cur.fetchone()
            if not row:
                raise ToolExecutionError(f"Table '{schema}.{table_name}' introuvable")

            def format_size(bytes_val):
                if bytes_val is None:
                    return "N/A"
                if bytes_val >= 1024 * 1024 * 1024:
                    return f"{bytes_val / (1024*1024*1024):.1f} GB"
                elif bytes_val >= 1024 * 1024:
                    return f"{bytes_val / (1024*1024):.1f} MB"
                elif bytes_val >= 1024:
                    return f"{bytes_val / 1024:.1f} KB"
                return f"{bytes_val} B"

            return {
                "total_size": format_size(row['total_size']),
                "table_size": format_size(row['table_size']),
                "indexes_size": format_size(row['indexes_size']),
                "live_rows": row['live_rows'],
                "dead_rows": row['dead_rows'],
                "last_vacuum": str(row['last_vacuum']) if row['last_vacuum'] else None,
                "last_analyze": str(row['last_analyze']) if row['last_analyze'] else None
            }

    def _execute_query(self, conn, query: str, limit: int = 100) -> QueryResult:
        """Exécute une requête SELECT."""

        if not query.strip().upper().startswith('SELECT'):
            raise ToolExecutionError("Seules les requêtes SELECT sont autorisées")


        if 'LIMIT' not in query.upper():
            query = f"{query.rstrip(';')} LIMIT {limit}"

        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description] if cur.description else []


            serialized_rows = []
            for row in rows:
                serialized = {}
                for k, v in row.items():
                    if hasattr(v, 'isoformat'):
                        serialized[k] = v.isoformat()
                    elif isinstance(v, (bytes, bytearray)):
                        serialized[k] = f"<binary {len(v)} bytes>"
                    else:
                        serialized[k] = v
                serialized_rows.append(serialized)

            return QueryResult(
                columns=columns,
                rows=serialized_rows[:limit],
                row_count=len(serialized_rows),
                truncated=len(rows) >= limit
            )

    def _sample_data(self, conn, table_name: str, schema: str = "public", limit: int = 10) -> QueryResult:
        """Échantillonne les données d'une table."""
        query = f'SELECT * FROM "{schema}"."{table_name}" LIMIT {limit}'
        return self._execute_query(conn, query, limit)

    def _execute_core_logic(self, request: PostgresRequest, **kwargs) -> PostgresResponse:
        """Exécute la logique principale."""
        conn = None
        try:
            conn = self._get_connection(request.connection_string)

            if request.command == 'list_schemas':
                schemas = self._list_schemas(conn)
                return PostgresResponse(
                    success=True,
                    command=request.command,
                    message=f"✅ {len(schemas)} schéma(s) trouvé(s)",
                    schemas=schemas
                )

            elif request.command == 'list_tables':
                tables = self._list_tables(conn, request.schema_name)
                return PostgresResponse(
                    success=True,
                    command=request.command,
                    message=f"✅ {len(tables)} table(s) dans '{request.schema_name}'",
                    tables=tables
                )

            elif request.command == 'describe_table':
                if not request.table_name:
                    raise ToolExecutionError("table_name requis pour describe_table")
                columns = self._describe_table(conn, request.table_name, request.schema_name)
                return PostgresResponse(
                    success=True,
                    command=request.command,
                    message=f"✅ Table '{request.schema_name}.{request.table_name}': {len(columns)} colonne(s)",
                    columns=columns
                )

            elif request.command == 'indexes':
                if not request.table_name:
                    raise ToolExecutionError("table_name requis pour indexes")
                indexes = self._get_indexes(conn, request.table_name, request.schema_name)
                return PostgresResponse(
                    success=True,
                    command=request.command,
                    message=f"✅ {len(indexes)} index sur '{request.table_name}'",
                    indexes=indexes
                )

            elif request.command == 'foreign_keys':
                if not request.table_name:
                    raise ToolExecutionError("table_name requis pour foreign_keys")
                fks = self._get_foreign_keys(conn, request.table_name, request.schema_name)
                return PostgresResponse(
                    success=True,
                    command=request.command,
                    message=f"✅ {len(fks)} clé(s) étrangère(s) sur '{request.table_name}'",
                    foreign_keys=fks
                )

            elif request.command == 'table_stats':
                if not request.table_name:
                    raise ToolExecutionError("table_name requis pour table_stats")
                stats = self._get_table_stats(conn, request.table_name, request.schema_name)
                return PostgresResponse(
                    success=True,
                    command=request.command,
                    message=f"✅ Statistiques de '{request.table_name}'",
                    stats=stats
                )

            elif request.command == 'sample_data':
                if not request.table_name:
                    raise ToolExecutionError("table_name requis pour sample_data")
                result = self._sample_data(conn, request.table_name, request.schema_name, request.limit)
                return PostgresResponse(
                    success=True,
                    command=request.command,
                    message=f"✅ {result.row_count} ligne(s) de '{request.table_name}'",
                    query_result=result
                )

            elif request.command == 'query':
                if not request.query:
                    raise ToolExecutionError("query requis pour command=query")
                result = self._execute_query(conn, request.query, request.limit)
                return PostgresResponse(
                    success=True,
                    command=request.command,
                    message=f"✅ Requête exécutée: {result.row_count} ligne(s)",
                    query_result=result
                )

            else:
                raise ToolExecutionError(f"Commande inconnue: {request.command}")

        finally:
            if conn:
                conn.close()
