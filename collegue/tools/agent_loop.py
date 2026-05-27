"""
Agent Loop — Mixin pour transformer les tools one-shot en agents itératifs.

Ce mixin ajoute une boucle perception-action aux tools qui utilisent un LLM :
  Prompt → LLM → Validation → [échec?] → Feedback → Re-prompt → ... → Output

Chaque tool qui adopte ce mixin implémente ses propres critères de validation
et de qualité via les méthodes abstraites validate_output(), assess_quality()
et build_feedback().
"""

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("agent_loop")


class AgentLoopConfig(BaseModel):
    """Configuration de la boucle agentique."""

    max_iterations: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Nombre maximum d'itérations (1 = one-shot classique)",
    )
    improvement_threshold: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description="Amélioration minimale attendue entre itérations",
    )
    abort_on_regression: bool = Field(
        default=True,
        description="Arrêter si la qualité régresse entre deux itérations",
    )
    initial_temperature: float = Field(
        default=0.7,
        ge=0.0,
        le=2.0,
        description="Température initiale du LLM (décroît à chaque itération)",
    )
    temperature_decay: float = Field(
        default=0.15,
        ge=0.0,
        le=0.5,
        description="Réduction de température par itération",
    )
    min_temperature: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        description="Température minimale (plancher)",
    )


class AgentIteration(BaseModel):
    """Résultat d'une itération de la boucle agentique."""

    iteration: int = Field(..., description="Numéro de l'itération (1-indexed)")
    validation_passed: bool = Field(..., description="True si la validation a réussi")
    validation_errors: List[str] = Field(
        default_factory=list,
        description="Erreurs de validation détectées",
    )
    quality_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Score de qualité de l'output (0.0-1.0)",
    )
    temperature_used: float = Field(
        default=0.7,
        description="Température LLM utilisée pour cette itération",
    )
    feedback_sent: Optional[str] = Field(
        default=None,
        description="Feedback envoyé au LLM pour la prochaine itération",
    )


class AgentLoopResult(BaseModel):
    """Résultat complet de la boucle agentique."""

    best_output: str = Field(..., description="Meilleur output produit")
    iterations: List[AgentIteration] = Field(
        default_factory=list,
        description="Historique de toutes les itérations",
    )
    total_iterations: int = Field(
        default=1,
        description="Nombre total d'itérations effectuées",
    )
    best_score: float = Field(
        default=0.0,
        description="Meilleur score de qualité atteint",
    )
    converged: bool = Field(
        default=False,
        description="True si la boucle a convergé (validation réussie)",
    )
    errors_fixed: List[str] = Field(
        default_factory=list,
        description="Erreurs corrigées au cours des itérations",
    )


