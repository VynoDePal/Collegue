"""
Système de Rate Limiting pour les outils Collegue.

Implémente plusieurs algorithmes de rate limiting:
- Token Bucket: Pour gérer les bursts
- Fixed Window: Simple et efficace
- Sliding Window Log: Précis mais plus coûteux en mémoire
"""
import time
import threading
from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum
import logging


class RateLimitExceeded(Exception):
    """Exception levée quand une limite de taux est dépassée."""
    
    def __init__(self, tool_name: str, limit: int, window: float, retry_after: float):
        self.tool_name = tool_name
        self.limit = limit
        self.window = window
        self.retry_after = retry_after
        message = (
            f"Rate limit exceeded pour '{tool_name}': "
            f"{limit} requêtes par {window:.0f}s. "
            f"Réessayez dans {retry_after:.1f}s."
        )
        super().__init__(message)


class RateLimitStrategy(Enum):
    """Stratégies de rate limiting disponibles."""
    TOKEN_BUCKET = "token_bucket"
    FIXED_WINDOW = "fixed_window"
    SLIDING_WINDOW = "sliding_window"


@dataclass
class RateLimitConfig:
    """Configuration d'une limite de taux."""
    requests_per_minute: int = 60
    burst: int = 10
    strategy: RateLimitStrategy = RateLimitStrategy.TOKEN_BUCKET
    
    @property
    def requests_per_second(self) -> float:
        return self.requests_per_minute / 60.0


class RateLimiter(ABC):
    """Classe de base pour les implémentations de rate limiting."""
    
    def __init__(self, config: RateLimitConfig, name: str = "default"):
        self.config = config
        self.name = name
        self._lock = threading.Lock()
        self._logger = logging.getLogger(f"rate_limiter.{name}")
    
    @abstractmethod
    def allow_request(self) -> Tuple[bool, Optional[float]]:
        """
        Vérifie si une requête est autorisée.
        
        Returns:
            Tuple[bool, Optional[float]]: (autorisé, temps d'attente si non autorisé)
        """
        pass
    
    @abstractmethod
    def get_stats(self) -> Dict[str, Any]:
        """Retourne les statistiques actuelles."""
        pass
    
    def check_and_record(self) -> None:
        """
        Vérifie et enregistre une requête.
        
        Raises:
            RateLimitExceeded: Si la limite est dépassée
        """
        allowed, retry_after = self.allow_request()
        if not allowed:
            raise RateLimitExceeded(
                tool_name=self.name,
                limit=self.config.requests_per_minute,
                window=60.0,
                retry_after=retry_after or 1.0
            )


class TokenBucketLimiter(RateLimiter):
    """
    Implémentation Token Bucket.
    
    Permet des bursts courts tout en maintenant un taux moyen.
    Le bucket se remplit à taux constant et chaque requête consomme un token.
    """
    
    def __init__(self, config: RateLimitConfig, name: str = "default"):
        super().__init__(config, name)
        self._tokens = float(config.burst)
        self._last_update = time.time()
        self._max_tokens = config.burst
        self._fill_rate = config.requests_per_second
    
    def _add_tokens(self):
        """Ajoute des tokens au bucket selon le temps écoulé."""
        now = time.time()
        elapsed = now - self._last_update
        self._last_update = now
        
        # Ajouter des tokens proportionnellement au temps écoulé
        self._tokens = min(
            self._max_tokens,
            self._tokens + elapsed * self._fill_rate
        )
    
    def allow_request(self) -> Tuple[bool, Optional[float]]:
        with self._lock:
            self._add_tokens()
            
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                self._logger.debug(f"Token consumed, remaining: {self._tokens:.2f}")
                return True, None
            
            # Calculer le temps d'attente
            wait_time = (1.0 - self._tokens) / self._fill_rate
            self._logger.warning(f"Rate limit hit, retry after: {wait_time:.2f}s")
            return False, wait_time
    
    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            self._add_tokens()
            return {
                "type": "token_bucket",
                "tokens_available": self._tokens,
                "tokens_max": self._max_tokens,
                "fill_rate": self._fill_rate,
                "utilization": 1.0 - (self._tokens / self._max_tokens)
            }


