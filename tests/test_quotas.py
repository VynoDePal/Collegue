"""
Tests unitaires pour le système de quotas.
"""
import os
import time
import pytest
import tempfile
from unittest.mock import patch
from collegue.tools.quotas import (
    QuotaManager,
    GlobalQuotaManager,
    QuotaConfig,
    QuotaExceeded,
    QuotaType,
    ResourceUsage,
    get_global_quota_manager,
    reset_global_quota_manager,
    check_all_quotas,
)


class TestQuotaConfig:
    """Tests pour la configuration des quotas."""
    
    def test_default_values(self):
        """Test les valeurs par défaut."""
        config = QuotaConfig()
        
        assert config.llm_tokens_per_session == 100000
        assert config.max_file_size_bytes == 1024 * 1024  # 1MB
        assert config.max_files_per_request == 100
        assert config.max_execution_time_seconds == 300.0
        assert config.max_request_size_bytes == 10 * 1024 * 1024  # 10MB
    
    def test_from_env(self):
        """Test le chargement depuis les variables d'environnement."""
        env_vars = {
            'COLLEGUE_QUOTA_LLM_TOKENS': '50000',
            'COLLEGUE_QUOTA_MAX_FILE_SIZE': '524288',  # 512KB
            'COLLEGUE_QUOTA_MAX_FILES': '50',
            'COLLEGUE_QUOTA_MAX_EXEC_TIME': '600.0',
            'COLLEGUE_QUOTA_MAX_REQUEST_SIZE': '5242880',  # 5MB
        }
        
        with patch.dict(os.environ, env_vars):
            config = QuotaConfig.from_env()
        
        assert config.llm_tokens_per_session == 50000
        assert config.max_file_size_bytes == 524288
        assert config.max_files_per_request == 50
        assert config.max_execution_time_seconds == 600.0
        assert config.max_request_size_bytes == 5242880


class TestResourceUsage:
    """Tests pour le suivi de l'utilisation des ressources."""
    
    def test_initial_state(self):
        """Test l'état initial."""
        usage = ResourceUsage()
        
        assert usage.llm_tokens_used == 0
        assert usage.files_processed == 0
        assert usage.total_bytes_processed == 0
        assert usage.execution_start_time is None
        assert usage.execution_time == 0.0
    
    def test_execution_time_calculation(self):
        """Test le calcul du temps d'exécution."""
        usage = ResourceUsage()
        usage.execution_start_time = time.time()
        
        time.sleep(0.1)
        elapsed = usage.execution_time
        
        assert elapsed >= 0.1
    
    def test_to_dict(self):
        """Test la conversion en dictionnaire."""
        usage = ResourceUsage(
            llm_tokens_used=100,
            files_processed=5,
            total_bytes_processed=1024,
            execution_start_time=time.time(),
            request_size_bytes=512
        )
        
        data = usage.to_dict()
        
        assert data['llm_tokens_used'] == 100
        assert data['files_processed'] == 5
        assert data['total_bytes_processed'] == 1024
        assert data['request_size_bytes'] == 512
        assert 'execution_time_seconds' in data


