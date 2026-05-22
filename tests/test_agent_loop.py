"""Tests pour AgentLoopMixin — boucle agentique des tools."""

import pytest

from collegue.tools.agent_loop import (
    AgentIteration,
    AgentLoopConfig,
    AgentLoopMixin,
    AgentLoopResult,
)

# ---------------------------------------------------------------------------
# Helpers — fake ctx & concrete agents for testing
# ---------------------------------------------------------------------------


class FakeCtx:
    """Simule le contexte FastMCP pour les tests."""

    def __init__(self, responses=None):
        self.responses = list(responses or ["default response"])
        self._call_index = 0
        self.infos = []
        self.progress = []

    async def sample(self, messages=None, system_prompt=None, temperature=0.7, max_tokens=2000, **kwargs):
        text = self.responses[min(self._call_index, len(self.responses) - 1)]
        self._call_index += 1

        class FakeResult:
            pass

        r = FakeResult()
        r.text = text
        return r

    async def info(self, msg):
        self.infos.append(msg)

    async def report_progress(self, progress, total):
        self.progress.append((progress, total))


class AlwaysValidAgent(AgentLoopMixin):
    """Agent qui valide toujours au premier coup."""

    async def validate_agent_output(self, output, context):
        return []

    async def assess_agent_quality(self, output, context):
        return 1.0

    async def build_agent_feedback(self, output, errors, quality, context):
        return "N/A"


class FailsThenSucceedsAgent(AgentLoopMixin):
    """Agent qui échoue à la première itération puis réussit à la seconde."""

    def __init__(self):
        self.agent_config = AgentLoopConfig(max_iterations=3)
        self._call_count = 0

    async def validate_agent_output(self, output, context):
        self._call_count += 1
        if self._call_count == 1:
            return ["Syntaxe invalide: ligne 5: unexpected indent"]
        return []

    async def assess_agent_quality(self, output, context):
        if self._call_count <= 1:
            return 0.3
        return 0.9

    async def build_agent_feedback(self, output, errors, quality, context):
        return f"Erreurs détectées: {'; '.join(errors)}. Corrige la syntaxe."


class AlwaysFailsAgent(AgentLoopMixin):
    """Agent qui échoue systématiquement (pour tester max_iterations)."""

    def __init__(self):
        self.agent_config = AgentLoopConfig(max_iterations=3)

    async def validate_agent_output(self, output, context):
        return ["Erreur persistante"]

    async def assess_agent_quality(self, output, context):
        return 0.2

    async def build_agent_feedback(self, output, errors, quality, context):
        return "Toujours en erreur."


class RegressingAgent(AgentLoopMixin):
    """Agent dont la qualité régresse entre itérations."""

    def __init__(self):
        self.agent_config = AgentLoopConfig(max_iterations=3, abort_on_regression=True)
        self._call_count = 0

    async def validate_agent_output(self, output, context):
        return ["Erreur"]

    async def assess_agent_quality(self, output, context):
        self._call_count += 1
        if self._call_count == 1:
            return 0.6
        return 0.3  # régression

    async def build_agent_feedback(self, output, errors, quality, context):
        return "Feedback"


class KeywordAgent(AgentLoopMixin):
    """Agent qui vérifie la présence d'un mot-clé dans la réponse."""

    def __init__(self, keyword="python"):
        self.agent_config = AgentLoopConfig(max_iterations=3)
        self.keyword = keyword

    async def validate_agent_output(self, output, context):
        if self.keyword not in output.lower():
            return [f"Le mot '{self.keyword}' n'apparaît pas dans la réponse"]
        return []

    async def assess_agent_quality(self, output, context):
        return 1.0 if self.keyword in output.lower() else 0.3

    async def build_agent_feedback(self, output, errors, quality, context):
        return f"Ta réponse doit mentionner '{self.keyword}'. Erreurs: {errors}"


# ---------------------------------------------------------------------------
# Tests des modèles Pydantic
# ---------------------------------------------------------------------------


class TestAgentLoopConfig:
    def test_defaults(self):
        config = AgentLoopConfig()
        assert config.max_iterations == 3
        assert config.improvement_threshold == 0.1
        assert config.abort_on_regression is True
        assert config.initial_temperature == 0.7
        assert config.temperature_decay == 0.15
        assert config.min_temperature == 0.2

    def test_custom_config(self):
        config = AgentLoopConfig(max_iterations=5, abort_on_regression=False)
        assert config.max_iterations == 5
        assert config.abort_on_regression is False

    def test_validation_bounds(self):
        with pytest.raises(Exception):
            AgentLoopConfig(max_iterations=0)
        with pytest.raises(Exception):
            AgentLoopConfig(max_iterations=11)