class FixedWindowLimiter(RateLimiter):
    """
    Implémentation Fixed Window.
    
    Compte les requêtes dans des fenêtres de temps fixes.
    Simple mais peut permettre des bursts aux limites de fenêtres.
    """
    
    def __init__(self, config: RateLimitConfig, name: str = "default"):
        super().__init__(config, name)
        self._window_size = 60.0  # 1 minute
        self._current_window = int(time.time() / self._window_size)
        self._request_count = 0
        self._max_requests = config.requests_per_minute
    
    def allow_request(self) -> Tuple[bool, Optional[float]]:
        with self._lock:
            now = time.time()
            current_window = int(now / self._window_size)
            
            # Nouvelle fenêtre
            if current_window > self._current_window:
                self._current_window = current_window
                self._request_count = 0
                self._logger.debug("New rate limit window started")
            
            if self._request_count < self._max_requests:
                self._request_count += 1
                return True, None
            
            # Calculer le temps avant la prochaine fenêtre
            next_window = (self._current_window + 1) * self._window_size
            wait_time = next_window - now
            self._logger.warning(f"Rate limit hit for window, retry after: {wait_time:.2f}s")
            return False, wait_time
    
    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            current_window = int(now / self._window_size)
            
            if current_window > self._current_window:
                return {
                    "type": "fixed_window",
                    "requests_in_window": 0,
                    "window_limit": self._max_requests,
                    "utilization": 0.0
                }
            
            return {
                "type": "fixed_window",
                "requests_in_window": self._request_count,
                "window_limit": self._max_requests,
                "utilization": self._request_count / self._max_requests
            }


class SlidingWindowLimiter(RateLimiter):
    """
    Implémentation Sliding Window Log.
    
    Garde un log des timestamps des requêtes.
    Plus précis mais utilise plus de mémoire.
    """
    
    def __init__(self, config: RateLimitConfig, name: str = "default"):
        super().__init__(config, name)
        self._window_size = 60.0  # 1 minute
        self._max_requests = config.requests_per_minute
        self._request_times: list = []
    
    def _cleanup_old_requests(self):
        """Supprime les requêtes hors de la fenêtre courante."""
        now = time.time()
        cutoff = now - self._window_size
        self._request_times = [t for t in self._request_times if t > cutoff]
    
    def allow_request(self) -> Tuple[bool, Optional[float]]:
        with self._lock:
            self._cleanup_old_requests()
            
            if len(self._request_times) < self._max_requests:
                self._request_times.append(time.time())
                return True, None
            
            # Calculer le temps d'attente
            oldest_request = self._request_times[0]
            wait_time = (oldest_request + self._window_size) - time.time()
            wait_time = max(0.0, wait_time)
            self._logger.warning(f"Rate limit hit, retry after: {wait_time:.2f}s")
            return False, wait_time
    
    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            self._cleanup_old_requests()
            return {
                "type": "sliding_window",
                "requests_in_window": len(self._request_times),
                "window_limit": self._max_requests,
                "utilization": len(self._request_times) / self._max_requests
            }


class RateLimiterFactory:
    """Factory pour créer les rate limiters appropriés."""
    
    _implementations = {
        RateLimitStrategy.TOKEN_BUCKET: TokenBucketLimiter,
        RateLimitStrategy.FIXED_WINDOW: FixedWindowLimiter,
        RateLimitStrategy.SLIDING_WINDOW: SlidingWindowLimiter,
    }
    
    @classmethod
    def create(
        cls,
        config: RateLimitConfig,
        name: str = "default"
    ) -> RateLimiter:
        """Crée un rate limiter selon la configuration."""
        impl = cls._implementations.get(config.strategy)
        if not impl:
            raise ValueError(f"Unknown rate limit strategy: {config.strategy}")
        return impl(config, name)


