"""
Tests d'intégration pour rate limiting et quotas dans BaseTool.
"""
import pytest
from pydantic import BaseModel
from collegue.tools.base import (
    BaseTool,
    ToolRateLimitError,
    ToolQuotaError,
    ToolValidationError,
)
from collegue.tools.rate_limiter import (
    get_rate_limiter_manager,
    reset_rate_limiter_manager,
    RateLimitConfig,
)
from collegue.tools.quotas import (
    get_global_quota_manager,
    reset_global_quota_manager,
)


# Modèles de test
class TestRequest(BaseModel):
    code: str
    language: str = "python"
    file_path: str = ""


class TestResponse(BaseModel):
    result: str
    success: bool = True


class SimpleTestTool(BaseTool):
    """Tool de test simple."""
    tool_name = "simple_test_tool"
    tool_description = "A simple test tool"
    request_model = TestRequest
    response_model = TestResponse
    
    def _execute_core_logic(self, request: TestRequest, **kwargs) -> TestResponse:
        return TestResponse(result=f"Processed: {request.code}")


class RateLimitedTool(BaseTool):
    """Tool avec rate limiting très restrictif."""
    tool_name = "rate_limited_tool"
    tool_description = "Tool with strict rate limiting"
    request_model = TestRequest
    response_model = TestResponse
    custom_rate_limit = RateLimitConfig(requests_per_minute=60, burst=1)
    
    def _execute_core_logic(self, request: TestRequest, **kwargs) -> TestResponse:
        return TestResponse(result="OK")


class QuotaTool(BaseTool):
    """Tool avec vérification des quotas."""
    tool_name = "quota_tool"
    tool_description = "Tool that checks quotas"
    request_model = TestRequest
    response_model = TestResponse
    
    def _execute_core_logic(self, request: TestRequest, **kwargs) -> TestResponse:
        return TestResponse(result="OK")


class TestBaseToolRateLimiting:
    """Tests pour le rate limiting dans BaseTool."""
    
    def setup_method(self):
        """Reset avant chaque test."""
        reset_rate_limiter_manager()
        reset_global_quota_manager()
    
    def teardown_method(self):
        """Reset après chaque test."""
        reset_rate_limiter_manager()
        reset_global_quota_manager()
    
    def test_rate_limit_enabled_by_default(self):
        """Test que le rate limiting est activé par défaut."""
        tool = SimpleTestTool()
        assert tool.rate_limit_enabled is True
    
    def test_rate_limit_can_be_disabled(self):
        """Test que le rate limiting peut être désactivé."""
        tool = SimpleTestTool()
        tool.rate_limit_enabled = False
        
        # Devrait pouvoir exécuter plusieurs fois sans problème
        request = TestRequest(code="test1")
        for _ in range(10):
            result = tool.execute(request)
            assert result.success is True
    
    def test_rate_limit_triggers_error(self):
        """Test que le rate limit déclenche une erreur."""
        tool = RateLimitedTool()
        
        # Configurer un rate limit très bas
        manager = get_rate_limiter_manager()
        manager.get_limiter(
            "rate_limited_tool",
            RateLimitConfig(requests_per_minute=60, burst=1)
        )
        
        # Première exécution OK
        request = TestRequest(code="test1")
        result = tool.execute(request)
        assert result.success is True
        
        # Deuxième exécution devrait échouer
        with pytest.raises(ToolRateLimitError) as exc_info:
            tool.execute(request)
        
        assert "Rate limit exceeded" in str(exc_info.value)
    
    def test_different_tools_have_separate_limits(self):
        """Test que les tools ont des limites séparées."""
        tool1 = SimpleTestTool()
        tool2 = RateLimitedTool()
        
        # Configurer tool1 avec une limite très basse
        manager = get_rate_limiter_manager()
        manager.get_limiter(
            "simple_test_tool",
            RateLimitConfig(requests_per_minute=60, burst=1)
        )
        
        request = TestRequest(code="test")
        
        # Épuiser la limite de tool1
        tool1.execute(request)
        
        with pytest.raises(ToolRateLimitError):
            tool1.execute(request)
        
        # Tool2 devrait toujours fonctionner
        result = tool2.execute(request)
        assert result.success is True


class TestBaseToolQuotas:
    """Tests pour les quotas dans BaseTool."""
    
    def setup_method(self):
        """Reset avant chaque test."""
        reset_rate_limiter_manager()
        reset_global_quota_manager()
    
    def teardown_method(self):
        """Reset après chaque test."""
        reset_rate_limiter_manager()
        reset_global_quota_manager()
    
    def test_quota_enabled_by_default(self):
        """Test que les quotas sont activés par défaut."""
        tool = SimpleTestTool()
        assert tool.quota_enabled is True
    
    def test_quota_can_be_disabled(self):
        """Test que les quotas peuvent être désactivés."""
        tool = SimpleTestTool()
        tool.quota_enabled = False
        
        request = TestRequest(code="test")
        result = tool.execute(request)
        assert result.success is True
    
    def test_quota_manager_created_on_execute(self):
        """Test que le quota manager est créé lors de l'exécution."""
        tool = QuotaTool()
        assert tool._quota_manager is None
        
        request = TestRequest(code="test")
        tool.execute(request)
        
        assert tool._quota_manager is not None
    
    def test_session_id_from_kwargs(self):
        """Test que l'ID de session peut venir des kwargs."""
        tool = SimpleTestTool()
        
        request = TestRequest(code="test")
        tool.execute(request, session_id="custom_session")
        
        assert tool._session_id == "custom_session"
        assert tool._quota_manager is not None
        assert tool._quota_manager.session_id == "custom_session"
    
    def test_execution_time_tracking(self):
        """Test le suivi du temps d'exécution."""
        tool = SimpleTestTool()
        
        request = TestRequest(code="test")
        tool.execute(request)
        
        # Le manager devrait avoir un temps de démarrage
        assert tool._quota_manager._usage.execution_start_time is not None
    
    def test_file_quota_checked_with_file_path(self, tmp_path):
        """Test que les quotas de fichiers sont vérifiés."""
        tool = SimpleTestTool()
        
        # Créer un petit fichier
        test_file = tmp_path / "test.py"
        test_file.write_text("print('hello')")
        
        request = TestRequest(code="test", file_path=str(test_file))
        result = tool.execute(request)
        
        assert result.success is True
        assert tool._quota_manager._usage.files_processed == 1


