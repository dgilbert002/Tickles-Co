---
name: bug-hunter
description: Systematically hunt for bugs in code. Use when asked to find bugs, debug issues, or investigate errors. Goes beyond surface-level checking.
---

# Bug Hunter Skill

## When Activated
User asks to "find bugs", "hunt bugs", "investigate", or "what's wrong with this code".

## Methodology

### Step 1 — Read Everything
Read all files in scope. Build a mental map of:
- What calls what
- Where data flows
- Where external services are called
- Where state is mutated

### Step 2 — Check Common Trading System Bugs
- [ ] Timezone: Are all timestamps UTC? Any local time conversions?
- [ ] Candle alignment: Is the signal using the closed candle or the forming candle?
- [ ] Decimal precision: Any float arithmetic on financial values? (Must use Decimal)
- [ ] Rate limits: Are exchange API calls throttled?
- [ ] Duplicate prevention: Can the same trade/signal/candle be processed twice?
- [ ] Fee calculation: Are trading fees included in P&L?
- [ ] Balance check: Can position sizing exceed available balance?
- [ ] Connection handling: What happens when MySQL/exchange connection drops?

### Step 3 — Trace Edge Cases
For each function, ask:
- What if this is called with None?
- What if the list is empty?
- What if the API returns an empty response?
- What if two processes call this simultaneously?
- What if the value is negative?
- What if the string contains special characters?

### Step 4 — Report
For each bug found:
```
BUG: [title]
File: [path]
Line: [number]
Severity: Critical / Medium / Low
What happens: [description of the failure]
Root cause: [why it's broken]
Fix: [specific code change needed]
```
