"""Tool registry and invocation."""

import asyncio
import functools
import importlib
import inspect
from typing import Any, Callable, Dict, List, Optional, Set
from ..config.schema import ToolDefinition, ParameterSchema
from ..errors import ToolError
from ..tools.builtins import get_builtin_tool

# Sentinel callable string used for MCP tools
_MCP_SENTINEL = "_mcp_"


class ToolRegistry:
    """Registry for tools that agents can invoke.

    Supports three kinds of tools:
    - Built-in tools (``builtin`` field in config)
    - Custom callables (``callable`` dotted path in config)
    - MCP tools (registered via ``register_mcp()``, invoked via MCPRegistry)
    """

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}
        self._callables: Dict[str, Callable] = {}
        self._initialized_modules: Set[str] = set()
        # Maps full MCP tool name -> MCPRegistry instance
        self._mcp_registries: Dict[str, Any] = {}

    def register(self, name: str, definition: ToolDefinition) -> None:
        """Register a tool definition.

        Args:
            name: Tool name
            definition: Tool definition

        Raises:
            ToolError: If builtin tool name is invalid
        """
        if definition.builtin:
            try:
                builtin_def = get_builtin_tool(definition.builtin)
                # Determine description: user override > _tool_describe(config) > builtin default
                description = definition.description
                if not description and definition.config:
                    module_path = builtin_def.callable.rsplit(".", 1)[0]
                    try:
                        module = importlib.import_module(module_path)
                        describe_fn = getattr(module, "_tool_describe", None)
                        if describe_fn is not None:
                            description = describe_fn(definition.config)
                    except Exception:
                        pass
                if not description:
                    description = builtin_def.description
                resolved_def = ToolDefinition(
                    description=description,
                    callable=builtin_def.callable,
                    parameters=builtin_def.parameters,
                    config=definition.config,
                )
                self._tools[name] = resolved_def
            except KeyError as e:
                raise ToolError(f"Unknown built-in tool '{definition.builtin}': {e}") from e
        else:
            self._tools[name] = definition

    def register_callable(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        fn: Callable,
    ) -> None:
        """Register a direct Python callable as a tool, bypassing dotted-path lookup.

        This is used internally to register team orchestrations as tools for
        the conductor agent.  The callable is pre-cached so ``load_callable``
        never attempts to import it as a module path.

        Args:
            name: Tool name exposed to the LLM.
            description: Human-readable description shown to the LLM.
            parameters: Dict of parameter name -> ParameterSchema.
            fn: Sync or async callable implementing the tool.
        """
        # model_construct skips Pydantic validation so we can omit the
        # normally-required 'callable' dotted-path field.
        definition = ToolDefinition.model_construct(
            description=description,
            callable="_direct_",
            builtin=None,
            parameters=parameters,
            config={},
        )
        self._tools[name] = definition
        self._callables[name] = fn

    def unregister(self, name: str) -> None:
        """Remove a tool from the registry.

        Safe to call for names that were never registered.  In-flight
        invocations already hold a reference to the callable so they complete
        normally even after unregistration.
        """
        self._tools.pop(name, None)
        self._callables.pop(name, None)
        self._mcp_registries.pop(name, None)

    def register_mcp(self, name: str, definition: ToolDefinition, mcp_registry: Any) -> None:
        """Register an MCP tool. Invocations are routed through ``mcp_registry``.

        Args:
            name: Full tool name (e.g., ``"github.search_repositories"``)
            definition: Tool definition with ``_mcp_`` sentinel callable
            mcp_registry: MCPRegistry instance to route calls through
        """
        self._tools[name] = definition
        self._mcp_registries[name] = mcp_registry

    def load_callable(self, name: str) -> Callable:
        """Load the callable for a tool.

        Raises:
            ToolError: If tool not found or callable cannot be loaded
        """
        if name not in self._tools:
            raise ToolError(f"Tool '{name}' not found")

        if name in self._callables:
            return self._callables[name]

        definition = self._tools[name]
        if not definition.callable:
            raise ToolError(f"Tool '{name}' has no callable defined")

        try:
            module_path, func_name = definition.callable.rsplit(".", 1)
            module = importlib.import_module(module_path)

            if module_path not in self._initialized_modules:
                init_fn = getattr(module, "_tool_init", None)
                if init_fn is not None:
                    init_fn(definition.config)
                self._initialized_modules.add(module_path)

            callable_func = getattr(module, func_name)
            # Inject tool config for callables that declare a _tool_config parameter
            sig = inspect.signature(callable_func)
            if '_tool_config' in sig.parameters:
                callable_func = functools.partial(callable_func, _tool_config=definition.config)
            self._callables[name] = callable_func
            return callable_func
        except Exception as e:
            raise ToolError(f"Failed to load callable for tool '{name}': {e}") from e

    def get_tool_schema(self, name: str) -> Dict[str, Any]:
        """Get the function-calling schema for a tool (OpenAI format)."""
        if name not in self._tools:
            raise ToolError(f"Tool '{name}' not found")

        definition = self._tools[name]
        properties = {}
        required = []

        for param_name, param_schema in definition.parameters.items():
            param_type_map = {
                "string": "string",
                "integer": "integer",
                "float": "number",
                "boolean": "boolean",
            }
            param_type = param_type_map.get(param_schema.type, "string")

            properties[param_name] = {
                "type": param_type,
                "description": param_schema.description or f"{param_name} parameter",
            }

            if param_schema.required:
                required.append(param_name)

        return {
            "type": "function",
            "function": {
                "name": name,
                "description": definition.description or "",
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def get_available_tools(self) -> List[str]:
        """Get list of available tool names."""
        return list(self._tools.keys())

    def invoke(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Invoke a tool with arguments.

        For MCP tools, this runs the async call synchronously (blocking).
        Regular callable tools are called directly.

        Args:
            name: Tool name
            arguments: Tool arguments

        Returns:
            Tool result

        Raises:
            ToolError: If tool invocation fails
        """
        if name not in self._tools:
            raise ToolError(f"Tool '{name}' not found")

        definition = self._tools[name]
        # Work on a copy so we don't mutate the caller's dict.
        arguments = {**arguments}

        # Validate and fill defaults
        for param_name, param_schema in definition.parameters.items():
            if param_name not in arguments:
                if param_schema.required:
                    raise ToolError(f"Missing required parameter '{param_name}' for tool '{name}'")
                elif param_schema.default is not None:
                    arguments[param_name] = param_schema.default

        # MCP tool: route through the MCPRegistry asynchronously
        if name in self._mcp_registries:
            return self._invoke_mcp(name, arguments)

        # Regular callable tool
        try:
            callable_func = self.load_callable(name)
            return callable_func(**arguments)
        except Exception as e:
            raise ToolError(f"Tool '{name}' invocation failed: {e}") from e

    def _invoke_mcp(self, name: str, arguments: Dict[str, Any]) -> str:
        """Invoke an MCP tool synchronously (only safe outside an async context)."""
        # Detect if we're already inside a running event loop. Blocking on a
        # concurrent.futures.Future from within the same loop deadlocks because
        # the loop thread is occupied waiting and cannot run the scheduled task.
        try:
            asyncio.get_running_loop()
            raise ToolError(
                f"MCP tool '{name}' cannot be invoked synchronously from an async context. "
                "Use invoke_async() instead."
            )
        except RuntimeError:
            pass  # No running loop — safe to use asyncio.run()

        mcp_registry = self._mcp_registries[name]
        definition = self._tools[name]
        server_name = definition.config.get("_mcp_server", "")
        tool_name = definition.config.get("_mcp_tool", "")
        try:
            return asyncio.run(mcp_registry.call_tool(server_name, tool_name, arguments))
        except Exception as e:
            raise ToolError(f"MCP tool '{name}' invocation failed: {e}") from e

    async def invoke_async(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Async version of invoke. Preferred for MCP tools in async contexts."""
        if name not in self._tools:
            raise ToolError(f"Tool '{name}' not found")

        definition = self._tools[name]
        # Work on a copy so we don't mutate the caller's dict.
        arguments = {**arguments}

        for param_name, param_schema in definition.parameters.items():
            if param_name not in arguments:
                if param_schema.required:
                    raise ToolError(f"Missing required parameter '{param_name}' for tool '{name}'")
                elif param_schema.default is not None:
                    arguments[param_name] = param_schema.default

        if name in self._mcp_registries:
            mcp_registry = self._mcp_registries[name]
            server_name = definition.config.get("_mcp_server", "")
            tool_name = definition.config.get("_mcp_tool", "")
            return await mcp_registry.call_tool(server_name, tool_name, arguments)

        try:
            callable_func = self.load_callable(name)
            result = callable_func(**arguments)
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception as e:
            raise ToolError(f"Tool '{name}' invocation failed: {e}") from e