class AgentLoopMixin:
    """Mixin qui ajoute le comportement agentique à un BaseTool.

    Pour utiliser ce mixin, un tool doit :
    1. Hériter de AgentLoopMixin en plus de BaseTool
    2. Définir un attribut agent_config (ou utiliser le défaut)
    3. Implémenter validate_agent_output(), assess_agent_quality(), build_agent_feedback()
    4. Appeler self.agent_execute() dans _execute_core_logic_async()
    """

    agent_config: AgentLoopConfig = AgentLoopConfig()

    async def agent_execute(
        self,
        initial_prompt: str,
        system_prompt: str,
        ctx: Any,
        context: Optional[Dict[str, Any]] = None,
        max_tokens: int = 2000,
    ) -> AgentLoopResult:
        """Boucle agentique : prompt → LLM → validate → feedback → re-prompt.

        Args:
            initial_prompt: Le prompt initial à envoyer au LLM.
            system_prompt: Le system prompt (rôle du LLM).
            ctx: Le contexte FastMCP (pour ctx.sample, ctx.info, etc.).
            context: Dictionnaire de contexte passé aux méthodes de validation.
            max_tokens: Nombre maximum de tokens pour la réponse LLM.

        Returns:
            AgentLoopResult avec le meilleur output et l'historique des itérations.
        """
        context = context or {}
        iterations: List[AgentIteration] = []
        best_output = ""
        best_score = -1.0
        current_prompt = initial_prompt
        errors_fixed: List[str] = []
        previous_errors: List[str] = []

        config = self.agent_config

        for i in range(config.max_iterations):
            temperature = max(
                config.min_temperature,
                config.initial_temperature - i * config.temperature_decay,
            )

            if ctx:
                if hasattr(ctx, "report_progress"):
                    await ctx.report_progress(progress=i, total=config.max_iterations)
                if config.max_iterations > 1:
                    await ctx.info(f"🔄 Itération {i + 1}/{config.max_iterations} (température: {temperature:.2f})")

            # 1. Appel LLM
            try:
                sample_kwargs: Dict[str, Any] = {
                    "messages": current_prompt,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if system_prompt is not None:
                    sample_kwargs["system_prompt"] = system_prompt
                result = await ctx.sample(**sample_kwargs)
                raw_output = result.text or ""
            except Exception as e:
                logger.error(f"Erreur LLM à l'itération {i + 1}: {e}")
                iteration = AgentIteration(
                    iteration=i + 1,
                    validation_passed=False,
                    validation_errors=[f"Erreur LLM: {e}"],
                    quality_score=0.0,
                    temperature_used=temperature,
                )
                iterations.append(iteration)
                break

            # 2. Validation
            errors = await self.validate_agent_output(raw_output, context)

            # 3. Évaluation de la qualité
            quality = await self.assess_agent_quality(raw_output, context)
            quality = max(0.0, min(1.0, quality))

            iteration = AgentIteration(
                iteration=i + 1,
                validation_passed=len(errors) == 0,
                validation_errors=errors,
                quality_score=quality,
                temperature_used=temperature,
            )

            # 4. Tracking des erreurs corrigées
            if i > 0 and previous_errors:
                fixed = [e for e in previous_errors if e not in errors]
                errors_fixed.extend(fixed)

            previous_errors = errors[:]

            # 5. Tracking du meilleur résultat
            if quality > best_score:
                best_score = quality
                best_output = raw_output
            elif config.abort_on_regression and i > 0:
                if ctx:
                    await ctx.info(
                        f"⚠️ Qualité en régression ({quality:.2f} < {best_score:.2f}), "
                        f"arrêt et retour du meilleur résultat."
                    )
                iterations.append(iteration)
                break

            # 6. Succès → sortir
            success_threshold = 1.0 - config.improvement_threshold
            if len(errors) == 0 and quality >= success_threshold:
                if ctx and config.max_iterations > 1:
                    await ctx.info(f"✅ Validation réussie à l'itération {i + 1} (score: {quality:.2f})")
                iterations.append(iteration)
                break

            # 7. Construire le feedback pour la prochaine itération
            if i < config.max_iterations - 1:
                feedback = await self.build_agent_feedback(raw_output, errors, quality, context)
                iteration.feedback_sent = feedback
                current_prompt = (
                    f"{initial_prompt}\n\n"
                    f"## FEEDBACK DE L'ITÉRATION {i + 1}\n"
                    f"Score de qualité: {quality:.2f}/1.0\n\n"
                    f"{feedback}\n\n"
                    f"Corrige les problèmes ci-dessus et régénère ta réponse."
                )
                if ctx:
                    await ctx.info(f"📝 {len(errors)} erreur(s) détectée(s), envoi du feedback pour correction...")

            iterations.append(iteration)

        # Progression finale
        if ctx and hasattr(ctx, "report_progress"):
            await ctx.report_progress(
                progress=config.max_iterations,
                total=config.max_iterations,
            )

        converged = any(it.validation_passed for it in iterations)

        return AgentLoopResult(
            best_output=best_output,
            iterations=iterations,
            total_iterations=len(iterations),
            best_score=best_score,
            converged=converged,
            errors_fixed=errors_fixed,
        )

    # --- Hooks mémoire projet ---

    def _store_to_memory(
        self,
        entry_type: str,
        category: str,
        title: str,
        data: Dict[str, Any],
        score: float = 0.0,
        file_path: Optional[str] = None,
        language: Optional[str] = None,
    ) -> None:
        """Stocke une entrée dans la mémoire projet (si disponible)."""
        try:
            from ..core.project_memory import get_project_memory

            memory = get_project_memory()
            tool_name = getattr(self, "tool_name", self.__class__.__name__)
            memory.store(
                expert=tool_name,
                entry_type=entry_type,
                category=category,
                title=title,
                data=data,
                score=score,
                file_path=file_path,
                language=language,
            )
        except Exception as exc:
            logger.debug("Mémoire projet non disponible: %s", exc)

    def _recall_from_memory(self, language: Optional[str] = None) -> Dict[str, Any]:
        """Rappelle le contexte mémoire pour cet expert."""
        try:
            from ..core.project_memory import get_project_memory

            memory = get_project_memory()
            tool_name = getattr(self, "tool_name", self.__class__.__name__)
            return memory.get_context_for(tool_name, language=language)
        except Exception:
            return {}

    # --- Méthodes abstraites à implémenter par chaque tool ---

    async def validate_agent_output(self, output: str, context: Dict[str, Any]) -> List[str]:
        """Valide l'output du LLM. Retourne une liste d'erreurs (vide = succès).

        À implémenter par chaque tool. Exemples de validations :
        - Vérification syntaxique (AST parse)
        - Vérification de couverture (nombre de tests, éléments documentés)
        - Vérification de format (JSON valide, markdown correct)

        Args:
            output: La réponse brute du LLM.
            context: Dictionnaire avec les données contextuelles du tool.

        Returns:
            Liste d'erreurs. Liste vide = validation réussie.
        """
        raise NotImplementedError(f"{self.__class__.__name__} doit implémenter validate_agent_output()")

    async def assess_agent_quality(self, output: str, context: Dict[str, Any]) -> float:
        """Évalue la qualité de l'output. Retourne un score 0.0-1.0.

        À implémenter par chaque tool. Le score doit refléter :
        - 0.0 : output inutilisable
        - 0.5 : output utilisable mais avec des défauts
        - 1.0 : output parfait

        Args:
            output: La réponse brute du LLM.
            context: Dictionnaire avec les données contextuelles du tool.

        Returns:
            Score de qualité entre 0.0 et 1.0.
        """
        raise NotImplementedError(f"{self.__class__.__name__} doit implémenter assess_agent_quality()")

    async def build_agent_feedback(
        self,
        output: str,
        errors: List[str],
        quality: float,
        context: Dict[str, Any],
    ) -> str:
        """Construit un feedback pour corriger l'output à la prochaine itération.

        À implémenter par chaque tool. Le feedback doit être :
        - Spécifique (quelles erreurs corriger)
        - Actionnable (comment les corriger)
        - Concis (pas de répétition du prompt initial)

        Args:
            output: La réponse brute du LLM.
            errors: Les erreurs de validation détectées.
            quality: Le score de qualité.
            context: Dictionnaire avec les données contextuelles du tool.

        Returns:
            Texte de feedback à ajouter au prompt pour la prochaine itération.
        """
        raise NotImplementedError(f"{self.__class__.__name__} doit implémenter build_agent_feedback()")
