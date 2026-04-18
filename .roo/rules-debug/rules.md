# Debug Rules

## First Response Protocol
When presented with a bug or error:
1. DO NOT immediately suggest a fix.
2. First ask: "What was the expected behavior?" and "What actually happened?"
3. If shown a screenshot or error message, read EVERY detail before responding.
4. Identify: Is this a code bug, config issue, data issue, or infrastructure issue?

## Investigation Checklist
For every bug, systematically check:
- [ ] Is the service running? (systemctl status, pm2 list, docker ps)
- [ ] Are the environment variables set correctly?
- [ ] Is the database reachable? Can you query it?
- [ ] Are the API keys valid and not expired?
- [ ] Are the file permissions correct?
- [ ] Is disk space available? (df -h)
- [ ] Is memory available? (free -m)
- [ ] Are there relevant log entries? (journalctl, pm2 logs, /var/log/)

## Common Trading System Bugs
Check for these first — they're the most frequent:
1. **Timezone mismatch** — is the candle timestamp UTC? Is the comparison in UTC?
2. **Off-by-one candle** — is the signal using the closed candle or the forming candle?
3. **Decimal precision** — is float arithmetic causing rounding errors? Use Decimal.
4. **Rate limiting** — is the exchange API returning 429? Check request frequency.
5. **Stale data** — is the candle cache returning old data?
6. **Duplicate trades** — is the unique constraint working? Check for race conditions.
7. **Missing fees** — is P&L calculated with or without trading fees?

## Fix Verification Protocol
After applying a fix:
1. Run the specific failing case — does it pass now?
2. Run related test cases — did the fix break anything else?
3. Check edge cases: empty input, null values, maximum values, negative values.
4. If it's a database fix: verify with a SELECT query that the data looks right.
5. If it's a service fix: verify the service stays running for at least 60 seconds.

## Multi-Pass Review
When asked to review code quality (not a specific bug):
- PASS 1: Read every file. List all issues found. Categorize as Critical / Medium / Low.
- PASS 2: After fixes are applied, re-read every file. List remaining issues.
- PASS 3: If Pass 2 found issues, review again after those are fixed.
- Only report "clean" when a full pass finds ZERO issues.
- Format: "Pass 1: 7 issues (2 critical, 3 medium, 2 low). Pass 2: 1 issue (1 low). Pass 3: Clean."

## Log Analysis
When reading logs:
1. Start from the BOTTOM (most recent entries first).
2. Look for ERROR and WARNING levels first.
3. Note timestamps — when did the problem start?
4. Look for patterns — does it happen every N minutes? After a specific event?
5. Cross-reference with other service logs — did something else fail first?

## Escalation Rules
- If you can't reproduce the bug after 3 attempts, report what you tried and ask for more info.
- If the fix requires changing the database schema, flag it — that's an Architect decision.
- If the fix requires changing multiple services, flag it — that's an Orchestrator decision.
- Never apply a "temporary workaround" without clearly labeling it as such.
