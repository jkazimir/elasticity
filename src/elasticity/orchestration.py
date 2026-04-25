"""Orchestration: the primary public API for loading and running orchestrations."""

import asyncio
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional

from .config.loader import load_config
from .config.global_loader import load_global_config
from .config.validator import validate_references
from .compiler.graph import GraphBuilder
from .runtime.executor import Executor, HumanApprovalFn, HumanApprovalResult
from .runtime.agent import ApprovalFn
from .runtime.tools import ToolRegistry
from .runtime.context import ContextManager
from .runtime.context_strategy import ContextStrategy, CognitiveStrategy
from .runtime.session import Session
from .runtime.input_handler import InputHandler
from .events import EventBus
from .tracing import RunTrace, write_chat_turn_log
from .errors import ValidationError
from .config.schema import CognitiveContextConfig, ToolDefinition
from .tools.builtins import list_builtin_tools

try:
    from .mcp import MCPRegistry
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False


class Orchestration:
    """Represents a loaded and compiled orchestration."""

    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        self.config = load_config(self.config_path)
        validate_references(self.config)

        # Merge global MCP servers (global first, per-orchestration wins on conflicts)
        global_cfg = load_global_config()
        if global_cfg.mcp_servers:
            merged = {**global_cfg.mcp_servers, **self.config.mcp_servers}
            self.config = self.config.model_copy(update={"mcp_servers": merged})

        # Build tool registry — register explicitly defined tools first
        self.tool_registry = ToolRegistry()
        for tool_name, tool_def in self.config.tools.items():
            self.tool_registry.register(tool_name, tool_def)

        # Implicitly register any builtin tool referenced by an agent but not explicitly defined
        valid_builtins = set(list_builtin_tools())
        explicitly_defined = set(self.config.tools.keys())
        implicitly_referenced: set[str] = set()
        for agent_def in self.config.agent_types.values():
            for tool_name in agent_def.tools:
                if tool_name in valid_builtins and tool_name not in explicitly_defined:
                    implicitly_referenced.add(tool_name)
        for tool_name in implicitly_referenced:
            self.tool_registry.register(
                tool_name, ToolDefinition(builtin=tool_name)
            )

        # Build graph builder
        self.graph_builder = GraphBuilder(self.config)

        # MCP registry (lazily populated on first run if mcp_servers is configured)
        self._mcp_registry: Optional["MCPRegistry"] = None

        # Cognitive context strategy (lazily built per orchestration)
        self._context_strategies: Dict[str, ContextStrategy] = {}

    @classmethod
    def from_file(cls, config_path: str) -> "Orchestration":
        """Load an orchestration from a YAML file."""
        return cls(config_path)

    def get_orchestration_names(self) -> list[str]:
        """Get list of available orchestration names."""
        return list(self.config.orchestrations.keys())

    def _validate_input_data(
        self,
        orchestration_name: str,
        orch_def: Any,
        input_data: Optional[Dict[str, Any]],
    ) -> None:
        """Validate input_data against the orchestration's declared input schema."""
        if not orch_def.input:
            return

        schema: Dict[str, str] = orch_def.input
        data = input_data or {}

        missing = [k for k in schema if k not in data]
        extra = [k for k in data if k not in schema]

        problems = []
        if missing:
            problems.append(f"Missing required input parameters: {', '.join(sorted(missing))}")
        if extra:
            problems.append(f"Unexpected input parameters: {', '.join(sorted(extra))}")

        if problems:
            raise ValidationError(
                f"Input validation failed for orchestration '{orchestration_name}':\n"
                + "\n".join(f"  - {p}" for p in problems)
            )

    def _build_context_strategy(
        self,
        orchestration_name: str,
        config: CognitiveContextConfig,
        event_bus: Optional[EventBus] = None,
    ) -> ContextStrategy:
        """Build (or reuse) a :class:`CognitiveStrategy` for the orchestration."""
        if orchestration_name in self._context_strategies:
            return self._context_strategies[orchestration_name]

        from .memory.embeddings import resolve_embedding_provider
        from .memory.vector_store import VectorStore

        db_path = config.memory_db_path
        if db_path is None:
            import platformdirs

            db_path = str(
                Path(platformdirs.user_data_dir("elasticity")) / "cognitive.db"
            )

        embedding_provider = resolve_embedding_provider(config.embedding_provider)
        vector_store = VectorStore(db_path)

        async def _llm_fn(model: str, messages: list, max_tokens: int) -> str:
            from .backends.registry import resolve_backend
            backend, model_name = resolve_backend(model)
            response = await backend.complete(model_name, messages, max_tokens=max_tokens)
            return response.content

        strategy = CognitiveStrategy(
            config=config,
            vector_store=vector_store,
            embedding_provider=embedding_provider,
            event_bus=event_bus,
            llm_fn=_llm_fn,
        )
        self._context_strategies[orchestration_name] = strategy
        return strategy

    async def run(
        self,
        orchestration_name: str,
        input_data: Optional[Dict[str, Any]] = None,
        trace: Optional[RunTrace] = None,
        event_bus: Optional[EventBus] = None,
        session: Optional[Session] = None,
        stream_responses: bool = False,
        approval_fn: Optional[ApprovalFn] = None,
        input_handler: Optional[InputHandler] = None,
        human_approval_fn: Optional[HumanApprovalFn] = None,
    ) -> Dict[str, Any]:
        """Run an orchestration.

        Args:
            orchestration_name: Name of orchestration to run
            input_data: Input data dictionary
            trace: Optional RunTrace for observability (subscribed to the event bus)
            event_bus: Optional EventBus to receive runtime events
            session: Optional session for conversational mode

        Returns:
            Final context state
        """
        if orchestration_name not in self.config.orchestrations:
            raise ValueError(f"Orchestration '{orchestration_name}' not found")

        orch_def = self.config.orchestrations[orchestration_name]
        self._validate_input_data(orchestration_name, orch_def, input_data)

        # Create or reuse event bus; subscribe trace if provided
        bus = event_bus or EventBus()
        if trace is not None:
            trace.subscribe_to(bus)

        # Start MCP servers if configured and not yet running
        if self.config.mcp_servers and _MCP_AVAILABLE:
            if self._mcp_registry is None:
                self._mcp_registry = MCPRegistry(self.config.mcp_servers)
                await self._mcp_registry.start()
                self._mcp_registry.register_tools(self.tool_registry)

        graph = self.graph_builder.build(orchestration_name)
        context = ContextManager(orch_def.communication)

        # Build cognitive context strategy if configured
        context_strategy = None
        if orch_def.context_strategy is not None:
            context_strategy = self._build_context_strategy(
                orchestration_name, orch_def.context_strategy, bus
            )

        executor = Executor(
            self.config,
            self.tool_registry,
            event_bus=bus,
            stream_responses=stream_responses,
            approval_fn=approval_fn,
            human_approval_fn=human_approval_fn,
            context_strategy=context_strategy,
        )

        try:
            result = await executor.execute(
                graph,
                orchestration_name,
                context,
                input_data=input_data,
                default_error_strategy=orch_def.error_strategy,
                session=session,
                input_handler=input_handler,
            )
        finally:
            # Stop MCP servers after batch runs; keep alive for conversational mode
            if self._mcp_registry is not None and orch_def.mode == "batch":
                await self._mcp_registry.stop()
                self._mcp_registry = None

        return result

    async def arun(
        self,
        orchestration_name: str,
        input_data: Optional[Dict[str, Any]] = None,
        trace: Optional[RunTrace] = None,
        event_bus: Optional[EventBus] = None,
        session: Optional[Session] = None,
        stream_responses: bool = False,
        approval_fn: Optional[ApprovalFn] = None,
        input_handler: Optional[InputHandler] = None,
        human_approval_fn: Optional[HumanApprovalFn] = None,
    ) -> Dict[str, Any]:
        """Async run - alias for run() for explicit async API."""
        return await self.run(
            orchestration_name,
            input_data=input_data,
            trace=trace,
            event_bus=event_bus,
            session=session,
            stream_responses=stream_responses,
            approval_fn=approval_fn,
            input_handler=input_handler,
            human_approval_fn=human_approval_fn,
        )

    def run_sync(
        self,
        orchestration_name: str,
        input_data: Optional[Dict[str, Any]] = None,
        trace: Optional[RunTrace] = None,
        event_bus: Optional[EventBus] = None,
        session: Optional[Session] = None,
        stream_responses: bool = False,
    ) -> Dict[str, Any]:
        """Run an orchestration synchronously (convenience method)."""
        return asyncio.run(
            self.run(
                orchestration_name,
                input_data=input_data,
                trace=trace,
                event_bus=event_bus,
                session=session,
                stream_responses=stream_responses,
            )
        )

    async def chat(
        self,
        orchestration_name: str,
        message: str,
        session: Optional[Session] = None,
        event_bus: Optional[EventBus] = None,
        stream_responses: bool = False,
        approval_fn: Optional[ApprovalFn] = None,
        input_handler: Optional[InputHandler] = None,
        human_approval_fn: Optional[HumanApprovalFn] = None,
    ) -> str:
        """Chat with a conversational orchestration.

        Args:
            orchestration_name: Name of orchestration to chat with
            message: User's message
            session: Session object (created if not provided)
            event_bus: Optional EventBus to receive runtime events
            approval_fn: Optional async callback invoked when a tool policy is
                ``"ask"``.  Receives ``(agent_name, tool_name, arguments)`` and
                returns ``True`` to allow or ``False`` to deny.  When ``None``
                (default) all ``"ask"`` policies are denied automatically.

        Returns:
            Assistant's response string
        """
        if orchestration_name not in self.config.orchestrations:
            raise ValueError(f"Orchestration '{orchestration_name}' not found")

        orch_def = self.config.orchestrations[orchestration_name]
        if orch_def.mode != "conversational":
            raise ValueError(
                f"Orchestration '{orchestration_name}' is not in conversational mode. "
                f"Set mode: conversational in the config."
            )

        if session is None:
            session = Session()

        # Determine the input key from the orchestration's schema
        input_key = "message"
        if orch_def.input:
            if "message" in orch_def.input:
                input_key = "message"
            elif len(orch_def.input) == 1:
                input_key = next(iter(orch_def.input))

        result = await self.run(
            orchestration_name,
            input_data={input_key: message},
            event_bus=event_bus,
            session=session,
            stream_responses=stream_responses,
            approval_fn=approval_fn,
            input_handler=input_handler,
            human_approval_fn=human_approval_fn,
        )

        # Extract response using response_key
        response_key = orch_def.response_key or "response"
        messages = result.get("messages") if isinstance(result.get("messages"), dict) else None
        if messages and response_key in messages:
            response = str(messages[response_key])
        elif response_key and response_key in result:
            response = str(result[response_key])
        elif result:
            response = str(result)
        else:
            response = ""

        session.add_turn(message, response)

        # Notify cognitive context strategy of the completed turn
        if orch_def.context_strategy is not None and orchestration_name in self._context_strategies:
            strategy = self._context_strategies[orchestration_name]
            await strategy.on_turn_complete(session, message, response, tool_calls=None)

        write_chat_turn_log(
            session_id=session.id,
            conversation_turns=len(session.message_history) // 2,
            orchestration=orchestration_name,
            message=message,
            response_length=len(response),
            result=result,
        )

        return response

    async def end_session(self, orchestration_name: str, session: Session) -> None:
        """Notify the context strategy that the chat session has ended.

        Triggers session-end promotion of medium-term memories to long-term.
        No-op if no context strategy is configured for this orchestration.
        """
        if orchestration_name in self._context_strategies:
            await self._context_strategies[orchestration_name].on_session_end(session)

    async def achat(
        self,
        orchestration_name: str,
        message: str,
        session: Optional[Session] = None,
        event_bus: Optional[EventBus] = None,
        stream_responses: bool = False,
        approval_fn: Optional[ApprovalFn] = None,
        input_handler: Optional[InputHandler] = None,
        human_approval_fn: Optional[HumanApprovalFn] = None,
    ) -> str:
        """Async chat - alias for chat() for explicit async API."""
        return await self.chat(
            orchestration_name,
            message,
            session=session,
            event_bus=event_bus,
            stream_responses=stream_responses,
            approval_fn=approval_fn,
            input_handler=input_handler,
            human_approval_fn=human_approval_fn,
        )

    def chat_sync(
        self,
        orchestration_name: str,
        message: str,
        session: Optional[Session] = None,
        event_bus: Optional[EventBus] = None,
        stream_responses: bool = False,
    ) -> str:
        """Chat with a conversational orchestration synchronously."""
        return asyncio.run(self.chat(orchestration_name, message, session, event_bus, stream_responses))
