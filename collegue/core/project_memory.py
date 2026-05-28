"""
ProjectMemory — Mémoire persistante inter-sessions pour les experts IA.

Stocke et rappelle les patterns, problèmes et décisions du projet
entre les sessions MCP. Chaque expert peut enrichir ses analyses
avec le contexte historique mémorisé.
"""

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class MemoryEntry:
    """Une entrée dans la mémoire du projet."""

    expert: str
    entry_type: str  # pattern_learned, issue_found, fix_applied, project_profile
    category: str
    title: str
    data: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    score: float = 0.0
    file_path: Optional[str] = None
    language: Optional[str] = None
    ttl_seconds: Optional[float] = None  # None = permanent

    def is_expired(self) -> bool:
        if self.ttl_seconds is None:
            return False
        return (time.time() - self.timestamp) > self.ttl_seconds

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MemoryEntry":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# TTL par défaut par type d'entrée
DEFAULT_TTL = {
    "pattern_learned": 30 * 86400,  # 30 jours
    "issue_found": 7 * 86400,  # 7 jours
    "fix_applied": 14 * 86400,  # 14 jours
    "project_profile": None,  # permanent
    "expert_result": 3 * 86400,  # 3 jours
}

MAX_ENTRIES_PER_EXPERT = 200
MAX_TOTAL_ENTRIES = 2000


