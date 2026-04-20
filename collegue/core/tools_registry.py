"""Tool discovery + registry, decoupled from the orchestrator handler.

Rationale (issue #211)
----------------------
The previous implementation kept a mutable module-level ``_TOOLS_CACHE``
variable inside ``meta_orchestrator`` that was populated lazily by the first
request to ``smart_orchestrator``. That design had four downsides:

1. No lock around the initialisation — a burst of concurrent cold-start
   requests could populate the cache twice or corrupt it.
2. No way to refresh / invalidate without restarting the process.
3. The name "global" suggested cross-replica state; it was actually
   per-process, which confused contributors.
4. Tests had to reach into the module and monkey-patch the global to inject
   fake tools.

This module exposes two pieces of public API:

* :func:`discover_tools` — a pure-sync function that walks ``collegue.tools``,
  instantiates every ``BaseTool`` subclass once to extract its metadata and
  returns a ``{tool_name: {class, description, prompt_desc, schema}}`` dict.
* :class:`ToolsRegistry` — a thin wrapper around that dict that guards lazy
  rediscovery with an ``asyncio.Lock``, so callers that don't (or can't) rely
  on the lifespan-injected registry never race with each other.

The orchestrator reads the registry via ``ctx.lifespan_context['tools_registry']``
— the old ``_TOOLS_CACHE`` global is gone.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import pkgutil
from typing import Any, Dict

logger = logging.getLogger(__name__)

#: A discovered tool entry. ``class`` is the un-instantiated BaseTool subclass;
#: ``prompt_desc`` is a pre-formatted block ready to paste into the LLM prompt.
ToolEntry = Dict[str, Any]

#: Mapping from tool name (e.g. ``"code_documentation"``) to its entry.
ToolsRegistryDict = Dict[str, ToolEntry]

# The orchestrator itself must NOT be listed among the registered tools —
# otherwise the LLM would try to plan calls to itself.
_EXCLUDED_TOOL_NAMES = {"smart_orchestrator"}


def _build_prompt_desc(tool_name: str, description: str, schema: Dict[str, Any]) -> str:
    """Render the same prompt description format the orchestrator used inline."""
    props = schema.get("properties", {})
    required = schema.get("required", [])
    args_desc: list[str] = []
    for prop_name, prop_info in props.items():
        req_mark = "(REQUIS)" if prop_name in required else "(optionnel)"
        prop_type = prop_info.get("type", "any")
        prop_desc = prop_info.get("description", "")
        args_desc.append(f"    - {prop_name} ({prop_type}): {prop_desc} {req_mark}")
    formatted_args = "\n".join(args_desc)
    return f"{tool_name}: {description}\n  Arguments:\n{formatted_args}"


def discover_tools() -> ToolsRegistryDict:
    """Walk the ``collegue.tools`` package and return all registered tools.

    This function is synchronous on purpose: it is meant to be called once
    during ``core_lifespan`` startup (before ``yield``). Import failures of
    individual tool modules are logged and skipped; they do NOT abort
    discovery so one broken tool doesn't take down the whole orchestrator.
    """
    import collegue.tools as tools_pkg
    from collegue.tools.base import BaseTool

    registry: ToolsRegistryDict = {}

    for _, name, _ in pkgutil.iter_modules(tools_pkg.__path__):
        if name.startswith("_") or name == "base":
            continue

        try:
            module = importlib.import_module(f"collegue.tools.{name}")
        except Exception as exc:
            logger.warning("Tool module '%s' failed to import, skipping: %s", name, exc)
            continue

        for _, obj in inspect.getmembers(module):
            if not (
                inspect.isclass(obj)
                and issubclass(obj, BaseTool)
                and obj is not BaseTool
                and obj.__module__.startswith(module.__name__)
            ):
                continue

            instance = None
            try:
                instance = obj({})
                tool_name = instance.get_name()

                if tool_name in _EXCLUDED_TOOL_NAMES:
                    continue

                schema = instance.get_request_model().model_json_schema()
                registry[tool_name] = {
                    "class": obj,
                    "description": instance.get_description(),
                    "prompt_desc": _build_prompt_desc(
                        tool_name, instance.get_description(), schema
                    ),
                    "schema": schema,
                }
            except Exception as exc:
                logger.warning(
                    "Skipping tool class %s in module %s: %s",
                    obj.__name__, name, exc,
                )
            finally:
                # Release any resources held by the temporary instance so we
                # don't leak connection pools, file handles, etc.
                if instance is not None and hasattr(instance, "cleanup"):
                    try:
                        instance.cleanup()
                    except Exception:
                        pass

    logger.info("Tools registry discovered %d tool(s): %s",
                 len(registry), sorted(registry.keys()))
    return registry


class ToolsRegistry:
    """Asyncio-safe wrapper around a tools registry dict.

    The lifespan-injected registry is the primary path; this wrapper only
    matters for the degraded cases (tests, ad-hoc scripts) where the
    orchestrator is invoked without an initialised ``ctx.lifespan_context``.
    It guarantees that concurrent callers trigger at most one ``discover_tools``
    invocation.
    """

    def __init__(self, initial: ToolsRegistryDict | None = None):
        self._registry: ToolsRegistryDict | None = dict(initial) if initial else None
        self._lock = asyncio.Lock()

    @property
    def is_loaded(self) -> bool:
        return self._registry is not None

    async def get(self) -> ToolsRegistryDict:
        """Return the registry, discovering once if needed."""
        if self._registry is not None:
            return self._registry
        async with self._lock:
            # Double-checked locking: another coroutine may have populated it
            # while we were waiting on the lock.
            if self._registry is None:
                # ``discover_tools`` is sync; run it in a thread so we do not
                # stall the event loop during cold start.
                self._registry = await asyncio.to_thread(discover_tools)
            return self._registry

    def set(self, registry: ToolsRegistryDict) -> None:
        """Replace the backing dict (used by tests and by the lifespan)."""
        self._registry = dict(registry)

    def clear(self) -> None:
        """Force the next ``get()`` call to re-run discovery."""
        self._registry = None
