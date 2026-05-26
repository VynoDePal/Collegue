"""
ProactiveMonitor — Détection proactive de problèmes sur le repo.

Surveille les changements de fichiers et déclenche automatiquement
les experts pertinents. Fonctionne en mode background comme le Watchdog.
"""

import asyncio
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class FileChange:
    """Un fichier modifié détecté."""

    path: str
    change_type: str  # added, modified, deleted, renamed
    language: Optional[str] = None
    lines_changed: int = 0


@dataclass
class TriggerDecision:
    """Décision de déclenchement d'un expert."""

    expert: str
    reason: str
    priority: int = 5
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MonitorConfig:
    """Configuration du moniteur proactif."""

    scan_interval: float = 300.0  # 5 minutes
    enabled_experts: Set[str] = field(
        default_factory=lambda: {
            "code_review",
            "architecture_analysis",
            "performance_analysis",
            "iac_guardrails_scan",
            "test_generation",
            "impact_analysis",
            "code_documentation",
        }
    )
    min_changes_to_trigger: int = 1
    max_files_per_scan: int = 50
    repo_path: Optional[str] = None


# Mapping extensions → langages
LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".rb": "ruby",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".cs": "csharp",
    ".php": "php",
}

# Mapping patterns de fichiers → experts à déclencher
TRIGGER_RULES = [
    {
        "pattern": r"\.(py|js|ts|tsx|jsx|rb|go|rs|java|kt|swift|cs|php)$",
        "experts": ["code_review", "performance_analysis"],
        "reason": "fichier source modifié",
        "priority": 5,
    },
    {
        "pattern": r"(Dockerfile|docker-compose|\.ya?ml|\.hcl|\.tf)$",
        "experts": ["iac_guardrails_scan"],
        "reason": "fichier IaC/infrastructure modifié",
        "priority": 8,
    },
    {
        "pattern": r"(requirements\.txt|Pipfile|pyproject\.toml|package\.json|Cargo\.toml|go\.mod)",
        "experts": ["architecture_analysis"],
        "reason": "dépendances modifiées",
        "priority": 7,
    },
    {
        "pattern": r"test[_/]|_test\.|\.test\.|spec[_/]|_spec\.",
        "experts": [],  # les tests ne déclenchent rien par défaut
        "reason": "fichier de test modifié",
        "priority": 2,
    },
    {
        "pattern": r"(README|CHANGELOG|docs/|\.md$)",
        "experts": [],
        "reason": "documentation modifiée",
        "priority": 1,
    },
]


class ChangeDetector:
    """Détecte les fichiers modifiés dans un repo git."""

    def __init__(self, repo_path: str):
        self._repo_path = repo_path
        self._last_commit: Optional[str] = None

    def detect_changes(self, since_commit: Optional[str] = None) -> List[FileChange]:
        """Détecte les changements depuis le dernier commit scanné."""
        try:
            ref = since_commit or self._last_commit or "HEAD~1"
            result = subprocess.run(
                ["git", "diff", "--name-status", ref, "HEAD"],
                capture_output=True,
                text=True,
                cwd=self._repo_path,
                timeout=30,
            )
            if result.returncode != 0:
                logger.warning("git diff failed: %s", result.stderr)
                return []

            changes = []
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                status = parts[0][0]
                filepath = parts[-1]

                change_type_map = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed"}
                change_type = change_type_map.get(status, "modified")

                ext = Path(filepath).suffix.lower()
                language = LANGUAGE_MAP.get(ext)

                changes.append(
                    FileChange(
                        path=filepath,
                        change_type=change_type,
                        language=language,
                    )
                )

            # Mettre à jour le dernier commit scanné
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=self._repo_path,
                timeout=10,
            )
            if head.returncode == 0:
                self._last_commit = head.stdout.strip()

            return changes

        except Exception as exc:
            logger.error("Erreur détection changements: %s", exc)
            return []

    def detect_uncommitted(self) -> List[FileChange]:
        """Détecte les fichiers non commités (working dir + staging)."""
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                cwd=self._repo_path,
                timeout=30,
            )
            if result.returncode != 0:
                return []

            changes = []
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                status = line[:2].strip()
                filepath = line[3:].strip()

                # Handle renamed files: "old -> new" format
                if " -> " in filepath:
                    filepath = filepath.split(" -> ")[-1]

                change_type = "modified"
                if "A" in status or "?" in status:
                    change_type = "added"
                elif "D" in status:
                    change_type = "deleted"
                elif "R" in status:
                    change_type = "renamed"

                ext = Path(filepath).suffix.lower()
                language = LANGUAGE_MAP.get(ext)

                changes.append(
                    FileChange(
                        path=filepath,
                        change_type=change_type,
                        language=language,
                    )
                )
            return changes

        except Exception as exc:
            logger.error("Erreur détection uncommitted: %s", exc)
            return []


