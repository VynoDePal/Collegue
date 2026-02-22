"""
PostgreSQL Database Tool - Inspection et requêtes de bases de données PostgreSQL

Permet à Collègue d'inspecter le schéma, vérifier les données et debugger les requêtes SQL.
"""
import logging
import re
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, Field, field_validator
from .base import BaseTool, ToolExecutionError
from ..core.shared import validate_postgres_command
from .clients import PostgresClient

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
        return validate_postgres_command(v)

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

    tool_name = "postgres_db"
    tool_description = "Inspecte et interroge les bases de données PostgreSQL (schéma, tables, requêtes lecture seule)"
    tags = {"integration", "database"}
    request_model = PostgresRequest
    response_model = PostgresResponse
    supported_languages = ["sql"]

    def _get_postgres_client(self, request: PostgresRequest) -> PostgresClient:
        """Create and configure PostgresClient from request."""
        return PostgresClient(
            connection_string=request.connection_string,
            schema=request.schema_name
        )

    def _transform_tables(self, tables_data: List[Dict], schema_name: str) -> List[TableInfo]:
        """Transform raw table data into TableInfo objects."""
        result = []
        for row in tables_data:
            size_str = row.get('total_size', 'N/A')
            result.append(TableInfo(
                name=row.get('table_name', ''),
                schema_name=schema_name,
                type="table",
                row_count=row.get('row_count'),
                size=size_str
            ))
        return result

    def _transform_columns(self, columns_data: List[Dict]) -> List[ColumnInfo]:
        """Transform raw column data into ColumnInfo objects."""
        result = []
        for row in columns_data:
            result.append(ColumnInfo(
                name=row.get('column_name', ''),
                type=row.get('data_type', ''),
                nullable=row.get('is_nullable', 'YES') == 'YES',
                default=row.get('column_default'),
                is_primary_key=row.get('is_pk', False),
                is_foreign_key=row.get('is_fk', False),
                references=row.get('references')
            ))
        return result

    def _transform_indexes(self, indexes_data: List[Dict]) -> List[IndexInfo]:
        """Transform raw index data into IndexInfo objects."""
        result = []
        for row in indexes_data:
            # Parse indexdef to extract columns
            index_def = row.get('indexdef', '')
            columns = []
            if '(' in index_def and ')' in index_def:
                cols_str = index_def.split('(')[1].split(')')[0]
                columns = [c.strip() for c in cols_str.split(',')]

            result.append(IndexInfo(
                name=row.get('indexname', ''),
                table=row.get('tablename', ''),
                columns=columns,
                is_unique='UNIQUE' in index_def.upper(),
                is_primary=False,
                type='btree'
            ))
        return result

    def _transform_foreign_keys(self, fks_data: List[Dict]) -> List[ForeignKeyInfo]:
        """Transform raw foreign key data into ForeignKeyInfo objects."""
        result = []
        for row in fks_data:
            result.append(ForeignKeyInfo(
                name=row.get('constraint_name', ''),
                table=row.get('table_name', ''),
                column=row.get('column_name', ''),
                references_table=row.get('foreign_table_name', ''),
                references_column=row.get('foreign_column_name', '')
            ))
        return result

    def _transform_query_result(self, data: List[Dict], limit: int) -> QueryResult:
        """Transform raw query data into QueryResult object."""
        if not data:
            return QueryResult(columns=[], rows=[], row_count=0, truncated=False)

        columns = list(data[0].keys()) if data else []
        rows = []
        for row in data[:limit]:
            serialized = {}
            for k, v in row.items():
                if hasattr(v, 'isoformat'):
                    serialized[k] = v.isoformat()
                elif isinstance(v, (bytes, bytearray)):
                    serialized[k] = f"<binary {len(v)} bytes>"
                else:
                    serialized[k] = v
            rows.append(serialized)

        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=len(data) >= limit
        )

    def _execute_core_logic(self, request: PostgresRequest, **kwargs) -> PostgresResponse:
        """Exécute la logique principale."""
        client = self._get_postgres_client(request)

        if request.command == 'list_schemas':
            response = client.list_schemas()
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to list schemas")
            schemas = [row.get('schema_name') for row in response.data]
            return PostgresResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(schemas)} schéma(s) trouvé(s)",
                schemas=schemas
            )

        elif request.command == 'list_tables':
            response = client.list_tables(request.schema_name)
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to list tables")
            tables_data = response.data or []
            tables = self._transform_tables(tables_data, request.schema_name)
            return PostgresResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(tables)} table(s) dans '{request.schema_name}'",
                tables=tables
            )

        elif request.command == 'describe_table':
            if not request.table_name:
                raise ToolExecutionError("table_name requis pour describe_table")
            response = client.describe_table(request.table_name, request.schema_name)
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to describe table")
            columns_data = response.data or []
            columns = self._transform_columns(columns_data)
            return PostgresResponse(
                success=True,
                command=request.command,
                message=f"✅ Table '{request.schema_name}.{request.table_name}': {len(columns)} colonne(s)",
                columns=columns
            )

        elif request.command == 'indexes':
            if not request.table_name:
                raise ToolExecutionError("table_name requis pour indexes")
            response = client.get_indexes(request.table_name, request.schema_name)
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to get indexes")
            indexes_data = response.data or []
            indexes = self._transform_indexes(indexes_data)
            return PostgresResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(indexes)} index sur '{request.table_name}'",
                indexes=indexes
            )

        elif request.command == 'foreign_keys':
            if not request.table_name:
                raise ToolExecutionError("table_name requis pour foreign_keys")
            response = client.get_foreign_keys(request.table_name, request.schema_name)
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to get foreign keys")
            fks_data = response.data or []
            fks = self._transform_foreign_keys(fks_data)
            return PostgresResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(fks)} clé(s) étrangère(s) sur '{request.table_name}'",
                foreign_keys=fks
            )

        elif request.command == 'table_stats':
            if not request.table_name:
                raise ToolExecutionError("table_name requis pour table_stats")
            response = client.get_table_stats(request.table_name, request.schema_name)
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to get table stats")
            stats_list = response.data or []
            stats_data = stats_list[0] if stats_list else {}
            stats = {
                "total_size": stats_data.get('total_size', 'N/A') if stats_data else 'N/A',
                "table_size": stats_data.get('table_size', 'N/A') if stats_data else 'N/A',
                "indexes_size": stats_data.get('indexes_size', 'N/A') if stats_data else 'N/A',
                "live_rows": stats_data.get('live_rows', 0) if stats_data else 0,
                "dead_rows": stats_data.get('dead_rows', 0) if stats_data else 0,
                "last_vacuum": stats_data.get('last_vacuum') if stats_data else None,
                "last_analyze": stats_data.get('last_analyze') if stats_data else None
            }
            return PostgresResponse(
                success=True,
                command=request.command,
                message=f"✅ Statistiques de '{request.table_name}'",
                stats=stats
            )

        elif request.command == 'sample_data':
            if not request.table_name:
                raise ToolExecutionError("table_name requis pour sample_data")
            response = client.sample_data(request.table_name, request.schema_name, request.limit)
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to sample data")
            result = self._transform_query_result(response.data, request.limit)
            return PostgresResponse(
                success=True,
                command=request.command,
                message=f"✅ {result.row_count} ligne(s) de '{request.table_name}'",
                query_result=result
            )

        elif request.command == 'query':
            if not request.query:
                raise ToolExecutionError("query requis pour command=query")
            response = client.execute_query(request.query, request.limit)
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to execute query")
            result = self._transform_query_result(response.data, request.limit)
            return PostgresResponse(
                success=True,
                command=request.command,
                message=f"✅ Requête exécutée: {result.row_count} ligne(s)",
                query_result=result
            )

        else:
            raise ToolExecutionError(f"Commande inconnue: {request.command}")