class TestQuotaManager:
    """Tests pour le gestionnaire de quotas."""
    
    def setup_method(self):
        """Setup avant chaque test."""
        self.config = QuotaConfig(
            llm_tokens_per_session=1000,
            max_file_size_bytes=1024,
            max_files_per_request=5,
            max_execution_time_seconds=1.0,
            max_request_size_bytes=1024
        )
        self.manager = QuotaManager(config=self.config, session_id="test_session")
    
    def test_start_execution(self):
        """Test le démarrage de l'exécution."""
        self.manager.start_execution()
        
        assert self.manager._usage.execution_start_time is not None
    
    def test_record_llm_tokens(self):
        """Test l'enregistrement des tokens LLM."""
        self.manager.record_llm_tokens(100)
        
        assert self.manager._usage.llm_tokens_used == 100
    
    def test_record_llm_tokens_exceeds_quota(self):
        """Test le dépassement du quota de tokens."""
        with pytest.raises(QuotaExceeded) as exc_info:
            self.manager.record_llm_tokens(1001)
        
        assert exc_info.value.quota_type == QuotaType.LLM_TOKENS.value
        assert exc_info.value.current == 1001
        assert exc_info.value.limit == 1000
    
    def test_record_llm_tokens_accumulates(self):
        """Test l'accumulation des tokens."""
        self.manager.record_llm_tokens(400)
        self.manager.record_llm_tokens(400)
        
        with pytest.raises(QuotaExceeded):
            self.manager.record_llm_tokens(300)  # 400 + 400 + 300 = 1100 > 1000
        
        assert self.manager._usage.llm_tokens_used == 800
    
    def test_check_file_size_with_content(self):
        """Test la vérification de taille avec contenu."""
        content = b"x" * 512  # 512 bytes
        
        size = self.manager.check_file_size("test.txt", content)
        
        assert size == 512
    
    def test_check_file_size_exceeds_limit(self):
        """Test le dépassement de la limite de taille."""
        content = b"x" * 1025  # 1025 bytes > 1024
        
        with pytest.raises(QuotaExceeded) as exc_info:
            self.manager.check_file_size("large.txt", content)
        
        assert exc_info.value.quota_type == QuotaType.FILE_SIZE.value
        assert exc_info.value.current == 1025
    
    def test_check_file_size_with_real_file(self, tmp_path):
        """Test la vérification avec un fichier réel."""
        test_file = tmp_path / "test.txt"
        test_file.write_bytes(b"x" * 512)
        
        size = self.manager.check_file_size(str(test_file))
        
        assert size == 512
    
    def test_record_file_processed(self):
        """Test l'enregistrement d'un fichier traité."""
        self.manager.record_file_processed("file1.txt", 100)
        self.manager.record_file_processed("file2.txt", 200)
        
        assert self.manager._usage.files_processed == 2
        assert self.manager._usage.total_bytes_processed == 300
    
    def test_record_file_processed_exceeds_limit(self):
        """Test le dépassement du nombre max de fichiers."""
        # Enregistrer 5 fichiers (la limite)
        for i in range(5):
            self.manager.record_file_processed(f"file{i}.txt", 10)
        
        # Le 6ème devrait échouer
        with pytest.raises(QuotaExceeded) as exc_info:
            self.manager.record_file_processed("file6.txt", 10)
        
        assert exc_info.value.quota_type == QuotaType.FILE_COUNT.value
        assert exc_info.value.current == 6
    
    def test_check_execution_time(self):
        """Test la vérification du temps d'exécution."""
        self.manager.start_execution()
        
        time.sleep(0.05)
        elapsed = self.manager.check_execution_time()
        
        assert elapsed >= 0.05
    
    def test_check_execution_time_exceeds_limit(self):
        """Test le dépassement du temps d'exécution."""
        self.manager.start_execution()
        
        # Simuler un temps dépassé en modifiant manuellement
        self.manager._usage.execution_start_time = time.time() - 2.0
        
        with pytest.raises(QuotaExceeded) as exc_info:
            self.manager.check_execution_time()
        
        assert exc_info.value.quota_type == QuotaType.EXECUTION_TIME.value
        assert exc_info.value.current >= 2.0
    
    def test_check_request_size(self):
        """Test la vérification de la taille de requête."""
        self.manager.check_request_size(512)
        
        assert self.manager._usage.request_size_bytes == 512
    
    def test_check_request_size_exceeds_limit(self):
        """Test le dépassement de la taille de requête."""
        with pytest.raises(QuotaExceeded) as exc_info:
            self.manager.check_request_size(1025)
        
        assert exc_info.value.quota_type == QuotaType.REQUEST_SIZE.value
    
    def test_get_usage_stats(self):
        """Test les statistiques d'utilisation."""
        self.manager.start_execution()
        self.manager.record_llm_tokens(500)
        self.manager.record_file_processed("test.txt", 100)
        
        stats = self.manager.get_usage_stats()
        
        assert stats['llm_tokens_used'] == 500
        assert stats['files_processed'] == 1
        assert stats['quotas']['llm_tokens'] == 1000
        assert stats['utilization']['llm_tokens_pct'] == 50.0
    
    def test_reset(self):
        """Test la réinitialisation."""
        self.manager.record_llm_tokens(100)
        self.manager.record_file_processed("test.txt", 50)
        
        self.manager.reset()
        
        assert self.manager._usage.llm_tokens_used == 0
        assert self.manager._usage.files_processed == 0
    
    def test_format_bytes(self):
        """Test le formatage des bytes."""
        assert QuotaManager._format_bytes(512) == "512.0 B"
        assert QuotaManager._format_bytes(1024) == "1.0 KB"
        assert QuotaManager._format_bytes(1024 * 1024) == "1.0 MB"
        assert QuotaManager._format_bytes(1024 * 1024 * 1024) == "1.0 GB"


