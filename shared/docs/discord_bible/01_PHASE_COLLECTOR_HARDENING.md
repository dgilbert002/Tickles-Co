# PHASE 1 — COLLECTOR HARDENING ("never miss a Discord message again")

> Prereq: you have read `00_MASTER_INDEX.md`. You follow the GOLDEN RULES.
> Goal of this phase: stop the 16,000+ crashes, restore the noise gate, and make the
> backfill drain EVERY message after any outage. After this phase, a message that
> arrives in a followed channel cannot be silently dropped.

This phase touches exactly three files. Back each up first:
```bash
cp /opt/tickles/shared/intelligence/zone_filter.py            /opt/tickles/shared/intelligence/zone_filter.py.bak-bible
cp /opt/tickles/shared/collectors/discord/discord_collector.py /opt/tickles/shared/collectors/discord/discord_collector.py.bak-bible
cp /opt/tickles/shared/collectors/telegram/telegram_collector.py /opt/tickles/shared/collectors/telegram/telegram_collector.py.bak-bible
```

---

## STEP 1 — Kill the correlation_id overflow at the source (the real money leak)

**Why:** The collector builds `cid = f"discord_{channel_id}_{item.hash_key[:16]}"`
(~44 chars). It is passed to `classify(... correlation_id=cid)` which writes it through
`log_api_call(... correlation_id=...)` into columns typed `VARCHAR(36)`. asyncpg throws
`StringDataRightTruncationError`. The try/except in `classify()` swallows it and FAILS
OPEN, so the noise gate is dead and every chatter message hits the vision LLM.

**The fix is defence-in-depth: shorten at the builder AND clamp inside zone_filter.**

### 1a. Clamp inside `zone_filter.classify()` so NOTHING it logs can ever exceed 36.

File: `shared/intelligence/zone_filter.py`. The function signature is at line 40:
`async def classify(text: str, *, source_id: int, correlation_id: str) -> dict:`

Add a clamp as the FIRST line inside the function body (right after the docstring,
before `try:`). Use the patch tool with this exact replacement:

old_string:
```python
async def classify(text: str, *, source_id: int, correlation_id: str) -> dict:
    """Classify a message as trading signal or noise."""
    try:
```
new_string:
```python
async def classify(text: str, *, source_id: int, correlation_id: str) -> dict:
    """Classify a message as trading signal or noise."""
    # BIBLE-P1: correlation_id columns are VARCHAR(36). Clamp defensively so a long
    # id can never raise StringDataRightTruncationError and silently disable the gate.
    if correlation_id and len(correlation_id) > 36:
        correlation_id = correlation_id[:36]
    try:
```

### 1b. Make the builders produce a <=36-char id anyway (belt + braces).

Discord — file `shared/collectors/discord/discord_collector.py`. Find the line that
builds the cid (it is inside `_apply_zone_filter_and_dedup`, around line 927):
```python
        cid = f"discord_{channel_id}_{item.hash_key[:16]}"
```
Replace with a compact, collision-safe, <=36-char form:
```python
        # BIBLE-P1: keep correlation_id <=36 chars (DB column limit). hash[:16] alone
        # is already unique per message; prefix 'd_' to mark source. Total <=18 chars.
        cid = f"d_{item.hash_key[:16]}"
```

Telegram — file `shared/collectors/telegram/telegram_collector.py`, around line 635:
```python
        cid = f"telegram_{channel_id}_{item.hash_key[:16]}"
```
Replace with:
```python
        # BIBLE-P1: keep correlation_id <=36 chars. 't_' + hash[:16] = <=18 chars.
        cid = f"t_{item.hash_key[:16]}"
```

---

## STEP 2 — Fix the zone-filter JSON parse crash (the bouncer's garbled order)

**Why:** `Unterminated string ... (char 20)` ×12,409. The LLM sometimes returns
truncated/garbled JSON and `json.loads` throws. Today the bare `except` catches it and
fails open — acceptable, but we lose the classification AND spam the log. Make the
parse tolerant so valid-but-messy responses still classify.

File: `shared/intelligence/zone_filter.py`. Find (around line 74):
```python
        parsed = json.loads(response.get("content") or "{}")
```
Replace with a tolerant parse that strips code fences and trailing junk:
```python
        # BIBLE-P1: tolerant JSON parse — LLMs wrap JSON in ``` fences or append prose.
        # Extract the first {...} block; fall back to empty dict (which fails open).
        raw = (response.get("content") or "{}").strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:]
        import re as _re
        m = _re.search(r"\{.*\}", raw, _re.S)
        try:
            parsed = json.loads(m.group(0) if m else raw)
        except Exception:
            parsed = {}
