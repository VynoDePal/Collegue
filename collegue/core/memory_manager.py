"""
Gestionnaire de mémoire pour Collègue.

Fournit des utilitaires pour surveiller et nettoyer la mémoire,
prévenant les fuites sur les sessions longues.
"""
import gc
import sys
import weakref
import logging
from typing import Any, Dict, Optional, Callable, List
from dataclasses import dataclass, field
from collections import deque


# Logger pour le module
logger = logging.getLogger(__name__)


@dataclass
class MemoryStats:
    """Statistiques de mémoire."""
    tracked_objects: int = 0
    cleaned_objects: int = 0
    peak_memory_mb: float = 0.0
    current_memory_mb: float = 0.0


class MemoryManager:
    """
    Gestionnaire centralisé de la mémoire.
    
    Fournit:
    - Suivi des objets avec références faibles
    - Nettoyage périodique automatique
    - Limitation de la taille des collections
    - Cache avec TTL (Time To Live)
    """
    
    def __init__(self, max_history_size: int = 100, cleanup_threshold: int = 1000):
        self.max_history_size = max_history_size
        self.cleanup_threshold = cleanup_threshold
        self._tracked_objects: Dict[str, weakref.ref] = {}
        self._cleanup_callbacks: List[Callable] = []
        self._stats = MemoryStats()
        self._logger = logging.getLogger(__name__)
    
    def track_object(self, obj_id: str, obj: Any) -> None:
        """
        Suit un objet avec une référence faible.
        
        Args:
            obj_id: Identifiant unique de l'objet
            obj: Objet à suivre
        """
        def on_delete(ref):
            self._stats.cleaned_objects += 1
            self._logger.debug(f"Object {obj_id} garbage collected")
        
        self._tracked_objects[obj_id] = weakref.ref(obj, on_delete)
        self._stats.tracked_objects = len(self._tracked_objects)
    
    def untrack_object(self, obj_id: str) -> None:
        """Arrête de suivre un objet."""
        if obj_id in self._tracked_objects:
            del self._tracked_objects[obj_id]
            self._stats.tracked_objects = len(self._tracked_objects)
    
    def register_cleanup_callback(self, callback: Callable) -> None:
        """
        Enregistre une fonction de nettoyage à appeler périodiquement.
        
        Args:
            callback: Fonction à appeler lors du nettoyage
        """
        self._cleanup_callbacks.append(callback)
    
    def cleanup(self, force: bool = False) -> int:
        """
        Déclenche le nettoyage de la mémoire.
        
        Args:
            force: Si True, force le garbage collection complet
        
        Returns:
            Nombre d'objets nettoyés
        """
        # Exécuter les callbacks de nettoyage enregistrés
        for callback in self._cleanup_callbacks:
            try:
                callback()
            except Exception as e:
                self._logger.warning(f"Cleanup callback failed: {e}")
        
        # Nettoyer les références faibles mortes
        dead_refs = [
            obj_id for obj_id, ref in self._tracked_objects.items()
            if ref() is None
        ]
        for obj_id in dead_refs:
            del self._tracked_objects[obj_id]
        
        # Forcer le garbage collection si demandé
        if force:
            gc.collect()
        
        cleaned = len(dead_refs)
        self._stats.cleaned_objects += cleaned
        self._stats.tracked_objects = len(self._tracked_objects)
        
        if cleaned > 0:
            self._logger.info(f"Memory cleanup: {cleaned} objects cleaned")
        
        return cleaned
    
    def get_stats(self) -> MemoryStats:
        """Retourne les statistiques de mémoire."""
        return self._stats
    
    @staticmethod
    def limit_collection_size(collection: deque, max_size: int) -> int:
        """
        Limite la taille d'une collection (deque, list, etc.).
        
        Args:
            collection: Collection à limiter
            max_size: Taille maximale
        
        Returns:
            Nombre d'éléments supprimés
        """
        if isinstance(collection, deque):
            removed = max(0, len(collection) - max_size)
            while len(collection) > max_size:
                try:
                    collection.popleft()
                except IndexError:
                    break
            return removed
        elif isinstance(collection, list):
            removed = max(0, len(collection) - max_size)
            if removed > 0:
                del collection[:removed]
            return removed
        return 0


