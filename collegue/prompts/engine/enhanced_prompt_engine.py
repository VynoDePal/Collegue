"""
Enhanced Prompt Engine avec versioning, optimisation et tracking de performance
"""
import os
import json
import logging
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path
import random

from .prompt_engine import PromptEngine, PromptTemplate
from .versioning import PromptVersionManager, PromptVersion
from .optimizer import LanguageOptimizer

logger = logging.getLogger(__name__)


class EnhancedPromptEngine(PromptEngine):
    """Extension du PromptEngine avec versioning, optimisation et A/B testing."""
    
    def __init__(self, templates_dir: str = None, storage_dir: str = None):
        """Initialise le moteur de prompts amélioré."""
        # Initialiser le moteur de base avec le storage_path
        super().__init__(storage_path=storage_dir)
        
        # Initialiser les composants
        self.version_manager = PromptVersionManager(storage_dir)
        self.language_optimizer = LanguageOptimizer()
        
        # Configuration A/B testing
        self.ab_testing_enabled = True
        self.exploration_rate = 0.1  # 10% exploration, 90% exploitation
        
        # Cache des performances
        self.performance_cache: Dict[str, Dict[str, float]] = {}
        
        # Dictionnaire des templates chargés (pour compatibilité avec les tests)
        self.templates: Dict[str, PromptTemplate] = {}
        
        # Charger les templates YAML pour les outils
        self.tool_templates_dir = os.path.join(
            os.path.dirname(__file__), '..', 'templates', 'tools'
        )
        self._load_tool_templates()
    
    def _load_tool_templates(self) -> None:
        """Charge les templates YAML spécifiques aux outils."""
        if not os.path.exists(self.tool_templates_dir):
            Path(self.tool_templates_dir).mkdir(parents=True, exist_ok=True)
            return
        
        # Parcourir les répertoires d'outils
        for tool_dir in Path(self.tool_templates_dir).iterdir():
            if tool_dir.is_dir():
                tool_name = tool_dir.name
                
                # Charger chaque template YAML
                for yaml_file in tool_dir.glob("*.yaml"):
                    try:
                        import yaml
                        with open(yaml_file, 'r', encoding='utf-8') as f:
                            template_data = yaml.safe_load(f)
                            
                        # Créer le template
                        template = self.create_template({
                            "name": template_data.get("name"),
                            "description": template_data.get("description", ""),
                            "template": template_data.get("template"),
                            "variables": template_data.get("variables", []),
                            "category": f"tool/{tool_name}",
                            "tags": template_data.get("tags", []),
                            "provider_specific": template_data.get("provider_specific", {})
                        })
                        
                        # Ajouter au dictionnaire des templates
                        template_key = f"{tool_name}_{yaml_file.stem}"
                        self.templates[template_key] = template
                        
                        # Créer une version
                        self.version_manager.create_version(
                            template_id=template.id,
                            content=template_data.get("template"),
                            variables=template_data.get("variables", []),
                            version=template_data.get("version", "1.0.0")
                        )
                        
                        logger.info(f"Template {template.name} chargé depuis {yaml_file}")
                        
                    except Exception as e:
                        logger.error(f"Erreur lors du chargement de {yaml_file}: {e}")
    
    async def get_optimized_prompt(
        self,
        tool_name: str,
        context: Dict[str, Any],
        language: Optional[str] = None,
        version: Optional[str] = None
    ) -> Tuple[str, str]:
        """
        Récupère un prompt optimisé pour un outil.
        
        Args:
            tool_name: Nom de l'outil
            context: Contexte avec les variables
            language: Langage de programmation
            version: Version spécifique (ou auto-sélection)
            
        Returns:
            Tuple (prompt formaté, version utilisée)
        """
        # Trouver le template pour l'outil
        category = f"tool/{tool_name.lower()}"
        templates = self.get_templates_by_category(category)
        
        if not templates:
            # Fallback vers le template par défaut
            default_template = self._get_default_template(tool_name)
            if default_template:
                prompt = self.format_prompt(default_template.id, context)
                return prompt, "default"
            raise ValueError(f"Aucun template trouvé pour l'outil {tool_name}")
        
        # Sélectionner le template approprié
        template = templates[0]  # Pour l'instant, prendre le premier
        
        # Sélectionner la version
        if version:
            prompt_version = self.version_manager.get_version(template.id, version)
        else:
            # A/B testing pour sélection automatique
            prompt_version = self._select_version_ab_testing(template.id)
        
        if not prompt_version:
            # Utiliser le template de base
            prompt = self.format_prompt(template.id, context)
            if language:
                prompt = self.language_optimizer.optimize_prompt(prompt, language, context)
            return prompt, "base"
        
        # Formater le prompt avec les variables
        prompt = self._format_version_prompt(prompt_version, context)
        
        # Optimiser pour le langage si spécifié
        if language:
            prompt = self.language_optimizer.optimize_prompt(prompt, language, context)
        
        return prompt, prompt_version.version
    
    def _select_version_ab_testing(self, template_id: str) -> Optional[PromptVersion]:
        """
        Sélectionne une version en utilisant l'algorithme epsilon-greedy.
        
        Args:
            template_id: ID du template
            
        Returns:
            Version sélectionnée
        """
        if not self.ab_testing_enabled:
            return self.version_manager.get_best_version(template_id)
        
        # Epsilon-greedy: exploration vs exploitation
        if random.random() < self.exploration_rate:
            # Exploration: choisir une version aléatoire
            versions = self.version_manager.get_all_versions(template_id)
            if versions:
                return random.choice(versions)
        else:
            # Exploitation: choisir la meilleure version
            return self.version_manager.get_best_version(template_id)
        
        return None
    
    def _format_version_prompt(self, version: PromptVersion, variables: Dict[str, Any]) -> str:
        """
        Formate un prompt versionné avec les variables.
        
        Args:
            version: Version du prompt
            variables: Variables à injecter
            
        Returns:
            Prompt formaté
        """
        prompt = version.content
        
        # Remplacer les variables
        for key, value in variables.items():
            placeholder = f"{{{key}}}"
            if placeholder in prompt:
                prompt = prompt.replace(placeholder, str(value))
        
        return prompt
    
    def _get_default_template(self, tool_name: str) -> Optional[PromptTemplate]:
        """
        Récupère un template par défaut pour un outil.
        
        Args:
            tool_name: Nom de l'outil
            
        Returns:
            Template par défaut ou None
        """
        # Chercher un template avec le nom de l'outil
        all_templates = self.get_all_templates()
        for template in all_templates:
            if tool_name.lower() in template.name.lower():
                return template
        return None
    
    def track_performance(
        self,
        template_id: str,
        version: str,
        execution_time: float,
        tokens_used: int,
        success: bool,
        user_feedback: Optional[float] = None
    ) -> None:
        """
        Enregistre les performances d'une exécution.
        
        Args:
            template_id: ID du template
            version: Version utilisée
            execution_time: Temps d'exécution
            tokens_used: Tokens consommés
            success: Succès de l'exécution
            user_feedback: Note utilisateur (0-1)
        """
        # Mettre à jour les métriques dans le version manager
        self.version_manager.update_performance_metrics(
            template_id=template_id,
            version=version,
            execution_time=execution_time,
            tokens_used=tokens_used,
            success=success
        )
        
        # Mettre à jour le cache de performances local
        if template_id not in self.performance_cache:
            self.performance_cache[template_id] = []
        
        # Ajouter les métriques au cache
        metric_entry = {
            'version': version,
            'execution_time': execution_time,
            'tokens_used': tokens_used,
            'success': success,
            'timestamp': os.path.getmtime('.')  # Timestamp actuel
        }
        
        if user_feedback is not None:
            metric_entry['user_feedback'] = user_feedback
        
        self.performance_cache[template_id].append(metric_entry)
        
        # Calculer et mettre à jour le score de performance
        prompt_version = self.version_manager.get_version(template_id, version)
        if prompt_version:
            # Score basé sur: succès (40%), temps (30%), tokens (20%), feedback (10%)
            success_score = prompt_version.success_rate * 40
            
            # Normaliser le temps (moins c'est mieux, max 10s)
            time_score = max(0, (10 - prompt_version.average_generation_time) / 10) * 30
            
            # Normaliser les tokens (moins c'est mieux, max 2000)
            token_score = max(0, (2000 - prompt_version.average_tokens) / 2000) * 20
            
            # Feedback utilisateur si disponible
            feedback_score = (user_feedback * 10) if user_feedback else 5
            
            prompt_version.performance_score = success_score + time_score + token_score + feedback_score
            
            # Sauvegarder
            self.version_manager._save_versions()
        
        # Mettre à jour les statistiques dans le cache pour un accès rapide
        if template_id in self.performance_cache:
            # Calculer les statistiques agrégées
            metrics = self.performance_cache[template_id]
            total_count = len(metrics)
            success_count = sum(1 for m in metrics if m.get('success', False))
            
            # Ajouter les statistiques au cache
            self.performance_cache[f"{template_id}_stats"] = {
                'total_executions': total_count,
                'success_rate': success_count / total_count if total_count > 0 else 0,
                'average_time': sum(m.get('execution_time', 0) for m in metrics) / total_count if total_count > 0 else 0,
                'average_tokens': sum(m.get('tokens_used', 0) for m in metrics) / total_count if total_count > 0 else 0
            }
    
    def get_performance_report(self, template_id: str) -> Dict[str, Any]:
        """
        Génère un rapport de performance pour un template.
        
        Args:
            template_id: ID du template
            
        Returns:
            Rapport avec métriques par version
        """
        versions = self.version_manager.get_all_versions(template_id)
        
        report = {
            "template_id": template_id,
            "total_versions": len(versions),
            "versions": []
        }
        
        for version in versions:
            report["versions"].append({
                "version": version.version,
                "is_active": version.is_active,
                "usage_count": version.usage_count,
                "success_rate": version.success_rate,
                "average_tokens": version.average_tokens,
                "average_time": version.average_generation_time,
                "performance_score": version.performance_score
            })
        
        # Trier par score de performance
        report["versions"].sort(key=lambda v: v["performance_score"], reverse=True)
        
        return report
