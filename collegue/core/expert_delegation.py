"""
Expert Delegation — Système de délégation inter-experts pour Collègue MCP.

Ce module gère le déclenchement automatique d'un expert (tool) par un autre
via une matrice de règles configurables. Il transforme le chaînage hardcodé
``auto_chain`` en un système générique et extensible.

Architecture :
    Source Tool → DelegationRule (condition) → Target Tool (params_builder)
    → Exécution → DelegationResult

Sécurité :
    - max_chain_depth pour éviter les boucles infinies
    - timeout global pour les chaînes longues
    - logging structuré de chaque délégation
"""

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("expert_delegation")


class DelegationRule(BaseModel):
    """Règle de délégation entre deux experts."""

    source_tool: str = Field(..., description="Tool qui déclenche la délégation")
    target_tool: str = Field(..., description="Tool déclenché")
    condition_name: str = Field(
        ...,
        description="Nom lisible de la condition (pour logging/debug)",
    )
    priority: int = Field(
        default=10,
        ge=0,
        le=100,
        description="Priorité (plus bas = plus prioritaire)",
    )

    model_config = {"arbitrary_types_allowed": True}


class DelegationTask(BaseModel):
    """Tâche de délégation prête à être exécutée."""

    rule: DelegationRule = Field(..., description="Règle de délégation source")
    target_tool: str = Field(..., description="Nom du tool cible")
    params: Dict[str, Any] = Field(default_factory=dict, description="Paramètres pour le tool cible")
    depth: int = Field(default=1, description="Profondeur actuelle dans la chaîne")


class DelegationResult(BaseModel):
    """Résultat d'une délégation exécutée."""

    source_tool: str = Field(..., description="Tool source")
    target_tool: str = Field(..., description="Tool exécuté")
    success: bool = Field(..., description="True si l'exécution a réussi")
    result: Optional[Dict[str, Any]] = Field(None, description="Résultat du tool")
    error: Optional[str] = Field(None, description="Erreur si échec")
    execution_time: float = Field(default=0.0, description="Temps d'exécution en secondes")
    depth: int = Field(default=1, description="Profondeur dans la chaîne")
    sub_delegations: List["DelegationResult"] = Field(
        default_factory=list,
        description="Sous-délégations déclenchées par ce résultat",
    )


class DelegationChainReport(BaseModel):
    """Rapport complet d'une chaîne de délégation."""

    source_tool: str = Field(..., description="Tool initiateur")
    total_experts_activated: int = Field(default=0, description="Nombre total d'experts activés")
    max_depth_reached: int = Field(default=0, description="Profondeur maximale atteinte")
    results: List[DelegationResult] = Field(default_factory=list, description="Résultats de la chaîne")
    total_time: float = Field(default=0.0, description="Temps total de la chaîne")
    chain_completed: bool = Field(default=True, description="True si la chaîne s'est terminée normalement")
    abort_reason: Optional[str] = Field(None, description="Raison de l'arrêt si non complétée")


# Type aliases for rule callables (not stored in Pydantic, kept in engine)
ConditionFn = Callable[[Dict[str, Any]], bool]
ParamsBuilderFn = Callable[[str, Dict[str, Any]], Dict[str, Any]]


