"""
Gestionnaire de mémoire pour Collègue.

Fournit des utilitaires pour surveiller et nettoyer la mémoire,
prévenant les fuites sur les sessions longues.
"""
import gc
import weakref
import logging
import threading
import time
from typing import Any, Dict, Optional, Callable, List, Iterator, Tuple
from dataclasses import dataclass
from collections import deque


# Lock pour le singleton MemoryManager
_memory_manager_lock = threading.Lock()


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
        self._lock = threading.RLock()
    
    def track_object(self, obj_id: str, obj: Any) -> None:
        """
        Suit un objet avec une référence faible.

        Args:
            obj_id: Identifiant unique de l'objet
            obj: Objet à suivre (doit être weakref-able)

        Raises:
            ValueError: Si un objet avec le même ID est déjà suivi
            TypeError: Si l'objet n'est pas weakref-able (ex: int, str, tuple)
        """
        # Capturer l'ID dans la closure
        captured_id = obj_id

        def on_delete(ref):
            # Supprimer l'entrée du dict quand l'objet est garbage collecté
            with self._lock:
                try:
                    # Vérifier que l'entrée correspond toujours à cette weakref
                    # pour éviter de supprimer un nouvel objet tracké avec le même ID
                    if captured_id in self._tracked_objects:
                        existing_ref = self._tracked_objects[captured_id]
                        if existing_ref is ref or existing_ref() is None:
                            del self._tracked_objects[captured_id]
                            self._stats.tracked_objects = len(self._tracked_objects)
                except KeyError:
                    pass  # Déjà supprimé

        with self._lock:
            # Refuser les doublons pour éviter les problèmes de callback
            if obj_id in self._tracked_objects:
                existing_ref = self._tracked_objects[obj_id]
                if existing_ref() is not None:
                    raise ValueError(f"Object with ID '{obj_id}' is already tracked")
                # L'ancienne entrée est morte, on peut la remplacer
            try:
                self._tracked_objects[obj_id] = weakref.ref(obj, on_delete)
            except TypeError as e:
                raise TypeError(
                    f"Object of type {type(obj).__name__} is not weakref-able. "
                    f"Only objects that support weak references can be tracked."
                ) from e
            self._stats.tracked_objects = len(self._tracked_objects)

    def untrack_object(self, obj_id: str) -> None:
        """Arrête de suivre un objet."""
        with self._lock:
            if obj_id in self._tracked_objects:
                del self._tracked_objects[obj_id]
                self._stats.tracked_objects = len(self._tracked_objects)
    
    def register_cleanup_callback(self, callback: Callable) -> None:
        """
        Enregistre une fonction de nettoyage à appeler périodiquement.
        Thread-safe via verrou interne.

        Args:
            callback: Fonction à appeler lors du nettoyage
        """
        with self._lock:
            self._cleanup_callbacks.append(callback)
    
    def cleanup(self, force: bool = False) -> int:
        """
        Déclenche le nettoyage de la mémoire.

        Args:
            force: Si True, force le garbage collection complet

        Returns:
            Nombre d'objets nettoyés
        """
        with self._lock:
            # Exécuter les callbacks de nettoyage enregistrés
            for callback in self._cleanup_callbacks:
                try:
                    callback()
                except Exception as e:
                    self._logger.warning(f"Cleanup callback failed: {e}")

            # Forcer le garbage collection si demandé (pour mettre à jour les weakrefs)
            if force:
                gc.collect()

            # Nettoyer les références faibles mortes
            dead_refs = [
                obj_id for obj_id, ref in list(self._tracked_objects.items())
                if ref() is None
            ]
            for obj_id in dead_refs:
                del self._tracked_objects[obj_id]

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
    Fournit des opérations de cache de base de type dict (get/set/clear/items/keys/values).
    Thread-safe via un verrou interne.
    """

    def __init__(self, max_size: int = 100, ttl_seconds: float = 3600, name: str = "cache"):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self.name = name
        self._cache: Dict[str, Any] = {}
        self._timestamps: Dict[str, float] = {}
        self._logger = logging.getLogger(f"{__name__}.{name}")
        self._lock = threading.RLock()
    
    def _is_expired(self, key: str) -> bool:
        """Vérifie si une entrée est expirée (utilise time.monotonic pour robustesse)."""
        if key not in self._timestamps:
            return True
        return time.monotonic() - self._timestamps[key] > self.ttl_seconds

    def _cleanup_expired(self) -> int:
        """Nettoie les entrées expirées."""
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            # Nettoyer si nécessaire avant d'ajouter
            if len(self._cache) >= self.max_size:
                self._cleanup_expired()

                # Si toujours plein, supprimer l'entrée la plus ancienne
                if len(self._cache) >= self.max_size and self._timestamps:
                    oldest_key = min(self._timestamps, key=self._timestamps.get)
                    del self._cache[oldest_key]
                    del self._timestamps[oldest_key]

            self._cache[key] = value
            self._timestamps[key] = time.monotonic()

    def clear(self) -> None:
        """Vide le cache."""
        with self._lock:
            self._cache.clear()
            self._timestamps.clear()
            self._logger.info(f"Cache {self.name} cleared")
    
    def items(self) -> Iterator[Tuple[str, Any]]:
        """Retourne un itérateur sur les paires (clé, valeur) non expirées."""
        with self._lock:
            self._cleanup_expired()
            return iter(list(self._cache.items()))

    def keys(self) -> Iterator[str]:
        """Retourne un itérateur sur les clés non expirées."""
        with self._lock:
            self._cleanup_expired()
            return iter(list(self._cache.keys()))

    def values(self) -> Iterator[Any]:
        """Retourne un itérateur sur les valeurs non expirées."""
        with self._lock:
            self._cleanup_expired()
            return iter(list(self._cache.values()))

    def __getitem__(self, key: str) -> Any:
        """Permet l'accès par index : cache[key]"""
        with self._lock:
            result = self.get(key)
            if result is None and key not in self._cache:
                raise KeyError(key)
            return result

    def __contains__(self, key: str) -> bool:
        """Permet l'opérateur 'in' : key in cache"""
        with self._lock:
            self._cleanup_expired()
            return key in self._cache and not self._is_expired(key)

    def __len__(self) -> int:
        with self._lock:
            self._cleanup_expired()
            return len(self._cache)

    def __iter__(self) -> Iterator[str]:
        """Permet d'itérer sur les clés : for key in cache"""
        with self._lock:
            self._cleanup_expired()
            return iter(list(self._cache.keys()))


# Instance globale du gestionnaire de mémoire
_memory_manager: Optional[MemoryManager] = None


def get_memory_manager() -> MemoryManager:
    """Retourne l'instance globale du gestionnaire de mémoire (thread-safe)."""
    global _memory_manager
    if _memory_manager is None:
        with _memory_manager_lock:
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
    
    return cleaned
