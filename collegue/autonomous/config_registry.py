"""
Registre de configurations utilisateurs pour le watchdog multi-utilisateur.

Ce module permet de stocker les configurations des utilisateurs (tokens, org)
lorsqu'ils font des requêtes MCP, afin que le watchdog puisse les utiliser
pour scanner tous les projets de tous les utilisateurs.
"""
import hashlib
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, List, Optional


@dataclass
class UserConfig:
    sentry_org: str
    sentry_token: Optional[str] = None
    github_token: Optional[str] = None
    github_owner: Optional[str] = None
    github_repo: Optional[str] = None
    last_seen: float = field(default_factory=time.time)

    def update_last_seen(self):
        self.last_seen = time.time()

    @property
    def config_id(self) -> str:
        return hashlib.sha256(self.sentry_org.encode()).hexdigest()[:16]


class UserConfigRegistry:
    _instance: Optional["UserConfigRegistry"] = None
    _lock = Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._configs: Dict[str, UserConfig] = {}
                    cls._instance._config_lock = Lock()
        return cls._instance

    PLACEHOLDER_ORGS = {
        "your-org", "my-organization", "your-organization",
        "my-org", "example-org", "test-org", "placeholder"
    }

    def register(
        self,
        sentry_org: str,
        sentry_token: Optional[str] = None,
        github_token: Optional[str] = None,
        github_owner: Optional[str] = None,
        github_repo: Optional[str] = None
    ) -> Optional[str]:

        if sentry_org.lower() in self.PLACEHOLDER_ORGS:
            return None


        normalized_org = sentry_org.lower()

        config = UserConfig(
            sentry_org=normalized_org,
            sentry_token=sentry_token,
            github_token=github_token,
            github_owner=github_owner,
            github_repo=github_repo
        )

        with self._config_lock:
            existing = self._configs.get(config.config_id)
            if existing:

                if sentry_token:
                    existing.sentry_token = sentry_token
                if github_token:
                    existing.github_token = github_token
                if github_owner:
                    existing.github_owner = github_owner
                if github_repo:
                    existing.github_repo = github_repo
                existing.update_last_seen()
            else:
                self._configs[config.config_id] = config

        return config.config_id

    def get_all_active(self, max_age_hours: float = 24.0) -> List[UserConfig]:
        cutoff = time.time() - (max_age_hours * 3600)

        with self._config_lock:
            return [
                config for config in self._configs.values()
                if config.last_seen >= cutoff
            ]

    def get_config(self, config_id: str) -> Optional[UserConfig]:
        with self._config_lock:
            return self._configs.get(config_id)

    def cleanup_stale(self, max_age_hours: float = 24.0) -> int:
        cutoff = time.time() - (max_age_hours * 3600)
        removed = 0

        with self._config_lock:
            stale_ids = [
                cid for cid, config in self._configs.items()
                if config.last_seen < cutoff
            ]
            for cid in stale_ids:
                del self._configs[cid]
                removed += 1

        return removed

    def count(self) -> int:
        with self._config_lock:
            return len(self._configs)

    def clear_all(self) -> int:
        with self._config_lock:
            count = len(self._configs)
            self._configs.clear()
            return count

    def remove_by_org(self, sentry_org: str) -> bool:
        org_lower = sentry_org.lower()
        with self._config_lock:
            to_remove = [
                cid for cid, config in self._configs.items()
                if config.sentry_org.lower() == org_lower
            ]
            for cid in to_remove:
                del self._configs[cid]
            return len(to_remove) > 0


def get_config_registry() -> UserConfigRegistry:
    return UserConfigRegistry()
