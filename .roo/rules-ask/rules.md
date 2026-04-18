# Ask Rules

## Response Structure for Complex Questions
1. **UNDERSTAND** — Restate the question to confirm understanding.
2. **CONTEXT** — What do we already know? Reference CLAUDE.md or project docs.
3. **ANALYSIS** — Break down the problem. Consider multiple angles.
4. **RECOMMENDATION** — Give a clear recommendation with rationale.
5. **TRADE-OFFS** — What are we gaining? What are we sacrificing?
6. **NEXT STEPS** — What should happen next if the user agrees?

## Devil's Advocate Triggers
When asked to review or critique, always check:
- What happens if the exchange API goes down for 2 hours?
- What happens if two agents try to trade the same asset simultaneously?
- What happens if the database fills up?
- What happens if the model hallucinates a trade signal?
- What happens if the backtest data has gaps or errors?
- What happens during a market flash crash?
- What happens if this costs 10x more than expected?
- Is this simpler than it needs to be? Is it more complex than it needs to be?

## Screenshot Analysis Protocol
1. Read ALL text in the screenshot — error codes, timestamps, status indicators.
2. Identify the tool/service shown (Paperclip, OpenClaw, terminal, browser, MySQL).
3. Describe what you see factually before interpreting.
4. If it's an error: identify the error type, likely cause, and specific fix.
5. If it's a UI: identify what's configured correctly and what needs attention.

## Trading Ideas Protocol
When the user shares a trading idea:
1. Does this idea have a testable hypothesis? (If not, help formulate one)
2. Can this be backtested with the data we have?
3. What's the minimum viable version of this idea?
4. What would make us STOP using this strategy? (Define failure criteria)
5. How does this interact with existing strategies? (Correlation risk)

## Cost Estimation
When discussing options that have cost implications:
- Estimate monthly API/token costs
- Estimate development time
- Estimate ongoing maintenance burden
- Compare: "Option A costs $X/month but saves Y hours. Option B is free but needs Z maintenance."
