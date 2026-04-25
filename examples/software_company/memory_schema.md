# Software Company Memory Schema

## Database

All agents use a shared SQLite database:

`~/.local/share/elasticity/software_company.db`

(Resolved with `expanduser` + absolute path by the memory tool.) The directory is
created on first use. Teams hand off state via memory keys rather than passing large
text through conductor inputs.

**ops_improver** uses a separate DB for improvement notes only:

`~/.local/share/elasticity/software_company_ops_memory.db`

---

## Key Conventions

### Project-Level Keys
Persistent across all tasks and sessions. Updated infrequently.

| Key | Writer | Readers | Content |
|-----|--------|---------|---------|
| `project:overview` | planning/archivist | all agents | Tech stack, component map, repo path, current status. ≤800 words. |
| `project:architecture` | planning/architect | all agents | Patterns, conventions, naming rules, data flow, key ADRs. |
| `project:definition_of_done` | qa/qa_lead | qa, dev | QA checklist with thresholds (e.g., "coverage ≥ 80%"). |
| `project:tech_debt` | maintenance/debt_tracker | maintenance | JSON array of debt items (see format below). |
| `project:spec:{slug}` | planning/pm | dev/archivist | Path to a feature spec file on disk (e.g., `specs/user-auth.md`). |
| `project:adr:{slug}` | planning/architect | review/arch | Architecture Decision Record content. |
| `project:bugfix:{slug}` | bugfix/analyst | — | Root cause + fix summary for a resolved bug. |

### Task-Level Keys
Set at the start of each task by planning, read by dev and QA.
Should be cleared or overwritten when a new task begins.

| Key | Set By | Read By | Content |
|-----|--------|---------|---------|
| `task:current_spec` | planning/pm | dev/archivist, qa/spec_validator | Absolute path to the current spec file on disk. |
| `task:architecture_plan` | planning/architect | dev/archivist | Absolute path to the current architecture plan on disk. |
| `task:risk_level` | planning/architect | cto | `low`, `medium`, or `high`. Informs CTO routing decisions. |
| `task:branch_name` | dev/archivist | qa, review, merge | Integration branch name after dev completes. |
| `task:review_findings` | review/synthesizer | cto | JSON review verdict (approved, blocking_issues, suggestions). |
| `task:research_report` | research/synthesizer | planning/pm, planning/architect, cto | Path to the research report file (e.g., `research/stripe-api.md`). |
| `task:research_findings` | research/synthesizer | planning/pm, planning/architect | Full synthesized research text for direct context injection. |

### CTO Coordination Keys
Used by the CTO conductor to track work across sessions.

| Key | Content |
|-----|---------|
| `cto:active_task` | Description of the task currently in progress. |
| `cto:task_history` | JSON array of completed task summaries (append-only). |
| `cto:blocked_reason` | Why a team got stuck. Cleared when resolved. |
| `cto:autonomy_mode` | Optional: `conservative` \| `balanced` (default) \| `aggressive` — controls merge/review prompts (see README). |
| `task:review_skipped` | Set to `"true"` when the CTO skips the **review** team for low-risk work; otherwise omit or `"false"`. |

---

## Format Details

### `project:tech_debt` value format
```json
[
  {
    "id": "debt-001",
    "title": "Synchronous DB calls in async endpoints",
    "description": "Several endpoints use SQLAlchemy synchronous calls inside async handlers, causing thread pool exhaustion under load.",
    "severity": "high",
    "area": "src/api/users.py, src/api/orders.py",
    "added": "2026-03-14",
    "resolved": null
  }
]
```

### `task:review_findings` value format
```json
{
  "approved": false,
  "blocking_issues": "- auth.py:42: token.user_id accessed without None check (security: high)",
  "suggestions": "- users.py:17: consider caching the DB lookup (performance: low)",
  "review_summary": "One high-severity security issue blocks this PR."
}
```

### `cto:task_history` value format
```json
[
  {
    "task": "Add user authentication",
    "completed": "2026-03-14",
    "branch_name": "feature/integrated-user-auth",
    "risk_level": "medium"
  }
]
```

---

## Naming Conventions

- **Slugs**: lowercase, hyphens only, no spaces or underscores.
  Examples: `user-authentication`, `rate-limiting`, `export-csv`
- **File paths in memory**: store as absolute paths when possible, or paths
  relative to the project root. Always readable with `file_read`.
- **JSON values**: stored as stringified JSON strings. Retrieve and parse
  before use. Use `json.loads()` equivalent in prompts.
- **Boolean flags**: store as string `"true"` or `"false"` (memory values
  are always strings).

---

## Memory Isolation Warning

Parallel branches in the same orchestration share the **same SQLite database**.
If two parallel agents write to the same memory key simultaneously, the last
write wins and one result is lost.

**Design rule**: In any PARALLEL node, each branch must write to a UNIQUE key.

Examples of safe parallel memory usage:
```
security_reviewer   → writes: task:review:security
performance_reviewer → writes: task:review:performance
architecture_reviewer → writes: task:review:architecture
```

Examples of unsafe (don't do this):
```
reviewer_a → writes: task:review_findings  ← collision!
reviewer_b → writes: task:review_findings  ← collision!
```

---

## Adding New Memory Keys

When adding a new memory key in a config:
1. Add it to this table with writer, readers, and content description.
2. Use the standard namespace prefix (`project:`, `task:`, `cto:`).
3. Document the value format if it's JSON.
4. Update the corresponding agent's system prompt to mention the key.
