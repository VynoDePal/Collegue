"""
Tests pour les corrections de l'issue #199 - Problèmes résiduels rate limiting.
"""
import pytest
from pydantic import BaseModel
from collegue.tools.base import BaseTool, ToolValidationError
from collegue.tools.rate_limiter import (
    get_rate_limiter_manager,
    reset_rate_limiter_manager,
    RateLimitConfig,
    RateLimitStrategy,
)
from collegue.tools.quotas import (
    get_global_quota_manager,
    reset_global_quota_manager,
)


# Modèles de test
class TestRequest(BaseModel):
    code: str
    language: str = "python"


class TestResponse(BaseModel):
    result: str
    success: bool = True


class TestTool(BaseTool):
    """Tool de test simple."""
    tool_name = "test_tool_199"
    tool_description = "A test tool"
    request_model = TestRequest
    response_model = TestResponse
    
    def _execute_core_logic(self, request: TestRequest, **kwargs) -> TestResponse:
        return TestResponse(result=f"Processed: {request.code}")


class TestCustomRateLimitTool(BaseTool):
    """Tool avec rate limit personnalisé."""
    tool_name = "custom_rate_tool"
    tool_description = "Tool with custom rate limit"
    request_model = TestRequest
    response_model = TestResponse
    custom_rate_limit = RateLimitConfig(
        requests_per_minute=30,
        burst=5,
        strategy=RateLimitStrategy.TOKEN_BUCKET
    )
    
    def _execute_core_logic(self, request: TestRequest, **kwargs) -> TestResponse:
        return TestResponse(result="OK")


class TestNormalizeRequestReturnsNormalized:
    """Tests que normalize_request retourne la requête normalisée."""
    
    def test_normalize_request_returns_base_model(self):
        """Test que normalize_request retourne une instance BaseModel."""
        tool = TestTool()
        request = TestRequest(code="test")
        
        result = tool.normalize_request(request)
        
        assert isinstance(result, BaseModel)
        assert isinstance(result, TestRequest)
        assert result.code == "test"
    
    def test_normalize_request_returns_same_instance_if_correct(self):
        """Test que normalize_request retourne la même instance si déjà correct."""
        tool = TestTool()
        request = TestRequest(code="hello", language="python")
        
        result = tool.normalize_request(request)
        
        assert result is request  # Même instance si déjà bon type
    
    def test_normalize_request_converts_dict(self):
        """Test que normalize_request convertit un dict vers le modèle."""
        tool = TestTool()
        request_dict = {"code": "from_dict", "language": "javascript"}
        
        result = tool.normalize_request(request_dict)
        
        assert isinstance(result, TestRequest)
        assert result.code == "from_dict"
        assert result.language == "javascript"
    
    def test_normalize_request_invalid_type_raises(self):
        """Test que normalize_request lève une erreur pour type invalide."""
        tool = TestTool()
        
        with pytest.raises(ToolValidationError):
            tool.normalize_request(12345)


class TestValidateRequest:
    """Tests que validate_request fonctionne correctement."""
    
    def test_validate_request_returns_true_for_valid_request(self):
        """Test que validate_request retourne True pour une requête valide."""
        tool = TestTool()
        request = TestRequest(code="test")
        
        result = tool.validate_request(request)
        
        assert result is True
    
    def test_validate_request_returns_true_for_valid_dict(self):
        """Test que validate_request retourne True pour un dict valide."""
        tool = TestTool()
        request_dict = {"code": "test", "language": "python"}
        
        result = tool.validate_request(request_dict)
        
        assert result is True
    
    def test_validate_request_raises_for_invalid_type(self):
        """Test que validate_request lève une erreur pour type invalide."""
        tool = TestTool()
        
        with pytest.raises(ToolValidationError):
            tool.validate_request(12345)


