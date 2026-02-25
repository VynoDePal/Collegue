"""
Tests unitaires pour le SecurityLogger
"""
import pytest
import json
import logging
from unittest.mock import MagicMock, patch
from collegue.core.security_logger import SecurityLogger, security_logger


class TestSecurityLogger:
    
    @pytest.fixture
    def mock_logger(self):
        with patch('collegue.core.security_logger.logging.getLogger') as mock_get_logger:
            mock_logger = MagicMock()
            mock_logger.handlers = []  # Pas de handlers par défaut
            mock_get_logger.return_value = mock_logger
            yield mock_logger
    
    def test_log_auth_failure(self, mock_logger):
        """Test le logging d'un échec d'authentification."""
        logger = SecurityLogger("test")
        
        logger.log_auth_failure(
            reason="invalid_token",
            client_ip="192.168.1.1",
            user_agent="Mozilla/5.0",
            username="admin"
        )
        
        # Vérifier que log a été appelé
        assert mock_logger.log.called
        args = mock_logger.log.call_args[0]
        
        # Vérifier le niveau de log (WARNING = 30)
        assert args[0] == logging.WARNING
        
        # Vérifier le contenu JSON
        log_data = json.loads(args[1])
        assert log_data["event"] == "AUTH_FAILURE"
        assert log_data["reason"] == "invalid_token"
        assert log_data["client_ip"] == "192.168.1.1"
        assert log_data["user_agent"] == "Mozilla/5.0"
        assert log_data["username"] == "admin"
        assert "timestamp" in log_data
    
    def test_log_auth_success(self, mock_logger):
        """Test le logging d'une authentification réussie."""
        logger = SecurityLogger("test")
        
        logger.log_auth_success(
            user_id="user123",
            client_ip="192.168.1.1",
            auth_method="jwt"
        )
        
        assert mock_logger.log.called
        args = mock_logger.log.call_args[0]
        
        # Vérifier le niveau de log (INFO = 20)
        assert args[0] == logging.INFO
        
        log_data = json.loads(args[1])
        assert log_data["event"] == "AUTH_SUCCESS"
        assert log_data["user_id"] == "user123"
        assert log_data["auth_method"] == "jwt"
    
    def test_log_data_access(self, mock_logger):
        """Test le logging d'un accès aux données."""
        logger = SecurityLogger("test")
        
        logger.log_data_access(
            user_id="user456",
            resource="github_repo",
            action="read",
            resource_id="my-repo",
            client_ip="10.0.0.1"
        )
        
        assert mock_logger.log.called
        args = mock_logger.log.call_args[0]
        
        assert args[0] == logging.INFO
        log_data = json.loads(args[1])
        assert log_data["event"] == "DATA_ACCESS"
        assert log_data["user_id"] == "user456"
        assert log_data["resource"] == "github_repo"
        assert log_data["action"] == "read"
        assert log_data["resource_id"] == "my-repo"
    
    def test_log_config_change(self, mock_logger):
        """Test le logging d'un changement de configuration."""
        logger = SecurityLogger("test")
        
        logger.log_config_change(
            user_id="admin789",
            setting="debug_mode",
            old_value=False,
            new_value=True,
            client_ip="192.168.1.100"
        )
        
        assert mock_logger.log.called
        args = mock_logger.log.call_args[0]
        
        # Vérifier le niveau de log (WARNING = 30)
        assert args[0] == logging.WARNING
        
        log_data = json.loads(args[1])
        assert log_data["event"] == "CONFIG_CHANGE"
        assert log_data["setting"] == "debug_mode"
        assert log_data["old_value"] == "False"
        assert log_data["new_value"] == "True"
    
    def test_log_suspicious_activity(self, mock_logger):
        """Test le logging d'une activité suspecte."""
        logger = SecurityLogger("test")
        
        logger.log_suspicious_activity(
            activity_type="path_traversal_attempt",
            description="Attempted to access ../../etc/passwd",
            severity="error"
        )
        
        assert mock_logger.log.called
        args = mock_logger.log.call_args[0]
        
        # Vérifier le niveau de log (ERROR = 40)
        assert args[0] == logging.ERROR
        
        log_data = json.loads(args[1])
        assert log_data["event"] == "SUSPICIOUS_ACTIVITY"
        assert log_data["activity_type"] == "path_traversal_attempt"
    
    def test_global_instance(self):
        """Test que l'instance globale existe et est fonctionnelle."""
        assert security_logger is not None
        assert isinstance(security_logger, SecurityLogger)
