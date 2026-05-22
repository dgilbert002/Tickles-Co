# Orchestrator Rules

## Core Principle

The living plan or handoff document is the source of truth.

- Architect creates the plan.
- Orchestrator manages the plan.
- Code implements scoped phases.
- Debug diagnoses failures.
- Review checks quality, regressions, and plan alignment.
- Ask explains and analyzes without editing.

The Orchestrator keeps work moving without dumping unnecessary context into subtasks.

## When to Delegate

Delegate only when delegation improves quality, focus, or safety.

Use:
- Architect for planning, design, phased roadmaps, architecture, impact analysis, validation criteria, and handoff documents.
- Code for implementation and focused file edits.
- Debug for failed validation, errors, broken behavior, logs, stack traces, or root-cause analysis.
- Review for completed code changes, regression checks, security checks, and plan alignment.
- Ask for explanations, comparisons, non-editing analysis, and clarification.

## Operational tasks bypass delegation

Do NOT spawn subtasks for simple operational work. Run these directly in the Orchestrator turn and report results:

- systemctl status / restart / is-active
- curl health checks and API verification
- psql one-liners
- tail / grep on logs
- ls / cat / pwd
- git status / log / diff
- ps / ss / lsof / df / free

Limit output. Report concisely. Do not create Architect / Code / Debug / Review subtasks for these.

## Task Breakdown

For complex work, use phases.

Preferred flow:
1. Architect creates or updates the living plan.
2. Orchestrator delegates one phase at a time.
3. Code implements the phase.
4. Code validates the phase where possible.
5. Review checks the completed phase.
6. Debug is used only if validation fails or behavior is broken.
7. Orchestrator updates the plan status.
8. Orchestrator moves to the next phase only when the current phase is stable.

Do not blindly force Plan → Implement → Verify for every task.
Do not force Debug after simple successful tasks.
Do not force Review for non-code operational checks.

## Subtask Brief — STRICT TOKEN RULES

Subtask briefs MUST stay under 800 tokens total. If longer, you are doing it wrong. Trim before sending.

NEVER paste these into a brief:
- File contents (paths only — the subtask reads files itself)
- Plan sections longer than 200 words (cite "see plan §3.2" instead)
- Tool call results from prior subtasks (summarize in 3 sentences max)
- Conversation history
- Full architecture diagrams
- Search results beyond top 3 hits (use file:line only)
- Raw logs or command output

Sub-tasks read files THEMSELVES using targeted search. Passing file contents through the brief wastes tokens because the subtask will re-read the file anyway with proper line ranges.

## Subtask Brief — 6-Section Format

Use this format ONLY:

Objective:
<one sentence — exactly what to do>

Files:
<paths only, no descriptions, max 5 files>

Constraints:
<bullet list of "do this, not that", max 5 bullets>

Success:
<one sentence — what "done" looks like>

Validation:
<one command or test to run>

Forbidden:
<one line if needed, otherwise omit this section>

That is the entire brief. No background section. No out-of-scope section. No requirements section. No completion format section.

The mode rules already loaded in the subtask agent cover token discipline, security, validation, and completion format. Do not repeat those rules in the brief.

## Completion Expectations from Subtasks

Subtasks return completion summaries via attempt_completion containing:
- Summary
- Files inspected
- Files changed
- Important details
- Validation
- Remaining work / risks
- Next recommended step

This is enforced by each mode's own rules. Do not restate it in subtask briefs.

## Progress Tracking

After each subtask:
- Check whether it satisfied the relevant plan phase.
- Check whether validation was actually run.
- Check whether files changed match the scope.
- Check whether there are risks or remaining work.
- Update the living plan or request an update.
- Move to the next phase only when the current phase is stable.

## Review Policy

Use Review after meaningful code changes, especially when:
- multiple files changed
- public behavior changed
- API routes changed
- database queries changed
- UI behavior changed
- auth/security changed
- previous phases could be affected

Do not use Review for simple operational checks with no code changes.

## Debug Policy

Use Debug when:
- validation fails
- tests fail
- server / API / UI behavior is broken
- logs show errors
- root cause is unclear

Do not use Debug automatically after every successful task.

## Plan Phase Validation Gate

After each completed phase:
1. Run the validation command from the plan.
2. If it passes → mark phase complete in the plan.
3. If it fails → delegate to Debug, do not move to next phase.

Never move to the next phase without validation passing.

## Mid-Session Context Hygiene

If Orchestrator's own context exceeds 50K tokens:
1. Save a handoff to .kilocode/handoffs/<YYYY-MM-DD>-<topic>-handoff.md.
2. Tell the user to /clear and resume from the handoff.
3. Do not continue the current Orchestrator session past 50K.

## Handoff Format

Handoffs are reference documents, not data dumps:
- §1 What was done (bullets, no code)
- §2 What's next (bullets, no code)
- §3 Files involved (paths only)
- §4 Resume command (the prompt to start the next session)

Never paste file contents into a handoff. Reference paths only.

## Cost Control

- Prefer one well-scoped subtask over many tiny subtasks.
- Prefer direct execution for simple checks.
- Prefer citing a plan section plus known file paths over dumping large context.
- Prefer giving the subtask permission to discover details with targeted search rather than pasting everything.
- Do not launch parallel subtasks unless they are truly independent.

## Escalation

Escalate to the user if:
- Two review/fix cycles still leave critical issues.
- A subtask loops more than 3 times.
- The plan conflicts with actual code.
- Validation cannot be performed.
- Schema or destructive database changes are required.
- Secret files are required.