class ExpertDelegationEngine:
    """Moteur de délégation inter-experts.

    Gère l'évaluation et l'exécution des chaînes de délégation entre tools.
    """

    def __init__(
        self,
        max_chain_depth: int = 5,
        chain_timeout: float = 300.0,
    ):
        self._rules: List[DelegationRule] = []
        self._conditions: Dict[str, ConditionFn] = {}
        self._params_builders: Dict[str, ParamsBuilderFn] = {}
        self.max_chain_depth = max_chain_depth
        self.chain_timeout = chain_timeout
        self._chain_history: List[DelegationResult] = []

    def register_rule(
        self,
        source_tool: str,
        target_tool: str,
        condition: ConditionFn,
        params_builder: ParamsBuilderFn,
        condition_name: str = "",
        priority: int = 10,
    ) -> None:
        """Enregistre une règle de délégation."""
        if not condition_name:
            condition_name = f"{source_tool}_to_{target_tool}"

        rule = DelegationRule(
            source_tool=source_tool,
            target_tool=target_tool,
            condition_name=condition_name,
            priority=priority,
        )
        rule_key = f"{source_tool}->{target_tool}:{condition_name}"
        self._rules.append(rule)
        self._conditions[rule_key] = condition
        self._params_builders[rule_key] = params_builder

        logger.info(
            "Règle de délégation enregistrée: %s → %s (condition: %s, priorité: %d)",
            source_tool,
            target_tool,
            condition_name,
            priority,
        )

    def _get_rule_key(self, rule: DelegationRule) -> str:
        return f"{rule.source_tool}->{rule.target_tool}:{rule.condition_name}"

    async def evaluate_delegations(
        self,
        source_tool: str,
        result: Dict[str, Any],
    ) -> List[DelegationTask]:
        """Évalue quelles délégations doivent être déclenchées.

        Args:
            source_tool: Nom du tool qui vient de s'exécuter.
            result: Résultat du tool (dict du response model).

        Returns:
            Liste de DelegationTask triées par priorité.
        """
        matching_rules = [r for r in self._rules if r.source_tool == source_tool]
        matching_rules.sort(key=lambda r: r.priority)

        tasks: List[DelegationTask] = []
        for rule in matching_rules:
            rule_key = self._get_rule_key(rule)
            condition = self._conditions.get(rule_key)
            params_builder = self._params_builders.get(rule_key)

            if not condition or not params_builder:
                continue

            try:
                should_delegate = condition(result)
            except Exception as e:
                logger.warning(
                    "Erreur évaluation condition %s: %s",
                    rule.condition_name,
                    e,
                )
                continue

            if should_delegate:
                try:
                    params = params_builder(source_tool, result)
                except Exception as e:
                    logger.warning(
                        "Erreur construction params pour %s → %s: %s",
                        source_tool,
                        rule.target_tool,
                        e,
                    )
                    continue

                tasks.append(
                    DelegationTask(
                        rule=rule,
                        target_tool=rule.target_tool,
                        params=params,
                        depth=1,
                    )
                )
                logger.info(
                    "Délégation planifiée: %s → %s (condition: %s)",
                    source_tool,
                    rule.target_tool,
                    rule.condition_name,
                )

        return tasks

    async def execute_delegation_chain(
        self,
        tasks: List[DelegationTask],
        tool_registry: Dict[str, Any],
        ctx: Any = None,
        current_depth: int = 1,
        chain_start_time: Optional[float] = None,
        tool_kwargs: Optional[Dict[str, Any]] = None,
    ) -> List[DelegationResult]:
        """Exécute une chaîne de délégation.

        Args:
            tasks: Tâches de délégation à exécuter.
            tool_registry: Registre des tools disponibles.
            ctx: Contexte FastMCP.
            current_depth: Profondeur actuelle dans la chaîne.
            chain_start_time: Timestamp de début de la chaîne.
            tool_kwargs: Arguments supplémentaires pour les tools (parser, prompt_engine, etc.).

        Returns:
            Liste de DelegationResult.
        """
        if chain_start_time is None:
            chain_start_time = time.time()
            self._chain_history = []

        results: List[DelegationResult] = []

        for task in tasks:
            # Anti-boucle infinie
            if current_depth > self.max_chain_depth:
                logger.warning(
                    "Profondeur max atteinte (%d), arrêt de la chaîne",
                    self.max_chain_depth,
                )
                results.append(
                    DelegationResult(
                        source_tool=task.rule.source_tool,
                        target_tool=task.target_tool,
                        success=False,
                        error=f"Profondeur maximale atteinte ({self.max_chain_depth})",
                        depth=current_depth,
                    )
                )
                continue

            # Timeout global
            elapsed = time.time() - chain_start_time
            if elapsed > self.chain_timeout:
                logger.warning(
                    "Timeout chaîne atteint (%.1fs > %.1fs)",
                    elapsed,
                    self.chain_timeout,
                )
                results.append(
                    DelegationResult(
                        source_tool=task.rule.source_tool,
                        target_tool=task.target_tool,
                        success=False,
                        error=f"Timeout chaîne ({self.chain_timeout}s)",
                        depth=current_depth,
                    )
                )
                continue

            # Exécution du tool cible
            delegation_result = await self._execute_single_delegation(task, tool_registry, ctx, tool_kwargs)
            delegation_result.depth = current_depth

            # Évaluer les sous-délégations si l'exécution a réussi
            if delegation_result.success and delegation_result.result:
                sub_tasks = await self.evaluate_delegations(
                    task.target_tool,
                    delegation_result.result,
                )
                if sub_tasks:
                    sub_results = await self.execute_delegation_chain(
                        sub_tasks,
                        tool_registry,
                        ctx,
                        current_depth + 1,
                        chain_start_time,
                        tool_kwargs,
                    )
                    delegation_result.sub_delegations = sub_results

            results.append(delegation_result)
            self._chain_history.append(delegation_result)

            if ctx and hasattr(ctx, "info"):
                status = "✅" if delegation_result.success else "❌"
                await ctx.info(
                    f"{status} Délégation {task.rule.source_tool} → {task.target_tool} "
                    f"(profondeur: {current_depth}, temps: {delegation_result.execution_time:.1f}s)"
                )

        return results

    async def _execute_single_delegation(
        self,
        task: DelegationTask,
        tool_registry: Dict[str, Any],
        ctx: Any = None,
        tool_kwargs: Optional[Dict[str, Any]] = None,
    ) -> DelegationResult:
        """Exécute une seule délégation."""
        start_time = time.time()

        if task.target_tool not in tool_registry:
            return DelegationResult(
                source_tool=task.rule.source_tool,
                target_tool=task.target_tool,
                success=False,
                error=f"Tool '{task.target_tool}' non trouvé dans le registre",
                execution_time=time.time() - start_time,
            )

        try:
            tool_entry = tool_registry[task.target_tool]
            tool_class = tool_entry["class"]
            tool_instance = tool_class({})

            req_model = tool_instance.get_request_model()
            req_obj = req_model(**task.params)

            kwargs = dict(tool_kwargs or {})
            if ctx is not None:
                kwargs["ctx"] = ctx

            result = await tool_instance.execute_async(req_obj, **kwargs)

            result_dict = (
                result.model_dump()
                if hasattr(result, "model_dump")
                else (result.dict() if hasattr(result, "dict") else str(result))
            )

            execution_time = time.time() - start_time

            logger.info(
                "Délégation %s → %s réussie en %.1fs",
                task.rule.source_tool,
                task.target_tool,
                execution_time,
            )

            return DelegationResult(
                source_tool=task.rule.source_tool,
                target_tool=task.target_tool,
                success=True,
                result=result_dict,
                execution_time=execution_time,
            )

        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(
                "Erreur délégation %s → %s: %s (%.1fs)",
                task.rule.source_tool,
                task.target_tool,
                e,
                execution_time,
            )
            return DelegationResult(
                source_tool=task.rule.source_tool,
                target_tool=task.target_tool,
                success=False,
                error=str(e),
                execution_time=execution_time,
            )
        finally:
            if "tool_instance" in locals() and hasattr(tool_instance, "cleanup"):
                try:
                    tool_instance.cleanup()
                except Exception:
                    pass

    def get_chain_history(self) -> List[DelegationResult]:
        """Retourne l'historique de la chaîne en cours."""
        return list(self._chain_history)

    def build_chain_report(
        self,
        source_tool: str,
        results: Optional[List[DelegationResult]] = None,
    ) -> DelegationChainReport:
        """Construit un rapport de la chaîne de délégation.

        Args:
            source_tool: Nom du tool initiateur.
            results: Résultats de la chaîne (top-level). Si fourni, le rapport
                est construit à partir de ces résultats plutôt que de l'historique
                interne partagé, ce qui le rend safe pour les appels concurrents.
        """
        chain_results = results if results is not None else self._chain_history

        def _count_experts(res_list: List[DelegationResult]) -> int:
            count = 0
            for r in res_list:
                if r.success:
                    count += 1
                count += _count_experts(r.sub_delegations)
            return count

        def _max_depth(res_list: List[DelegationResult]) -> int:
            if not res_list:
                return 0
            return max(
                max(r.depth for r in res_list),
                max((_max_depth(r.sub_delegations) for r in res_list), default=0),
            )

        total_time = sum(r.execution_time for r in chain_results)

        abort_reason: Optional[str] = None
        chain_completed = True
        for r in chain_results:
            if not r.success and r.error:
                if "Profondeur maximale" in r.error or "Timeout" in r.error:
                    chain_completed = False
                    abort_reason = r.error
                    break

        return DelegationChainReport(
            source_tool=source_tool,
            total_experts_activated=_count_experts(chain_results),
            max_depth_reached=_max_depth(chain_results),
            results=list(chain_results),
            total_time=total_time,
            chain_completed=chain_completed,
            abort_reason=abort_reason,
        )

    def get_rules_for_tool(self, tool_name: str) -> List[DelegationRule]:
        """Retourne les règles de délégation pour un tool donné."""
        return [r for r in self._rules if r.source_tool == tool_name]

    def clear_history(self) -> None:
        """Réinitialise l'historique de la chaîne."""
        self._chain_history = []


