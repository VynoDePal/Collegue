"""
Enhanced Prompt Engine avec versioning, optimisation et tracking de performance
"""
import datetime
import logging
import os
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import PromptVariable
from .optimizer import LanguageOptimizer
from .prompt_engine import PromptEngine, PromptTemplate
from .versioning import PromptVersion, PromptVersionManager

logger = logging.getLogger(__name__)


class EnhancedPromptEngine(PromptEngine):

    def __init__(self, templates_dir: str = None, storage_dir: str = None):
        """Construct the engine and load seed templates from ``templates_dir``.

        A/B testing (``ab_testing_enabled=True``) runs a ε-greedy bandit on
        the versions attached to each template. The bandit is only *actually*
        exploratory when a template has **≥ 2 versions with distinct content**
        (see :meth:`_select_version_ab_testing`) — otherwise the single
        available version is returned deterministically. This safety guard
        was added under #232 to stop the selection from faking randomness
        over identical clones (which happened before #231 dedup).

        Parameters
        ----------
        templates_dir:
            Override for the YAML seed directory. Defaults to the
            ``collegue/prompts/templates/tools`` tree in the installed package.
        storage_dir:
            Override for the on-disk JSON store. Defaults to the package
            ``collegue/prompts/templates`` directory (``versions/versions.json``
            lives alongside).
        """
        super().__init__(storage_path=storage_dir)
        self.version_manager = PromptVersionManager(storage_dir)
        self.language_optimizer = LanguageOptimizer()
        self.ab_testing_enabled = True
        self.exploration_rate = 0.1
        self.performance_cache: Dict[str, Dict[str, float]] = {}
        self.templates: Dict[str, PromptTemplate] = {}
        self.tool_templates_dir = templates_dir or os.path.join(
            os.path.dirname(__file__), '..', 'templates', 'tools'
        )
        self._load_tool_templates()
        self._log_ab_readiness()

    def _log_ab_readiness(self) -> None:
        """Count templates with real variants vs clones and log the breakdown.

        Gives operators a one-line signal at startup : if the count of
        "A/B enabled" templates is 0, the fancy bandit is not doing anything —
        someone needs to author variant YAMLs (v2, experimental, …) with
        different content.
        """
        active = 0
        single_variant = 0
        for template in self.library.templates.values():
            versions = self.version_manager.get_all_versions(template.id)
            unique = {v.content for v in versions}
            if len(unique) >= 2:
                active += 1
            else:
                single_variant += 1
        logger.info(
            "A/B testing status: %d template(s) with ≥ 2 real variants, "
            "%d with a single variant (bandit returns deterministically for these)",
            active, single_variant,
        )

    def _load_tool_templates(self) -> None:
        """Load YAML seed templates from ``tool_templates_dir`` — idempotently.

        Before #231 this method created a brand-new UUID for every YAML on
        every server startup, accumulating 132× duplicates after 132 restarts.
        The fix keyed on the template ``name`` (set in the YAML frontmatter) :

        - **Same name, same content** → skip. The in-memory entry already
          exists because ``PromptEngine._load_library()`` just read the JSON
          from disk. Nothing to write.
        - **Same name, content changed** → update the existing template in
          place, preserving the UUID so any accumulated ``performance_score``
          / ``usage_count`` survive the edit.
        - **New name** → create as before.

        Same logic applies to the version_manager via :meth:`_ensure_version`
        — we don't append a new ``PromptVersion`` when the current content
        is already represented.
        """
        if not os.path.exists(self.tool_templates_dir):
            Path(self.tool_templates_dir).mkdir(parents=True, exist_ok=True)
            return

        import yaml

        # Build a name → canonical-template index from whatever was loaded
        # from disk by the parent class. When legacy duplicates exist on
        # disk (pre-#231), several templates share the same name — pick the
        # first one; the purge script handles the cleanup separately.
        existing_by_name: Dict[str, PromptTemplate] = {}
        for template in self.library.templates.values():
            existing_by_name.setdefault(template.name, template)

        created = updated = skipped = 0

        for tool_dir in Path(self.tool_templates_dir).iterdir():
            if not tool_dir.is_dir():
                continue
            tool_name = tool_dir.name

            for yaml_file in tool_dir.glob("*.yaml"):
                try:
                    with open(yaml_file, 'r', encoding='utf-8') as f:
                        template_data = yaml.safe_load(f)

                    name = template_data.get("name")
                    if not name:
                        logger.warning(
                            "Template YAML %s has no 'name' key, skipped", yaml_file
                        )
                        continue

                    yaml_content = template_data.get("template", "")
                    key = f"{tool_name}_{yaml_file.stem}"
                    existing = existing_by_name.get(name)

                    if existing and existing.template == yaml_content:
                        # Already loaded and identical — nothing to persist.
                        self.templates[key] = existing
                        skipped += 1
                        continue

                    if existing:
                        # Same name, content drifted → update in place.
                        existing.template = yaml_content
                        existing.description = template_data.get(
                            "description", existing.description
                        )
                        existing.variables = [
                            PromptVariable(**var) if isinstance(var, dict) else var
                            for var in template_data.get("variables", [])
                        ]
                        existing.tags = template_data.get("tags", existing.tags)
                        existing.updated_at = datetime.datetime.now()
                        self._save_library()
                        self.templates[key] = existing
                        self._ensure_version(existing.id, yaml_content, template_data)
                        updated += 1
                        logger.info(
                            "Template '%s' updated from %s (UUID preserved)",
                            name, yaml_file,
                        )
                        continue

                    # Brand-new template: create fresh.
                    template = self.create_template({
                        "name": name,
                        "description": template_data.get("description", ""),
                        "template": yaml_content,
                        "variables": template_data.get("variables", []),
                        "category": f"tool/{tool_name}",
                        "tags": template_data.get("tags", []),
                        "provider_specific": template_data.get("provider_specific", {}),
                    })
                    self.templates[key] = template
                    existing_by_name[name] = template
                    self._ensure_version(template.id, yaml_content, template_data)
                    created += 1
                    logger.info("Template '%s' created from %s", name, yaml_file)

                except Exception as exc:
                    logger.error("Erreur lors du chargement de %s: %s", yaml_file, exc)

        logger.info(
            "Tool templates loaded: %d created, %d updated, %d skipped (already loaded)",
            created, updated, skipped,
        )

    def _ensure_version(
        self,
        template_id: str,
        content: str,
        template_data: Dict[str, Any],
    ) -> None:
        """Create a ``PromptVersion`` for this template only if no existing
        version already has the same content. Called from the YAML loader
        to keep ``versions.json`` idempotent across restarts.
        """
        existing = self.version_manager.get_all_versions(template_id)
        if any(v.content == content for v in existing):
            return
        self.version_manager.create_version(
            template_id=template_id,
            content=content,
            variables=template_data.get("variables", []),
            version=template_data.get("version", "1.0.0"),
        )

    async def get_optimized_prompt(
        self,
        tool_name: str,
        context: Dict[str, Any],
        language: Optional[str] = None,
        version: Optional[str] = None
    ) -> Tuple[str, str]:

        category = f"tool/{tool_name.lower()}"
        templates = self.get_templates_by_category(category)

        # Compat: si aucun template n'existe, on peut quand même utiliser
        # des versions créées explicitement via version_manager (tests/unit).
        if not templates:
            versions = self.version_manager.get_all_versions(tool_name)
            if versions:
                prompt_version = (
                    self.version_manager.get_version(tool_name, version)
                    if version else self._select_version_ab_testing(tool_name)
                )
                if prompt_version is None:
                    prompt_version = versions[-1]
                prompt = self._format_version_prompt(prompt_version, context)
                if language:
                    prompt = self.language_optimizer.optimize_prompt(
                        prompt,
                        language,
                        context,
                    )
                return prompt, prompt_version.id

            default_template = self._get_default_template(tool_name)
            if default_template:
                prompt = self.format_prompt(default_template.id, context)
                return prompt, "default"
            raise ValueError(f"Aucun template trouvé pour l'outil {tool_name}")

        # Prefer the canonical ``<tool>_default`` template when it exists.
        # Without this, the engine picks whatever ``templates[0]`` happens
        # to be — which was empirically ``test_generation_v2`` (the
        # alphabetical first), silently masking any edit to the default
        # YAML and making the "A/B between v2 / experimental / default"
        # story fiction. Loading order is not a selection policy.
        default_name = f"{tool_name.lower()}_default"
        template = next(
            (t for t in templates if t.name.lower() == default_name),
            templates[0],
        )

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

        return prompt, prompt_version.id

    def _select_version_ab_testing(self, template_id: str) -> Optional[PromptVersion]:
        """Pick a :class:`PromptVersion` for ``template_id``.

        A/B-safe selection policy (added under #232) :

        1. Gather all versions attached to this template.
        2. Compute the set of **distinct contents**. If there is only one
           unique content, skip the bandit entirely and return the best
           (or latest) version deterministically. No fake randomness over
           clones.
        3. Otherwise run the ε-greedy bandit : ``exploration_rate`` of the
           time, pick a random version (explore); the rest of the time,
           pick the current best (exploit).

        Without step 2, before #231 fixed the loader, this function would
        pick between 132 identical JSON copies — spending CPU on a coin
        flip that couldn't change the output. After #231, templates are
        distinct but most tools still have only 1 version, so the guard
        keeps the path deterministic until someone authors real variants.
        """
        versions = self.version_manager.get_all_versions(template_id)
        if not versions:
            return None

        unique_contents = {v.content for v in versions}
        if len(unique_contents) < 2:
            # No real variation to explore — return a stable choice.
            best = self.version_manager.get_best_version(template_id)
            return best or versions[-1]

        if not self.ab_testing_enabled:
            return self.version_manager.get_best_version(template_id)

        if random.random() < self.exploration_rate:
            return random.choice(versions)
        return self.version_manager.get_best_version(template_id)

    def _format_version_prompt(self, version: PromptVersion, variables: Dict[str, Any]) -> str:

        prompt = version.content

        for key, value in variables.items():
            placeholder = f"{{{key}}}"
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
