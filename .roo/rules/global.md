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
- Never create files outside of /opt/tickles/ unless explicitly asked.
- Never install system packages without mentioning it.
- Never run destructive commands (DROP, DELETE, rm -rf) without confirming first.