def _refactoring_has_changes(result: Dict[str, Any]) -> bool:
    """Condition: le refactoring a effectué des changements."""
    changes = result.get("changes", [])
    refactored = result.get("refactored_code", "")
    original = result.get("original_code", "")
    return bool(changes) or (refactored and original and refactored != original)


def _consistency_needs_refactoring(result: Dict[str, Any]) -> bool:
    """Condition: le score de refactoring dépasse le seuil."""
    return result.get("refactoring_score", 0.0) > 0.5


def _iac_needs_remediation(result: Dict[str, Any]) -> bool:
    """Condition: le score de sécurité est trop bas."""
    return result.get("security_score", 1.0) < 0.5


def _impact_has_risks(result: Dict[str, Any]) -> bool:
    """Condition: l'analyse d'impact a détecté des risques."""
    return len(result.get("risk_notes", [])) > 0


def _impact_has_iac_files(result: Dict[str, Any]) -> bool:
    """Condition: l'impact touche des fichiers IaC."""
    iac_extensions = {".tf", ".yaml", ".yml", ".json", ".hcl"}
    impacted = result.get("impacted_files", [])
    for f in impacted:
        path = f.get("path", "") if isinstance(f, dict) else ""
        if any(path.endswith(ext) for ext in iac_extensions):
            return True
    return False