class RateLimiterManager:
    """
    Gestionnaire global de rate limiting.
    
    Gère les limites pour tous les tools avec une API simple.
    """
    
    # Configuration par défaut pour les tools
    DEFAULT_LIMITS: Dict[str, RateLimitConfig] = {
        "github_ops": RateLimitConfig(requests_per_minute=30, burst=5),
        "sentry_monitor": RateLimitConfig(requests_per_minute=20, burst=3),
        "iac_guardrails_scan": RateLimitConfig(requests_per_minute=10, burst=2),
        "dependency_guard": RateLimitConfig(requests_per_minute=60, burst=10),
        "postgres_db": RateLimitConfig(requests_per_minute=30, burst=5),
        "kubernetes_ops": RateLimitConfig(requests_per_minute=20, burst=3),
        "default": RateLimitConfig(requests_per_minute=60, burst=10),
    }
    
    def __init__(self):
        self._limiters: Dict[str, RateLimiter] = {}
        self._lock = threading.Lock()
        self._logger = logging.getLogger(__name__)
    
    def get_limiter(
        self,
        tool_name: str,
        config: Optional[RateLimitConfig] = None
    ) -> RateLimiter:
        """
        Récupère ou crée un rate limiter pour un tool.
        
        Args:
            tool_name: Nom du tool
            config: Configuration optionnelle (sinon utilise DEFAULT_LIMITS)
        
        Returns:
            RateLimiter configuré pour ce tool
        """
        with self._lock:
            if tool_name not in self._limiters:
                if config is None:
                    config = self.DEFAULT_LIMITS.get(
                        tool_name,
                        self.DEFAULT_LIMITS["default"]
                    )
                
                self._limiters[tool_name] = RateLimiterFactory.create(
                    config,
                    tool_name
                )
                self._logger.info(f"Created rate limiter for {tool_name}")
            
            return self._limiters[tool_name]
    
    def check_rate_limit(self, tool_name: str) -> None:
        """
        Vérifie la limite de taux pour un tool.
        
        Raises:
            RateLimitExceeded: Si la limite est dépassée
        """
        limiter = self.get_limiter(tool_name)
        limiter.check_and_record()
    
    def get_stats(self, tool_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Récupère les statistiques de rate limiting.
        
        Args:
            tool_name: Nom spécifique ou None pour tous
        
        Returns:
            Dict avec les statistiques
        """
        with self._lock:
            if tool_name:
                if tool_name in self._limiters:
                    return {tool_name: self._limiters[tool_name].get_stats()}
                return {}
            
            return {
                name: limiter.get_stats()
                for name, limiter in self._limiters.items()
            }
    
    def reset(self, tool_name: Optional[str] = None):
        """
        Réinitialise les rate limiters.
        
        Args:
            tool_name: Nom spécifique ou None pour tous
        """
        with self._lock:
            if tool_name:
                if tool_name in self._limiters:
                    del self._limiters[tool_name]
                    self._logger.info(f"Reset rate limiter for {tool_name}")
            else:
                self._limiters.clear()
                self._logger.info("Reset all rate limiters")


# Instance globale du gestionnaire
_rate_limiter_manager: Optional[RateLimiterManager] = None
_manager_lock = threading.Lock()


def get_rate_limiter_manager() -> RateLimiterManager:
    """Retourne l'instance globale du gestionnaire de rate limiting."""
    global _rate_limiter_manager
    if _rate_limiter_manager is None:
        with _manager_lock:
            if _rate_limiter_manager is None:
                _rate_limiter_manager = RateLimiterManager()
    return _rate_limiter_manager


def reset_rate_limiter_manager():
    """Réinitialise l'instance globale (utile pour les tests)."""
    global _rate_limiter_manager
    with _manager_lock:
        _rate_limiter_manager = None
