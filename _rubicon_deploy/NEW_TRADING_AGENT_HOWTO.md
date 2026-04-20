# NEW TRADING AGENT — HOW-TO (OpenClaw-native, Twilly-compliant)

**This is the reusable blueprint for spawning Twilly-style LLM trading agents.**
Everything runs on **OpenClaw's own agent + cron primitives** — no custom timers,
no custom runner, no backend edits.

> ✅ Validated 2026-04-20 against Twilly's Surgeon document with `rubicon_surgeon`
> (Claude Sonnet 4.6) and `rubicon_surgeon2` (GPT-4.1). Both cycles ran cleanly,
> updated `TRADE_STATE.md`, appended to `TRADE_LOG.md`, and returned `status: ok`.

---

## 1. What each trading agent is (under the new model)

Each trading agent on the VPS is made of **four OpenClaw-native pieces**:

1. **A registered agent** — `openclaw agents add <id> --workspace <dir> --model <model>`
2. **A workspace directory** with Twilly's files:
   - `SOUL.md` — strategy + persona (Twilly's document verbatim)
   - `TRADE_STATE.md` — live state (balance, open positions, last scan)
   - `TRADE_LOG.md` — append-only trade journal
   - `MARKET_STATE.json`, `MARKET_INDICATORS.json` — **symlinks** to the shared scanner's output
   - `IDENTITY.md`, `BOOTSTRAP.md`, `AGENTS.md`, `TOOLS.md`, `USER.md`, `HEARTBEAT.md` — Twilly companions
3. **A registered cron job** — `openclaw cron add --cron '*/5 * * * *' --agent <id> --tools read,write,exec …`
4. **The shared market scanner** — ONE systemd service (`rubicon-surgeon-scanner.service`)
   that writes `MARKET_STATE.json` + `MARKET_INDICATORS.json` every ~60s into a single
   canonical workspace. All other trading agents read via symlink.

**Key insight:** The cron flag `--tools read,write,exec` scopes the LLM's tool menu to
just the filesystem tools it actually needs. Without this, the agent is fed 50+ MCP tools
per turn and silently returns empty responses (`livenessState: abandoned`). This is the
single most important line in the recipe.

---

## 2. Prerequisites (one-time setup per VPS)

| Thing | Check |
|---|---|
| OpenClaw CLI installed | `openclaw --version` |
| OpenRouter auth configured | `openclaw models list` shows `…configured` on your chosen model |
| Scanner service running | `systemctl status rubicon-surgeon-scanner.service` → `active (running)` |
| Canonical scanner workspace exists | `ls /root/.openclaw/workspace/rubicon_surgeon/MARKET_STATE.json` |

Valid OpenRouter model IDs (as of 2026-04-20):
- `openrouter/openai/gpt-4.1` (default, cheapest)
- `openrouter/anthropic/claude-sonnet-4.6` (Twilly's recommendation — successor to 4.5)
- `openrouter/anthropic/claude-opus-4.6`
- `openrouter/google/gemini-2.5-pro`
- `openrouter/anthropic/claude-sonnet-4`

---

## 3. Create a new trading agent (the 5-step recipe)

All commands run via `ssh vps`. Replace `<ID>` with the agent id (e.g. `rubicon_surgeon3`).

### Step 1 — Prepare the workspace

For a **brand-new agent** (no existing workspace yet), create the directory and seed it
from the canonical Surgeon workspace:

```bash
NEW=<ID>
SRC=/root/.openclaw/workspace/rubicon_surgeon
DST=/root/.openclaw/workspace/${NEW}

mkdir -p "${DST}"

# Copy Twilly's companion files
cp "${SRC}/SOUL.md"       "${DST}/SOUL.md"
cp "${SRC}/AGENTS.md"     "${DST}/AGENTS.md"
cp "${SRC}/BOOTSTRAP.md"  "${DST}/BOOTSTRAP.md"
cp "${SRC}/IDENTITY.md"   "${DST}/IDENTITY.md"
cp "${SRC}/TOOLS.md"      "${DST}/TOOLS.md"
cp "${SRC}/USER.md"       "${DST}/USER.md"
cp "${SRC}/HEARTBEAT.md"  "${DST}/HEARTBEAT.md"

# Start with empty state files
printf '# TRADE_STATE\n\nNo open positions.\n' > "${DST}/TRADE_STATE.md"
: > "${DST}/TRADE_LOG.md"

# Symlink market data (so you only run ONE scanner for all agents)
ln -sf "${SRC}/MARKET_STATE.json"      "${DST}/MARKET_STATE.json"
ln -sf "${SRC}/MARKET_INDICATORS.json" "${DST}/MARKET_INDICATORS.json"
```

### Step 2 — Register the agent

```bash
openclaw agents add ${NEW} \
  --workspace /root/.openclaw/workspace/${NEW} \
  --model openrouter/anthropic/claude-sonnet-4.6 \
  --non-interactive --json
```

### Step 3 — Smoke-test a single turn

Before wiring the cron, confirm the agent responds:

```bash
openclaw agent --agent ${NEW} \
  -m 'PING. Reply with the single word PONG and nothing else.' \
  --json --timeout 120 | tail -20
```

Look for `"finalAssistantVisibleText": "PONG"`, `"livenessState": "working"`,
`"replayInvalid": false`. If instead you see `livenessState: abandoned`, something is
wrong with the agent definition — delete and recreate before proceeding.

### Step 4 — Register the 5-minute trading cron

**The `--tools read,write,exec` flag is mandatory.** It fixes the silent-empty-response bug.

```bash
openclaw cron add \
  --agent ${NEW} \
  --name ${NEW}_cycle \
  --description 'Twilly Surgeon 5-min trading cycle' \
  --cron '*/5 * * * *' \
  --tz UTC \
  --session isolated \
  --tools read,write,exec \
  --thinking low \
  --timeout-seconds 180 \
  --no-deliver \
  --message 'Heartbeat. Execute Surgeon trading cycle per SOUL.md: (1) read TRADE_STATE.md and EXECUTION_REALITY.md if present; (2) read MARKET_STATE.json and MARKET_INDICATORS.json; (3) manage open positions per SOUL exit rules; (4) scan all assets for Mark/Index divergence > 0.15% or extreme funding; (5) enter qualifying trades within max positions limit; (6) overwrite TRADE_STATE.md with current state; (7) APPEND (never overwrite) to TRADE_LOG.md.' \
  --json | tail -30
```

### Step 5 — Force-run once and verify

```bash
# Find the cron id
JOB_ID=$(openclaw cron list | awk -v name="${NEW}_cycle" '$2==name{print $1}')

# Force it to run now (instead of waiting up to 5 min)
openclaw cron run ${JOB_ID}

# Wait ~90 seconds, then check
sleep 90
openclaw cron runs --id ${JOB_ID} | head -40
cat /root/.openclaw/workspace/${NEW}/TRADE_STATE.md
tail -30 /root/.openclaw/workspace/${NEW}/TRADE_LOG.md
```

Success criteria:
- `"action": "finished"`, `"status": "ok"` in the run log
- `TRADE_STATE.md` has a fresh timestamp in "Last Updated" or "Last Scan"
- `TRADE_LOG.md` has a new appended cycle entry

---

## 4. Flag reference (why each flag matters)

| Flag | Value | Why |
|---|---|---|
| `--session isolated` | fixed | Each cron turn runs in its own session — no pollution of the main chat |
| `--tools read,write,exec` | fixed | **Scopes the LLM's tool menu.** Without this, LLM drowns in MCP tools and returns empty |
| `--thinking low` | fixed | Tools-driven work doesn't need deep reasoning; keeps cost + latency down |
| `--timeout-seconds 180` | fixed | LLM + file I/O for a trade cycle completes in well under 3 min |
| `--no-deliver` | fixed | Suppress chat announcements; paper trading should be silent |
| `--tz UTC` | fixed | Deterministic, matches Twilly's log timestamps |
| `--cron '*/5 * * * *'` | fixed | Twilly's mandated 5-min spawn cadence |
| `--model` | omit | Let the cron use the agent's default model (simpler); override only for A/B tests |

---

## 5. Per-agent customisation

You do **not** edit any config file. Everything that differs between agents is set at
`agents add` + `cron add` time. To change something later:

| To change | How |
|---|---|
| Model | `openclaw agents delete <id> --force` then `openclaw agents add <id> --workspace … --model NEW` (workspace preserved) |
| Cron schedule | `openclaw cron edit <job-id> --cron '<new-expr>'` |
| Cron message/prompt | `openclaw cron edit <job-id> --message '<new>'` |
| Strategy (not code) | Edit `SOUL.md` in the workspace — no other change needed |

---

## 6. A/B testing multiple strategies

This is exactly how `rubicon_surgeon` and `rubicon_surgeon2` are run side-by-side:

- `rubicon_surgeon`  → `openrouter/anthropic/claude-sonnet-4.6` (reasoning-heavy model)
- `rubicon_surgeon2` → `openrouter/openai/gpt-4.1` (faster, cheaper, different reasoning style)

Both:
- Read the **same** market data (via symlinks to the shared scanner workspace)
- Use the **same** SOUL.md strategy rules
- Run the **same** 5-min cron schedule
- Keep **separate** TRADE_STATE.md / TRADE_LOG.md

Over time you can compare their P&L curves to see which LLM trades the same rules better.

---

## 7. Verify the whole system (one command)

```bash
ssh vps "
  echo '=== agents ===';           openclaw agents list | grep -E '^- ';
  echo '=== cron jobs ===';        openclaw cron list;
  echo '=== scanner ===';          systemctl is-active rubicon-surgeon-scanner.service;
  echo '=== surgeon1 state ===';   head -8 /root/.openclaw/workspace/rubicon_surgeon/TRADE_STATE.md;
  echo '=== surgeon2 state ===';   head -8 /root/.openclaw/workspace/rubicon_surgeon2/TRADE_STATE.md;
"
```

Healthy output looks like:
- 3 agents listed (`main`, `rubicon_ceo`, `rubicon_surgeon`[, `rubicon_surgeon2`])
- Cron jobs both showing `Status: ok`, `Last: <recent>`, `Next: <soon>`
- Scanner `active`
- Each surgeon's `TRADE_STATE.md` has a fresh `Last Updated` timestamp

---

## 8. Kill / reset / roll back an agent

### Disable without losing history
```bash
JOB_ID=$(openclaw cron list | awk '$2=="<ID>_cycle"{print $1}')
openclaw cron disable ${JOB_ID}
```

### Remove the agent but keep the workspace (trade history preserved)
```bash
openclaw cron rm ${JOB_ID}
openclaw agents delete <ID> --force
# Workspace files still at /root/.openclaw/workspace/<ID>/ for audit
```

### Full nuke (including history)
```bash
openclaw cron rm ${JOB_ID}
openclaw agents delete <ID> --force
rm -rf /root/.openclaw/workspace/<ID>
```

---

## 9. What was REMOVED vs the old HOWTO (and why)

Per the project rule ("leave old code in place, commented out, so we can roll back"),
here is the deprecated approach that this document replaces. It is **no longer used**
but is documented so we can restore it if ever needed.

**Old approach (deprecated 2026-04-20):**
- Custom `surgeon_llm_runner.py` loop called OpenRouter directly
- Custom `tickles-trader-<agent>.service` systemd unit per agent (one timer per agent)
- Custom `spawn_trading_agent.sh` that wrote service files + rendered templates
- Agent was NOT registered with OpenClaw; it was a pure Python daemon

**Why removed:** The user directive was explicit — *"never create your own timer or
heartbeat. Instead get OpenClaw and/or Paperclip working."* OpenClaw already provides
agents + cron + tool-scoping + session isolation natively, so the custom runner was
redundant and drifted from Twilly's document (which specifies an "AI agent," not a
deterministic Python loop wrapping an LLM call).

**How to roll back** (if ever needed):
1. The archived workspace(s) under `/root/.openclaw/_archive_workspace_*/` contain
   the previous state incl. the working `.surgeon_state.json`.
2. The old runner lives in `/opt/tickles/shared/templates/trading_agent/surgeon_llm_runner.py`
   on the VPS (unchanged; still importable).
3. The old systemd unit template is in the same folder as `spawn_trading_agent.sh`.
4. To revert: delete the openclaw cron jobs for the surgeons, then
   `sudo bash /opt/tickles/shared/templates/trading_agent/spawn_trading_agent.sh rubicon surgeon anthropic/claude-sonnet-4.5 10000`.

**What was KEPT from the old stack:**
- `rubicon-surgeon-scanner.service` (pure data ingestion — not a heartbeat).
  This writes MARKET_STATE.json + MARKET_INDICATORS.json every ~60s.
- All workspace files (SOUL.md, AGENTS.md, BOOTSTRAP.md, IDENTITY.md, TOOLS.md,
  USER.md, HEARTBEAT.md, config.json).
- The Twilly SOUL.md strategy rules (unchanged).

---

## 10. Cost expectations (at 5-min cadence, ~8–15k tokens per cycle)

Measured 2026-04-20:

| Model | Tokens / cycle | Est. daily cost |
|---|---|---|
| `openrouter/anthropic/claude-sonnet-4.6` | ~15,000 | ~$5–6 |
| `openrouter/openai/gpt-4.1` | ~10,000 | ~$2–3 |
| `openrouter/openai/gpt-4.1-mini` | ~10,000 | ~$0.30 |

Running both surgeons side-by-side at 5-min cadence: ~$7–9/day combined.

---

## 11. Reference: the exact commands used for the current production agents

```bash
# --- rubicon_surgeon (Claude Sonnet 4.6) -------------------------------
openclaw agents add rubicon_surgeon \
  --workspace /root/.openclaw/workspace/rubicon_surgeon \
  --model openrouter/anthropic/claude-sonnet-4.6 \
  --non-interactive --json

openclaw cron add \
  --agent rubicon_surgeon \
  --name rubicon_surgeon_cycle \
  --description 'Twilly Surgeon 5-min trading cycle' \
  --cron '*/5 * * * *' --tz UTC \
  --session isolated \
  --tools read,write,exec \
  --thinking low --timeout-seconds 180 \
  --no-deliver \
  --message 'Heartbeat. Execute Surgeon trading cycle per SOUL.md: (1) read TRADE_STATE.md and EXECUTION_REALITY.md if present; (2) read MARKET_STATE.json and MARKET_INDICATORS.json; (3) manage open positions per SOUL exit rules; (4) scan all assets for Mark/Index divergence > 0.15% or extreme funding; (5) enter qualifying trades within max positions limit; (6) overwrite TRADE_STATE.md with current state; (7) APPEND (never overwrite) to TRADE_LOG.md.' \
  --json

# --- rubicon_surgeon2 (GPT-4.1 A/B) -------------------------------------
openclaw agents add rubicon_surgeon2 \
  --workspace /root/.openclaw/workspace/rubicon_surgeon2 \
  --model openrouter/openai/gpt-4.1 \
  --non-interactive --json

openclaw cron add \
  --agent rubicon_surgeon2 \
  --name rubicon_surgeon2_cycle \
  --description 'Twilly Surgeon2 5-min trading cycle (GPT-4.1 A/B)' \
  --cron '*/5 * * * *' --tz UTC \
  --session isolated \
  --tools read,write,exec \
  --thinking low --timeout-seconds 180 \
  --no-deliver \
  --message '<same message as above>' \
  --json
```
