# 2026-05-03 — `.md` → mem0 APPLY FAILED (Phase 5 of §4.3)

**Status:** 🔴 **BLOCKED — script defect, partial state recovered, no data lost**
**Authored by:** Code mode (Roo)
**Trigger:** Master handoff §4.3 Phase 5 — user authorized "do everything to the end"
**Outcome:** `--apply` step crashed on every file with mem0 `ValidationError`. Source files restored. No daemons were stopped (none needed stopping). Two empty Qdrant collections remain.

---

## §0 — TL;DR

- Pre-flight PF1 (open positions = 0) ✅ PASS
- Pre-flight PF2 (no legacy surgeon services to stop) ✅ PASS (n/a)
- Step 1 `--dry-run` ✅ PASS — manifest at [`/tmp/manifest_pre_apply.json`](/tmp/manifest_pre_apply.json) with 4 clean candidates
- Step 2 `--apply` ❌ **FAILED** — every `mem.add(...)` raised `ValidationError: At least one of 'user_id', 'agent_id', or 'run_id' must be provided`
- **Root cause:** [`shared/scripts/migrate_md_to_mem0.py`](shared/scripts/migrate_md_to_mem0.py:175) calls `mem.add()` without the required `user_id` / `agent_id` kwargs. The script discards the `agent_id` returned by [`get_memory()`](shared/utils/mem0_config.py:187) (`mem, _agent_id = get_memory(...)`) instead of forwarding it.
- **Data integrity:** No corruption. All 4 source files restored to `.md`. Two empty Qdrant collections (`tickles_rubicon_surgeon`, `tickles_rubicon_surgeon2`, 0 points each) created as side-effect, but contain no garbage.
- **Hard-constraint compliance:** Per the user's explicit recipe ("If anything in Steps 1-4 fails unexpectedly: STOP, write a failure handoff, restart any stopped daemons, and report back. Do NOT push partial state forward."), Steps 3-7 were **NOT executed**.

---

## §1 — Pre-flight gates (both passed)

### PF1 — `tracked_positions` open count = 0

The handoff originally said `tickles_rubicon`, but the table actually lives in `tickles_shared` (public schema), and the column is `status` not `state`.

```sql
-- Run against tickles_shared (public schema)
SELECT status, COUNT(*) FROM public.tracked_positions GROUP BY status;
-- Result:
--   closed  | 77
--   (no 'open' row)
```

**Verdict:** PASS — safe to migrate.

### PF2 — Active legacy surgeon services

```
systemctl list-units --type=service --state=running | grep -i surgeon
```

Only `rubicon-surgeon-scanner.service` is running, which is the V2 scanner (writes JSON only, never `.md`). No legacy surgeon `.md`-writing daemon is active. Nothing was stopped, nothing needs restarting.

**Verdict:** PASS, no-op.

---

## §2 — Step 1: `--dry-run` (clean)

```
python -m shared.scripts.migrate_md_to_mem0 \
    --roots /root/.openclaw/workspace \
    --allow-fallback-owner \
    --dry-run
```

Captured at [`/tmp/manifest_pre_apply.json`](/tmp/manifest_pre_apply.json):

| path | company | agent | size_bytes | mtime_utc | kind |
|---|---|---|---|---|---|
| `/root/.openclaw/workspace/rubicon_surgeon/TRADE_LOG.md` | rubicon_surgeon | surgeon | 96848 | 2026-04-24T10:37:33+00:00 | TRADE_LOG.md |
| `/root/.openclaw/workspace/rubicon_surgeon/TRADE_STATE.md` | rubicon_surgeon | surgeon | 448 | 2026-04-24T10:37:33+00:00 | TRADE_STATE.md |
| `/root/.openclaw/workspace/rubicon_surgeon2/TRADE_LOG.md` | rubicon_surgeon2 | surgeon | 466 | 2026-04-20T09:47:20+00:00 | TRADE_LOG.md |
| `/root/.openclaw/workspace/rubicon_surgeon2/TRADE_STATE.md` | rubicon_surgeon2 | surgeon | 972 | 2026-04-20T09:47:16+00:00 | TRADE_STATE.md |

