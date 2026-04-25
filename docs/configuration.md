# Configuration Reference

This document is the authoritative reference for the Elasticity configuration format.

Elasticity uses **two separate configuration layers**, each with a distinct scope:

| Layer | Location | Purpose |
|-------|----------|---------|
| **Global config** | `~/.config/elasticity/config.yaml` | Application-level settings: sandbox, storage, logging, shared MCP servers |
| **Orchestration config** | Any path you choose (e.g. `myproject/agents.yaml`) | Per-project definitions: agent types, tools, orchestration flows |

This separation keeps orchestration files focused on *what agents do* while application-level concerns live in one central place.

---

## Global Configuration

The global config is optional. When absent, sensible defaults apply. Manage it with the `elasticity config` commands:

```bash
elasticity config init        # create ~/.config/elasticity/config.yaml with defaults
elasticity config path        # print the resolved config file path
elasticity config show        # display the current effective configuration
```

### Path resolution

| Priority | Source |
|----------|--------|
| 1 | `ELASTICITY_CONFIG` environment variable |
| 2 | `$XDG_CONFIG_HOME/elasticity/config.yaml` |
| 3 | `~/.config/elasticity/config.yaml` (default) |

### Data directory resolution

Session databases and logs are stored in the data directory:

| Priority | Source |
|----------|--------|
| 1 | `ELASTICITY_DATA_DIR` environment variable |
| 2 | `$XDG_DATA_HOME/elasticity/` |
| 3 | `~/.local/share/elasticity/` (default) |

### Global config schema

```yaml
# ~/.config/elasticity/config.yaml

sandbox:
  provider: local          # "local" (default), "docker", "daytona", …
  settings: {}             # Provider-specific settings

storage:
  session_db: null         # null → XDG data dir / sessions.db; or explicit path

logging:
  chat_log: null           # null → XDG data dir / chat.log; path string; or false

mcp_servers:               # Global MCP servers available to ALL orchestrations
  my_server:
    command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "."]
    transport: stdio
```

#### `sandbox`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `provider` | string | `"local"` | Execution provider. `"local"` runs agents in-process with no isolation. |
| `settings` | object | `{}` | Provider-specific settings passed to the sandbox backend. |

#### `storage`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `session_db` | string or null | `null` | Path to the SQLite session database. `null` uses the XDG data directory. Supports `~` expansion. Overridden by `ELASTICITY_SESSION_DB` env var. |

#### `logging`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `chat_log` | string, `false`, or null | `null` | Chat log file path. `null` uses the XDG data directory. `false` disables logging. Overridden by `ELASTICITY_CHAT_LOG_FILE` env var. |

#### `mcp_servers` (global)

