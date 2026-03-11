"""
Système de Quotas pour les outils Collegue.

Gère les limites globales pour:
- Tokens LLM par session
- Taille des fichiers analysés
- Nombre de fichiers par requête
- Temps d'exécution des tools
"""
import os
import time
import threading
from typing import Dict, Optional, Any, List
from dataclasses import dataclass, field
from enum import Enum
import logging


class QuotaExceeded(Exception):
    """Exception levée quand un quota est dépassé."""
    
    def __init__(self, quota_type: str, current: float, limit: float, details: str = ""):
        self.quota_type = quota_type
        self.current = current
        self.limit = limit
        message = f"Quota exceeded pour '{quota_type}': {current:.0f}/{limit:.0f}"
        if details:
            message += f" ({details})"
        super().__init__(message)


class QuotaType(Enum):
    """Types de quotas disponibles."""
    LLM_TOKENS = "llm_tokens"
    FILE_SIZE = "file_size"
    FILE_COUNT = "file_count"
    EXECUTION_TIME = "execution_time"
    REQUEST_SIZE = "request_size"


@dataclass
class QuotaConfig:
    """Configuration des quotas."""
    llm_tokens_per_session: int = 100000  # 100k tokens
    max_file_size_bytes: int = 1024 * 1024  # 1MB
    max_files_per_request: int = 100
    max_execution_time_seconds: float = 300.0  # 5 minutes
    max_request_size_bytes: int = 10 * 1024 * 1024  # 10MB
    
    @classmethod
    def from_env(cls) -> 'QuotaConfig':
        """Charge la configuration depuis les variables d'environnement."""
        return cls(
            llm_tokens_per_session=int(
                os.getenv('COLLEGUE_QUOTA_LLM_TOKENS', '100000')
            ),
            max_file_size_bytes=int(
                os.getenv('COLLEGUE_QUOTA_MAX_FILE_SIZE', str(1024 * 1024))
            ),
            max_files_per_request=int(
                os.getenv('COLLEGUE_QUOTA_MAX_FILES', '100')
            ),
            max_execution_time_seconds=float(
                os.getenv('COLLEGUE_QUOTA_MAX_EXEC_TIME', '300.0')
            ),
            max_request_size_bytes=int(
                os.getenv('COLLEGUE_QUOTA_MAX_REQUEST_SIZE', str(10 * 1024 * 1024))
            ),
        )


@dataclass
class ResourceUsage:
    """Suivi de l'utilisation des ressources."""
    llm_tokens_used: int = 0
    files_processed: int = 0
    total_bytes_processed: int = 0
    execution_start_time: Optional[float] = None
    request_size_bytes: int = 0
    
    @property
    def execution_time(self) -> float:
        """Calcule le temps d'exécution écoulé."""
        if self.execution_start_time is None:
            return 0.0
        return time.time() - self.execution_start_time
    
    def to_dict(self) -> Dict[str, Any]:
        """Convertit en dictionnaire."""
        return {
            "llm_tokens_used": self.llm_tokens_used,
            "files_processed": self.files_processed,
            "total_bytes_processed": self.total_bytes_processed,
            "execution_time_seconds": self.execution_time,
            "request_size_bytes": self.request_size_bytes,
        }


