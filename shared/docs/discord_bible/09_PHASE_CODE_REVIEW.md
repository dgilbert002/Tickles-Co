# PHASE 9 — CODE REVIEW (mandatory quality gate before we call it done)

> Prereq: Phases 1–8 DONE and GREEN. Read rules in `00_MASTER_INDEX.md`.
> Goal: a disciplined review pass over EVERYTHING this Bible changed. You are now the
> reviewer, not the author. Be strict. Fix every issue you find before Phase 10. Do not
> rationalize — if a check fails, fix it.

This phase writes/edits nothing new by design EXCEPT fixes for issues the checklist
surfaces. For each fix, keep the `# BIBLE-` comment convention and re-run the relevant
phase's VERIFY block afterward.

## REVIEW CHECKLIST — go through every item, mark PASS/FIXED in REVIEW.md

Create `discord_bible/REVIEW.md` and record each result.

### A. Correctness of the "never miss a message" guarantee (Phase 1/3)
- [ ] `_fetch_channel_messages` pages with `oldest_first=True` and loops until a page
      returns fewer than `page_size` (drain-to-empty). Confirm by reading the code.
- [ ] HWM only advances to the MAX id of messages actually returned/processed — verify
      the caller still computes HWM from post-filter `messages`, NOT from a raw cap.
- [ ] The per-cycle `hard_cap` path logs a warning and does NOT advance HWM past
      unread messages (so the remainder drains next cycle). Trace it.
- [ ] correlation_id is <=36 at BOTH the builder and inside `zone_filter.classify`.
      `grep -n "cid = " shared/collectors/**/*_collector.py` and the clamp in zone_filter.
- [ ] zone_filter tolerant parse: a fenced/garbled JSON still classifies (not silently
      fails open). Feed it a sample: `python3 -c "..."` with a ```json fenced string.

### B. Error handling — no silently-swallowed failures that lose data
- [ ] Every `except Exception` either (a) logs at WARNING+ with context, or (b) is a
      best-effort side-channel (status mirror, color parse) that CANNOT drop a message.
      Grep `except Exception` in the three collector/zone files; classify each.
- [ ] Media download failures do NOT abort the message write (image optional, text
      kept). Confirm the write happens even when `_download_attachment` returns False.
- [ ] DB writes use parametrised queries everywhere (NO f-string interpolation of user
      content into SQL). Grep the new provider/routes/tools for `f"...{`...`}..."` inside
      SQL — the ONLY interpolation allowed is positional placeholder COUNTING
      (`${len(args)}`), never values.

### C. Security
- [ ] `/api/discord/media/{path}` rejects traversal: re-run
      `curl ... /api/discord/media/../../etc/passwd` → 403. Confirm `os.path.normpath`
      + realpath prefix check + explicit `".." in rel` guard all present.
- [ ] No secrets logged. Grep new code for `password|secret|token|API_KEY` — none echoed.
- [ ] Config write endpoint validates `media_policy` against the allowlist and
      `source_id` is an int (already in Phase 4 — confirm not bypassable).
- [ ] No raw HTTP to exchanges anywhere in changed files (CCXT rule). N/A here but grep
      `requests.|urllib|aiohttp` in collector media path — aiohttp to Discord CDN is the
      ONLY allowed external fetch, and it's read-only media.

### D. API contract stability (didn't break existing consumers)
- [ ] Existing endpoints byte-for-byte same behavior: `/api/news/feed`, `/api/snapshot`,
      `/api/signals`, `/api/positions/live` all still 200 + same JSON shape.
- [ ] New endpoints are additive only; route table for old paths unchanged. Diff
      `server.py` against `.bak-bible` — only ADDED lines for discord routes.

### E. Schema/index sanity (Phase 2)
- [ ] All new columns nullable or defaulted (no NOT NULL added to a populated table).
- [ ] Indexes are partial/targeted (not full-table bloat) and match the feed/tree query
      WHERE clauses. `EXPLAIN` one feed query to confirm an index is used:
      ```sql
      EXPLAIN SELECT id FROM news_items WHERE source='discord' AND channel_name='X' ORDER BY id DESC LIMIT 100;
      ```
      Should show an Index Scan, not Seq Scan.

### F. Frontend safety (Phase 6/7)
- [ ] NO inline `onclick=`/`onload=` anywhere in the new HTML/JS (SES strips them).
      `grep -rn "onclick=" shared/dashboard/static/js/discord-feed.js` → empty.
- [ ] All user/message content is inserted via `textContent` (escaped), NOT innerHTML,
      EXCEPT the AI block which builds from OUR trusted enrichment — confirm message
      `content`, `author`, `reply_to_*` use textContent/`esc`, never innerHTML.
      (XSS guard: a trader posting `<img onerror=...>` must render as text.)
- [ ] Refresh interval is cleared on tab-leave (no timer leak): trace `setMode`/`stop`.
- [ ] Cache-buster present on the new `<link>`/`<script>` includes.

### G. Reusability (the MCP rule)
- [ ] The capabilities agents would reuse (tree browse, media policy, backfill, health)
      are MCP tools (Phase 8) — confirm all callable via `tools/list`.

## STEP — Run the automated portion
```bash
cd /opt/tickles
echo "=== inline handlers (must be empty) ==="
grep -rn "onclick=\|onload=\|onerror=" shared/dashboard/static/js/discord-feed.js || echo "  clean"
echo "=== innerHTML on untrusted fields (review each hit) ==="
grep -n "innerHTML" shared/dashboard/static/js/discord-feed.js
echo "=== SQL value interpolation smell (review each hit) ==="
grep -rn 'f"[^"]*{[^}]*}[^"]*"' shared/dashboard/discord_feed_provider.py | grep -i "select\|insert\|update\|where" || echo "  none"
echo "=== server.py only added lines ==="
diff shared/dashboard/server.py shared/dashboard/server.py.bak-bible | grep '^>' | grep -c discord
echo "   ^ should equal the number of discord routes you added (~5) + imports"
echo "=== feed query uses an index ==="
PGPASSWORD=Tickles21! psql -h 127.0.0.1 -U admin -d tickles_shared -c \
"EXPLAIN SELECT id FROM news_items WHERE source='discord' ORDER BY id DESC LIMIT 100;" | grep -i "scan"
```

## VERIFY (all GREEN before Phase 10)
- `REVIEW.md` exists and every checklist item is marked PASS or FIXED (none OPEN).
- All automated greps above are clean or each hit is justified IN `REVIEW.md`.
- Re-run Phase 1, 4, 6 VERIFY blocks — still all green after any fixes.

## DOWNSTREAM SAFETY
- After any fix, restart the affected service and confirm `active` + clean log.
- Confirm `news_items` write rate unaffected (collector still inserting).

## ON SUCCESS
Append to PROGRESS.md:
`Phase 9 — DONE <iso> — code review complete; REVIEW.md all PASS/FIXED; SQL parametrised, media traversal-guarded, frontend SES/XSS-safe, indexes used, APIs additive.`
Then open `10_PHASE_DEVILS_ADVOCATE.md`.