Same schema as per-orchestration `mcp_servers` (see [MCP Servers](#mcp-servers)). Global servers are merged into every orchestration. When the same server name appears in both, the **per-orchestration definition takes precedence**.

---

## Orchestration Configuration

Each project has its own orchestration config file, passed as an argument to every `elasticity` command:

```bash
elasticity run myproject/agents.yaml --orchestration research
elasticity chat myproject/agents.yaml
```

### File Structure

A configuration file has four top-level sections. At least one must be non-empty.

```yaml
agent_types: { ... }     # Agent type definitions
tools: { ... }           # Tool definitions (optional if agents only use builtins)
tool_groups: { ... }     # Named tool sets for reuse (optional)
orchestrations: { ... }  # Orchestration flow definitions
```

For pattern-oriented examples, see the files in `examples/`.

---

## `agent_types`

Maps agent type names to definitions. Each agent type is an archetype with LLM properties and capabilities.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `model` | string | yes | — | LLM model identifier in format `provider/model-name` (e.g., `openai/gpt-4o`, `anthropic/claude-sonnet-4-6`) |
| `system_prompt` | string | yes | — | System prompt for the agent |
| `rules` | list of string | no | `[]` | Additional behavioral rules appended to the system prompt after a blank line. Each rule is a separate string. |
| `tools` | list of string | no | `[]` | Tool names this agent can use. May reference names from `tools:`, known builtin names directly, or `@group_name` references. |
| `can_spawn` | list of string | no | `[]` | Agent type names this agent can spawn (must exist in `agent_types`) |
| `max_concurrent_spawns` | integer or null | no | `null` | Maximum concurrent child agents; `null` means unlimited |
| `max_tokens` | integer | no | `4096` | Maximum tokens in the response |
| `max_tool_rounds` | integer | no | `10` | Maximum number of tool calling rounds before returning |
| `max_concurrent_tools` | integer or null | no | `null` | Maximum tool calls to run concurrently in a single round; `null` means unlimited |
| `output_schema` | object | no | `null` | Expected fields in the agent output. See [Output Schema](#output-schema). |
| `tool_policies` | object | no | `{}` | Per-tool execution policy. See [Tool Policies](#tool-policies). |

### `rules`

Rules are additional behavioral constraints appended to the agent's `system_prompt` after a blank line. Each rule is a plain string and is typically phrased as a command or constraint:

```yaml
agent_types:
  coder:
    model: anthropic/claude-sonnet-4-6
    system_prompt: "You are an expert software engineer."
    rules:
      - "Always write tests for new functions."
      - "Prefer existing library functions over reimplementing them."
      - "Never commit directly to main."
```

### Tool Policies

`tool_policies` controls whether a tool may execute when called by this agent. Keys are tool names (must be present in the agent's `tools` list); values are one of:

| Policy | Behaviour |
|--------|-----------|
| `allow` | Execute immediately. This is the default for any tool not listed in `tool_policies`. |
| `deny` | Always block execution. The agent receives an informational denial message and can adjust its approach. |
| `ask` | Pause and prompt the user for approval. The user can allow, deny, or set a session-level preference for the tool. In non-interactive (batch) mode, `ask` falls back to `deny` automatically. |

```yaml
agent_types:
  assistant:
    model: openai/gpt-4o
    system_prompt: "You are a helpful assistant."
    tools: [file_read, file_write, shell, web_search]
    tool_policies:
      shell: ask        # prompt before running shell commands
      file_write: ask   # prompt before writing files
      # file_read and web_search default to "allow"
```

When a tool policy is `ask`, the CLI presents a compact prompt:

```
Tool approval required  assistant wants to call shell(command='rm -rf /tmp/old')
  [y] allow  [n] deny  [a] always allow this session  [d] always deny this session
  >
```

Choosing `a` (always allow) or `d` (always deny) remembers the decision for the remainder of the session, so you won't be asked again for the same tool. These runtime overrides are ephemeral and are never written to disk.

**Validation:** Every key in `tool_policies` must exist in the agent's `tools` list. The validator will report an error otherwise.

### Output Schema

`output_schema` declares the expected fields in an agent's JSON response. When set, the executor parses the agent's output as JSON (or extracts the first JSON object from the text) and stores each declared field as a separate context variable.

```yaml
agent_types:
  classifier:
    model: openai/gpt-4o
    system_prompt: "Classify the input. Return JSON with 'category' and 'confidence'."
    output_schema:
      category: string
      confidence: float
```

After the agent runs, `{category}` and `{confidence}` are available as context variables for subsequent steps.

**Supported type hints:** `string` / `str`, `integer` / `int`, `float`, `boolean` / `bool`. Type hints are used for coercion — the raw JSON value is cast to the declared type. Any field present in the response but absent from `output_schema` is silently ignored. If the response cannot be parsed as JSON, a warning is logged and no fields are extracted.

**Note:** `output_schema` instructs the *executor* how to parse the response. It does not constrain the LLM to produce structured output — you must prompt for JSON in the `system_prompt`.

---

### Model field

The `model` value must be in the format `provider/model-name`. The provider prefix determines which backend SDK is used.

| Provider | Format | Example model identifiers | SDK Package |
|----------|--------|--------------------------|-------------|
| OpenAI | `openai/model-name` | `openai/gpt-4o`, `openai/gpt-4o-mini`, `openai/gpt-4-turbo` | `elasticity[openai]` |
| Anthropic | `anthropic/model-name` | `anthropic/claude-sonnet-4-6`, `anthropic/claude-opus-4-6` | `elasticity[anthropic]` |

**OpenAI-compatible backends:** The `openai` backend works with any OpenAI-compatible API endpoint. Set `OPENAI_BASE_URL` to use custom endpoints (e.g., Ollama, vLLM, local servers).

**Installation:** Install the required SDK packages:
- `pip install elasticity[openai]` for OpenAI support
- `pip install elasticity[anthropic]` for Anthropic support
- `pip install elasticity[all]` for both

**API Keys:** Set provider-specific API keys via environment variables:
- `OPENAI_API_KEY` for OpenAI (or custom OpenAI-compatible endpoints)
- `ANTHROPIC_API_KEY` for Anthropic

---

## `tools`

Maps tool names to definitions. Tools are capabilities agents can invoke via function calling. Use `{}` when empty (not `[]`).

**Implicit builtin registration:** If an agent's `tools` list references a known builtin name that is not explicitly defined in `tools:`, Elasticity automatically registers it with default settings. You only need an explicit definition when you want to override the builtin's configuration (e.g., `shell` with `mode: bash`, `web_search` with a specific provider).

**Tool definition:** Either `builtin` or `callable` must be provided. When using `builtin`, the `description` field is optional as it's inherited from the built-in tool definition.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `builtin` | string or null | no* | `null` | Built-in tool name (alternative to `callable`). See [Built-in Tools](#built-in-tools) for available options. |
| `description` | string | yes* | — | Description exposed to the LLM for function calling. Required when using `callable`; optional when using `builtin` (inherited from built-in definition). |
| `callable` | string | yes* | — | Python dotted path to callable (e.g., `mypackage.module.function_name`). Must be importable at runtime. |
| `parameters` | object | no | `{}` | Map of parameter name to parameter schema |
| `config` | object | no | `{}` | Tool-specific configuration dict, passed to `_tool_init` hook if present |

### Parameter schema

Each parameter in `parameters` is an object with:

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `type` | string | yes | — | One of: `string`, `integer`, `float`, `boolean` |
| `required` | boolean | no | `true` | Whether the parameter is required |
| `default` | any | no | `null` | Default value when not provided |
| `description` | string | no | `null` | Optional description for the LLM |

### Tool initialization hook

Tools can optionally define a module-level `_tool_init(config)` function that receives the tool's `config` dict when the tool is first loaded. This allows tools to initialize persistent resources (e.g., database connections, file handles) before use.

The `_tool_init` hook is called once per module, even if multiple tools from the same module are registered. If a tool module doesn't define `_tool_init`, it's simply skipped.

Example:

```yaml
tools:
  web_search:
    description: "Search the web for information"
    callable: "myproject.tools.web_search"
    parameters:
      query:
        type: string
        required: true
        description: "Search query"
      max_results:
        type: integer
        required: false
        default: 10

  memory_store:
    description: "Store a key-value pair in persistent memory"
    callable: "elasticity.tools.memory.store"
    config:
      db_path: "./memory.db"
    parameters:
      key:
        type: string
        required: true
        description: "Memory key"
      value:
        type: string
        required: true
        description: "Content to store"
```

### Built-in Tools

Elasticity provides a comprehensive set of built-in tools. Reference them by name in an agent's `tools` list — no explicit `tools:` declaration required unless you need to override their configuration.

#### Filesystem tools

| Name | Description | Parameters |
|------|-------------|------------|
| `file_read` | Read file contents, optionally restricted to a line range | `path` (required); `start_line` (optional, 1-based, 0=start); `end_line` (optional, 1-based inclusive, 0=end) |
| `file_write` | Write content to a file | `path` (required); `content` (required) |
| `file_edit` | Replace an exact string in a file (must appear exactly once) | `path` (required); `old_string` (required); `new_string` (required) |
| `file_list` | List files and directories in a directory | `path` (required) |
| `file_grep` | Search file contents for a pattern (regex supported) | `pattern` (required); `path` (optional, default `.`); `glob` (optional file filter, e.g. `*.py`) |
| `file_glob` | Find files matching a glob pattern | `pattern` (required, e.g. `**/*.py`); `path` (optional base dir, default `.`) |
| `file_delete` | Delete a file or empty directory | `path` (required) |
| `file_move` | Move or rename a file or directory | `source` (required); `destination` (required) |

#### Network tools

| Name | Description | Parameters |
|------|-------------|------------|
| `http_request` | Make an HTTP request to a URL | `url` (required); `method` (optional, default `GET`); `body` (optional); `headers` (optional JSON string) |
| `web_search` | Search the web using a configured search provider | `query` (required) |

#### Shell tool

| Name | Description | Parameters |
|------|-------------|------------|
| `shell` | Execute a shell command | `command` (required); `timeout` (optional, default 120s) |

The `shell` builtin runs each command as a single process. Use the `config` section to change the execution mode:

```yaml
tools:
  shell:
    builtin: shell
    config:
      mode: bash   # enables pipes, redirects, and command chaining
```

#### Memory tools

| Name | Description | Parameters |
|------|-------------|------------|
| `memory_store` | Store a key-value pair in persistent memory | `key` (required); `value` (required) |
| `memory_retrieve` | Retrieve memories by query | `query` (required) |

Memory tools default to an in-process store. To use a persistent database, declare them explicitly with a `db_path`:

```yaml
tools:
  memory_store:
    builtin: memory_store
    config:
      db_path: "./agent_memory.db"
  memory_retrieve:
    builtin: memory_retrieve
    config:
      db_path: "./agent_memory.db"
```

#### Interaction tool

| Name | Description | Parameters |
|------|-------------|------------|
| `ask_user` | Ask the user a clarifying question and return their answer | `question` (required) |

`ask_user` pauses execution and presents the question to the user via the CLI. In batch mode (non-interactive), it returns an empty string.

#### Git tools

All git tools default to operating on the repository at `.` (the current working directory). Override with the `path` or `repo_path` parameter.

| Name | Description | Key Parameters |
|------|-------------|----------------|
| `git_status` | Show working tree status (`git status --short`) | `path` (optional) |
| `git_diff` | Show diff of working changes or between refs | `path` (optional); `ref` (optional, e.g. `HEAD` or `main..HEAD`) |
| `git_log` | Show recent commit log (one line per commit with graph) | `path` (optional); `n` (optional, default 10) |
| `git_add` | Stage files for commit | `paths` (required, space-separated or `.`); `repo_path` (optional) |
| `git_commit` | Create a commit with a conventional commit message | `message` (required, e.g. `feat: add login`); `repo_path` (optional) |
| `git_create_branch` | Create and checkout a new branch | `branch` (required, must start with `feature/`, `fix/`, `chore/`, `docs/`, `test/`, or `refactor/`); `path` (optional) |
| `git_checkout` | Checkout an existing branch or commit ref | `ref` (required); `path` (optional) |
| `git_merge` | Merge a branch into the current branch (uses `--no-ff` by default) | `branch` (required); `message` (optional); `no_ff` (optional, default true); `repo_path` (optional) |
| `git_pull` | Pull changes from a remote | `remote` (optional, default `origin`); `branch` (optional); `repo_path` (optional) |
| `git_push` | Push commits to a remote (force push is blocked) | `remote` (optional, default `origin`); `branch` (optional); `repo_path` (optional) |
| `git_worktree_add` | Create an isolated git worktree with a new branch | `path` (required); `branch` (required); `repo_path` (optional) |
| `git_worktree_remove` | Remove a git worktree after work is complete | `path` (required); `repo_path` (optional) |

Git tools enforce safety constraints: `git_create_branch` requires a conventional branch name prefix; `git_checkout` refuses `--force`/`--hard` flags; `git_push` blocks force push; `git_merge` uses `--no-ff` by default.

**Built-in tool configuration:** Some built-in tools accept configuration via the `config` field. For example, `web_search` supports:

```yaml
tools:
  web_search:
    builtin: web_search
    config:
      provider: duckduckgo  # or "brave"
      # api_key_env: BRAVE_API_KEY  # Required if using Brave provider
```

---

## `tool_groups`

Named sets of tools that can be referenced in agent `tools` lists using `@group_name` syntax. This eliminates repetition when many agents share the same tool sets.

```yaml
tool_groups:
  filesystem: [file_read, file_write, file_edit, file_list, file_grep, file_glob]
  git: [git_status, git_diff, git_log, git_add, git_commit, git_create_branch,
        git_checkout, git_merge, git_push, git_pull]
  memory: [memory_store, memory_retrieve]

agent_types:
  developer:
    model: anthropic/claude-3-5-sonnet-20241022
    system_prompt: "You are a software engineer."
    tools: ["@filesystem", "@git", shell]

  architect:
    model: anthropic/claude-3-5-sonnet-20241022
    system_prompt: "You design software architecture."
    tools: ["@filesystem", "@memory", web_search]
```

Groups are expanded before validation. An `@group_name` that does not exist in `tool_groups` causes a validation error.

---

## `orchestrations`

Maps orchestration names to flow definitions. Each orchestration composes agent types using flow primitives.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `description` | string | no | `null` | Human-readable description |
| `input` | object | no | `null` | Input schema: parameter name → type string (e.g., `topic: string`) |
| `mode` | string | no | `"batch"` | Execution mode: `"batch"` (one-shot) or `"conversational"` (multi-turn). See [Execution Modes](#execution-modes). |
| `response_key` | string or null | no | `null` | Which `output_as` variable becomes the user-facing response (for conversational mode) |
| `communication` | string | no | `"message_passing"` | Communication mode: `shared_context`, `message_passing`, or `both` |
| `error_strategy` | object | no | `null` | Default error strategy for all steps. See [Error Strategy](#error-strategy). |
| `input_handling` | object | no | `null` | For conversational mode: how to handle user input during execution. See [Input Handling](#input-handling). |
| `flow` | list | yes | — | List of flow primitives. See [Flow Primitives](#flow-primitives). |

### Execution modes

Orchestrations can run in two modes:

| Mode | Behavior |
|------|----------|
| `batch` | One-shot execution (default). The orchestration runs once with the provided inputs and returns the final result. |
| `conversational` | Multi-turn execution. The orchestration maintains state across multiple invocations, allowing for back-and-forth interactions. Use `response_key` to specify which `output_as` variable becomes the user-facing response. |

**Example - Conversational mode:**

```yaml
orchestrations:
  assistant:
    mode: conversational
    response_key: response
    input:
      message: string
    flow:
      - agent: classifier
        input: "{message}"
        output_as: intent
      - agent: responder
        input: "{message}"
        output_as: response
```

### Communication modes

| Mode | Behavior |
|------|----------|
| `message_passing` | Step outputs (`output_as`) are stored as messages; subsequent steps reference them by name in templates |
| `shared_context` | Step outputs update a shared key-value context; all steps see the same context |
| `both` | Both message passing and shared context are active |

---

## Flow Primitives

Flow steps can be basic steps (agent execution) or composite primitives. A flat list in `flow` is implicitly a sequence executed in order.

### Step (sequence)

A single agent execution step. Use a dict with `agent`, `input`, and optionally `output_as`.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `agent` | string | yes* | — | Agent type name (must exist in `agent_types`) |
| `input` | string | no | `null` | Input template; supports `{variable}` interpolation |
| `output_as` | string | no | `null` | Variable name to store the output |
| `on_error` | object | no | `null` | Per-step error strategy (overrides orchestration default). See [Error Strategy](#error-strategy). |
| `spawn_strategy` | string | no | `null` | If `"dynamic"`, enables dynamic child agent spawning. See [Dynamic Spawning](#dynamic-spawning). |
| `collect_as` | string | no | `null` | Variable name to collect all spawned outputs (used with `spawn_strategy: dynamic`) |

\*Required when this step invokes an agent.

```yaml
- agent: researcher
  input: "Research the topic: {topic}"
  output_as: research_results
```

### `parallel`

Runs multiple steps concurrently. Each child can be a step or a nested primitive.

```yaml
- parallel:
    - agent: economics_researcher
      input: "Research economic aspects of {topic}"
      output_as: econ_research
    - agent: history_researcher
      input: "Research historical context of {topic}"
      output_as: hist_research
```

### `loop`

Repeats a body until a condition is met or `max_iterations` is reached.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `max_iterations` | integer | no | Maximum loop iterations. Defaults to 10 if omitted. |
| `until` | string | no | Stop condition. See [Condition Expressions](#condition-expressions). |
| `body` | list | yes | List of flow steps to execute each iteration |

Inside loop body steps, two special context variables are available:

| Variable | Value |
|----------|-------|
| `{_loop_iteration}` | Current iteration number, starting at 1 |
| `{_loop_max}` | The configured `max_iterations` value |

```yaml
- loop:
    max_iterations: 5
    until: "quality_score >= 0.9"
    body:
      - agent: critic
        input: "Iteration {_loop_iteration}/{_loop_max}. Evaluate: {draft}"
        output_as: evaluation
      - agent: writer
        input: "Improve based on: {evaluation}"
        output_as: draft
```

### `route`

Conditional branching based on a context variable value.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `condition` | string | yes | Name of the context variable to route on. Braces are optional: both `condition: classification` and `condition: "{classification}"` work identically. |
| `cases` | object | yes | Map of expected value → list of flow steps |
| `default` | list | no | Steps to run when no case matches |

**Note:** `condition` is the only supported field name for specifying the routing variable. The value is always a context variable name — not an expression.

```yaml
- route:
    condition: "classification"
    cases:
      urgent:
        - agent: priority_handler
          input: "{message}"
      normal:
        - agent: standard_handler
          input: "{message}"
    default:
      - agent: fallback_handler
        input: "{message}"
```

### `supervise`

A supervisor agent monitors worker output, providing feedback and requesting retries when the work doesn't meet standards.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `supervisor` | string | yes | Agent type name for the supervisor |
| `workers` | list | yes | List of worker configs. Each has `agent` (required), `input` (optional), `output_as` (optional). |
| `on_reject` | string | no | Action when supervisor rejects: `"retry_with_feedback"` (default) or `"fail"` |
| `max_retries` | integer | no | Maximum rejection retries before failing (default 3) |

**Worker fields:**

| Field | Type | Description |
|-------|------|-------------|
| `agent` | string | Agent type name (must exist in `agent_types`) |
| `input` | string | Input template for the worker |
| `output_as` | string | Context variable to store worker output |

> **Note:** The `task` field is a deprecated alias for `input`. Use `input` instead.

```yaml
- supervise:
    supervisor: reviewer
    workers:
      - agent: writer
        input: "Write a section about: {topic}"
        output_as: draft
    on_reject: retry_with_feedback
    max_retries: 3
```

### `interval`

Runs an agent on a timer until a condition is met or the orchestration completes.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `every` | string | yes | Duration: `30s`, `5m`, `1h`, or plain seconds (e.g., `60`) |
| `agent` | string | yes | Agent type name |
| `input` | string | no | Input template |
| `output_as` | string | yes | Variable name for the output (required) |
| `until` | string | no | Condition to stop. Use `"orchestration.complete"` to stop when the orchestration finishes; or a condition expression. See [Condition Expressions](#condition-expressions). |

```yaml
- interval:
    every: 30s
    agent: monitor
    input: "Check progress. Task: {task}"
    output_as: status_log
    until: "orchestration.complete"
```

### `approve`

A human-in-the-loop step that pauses execution and asks for approval before proceeding. The reviewer can approve, reject, or edit the content.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `content` | string | yes | Content to display for review (template string; supports `{variable}` interpolation) |
| `message` | string | no | Prompt message shown to the reviewer. Defaults to a generic approval prompt. |
| `output_as` | string | no | Context variable to store the approved (or edited) content |
| `on_reject` | string | no | What to do on rejection: `"retry_previous"` (default, re-runs the preceding step) or `"fail"` (halt the orchestration) |
| `max_retries` | integer | no | Maximum rejection retries before failing (default 3, minimum 1) |

```yaml
- agent: writer
  input: "Draft an article about {topic}"
  output_as: draft

- approve:
    content: "{draft}"
    message: "Please review this draft. Approve, reject, or edit as needed."
    output_as: approved_draft
    on_reject: retry_previous
    max_retries: 2

- agent: publisher
  input: "Publish: {approved_draft}"
```

When the reviewer chooses **edit**, the edited version is stored in `output_as` and execution continues without rerunning the preceding step.

### `load_context`

A zero-LLM-cost step that loads values from persistent memory into context variables. Useful for resuming work across sessions.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `memory_tool` | string | yes | Name of a `memory_retrieve`-compatible tool defined in `tools:` |
| `load` | list | yes | List of memory entries to load. Each entry has `key`, `as`, and optionally `read_file`. |
| `output_as` | string | no | Combine all loaded values into this single context variable (JSON object) |

Each `load` entry:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `key` | string | yes | Exact memory key to retrieve |
| `as` | string | yes | Context variable name to store the value under |
| `read_file` | boolean | no | If true and the retrieved value is a file path, read the file contents instead (default false) |

```yaml
- load_context:
    memory_tool: memory_retrieve
    load:
      - key: "task:branch"
        as: branch_name
      - key: "task:description"
        as: task_description
      - key: "task:spec_path"
        as: spec_contents
        read_file: true
```

### `save_context`

A zero-LLM-cost step that writes context variables to persistent memory. Useful for checkpointing state between sessions.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `memory_tool` | string | yes | Name of a `memory_store`-compatible tool defined in `tools:` |
| `save` | object | yes | Map of memory keys → template strings. Values support `{variable}` interpolation. |

```yaml
- save_context:
    memory_tool: memory_store
    save:
      "task:branch": "{branch_name}"
      "task:status": "in_progress"
      "task:last_output": "{agent_output}"
```

### `tool_call`

A zero-LLM-cost step that invokes registered tools directly from the flow without requiring an agent. Works with builtins, custom tools, and MCP tools.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `tool` | string | no* | Name of a tool defined in `tools:` (single-tool shorthand) |
| `parameters` | object | no | Tool parameters. String values support `{variable}` interpolation. |
| `calls` | list | no* | Sequence of tool invocations (multi-tool form) |
| `output_as` | string | no | Context variable for the result (single-tool or last call result) |
| `on_error` | string | no | `"fail"` (default) raises on error; `"skip"` logs a warning and continues |

*Exactly one of `tool` or `calls` must be specified.

Each entry in `calls` has:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `tool` | string | yes | Name of the tool to invoke |
| `parameters` | object | no | Tool parameters (string values support `{variable}` interpolation) |
| `output_as` | string | no | Context variable for this call's result |

**Single tool:**

```yaml
- tool_call:
    tool: http_request
    parameters:
      url: "https://api.example.com/status"
      method: GET
    output_as: api_status
```

**Multiple sequential calls** (each can reference prior `output_as` variables):

```yaml
- tool_call:
    calls:
      - tool: memory_retrieve
        parameters:
          query: "project:overview"
        output_as: overview
      - tool: file_read
        parameters:
          path: "{overview}"
        output_as: file_content
    output_as: file_content
    on_error: skip
```

---

## Condition Expressions

Condition strings are used in `loop.until` and `interval.until`. The following forms are supported:

| Form | Example | Behaviour |
|------|---------|-----------|
| Comparison | `score >= 0.9` | Left side is a context variable name. Right side is a literal. Supported operators: `>=`, `<=`, `>`, `<`, `==`, `!=`. |
| Bare variable | `is_done` | Truthy check: numeric non-zero, string `"true"` / `"1"` / `"yes"`, or any other truthy value. |
| Braced variable | `{is_done}` | Same as bare variable. |
| Special | `orchestration.complete` | True when the orchestration has finished (for `interval.until` only). |

**Right-side literals:** Quoted strings (`"hello"` or `'hello'`), integers (`42`), floats (`3.14`), and booleans (`true` / `false`).

**Variable resolution:** Variables are looked up first in message-passing outputs, then in shared context. Returns `false` if the variable is not found.

---

## Dynamic Spawning

When `spawn_strategy: dynamic` is set on a step, the agent's response is parsed for spawn requests instead of being stored directly. The agent must output a JSON array of spawn requests:

```json
[
  {"agent": "researcher", "input": "Research quantum computing"},
  {"agent": "researcher", "input": "Research blockchain technology"}
]
```

Each spawn request creates a child agent run. Results are collected and stored in `collect_as` as a list of outputs.

```yaml
agent_types:
  coordinator:
    model: openai/gpt-4o
    system_prompt: "You coordinate research tasks. Output a JSON array of spawn requests."
    can_spawn: [researcher]
    max_concurrent_spawns: 3

orchestrations:
  research:
    flow:
      - agent: coordinator
        input: "Coordinate research on: {topic}"
        spawn_strategy: dynamic
        collect_as: research_outputs
```

**Wave-based ordering:** If tasks have sequential dependencies, the agent can output nested arrays. Tasks in the same wave (inner array) run concurrently; waves execute sequentially:

```json
[
  [{"agent": "researcher", "input": "Gather data"}, {"agent": "researcher", "input": "Review existing work"}],
  [{"agent": "synthesizer", "input": "Synthesize: {research_outputs}"}]
]
```

**Prerequisites:** The step's agent type must have the spawned agent types listed in `can_spawn`.

---

## Error Strategy

Used at orchestration level (`error_strategy`) or per-step (`on_error`). A step-level `on_error` **overrides** the orchestration-level `error_strategy` for that step.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `strategy` | string | no | `"retry"` | One of: `retry`, `skip`, `fallback`, `fail` |
| `max_retries` | integer | no | `3` | Maximum retries (for `retry` strategy) |
| `backoff` | string | no | `"exponential"` | One of: `exponential`, `linear`, `fixed` |
| `fallback_agent` | string | no | `null` | Agent type for `fallback` strategy (must exist in `agent_types`) |

| Strategy | Behaviour |
|----------|-----------|
| `retry` | Retry the failed step up to `max_retries` times with the configured backoff. |
| `skip` | Log the error and continue execution with the next step. |
| `fallback` | Run `fallback_agent` in place of the failed step. |
| `fail` | Halt the orchestration immediately and propagate the error. |

```yaml
orchestrations:
  pipeline:
    error_strategy:
      strategy: retry
      max_retries: 3
      backoff: exponential
    flow:
      - agent: risky_step
        input: "{data}"
        output_as: result
        on_error:           # overrides orchestration-level error_strategy for this step
          strategy: fallback
          fallback_agent: safe_fallback
```

---

## Input Handling

For conversational orchestrations, `input_handling` controls how user messages sent *during* an agent's execution are managed.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mode` | string | `"queue"` | `"queue"` — buffer messages for the next turn; `"interrupt"` — handle immediately; `"ignore"` — discard |
| `interrupt_behavior` | string | `null` | For `interrupt` mode: `"cancel"` (stop current execution) or `"graceful"` (inject input for the orchestration to handle) |
| `interrupt_delivery` | list | `null` | For graceful interrupts: one or more of `"event"`, `"context"`, `"agent"`. At least one required. |
| `queue_limit` | integer | `10` | Maximum messages to buffer in `queue` mode (1–100) |

```yaml
orchestrations:
  assistant:
    mode: conversational
    input_handling:
      mode: interrupt
      interrupt_behavior: graceful
      interrupt_delivery: [context, agent]
    flow:
      - agent: responder
        input: "{message}"
        output_as: response
```

**Delivery modes for graceful interrupts:**

| Delivery | Effect |
|----------|--------|
| `event` | Emits an `InterruptReceived` event on the event bus |
| `context` | Injects the interrupt message into the shared context as `_interrupt_message` |
| `agent` | Delivers the interrupt to the currently running agent as additional input |

---

## Template Variables

Input templates use Python-style `{variable_name}` interpolation. Available variables:

- **Orchestration inputs:** Parameters from `orchestrations.<name>.input` (e.g., `{topic}`, `{task}`)
- **Step outputs:** Values from previous steps' `output_as` (e.g., `{research_results}`, `{draft}`)
- **Loop magic variables:** Inside loop body steps: `{_loop_iteration}` (current 1-based iteration), `{_loop_max}` (configured max iterations)
- **Interrupt context:** `{_interrupt_message}` — set when a graceful interrupt is delivered via `context`
- **Condition special:** `orchestration.complete` in `until` — not a template variable; evaluated directly by the scheduler

---

## MCP Servers

MCP (Model Context Protocol) servers provide tools to agents from external processes. They are defined in the `mcp_servers` section of an orchestration config or in the global config.

```yaml
mcp_servers:
  filesystem:
    command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    transport: stdio

  remote_api:
    transport: sse
    url: "http://localhost:8000/sse"
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `command` | list of string | yes* | — | Subprocess command to start the server. Required for `stdio` transport. |
| `env` | object | no | `{}` | Environment variables for the server process. Supports `${VAR}` interpolation from the host environment. |
| `transport` | string | no | `"stdio"` | `"stdio"` (subprocess) or `"sse"` (HTTP Server-Sent Events) |
| `url` | string | yes* | `null` | Server URL. Required for `sse` transport. |

\*`command` is required for `stdio`; `url` is required for `sse`.

Tools from MCP servers are registered as `server_name.tool_name` in the tool registry. Agents reference them by that combined name:

```yaml
agent_types:
  coder:
    tools: [filesystem.read_file, filesystem.write_file]
```

MCP servers start lazily on first use and remain alive across turns in conversational mode. They are stopped after batch runs complete.

**Installation:** `pip install elasticity[mcp]` to enable MCP support.

---

## Validation Rules

The validator enforces these cross-references:

| Reference | Rule |
|-----------|------|
| Agent `tools` | Each entry must exist in the top-level `tools` section **or** be a known builtin name. `@group_name` references must exist in `tool_groups`. |
| Agent `tool_policies` | Each key must exist in the agent's `tools` list; values must be `allow`, `deny`, or `ask` |
| Agent `can_spawn` | Each entry must exist in `agent_types` |
| Tool `builtin` | If specified, must be a valid built-in tool name (see [Built-in Tools](#built-in-tools)) |
| Tool `callable` | Either `builtin` or `callable` must be provided |
| Tool `description` | Required when using `callable` (without `builtin`); optional when using `builtin` |
| Flow step `agent` | Must exist in `agent_types` |
| `error_strategy.fallback_agent` | Must exist in `agent_types` |
| `on_error.fallback_agent` | Must exist in `agent_types` |
| `supervise.supervisor` | Must exist in `agent_types` |
| `supervise` worker `agent` | Must exist in `agent_types` |
| `interval.agent` | Must exist in `agent_types` |
| `load_context.memory_tool` | Must exist in `tools` |
| `save_context.memory_tool` | Must exist in `tools` |
| `tool_call.tool` / `tool_call.calls[].tool` | Must exist in `tools` |

Run `elasticity validate config.yaml` to check a configuration file.
