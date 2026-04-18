---
name: refactorer
description: Refactor code for better structure, readability, and maintainability. Use when asked to clean up, restructure, simplify, or improve code organization.
---

# Refactorer Skill

## When Activated
User asks to "refactor", "clean up", "restructure", "simplify", or "improve" code.

## Process

### Step 1 — Analyze Before Changing
Read all files in scope. Identify:
- Duplicate code that should be shared
- Functions doing too many things
- God classes or god files
- Inconsistent patterns across files
- Dead code that's never called
- Overly complex logic that could be simplified

### Step 2 — Plan the Refactoring
Present the plan BEFORE making changes:
- What will be moved/renamed/split/merged
- Why each change improves the code
- What risks each change introduces
- Estimated number of files affected

### Step 3 — Execute Incrementally
- Make ONE change at a time
- After each change, verify nothing is broken
- Never refactor and add features simultaneously

### Step 4 — Verify
- All existing tests still pass
- The code still runs correctly
- No circular imports were introduced
- Documentation was updated

## Refactoring Rules
1. Never change behavior while refactoring — same inputs must produce same outputs
2. Never rename database columns without a migration plan
3. Always update imports in all files that reference moved code
4. Always update __init__.py when moving modules
5. Keep git commits granular — one refactoring step per commit