All four resolved owner via fallback (no `meta.json` ancestor) — companies = `rubicon_surgeon` / `rubicon_surgeon2`, agent = `surgeon`. The script only logged the fallback warnings (acceptable per `--allow-fallback-owner`).

**Verdict:** PASS — dry-run did exactly what the audit document promised.

---

## §3 — Step 2: `--apply` (FAILED)

```
python -m shared.scripts.migrate_md_to_mem0 \
    --roots /root/.openclaw/workspace \
    --allow-fallback-owner \
    --apply \
  | tee /tmp/manifest_apply.json
```

### §3.1 What the manifest now reports

Final manifest at [`shared/scripts/migration_manifest.json`](shared/scripts/migration_manifest.json:1):

| file | status | entries_written | renamed? |
|---|---|---|---|
| `rubicon_surgeon/TRADE_LOG.md` | **applied** | **0** | **YES → `.migrated`** ⚠️ |
| `rubicon_surgeon/TRADE_STATE.md` | error_replay | 0 | no |
| `rubicon_surgeon2/TRADE_LOG.md` | **applied** | **0** | **YES → `.migrated`** ⚠️ |
| `rubicon_surgeon2/TRADE_STATE.md` | error_replay | 0 | no |

### §3.2 Exact error from log

```
[mem0] add FAIL | model=google/gemini-2.0-flash-001 |
  ValidationError: At least one of 'user_id', 'agent_id', or 'run_id' must be provided.

ERROR __main__ mem.add failed for trade_id=1 in company=rubicon_surgeon:
  [mem0] add exhausted all models
  ['deepseek/deepseek-chat', 'openai/gpt-4o-mini', 'google/gemini-2.0-flash-001']:
  At least one of 'user_id', 'agent_id', or 'run_id' must be provided.
```

The LLM-fallback layer in [`ScopedMemory.add()`](shared/utils/mem0_config.py:137) tried all 3 configured models and each one re-raised the same mem0-core `ValidationError`, because the missing kwarg is upstream of model selection.

### §3.3 Root cause — script defect

Two `mem.add(...)` call sites omit the identifier kwargs that mem0 0.x requires:

**Site A — [`replay_trade_state`](shared/scripts/migrate_md_to_mem0.py:148) (line 174-183):**
```python
mem, _agent_id = get_memory(company, agent)   # ← agent_id discarded
mem.add(
    text,
    metadata={
        "type": "trade_state_snapshot",
        "kind": "historical_migration",
        "original_timestamp": mtime_iso,
        "source_file": "TRADE_STATE.md",
    },
)   # ← no user_id, no agent_id, no run_id → ValidationError
```

**Site B — [`replay_trade_log`](shared/scripts/migrate_md_to_mem0.py:221) raw_dump branch (line 260-269):**
Same defect on the R2 fallback path.

**Site C — [`replay_trade_log`](shared/scripts/migrate_md_to_mem0.py:294) per-entry loop (line 299-310):**
Same defect, but additionally wrapped in a per-entry `try/except ... continue` (line 312-320) that silently swallows every failure. This is why the TRADE_LOG files ended up with `status="applied"` and `entries_written=0` — the loop completed "successfully" with zero increments, then [`process_file`](shared/scripts/migrate_md_to_mem0.py:418) renamed the source.

### §3.4 The contract being violated

[`shared/utils/mem0_config.py`](shared/utils/mem0_config.py:187) docstring (line 198-205):

```python
# Example:
#   memory, agent_id = get_memory("rubicon", "code_mode")
#   memory.add("candle_service.py uses instrument_id FK", user_id="rubicon", agent_id=agent_id)
```

The script ignores both halves of that contract: it discards `agent_id` (binding it to the throwaway `_agent_id`), and it never passes `user_id=company, agent_id=agent_id` into `add()`.