class ProjectMemory:
    """Mémoire persistante du projet.

    Stocke les entrées sur disque dans un répertoire `.collegue/memory/`.
    Thread-safe avec filtrage par expert/type/catégorie/langage.
    """

    def __init__(self, memory_dir: Optional[str] = None, max_total: int = MAX_TOTAL_ENTRIES):
        self._memory_dir = Path(memory_dir or ".collegue/memory")
        self._max_total = max_total
        self._entries: List[MemoryEntry] = []
        self._lock = threading.RLock()
        self._dirty = False
        self._load()

    @property
    def memory_dir(self) -> Path:
        return self._memory_dir

    def _ensure_dir(self) -> None:
        self._memory_dir.mkdir(parents=True, exist_ok=True)

    def _storage_path(self) -> Path:
        return self._memory_dir / "project_memory.json"

    def _load(self) -> None:
        path = self._storage_path()
        if not path.exists():
            return
        try:
            with open(path) as f:
                raw = json.load(f)
            self._entries = [MemoryEntry.from_dict(e) for e in raw if isinstance(e, dict)]
            logger.info("ProjectMemory: chargé %d entrées depuis %s", len(self._entries), path)
        except Exception as exc:
            logger.warning("ProjectMemory: erreur de chargement: %s", exc)
            self._entries = []

    def save(self) -> None:
        """Persiste la mémoire sur disque (écriture atomique via fichier temporaire)."""
        with self._lock:
            if not self._dirty:
                return
            self._ensure_dir()
            path = self._storage_path()
            try:
                fd, tmp_path = tempfile.mkstemp(dir=str(self._memory_dir), suffix=".tmp", prefix="memory_")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump([e.to_dict() for e in self._entries], f, indent=2, default=str)
                    os.replace(tmp_path, str(path))
                except BaseException:
                    os.unlink(tmp_path)
                    raise
                self._dirty = False
                logger.debug("ProjectMemory: sauvegardé %d entrées vers %s", len(self._entries), path)
            except Exception as exc:
                logger.error("ProjectMemory: erreur de sauvegarde: %s", exc)

    def store(
        self,
        expert: str,
        entry_type: str,
        category: str,
        title: str,
        data: Dict[str, Any],
        score: float = 0.0,
        file_path: Optional[str] = None,
        language: Optional[str] = None,
        ttl_seconds: Optional[float] = None,
        auto_save: bool = True,
    ) -> MemoryEntry:
        """Ajoute une entrée dans la mémoire.

        Args:
            auto_save: Si True (défaut), persiste immédiatement sur disque.
                Passer False pour du batch (appeler save() manuellement après).
        """
        if ttl_seconds is None:
            ttl_seconds = DEFAULT_TTL.get(entry_type)

        entry = MemoryEntry(
            expert=expert,
            entry_type=entry_type,
            category=category,
            title=title,
            data=data,
            score=score,
            file_path=file_path,
            language=language,
            ttl_seconds=ttl_seconds,
        )

        with self._lock:
            self._entries.append(entry)
            self._dirty = True

            # Pruning si nécessaire (global ou per-expert)
            expert_count = sum(1 for e in self._entries if e.expert == expert)
            if len(self._entries) > self._max_total or expert_count > MAX_ENTRIES_PER_EXPERT:
                self._prune_locked()

        if auto_save:
            self.save()

        # Activity log: memory write
        try:
            from collegue.monitoring.activity_log import get_activity_log

            get_activity_log().log_memory_write(
                expert=expert,
                entry_type=entry_type,
                category=category,
                title=title,
                score=score,
            )
        except Exception:
            pass

        return entry

    def recall(
        self,
        expert: Optional[str] = None,
        entry_type: Optional[str] = None,
        category: Optional[str] = None,
        file_path: Optional[str] = None,
        language: Optional[str] = None,
        limit: int = 20,
    ) -> List[MemoryEntry]:
        """Recherche des entrées dans la mémoire."""
        with self._lock:
            results = []
            for entry in self._entries:
                if entry.is_expired():
                    continue
                if expert and entry.expert != expert:
                    continue
                if entry_type and entry.entry_type != entry_type:
                    continue
                if category and entry.category != category:
                    continue
                if file_path and entry.file_path != file_path:
                    continue
                if language and entry.language != language:
                    continue
                results.append(entry)

            # Trier par score décroissant, puis par timestamp décroissant
            results.sort(key=lambda e: (e.score, e.timestamp), reverse=True)
            return results[:limit]

    def search(self, query: str, limit: int = 10) -> List[MemoryEntry]:
        """Recherche textuelle dans les titres et données."""
        query_lower = query.lower()
        with self._lock:
            scored = []
            for entry in self._entries:
                if entry.is_expired():
                    continue
                relevance = 0.0
                if query_lower in entry.title.lower():
                    relevance += 1.0
                if query_lower in entry.category.lower():
                    relevance += 0.5
                if query_lower in entry.expert.lower():
                    relevance += 0.3
                data_str = json.dumps(entry.data, default=str).lower()
                if query_lower in data_str:
                    relevance += 0.2
                if relevance > 0:
                    scored.append((relevance, entry))

            scored.sort(key=lambda x: (x[0], x[1].timestamp), reverse=True)
            return [e for _, e in scored[:limit]]

    def get_context_for(self, expert: str, language: Optional[str] = None) -> Dict[str, Any]:
        """Construit un contexte mémoire pour un expert donné.

        Retourne un dict prêt à être injecté dans le prompt LLM.
        Les entrées pattern_learned, issue_found et fix_applied sont partagées
        entre tous les experts (pas de filtre expert) car la délégation
        inter-experts exige que chaque expert ait accès aux découvertes des
        autres. Seul expert_result reste spécifique à l'expert.
        """
        # Ne PAS filtrer par expert pour les types cross-expert : un code_refactoring
        # doit voir les issue_found d'un code_review pour savoir quoi corriger.
        patterns_entries = self.recall(entry_type="pattern_learned", language=language, limit=5)
        issues_entries = self.recall(entry_type="issue_found", language=language, limit=5)
        fixes_entries = self.recall(entry_type="fix_applied", language=language, limit=3)
        profile_entries = self.recall(entry_type="project_profile", language=language, limit=3)

        entries = patterns_entries + issues_entries + fixes_entries + profile_entries
        if not entries:
            return {}

        patterns = []
        known_issues = []
        past_fixes = []
        project_info = {}

        for entry in entries:
            if entry.entry_type == "pattern_learned":
                patterns.append(entry.title)
            elif entry.entry_type == "issue_found":
                known_issues.append({"title": entry.title, "category": entry.category})
            elif entry.entry_type == "fix_applied":
                past_fixes.append(entry.title)
            elif entry.entry_type == "project_profile":
                project_info.update(entry.data)

        context: Dict[str, Any] = {}
        if patterns:
            context["known_patterns"] = patterns
        if known_issues:
            context["known_issues"] = known_issues
        if past_fixes:
            context["past_fixes"] = past_fixes
        if project_info:
            context["project_profile"] = project_info

        return context

    def get_project_profile(self) -> Dict[str, Any]:
        """Retourne le profil agrégé du projet."""
        entries = self.recall(entry_type="project_profile", limit=50)
        profile: Dict[str, Any] = {}
        for entry in entries:
            profile.update(entry.data)
        return profile

    def prune(self) -> int:
        """Supprime les entrées expirées et limite la taille."""
        with self._lock:
            return self._prune_locked()

    def _prune_locked(self) -> int:
        before = len(self._entries)
        # Supprimer les expirées
        self._entries = [e for e in self._entries if not e.is_expired()]

        # Limiter par expert
        expert_counts: Dict[str, int] = {}
        kept = []
        # Trier par timestamp décroissant pour garder les plus récentes
        sorted_entries = sorted(self._entries, key=lambda e: e.timestamp, reverse=True)
        for entry in sorted_entries:
            count = expert_counts.get(entry.expert, 0)
            if count < MAX_ENTRIES_PER_EXPERT:
                kept.append(entry)
                expert_counts[entry.expert] = count + 1

        # Limiter au total
        if len(kept) > self._max_total:
            kept = kept[: self._max_total]

        self._entries = kept
        pruned = before - len(self._entries)
        if pruned > 0:
            self._dirty = True
            logger.info("ProjectMemory: prunées %d entrées (%d restantes)", pruned, len(self._entries))
        return pruned

    def export_stats(self) -> Dict[str, Any]:
        """Statistiques de la mémoire."""
        with self._lock:
            by_expert: Dict[str, int] = {}
            by_type: Dict[str, int] = {}
            for entry in self._entries:
                if not entry.is_expired():
                    by_expert[entry.expert] = by_expert.get(entry.expert, 0) + 1
                    by_type[entry.entry_type] = by_type.get(entry.entry_type, 0) + 1

            return {
                "total_entries": len(self._entries),
                "active_entries": sum(1 for e in self._entries if not e.is_expired()),
                "by_expert": by_expert,
                "by_type": by_type,
                "storage_path": str(self._storage_path()),
            }

    def clear(self) -> None:
        """Vide la mémoire."""
        with self._lock:
            self._entries = []
            self._dirty = True
        self.save()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# Singleton global
_project_memory: Optional[ProjectMemory] = None
_pm_lock = threading.Lock()


def get_project_memory(memory_dir: Optional[str] = None) -> ProjectMemory:
    """Retourne le singleton ProjectMemory."""
    global _project_memory
    with _pm_lock:
        if _project_memory is None:
            _project_memory = ProjectMemory(memory_dir=memory_dir)
        elif memory_dir is not None and str(_project_memory.memory_dir) != str(Path(memory_dir)):
            logger.warning(
                "ProjectMemory: memory_dir demandé '%s' ignoré, singleton déjà initialisé avec '%s'",
                memory_dir,
                _project_memory.memory_dir,
            )
        return _project_memory


def reset_project_memory() -> None:
    """Réinitialise le singleton (pour les tests)."""
    global _project_memory
    with _pm_lock:
        _project_memory = None
