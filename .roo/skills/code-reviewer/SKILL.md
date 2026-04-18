---
name: code-reviewer
description: Run a multi-pass code review on any file or directory. Use when asked to review, audit, or check code quality. Performs 3 passes with increasing strictness.
---

# Code Review Skill

## When Activated
User asks to "review", "audit", "check quality", or "analyze" code.

## Process

### Pass 1 — Structural Review
Read every file in scope. Check for:
- Missing docstrings or type hints
- Functions over 50 lines
- Missing error handling
- Hardcoded values
- Import organization
- File/class naming conventions

Report: "Pass 1: Found X issues (Y critical, Z medium, W low)"

### Pass 2 — Logic Review
For each function, trace the logic mentally:
- What happens with empty/null input?
- What happens if the database is unreachable?
- What happens if the API returns an error?
- Are there race conditions?
- Are there off-by-one errors?
- Is decimal precision sufficient for financial calculations?

Report: "Pass 2: Found X additional issues"

### Pass 3 — Security Review
Check for:
- SQL injection (string interpolation in queries)
- Hardcoded credentials or API keys
- Sensitive data in log outputs
- Missing input validation
- Unsafe file operations (rm, DELETE without confirmation)

Report: "Pass 3: Found X security concerns"

### Final Report
Present a summary table:
| Pass | Critical | Medium | Low | Total |
|------|----------|--------|-----|-------|

List all issues with file, line number, severity, and recommended fix.