def _build_refactoring_params_from_consistency(source_tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Construit les paramètres de refactoring depuis un résultat de consistency check."""
    actions = result.get("suggested_actions", [])
    if actions:
        best = max(actions, key=lambda a: a.get("score", 0) if isinstance(a, dict) else 0)
        params = best.get("params", {}) if isinstance(best, dict) else {}
        if params.get("code"):
            return {
                "code": params["code"],
                "language": params.get("language", "python"),
                "refactoring_type": params.get("refactoring_type", "clean"),
                "parameters": {"context": "auto-delegated from repo_consistency_check"},
            }

    issues = result.get("issues", [])
    summary = f"# Issues détectées: {len(issues)}\n"
    for issue in issues[:5]:
        if isinstance(issue, dict):
            summary += f"- {issue.get('title', issue.get('message', str(issue)))}\n"
        else:
            summary += f"- {issue}\n"

    return {
        "code": summary,
        "language": "python",
        "refactoring_type": "clean",
        "parameters": {"context": "auto-delegated from repo_consistency_check"},
    }


def _empty_code_placeholder(language: str) -> str:
    """Return a language-appropriate placeholder when code is empty."""
    if language in ("javascript", "typescript"):
        return "// No refactored code available"
    return "# No refactored code available"


def _build_documentation_params_from_refactoring(source_tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Construit les paramètres de documentation depuis un résultat de refactoring."""
    lang = result.get("language", "python").lower()
    code = (result.get("refactored_code", "") or "").strip() or _empty_code_placeholder(lang)
    return {
        "code": code,
        "language": lang,
        "documentation_type": "auto",
        "parameters": {"context": "auto-delegated from code_refactoring"},
    }


def _build_test_params_from_refactoring(source_tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Construit les paramètres de test_generation depuis un résultat de refactoring."""
    lang = result.get("language", "python").lower()
    code = (result.get("refactored_code", "") or "").strip() or _empty_code_placeholder(lang)
    return {
        "code": code,
        "language": lang,
        "test_framework": "pytest" if lang == "python" else "jest",
        "coverage_target": 0.80,
        "parameters": {"context": "auto-delegated from code_refactoring"},
    }


def _build_test_params_from_impact(source_tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Construit les paramètres de test depuis un résultat d'impact analysis."""
    impacted = result.get("impacted_files", [])
    risks = result.get("risk_notes", [])
    code_summary = "# Impact Analysis Results\n"
    code_summary += f"## {len(impacted)} fichiers impactés\n"
    for f in impacted[:5]:
        if isinstance(f, dict):
            code_summary += f"- {f.get('path', '')}: {f.get('reason', '')}\n"
    code_summary += f"\n## {len(risks)} risques détectés\n"
    for r in risks[:5]:
        if isinstance(r, dict):
            code_summary += f"- [{r.get('severity', 'medium')}] {r.get('note', '')}\n"

    return {
        "code": code_summary,
        "language": "python",
        "test_framework": "pytest",
        "coverage_target": 0.80,
        "parameters": {"context": "auto-delegated from impact_analysis"},
    }


def _build_iac_params_from_impact(source_tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Construit les paramètres de scan IaC depuis un résultat d'impact analysis."""
    iac_extensions = {".tf", ".yaml", ".yml", ".json", ".hcl"}
    impacted = result.get("impacted_files", [])
    iac_files = []
    for f in impacted:
        if isinstance(f, dict):
            path = f.get("path", "")
            if any(path.endswith(ext) for ext in iac_extensions):
                iac_files.append({"path": path, "content": f"# IaC file: {path}", "type": "auto-detected"})

    if not iac_files:
        iac_files = [{"path": "Dockerfile", "content": "# placeholder", "type": "auto-detected"}]

    return {
        "files": iac_files,
        "scan_type": "security",
        "analysis_depth": "fast",
        "parameters": {"context": "auto-delegated from impact_analysis"},
    }


def _build_refactoring_params_from_iac(source_tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Construit les paramètres de refactoring depuis un résultat IaC."""
    findings = result.get("findings", [])
    summary = "# Security findings to remediate\n"
    for f in findings[:10]:
        if isinstance(f, dict):
            summary += f"- [{f.get('severity', 'medium')}] {f.get('title', f.get('message', str(f)))}\n"
        else:
            summary += f"- {f}\n"

    return {
        "code": summary,
        "language": "dockerfile",
        "refactoring_type": "clean",
        "parameters": {"context": "auto-delegated from iac_guardrails_scan (remediation)"},
    }


# --- Phase 3: Conditions et builders pour les nouveaux experts ---


def _refactoring_needs_review(result: Dict[str, Any]) -> bool:
    """Condition: le refactoring a produit des changements → déclencher une revue."""
    return _refactoring_has_changes(result)


def _review_quality_low(result: Dict[str, Any]) -> bool:
    """Condition: le score de qualité de la revue est trop bas → déclencher un refactoring."""
    return result.get("quality_score", 1.0) < 0.5


def _consistency_has_architectural_issues(result: Dict[str, Any]) -> bool:
    """Condition: le consistency check détecte des problèmes architecturaux."""
    issues = result.get("issues", [])
    for issue in issues:
        if isinstance(issue, dict):
            msg = issue.get("title", issue.get("message", "")).lower()
            if any(kw in msg for kw in ("architecture", "structure", "coupling", "cohesion", "circular", "dependency")):
                return True
    return result.get("refactoring_score", 0.0) > 0.7


def _architecture_has_debt(result: Dict[str, Any]) -> bool:
    """Condition: l'analyse architecturale a détecté de la dette technique."""
    return result.get("debt_score", 0.0) > 0.5


def _architecture_needs_impact(result: Dict[str, Any]) -> bool:
    """Condition: l'analyse architecturale recommande un refactoring important."""
    issues = result.get("issues", [])
    critical = sum(1 for i in issues if isinstance(i, dict) and i.get("severity") in ("error", "critical"))
    return critical > 0


def _consistency_has_performance_issues(result: Dict[str, Any]) -> bool:
    """Condition: le consistency check détecte des problèmes de performance."""
    issues = result.get("issues", [])
    for issue in issues:
        if isinstance(issue, dict):
            msg = issue.get("title", issue.get("message", "")).lower()
            if any(kw in msg for kw in ("performance", "slow", "complexity", "o(n", "bottleneck", "inefficient")):
                return True
    return False


def _performance_needs_refactoring(result: Dict[str, Any]) -> bool:
    """Condition: l'analyse de performance détecte des problèmes à corriger."""
    return result.get("performance_score", 1.0) < 0.5


def _performance_needs_tests(result: Dict[str, Any]) -> bool:
    """Condition: l'analyse de performance a proposé des optimisations → tester."""
    optimizations = result.get("optimizations", [])
    return len(optimizations) > 0


def _build_review_params_from_refactoring(source_tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Construit les paramètres de code review depuis un résultat de refactoring."""
    lang = result.get("language", "python").lower()
    code = (result.get("refactored_code", "") or "").strip() or _empty_code_placeholder(lang)
    return {
        "code": code,
        "language": lang,
        "review_standards": ["naming", "complexity", "security", "dry", "solid"],
        "severity_threshold": "warning",
        "context": "auto-delegated from code_refactoring — vérifier la qualité du refactoring",
    }


def _build_refactoring_params_from_review(source_tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Construit les paramètres de refactoring depuis un résultat de code review."""
    findings = result.get("findings", [])
    summary = "# Code Review Findings to Fix\n"
    for f in findings[:10]:
        if isinstance(f, dict):
            summary += f"- [{f.get('severity', 'info')}] {f.get('title', '')}: {f.get('description', '')}\n"
            if f.get("suggestion"):
                summary += f"  Suggestion: {f['suggestion']}\n"

    return {
        "code": summary,
        "language": result.get("language", "python"),
        "refactoring_type": "clean",
        "parameters": {"context": "auto-delegated from code_review (quality < 0.5)"},
    }


def _build_architecture_params_from_consistency(source_tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Construit les paramètres d'analyse architecturale depuis un consistency check."""
    issues = result.get("issues", [])
    summary = "# Consistency Check — Architectural Issues\n"
    for issue in issues[:10]:
        if isinstance(issue, dict):
            summary += f"- {issue.get('title', issue.get('message', str(issue)))}\n"

    return {
        "code": summary,
        "language": "python",
        "analysis_types": ["dependencies", "coupling", "cohesion", "patterns", "debt"],
        "context": "auto-delegated from repo_consistency_check",
    }


def _build_refactoring_params_from_architecture(source_tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Construit les paramètres de refactoring depuis une analyse architecturale."""
    issues = result.get("issues", [])
    summary = "# Architectural Issues to Fix\n"
    for i in issues[:10]:
        if isinstance(i, dict):
            summary += f"- [{i.get('severity', 'info')}] {i.get('title', '')}\n"
            if i.get("recommendation"):
                summary += f"  Recommandation: {i['recommendation']}\n"

    return {
        "code": summary,
        "language": result.get("language", "python"),
        "refactoring_type": "clean",
        "parameters": {"context": "auto-delegated from architecture_analysis (dette technique)"},
    }


def _build_impact_params_from_architecture(source_tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Construit les paramètres d'impact analysis depuis une analyse architecturale."""
    modules = []
    for issue in result.get("issues", []):
        if isinstance(issue, dict):
            modules.extend(issue.get("affected_modules", []))

    issues_summary = "; ".join(i.get("title", "") for i in result.get("issues", []) if isinstance(i, dict))

    return {
        "change_intent": f"Refactoring architectural recommandé: {issues_summary}"
        if issues_summary
        else "Refactoring architectural",
        "files": [{"path": m, "content": f"# Module impacté: {m}", "language": "python"} for m in modules[:10]]
        or [{"path": "unknown", "content": "# Aucun module identifié", "language": "python"}],
    }


def _build_performance_params_from_consistency(source_tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Construit les paramètres d'analyse performance depuis un consistency check."""
    issues = result.get("issues", [])
    summary = "# Consistency Check — Performance Issues\n"
    for issue in issues[:10]:
        if isinstance(issue, dict):
            summary += f"- {issue.get('title', issue.get('message', str(issue)))}\n"

    return {
        "code": summary,
        "language": "python",
        "analysis_categories": ["cpu", "memory", "io", "algorithmic"],
        "context": "auto-delegated from repo_consistency_check",
    }


def _build_refactoring_params_from_performance(source_tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Construit les paramètres de refactoring depuis une analyse performance."""
    issues = result.get("issues", [])
    summary = "# Performance Issues to Optimize\n"
    for i in issues[:10]:
        if isinstance(i, dict):
            summary += f"- [{i.get('severity', 'info')}] {i.get('title', '')}"
            if i.get("estimated_complexity"):
                summary += f" ({i['estimated_complexity']})"
            summary += "\n"
            if i.get("suggestion"):
                summary += f"  Suggestion: {i['suggestion']}\n"

    return {
        "code": summary,
        "language": result.get("language", "python"),
        "refactoring_type": "optimize",
        "parameters": {"context": "auto-delegated from performance_analysis"},
    }


def _build_test_params_from_performance(source_tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Construit les paramètres de test_generation depuis une analyse performance."""
    optimizations = result.get("optimizations", [])
    summary = "# Performance Optimizations to Test\n"
    for opt in optimizations[:10]:
        summary += f"- {opt}\n"

    return {
        "code": summary,
        "language": result.get("language", "python"),
        "test_framework": "pytest" if result.get("language", "python") == "python" else "jest",
        "coverage_target": 0.80,
        "parameters": {"context": "auto-delegated from performance_analysis"},
    }


def create_default_delegation_engine(
    max_chain_depth: int = 5,
    chain_timeout: float = 300.0,
) -> ExpertDelegationEngine:
    """Crée un ExpertDelegationEngine avec les règles par défaut.

    Matrice de délégation :
        repo_consistency_check → code_refactoring     (si score > 0.5)
        repo_consistency_check → architecture_analysis (si problèmes architecturaux)
        repo_consistency_check → performance_analysis  (si problèmes de performance)
        code_refactoring → code_documentation          (si changements > 0)
        code_refactoring → test_generation             (si changements > 0)
        code_refactoring → code_review                 (si changements > 0)
        code_review → code_refactoring                 (si quality_score < 0.5)
        architecture_analysis → code_refactoring       (si dette > 0.5)
        architecture_analysis → impact_analysis        (si issues critiques)
        performance_analysis → code_refactoring        (si perf_score < 0.5)
        performance_analysis → test_generation         (si optimisations proposées)
        impact_analysis → test_generation              (si risques > 0)
        impact_analysis → iac_guardrails_scan          (si fichiers IaC impactés)
        iac_guardrails_scan → code_refactoring         (si score sécurité < 0.5)
    """
    engine = ExpertDelegationEngine(
        max_chain_depth=max_chain_depth,
        chain_timeout=chain_timeout,
    )

    engine.register_rule(
        source_tool="repo_consistency_check",
        target_tool="code_refactoring",
        condition=_consistency_needs_refactoring,
        params_builder=_build_refactoring_params_from_consistency,
        condition_name="refactoring_score > 0.5",
        priority=5,
    )

    engine.register_rule(
        source_tool="code_refactoring",
        target_tool="code_documentation",
        condition=_refactoring_has_changes,
        params_builder=_build_documentation_params_from_refactoring,
        condition_name="changements effectués",
        priority=10,
    )

    engine.register_rule(
        source_tool="code_refactoring",
        target_tool="test_generation",
        condition=_refactoring_has_changes,
        params_builder=_build_test_params_from_refactoring,
        condition_name="changements effectués",
        priority=10,
    )

    engine.register_rule(
        source_tool="impact_analysis",
        target_tool="test_generation",
        condition=_impact_has_risks,
        params_builder=_build_test_params_from_impact,
        condition_name="risques détectés",
        priority=5,
    )

    engine.register_rule(
        source_tool="impact_analysis",
        target_tool="iac_guardrails_scan",
        condition=_impact_has_iac_files,
        params_builder=_build_iac_params_from_impact,
        condition_name="fichiers IaC impactés",
        priority=15,
    )

    engine.register_rule(
        source_tool="iac_guardrails_scan",
        target_tool="code_refactoring",
        condition=_iac_needs_remediation,
        params_builder=_build_refactoring_params_from_iac,
        condition_name="score sécurité < 0.5",
        priority=10,
    )

    # --- Phase 3: Nouveaux experts ---

    engine.register_rule(
        source_tool="code_refactoring",
        target_tool="code_review",
        condition=_refactoring_needs_review,
        params_builder=_build_review_params_from_refactoring,
        condition_name="changements effectués → revue qualité",
        priority=15,
    )

    engine.register_rule(
        source_tool="code_review",
        target_tool="code_refactoring",
        condition=_review_quality_low,
        params_builder=_build_refactoring_params_from_review,
        condition_name="quality_score < 0.5 → auto-correction",
        priority=5,
    )

    engine.register_rule(
        source_tool="repo_consistency_check",
        target_tool="architecture_analysis",
        condition=_consistency_has_architectural_issues,
        params_builder=_build_architecture_params_from_consistency,
        condition_name="problèmes architecturaux détectés",
        priority=10,
    )

    engine.register_rule(
        source_tool="architecture_analysis",
        target_tool="code_refactoring",
        condition=_architecture_has_debt,
        params_builder=_build_refactoring_params_from_architecture,
        condition_name="dette technique > 0.5",
        priority=5,
    )

    engine.register_rule(
        source_tool="architecture_analysis",
        target_tool="impact_analysis",
        condition=_architecture_needs_impact,
        params_builder=_build_impact_params_from_architecture,
        condition_name="issues critiques → évaluer impact",
        priority=10,
    )

    engine.register_rule(
        source_tool="repo_consistency_check",
        target_tool="performance_analysis",
        condition=_consistency_has_performance_issues,
        params_builder=_build_performance_params_from_consistency,
        condition_name="problèmes de performance détectés",
        priority=10,
    )

    engine.register_rule(
        source_tool="performance_analysis",
        target_tool="code_refactoring",
        condition=_performance_needs_refactoring,
        params_builder=_build_refactoring_params_from_performance,
        condition_name="performance_score < 0.5 → optimiser",
        priority=5,
    )

    engine.register_rule(
        source_tool="performance_analysis",
        target_tool="test_generation",
        condition=_performance_needs_tests,
        params_builder=_build_test_params_from_performance,
        condition_name="optimisations proposées → tester",
        priority=10,
    )

    return engine
