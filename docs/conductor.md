# Conductor

A **conductor** is a meta-orchestration layer that sits above one or more team orchestrations. It is a single LLM agent whose tools are entire orchestrations — each team is a black box the conductor can invoke, inspect the result of, and invoke again.

The mental model follows a three-tier structure:

| Role | Description |
|------|-------------|
| **Stakeholder** | The human providing a high-level goal |
| **Conductor** | An LLM agent that breaks the goal into tasks and delegates them to teams |
| **Teams** | Self-contained orchestrations (each in their own YAML file) that execute the delegated tasks |

The conductor never reaches into a team's internal steps. It only knows what a team does (from the description), what inputs it accepts, and what output it returns. This keeps team configs independently maintainable and reusable across multiple conductors.

---

## How it works

At startup, the conductor:

1. Loads each team's orchestration from its config file.
2. Registers each team as a callable tool on the conductor agent's `ToolRegistry`.
3. Auto-generates a **team manifest** — a structured summary of every team's description and I/O schema — and appends it to the conductor agent's system prompt.

At runtime, the conductor agent runs in the standard LLM tool-call loop. When it calls a team tool, the corresponding orchestration runs to completion and the result is returned as the tool's return value. The conductor can call teams sequentially, in parallel (if the underlying LLM supports parallel tool calls), iteratively, or in any combination.

---

## Conductor config format

A conductor config file is separate from orchestration config files. It has four top-level sections:

```yaml
conductor: { ... }     # Which agent acts as the conductor
agent_types: { ... }   # Agent type definitions (same schema as orchestration configs)
tools: { ... }         # Optional tools for the conductor agent itself
teams: { ... }         # Team definitions (references to other orchestration configs)
```

Unlike orchestration configs, conductor configs do not have an `orchestrations` section — the conductor itself is the entry point.

---

## `conductor`

