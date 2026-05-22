# Global Rules — All Modes

## Before Starting Any Task

1. If a living plan or handoff document exists for the task, read only the relevant section first.
2. Read `/opt/tickles/CLAUDE.md` only when the task requires broad project context, architecture context, schema context, or unclear system ownership.
3. If a project folder is involved, check for a README.md or CONTEXT.md in that folder when relevant.
4. When starting a new task, check `.kilocode/handoffs/` and `.roo/handoffs/` for recent handoff documents from the past 7 days. Read the most relevant section only.
5. Never assume what exists. Verify with targeted file listing, search, or line-range reads.

## Hard Token Rules — All Modes

These rules apply universally across Code, Ask, Architect, Debug, Orchestrator, and Review modes.

### Brief and handoff limits
- Subtask briefs: under 800 tokens. If longer, trim before sending.
- Tool call results carried forward: summarize in 3 sentences before next turn.
- Plan references: cite "§section" — do not paste plan content into briefs or replies.

### File access discipline
- For files over 20 KB: use grep, symbol lookup, or line ranges before reading whole file.
- Never read full file contents into context just to "have them available."
- Pass file paths, not file contents, when delegating to subtasks.
- Do not re-read files you already read in the same task — refer to your prior read.

### Output truncation
- Logs: use `tail -N` and `grep` — never paste full logs.
- API responses: use `jq`, `head`, or Python field extraction.
- Search results: top 3 hits with `file:line` only — not all 10 with content.
- Command output longer than 80 lines: summarize first.
- psql query output: use LIMIT and only the columns needed.

### Verification discipline
- Do NOT loop running multiple verification commands when one would do.
- Do NOT run `python ast.parse` on every file you touched — pick one or two critical ones only.
- Do NOT verify by re-reading files you just edited (the str_replace already validated the patch applied).
- One curl + one syntax check is enough for most changes.
- Trust the str_replace tool — if it succeeded, the patch was applied correctly.

### Mid-session context hygiene
- If a session's context exceeds 50K tokens: save a handoff, /clear, resume in new session.
- Handoffs go to `.kilocode/handoffs/<YYYY-MM-DD>-<topic>-handoff.md`.
- Handoffs are reference docs (paths and summaries), not data dumps (file contents).

### What NOT to read
Never read these unless explicitly required by the current task:
- `.env`, `auth-state.json`, `auth-profiles.json`, token files, cookie files
- Private keys, password files, session files, credential files
- Generated assets (SVGs, images, dumps, archives, backups)
- `node_modules`, `.git`, build outputs, lock files
- Virtual environments (`.venv`, `venv`, `env`)

## Code Standards

- Language: Python 3.12 unless explicitly told otherwise.
- Style: PEP 8, 4-space indentation, max line length 120 characters.
- Imports: stdlib first, third-party second, local third. Separated by blank lines.
- Strings: Use f-strings for formatting. Double quotes for strings.
- Paths: Use `pathlib.Path`, never `os.path` string concatenation.

## Database Standards

- All queries use parameterized statements. NEVER string interpolation.
- All timestamps stored as UTC datetime.
- All monetary values use `decimal(20,8)` for prices, `decimal(30,8)` for volumes.
- Connection strings read from environment variables, never hardcoded.
- Use LIMIT on exploratory queries.

## Security — Non-Negotiable

- Never hardcode API keys, passwords, or tokens in code.
- Never log sensitive data (API keys, passwords, account balances).
- Never commit `.env` files.
- All exchange API keys must be trade-only (no withdrawal permissions).
- Never read, summarize, print, or transmit secret files unless explicitly requested in the same message.

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
- Never modify `tickles_shared` schema without updating `CLAUDE.md`.
- Never create files outside of `/opt/tickles` unless explicitly asked.
- Never install system packages without mentioning it.
- Never run destructive commands (DROP, DELETE, rm -rf) without confirming first.

## Completion Summary — Required for All Tasks

Every task completion must include:

- **Summary** — what was done in 1-2 sentences
- **Reason** — why it was needed
- **Files inspected** — paths only
- **Files changed** — paths with brief description of each change
- **Important details** — functions, parameters, types, routes, config affected
- **Validation** — commands run and results
- **Remaining work / risks** — anything uncertain or pending
- **Next recommended step** — Review, Debug, next phase, update plan, or stop

Rules:
- Do not say something is fixed or tested unless it actually was.
- Do not end with vague offers.
- Do not hide uncertainty.
- Do not overstate success.

## Session Handoff Rule

When the user says "wrap up", "handoff", "save context", or context exceeds 50K tokens:

1. Create a handoff document at `.kilocode/handoffs/YYYY-MM-DD-{topic}-handoff.md`
2. Include sections:
   - §1 What was done (bullets, no code)
   - §2 What's next (bullets, no code)
   - §3 Files involved (paths only — never paste contents)
   - §4 Decisions made
   - §5 Pending TODOs and unresolved questions
   - §6 Resume command (exact prompt to give next session)
3. Reference code files with full paths.
4. Tag unresolved questions clearly.
5. Never paste file contents into a handoff.

## Mem0 Memory Rules

The dev environment has its own persistent memory namespace, completely isolated from trading agent memory. ALL modes (Architect, Code, Debug, Ask, Orchestrator) MUST use the dev namespace for dev work. NEVER use trading company namespaces for dev work.

### Namespace map

- `dev` — build environment (Roo, Kilo, Claude Code, Hermes, you)
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
mem, agent_id = get_dev_memory(agent="roo")  # or "architect", "code", "debug", "kilo"

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
- Tag with `metadata.type`: `decision` | `completion` | `bug` | `preference` | `file_change`
- One memory per significant event — don't batch unrelated decisions