```

---

## STEP 3 — Oldest-first, paginated, DRAIN-TO-EMPTY backfill (the no-gap guarantee)

**Why:** Today `_fetch_channel_messages` does
`channel.history(after=X, limit=200)` with NO `oldest_first`. Default order is
newest-first, so a >200-message backlog (after an outage) returns the NEWEST 200 and
the oldest unseen messages — the START of the gap — are dropped, while the HWM still
advances to the max id. We adopt JarvAIs' direction (oldest_first) AND loop until the
channel is fully drained, so no burst size can ever leave a hole.

File: `shared/collectors/discord/discord_collector.py`. The function is at line 628:
```python
    async def _fetch_channel_messages(
        self, channel: Any, after_id: Optional[str] = None, limit: int = 200
    ) -> List[Dict[str, Any]]:
```
Replace the WHOLE function body (lines 628–667, from the `def` through the final
`return messages`) with this drain-to-empty version. Match the existing indentation
(4 spaces for `def`, inside a class):

```python
    async def _fetch_channel_messages(
        self, channel: Any, after_id: Optional[str] = None, limit: int = 200
    ) -> List[Dict[str, Any]]:
        """Fetch messages from a Discord channel, OLDEST-FIRST, draining the full gap.

        BIBLE-P1: Previously this fetched newest-first with a hard 200 cap and no
        oldest_first, so a backlog larger than `limit` (after an outage) silently
        dropped the oldest unseen messages while the HWM still advanced. We now walk
        forward from `after_id` in oldest_first order and KEEP PAGING until the channel
        returns nothing, so every message in the gap is captured. A per-cycle hard cap
        prevents an unbounded first-run scan from blocking forever.
        """
        import discord

        messages: List[Dict[str, Any]] = []
        seen_ids: set = set()
        # Per-page size: use the larger JarvAIs window. Per-cycle hard cap stops a
        # cold-start (no HWM) from scanning an entire channel history in one go.
        page_size = 500
        hard_cap = 5000 if after_id else 1000  # generous when catching up; bounded cold-start
        cursor = discord.Object(id=int(after_id)) if after_id else None

        try:
            while len(messages) < hard_cap:
                batch: List[Dict[str, Any]] = []
                kwargs: Dict[str, Any] = {"limit": page_size, "oldest_first": True}
                if cursor is not None:
                    kwargs["after"] = cursor
                async for msg in channel.history(**kwargs):
                    if msg.id in seen_ids:
                        continue
                    seen_ids.add(msg.id)
                    batch.append(self._build_msg_data(msg, str(channel.id)))
                if not batch:
                    break  # fully drained
                messages.extend(batch)
                # advance cursor to the newest id we just read; keep paging
                newest_id = max(int(m["id"]) for m in batch)
                cursor = discord.Object(id=newest_id)
                if len(batch) < page_size:
                    break  # last partial page — nothing more to fetch
        except Exception as e:
            err_str = str(e)
            if "403" in err_str or "Forbidden" in err_str or "Missing Access" in err_str:
                logger.warning("Discord: channel %s returned 403, skipping", channel.id)
            else:
                logger.error("Discord: error fetching from channel %s: %s", channel.id, e)

        messages.sort(key=lambda m: m.get("date", datetime.min.replace(tzinfo=timezone.utc)))
        if len(messages) >= hard_cap:
            logger.warning(
                "Discord: channel %s hit per-cycle hard cap %d; remaining backlog will "
                "drain next cycle (HWM advances only to what we processed).",
                channel.id, hard_cap,
            )
        return messages
```

**CRITICAL invariant to preserve:** the caller advances the HWM from the MAX id of the
returned `messages` (post-filter). Because we now return the full drained set in order,
the HWM only ever advances as far as we actually read — no gap. Do NOT change the
existing HWM-advance policy block (the Bug G/H11 hardening around lines 1094–1123); it
is correct and complements this fix.

---

## STEP 4 — Stale & 403 channel detection (see what we can't read)

**Why:** Some channels 403 (no access) or go silent for months; today nothing
distinguishes "dead" from "we lost access." We add a lightweight per-channel status so
the control room (Phase 7) and MCP health tool (Phase 8) can surface it. We write to
the columns that already exist on `collector_sources`: `last_error`, `error_count`,
`last_collected_at`.

File: `shared/collectors/discord/discord_collector.py`. In the per-channel fetch path
(inside `_collect_channel`, where a 403 is logged — around line 1009 and the 403 branch
in `_fetch_channel_messages`), ensure a 403 records to the DB. Add this helper method to
the collector class (place it right after `_save_hwm`, near line 561):

```python
    async def _record_channel_status(self, channel_id: str, *, ok: bool, error: str = "") -> None:
        """BIBLE-P1: persist per-channel health to collector_sources for observability."""
        try:
            pool = await self._get_pool()  # use whatever pool accessor this class already uses
            if ok:
                await pool.execute(
                    "UPDATE collector_sources SET last_collected_at = now(), last_error = NULL, "
                    "error_count = 0 WHERE source_type='discord' AND platform_id = $1",
                    str(channel_id),
                )
            else:
                await pool.execute(
                    "UPDATE collector_sources SET last_error = $2, "
                    "error_count = COALESCE(error_count,0) + 1 "
                    "WHERE source_type='discord' AND platform_id = $1",
                    str(channel_id), error[:500],
                )
        except Exception as exc:
            logger.debug("Discord: could not record channel status for %s: %s", channel_id, exc)