class TestCustomRateLimitUpdatesExisting:
    """Tests que custom_rate_limit met à jour un limiter existant."""
    
    def setup_method(self):
        """Reset avant chaque test."""
        reset_rate_limiter_manager()
    
    def teardown_method(self):
        """Reset après chaque test."""
        reset_rate_limiter_manager()
    
    def test_custom_rate_limit_creates_new_limiter(self):
        """Test que custom_rate_limit crée un nouveau limiter."""
        tool = TestCustomRateLimitTool()
        
        tool._check_rate_limit()
        
        manager = get_rate_limiter_manager()
        limiter = manager.get_limiter("custom_rate_tool")
        
        assert limiter.config.requests_per_minute == 30
        assert limiter.config.burst == 5
    
    def test_custom_rate_limit_updates_existing_limiter(self):
        """Test que custom_rate_limit met à jour un limiter existant."""
        manager = get_rate_limiter_manager()
        
        default_config = RateLimitConfig(requests_per_minute=60, burst=10)
        manager.get_limiter("dynamic_tool", default_config)
        
        limiter = manager.get_limiter("dynamic_tool")
        assert limiter.config.requests_per_minute == 60
        
        class DynamicTool(BaseTool):
            tool_name = "dynamic_tool"
            request_model = TestRequest
            response_model = TestResponse
            custom_rate_limit = RateLimitConfig(
                requests_per_minute=15,
                burst=3
            )
            
            def _execute_core_logic(self, request, **kwargs):
                return TestResponse(result="OK")
        
        tool = DynamicTool()
        tool._check_rate_limit()
        
        updated_limiter = manager.get_limiter("dynamic_tool")
        assert updated_limiter.config.requests_per_minute == 15
        assert updated_limiter.config.burst == 3
    
    def test_rate_limiter_manager_config_differs(self):
        """Test la méthode _config_differs du manager."""
        manager = get_rate_limiter_manager()
        
        config1 = RateLimitConfig(requests_per_minute=60, burst=10)
        config2 = RateLimitConfig(requests_per_minute=60, burst=10)
        config3 = RateLimitConfig(requests_per_minute=30, burst=5)
        
        assert not manager._config_differs(config1, config2)
        assert manager._config_differs(config1, config3)
        
        config4 = RateLimitConfig(requests_per_minute=60, burst=20)
        assert manager._config_differs(config1, config4)
        
        config5 = RateLimitConfig(
            requests_per_minute=60,
            burst=10,
            strategy=RateLimitStrategy.FIXED_WINDOW
        )
        assert manager._config_differs(config1, config5)


class TestExecuteUsesValidatedRequest:
    """Tests que execute() utilise la requête validée."""
    
    def setup_method(self):
        """Reset avant chaque test."""
        reset_rate_limiter_manager()
        reset_global_quota_manager()
    
    def teardown_method(self):
        """Reset après chaque test."""
        reset_rate_limiter_manager()
        reset_global_quota_manager()
    
    def test_execute_with_dict_request(self):
        """Test execute() avec un dict comme requête."""
        tool = TestTool()
        
        request_dict = {"code": "test_dict", "language": "python"}
        result = tool.execute(request_dict)
        
        assert result.success is True
        assert "test_dict" in result.result
    
    def test_execute_async_with_dict_request(self):
        """Test execute_async() avec un dict comme requête."""
        import asyncio
        
        tool = TestTool()
        
        async def run_test():
            request_dict = {"code": "async_dict", "language": "python"}
            result = await tool.execute_async(request_dict)
            
            assert result.success is True
            assert "async_dict" in result.result
        
        asyncio.run(run_test())


class TestUpdateLimiterMethod:
    """Tests pour la méthode update_limiter."""
    
    def setup_method(self):
        """Reset avant chaque test."""
        reset_rate_limiter_manager()
    
    def teardown_method(self):
        """Reset après chaque test."""
        reset_rate_limiter_manager()
    
    def test_update_limiter_force_update(self):
        """Test que update_limiter force la mise à jour."""
        manager = get_rate_limiter_manager()
        
        initial_config = RateLimitConfig(requests_per_minute=100, burst=20)
        manager.get_limiter("force_update_tool", initial_config)
        
        new_config = RateLimitConfig(requests_per_minute=5, burst=1)
        manager.update_limiter("force_update_tool", new_config)
        
        limiter = manager.get_limiter("force_update_tool")
        assert limiter.config.requests_per_minute == 5
        assert limiter.config.burst == 1
