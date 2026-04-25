# Elasticity

Configuration-driven LLM agent orchestration framework for prototyping multi-agent patterns.

> Human Note: This is largely "vibe" coded, example configs not fully fleshed out, abandoned features, haven't really used it in docker, but it got me into a bit of a rabbit hole elsewhere with this as a dependency, so initially releasing as-is for now.

## Overview

Elasticity lets you define agent types and orchestration patterns in YAML, then run them. Like a CMS defines content types and relationships, Elasticity defines agent types (with LLM properties) and orchestration flows (with composable primitives).

## Features

- **YAML Configuration**: Define agent types, tools, and orchestrations declaratively
- **Composable Primitives**: Sequence, parallel, loop, route, spawn, supervise, interval
- **Multi-Provider LLM**: Native support for OpenAI-compatible and Anthropic backends
- **Async Execution**: Supports both parallel and sequential execution patterns
- **CLI + Library**: Run orchestrations from command line or Python code
- **Conductor**: A meta-orchestration layer where a single LLM agent directs multiple team orchestrations toward a high-level goal

## Quick Start

```yaml
# config.yaml
agent_types:
  researcher:
    model: openai/gpt-4o
    system_prompt: "You are a research agent."
    tools: [web_search]

tools:
  web_search:
    description: "Search the web"
    callable: "myproject.tools.web_search"
    parameters:
      query: { type: string, required: true }

orchestrations:
  research:
    input:
      topic: string
    flow:
      - agent: researcher
        input: "Research {topic}"
```

```bash
elasticity run config.yaml --input '{"topic": "AI safety"}'
```

### CLI Usage

The `run` command executes an orchestration from your config file. Here's how it works:

**Input and prompts:** The `--input` option provides the orchestration's input data as JSON. Keys must match the orchestration's `input` schema (e.g. `topic` in the example above). This data is used to fill `{variable}` placeholders in flow step templates. For example, `input: "Research {topic}"` becomes the user prompt sent to the agent—so the topic is the prompt content the LLM receives.

**Providing input:**
- **Inline JSON:** `--input '{"topic": "AI safety"}'`
- **From file:** `--input @input.json` (file must contain valid JSON)

**Choosing an orchestration:** If the config defines a single orchestration, it runs automatically. With multiple orchestrations, specify one with `--orchestration` (or `-o`):

```bash
elasticity run config.yaml --orchestration research --input '{"topic": "AI safety"}'
```

**Execution trace:** Add `--trace` to print a detailed execution trace after the run:

```bash
elasticity run config.yaml --input '{"topic": "AI safety"}' --trace
```

**Other commands:**
- `elasticity validate config.yaml` — Validate configuration
- `elasticity list config.yaml` — List orchestrations in the config

## Conductor

For goals that require coordinating multiple independent teams, use a conductor. A conductor is a single LLM agent (the "CEO") whose tools are entire orchestrations (the "teams"). Give it a high-level goal and it delegates, synthesizes, and iterates autonomously.

```yaml
# conductor.yaml
agent_types:
  ceo:
    model: anthropic/claude-opus-4-6
    system_prompt: "You are the CEO. Break goals into tasks and delegate to teams."
    max_tool_rounds: 20

conductor:
  agent: ceo

teams:
  research:
    config: ./research_team.yaml
    orchestration: main
    description: "Researches any topic and returns a structured report"
    input:
      topic: string
    output: report

  writing:
    config: ./writing_team.yaml
    orchestration: main
    description: "Writes polished content from research and a brief"
    input:
      research: string
      brief: string
    output: article
```

```bash
elasticity conduct conductor.yaml --input "Write a blog post about AI safety"
elasticity conduct-chat conductor.yaml   # interactive multi-turn session
```

Team descriptions are auto-injected into the conductor's system prompt at startup — add or remove teams without touching the prompt. See **[Conductor](docs/conductor.md)** for the full reference.

## Installation

```bash
# Install with OpenAI support
pip install elasticity[openai]

# Install with Anthropic support
pip install elasticity[anthropic]

# Install with both
pip install elasticity[all]
```

## Documentation

- **[Configuration Reference](docs/configuration.md)** — Complete reference for the YAML config format (fields, types, options, validation)
- **[Conductor](docs/conductor.md)** — Meta-orchestration layer for directing multiple teams toward a high-level goal

## License

MIT