Identifies which agent type (defined in this file's `agent_types`) acts as the conductor.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `agent` | string | yes | Name of the agent type to use as the conductor |

```yaml
conductor:
  agent: ceo
```

---

## `agent_types`

Same schema as in [orchestration configs](configuration.md#agent_types). The conductor agent's system prompt is automatically extended with the team manifest at load time — you do not need to describe the teams manually in the prompt.

The most relevant fields for a conductor agent:

| Field | Notes |
|-------|-------|
| `model` | A capable model is recommended (e.g., `anthropic/claude-opus-4-6`) since the conductor makes strategic decisions |
| `system_prompt` | Describe the conductor's role and decision-making approach; team capabilities are injected automatically |
| `max_tool_rounds` | Set higher than for typical agents — the conductor may call multiple teams in sequence. `20`–`30` is a reasonable starting point |
| `tools` | Optional: give the conductor its own tools (e.g., `memory_store`, `memory_retrieve`) in addition to team tools |

```yaml
agent_types:
  ceo:
    model: anthropic/claude-opus-4-6
    system_prompt: |
      You are the CEO of a content studio. Break stakeholder goals into tasks
      and delegate them to specialized teams. Synthesize results into a final
      deliverable.
    max_tool_rounds: 25
    max_tokens: 2048
```

---

## `tools`

Optional. Same schema as in [orchestration configs](configuration.md#tools). These are tools available only to the conductor agent itself — they are not inherited by teams.

Common uses: `memory_store` / `memory_retrieve` for tracking project state across a multi-turn conversation, or `ask_user` for requesting clarification.

```yaml
tools:
  remember:
    builtin: memory_store
  recall:
    builtin: memory_retrieve
```

---

## `teams`

Maps team names to their definitions. Each team becomes a callable tool for the conductor.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `config` | string | yes | Path to the team's orchestration YAML, relative to the conductor config file |
| `orchestration` | string | yes | Name of the orchestration within the team config to run |
| `description` | string | yes | What this team does; injected verbatim into the conductor's system prompt |
| `input` | object | no | Input parameter names mapped to types (e.g., `topic: string`). These become the tool's parameters |
| `output` | string | no | Context key to extract from the team's result as the tool return value. If omitted, the full result dict is serialized as JSON |

```yaml
teams:
  research:
    config: ./teams/research_team.yaml
    orchestration: main
    description: >
      Researches any topic and returns a structured report covering key facts,
      current developments, notable examples, and open questions.
    input:
      topic: string
    output: report

  writing:
    config: ./teams/writing_team.yaml
    orchestration: main
    description: >
      Writes and edits polished content given research findings and a brief
      describing the desired format, tone, and audience.
    input:
      research: string
      brief: string
    output: article
```

### Team input

Fields in `input` are exposed as tool parameters the LLM can fill. They map directly to the orchestration's own `input` schema — they should match by name and type.

### Team output

The `output` field names a key in the orchestration's result context. The context is the dict returned by `Orchestration.run()`. If the team uses `message_passing` communication, the key lives inside the `messages` sub-dict; if it uses `shared_context`, it's at the top level. The conductor handles both automatically. If `output` is not set, the full context is returned as a JSON string.

### Team manifest (auto-injection)

At load time, the following block is appended to the conductor's system prompt:

```
## Available Teams

Delegate work by calling team names as tools.
Each team runs a full orchestration and returns its result.

### research
Researches any topic and returns a structured report covering...

**Input parameters:**
  - `topic` (string)

**Returns:** the `report` value from the result

### writing
...
```

This means the conductor always has an accurate picture of available teams without manual prompt maintenance. Adding or removing a team from `teams` automatically updates what the conductor knows.

---

## Team config requirements

Team orchestration files are standard Elasticity orchestration configs. The only requirement is that the named orchestration exists and its `input` schema matches the `input` declared in the conductor's team definition.

Team configs are fully independent — they can define their own tools, MCP servers, error strategies, and multi-step flows. The conductor sees none of this internals.

---

## CLI

### One-shot run

```bash
elasticity conduct conductor.yaml --input "Write a blog post about AI safety"
```

Options:

| Option | Description |
|--------|-------------|
| `--input`, `-i` | Goal as a plain string |
| `--stream` | Stream tokens to the terminal as they arrive |

If `--input` is not provided, the CLI will prompt for it.

### Interactive chat

```bash
elasticity conduct-chat conductor.yaml
```

Opens a REPL where you can give the conductor goals across multiple turns. The conductor maintains conversation history within the session — you can refine, follow up, or pivot without re-stating context.

Options:

| Option | Description |
|--------|-------------|
| `--stream` | Stream tokens to the terminal as they arrive |

Type `exit`, `quit`, or press `Ctrl+C` to end the session.

---

## Python API

```python
from elasticity import Conductor

conductor = Conductor("conductor.yaml")

# One-shot
result = await conductor.run("Write a report on fusion energy")

# Interactive (with session continuity)
from elasticity.runtime.session import Session

session = Session()
reply = await conductor.chat("Research quantum computing for me", session=session)
reply = await conductor.chat("Now write a 500-word summary for a general audience", session=session)
```

### `Conductor(config_path)`

Loads the conductor config, instantiates each team's `Orchestration`, registers teams as tools, and injects the team manifest.

### `await conductor.run(goal, event_bus=None, stream_responses=False) -> str`

Runs the conductor with a single goal and returns its final response string.

### `await conductor.chat(message, session=None, event_bus=None, stream_responses=False) -> str`

Sends a message in an ongoing conversation. Creates a new `Session` if one is not provided. Returns the conductor's response string.

### `conductor.run_sync(goal) -> str`

Synchronous convenience wrapper around `run()`.

### `conductor.chat_sync(message, session=None) -> str`

Synchronous convenience wrapper around `chat()`.

---

## Example

The `examples/conductor/` directory contains a working three-file example:

```
examples/conductor/
  conductor.yaml        # CEO conductor with research + writing teams
  research_team.yaml    # Web research orchestration
  writing_team.yaml     # Writer + editor pipeline
```

```bash
elasticity conduct examples/conductor/conductor.yaml \
  --input "Write a blog post about the current state of nuclear fusion"
```

---

## Relationship to `supervise`

The `supervise` flow primitive and the `Conductor` serve different purposes:

| | `supervise` | `Conductor` |
|--|-------------|-------------|
| **Scope** | Inside one orchestration config | Across multiple orchestration configs |
| **Role** | An agent that reviews and rejects/accepts worker output | An agent that decides what to do, who to delegate to, and synthesizes results |
| **Control** | Quality gate — approve/reject with feedback | Strategic direction — breaks goals into tasks |
| **Configuration** | A step in a flow | A separate config file |
| **Visibility** | Supervisor sees worker output only | Conductor sees all team outputs and orchestrates across them |

Use `supervise` when you need quality control within a single pipeline. Use `Conductor` when you need a strategic layer that coordinates multiple independent pipelines toward a stakeholder's goal.
