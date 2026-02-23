import pytest
from unittest.mock import patch, MagicMock
from collegue.tools.postgres_db import PostgresDBTool, PostgresRequest
from collegue.tools.clients.base import APIResponse

@pytest.fixture
def mock_postgres_client():
    with patch('collegue.tools.postgres_db.PostgresDBTool._get_postgres_client') as mock:
        client = MagicMock()
        mock.return_value = client
        yield client

def test_table_stats_success(mock_postgres_client):
    tool = PostgresDBTool()
    
    mock_postgres_client.get_table_stats.return_value = APIResponse(
        success=True,
        data=[{
            'total_size': '10 MB',
            'table_size': '8 MB',
            'indexes_size': '2 MB',
            'live_rows': 100,
            'dead_rows': 5,
            'last_vacuum': '2023-01-01',
            'last_analyze': '2023-01-01'
        }]
    )
    
    request = PostgresRequest(command='table_stats', table_name='users', schema_name='public')
    response = tool.execute(request)
    
    assert response.success
    assert response.stats is not None
    assert response.stats['total_size'] == '10 MB'
    assert response.stats['live_rows'] == 100

def test_describe_table_success(mock_postgres_client):
    tool = PostgresDBTool()
    
    mock_postgres_client.describe_table.return_value = APIResponse(
        success=True,
        data=[{
            'column_name': 'id',
            'data_type': 'integer',
            'is_nullable': 'NO',
            'column_default': 'nextval(seq)',
            'is_pk': True,
            'is_fk': False,
            'references': None
        }]
    )
    
    request = PostgresRequest(command='describe_table', table_name='users', schema_name='public')
    response = tool.execute(request)
    
    assert response.success
    assert len(response.columns) == 1
    assert response.columns[0].is_primary_key is True

def test_list_tables_success(mock_postgres_client):
    tool = PostgresDBTool()
    
    mock_postgres_client.list_tables.return_value = APIResponse(
        success=True,
        data=[{
            'table_name': 'users',
            'row_count': 100,
            'total_size': '10 MB'
        }]
    )
    
    request = PostgresRequest(command='list_tables', schema_name='public')
    response = tool.execute(request)
    
    assert response.success
    assert len(response.tables) == 1
    assert response.tables[0].name == 'users'
    assert response.tables[0].size == '10 MB'
