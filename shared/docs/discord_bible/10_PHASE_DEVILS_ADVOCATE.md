# PHASE 10 — DEVIL'S ADVOCATE (try to break it; fix what breaks)

> Prereq: Phase 9 DONE (REVIEW.md all PASS/FIXED). Read rules in `00_MASTER_INDEX.md`.
> Goal: you are now an ADVERSARY. Your job is to make the collector drop a message, make
> the feed render fewer cards than real Discord, break the control room, or crash the
> lightbox/feed. Every attack that succeeds is a bug — FIX it, then re-run the attack
> until it fails. Record everything in `discord_bible/DEVILS_ADVOCATE.md`.

> Best practice for THIS project (from the skill): for a confidence pass, you MAY spawn
> 3 parallel sub-investigations with different attack angles (collector logic, API/
> injection, end-to-end render). If you have delegation available, use it; the agents
> attack the SYSTEM, not each other. Otherwise run the attacks yourself in sequence.

Create `discord_bible/DEVILS_ADVOCATE.md` and log each attack: what you tried, what
happened, whether it broke, and the fix.

## ATTACK 1 — Make the collector drop a message (the core promise)
1. **Burst bigger than a page.** Pick a busy channel's source_id. Force a backfill:
   call MCP `collector.backfill_channel` (clears HWM). Confirm the next cycle ingests
   MORE than `page_size` (500) messages if that channel has that backlog — i.e. the
   drain loop actually pages. Check log for the "hard cap" warning only if backlog
   >5000; otherwise it must drain fully.
   - Count before vs after: `SELECT count(*) FROM news_items WHERE source='discord' AND channel_name=$X`.
   - **PASS if** no gap: the set of message_ids ingested is contiguous against Discord.
     Spot-check: are there message_ids missing between min and max for that cycle that
     exist in Discord? (Compare against the channel in real Discord for the window.)
2. **Filter eats everything, then operator adds a trader.** Set a channel's
   `allowed_users` to a user who didn't post; confirm HWM does NOT advance past the
   unmatched messages (Bug G/H11 invariant). Then clear the filter; confirm those
   earlier messages become eligible and get ingested (not skipped forever).
3. **Service restart mid-cycle.** `systemctl restart tickles-discord-collector` while a
   busy channel is mid-drain. Confirm on restart it resumes from the last COMMITTED
   HWM and loses nothing (re-ingest of a few dupes is fine; a GAP is a fail).
4. **DB write error injection (read-only).** Temporarily point a single insert at a bad
   value (e.g. a 300-char author into varchar(255)) in a scratch test — confirm the
   collector logs it and CONTINUES (one bad row must not stop the cycle). Revert.

## ATTACK 2 — Make the feed show fewer cards than Discord (the trust promise)
1. Open the same channel in real Discord and in our feed for the same time window.
   Count visible messages. **PASS if** ours ≥ Discord for that window (we should never
   show fewer; grouped messages still each render).
2. **Grouping edge cases:** two messages same author seconds apart (should group under
   one avatar), same author 20 min apart (should NOT group — new avatar). Confirm.
3. **Reply where parent is missing/expired.** A reply whose `reply_to_content` is empty
   must still render the reply line gracefully ("@author"), not crash or hide the message.
4. **Pagination/scroll:** scroll up — older messages load (or are present); the slow
   15s refresh appends NEW ones at the bottom without duplicating or reordering.

## ATTACK 3 — Break the media / lightbox
1. **Expired CDN, local present:** confirm images load from `/api/discord/media/...`
   (local), NOT from Discord's CDN — so they work even when the CDN URL is dead.
2. **Missing file:** a row references a `local_media_paths` entry whose file was deleted.
   The feed must show a broken-thumbnail gracefully (404 from media endpoint), NOT crash
   the river render.
3. **Traversal (re-confirm):** `/api/discord/media/..%2f..%2fetc%2fpasswd` and
   `/api/discord/media/../../etc/passwd` → 403. Try URL-encoded variants.
4. **Huge image / many images:** a message with 5+ attachments renders all, lightbox
   opens each on click, closes on background click and on Esc (add Esc handler if
   missing — that's a fix).

## ATTACK 4 — Break the control room / collector obedience
1. **Set media_policy=none, then post an image.** Within one cycle, confirm NO new
   `local_media_paths` for that channel (text still ingested if policy='text'? — note:
   'none' here means no media; text still flows). Define + confirm the exact semantics
   you implemented and that they match the dropdown labels.
2. **Disable a channel.** Confirm new messages STOP for it but history remains. Re-enable
   → collection resumes.
3. **Concurrent edits:** flip the same source's policy twice fast; DB ends in the last
   state, no error, echo matches DB.
4. **Bad input to config POST:** `source_id` as string, unknown `media_policy` → 400,
   not 500, and DB unchanged.

## ATTACK 5 — Cost / log-flood regression (the original bug must stay dead)
1. Tail the collector log for 10 minutes of normal running:
   `StringDataRightTruncationError` count MUST be 0; `Zone filter failed` count must be
   near 0 (only genuine LLM timeouts, not the parse crash).
2. Confirm the zone filter is actually GATING now (not failing open on everything):
   recent rows show a mix of `enrichment_status` ('pending' vs 'non_signal'), not 100%
   pending. If 100% pending, the gate may still be effectively disabled — investigate.

## STEP — Run, fix, re-run
For every attack that breaks something: fix it (with `# BIBLE-P10:` comment), restart
the affected service, re-run that attack until it FAILS to break the system, and log
the before/after in `DEVILS_ADVOCATE.md`.

## VERIFY (the whole Bible is DONE when all GREEN)
- `DEVILS_ADVOCATE.md` lists all attacks; every one ends in "could not break / fixed".
- Re-run the VERIFY blocks of Phases 1, 4, 6, 7, 8 — all still green.
- 10-minute clean-log check: 0 truncation errors, collector cycling, feed matches
  Discord on a side-by-side, images open fullscreen, control room writes stick.

## FINAL DOWNSTREAM SAFETY (whole system)
```bash
systemctl is-active tickles-discord-collector tickles-telegram-collector \
  tickles-interpretation tickles-dashboard tickles-mcpd
# ^ all MUST be active
PGPASSWORD=Tickles21! psql -h 127.0.0.1 -U admin -d tickles_shared -tAc \
"SELECT count(*) FROM news_items WHERE collected_at > now() - interval '10 minutes';"
# ^ >0 while channels active — pipeline still flowing
```

## ON SUCCESS — THE BIBLE IS COMPLETE
Append to PROGRESS.md:
`Phase 10 — DONE <iso> — devil's-advocate complete; collector cannot drop a message (drain-to-empty + HWM invariant + restart-safe), feed ≥ Discord, media local+traversal-safe, control room obeyed, original cost-bug stays dead. BIBLE COMPLETE.`

Then write a short `discord_bible/FINAL_SUMMARY.md`: what shipped, any FINDINGS items
left open, and hand back to Dean for his unbiased audit.