class LimitedSizeHistory:
    """
    Historique avec taille limitée.
    
    Supprime automatiquement les anciens éléments quand la limite est atteinte.
    """
    
    def __init__(self, max_size: int = 100, name: str = "history"):
        self.max_size = max_size
        self.name = name
        self._deque: deque = deque(maxlen=max_size)
        self._logger = logging.getLogger(f"{__name__}.{name}")
    
    def append(self, item: Any) -> None:
        """Ajoute un élément, supprime automatiquement le plus ancien si nécessaire."""
        if len(self._deque) >= self.max_size:
            self._logger.debug(f"History {self.name} full, removing oldest item")
        self._deque.append(item)
    
    def get_all(self) -> List[Any]:
        """Retourne tous les éléments sous forme de liste."""
        return list(self._deque)
    
    def clear(self) -> None:
        """Vide l'historique."""
        self._deque.clear()
        self._logger.info(f"History {self.name} cleared")
    
    def __len__(self) -> int:
        return len(self._deque)
    
    def __iter__(self):
        return iter(self._deque)


class TTLCache:
    """
    Cache avec expiration automatique (TTL).
    
    Les entrées expirent après un certain temps et sont automatiquement nettoyées.
    """
    
    def __init__(self, max_size: int = 100, ttl_seconds: float = 3600, name: str = "cache"):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self.name = name
        self._cache: Dict[str, Any] = {}
        self._timestamps: Dict[str, float] = {}
        self._logger = logging.getLogger(f"{__name__}.{name}")
    
    def _is_expired(self, key: str) -> bool:
        """Vérifie si une entrée est expirée."""
        import time
        if key not in self._timestamps:
            return True
        return time.time() - self._timestamps[key] > self.ttl_seconds
    
    def _cleanup_expired(self) -> int:
        """Nettoie les entrées expirées."""
        expired_keys = [
            key for key in list(self._cache.keys())
            if self._is_expired(key)
        ]
        for key in expired_keys:
            del self._cache[key]
            del self._timestamps[key]
        
        if expired_keys:
            self._logger.debug(f"Cleaned {len(expired_keys)} expired entries from {self.name}")
        
        return len(expired_keys)
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        Récupère une valeur du cache.
        
        Retourne default si la clé n'existe pas ou est expirée.
        """
        self._cleanup_expired()
        
        if key in self._cache and not self._is_expired(key):
            return self._cache[key]
        
        # Nettoyer si expiré
        if key in self._cache:
            del self._cache[key]
            del self._timestamps[key]
        
        return default
    
    def set(self, key: str, value: Any) -> None:
        """Stocke une valeur dans le cache."""
        import time
        
        # Nettoyer si nécessaire avant d'ajouter
        if len(self._cache) >= self.max_size:
            self._cleanup_expired()
            
            # Si toujours plein, supprimer l'entrée la plus ancienne
            if len(self._cache) >= self.max_size and self._timestamps:
                oldest_key = min(self._timestamps, key=self._timestamps.get)
                del self._cache[oldest_key]
                del self._timestamps[oldest_key]
        
        self._cache[key] = value
        self._timestamps[key] = time.time()
    
    def clear(self) -> None:
        """Vide le cache."""
        self._cache.clear()
        self._timestamps.clear()
        self._logger.info(f"Cache {self.name} cleared")
    
    def __len__(self) -> int:
        self._cleanup_expired()
        return len(self._cache)


# Instance globale du gestionnaire de mémoire
_memory_manager: Optional[MemoryManager] = None


def get_memory_manager() -> MemoryManager:
    """Retourne l'instance globale du gestionnaire de mémoire."""
    global _memory_manager
    if _memory_manager is None:
        _memory_manager = MemoryManager()
    return _memory_manager


def cleanup_all() -> int:
    """
    Nettoie toute la mémoire (fonction utilitaire globale).
    
    Returns:
        Nombre total d'objets nettoyés
    """
    manager = get_memory_manager()
    cleaned = manager.cleanup(force=True)
    
    # Forcer le garbage collection Python
    gc.collect()
    
    return cleaned