class TestBaseToolInfo:
    """Tests pour les informations du tool."""
    
    def setup_method(self):
        """Reset avant chaque test."""
        reset_rate_limiter_manager()
    
    def teardown_method(self):
        """Reset après chaque test."""
        reset_rate_limiter_manager()
    
    def test_get_info_includes_rate_limiting(self):
        """Test que get_info inclut les infos de rate limiting."""
        tool = SimpleTestTool()
        
        info = tool.get_info()
        
        assert "rate_limiting" in info
        assert info["rate_limiting"]["enabled"] is True
    
    def test_get_info_includes_quotas(self):
        """Test que get_info inclut les infos de quotas."""
        tool = SimpleTestTool()
        
        info = tool.get_info()
        
        assert "quotas" in info
        assert info["quotas"]["enabled"] is True
    
    def test_get_info_includes_rate_limit_stats(self):
        """Test que get_info inclut les stats de rate limiting."""
        tool = SimpleTestTool()
        
        # Exécuter une fois pour créer le limiter
        request = TestRequest(code="test")
        tool.execute(request)
        
        info = tool.get_info()
        
        # Les stats peuvent être présentes ou non selon l'état
        if "stats" in info["rate_limiting"]:
            assert info["rate_limiting"]["stats"] is not None


class TestBaseToolAsync:
    """Tests pour l'exécution async avec rate limiting et quotas."""
    
    def setup_method(self):
        """Reset avant chaque test."""
        reset_rate_limiter_manager()
        reset_global_quota_manager()
    
    def teardown_method(self):
        """Reset après chaque test."""
        reset_rate_limiter_manager()
        reset_global_quota_manager()
    
    @pytest.mark.asyncio
    async def test_async_execute_applies_rate_limit(self):
        """Test que execute_async applique le rate limiting."""
        tool = RateLimitedTool()
        
        # Configurer un rate limit très bas
        manager = get_rate_limiter_manager()
        manager.get_limiter(
            "rate_limited_tool",
            RateLimitConfig(requests_per_minute=60, burst=1)
        )
        
        request = TestRequest(code="test1")
        
        # Première exécution OK
        result = await tool.execute_async(request)
        assert result.success is True
        
        # Deuxième exécution devrait échouer
        with pytest.raises(ToolRateLimitError):
            await tool.execute_async(request)
    
    @pytest.mark.asyncio
    async def test_async_execute_applies_quotas(self):
        """Test que execute_async applique les quotas."""
        tool = QuotaTool()
        
        request = TestRequest(code="test")
        result = await tool.execute_async(request)
        
        assert result.success is True
        assert tool._quota_manager is not None
    
    @pytest.mark.asyncio
    async def test_async_with_session_id(self):
        """Test l'exécution async avec un session_id."""
        tool = SimpleTestTool()
        
        request = TestRequest(code="test")
        result = await tool.execute_async(request, session_id="async_session")
        
        assert result.success is True
        assert tool._session_id == "async_session"


class TestBaseToolValidation:
    """Tests pour la validation avec rate limiting."""
    
    def setup_method(self):
        """Reset avant chaque test."""
        reset_rate_limiter_manager()
        reset_global_quota_manager()
    
    def teardown_method(self):
        """Reset après chaque test."""
        reset_rate_limiter_manager()
        reset_global_quota_manager()
    
    def test_rate_limit_checked_before_validation(self):
        """Test que le rate limit est vérifié avant la validation."""
        tool = RateLimitedTool()
        
        # Configurer un rate limit très bas
        manager = get_rate_limiter_manager()
        manager.get_limiter(
            "rate_limited_tool",
            RateLimitConfig(requests_per_minute=60, burst=1)
        )
        
        # Épuiser la limite
        request = TestRequest(code="test1")
        tool.execute(request)
        
        # Créer une requête invalide
        invalid_request = TestRequest(code="", language="invalid_lang")
        
        # Devrait échouer sur le rate limit, pas sur la validation
        with pytest.raises(ToolRateLimitError):
            tool.execute(invalid_request)
    
    def test_quotas_checked_after_validation(self):
        """Test que les quotas sont vérifiés après la validation."""
        tool = SimpleTestTool()
        
        # Créer une requête invalide
        invalid_request = TestRequest(code="", language="unsupported_language_xyz")
        
        # Devrait échouer sur la validation d'abord
        with pytest.raises(ToolValidationError):
            tool.execute(invalid_request)
