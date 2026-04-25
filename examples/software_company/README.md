# Software company example

Multi-team SDLC orchestration: planning → dev → QA → (optional) review → merge, plus bugfix and maintenance paths. The **CTO** conductor delegates to teams defined under `teams/`.

## Run

```bash
elasticity chat examples/software_company/cto.yaml
```

Use a working directory where Elasticity can reach your **target git repository** (dev agents run `git rev-parse --show-toplevel` for worktrees).

## Memory

Persistent keys live in `~/.local/share/elasticity/software_company.db`. See [memory_schema.md](./memory_schema.md).

## Autonomy modes

Set optional memory key `cto:autonomy_mode` (via the CTO or `memory_store`):

| Mode | Behavior |
|------|----------|
| **conservative** | Ask before merge; prefer full code review. |
| **balanced** (default) | Low risk: may skip review and auto-merge after QA; medium/high: user confirms merge. |
| **aggressive** | Same safety as balanced for **medium/high** risk; maximizes automation for **low** risk only. |

**Never auto-merge** medium or high risk work without explicit user confirmation.

## Self-improvement

If a team misbehaves, the CTO may call **ops_improver** with the absolute team config path from the manifest, then **reload_team**. Changes are limited to YAML under `examples/software_company/`. Capped at two improvement cycles per user request (see CTO system prompt).

## Validate configs

```bash
elasticity validate examples/software_company/cto.yaml
elasticity validate examples/software_company/teams/planning.yaml
# …other team files as needed
```
