"""Minimal FastMCP-style context for running tool logic outside the server.

The LLM tools expect a ``ctx`` object with at least an async ``sample(messages,
system_prompt, temperature, max_tokens)`` that returns an object with a
``.text`` attribute. In production this is wired by FastMCP to the configured
sampling handler (Gemini via ``OpenAISamplingHandler``). For the evals runner
we need the same shape but without a running MCP server, so we call
``generate_text()`` directly — the same helper the Watchdog already uses.

Intentionally small: the tools we evaluate only touch ``ctx.sample``,
``ctx.info``, ``ctx.warning``, ``ctx.error``, ``ctx.report_progress`` — see
``collegue/tools/test_generation/tool.py`` for the full surface. Any missing
method will raise ``AttributeError`` which is loud enough to catch on first
run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from collegue.config import settings
from collegue.resources.llm.providers import LLMConfig, LLMResponse, generate_text


@dataclass
class _SampleResult:
    """Drop-in shape for what FastMCP hands back from ``ctx.sample()``."""

    text: str
    annotations: List[Dict[str, Any]] = field(default_factory=list)


class EvalContext:
    """Context that runs the tool against the real LLM without MCP plumbing.

    Keep it dumb: the eval runner owns the lifecycle, we're just a thin
    adapter. ``lifespan_context`` is an empty dict — tools that need
    something from the lifespan (parser, prompt_engine, ...) must tolerate
    that path being None, which they already do for the HTTP transport.
    """

    #: Floor below which we never go, regardless of what the tool asks for.
    #: Gemini 2.5 reasoning can consume ~1-2k tokens before emitting any
    #: output — if the tool asked for 2000, we'd get a truncated response.
    #: 8000 leaves enough headroom for reasoning + real output.
    MIN_MAX_TOKENS = 8000

    def __init__(
        self,
        temperature: float = 0.5,
        max_tokens: int = MIN_MAX_TOKENS,
        model: Optional[str] = None,
    ):
        self.lifespan_context: Dict[str, Any] = {}
        self._default_temperature = temperature
        self._default_max_tokens = max_tokens
        # ``model`` overrides the env default so the matrix runner can drive
        # a sweep without touching settings. Falls back to LLM_MODEL for the
        # common "run against whatever is configured" case.
        self._model = model or settings.LLM_MODEL
        self.calls: list[Dict[str, Any]] = []

    @property
    def model(self) -> str:
        return self._model

    async def sample(
        self,
        messages: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> _SampleResult:
        effective_max = max(max_tokens or self._default_max_tokens, self.MIN_MAX_TOKENS)
        config = LLMConfig(
            model_name=self._model,
            api_key=settings.LLM_API_KEY,
            max_tokens=effective_max,
            temperature=temperature if temperature is not None else self._default_temperature,
        )
        response: LLMResponse = await generate_text(config, messages, system_prompt=system_prompt)
        self.calls.append({
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "prompt_len": len(messages),
            "response_len": len(response.text or ""),
        })
        return _SampleResult(text=response.text or "", annotations=list(response.annotations or []))

    # --- noop log sinks so the tool can await on them ---------------------

    async def info(self, _message: str) -> None:
        return None

    async def warning(self, _message: str) -> None:
        return None

    async def error(self, _message: str) -> None:
        return None

    async def report_progress(self, *args: Any, **kwargs: Any) -> None:
        return None
