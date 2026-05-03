# Global Rules — All Modes

## Before Starting Any Task
1. Read /opt/tickles/CLAUDE.md — this is the source of truth for what exists on this server.
2. If a project folder is involved, check for a README.md or CONTEXT.md in that folder.
3. Never assume what exists. Always verify by reading files or listing directories first.

## Code Standards
- Language: Python 3.12 unless explicitly told otherwise.
- Style: PEP 8, 4 spaces indentation, max line length 120 characters.
- Imports: stdlib first, third-party second, local third. Separated by blank lines.
- Strings: Use f-strings for formatting. Double quotes for strings.
- Paths: Use pathlib.Path, never os.path string concatenation.

## Database Standards
- All queries use parameterized statements. NEVER string interpolation.
- All timestamps stored as UTC datetime.
- All monetary values use decimal(20,8) for prices, decimal(30,8) for volumes.
- Connection strings read from environment variables, never hardcoded.

## Security — Non-Negotiable
- Never hardcode API keys, passwords, or tokens in code.
- Never log sensitive data (API keys, passwords, account balances).
- Never commit .env files.
- All exchange API keys must be trade-only (no withdrawal permissions).

## Error Handling
- Never use bare `except:` — always catch specific exceptions.
- Always log the exception with traceback.
- Network calls must have timeouts (default 30 seconds).
- Database operations must handle connection failures gracefully.
- Failed operations should retry up to 3 times with exponential backoff.

## Git Discipline
- Commit messages follow: `[category] brief description` (e.g., `[schema] add trades table`, `[connector] ccxt wrapper`)
- Never commit broken code. Test before committing.
- Never commit temporary or debug files.

## What NOT To Do
- Never delete production data without explicit confirmation.
- Never modify tickles_shared schema without updating CLAUDE.md.
- Never create files outside of /opt/tickles unless explicitly asked.
- Never install system packages without mentioning it.
- Never run destructive commands (DROP, DELETE, rm -rf) without confirming first.

## Session Handoff Rule

When the user says "wrap up" or "handoff" or "save context":

1. Create a handoff document at `.roo/handoffs/YYYY-MM-DD-{topic}-handoff.md`
2. Include: completed work, in-progress items, decisions, pending TODOs, key file paths, important context
3. Reference any code files with their full paths
4. Tag any unresolved questions clearly
5. End with "Resume command: <exact prompt to give next session>"

When starting a new task, ALWAYS check `.roo/handoffs/` for recent handoff documents from the past 7 days. Read the most relevant one before beginning.

## Mem0 Memory Rules

The dev environment has its own persistent memory namespace, completely isolated from trading agent memory. ALL Roo modes (Architect, Code, Debug, Ask, Orchestrator) MUST use the dev namespace. NEVER use trading company namespaces for dev work.

### Namespace map

- `dev` — build environment (Roo, Claude Code, Hermes, you)
- `rubicon` — test/sandbox trading company (validation only)
- `jarvais` — FROZEN legacy V1, DO NOT WRITE
- Future production company names — assigned later

### When to write to dev memory

Write a memory after:
- A non-trivial decision is made (architecture choice, library selection, schema design)
- A task or milestone is completed
- A bug is identified and fixed
- A file is created or significantly refactored
- The user states a preference or correction

### How to write

```python
from shared.utils.mem0_config import get_dev_memory
mem, agent_id = get_dev_memory(agent="roo")  # or "architect", "code", "debug"

mem.add(
    f"Decision: {what_was_decided}. Context: {why}. Files: {paths}.",
    user_id="dev",
    agent_id=agent_id,
    metadata={"type": "decision", "topic": "intelligence_pipeline"}
)
```

### How to search at task start

```python
mem, agent_id = get_dev_memory(agent="roo")
context = mem.search(
    "intelligence pipeline ChartHacker",
    user_id="dev",
    agent_id=agent_id,
    limit=10
)
```

### Memory hygiene — STRICT RULES

- Dev memory: ALWAYS use `get_dev_memory(agent=...)`
- Trading memory: ONLY use `get_memory(company, agent)` for live trading agents (rubicon, future production firms)
- NEVER write dev decisions to a trading company namespace
- NEVER write trading data to the dev namespace
- NEVER write to `jarvais` — it is frozen legacy
- Tag with `metadata.type`: decision | completion | bug | preference | file_change
- One memory per significant event — don't batch unrelated decisions
