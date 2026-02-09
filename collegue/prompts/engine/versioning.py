"""
Système de versioning des prompts avec gestion des versions et performances
"""
import json
import os
from datetime import datetime
from typing import Dict, List, Optional, Any
from pathlib import Path
import uuid
from dataclasses import dataclass, asdict
import logging

logger = logging.getLogger(__name__)


@dataclass
class PromptVersion:
    """Représente une version d'un template de prompt."""
    id: str
    template_id: str
    version: str
    content: str
    variables: List[Dict[str, Any]]
    created_at: str
    updated_at: str
    performance_score: float = 0.0
    is_active: bool = False
    usage_count: int = 0
    success_rate: float = 0.0
    average_tokens: int = 0
    average_generation_time: float = 0.0
    metadata: Dict[str, Any] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convertit en dictionnaire."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PromptVersion':
        """Crée une instance depuis un dictionnaire."""
        return cls(**data)


class PromptVersionManager:
    """Gestionnaire de versions des prompts avec tracking de performance."""

    def __init__(self, storage_path: str = None):
        """Initialise le gestionnaire de versions."""
        self.storage_path = storage_path or os.path.join(
            os.path.dirname(__file__), '..', 'versions'
        )
        Path(self.storage_path).mkdir(parents=True, exist_ok=True)
        self.versions_file = os.path.join(self.storage_path, 'versions.json')
        self.versions_cache: Dict[str, List[PromptVersion]] = {}
        self._load_versions()

    def _load_versions(self) -> None:
        """Charge les versions depuis le stockage."""
        if os.path.exists(self.versions_file):
            try:
                with open(self.versions_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for template_id, versions in data.items():
                        self.versions_cache[template_id] = [
                            PromptVersion.from_dict(v) for v in versions
                        ]
            except Exception as e:
                logger.error(f"Erreur lors du chargement des versions: {e}")
                self.versions_cache = {}

    def _save_versions(self) -> None:
        """Sauvegarde les versions dans le stockage."""
        try:
            data = {}
            for template_id, versions in self.versions_cache.items():
                data[template_id] = [v.to_dict() for v in versions]

            with open(self.versions_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Erreur lors de la sauvegarde des versions: {e}")

    def create_version(self, template_id: str, content: str,
                      variables: List[Dict[str, Any]], version: str = None) -> PromptVersion:
        """Crée une nouvelle version d'un template."""
        if version is None:
            version = self._get_next_version(template_id)

        now = datetime.now().isoformat()
        prompt_version = PromptVersion(
            id=str(uuid.uuid4()),
            template_id=template_id,
            version=version,
            content=content,
            variables=variables,
            created_at=now,
            updated_at=now,
            metadata={}
        )

        if template_id not in self.versions_cache:
            self.versions_cache[template_id] = []

        self.versions_cache[template_id].append(prompt_version)
        self._save_versions()
        return prompt_version

    def get_best_version(self, template_id: str) -> Optional[PromptVersion]:
        """Récupère la version avec le meilleur score de performance."""
        versions = self.versions_cache.get(template_id, [])
        if not versions:
            return None

        experienced = [v for v in versions if v.usage_count >= 10]
        if experienced:
            return max(experienced, key=lambda v: v.performance_score)

        for v in versions:
            if v.is_active:
                return v
        return versions[-1] if versions else None

    def _get_next_version(self, template_id: str) -> str:
        """Génère le prochain numéro de version."""
        versions = self.versions_cache.get(template_id, [])
        if not versions:
            return "1.0.0"

        latest = max(versions, key=lambda v: v.created_at)
        parts = latest.version.split('.')
        parts[-1] = str(int(parts[-1]) + 1)
        return '.'.join(parts)

    def get_version(self, template_id: str, version: str) -> Optional[PromptVersion]:
        """Récupère une version spécifique d'un template.

        Args:
            template_id: ID du template
            version: Numéro de version (ex: "1.0.0", "v2", "experimental")

        Returns:
            PromptVersion ou None si non trouvée
        """
        versions = self.versions_cache.get(template_id, [])

        for v in versions:
            if v.version == version:
                return v

        if version in ['default', 'v2', 'experimental', 'python']:
            return self._create_virtual_version(template_id, version)

        return None

    def _create_virtual_version(self, template_id: str, version_name: str) -> PromptVersion:
        """Crée une version virtuelle pour les templates YAML non versionnés.

        Args:
            template_id: ID du template
            version_name: Nom de la version (default, v2, experimental, etc.)

        Returns:
            PromptVersion virtuelle
        """
        now = datetime.now().isoformat()
        return PromptVersion(
            id=f"{template_id}_{version_name}",
            template_id=template_id,
            version="1.0.0",
            content="",
            variables=[],
            created_at=now,
            updated_at=now,
            is_active=True,
            metadata={"source": "yaml", "name": version_name}
        )

    def get_all_versions(self, template_id: str) -> List[PromptVersion]:
        """Récupère toutes les versions d'un template.

        Args:
            template_id: ID du template

        Returns:
            Liste des versions disponibles
        """
        return self.versions_cache.get(template_id, [])

    def update_metrics(self, template_id: str, version: str, metrics: Dict[str, Any]) -> None:
        """Met à jour les métriques de performance d'une version.

        Args:
            template_id: ID du template
            version: Numéro de version
            metrics: Dictionnaire des métriques à mettre à jour
        """
        versions = self.versions_cache.get(template_id, [])

        for v in versions:
            if v.version == version:
                if 'success_rate' in metrics:
                    v.success_rate = metrics['success_rate']
                if 'avg_execution_time' in metrics:
                    v.average_generation_time = metrics['avg_execution_time']
                if 'avg_tokens' in metrics:
                    v.average_tokens = metrics['avg_tokens']
                if 'executions' in metrics:
                    v.usage_count = metrics['executions']
                if 'performance_score' in metrics:
                    v.performance_score = metrics['performance_score']

                if 'performance_score' not in metrics:
                    v.performance_score = self._calculate_performance_score(v)

                v.updated_at = datetime.now().isoformat()
                self._save_versions()
                return

        if version in ['default', 'v2', 'experimental', 'python']:
            prompt_version = self._create_virtual_version(template_id, version)
            self.update_metrics_for_version(prompt_version, metrics)

            if template_id not in self.versions_cache:
                self.versions_cache[template_id] = []
            self.versions_cache[template_id].append(prompt_version)
            self._save_versions()

    def update_metrics_for_version(self, version: PromptVersion, metrics: Dict[str, Any]) -> None:
        """Met à jour les métriques directement sur un objet PromptVersion.

        Args:
            version: Objet PromptVersion à mettre à jour
            metrics: Dictionnaire des métriques
        """
        if 'success_rate' in metrics:
            version.success_rate = metrics['success_rate']
        if 'avg_execution_time' in metrics:
            version.average_generation_time = metrics['avg_execution_time']
        if 'avg_tokens' in metrics:
            version.average_tokens = metrics['avg_tokens']
        if 'executions' in metrics:
            version.usage_count = metrics['executions']
        if 'performance_score' in metrics:
            version.performance_score = metrics['performance_score']
        else:
            version.performance_score = self._calculate_performance_score(version)

    def _calculate_performance_score(self, version: PromptVersion) -> float:
        """Calcule le score de performance d'une version.

        Args:
            version: Version dont on calcule le score

        Returns:
            Score de performance (0.0 à 1.0)
        """

        score = 0.0


        score += version.success_rate * 0.4


        if version.average_generation_time > 0:
            speed_score = max(0, 1.0 - (version.average_generation_time / 5.0))
            score += speed_score * 0.3


        if version.average_tokens > 0:
            token_efficiency = max(0, 1.0 - (version.average_tokens / 2000))
            score += token_efficiency * 0.3

        return min(1.0, max(0.0, score))

    def update_performance_metrics(self, template_id: str, version: str,
                                  success: bool, execution_time: float,
                                  tokens_used: int, user_satisfaction: float = None) -> None:
        """Met à jour les métriques de performance après une exécution.

        Args:
            template_id: ID du template
            version: Numéro de version
            success: Si l'exécution était réussie
            execution_time: Temps d'exécution en secondes
            tokens_used: Nombre de tokens utilisés
            user_satisfaction: Score de satisfaction utilisateur (optionnel)
        """
        versions = self.versions_cache.get(template_id, [])

        for v in versions:
            if v.version == version:
                v.usage_count += 1

                success_value = 1.0 if success else 0.0
                v.success_rate = ((v.success_rate * (v.usage_count - 1)) + success_value) / v.usage_count

                v.average_generation_time = ((v.average_generation_time * (v.usage_count - 1)) + execution_time) / v.usage_count

                v.average_tokens = int(((v.average_tokens * (v.usage_count - 1)) + tokens_used) / v.usage_count)

                v.performance_score = self._calculate_performance_score(v)

                if user_satisfaction is not None and v.metadata is not None:
                    if 'user_satisfaction_scores' not in v.metadata:
                        v.metadata['user_satisfaction_scores'] = []
                    v.metadata['user_satisfaction_scores'].append(user_satisfaction)

                    v.metadata['avg_user_satisfaction'] = sum(v.metadata['user_satisfaction_scores']) / len(v.metadata['user_satisfaction_scores'])

                v.updated_at = datetime.now().isoformat()
                self._save_versions()
                return

        logger.warning(f"Version {version} non trouvée pour {template_id}, création automatique")
        prompt_version = self._create_virtual_version(template_id, version)
        prompt_version.usage_count = 1
        prompt_version.success_rate = 1.0 if success else 0.0
        prompt_version.average_generation_time = execution_time
        prompt_version.average_tokens = tokens_used
        prompt_version.performance_score = self._calculate_performance_score(prompt_version)

        if template_id not in self.versions_cache:
            self.versions_cache[template_id] = []
        self.versions_cache[template_id].append(prompt_version)
        self._save_versions()