class TestAgentIteration:
    def test_creation(self):
        it = AgentIteration(
            iteration=1,
            validation_passed=True,
            quality_score=0.95,
        )
        assert it.iteration == 1
        assert it.validation_passed is True
        assert it.validation_errors == []
        assert it.quality_score == 0.95
        assert it.feedback_sent is None


class TestAgentLoopResult:
    def test_creation(self):
        result = AgentLoopResult(
            best_output="code refactoré",
            total_iterations=2,
            best_score=0.9,
            converged=True,
        )
        assert result.best_output == "code refactoré"
        assert result.total_iterations == 2
        assert result.converged is True


# ---------------------------------------------------------------------------
# Tests de la boucle agentique
# ---------------------------------------------------------------------------


class TestAgentLoopMixin:
    @pytest.mark.asyncio
    async def test_success_first_iteration(self):
        """La boucle sort en 1 itération si validation réussie."""
        agent = AlwaysValidAgent()
        ctx = FakeCtx(responses=["print('hello')"])

        result = await agent.agent_execute(
            initial_prompt="Refactorise ce code",
            system_prompt="Tu es un expert Python",
            ctx=ctx,
        )

        assert result.total_iterations == 1
        assert result.converged is True
        assert result.best_score == 1.0
        assert result.best_output == "print('hello')"

    @pytest.mark.asyncio
    async def test_success_second_iteration(self):
        """La boucle corrige après feedback et réussit en 2 itérations."""
        agent = FailsThenSucceedsAgent()
        ctx = FakeCtx(responses=["bad code", "good code"])

        result = await agent.agent_execute(
            initial_prompt="Refactorise ce code",
            system_prompt="Tu es un expert Python",
            ctx=ctx,
        )

        assert result.total_iterations == 2
        assert result.converged is True
        assert result.best_score == 0.9
        assert result.best_output == "good code"
        assert len(result.errors_fixed) > 0

    @pytest.mark.asyncio
    async def test_max_iterations_reached(self):
        """La boucle s'arrête après max_iterations et retourne le meilleur."""
        agent = AlwaysFailsAgent()
        agent.agent_config = AgentLoopConfig(max_iterations=3, abort_on_regression=False)
        ctx = FakeCtx(responses=["bad1", "bad2", "bad3"])

        result = await agent.agent_execute(
            initial_prompt="Refactorise",
            system_prompt="Expert",
            ctx=ctx,
        )

        assert result.total_iterations == 3
        assert result.converged is False
        assert result.best_output in ("bad1", "bad2", "bad3")

    @pytest.mark.asyncio
    async def test_abort_on_regression(self):
        """La boucle s'arrête si la qualité régresse."""
        agent = RegressingAgent()
        ctx = FakeCtx(responses=["output1", "output2_worse", "output3"])

        result = await agent.agent_execute(
            initial_prompt="Refactorise",
            system_prompt="Expert",
            ctx=ctx,
        )

        assert result.total_iterations == 2
        assert result.best_score == 0.6
        assert result.best_output == "output1"

    @pytest.mark.asyncio
    async def test_temperature_decreases(self):
        """La température diminue à chaque itération."""
        agent = AlwaysFailsAgent()
        agent.agent_config = AgentLoopConfig(
            max_iterations=3,
            initial_temperature=0.7,
            temperature_decay=0.15,
            min_temperature=0.2,
            abort_on_regression=False,
        )
        ctx = FakeCtx(responses=["a", "b", "c"])

        result = await agent.agent_execute(
            initial_prompt="test",
            system_prompt="test",
            ctx=ctx,
        )

        temps = [it.temperature_used for it in result.iterations]
        assert temps[0] == pytest.approx(0.7)
        assert temps[1] == pytest.approx(0.55)
        assert temps[2] == pytest.approx(0.4)

    @pytest.mark.asyncio
    async def test_temperature_min_floor(self):
        """La température ne descend pas en dessous du minimum."""
        agent = AlwaysFailsAgent()
        agent.agent_config = AgentLoopConfig(
            max_iterations=3,
            initial_temperature=0.4,
            temperature_decay=0.15,
            min_temperature=0.2,
            abort_on_regression=False,
        )
        ctx = FakeCtx(responses=["a", "b", "c"])

        result = await agent.agent_execute(
            initial_prompt="test",
            system_prompt="test",
            ctx=ctx,
        )

        temps = [it.temperature_used for it in result.iterations]
        assert temps[0] == pytest.approx(0.4)
        assert temps[1] == pytest.approx(0.25)
        assert temps[2] == pytest.approx(0.2)  # plancher

    @pytest.mark.asyncio
    async def test_one_shot_mode(self):
        """max_iterations=1 se comporte comme un one-shot classique."""
        agent = AlwaysFailsAgent()
        agent.agent_config = AgentLoopConfig(max_iterations=1)
        ctx = FakeCtx(responses=["output"])

        result = await agent.agent_execute(
            initial_prompt="test",
            system_prompt="test",
            ctx=ctx,
        )

        assert result.total_iterations == 1
        assert len(result.iterations) == 1

    @pytest.mark.asyncio
    async def test_keyword_agent_corrects(self):
        """L'agent à mot-clé corrige après feedback."""
        agent = KeywordAgent(keyword="python")
        ctx = FakeCtx(
            responses=[
                "Java is a great language",
                "Python is a great language",
            ]
        )

        result = await agent.agent_execute(
            initial_prompt="Décris un langage",
            system_prompt="Expert",
            ctx=ctx,
        )

        assert result.converged is True
        assert result.best_output == "Python is a great language"
        assert result.total_iterations == 2

    @pytest.mark.asyncio
    async def test_context_passed_through(self):
        """Le contexte est correctement passé aux méthodes de validation."""
        received_context = {}

        class ContextCapture(AgentLoopMixin):
            async def validate_agent_output(self, output, context):
                received_context.update(context)
                return []

            async def assess_agent_quality(self, output, context):
                return 1.0

            async def build_agent_feedback(self, output, errors, quality, context):
                return ""

        agent = ContextCapture()
        ctx = FakeCtx(responses=["output"])

        await agent.agent_execute(
            initial_prompt="test",
            system_prompt="test",
            ctx=ctx,
            context={"language": "python", "original_code": "x = 1"},
        )

        assert received_context["language"] == "python"
        assert received_context["original_code"] == "x = 1"

    @pytest.mark.asyncio
    async def test_progress_reported(self):
        """La progression est reportée via ctx.report_progress()."""
        agent = AlwaysValidAgent()
        ctx = FakeCtx(responses=["output"])

        await agent.agent_execute(
            initial_prompt="test",
            system_prompt="test",
            ctx=ctx,
        )

        assert len(ctx.progress) >= 2  # au moins start + end

    @pytest.mark.asyncio
    async def test_feedback_in_iterations(self):
        """Le feedback est enregistré dans l'historique des itérations."""
        agent = FailsThenSucceedsAgent()
        ctx = FakeCtx(responses=["bad", "good"])

        result = await agent.agent_execute(
            initial_prompt="test",
            system_prompt="test",
            ctx=ctx,
        )

        assert result.iterations[0].feedback_sent is not None
        assert "Syntaxe invalide" in result.iterations[0].feedback_sent

    @pytest.mark.asyncio
    async def test_not_implemented_errors(self):
        """Les méthodes abstraites lèvent NotImplementedError."""
        agent = AgentLoopMixin()
        with pytest.raises(NotImplementedError):
            await agent.validate_agent_output("output", {})
        with pytest.raises(NotImplementedError):
            await agent.assess_agent_quality("output", {})
        with pytest.raises(NotImplementedError):
            await agent.build_agent_feedback("output", [], 0.5, {})

    @pytest.mark.asyncio
    async def test_llm_error_handling(self):
        """La boucle gère les erreurs LLM sans crash."""

        class ErrorCtx:
            async def sample(self, **kwargs):
                raise ConnectionError("LLM unavailable")

            async def info(self, msg):
                pass

            async def report_progress(self, progress, total):
                pass

        agent = AlwaysValidAgent()
        ctx = ErrorCtx()

        result = await agent.agent_execute(
            initial_prompt="test",
            system_prompt="test",
            ctx=ctx,
        )

        assert result.total_iterations == 1
        assert result.converged is False
        assert "Erreur LLM" in result.iterations[0].validation_errors[0]
