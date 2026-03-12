"""
Tests pour les corrections de fuites mémoire (issue #141).
"""
import gc
import pytest
from unittest.mock import Mock, patch
from pydantic import BaseModel

from collegue.core.memory_manager import (
    MemoryManager,
    LimitedSizeHistory,
    TTLCache,
    get_memory_manager,
    cleanup_all,
)
from collegue.tools.base import BaseTool


# Modèles de test
class TestRequest(BaseModel):
    code: str


class TestResponse(BaseModel):
    result: str


class TestTool(BaseTool):
    """Tool de test simple."""
    tool_name = "test_memory_tool"
    tool_description = "A test tool"
    request_model = TestRequest
    response_model = TestResponse
    
    def _execute_core_logic(self, request: TestRequest, **kwargs) -> TestResponse:
        return TestResponse(result=f"Processed: {request.code}")


class TestMemoryManager:
    """Tests pour MemoryManager."""
    
    def test_track_object(self):
        """Test le suivi d'objets."""
        manager = MemoryManager()
        
        # Utiliser un objet dummy weakref-able sans effets de bord
        class DummyObject:
            pass
        
        obj = DummyObject()
        
        try:
            manager.track_object("test_obj", obj)
            
            assert manager._stats.tracked_objects == 1
        finally:
            # Nettoyer explicitement pour ne pas laisser d'état global
            del obj
            gc.collect()
    
    def test_cleanup_removes_dead_refs(self):
        """Test que cleanup supprime les références mortes."""
        manager = MemoryManager()
        
        # Utiliser un objet dummy weakref-able sans effets de bord
        # pour éviter de polluer le MemoryManager global
        class DummyObject:
            pass
        
        # Créer un objet et le suivre
        obj = DummyObject()
        manager.track_object("temp_obj", obj)
        assert manager._stats.tracked_objects == 1
        
        # Supprimer la référence
        del obj
        
        # Forcer GC pour déclencher le callback de weakref
        gc.collect()
        
        # Le callback on_delete devrait avoir nettoyé l'entrée automatiquement
        assert manager._stats.tracked_objects == 0
    
    def test_register_cleanup_callback(self):
        """Test l'enregistrement de callbacks de nettoyage."""
        manager = MemoryManager()
        callback_called = [False]
        
        def my_callback():
            callback_called[0] = True
        
        manager.register_cleanup_callback(my_callback)
        manager.cleanup()
        
        assert callback_called[0] is True
    
    def test_cleanup_callback_exception_handled(self):
        """Test que les exceptions dans les callbacks sont gérées."""
        manager = MemoryManager()
        
        def failing_callback():
            raise ValueError("Test error")
        
        manager.register_cleanup_callback(failing_callback)
        
        # Ne devrait pas lever d'exception
        manager.cleanup()


class TestLimitedSizeHistory:
    """Tests pour LimitedSizeHistory."""
    
    def test_history_limits_size(self):
        """Test que l'historique limite sa taille."""
        history = LimitedSizeHistory(max_size=5, name="test")
        
        # Ajouter 10 éléments
        for i in range(10):
            history.append(f"item_{i}")
        
        # Ne devrait garder que les 5 derniers
        assert len(history) == 5
        items = history.get_all()
        assert items[0] == "item_5"  # Premier élément = item_5
        assert items[-1] == "item_9"  # Dernier élément = item_9
    
    def test_history_clear(self):
        """Test le vidage de l'historique."""
        history = LimitedSizeHistory(max_size=10, name="test")
        
        for i in range(5):
            history.append(f"item_{i}")
        
        history.clear()
        
        assert len(history) == 0


class TestTTLCache:
    """Tests pour TTLCache."""
    
    def test_cache_stores_and_retrieves(self):
        """Test le stockage et la récupération."""
        cache = TTLCache(max_size=10, ttl_seconds=3600, name="test")
        
        cache.set("key1", "value1")
        
        assert cache.get("key1") == "value1"
    
    def test_cache_returns_default_for_missing(self):
        """Test que get retourne default pour une clé manquante."""
        cache = TTLCache(max_size=10, ttl_seconds=3600, name="test")
        
        result = cache.get("missing_key", default="default_value")
        
        assert result == "default_value"
    
    def test_cache_expires_entries(self):
        """Test que les entrées expirées sont supprimées (avec patch du temps)."""
        from unittest.mock import patch
        
        cache = TTLCache(max_size=10, ttl_seconds=0.1, name="test")  # 100ms TTL
        
        # Simuler l'expiration en patchant le temps monotone
        start_time = 1000.0
        with patch(
            "collegue.core.memory_manager.time.monotonic",
            side_effect=[start_time, start_time + 0.2],  # set() puis get()
        ):
            cache.set("key1", "value1")
            # L'entrée devrait être expirée lorsque nous la lisons
            result = cache.get("key1", default="expired")
            
        assert result == "expired"
    
    def test_cache_respects_max_size(self):
        """Test que le cache respecte la taille maximale."""
        cache = TTLCache(max_size=3, ttl_seconds=3600, name="test")
        
        # Ajouter 5 éléments
        for i in range(5):
            cache.set(f"key_{i}", f"value_{i}")
        
        # Le cache ne devrait garder que les 3 plus récents
        assert len(cache) == 3
        assert cache.get("key_2") is not None
        assert cache.get("key_3") is not None
        assert cache.get("key_4") is not None


class TestBaseToolMemory:
    """Tests pour la gestion mémoire dans BaseTool."""
    
    def test_base_tool_tracked_by_memory_manager(self):
        """Test que BaseTool est suivi par le memory manager."""
        manager = get_memory_manager()
        initial_count = manager._stats.tracked_objects
        
        tool = TestTool()
        
        try:
            # Le tool devrait être suivi
            assert manager._stats.tracked_objects == initial_count + 1
        finally:
            # Nettoyer explicitement pour ne pas laisser d'état global
            tool.cleanup()
            del tool
            gc.collect()
    
    def test_base_tool_cleanup_releases_references(self):
        """Test que cleanup libère les références."""
        tool = TestTool()
        tool.config = {"key": "value"}
        tool.app_state = {"state": "data"}
        tool.prompt_engine = Mock()
        tool.context_manager = Mock()
        tool._quota_manager = Mock()
        
        tool.cleanup()
        
        assert tool.config is None
        assert tool.app_state is None
        assert tool.prompt_engine is None
        assert tool.context_manager is None
        assert tool._quota_manager is None
    
    def test_base_tool_cleanup_calls_gc(self):
        """Test que cleanup appelle gc.collect() quand force_gc=True."""
        tool = TestTool()
        
        with patch('gc.collect') as mock_gc:
            tool.cleanup(force_gc=True)
            mock_gc.assert_called_once()


class TestCleanupAll:
    """Tests pour cleanup_all."""
    
    def test_cleanup_all_calls_manager_cleanup(self):
        """Test que cleanup_all appelle manager.cleanup."""
        with patch.object(MemoryManager, 'cleanup') as mock_cleanup:
            cleanup_all()
            mock_cleanup.assert_called_once_with(force=True)
    
    def test_cleanup_all_calls_gc_collect(self):
        """Test que cleanup_all appelle gc.collect."""
        with patch('gc.collect') as mock_gc:
            cleanup_all()
            # Devrait être appelé au moins une fois
            assert mock_gc.call_count >= 1