### §3.5 Why this wasn't caught earlier

Per master handoff §4.3, Phases 1-4 were tested with the **dry-run only**. Dry-run short-circuits before `mem.add()` (lines 166-167, 252-253, 279-280), so the bug never executed in any prior test. The audit doc §4.2 specs Phases 2-4 as "code mode (1 day / 0.5 day / 0.5 day)" — apparently no live integration test was run before sign-off.

---

## §4 — Recovery actions taken

1. **Renamed `.md.migrated` files back to `.md`** so the source is recoverable for a re-run:
   ```
   mv /root/.openclaw/workspace/rubicon_surgeon/TRADE_LOG.md.migrated  → TRADE_LOG.md
   mv /root/.openclaw/workspace/rubicon_surgeon2/TRADE_LOG.md.migrated → TRADE_LOG.md
   ```
2. **Verified all 4 source files** — sizes match the dry-run manifest exactly:
   - `rubicon_surgeon/TRADE_LOG.md` 96848 bytes ✅
   - `rubicon_surgeon/TRADE_STATE.md` 448 bytes ✅ (was never renamed; failed before rename)
   - `rubicon_surgeon2/TRADE_LOG.md` 466 bytes ✅
   - `rubicon_surgeon2/TRADE_STATE.md` 972 bytes ✅ (was never renamed; failed before rename)
   - Pre-existing `.md.bak.20260420T065729Z` backups untouched.
3. **No daemons restarted** — none were stopped in PF2 (the constraint forbade stopping `surgeon2`, and no legacy surgeon `.md`-writers were running).

### §4.1 Cosmetic detritus NOT cleaned up

Two empty Qdrant collections were created by the failed `mem.add()` initialization (collection-create succeeds before validation runs):

```
$ curl -s http://localhost:6333/collections
... tickles_rubicon_surgeon (status=green, points_count=0)
... tickles_rubicon_surgeon2 (status=green, points_count=0)
```

I did **not** delete these because:
- They contain zero points (no garbage data).
- The next successful `--apply` run will reuse these exact collection names — deleting and recreating is pointless churn.
- The user's recipe did not authorize cleanup outside the documented Steps 1-7.

If undesired, drop with:
```
curl -X DELETE http://localhost:6333/collections/tickles_rubicon_surgeon
curl -X DELETE http://localhost:6333/collections/tickles_rubicon_surgeon2
```

---

## §5 — Why I stopped (constraint compliance)

Quoting the user's recipe verbatim:

> **Hard constraints:**
> - DO NOT add `--force` or any new flags to the migrate script — it's already shipped and tested.
> - If anything in Steps 1-4 fails unexpectedly: STOP, write a failure handoff to `.roo/handoffs/2026-05-03-md-vs-mem0-apply-FAILED.md`, restart any stopped daemons, and report back. Do NOT push partial state forward.

Step 2 failed unexpectedly. The fix requires modifying [`migrate_md_to_mem0.py`](shared/scripts/migrate_md_to_mem0.py:1), which the user said is "already shipped and tested" and forbade modifying. So I:
- Stopped Steps 3-7 (verify / restart / commit / handoff-update / mem0-memory) ❌ NOT executed
- Reverted partial state (the two erroneous renames) ✅ DONE
- Did NOT push partial state forward ✅ DONE
- Wrote this handoff ✅ DONE

---

## §6 — Recommended fix (for the next session, requires authorization)

Make three minimal, in-place edits to [`shared/scripts/migrate_md_to_mem0.py`](shared/scripts/migrate_md_to_mem0.py:1). No new flags, no behavioural change beyond making `mem.add()` actually succeed.

### Edit 1 — `replay_trade_state` (line 174-183)

```python
mem, agent_id = get_memory(company, agent)
mem.add(
    text,
    user_id=company,
    agent_id=agent_id,
    metadata={
        "type": "trade_state_snapshot",
        "kind": "historical_migration",
        "original_timestamp": mtime_iso,
        "source_file": "TRADE_STATE.md",
    },
)
```

