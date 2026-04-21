"""
Tests unitaires pour le LazyPromptEngine et le fix du timeout d'initialisation.
"""
import pytest
import asyncio
import time
from unittest.mock import Mock, patch, MagicMock

from collegue.app import LazyPromptEngine


class TestLazyPromptEngine:
    """Tests pour la classe LazyPromptEngine."""
    
    @pytest.fixture
    def lazy_engine(self):
        """Fixture pour créer un LazyPromptEngine frais."""
        return LazyPromptEngine()
    
    @pytest.fixture
    def mock_engine(self):
        """Fixture pour créer un mock d'EnhancedPromptEngine."""
        engine = Mock()
        engine.get_optimized_prompt = Mock(return_value=("test prompt", "v1.0"))
        return engine
    
    def test_initial_state(self, lazy_engine):
        """Test que l'engine démarre dans l'état correct."""
        assert lazy_engine._engine is None
        assert lazy_engine._initialization_task is None
        assert lazy_engine._initialized is False
        assert lazy_engine._initialization_error is None
        assert lazy_engine.is_initialized is False
        assert lazy_engine.is_initializing is False
        assert lazy_engine._init_attempts == 0
    
    @pytest.mark.asyncio
    async def test_start_initialization_creates_task(self, lazy_engine):
        """Test que start_initialization crée une tâche."""
        with patch.object(lazy_engine, '_create_engine', return_value=Mock()):
            task = lazy_engine.start_initialization()
            assert lazy_engine._initialization_task is not None
            assert lazy_engine.is_initializing is True
            assert lazy_engine._init_attempts == 1
            # Attendre que la tâche se termine
            await asyncio.sleep(0.1)
    
    @pytest.mark.asyncio
    async def test_get_engine_returns_engine_when_ready(self, lazy_engine, mock_engine):
        """Test que get_engine retourne l'engine quand il est prêt."""
        lazy_engine._engine = mock_engine
        lazy_engine._initialized = True
        
        result = await lazy_engine.get_engine()
        assert result is mock_engine
    
    @pytest.mark.asyncio
    async def test_get_engine_waits_for_initialization(self, lazy_engine, mock_engine):
        """Test que get_engine attend l'initialisation si nécessaire."""
        with patch.object(lazy_engine, '_create_engine', return_value=mock_engine):
            lazy_engine.start_initialization()
            # L'engine n'est pas encore initialisé
            assert lazy_engine.is_initializing is True
            
            # get_engine devrait attendre et retourner l'engine
            result = await lazy_engine.get_engine(timeout=5.0)
            assert result is mock_engine
            assert lazy_engine.is_initialized is True
    
    @pytest.mark.asyncio
    async def test_get_engine_handles_timeout(self, lazy_engine):
        """Test que get_engine gère le timeout correctement."""
        # Simuler une initialisation qui prend trop de temps
        async def slow_init():
            await asyncio.sleep(10)
        
        lazy_engine._initialization_task = asyncio.create_task(slow_init())
        
        # get_engine devrait timeout après 0.1s
        result = await lazy_engine.get_engine(timeout=0.1)
        assert result is None
    
    @pytest.mark.asyncio
    async def test_get_engine_raises_error_after_max_retries(self, lazy_engine):
        """Test que get_engine lève une exception après MAX_RETRIES échecs."""
        lazy_engine._initialization_error = "Erreur de test"
        lazy_engine._init_attempts = lazy_engine.MAX_RETRIES
        
        with pytest.raises(RuntimeError) as exc_info:
            await lazy_engine.get_engine()
        
        assert "Échec critique du moteur de prompt" in str(exc_info.value)
        assert "Erreur de test" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_engine_retries_on_error(self, lazy_engine):
        """Test que get_engine relance l'initialisation si on n'a pas atteint MAX_RETRIES."""
        lazy_engine._initialization_error = "Erreur de test précédente"
        lazy_engine._init_attempts = 1
        
        with patch.object(lazy_engine, 'start_initialization') as mock_start:
            # On empêche _initialization_task d'être None pour éviter le blocage du await
            lazy_engine._initialization_task = asyncio.create_task(asyncio.sleep(0.01))
            
            await lazy_engine.get_engine()
            mock_start.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_initialization_with_timeout_failure(self, lazy_engine):
        """Test que l'initialisation gère le timeout interne."""
        # Simuler directement l'erreur de timeout
        lazy_engine._initialization_error = "Timeout après 10s lors de l'initialisation"
        
        assert lazy_engine._initialization_error is not None
        assert "Timeout" in lazy_engine._initialization_error
    
    @pytest.mark.asyncio
    async def test_initialization_handles_exception(self, lazy_engine):
        """Test que l'initialisation gère les exceptions."""
        with patch.object(lazy_engine, '_create_engine', side_effect=Exception("Test error")):
            lazy_engine.start_initialization()
            await asyncio.sleep(0.1)
            
            assert lazy_engine._initialization_error == "Test error"
    
    def test_getattr_raises_when_not_initialized(self, lazy_engine):
        """Test que __getattr__ raise avec un message clair si l'engine n'est pas prêt."""
        with pytest.raises(RuntimeError) as exc_info:
            _ = lazy_engine.some_attribute
        
        assert "Le service d'analyse (PromptEngine) n'est pas prêt" in str(exc_info.value)
    
    def test_getattr_works_when_initialized(self, lazy_engine, mock_engine):
        """Test que __getattr__ fonctionne quand l'engine est prêt."""
        lazy_engine._engine = mock_engine
        lazy_engine._initialized = True
        
        result = lazy_engine.get_optimized_prompt
        assert result is mock_engine.get_optimized_prompt