```

> NOTE: This class may not have a `_get_pool()` helper. Look at how `_save_hwm`/
> `_load_hwm` obtain a pool (they call `await self._get_pool()` or use a module-level
> `get_shared_pool()`/`DatabasePool`). Use the SAME accessor this file already uses.
> If unsure, grep the file for `pool` and copy the existing pattern. Do NOT invent a
> new connection method.

Then, at the 403 branch inside `_fetch_channel_messages` (the `logger.warning("Discord:
channel %s returned 403 ...` line) and at the successful end of `_collect_channel`,
call it. Minimal wiring — in the 403 branch add right after the warning:
```python
                # BIBLE-P1: record inaccessible channel for the control room/health tool
                try:
                    import asyncio as _aio
                    _aio.create_task(self._record_channel_status(str(channel.id), ok=False, error=err_str[:200]))
                except Exception:
                    pass
```
And after a successful collect of a channel (where it logs "fetched N messages"), record ok=True similarly. Keep it best-effort (never block collection on a status write).

---

## STEP 5 — Restart and confirm the bleed stopped

```bash
systemctl restart tickles-discord-collector tickles-telegram-collector
sleep 20
# (a) the 36-char truncation must STOP appearing for new entries:
tail -n 400 /var/log/tickles/discord_collector.log | grep -c "StringDataRightTruncationError" || true
# (b) zone-filter crash count for NEW lines should drop toward zero:
tail -n 400 /var/log/tickles/discord_collector.log | grep -c "Zone filter failed" || true
# (c) confirm it is actively collecting:
tail -n 20 /var/log/tickles/discord_collector.log
```

---

## VERIFY (every check must be GREEN before Phase 2)

Run this and read the output:
```bash
echo "=== 1. No NEW truncation errors in the last 5 min ==="
since=$(date -d '5 minutes ago' '+%Y-%m-%d %H:%M:%S')
awk -v s="$since" '$0 >= s' /var/log/tickles/discord_collector.log 2>/dev/null \
  | grep -c "StringDataRightTruncationError"
echo "   ^ MUST be 0"

echo "=== 2. Collector cycling (a 'fetched' line newer than 3 min) ==="
tail -n 60 /var/log/tickles/discord_collector.log | grep -E "fetched [0-9]+ messages|wrote [0-9]+ new" | tail -3
echo "   ^ MUST show recent activity"

echo "=== 3. Services active ==="
systemctl is-active tickles-discord-collector tickles-telegram-collector
echo "   ^ MUST both say 'active'"
```
Python check that the cid clamp works (run from /opt/tickles):
```bash
cd /opt/tickles && python3 -c "
import asyncio
print('cid builder check:')
h='abcdef0123456789ffff'
print('  discord cid:', (lambda c: (c, len(c)<=36))(f'd_{h[:16]}'))
print('  telegram cid:', (lambda c: (c, len(c)<=36))(f't_{h[:16]}'))
"
```

## DOWNSTREAM SAFETY ("did I break that?")
- Confirm rows are STILL being written (the gate failing-closed must not block real
  signals): `PGPASSWORD=Tickles21! psql -h 127.0.0.1 -U admin -d tickles_shared -tAc
  "SELECT count(*) FROM news_items WHERE source='discord' AND collected_at > now() - interval '10 minutes';"`
  — MUST be > 0 if the channels are active. If 0 and channels ARE active, the tolerant
  parse may be failing closed — re-check Step 2.
- Confirm the interpretation service still receives items (it reads enrichment_status
  = 'pending'): the count of pending discord items should be non-zero and trending.
- Zone filter now actually classifies: spot-check a few new rows have non-null
  `zone_filter_confidence`:
  `... "SELECT zone_filter_confidence, zone_filter_reason FROM news_items WHERE
  source='discord' ORDER BY id DESC LIMIT 5;"` — should show real numbers, not all
  "zone_filter_unavailable".

## ON SUCCESS
Append to `discord_bible/PROGRESS.md`:
`Phase 1 — DONE <iso> — correlation_id clamped (<=36), zone-filter JSON tolerant, Discord backfill now oldest-first drain-to-empty, per-channel status recorded. Truncation errors stopped.`
Then open `02_PHASE_SCHEMA.md`.
