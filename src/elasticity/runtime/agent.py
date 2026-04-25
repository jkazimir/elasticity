"""Agent runner with backend abstraction."""

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

InterruptCheckFn = Callable[[], None]  # Raises OrchestrationInterrupted if cancel

from ..config.schema import AgentTypeDefinition
from ..runtime.tools import ToolRegistry
from ..runtime.context import ContextManager
from ..runtime.context_strategy import ContextStrategy
from ..runtime.session import Session
from ..errors import AgentError
from ..backends import resolve_backend, ToolSpec
from ..events import EventBus, AgentStarted, AgentCompleted, AgentToken, ToolCalled, ToolResult, ToolApprovalRequested, ToolDenied

# Async callable: (agent_name, tool_name, arguments) -> approved?
ApprovalFn = Callable[[str, str, Dict[str, Any]], Awaitable[bool]]


class AgentRunner:
    """Runs agents using backend abstraction."""

    def __init__(
        self,
        tool_registry: ToolRegistry,
        event_bus: Optional[EventBus] = None,
        approval_fn: Optional[ApprovalFn] = None,
        semaphore: Optional[asyncio.Semaphore] = None,
        context_strategy: Optional[ContextStrategy] = None,
    ):
        self.tool_registry = tool_registry
        self._events = event_bus or EventBus()
        self._approval_fn = approval_fn
        self._semaphore = semaphore
        self._context_strategy = context_strategy
        self.stream_responses: bool = False
        # When True, emit AgentStarted/AgentCompleted once per LLM call round that
        # produces text output.  Used by the Conductor so that each director message
        # gets its own chat bubble without the executor wrapping duplicate events.
        self.emit_agent_events: bool = False

    async def run(
        self,
        agent_type: AgentTypeDefinition,
        agent_name: str,
        input_text: Optional[str],
        context: ContextManager,
        session: Optional[Session] = None,
        step_id: str = "",
        interrupt_check: Optional[InterruptCheckFn] = None,
    ) -> Dict[str, Any]:
        """Run an agent.

        Args:
            agent_type: Agent type definition
            agent_name: Name of the agent type (for logging)
            input_text: Input text for the agent
            context: Context manager for accessing shared context/messages
            session: Optional session for conversational mode
            step_id: Step identifier for event correlation

        Returns:
            Dictionary with 'content' (response text) and optionally 'tool_calls'

        Raises:
            AgentError: If agent execution fails
        """
        # Build messages
        messages = []

        # System prompt with rules
        system_prompt = agent_type.system_prompt
        if agent_type.rules:
            rules_text = "\n".join(f"- {rule}" for rule in agent_type.rules)
            system_prompt = f"{system_prompt}\n\nRules:\n{rules_text}"

        # Instruct the LLM to return structured JSON when output_schema is defined
        if agent_type.output_schema:
            fields_list = "\n".join(
                f"- {name} ({type_hint})"
                for name, type_hint in agent_type.output_schema.items()
            )
            schema_example = json.dumps(
                {name: f"<{type_hint}>" for name, type_hint in agent_type.output_schema.items()}
            )
            system_prompt += (
                f"\n\nIMPORTANT: Your final response MUST be ONLY a valid JSON object — no prose, "
                f"no markdown headers, no explanation before or after.\n"
                f"Required fields:\n{fields_list}\n"
                f"Example: {schema_example}\n"
                f"Complete all tool calls first, then output ONLY the JSON object."
            )

        messages.append({"role": "system", "content": system_prompt})

        # Prepend conversation history if session is provided
        if session:
            if self._context_strategy:
                history = await self._context_strategy.build_context(session, input_text)
            else:
                history = session.get_history()
            messages.extend(history)

        # User input
        if input_text:
            messages.append({"role": "user", "content": input_text})

        # Build tools if agent has any
        tools = None
        if agent_type.tools:
            tools = []
            for tool_name in agent_type.tools:
                try:
                    # Get tool schema dict and convert to ToolSpec
                    tool_schema_dict = self.tool_registry.get_tool_schema(tool_name)
                    function_spec = tool_schema_dict["function"]
                    tool_spec = ToolSpec(
                        name=function_spec["name"],
                        description=function_spec["description"],
                        parameters=function_spec["parameters"],
                    )
                    tools.append(tool_spec)
                except Exception as e:
                    raise AgentError(f"Failed to get tool schema for '{tool_name}': {e}") from e

        try:
            # Resolve backend and model name
            backend, model_name = resolve_backend(agent_type.model)

            # Request JSON output at the API level when output_schema is defined
            response_format = {"type": "json_object"} if agent_type.output_schema else None

            if self.stream_responses:
                return await self._run_streaming(
                    backend, model_name, agent_type, agent_name, step_id,
                    messages, tools, response_format,
                    interrupt_check=interrupt_check,
                )
            else:
                return await self._run_complete(
                    backend, model_name, agent_type, agent_name, step_id,
                    messages, tools, response_format,
                )

        except Exception as e:
            raise AgentError(f"Agent execution failed: {e}") from e

    async def _run_complete(
        self,
        backend: Any,
        model_name: str,
        agent_type: AgentTypeDefinition,
        agent_name: str,
        step_id: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[ToolSpec]],
        response_format: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Non-streaming execution path using backend.complete()."""
        max_tool_rounds = agent_type.max_tool_rounds
        final_content = ""
        all_tool_calls = []
        truncation_recoveries = 0
        last_stop_reason: Optional[str] = None
        last_usage = None

        for _round in range(max_tool_rounds):
            response = await backend.complete(
                model=model_name,
                messages=messages,
                tools=tools,
                max_tokens=agent_type.max_tokens,
                response_format=response_format,
            )

            sr = response.stop_reason or ""
            last_stop_reason = sr
            if response.usage:
                last_usage = response.usage

            if response.content:
                final_content = response.content

            if response.stop_reason in ("max_tokens", "length") and response.tool_calls:
                truncation_recoveries += 1
                # Response was truncated mid-tool-call; don't execute the broken call.
                # Inject a recovery message so the LLM can retry with shorter content.
                messages.append({"role": "assistant", "content": response.content or ""})
                messages.append({
                    "role": "user",
                    "content": (
                        "Your previous response was truncated because it exceeded the maximum "
                        "token limit. Your tool call was incomplete and could not be executed. "
                        "Please try again with shorter content — for example, split large files "
                        "into smaller parts, or write less content at once."
                    ),
                })
                continue

            if not response.tool_calls:
                break

            messages, tool_records = await self._execute_tool_calls(
                response.tool_calls, response.content or "", agent_name, step_id, messages,
                agent_type.tool_policies,
            )
            all_tool_calls.extend(tool_records)

        result = {
            "content": final_content,
            "tool_calls": all_tool_calls,
            "stop_reason": last_stop_reason or "",
            "truncation_recoveries": truncation_recoveries,
        }
        if last_usage:
            result["input_tokens"] = last_usage.input_tokens
            result["output_tokens"] = last_usage.output_tokens
            result["cache_read_tokens"] = last_usage.cache_read_tokens
            result["cache_creation_tokens"] = last_usage.cache_creation_tokens
        return result

    async def _run_streaming(
        self,
        backend: Any,
        model_name: str,
        agent_type: AgentTypeDefinition,
        agent_name: str,
        step_id: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[ToolSpec]],
        response_format: Optional[Dict[str, Any]],
        interrupt_check: Optional[InterruptCheckFn] = None,
    ) -> Dict[str, Any]:
        """Streaming execution path using backend.stream(), emitting AgentToken events."""
        max_tool_rounds = agent_type.max_tool_rounds
        final_content = ""
        all_tool_calls = []
        token_count = 0
        INTERRUPT_CHECK_INTERVAL = 10
        truncation_recoveries = 0
        last_stop_reason: Optional[str] = None
        last_usage = None

        for _round in range(max_tool_rounds):
            accumulated_text = ""
            accumulated_tool_calls = []
            token_count = 0
            _round_started = False  # tracks whether AgentStarted was emitted this round
            _round_t0 = time.monotonic()
            truncated = False
            round_usage = None

            async for chunk in backend.stream(
                model=model_name,
                messages=messages,
                tools=tools,
                max_tokens=agent_type.max_tokens,
                response_format=response_format,
            ):
                if chunk.done:
                    truncated = chunk.truncated
                    if chunk.usage:
                        round_usage = chunk.usage
                    break
                if chunk.delta:
                    accumulated_text += chunk.delta
                    token_count += 1
                    if interrupt_check and token_count % INTERRUPT_CHECK_INTERVAL == 0:
                        interrupt_check()
                    # Emit AgentStarted just before the first token of this round so
                    # that the frontend can open a new bubble in sequence.
                    if self.emit_agent_events and not _round_started:
                        self._events.emit(AgentStarted(
                            step_id=step_id,
                            agent_name=agent_name,
                        ))
                        _round_started = True
                    self._events.emit(AgentToken(
                        step_id=step_id,
                        agent_name=agent_name,
                        token=chunk.delta,
                    ))
                if chunk.tool_call:
                    accumulated_tool_calls.append(chunk.tool_call)

            if round_usage:
                last_usage = round_usage

            round_stop = "max_tokens" if truncated else ""
            if accumulated_text:
                final_content = accumulated_text
                if self.emit_agent_events and _round_started:
                    self._events.emit(AgentCompleted(
                        step_id=step_id,
                        agent_name=agent_name,
                        output=accumulated_text,
                        duration_ms=(time.monotonic() - _round_t0) * 1000,
                        stop_reason=round_stop,
                        output_chars=len(accumulated_text),
                        truncation_recoveries=truncation_recoveries,
                        input_tokens=round_usage.input_tokens if round_usage else 0,
                        output_tokens=round_usage.output_tokens if round_usage else 0,
                        cache_read_tokens=round_usage.cache_read_tokens if round_usage else 0,
                        cache_creation_tokens=round_usage.cache_creation_tokens if round_usage else 0,
                    ))

            if truncated:
                truncation_recoveries += 1
                # Response was truncated due to max_tokens. Inject a recovery message so
                # the LLM can retry with shorter content. Any tool calls accumulated before
                # truncation are discarded (the backend already skips malformed ones).
                messages.append({"role": "assistant", "content": accumulated_text})
                messages.append({
                    "role": "user",
                    "content": (
                        "Your previous response was truncated because it exceeded the maximum "
                        "token limit. Your tool call was incomplete and could not be executed. "
                        "Please try again with shorter content — for example, split large files "
                        "into smaller parts, or write less content at once."
                    ),
                })
                continue

            if not accumulated_tool_calls:
                last_stop_reason = round_stop or "stop"
                break

            messages, tool_records = await self._execute_tool_calls(
                accumulated_tool_calls, accumulated_text, agent_name, step_id, messages,
                agent_type.tool_policies,
            )
            all_tool_calls.extend(tool_records)

        result = {
            "content": final_content,
            "tool_calls": all_tool_calls,
            "stop_reason": last_stop_reason or "",
            "truncation_recoveries": truncation_recoveries,
        }
        if last_usage:
            result["input_tokens"] = last_usage.input_tokens
            result["output_tokens"] = last_usage.output_tokens
            result["cache_read_tokens"] = last_usage.cache_read_tokens
            result["cache_creation_tokens"] = last_usage.cache_creation_tokens
        return result

    async def _run_single_tool(
        self,
        tool_call: Any,
        agent_name: str,
        step_id: str,
    ) -> Dict[str, Any]:
        """Execute one allowed tool call, gated by self._semaphore if set."""
        tool_name = tool_call.name
        tool_args = tool_call.arguments

        self._events.emit(ToolCalled(
            step_id=step_id,
            agent_name=agent_name,
            tool_name=tool_name,
            arguments=tool_args,
        ))
        t0 = time.monotonic()
        try:
            if self._semaphore is not None:
                async with self._semaphore:
                    tool_result = await self.tool_registry.invoke_async(tool_name, tool_args)
            else:
                tool_result = await self.tool_registry.invoke_async(tool_name, tool_args)

            duration_ms = (time.monotonic() - t0) * 1000
            self._events.emit(ToolResult(
                step_id=step_id,
                agent_name=agent_name,
                tool_name=tool_name,
                result=str(tool_result)[:500],
                duration_ms=duration_ms,
            ))
            return {"tool_call_id": tool_call.id, "name": tool_name, "result": tool_result}
        except Exception as e:
            duration_ms = (time.monotonic() - t0) * 1000
            error_msg = f"Error: {e}"
            self._events.emit(ToolResult(
                step_id=step_id,
                agent_name=agent_name,
                tool_name=tool_name,
                result=error_msg[:500],
                duration_ms=duration_ms,
            ))
            return {"tool_call_id": tool_call.id, "name": tool_name, "result": error_msg}

    async def _execute_tool_calls(
        self,
        tool_calls: List[Any],
        assistant_content: str,
        agent_name: str,
        step_id: str,
        messages: List[Dict[str, Any]],
        tool_policies: Optional[Dict[str, str]] = None,
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Execute a set of tool calls, emit events, and return updated messages + records.

        For each tool call, the effective policy is resolved from ``tool_policies``
        (defaulting to ``"allow"``):

        - ``"allow"`` -- invoke concurrently with other allowed calls (gated by self._semaphore).
        - ``"deny"``  -- block without invoking; return a denial message to the LLM.
        - ``"ask"``   -- call ``self._approval_fn``; deny if no callback is registered
                         (i.e., non-interactive / batch mode).

        Phase 1 (sequential): resolve all policies — "ask" requires awaiting approval_fn.
        Phase 2 (parallel): gather all "allow" calls concurrently.
        Phase 3: reassemble results in original tool_call order.
        """
        messages = list(messages)
        policies = tool_policies or {}

        assistant_message: Dict[str, Any] = {"role": "assistant", "content": assistant_content}
        assistant_message["tool_calls"] = [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
            for tc in tool_calls
        ]
        messages.append(assistant_message)

        # Phase 1: resolve policies sequentially (ask requires interactive await)
        resolved_policies: List[str] = []
        for tool_call in tool_calls:
            tool_name = tool_call.name
            policy = policies.get(tool_name, "allow")
            if policy == "ask":
                self._events.emit(ToolApprovalRequested(
                    step_id=step_id,
                    agent_name=agent_name,
                    tool_name=tool_name,
                    arguments=tool_call.arguments,
                ))
                if self._approval_fn is not None:
                    approved = await self._approval_fn(agent_name, tool_name, tool_call.arguments)
                    policy = "allow" if approved else "deny"
                else:
                    policy = "deny"
            resolved_policies.append(policy)

        # Phase 2: pre-populate deny results synchronously; gather allow calls in parallel
        results: Dict[int, Dict[str, Any]] = {}
        allow_indices: List[int] = []

        for idx, (tool_call, policy) in enumerate(zip(tool_calls, resolved_policies)):
            if policy == "deny":
                tool_name = tool_call.name
                denial_msg = (
                    f"Tool execution denied: '{tool_name}' requires approval and was not permitted."
                )
                self._events.emit(ToolDenied(
                    step_id=step_id,
                    agent_name=agent_name,
                    tool_name=tool_name,
                    reason="user_denied" if policies.get(tool_name) == "ask" else "policy",
                ))
                results[idx] = {"tool_call_id": tool_call.id, "name": tool_name, "result": denial_msg}
            else:
                allow_indices.append(idx)

        if allow_indices:
            gathered = await asyncio.gather(
                *[self._run_single_tool(tool_calls[i], agent_name, step_id) for i in allow_indices],
                return_exceptions=False,
            )
            for idx, record in zip(allow_indices, gathered):
                results[idx] = record

        # Phase 3: reassemble in original order
        tool_records = []
        for idx, tool_call in enumerate(tool_calls):
            record = results[idx]
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": tool_call.name,
                "content": str(record["result"]),
            })
            tool_records.append(record)

        return messages, tool_records