class ExpertTriggerer:
    """Décide quels experts déclencher selon les fichiers modifiés."""

    def __init__(self, config: MonitorConfig):
        self._config = config

    def decide_triggers(self, changes: List[FileChange]) -> List[TriggerDecision]:
        """Détermine quels experts déclencher pour un ensemble de changements."""
        if len(changes) < self._config.min_changes_to_trigger:
            return []

        triggered: Dict[str, TriggerDecision] = {}

        for change in changes:
            for rule in TRIGGER_RULES:
                if re.search(rule["pattern"], change.path, re.IGNORECASE):
                    for expert in rule["experts"]:
                        if expert not in self._config.enabled_experts:
                            continue
                        if expert not in triggered:
                            triggered[expert] = TriggerDecision(
                                expert=expert,
                                reason=rule["reason"],
                                priority=rule["priority"],
                                params={"files": [], "languages": set()},
                            )
                        triggered[expert].params["files"].append(change.path)
                        if change.language:
                            triggered[expert].params["languages"].add(change.language)

        # Convertir les sets en lists pour la sérialisation
        decisions = []
        for decision in triggered.values():
            decision.params["languages"] = list(decision.params.get("languages", set()))
            decision.params["files"] = decision.params.get("files", [])[: self._config.max_files_per_scan]
            decisions.append(decision)

        decisions.sort(key=lambda d: d.priority, reverse=True)
        return decisions

    def should_trigger_architecture(self, changes: List[FileChange]) -> bool:
        """Vérifie si une analyse architecturale est nécessaire."""
        dep_files = {"requirements.txt", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "Pipfile"}
        return any(Path(c.path).name in dep_files for c in changes)

    def should_trigger_tests(self, changes: List[FileChange]) -> bool:
        """Vérifie si des tests doivent être générés."""
        source_files = [c for c in changes if c.language and c.change_type == "added"]
        test_patterns = re.compile(r"test[_/]|_test\.|\.test\.|spec[_/]|_spec\.")
        source_without_tests = [c for c in source_files if not test_patterns.search(c.path)]
        return len(source_without_tests) > 0


@dataclass
class MonitorResult:
    """Résultat d'un cycle de surveillance."""

    scan_time: float
    changes_detected: int
    triggers_decided: int
    decisions: List[TriggerDecision]
    errors: List[str] = field(default_factory=list)


class ProactiveMonitor:
    """Moniteur proactif qui surveille le repo et déclenche les experts."""

    def __init__(self, config: Optional[MonitorConfig] = None):
        self._config = config or MonitorConfig()
        self._detector: Optional[ChangeDetector] = None
        self._triggerer = ExpertTriggerer(self._config)
        self._running = False
        self._scan_history: List[MonitorResult] = []
        self._max_history = 100

    @property
    def config(self) -> MonitorConfig:
        return self._config

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        """Marque le moniteur comme actif."""
        self._running = True

    def stop(self) -> None:
        """Marque le moniteur comme inactif."""
        self._running = False

    def set_repo_path(self, path: str) -> None:
        self._config.repo_path = path
        self._detector = ChangeDetector(path)

    def scan_once(self) -> MonitorResult:
        """Effectue un scan unique et retourne les décisions."""
        start = time.time()
        errors: List[str] = []

        if not self._detector:
            if self._config.repo_path:
                self._detector = ChangeDetector(self._config.repo_path)
            else:
                return MonitorResult(
                    scan_time=0.0,
                    changes_detected=0,
                    triggers_decided=0,
                    decisions=[],
                    errors=["Pas de repo_path configuré"],
                )

        changes = self._detector.detect_changes()
        if not changes:
            changes = self._detector.detect_uncommitted()

        decisions = self._triggerer.decide_triggers(changes)

        result = MonitorResult(
            scan_time=time.time() - start,
            changes_detected=len(changes),
            triggers_decided=len(decisions),
            decisions=decisions,
            errors=errors,
        )

        self._scan_history.append(result)
        if len(self._scan_history) > self._max_history:
            self._scan_history = self._scan_history[-self._max_history :]

        return result

    async def scan_once_async(self) -> MonitorResult:
        """Version asynchrone de scan_once."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.scan_once)

    def get_scan_history(self) -> List[MonitorResult]:
        return list(self._scan_history)

    def get_stats(self) -> Dict[str, Any]:
        """Statistiques du moniteur."""
        return {
            "is_running": self._running,
            "total_scans": len(self._scan_history),
            "total_changes_detected": sum(r.changes_detected for r in self._scan_history),
            "total_triggers": sum(r.triggers_decided for r in self._scan_history),
            "config": {
                "scan_interval": self._config.scan_interval,
                "enabled_experts": sorted(self._config.enabled_experts),
                "min_changes_to_trigger": self._config.min_changes_to_trigger,
            },
        }


# Singleton global
_proactive_monitor: Optional[ProactiveMonitor] = None
_pm_lock = threading.Lock()


def get_proactive_monitor(config: Optional[MonitorConfig] = None) -> ProactiveMonitor:
    """Retourne le singleton ProactiveMonitor."""
    global _proactive_monitor
    with _pm_lock:
        if _proactive_monitor is None:
            _proactive_monitor = ProactiveMonitor(config=config)
        return _proactive_monitor


def reset_proactive_monitor() -> None:
    """Réinitialise le singleton (pour les tests)."""
    global _proactive_monitor
    with _pm_lock:
        _proactive_monitor = None
