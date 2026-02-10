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

    def __init__(self, templates_dir: str = None, storage_dir: str = None):
        super().__init__(storage_path=storage_dir)
        self.version_manager = PromptVersionManager(storage_dir)
        self.language_optimizer = LanguageOptimizer()
        self.ab_testing_enabled = True
        self.exploration_rate = 0.1
        self.performance_cache: Dict[str, Dict[str, float]] = {}
        self.templates: Dict[str, PromptTemplate] = {}
        self.tool_templates_dir = os.path.join(
            os.path.dirname(__file__), '..', 'templates', 'tools'
        )
        self._load_tool_templates()

    def _load_tool_templates(self) -> None:

        if not os.path.exists(self.tool_templates_dir):
            Path(self.tool_templates_dir).mkdir(parents=True, exist_ok=True)
            return

        for tool_dir in Path(self.tool_templates_dir).iterdir():
            if tool_dir.is_dir():
                tool_name = tool_dir.name

                for yaml_file in tool_dir.glob("*.yaml"):
                    try:
                        import yaml
                        with open(yaml_file, 'r', encoding='utf-8') as f:
                            template_data = yaml.safe_load(f)

                        template = self.create_template({
                            "name": template_data.get("name"),
                            "description": template_data.get("description", ""),
                            "template": template_data.get("template"),
                            "variables": template_data.get("variables", []),
                            "category": f"tool/{tool_name}",
                            "tags": template_data.get("tags", []),
                            "provider_specific": template_data.get("provider_specific", {})
                        })

                        template_key = f"{tool_name}_{yaml_file.stem}"
                        self.templates[template_key] = template

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

        category = f"tool/{tool_name.lower()}"
        templates = self.get_templates_by_category(category)

        if not templates:
            default_template = self._get_default_template(tool_name)
            if default_template:
                prompt = self.format_prompt(default_template.id, context)
                return prompt, "default"
            raise ValueError(f"Aucun template trouvé pour l'outil {tool_name}")

        template = templates[0]

        if version:
            prompt_version = self.version_manager.get_version(template.id, version)
        else:
            prompt_version = self._select_version_ab_testing(template.id)

        if not prompt_version:
            prompt = self.format_prompt(template.id, context)
            if language:
                prompt = self.language_optimizer.optimize_prompt(prompt, language, context)
            return prompt, "base"

        prompt = self._format_version_prompt(prompt_version, context)

        if language:
            prompt = self.language_optimizer.optimize_prompt(prompt, language, context)

        return prompt, prompt_version.version

    def _select_version_ab_testing(self, template_id: str) -> Optional[PromptVersion]:

        if not self.ab_testing_enabled:
            return self.version_manager.get_best_version(template_id)


        if random.random() < self.exploration_rate:
            versions = self.version_manager.get_all_versions(template_id)
            if versions:
                return random.choice(versions)
        else:
            return self.version_manager.get_best_version(template_id)

        return None

    def _format_version_prompt(self, version: PromptVersion, variables: Dict[str, Any]) -> str:

        prompt = version.content

        for key, value in variables.items():
            placeholder = f"{ {key}} "
            if placeholder in prompt:
                prompt = prompt.replace(placeholder, str(value))

        return prompt

    def _get_default_template(self, tool_name: str) -> Optional[PromptTemplate]:

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

        self.version_manager.update_performance_metrics(
            template_id=template_id,
            version=version,
            execution_time=execution_time,
            tokens_used=tokens_used,
            success=success
        )

        if template_id not in self.performance_cache:
            self.performance_cache[template_id] = []

        metric_entry = {
            'version': version,
            'execution_time': execution_time,
            'tokens_used': tokens_used,
            'success': success,
            'timestamp': os.path.getmtime('.')
        }

        if user_feedback is not None:
            metric_entry['user_feedback'] = user_feedback

        self.performance_cache[template_id].append(metric_entry)

        prompt_version = self.version_manager.get_version(template_id, version)
        if prompt_version:

            success_score = prompt_version.success_rate * 40

            time_score = max(0, (10 - prompt_version.average_generation_time) / 10) * 30

            token_score = max(0, (2000 - prompt_version.average_tokens) / 2000) * 20

            feedback_score = (user_feedback * 10) if user_feedback else 5

            prompt_version.performance_score = success_score + time_score + token_score + feedback_score

            self.version_manager._save_versions()

        if template_id in self.performance_cache:
            metrics = self.performance_cache[template_id]
            total_count = len(metrics)
            success_count = sum(1 for m in metrics if m.get('success', False))

            self.performance_cache[f"{template_id}_stats"] = {
                'total_executions': total_count,
                'success_rate': success_count / total_count if total_count > 0 else 0,
                'average_time': sum(m.get('execution_time', 0) for m in metrics) / total_count if total_count > 0 else 0,
                'average_tokens': sum(m.get('tokens_used', 0) for m in metrics) / total_count if total_count > 0 else 0
            }

    def get_performance_report(self, template_id: str) -> Dict[str, Any]:
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

        report["versions"].sort(key=lambda v: v["performance_score"], reverse=True)

        return report