class QuotaManager:
    """
    Gestionnaire des quotas pour une session.
    
    Thread-safe et conçu pour être utilisé par tool.
    """
    
    def __init__(self, config: Optional[QuotaConfig] = None, session_id: str = "default"):
        self.config = config or QuotaConfig.from_env()
        self.session_id = session_id
        self._usage = ResourceUsage()
        self._lock = threading.Lock()
        self._logger = logging.getLogger(f"quota_manager.{session_id}")
        self._file_sizes: Dict[str, int] = {}  # Suivi individuel des fichiers
    
    def start_execution(self):
        """Marque le début de l'exécution d'un tool."""
        with self._lock:
            self._usage.execution_start_time = time.time()
            self._logger.debug("Execution started")
    
    def record_llm_tokens(self, tokens: int):
        """
        Enregistre l'utilisation de tokens LLM.
        
        Raises:
            QuotaExceeded: Si le quota de tokens est dépassé
        """
        with self._lock:
            new_total = self._usage.llm_tokens_used + tokens
            
            if new_total > self.config.llm_tokens_per_session:
                raise QuotaExceeded(
                    quota_type=QuotaType.LLM_TOKENS.value,
                    current=new_total,
                    limit=self.config.llm_tokens_per_session,
                    details=f"Session: {self.session_id}"
                )
            
            self._usage.llm_tokens_used = new_total
            self._logger.debug(f"Recorded {tokens} LLM tokens, total: {new_total}")
    
    def check_file_size(self, file_path: str, content: Optional[bytes] = None) -> int:
        """
        Vérifie si un fichier respecte la limite de taille.
        
        Args:
            file_path: Chemin du fichier
            content: Contenu du fichier (si déjà chargé)
        
        Returns:
            Taille du fichier en bytes
        
        Raises:
            QuotaExceeded: Si le fichier dépasse la limite
        """
        size = len(content) if content is not None else os.path.getsize(file_path)
        
        if size > self.config.max_file_size_bytes:
            raise QuotaExceeded(
                quota_type=QuotaType.FILE_SIZE.value,
                current=size,
                limit=self.config.max_file_size_bytes,
                details=f"File: {file_path} ({self._format_bytes(size)})"
            )
        
        with self._lock:
            self._file_sizes[file_path] = size
        
        return size
    
    def record_file_processed(self, file_path: str, size: int):
        """
        Enregistre le traitement d'un fichier.
        
        Raises:
            QuotaExceeded: Si le nombre max de fichiers est dépassé
        """
        with self._lock:
            new_count = self._usage.files_processed + 1
            
            if new_count > self.config.max_files_per_request:
                raise QuotaExceeded(
                    quota_type=QuotaType.FILE_COUNT.value,
                    current=new_count,
                    limit=self.config.max_files_per_request,
                    details=f"File: {file_path}"
                )
            
            self._usage.files_processed = new_count
            self._usage.total_bytes_processed += size
            self._logger.debug(f"Recorded file {file_path}, count: {new_count}")
    
    def check_execution_time(self) -> float:
        """
        Vérifie si le temps d'exécution est dans les limites.
        
        Returns:
            Temps d'exécution actuel
        
        Raises:
            QuotaExceeded: Si le temps max est dépassé
        """
        elapsed = self._usage.execution_time
        
        if elapsed > self.config.max_execution_time_seconds:
            raise QuotaExceeded(
                quota_type=QuotaType.EXECUTION_TIME.value,
                current=elapsed,
                limit=self.config.max_execution_time_seconds,
                details=f"Session: {self.session_id}"
            )
        
        return elapsed
    
    def check_request_size(self, size: int):
        """
        Vérifie si la taille de la requête est dans les limites.
        
        Raises:
            QuotaExceeded: Si la taille dépasse la limite
        """
        if size > self.config.max_request_size_bytes:
            raise QuotaExceeded(
                quota_type=QuotaType.REQUEST_SIZE.value,
                current=size,
                limit=self.config.max_request_size_bytes,
                details=f"Request too large ({self._format_bytes(size)})"
            )
        
        with self._lock:
            self._usage.request_size_bytes = size
    
    def get_usage_stats(self) -> Dict[str, Any]:
        """Retourne les statistiques d'utilisation."""
        with self._lock:
            stats = self._usage.to_dict()
            stats["quotas"] = {
                "llm_tokens": self.config.llm_tokens_per_session,
                "max_file_size": self.config.max_file_size_bytes,
                "max_files": self.config.max_files_per_request,
                "max_execution_time": self.config.max_execution_time_seconds,
                "max_request_size": self.config.max_request_size_bytes,
            }
            stats["utilization"] = {
                "llm_tokens_pct": (
                    self._usage.llm_tokens_used / self.config.llm_tokens_per_session * 100
                ),
                "files_pct": (
                    self._usage.files_processed / self.config.max_files_per_request * 100
                ),
                "execution_time_pct": (
                    self._usage.execution_time / self.config.max_execution_time_seconds * 100
                ),
            }
            return stats
    
    def reset(self):
        """Réinitialise les compteurs."""
        with self._lock:
            self._usage = ResourceUsage()
            self._file_sizes.clear()
            self._logger.info("Usage counters reset")
    
    @staticmethod
    def _format_bytes(size: int) -> str:
        """Formate une taille en bytes de manière lisible."""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"