class TestGlobalQuotaManager:
    """Tests pour le gestionnaire global de quotas."""
    
    def setup_method(self):
        """Reset avant chaque test."""
        reset_global_quota_manager()
    
    def teardown_method(self):
        """Reset après chaque test."""
        reset_global_quota_manager()
    
    def test_get_session_manager_creates_new(self):
        """Test la création d'un nouveau gestionnaire de session."""
        global_manager = get_global_quota_manager()
        
        manager = global_manager.get_session_manager("session1")
        
        assert manager is not None
        assert manager.session_id == "session1"
    
    def test_get_session_manager_returns_existing(self):
        """Test le retour d'un gestionnaire existant."""
        global_manager = get_global_quota_manager()
        
        manager1 = global_manager.get_session_manager("session1")
        manager2 = global_manager.get_session_manager("session1")
        
        assert manager1 is manager2
    
    def test_cleanup_session(self):
        """Test le nettoyage d'une session."""
        global_manager = get_global_quota_manager()
        
        global_manager.get_session_manager("session1")
        global_manager.cleanup_session("session1")
        
        # Recréer un manager pour vérifier que c'est un nouveau
        manager2 = global_manager.get_session_manager("session1")
        assert manager2._usage.llm_tokens_used == 0
    
    def test_get_all_stats(self):
        """Test les statistiques de toutes les sessions."""
        global_manager = get_global_quota_manager()
        
        manager1 = global_manager.get_session_manager("session1")
        manager2 = global_manager.get_session_manager("session2")
        
        manager1.record_llm_tokens(100)
        manager2.record_llm_tokens(200)
        
        all_stats = global_manager.get_all_stats()
        
        assert "session1" in all_stats
        assert "session2" in all_stats
        assert all_stats["session1"]["llm_tokens_used"] == 100
        assert all_stats["session2"]["llm_tokens_used"] == 200
    
    def test_reset_session(self):
        """Test la réinitialisation d'une session."""
        global_manager = get_global_quota_manager()
        
        manager = global_manager.get_session_manager("session1")
        manager.record_llm_tokens(100)
        
        global_manager.reset_session("session1")
        
        assert manager._usage.llm_tokens_used == 0
    
    def test_reset_all(self):
        """Test la réinitialisation de toutes les sessions."""
        global_manager = get_global_quota_manager()
        
        manager1 = global_manager.get_session_manager("session1")
        manager2 = global_manager.get_session_manager("session2")
        
        manager1.record_llm_tokens(100)
        manager2.record_llm_tokens(200)
        
        global_manager.reset_all()
        
        assert manager1._usage.llm_tokens_used == 0
        assert manager2._usage.llm_tokens_used == 0


class TestCheckAllQuotas:
    """Tests pour la fonction utilitaire check_all_quotas."""
    
    def setup_method(self):
        """Setup avant chaque test."""
        reset_global_quota_manager()
    
    def teardown_method(self):
        """Cleanup après chaque test."""
        reset_global_quota_manager()
    
    def test_check_all_quotas_returns_manager(self, tmp_path):
        """Test que check_all_quotas retourne un manager."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")
        
        manager = check_all_quotas(
            session_id="test_session",
            file_paths=[str(test_file)],
            request_size=100
        )
        
        assert isinstance(manager, QuotaManager)
        assert manager.session_id == "test_session"
    
    def test_check_all_quotas_validates_request_size(self):
        """Test la validation de la taille de requête."""
        with pytest.raises(QuotaExceeded) as exc_info:
            check_all_quotas(
                session_id="test_session",
                request_size=100 * 1024 * 1024  # 100MB, bien au-dessus de la limite par défaut
            )
        
        assert exc_info.value.quota_type == QuotaType.REQUEST_SIZE.value
    
    def test_check_all_quotas_validates_files(self, tmp_path):
        """Test la validation des fichiers."""
        # Créer un fichier trop grand
        large_file = tmp_path / "large.txt"
        large_file.write_bytes(b"x" * (2 * 1024 * 1024))  # 2MB > 1MB limite
        
        with pytest.raises(QuotaExceeded) as exc_info:
            check_all_quotas(
                session_id="test_session",
                file_paths=[str(large_file)]
            )
        
        assert exc_info.value.quota_type == QuotaType.FILE_SIZE.value


class TestQuotaExceeded:
    """Tests pour l'exception QuotaExceeded."""
    
    def test_exception_message(self):
        """Test le message d'erreur."""
        exc = QuotaExceeded(
            quota_type=QuotaType.LLM_TOKENS.value,
            current=150000,
            limit=100000,
            details="Session overflow"
        )
        
        message = str(exc)
        
        assert QuotaType.LLM_TOKENS.value in message
        assert "150000" in message
        assert "100000" in message
        assert "Session overflow" in message
    
    def test_exception_without_details(self):
        """Test le message sans détails."""
        exc = QuotaExceeded(
            quota_type=QuotaType.FILE_SIZE.value,
            current=2048,
            limit=1024
        )
        
        message = str(exc)
        
        assert QuotaType.FILE_SIZE.value in message
        assert "2048" in message
        assert "1024" in message
    
    def test_exception_attributes(self):
        """Test les attributs de l'exception."""
        exc = QuotaExceeded(
            quota_type="test_quota",
            current=50.5,
            limit=100.0
        )
        
        assert exc.quota_type == "test_quota"
        assert exc.current == 50.5
        assert exc.limit == 100.0