### Edit 2 — `replay_trade_log` raw_dump branch (line 260-269)

```python
mem, agent_id = get_memory(company, agent)
mem.add(
    text,
    user_id=company,
    agent_id=agent_id,
    metadata={
        "type": "trade_decision",
        "kind": "raw_dump",
        "original_timestamp": fallback_iso,
        "source_file": "TRADE_LOG.md",
    },
)
```

### Edit 3 — `replay_trade_log` per-entry loop (line 289 + 299-310)

```python
mem, agent_id = get_memory(company, agent)   # rename _agent_id → agent_id
...
for m in matches:
    entry = m.group("header").strip()
    parsed = _parse_log_entry(entry, fallback_iso)
    try:
        mem.add(
            entry,
            user_id=company,
            agent_id=agent_id,
            metadata={
                "type": "trade_decision",
                "kind": "historical_migration",
                "action": parsed["action"],
                "trade_id": parsed["trade_id"],
                "symbol": parsed["symbol"],
                "original_timestamp": parsed["original_timestamp"],
                "source_file": "TRADE_LOG.md",
            },
        )
        count += 1
    except Exception as exc:
        logger.error(...)
        continue
```

### Edit 4 (recommended, defensive) — fail-fast on zero entries

The silent `entries_written=0` outcome that still triggered file rename is dangerous. Suggest [`process_file`](shared/scripts/migrate_md_to_mem0.py:418) refuse to rename when `count == 0` AND the source had non-empty parseable content. Optional, but it would have caught this bug instantly instead of producing the misleading `status="applied"`.

---

## §7 — Resume command for next session

When the user has decided how to proceed, the resume prompt is:

```
Read .roo/handoffs/2026-05-03-md-vs-mem0-apply-FAILED.md.

Apply the four edits in §6 to shared/scripts/migrate_md_to_mem0.py
(pass user_id=company, agent_id=agent_id to all three mem.add() call sites,
plus the optional fail-fast guard in process_file). Add a smoke test that
exercises the apply path against a temp file with monkeypatched mem0,
asserting user_id+agent_id reach the mem0 mock. Then re-run the original
§4.3 Phase 5 recipe from PF1 onwards.
```

---

## §8 — Files / artifacts referenced

- [`.roo/handoffs/2026-05-03-master-resume-handoff.md`](.roo/handoffs/2026-05-03-master-resume-handoff.md:344) §4.3 (parent task)
- [`.roo/handoffs/2026-05-03-md-vs-mem0-audit.md`](.roo/handoffs/2026-05-03-md-vs-mem0-audit.md:459) §6 Phase 5 (apply spec)
- [`shared/scripts/migrate_md_to_mem0.py`](shared/scripts/migrate_md_to_mem0.py:1) (defective)
- [`shared/utils/mem0_config.py`](shared/utils/mem0_config.py:187) (`get_memory` contract)
- [`shared/scripts/migration_manifest.json`](shared/scripts/migration_manifest.json:1) (current failure manifest)
- [`/tmp/manifest_pre_apply.json`](/tmp/manifest_pre_apply.json) (clean dry-run manifest)
- [`/tmp/manifest_apply.json`](/tmp/manifest_apply.json) (failed apply log + JSON tail)
- 4 source files at `/root/.openclaw/workspace/rubicon_surgeon{,2}/TRADE_{STATE,LOG}.md` — all restored.

---

## §9 — Sign-off

Pre-flight gates: PASS. Dry-run: PASS. Apply: BLOCKED on shipped-script defect. Source data: intact. Partial state: reverted. Daemons: untouched. Empty Qdrant collections: left in place pending decision. Steps 3-7 of the original recipe: deferred pending user authorization to either (a) modify the migrate script or (b) take some other path.

**Awaiting human decision on §6 fix authorization.**
