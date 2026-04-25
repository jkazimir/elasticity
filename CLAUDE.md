# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install for development (with all optional backends)
pip install -e ".[all,dev]"

# Run tests
pytest tests/

# Run a single test
pytest tests/test_executor.py::test_name

# Run a single test file
pytest tests/test_executor.py

# Lint / format
ruff check src/
black src/

# Validate a config file
elasticity validate examples/research_and_write.yaml

# Run an orchestration
elasticity run examples/research_and_write.yaml --input '{"topic": "AI safety"}'

# Start a chat session
elasticity chat examples/conversational_assistant.yaml
```

## Architecture

### Public API

`src/elasticity/__init__.py` exposes the `Orchestration` class as the single entry point. It:
1. Loads and validates YAML config (`config/loader.py` + `config/validator.py`)
2. Merges global config (`~/.config/elasticity/config.yaml`) with per-orchestration config
3. Builds a `ToolRegistry` from `tools` definitions
4. Provides `run()`, `chat()`, and sync variants

### Config → Graph → Execution Pipeline

```
YAML file
  → config/loader.py       (Pydantic models in config/schema.py)
  → config/validator.py    (cross-reference checks)
  → compiler/graph.py      (GraphBuilder: flow steps → ExecutionGraph of GraphNodes)
  → runtime/executor.py    (Executor: walks the graph, dispatches each NodeType)
  → runtime/agent.py       (AgentRunner: calls LLM backends, runs tool loops)
```

`NodeType` values: `AGENT`, `PARALLEL`, `LOOP`, `ROUTE`, `SPAWN`, `SUPERVISE`, `INTERVAL`, `APPROVE`

### Key Runtime Files

- **`runtime/executor.py`** — Core orchestration engine. `_execute_node()` is recursive (depth follows flow chain length). Dispatches to per-type handlers: `_execute_agent_node`, `_execute_parallel_node`, etc.
- **`runtime/agent.py`** — `AgentRunner.run()`: formats context, calls backend, handles tool rounds (up to `max_tool_rounds`), manages `approval_fn` for `ask`-policy tools.
- **`runtime/context.py`** — `ContextManager`: holds `messages` dict (message-passing) and `shared` dict. `branch()` creates a snapshot for parallel branches; `merge_from()` merges branch results back.
- **`runtime/session.py`** — `Session`: in-memory conversation history + context for conversational orchestrations.
- **`runtime/input_handler.py`** — `InputHandler`: supports both turn-based and interrupt-mode input delivery.
- **`runtime/spawn.py`** — `SpawnManager`: tracks active child agents, enforces `max_concurrent_spawns`.
- **`runtime/scheduler.py`** — `IntervalScheduler`: runs `interval` nodes on a timer.

### Backends

`backends/registry.py` resolves `provider/model-name` strings to backend instances (cached at module level). Backends implement `backends/base.py`:
- `backends/anthropic.py` — Anthropic SDK, streaming + tool use
- `backends/openai.py` — OpenAI-compatible SDK (also used for local endpoints via `OPENAI_BASE_URL`)

### Events

`events.py` defines `EventBus` and all event dataclasses (`AgentStarted`, `AgentCompleted`, `AgentErrorEvent`, `NodeStarted`, etc.). The bus is the sole coupling point between the executor and the CLI display layer. All event fields default to `""`.

### CLI

`cli/main.py` — Click command group: `run`, `chat`, `validate`, `list`, `config`.

`cli/chat.py` — Interactive REPL. `ChatSessionState` dataclass holds mutable state across turns. Two execution paths:
- `_run_chat_turn_based()` — sequential prompt/response loop
- `_chat_session_async()` — concurrent: separate tasks for user input and agent output

`cli/display.py` + `cli/split_display.py` — Rich-based output rendering; subscribe to `EventBus` events. Console singletons live in `cli/_console.py`.

### Tools

`tools/builtins.py` — Maps builtin names to their implementations (`file_read`, `file_write`, `file_list`, `shell`, `http_request`, `memory_store`, `memory_retrieve`, `web_search`).

`tools/ask_user.py` — `ask_user` tool; wired to the CLI via `set_ask_user_fn()`.

MCP integration: `mcp/client.py` + `mcp/registry.py`. Started lazily on first `run()` call; kept alive across turns in conversational mode, stopped after batch runs.

### Config Layers

Two separate configs:
- **Global** (`~/.config/elasticity/config.yaml`): sandbox, storage, logging, shared MCP servers. Loaded by `config/global_loader.py`.
- **Orchestration** (any path): `agent_types`, `tools`, `orchestrations`. Loaded by `config/loader.py`.

### Storage

`storage.py` — `SessionStore` (SQLite via `~/.local/share/elasticity/sessions.db`) and `TurnRecord`. Persists chat history across CLI sessions.

`tracing.py` — `RunTrace` subscribes to `EventBus` and writes structured logs. `write_chat_turn_log()` appends to the chat log file.
