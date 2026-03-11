"""
Tests unitaires pour le système de rate limiting.
"""
import pytest
import time
import threading
from collegue.tools.rate_limiter import (
    TokenBucketLimiter,
    FixedWindowLimiter,
    SlidingWindowLimiter,
    RateLimiterManager,
    RateLimitConfig,
    RateLimitStrategy,
    RateLimitExceeded,
    RateLimiterFactory,
    get_rate_limiter_manager,
    reset_rate_limiter_manager,
)


class TestTokenBucketLimiter:
    """Tests pour le rate limiting Token Bucket."""
    
    def test_initial_tokens_available(self):
        """Test que les tokens initiaux sont disponibles."""
        config = RateLimitConfig(requests_per_minute=60, burst=10)
        limiter = TokenBucketLimiter(config, "test")
        
        # Les 10 premières requêtes devraient passer (burst)
        for i in range(10):
            allowed, wait = limiter.allow_request()
            assert allowed is True, f"Request {i+1} should be allowed"
    
    def test_rate_limit_hit(self):
        """Test que la limite est atteinte après épuisement des tokens."""
        config = RateLimitConfig(requests_per_minute=60, burst=2)
        limiter = TokenBucketLimiter(config, "test")
        
        # Épuiser les tokens
        limiter.allow_request()
        limiter.allow_request()
        
        # La troisième devrait être bloquée
        allowed, wait = limiter.allow_request()
        assert allowed is False
        assert wait > 0
    
    def test_token_refill(self):
        """Test que les tokens se remplissent avec le temps."""
        config = RateLimitConfig(requests_per_minute=60, burst=1)  # 1 token/sec
        limiter = TokenBucketLimiter(config, "test")
        
        # Épuiser le token
        limiter.allow_request()
        
        # Attendre un peu
        time.sleep(1.1)
        
        # Devrait avoir un token
        allowed, _ = limiter.allow_request()
        assert allowed is True
    
    def test_check_and_record_raises(self):
        """Test que check_and_record lève une exception si limite atteinte."""
        config = RateLimitConfig(requests_per_minute=60, burst=1)
        limiter = TokenBucketLimiter(config, "test")
        
        limiter.check_and_record()  # OK
        
        with pytest.raises(RateLimitExceeded) as exc_info:
            limiter.check_and_record()
        
        assert "test" in str(exc_info.value)
        assert "60" in str(exc_info.value)
    
    def test_get_stats(self):
        """Test les statistiques du limiter."""
        config = RateLimitConfig(requests_per_minute=60, burst=10)
        limiter = TokenBucketLimiter(config, "test")
        
        stats = limiter.get_stats()
        
        assert stats["type"] == "token_bucket"
        assert stats["tokens_max"] == 10
        assert stats["tokens_available"] <= 10
    
    def test_thread_safety(self):
        """Test la thread-safety du limiter."""
        config = RateLimitConfig(requests_per_minute=600, burst=100)
        limiter = TokenBucketLimiter(config, "test")
        
        results = {"allowed": 0, "blocked": 0}
        lock = threading.Lock()
        
        def make_requests():
            for _ in range(20):
                allowed, _ = limiter.allow_request()
                with lock:
                    if allowed:
                        results["allowed"] += 1
                    else:
                        results["blocked"] += 1
        
        threads = [threading.Thread(target=make_requests) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # Vérifier que le total est correct
        assert results["allowed"] + results["blocked"] == 100


class TestFixedWindowLimiter:
    """Tests pour le rate limiting Fixed Window."""
    
    def test_requests_in_window(self):
        """Test les requêtes dans une fenêtre."""
        config = RateLimitConfig(requests_per_minute=5, burst=5)
        limiter = FixedWindowLimiter(config, "test")
        
        # 5 requêtes devraient passer
        for i in range(5):
            allowed, _ = limiter.allow_request()
            assert allowed is True, f"Request {i+1} should be allowed"
        
        # La 6ème devrait être bloquée
        allowed, wait = limiter.allow_request()
        assert allowed is False
        assert wait > 0
    
    @pytest.mark.slow
    def test_window_reset(self):
        """Test la réinitialisation de la fenêtre."""
        config = RateLimitConfig(requests_per_minute=60, burst=1)  # 1 req/min
        limiter = FixedWindowLimiter(config, "test")
        
        limiter.allow_request()
        
        # Bloqué dans la même fenêtre
        allowed, _ = limiter.allow_request()
        assert allowed is False
        
        # Attendre la prochaine fenêtre (60 secondes pour 1 req/min)
        time.sleep(60.1)
        
        allowed, _ = limiter.allow_request()
        assert allowed is True


class TestSlidingWindowLimiter:
    """Tests pour le rate limiting Sliding Window."""
    
    @pytest.mark.slow
    def test_sliding_window_accuracy(self):
        """Test la précision de la fenêtre glissante."""
        config = RateLimitConfig(requests_per_minute=3, burst=3)
        limiter = SlidingWindowLimiter(config, "test")
        
        # 3 requêtes rapides
        for i in range(3):
            allowed, _ = limiter.allow_request()
            assert allowed is True, f"Request {i+1} should be allowed"
        
        # La 4ème devrait être bloquée
        allowed, _ = limiter.allow_request()
        assert allowed is False
        
        # Attendre que la plus vieille requête sorte de la fenêtre (60s)
        time.sleep(60.1)
        
        allowed, _ = limiter.allow_request()
        assert allowed is True

    @pytest.mark.slow
    def test_cleanup_old_requests(self):
        """Test le nettoyage des vieilles requêtes."""
        config = RateLimitConfig(requests_per_minute=10, burst=10)
        limiter = SlidingWindowLimiter(config, "test")
        
        # Faire quelques requêtes
        for _ in range(5):
            limiter.allow_request()
        
        # Attendre que les requêtes expirent (60s window)
        time.sleep(60.1)
        
        # Nettoyer
        limiter._cleanup_old_requests()
        
        # La liste devrait être vide
        assert len(limiter._request_times) == 0


class TestRateLimiterFactory:
    """Tests pour la factory de rate limiters."""
    
    def test_create_token_bucket(self):
        """Test la création d'un Token Bucket."""
        config = RateLimitConfig(
            requests_per_minute=60,
            burst=10,
            strategy=RateLimitStrategy.TOKEN_BUCKET
        )
        limiter = RateLimiterFactory.create(config, "test")
        
        assert isinstance(limiter, TokenBucketLimiter)
    
    def test_create_fixed_window(self):
        """Test la création d'un Fixed Window."""
        config = RateLimitConfig(
            requests_per_minute=60,
            burst=10,
            strategy=RateLimitStrategy.FIXED_WINDOW
        )
        limiter = RateLimiterFactory.create(config, "test")
        
        assert isinstance(limiter, FixedWindowLimiter)
    
    def test_create_sliding_window(self):
        """Test la création d'un Sliding Window."""
        config = RateLimitConfig(
            requests_per_minute=60,
            burst=10,
            strategy=RateLimitStrategy.SLIDING_WINDOW
        )
        limiter = RateLimiterFactory.create(config, "test")
        
        assert isinstance(limiter, SlidingWindowLimiter)


class TestRateLimitConfigValidation:
    """Tests pour la validation de RateLimitConfig."""
    
    def test_requests_per_minute_zero_raises(self):
        """Test que requests_per_minute=0 lève une erreur."""
        with pytest.raises(ValueError) as exc_info:
            RateLimitConfig(requests_per_minute=0, burst=10)
        
        assert "requests_per_minute must be > 0" in str(exc_info.value)
    
    def test_requests_per_minute_negative_raises(self):
        """Test que requests_per_minute négatif lève une erreur."""
        with pytest.raises(ValueError) as exc_info:
            RateLimitConfig(requests_per_minute=-1, burst=10)
        
        assert "requests_per_minute must be > 0" in str(exc_info.value)
    
    def test_burst_zero_raises(self):
        """Test que burst=0 lève une erreur."""
        with pytest.raises(ValueError) as exc_info:
            RateLimitConfig(requests_per_minute=60, burst=0)
        
        assert "burst must be > 0" in str(exc_info.value)
    
    def test_burst_negative_raises(self):
        """Test que burst négatif lève une erreur."""
        with pytest.raises(ValueError) as exc_info:
            RateLimitConfig(requests_per_minute=60, burst=-5)
        
        assert "burst must be > 0" in str(exc_info.value)
    
    def test_valid_config_no_raise(self):
        """Test qu'une config valide ne lève pas d'erreur."""
        config = RateLimitConfig(requests_per_minute=60, burst=10)
        
        assert config.requests_per_minute == 60
        assert config.burst == 10
    
    def test_create_unknown_strategy(self):
        """Test la création avec une stratégie inconnue."""
        config = RateLimitConfig()
        config.strategy = "unknown"
        
        with pytest.raises(ValueError) as exc_info:
            RateLimiterFactory.create(config, "test")
        
        assert "Unknown rate limit strategy" in str(exc_info.value)


class TestRateLimiterManager:
    """Tests pour le gestionnaire de rate limiting."""
    
    def setup_method(self):
        """Reset le manager avant chaque test."""
        reset_rate_limiter_manager()
    
    def teardown_method(self):
        """Reset le manager après chaque test."""
        reset_rate_limiter_manager()
    
    def test_get_limiter_creates_new(self):
        """Test que get_limiter crée un nouveau limiter."""
        manager = get_rate_limiter_manager()
        
        limiter = manager.get_limiter("test_tool")
        
        assert limiter is not None
        assert limiter.name == "test_tool"
    
    def test_get_limiter_returns_existing(self):
        """Test que get_limiter retourne un limiter existant."""
        manager = get_rate_limiter_manager()
        
        limiter1 = manager.get_limiter("test_tool")
        limiter2 = manager.get_limiter("test_tool")
        
        assert limiter1 is limiter2
    
    def test_check_rate_limit(self):
        """Test la vérification de rate limit."""
        manager = get_rate_limiter_manager()
        
        # Configurer une limite très basse
        config = RateLimitConfig(requests_per_minute=60, burst=1)
        manager.get_limiter("limited_tool", config)
        
        # Première requête OK
        manager.check_rate_limit("limited_tool")
        
        # Deuxième requête devrait échouer
        with pytest.raises(RateLimitExceeded):
            manager.check_rate_limit("limited_tool")
    
    def test_get_stats_empty(self):
        """Test les stats sans limiters."""
        manager = get_rate_limiter_manager()
        
        stats = manager.get_stats()
        
        assert stats == {}
    
    def test_get_stats_specific_tool(self):
        """Test les stats pour un tool spécifique."""
        manager = get_rate_limiter_manager()
        
        manager.get_limiter("tool1")
        
        stats = manager.get_stats("tool1")
        
        assert "tool1" in stats
    
    def test_get_stats_all_tools(self):
        """Test les stats pour tous les tools."""
        manager = get_rate_limiter_manager()
        
        manager.get_limiter("tool1")
        manager.get_limiter("tool2")
        
        stats = manager.get_stats()
        
        assert "tool1" in stats
        assert "tool2" in stats
    
    def test_reset_specific(self):
        """Test la réinitialisation d'un tool spécifique."""
        manager = get_rate_limiter_manager()
        
        manager.get_limiter("tool1")
        manager.get_limiter("tool2")
        
        manager.reset("tool1")
        
        stats = manager.get_stats()
        assert "tool1" not in stats
        assert "tool2" in stats
    
    def test_reset_all(self):
        """Test la réinitialisation de tous les tools."""
        manager = get_rate_limiter_manager()
        
        manager.get_limiter("tool1")
        manager.get_limiter("tool2")
        
        manager.reset()
        
        stats = manager.get_stats()
        assert stats == {}
    
    def test_default_limits(self):
        """Test que les limites par défaut sont appliquées."""
        manager = get_rate_limiter_manager()
        
        # Utiliser une limite par défaut
        limiter = manager.get_limiter("unknown_tool")
        
        stats = limiter.get_stats()
        assert stats is not None
    
    def test_custom_config(self):
        """Test l'utilisation d'une config personnalisée."""
        manager = get_rate_limiter_manager()
        
        config = RateLimitConfig(requests_per_minute=10, burst=2)
        limiter = manager.get_limiter("custom_tool", config)
        
        stats = limiter.get_stats()
        if stats["type"] == "token_bucket":
            assert stats["tokens_max"] == 2


class TestRateLimitExceeded:
    """Tests pour l'exception RateLimitExceeded."""
    
    def test_exception_message(self):
        """Test le message d'erreur."""
        exc = RateLimitExceeded(
            tool_name="github_ops",
            limit=30,
            window=60.0,
            retry_after=5.5
        )
        
        message = str(exc)
        
        assert "github_ops" in message
        assert "30" in message
        assert "5.5" in message or "5.5s" in message
    
    def test_exception_attributes(self):
        """Test les attributs de l'exception."""
        exc = RateLimitExceeded(
            tool_name="test",
            limit=10,
            window=60.0,
            retry_after=2.0
        )
        
        assert exc.tool_name == "test"
        assert exc.limit == 10
        assert exc.window == 60.0
        assert exc.retry_after == 2.0