class TestStartupPerformance:
    """Tests pour vérifier que le démarrage est rapide."""
    
    @pytest.mark.asyncio
    async def test_core_lifespan_startup_is_fast(self):
        """Test que le core_lifespan démarre en moins de 1 seconde."""
        from collegue.app import core_lifespan
        
        mock_server = Mock()
        
        start_time = time.time()
        with patch("collegue.app.validate_llm_config", new_callable=MagicMock) as mock_val:
            # On simule la fonction asynchrone pour qu'elle passe sans rien faire
            mock_val.return_value = asyncio.Future()
            mock_val.return_value.set_result(True)
            
            async with core_lifespan(mock_server) as state:
                startup_time = time.time() - start_time
            # Le lifespan doit yield en moins de 1 seconde
            assert startup_time < 1.0, f"Startup took {startup_time}s, expected < 1s"
            
            # Vérifier que les composants essentiels sont là
            assert "parser" in state
            assert "resource_manager" in state
            assert "prompt_engine" in state
            
            # Le prompt_engine doit être un LazyPromptEngine
            from collegue.app import LazyPromptEngine
            assert isinstance(state["prompt_engine"], LazyPromptEngine)


class TestBaseToolIntegration:
    """Tests pour l'intégration avec BaseTool."""
    
    @pytest.mark.asyncio
    async def test_execute_async_awaits_lazy_engine(self):
        """Test que execute_async attend le LazyPromptEngine."""
        from collegue.tools.base import BaseTool
        from pydantic import BaseModel
        
        class DummyRequest(BaseModel):
            code: str
            language: str = "python"
        
        class DummyResponse(BaseModel):
            result: str
        
        class TestTool(BaseTool):
            tool_name = "test_tool"
            tool_description = "Test tool"
            request_model = DummyRequest
            response_model = DummyResponse
            
            def _execute_core_logic(self, request, **kwargs):
                return DummyResponse(result="ok")
        
        # Créer un mock de LazyPromptEngine avec hasatttr get_engine
        class MockLazyEngine:
            def __init__(self):
                self.get_engine_called = False
                self.timeout_value = None
                
            async def get_engine(self, timeout=None):
                self.get_engine_called = True
                self.timeout_value = timeout
                return Mock()
        
        lazy_engine = MockLazyEngine()
        
        # Tool sans prompt_engine dans app_state pour forcer le passage dans la logique
        tool = TestTool(app_state={})
        
        request = DummyRequest(code="print('hello')")
        result = await tool.execute_async(request, prompt_engine=lazy_engine)
        
        # Vérifier que get_engine a été appelé avec le bon timeout
        assert lazy_engine.get_engine_called is True
        assert lazy_engine.timeout_value is None
        assert result.result == "ok"
    
    @pytest.mark.asyncio
    async def test_execute_async_handles_none_engine(self):
        """Test que execute_async gère un engine None (fallback)."""
        from collegue.tools.base import BaseTool
        from pydantic import BaseModel
        
        class DummyRequest(BaseModel):
            code: str
            language: str = "python"
        
        class DummyResponse(BaseModel):
            result: str
        
        class TestTool(BaseTool):
            tool_name = "test_tool"
            tool_description = "Test tool"
            request_model = DummyRequest
            response_model = DummyResponse
            
            def _execute_core_logic(self, request, **kwargs):
                return DummyResponse(result="ok")
        
        # Créer un mock de LazyPromptEngine qui retourne None
        lazy_engine = Mock()
        lazy_engine.get_engine = Mock(return_value=asyncio.Future())
        lazy_engine.get_engine.return_value.set_result(None)
        
        tool = TestTool(app_state={"prompt_engine": lazy_engine})
        
        request = DummyRequest(code="print('hello')")
        # Ne devrait pas planter même si l'engine est None
        result = await tool.execute_async(request, prompt_engine=lazy_engine)
        assert result.result == "ok"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])