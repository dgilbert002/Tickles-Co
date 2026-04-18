# Orchestrator Rules

## Task Breakdown Protocol
1. Every project gets decomposed into at least 3 subtasks: Plan → Implement → Verify.
2. Complex projects get the full cycle: Plan → Critique → Revise → Implement → Review → Fix → Review → Document.
3. Never skip the review step. Even simple tasks get at least one Debug pass.

## Delegation Rules
- Planning and design → Architect mode
- Critiquing plans → Ask mode
- Writing code → Code mode
- Finding bugs → Debug mode
- Answering questions → Ask mode
- Never delegate everything to one mode. Use the right specialist.

## Context Transfer
- When delegating, always include:
  - What was decided in previous subtasks
  - Which files were created or modified
  - Which database tables are involved
  - Any constraints or requirements from the user
- Never assume a subtask knows what happened before it. Pass ALL relevant context.

## Progress Tracking
- After each subtask completes, summarize: what was done, what remains, any blockers.
- If a subtask fails, diagnose why before retrying. Don't blindly retry the same approach.
- Keep a running count: "Subtask 3/7 complete."

## Cost Control
- Estimate token usage before launching parallel subtasks.
- If a task can be done in one Code subtask, don't split it into three.
- Only use parallel execution when subtasks are truly independent.

## Escalation
- If two review passes still find critical issues, escalate to the user before proceeding.
- If a subtask loops more than 3 times without resolution, stop and ask for guidance.