class GlobalQuotaManager:
    """
    Gestionnaire global des quotas pour toutes les sessions.
    
    Singleton qui gère les QuotaManager par session.
    """
    
    def __init__(self):
        self._sessions: Dict[str, QuotaManager] = {}
        self._lock = threading.Lock()
        self._logger = logging.getLogger(__name__)
        self._global_config = QuotaConfig.from_env()
    
    def get_session_manager(self, session_id: str) -> QuotaManager:
        """
        Récupère ou crée un QuotaManager pour une session.
        
        Args:
            session_id: Identifiant unique de la session
        
        Returns:
            QuotaManager pour cette session
        """
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = QuotaManager(
                    config=self._global_config,
                    session_id=session_id
                )
                self._logger.info(f"Created quota manager for session {session_id}")
            return self._sessions[session_id]
    
    def cleanup_session(self, session_id: str):
        """
        Supprime un gestionnaire de session (cleanup).
        
        Args:
            session_id: Identifiant de la session
        """
        with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                self._logger.info(f"Cleaned up quota manager for session {session_id}")
    
    def get_all_stats(self) -> Dict[str, Dict[str, Any]]:
        """Retourne les statistiques de toutes les sessions."""
        with self._lock:
            return {
                session_id: manager.get_usage_stats()
                for session_id, manager in self._sessions.items()
            }
    
    def reset_session(self, session_id: str):
        """Réinitialise une session spécifique."""
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id].reset()
    
    def reset_all(self):
        """Réinitialise toutes les sessions."""
        with self._lock:
            for manager in self._sessions.values():
                manager.reset()
            self._logger.info("All sessions reset")


# Instance globale
_global_quota_manager: Optional[GlobalQuotaManager] = None
_quota_lock = threading.Lock()


def get_global_quota_manager() -> GlobalQuotaManager:
    """Retourne l'instance globale du gestionnaire de quotas."""
    global _global_quota_manager
    if _global_quota_manager is None:
        with _quota_lock:
            if _global_quota_manager is None:
                _global_quota_manager = GlobalQuotaManager()
    return _global_quota_manager


def reset_global_quota_manager():
    """Réinitialise l'instance globale (utile pour les tests)."""
    global _global_quota_manager
    with _quota_lock:
        _global_quota_manager = None


def check_all_quotas(
    session_id: str,
    file_paths: Optional[List[str]] = None,
    request_size: int = 0
) -> QuotaManager:
    """
    Vérifie tous les quotas pour une requête.
    
    Args:
        session_id: ID de session
        file_paths: Liste des fichiers à traiter
        request_size: Taille de la requête en bytes
    
    Returns:
        QuotaManager configuré
    
    Raises:
        QuotaExceeded: Si un quota est dépassé
    """
    manager = get_global_quota_manager().get_session_manager(session_id)
    
    # Vérifier taille de la requête
    if request_size > 0:
        manager.check_request_size(request_size)
    
    # Vérifier fichiers
    if file_paths:
        for path in file_paths:
            size = manager.check_file_size(path)
            manager.record_file_processed(path, size)
    
    return manager
