# THE DISCORD FEED BIBLE — Master Index

> **READ THIS FIRST. You are an AI executor who knows NOTHING about this project.**
> This Bible turns the Tickles Discord/Telegram pipeline into a bulletproof collector
> plus a pixel-faithful Discord-style feed UI with a simple control room.
> You will execute it **one phase at a time**. You do NOT improvise. You use the exact
> code given. If code fails, you debug it, but you do not invent a different design.

---

## THE GOLDEN RULES (violating any of these = stop and re-read)

1. **NO IMPROVISATION.** Every phase gives you exact code, exact file paths, exact SQL.
   Copy it. If it doesn't fit because reality differs from the Bible, STOP and write
   what you found into `discord_bible/FINDINGS.md`, then continue with the closest
   faithful adaptation — never a redesign.
2. **ONE PHASE AT A TIME.** Finish a phase. Run its VERIFY block. Every check must be
   GREEN. Only then open the next phase file. You physically may not skip ahead.
3. **NEVER `git checkout` / `git restore` / `git reset --hard`.** This destroys work.
   If you need to undo, comment the code out with a `# BIBLE-ROLLBACK:` note.
4. **CCXT-ONLY for any exchange calls** (not relevant until later phases, but never
   use raw HTTP to an exchange).
5. **BACK UP BEFORE EDITING.** Before changing any file, `cp file file.bak-bible`.
6. **EVERY phase ends with: (a) a VERIFY block, (b) a "DID I BREAK ANYTHING?"
   downstream check, (c) a one-line status append to `discord_bible/PROGRESS.md`.**
7. **Reusable capability → MCP tool.** If a phase builds something agents/other
   services will reuse (collector health, HWM reset, source-tree browse, media
   toggle), it MUST be registered as an MCP tool. Phases say when.

---

## WHAT WE ARE BUILDING (the story, so you understand the WHY)

Dean runs a crypto signal platform. Traders post charts and calls in Discord and
Telegram. A "collector" service reads those messages into a Postgres table
(`news_items`), an AI pipeline reads the charts, and paper-trading agents act on them.

Two problems:
- **The collector silently loses messages and wastes money.** A too-small database
  column (`VARCHAR(36)`) makes the "is this a real signal?" filter crash ~16,000×,
  so it fails open and every bit of chatter gets sent to the expensive vision AI. And
  after any outage, the backfill walks the wrong direction and drops the oldest unseen
  messages. We will make it so we **never miss a Discord message again**.
- **The feed UI is ugly and untrustworthy.** Dean wants it to look and behave EXACTLY
  like real Discord — a left tree of servers→groups→channels with unread counts, a
  scrolling river of messages on the right with slow auto-refresh, real usernames in
  role colors, "replying-to" shown on top of the message, charts/images that open
  fullscreen on click, and click-to-expand showing OUR existing AI analysis. Plus a
  simple control room to pick which channels/users/media to ingest.

We are NOT rebuilding the AI analysis. We are fixing the pipe, widening the storage,
and dressing OUR data in a Discord-identical skin.

---

## GROUND TRUTH (verified live 2026-05-31 — do not re-derive, trust these)

**Hosts / services**
- Postgres: `127.0.0.1:5432`, db `tickles_shared`, user `admin`, password `Tickles21!`
- Discord collector service: `tickles-discord-collector` →
  `python3 -m shared.collectors.discord.discord_collector`, log
  `/var/log/tickles/discord_collector.log`, default cycle 120s.
- Telegram collector: `tickles-telegram-collector` →
  `shared/collectors/telegram/telegram_collector.py`, log
  `/var/log/tickles/telegram_collector.log`.
- Dashboard: `tickles-dashboard` on `:3101` (aiohttp). MCP daemon:
  `tickles-mcpd` on `:7777`.

**`news_items` columns that EXIST** (so you don't re-add them): id, hash_key char(64),
source varchar(50), headline text, content text, sentiment enum, instruments jsonb,
published_at, collected_at, source_id bigint, channel_name varchar(255), author
varchar(255), author_id varchar(128), message_id varchar(128), metadata jsonb,
has_media bool, media_count smallint, enrichment jsonb, enriched_at, enrichment_status
text, context_window jsonb, zone_filter_confidence numeric, zone_filter_reason text,
image_phash char(16), duplicate_of_id bigint.
- **MISSING (Phase 2 adds):** `reply_to_msg_id`, `reply_to_author`, `reply_to_content`,
  `local_media_paths` jsonb.

**`collector_sources` columns that EXIST** (35 rows — this is the control-room source of
truth, already a tree): id, **parent_id** (server→group→channel hierarchy), source_type,
entity_type, platform_id, name, description, enabled, priority,
collection_interval_seconds, max_messages_per_cycle, group_window_seconds,
**media_policy** varchar(64), **allowed_users** jsonb, **blocked_users** jsonb,
last_collected_at, last_error, error_count, items_collected, platform_config jsonb,
created_at, updated_at, zone_filter_enabled, zone_filter_threshold,
rate_limit_msgs_per_sec.

**The live bugs (Phase 1 fixes):**
- `correlation_id` VARCHAR(36) overflow: collector builds
  `f"discord_{channel_id}_{hash[:16]}"` (~44 chars) → `StringDataRightTruncationError`
  ×3,794 in the log. Same in Telegram (`f"telegram_..."`). It fails OPEN so messages
  are NOT lost, but the noise gate is disabled and cost logging breaks.
- Zone filter JSON parse crash: `Unterminated string ... (char 20)` ×12,409.
- Discord backfill: `channel.history(after=X, limit=200)` with NO `oldest_first=True`
  → after an outage, drops the OLDEST unseen messages in a >200 burst. JarvAIs uses
  `after=X, limit=500, oldest_first=True`. We adopt that + paginate to drain fully.
- HWM stored in `system_config` namespace `discord_hwm`/`telegram_hwm` (24 rows, all
  sane/past right now). We keep system_config as the live store but ALSO mirror to
  `collector_sources.last_collected_at` for observability.

**The render target = REAL DISCORD.** Screenshots + saved DOM are the Chart Hackers
server. We have the real Discord CSS (1.66 MB, 14,208 rules) saved at
`/tmp/discord_css_full.css` during analysis — Phase 5 extracts the exact rules. Key
verified facts: message text 1rem / line-height 1.375rem / color `--channels-default`;
timestamp 0.75rem / `--text-muted` / margin-inline-start 6px; reply spine = 2px border,
8px corner radius, color `--border-subtle`; unread counter = 999px-radius pill, white
normally, red (`--background-feedback-notification`) for mentions; consecutive
same-author messages group under one avatar. Dark palette primitives (supply these as
CSS vars): `--background-primary:#313338; --background-secondary:#2b2d31;
--background-tertiary:#1e1f22; --channels-default:#949ba4; --text-muted:#949ba4;
--interactive-active:#f2f3f5; --brand-500:#5865f2; --text-link:#00a8fc;
--background-feedback-notification:#f23f43; --white:#fff; --black:#000`.

**Channel sidebar order (exact, from DOM — Phase 6 hardcodes as fallback ordering):**
forex-trading, links-and-referrals, calendar-events, announcements, trading-zone,
daily-market-updates, altcoin-trading-setups, stonks-indices-setups, metals-commodities,
crypto-long-term, wen-n-tree, charts-and-setups(Chaoss), daily-update, ask-chaoss,
charts-and-setups(David), ask-david, panda-trades, trader-j-trades, nagel-trades,
charts-and-setups(ChartPrime), ask-chartprime, 20-club-action, 20-gen-chat,
trading-xcelerator, back-2-basics, alpha-zone, live-show-charts, recordings, zoom-charts,
Hacker Beats, Hacker Stage. (Use `collector_sources` ordering at runtime; this is the
visual reference.)

---

## THE PHASES (execute in order)

| Phase | File | What it does | Gate |
|------|------|--------------|------|
| 1 | `01_PHASE_COLLECTOR_HARDENING.md` | Fix correlation_id overflow, zone-filter crash, oldest-first paginated backfill, 403/stale detection, drain-to-empty loop. **Never miss a message again.** | Zero truncation errors in log for 10 min; backfill drains >200 bursts. |
| 2 | `02_PHASE_SCHEMA.md` | Add reply_to_* + local_media_paths to news_items; widen any tight columns; mirror HWM to collector_sources; backfill indexes. | Columns exist; collector writes them; no write errors. |
| 3 | `03_PHASE_REPLY_AND_MEDIA_CAPTURE.md` | Capture reply chains (Discord already; add Telegram); download + persist media locally for click-to-load; record local paths. | New rows carry reply_to_* and local_media_paths; files on disk. |
| 4 | `04_PHASE_API.md` | New/updated dashboard endpoints: `/api/discord/tree`, `/api/discord/feed`, `/api/discord/media/<id>`, `/api/discord/config` (read+write media policy & follow). Preserve existing APIs. | curl each endpoint returns valid JSON; existing endpoints unchanged. |
| 5 | `05_PHASE_CSS_TOKENS.md` | Build `discord-skin.css` from the real Discord rules: tokens, message row, reply-on-top, sidebar, unread counter, embed/chart card, lightbox. | Visual diff vs screenshots ≥ pixel-faithful on a static sample. |
| 6 | `06_PHASE_FEED_UI.md` | The page: left tree (server→group→channel, unread white/gray + counters, BTC-style search filter), right river with slow refresh, grouped messages, reply-on-top, role-color usernames, click-image-to-fullscreen lightbox, click-card-to-expand AI analysis. | Side-by-side with real Discord matches; images open fullscreen; AI shows on expand. |
| 7 | `07_PHASE_CONTROL_ROOM.md` | Combined inline control: a tab on the Discord page; per server/group/channel/user follow toggles + media dropdown (none / text / text+images / everything); selections always visible. Writes to collector_sources. | Toggle a source → collector respects it next cycle; selection persists. |
| 8 | `08_PHASE_MCP_TOOLS.md` | Register MCP tools: collector.health, collector.hwm.reset, collector.source_tree, collector.set_media_policy, collector.backfill_channel. | Each tool callable via MCP, returns expected shape, audited. |
| 9 | `09_PHASE_CODE_REVIEW.md` | Mandatory code-review pass: security, error handling, no raw HTTP, no dropped exceptions, schema/index sanity, API contract stability. | Review checklist all pass or items fixed. |
| 10 | `10_PHASE_DEVILS_ADVOCATE.md` | Skeptical attack pass: try to make the collector drop a message, make the feed render fewer cards than Discord, break the control room, crash the lightbox. Fix everything found. | All attacks fail to break the system. |

---

## HOW TO RUN EACH PHASE

1. Open the phase file. Read it fully before typing anything.
2. Do the steps in order. Back up files first (`cp x x.bak-bible`).
3. Run the **VERIFY** block at the bottom. All checks GREEN?
4. Run the **DOWNSTREAM SAFETY** check ("did I break that?").
5. Append one line to `discord_bible/PROGRESS.md`:
   `Phase N — DONE <iso-timestamp> — <one sentence what changed>`
6. Only now open Phase N+1.

If any VERIFY fails: debug within the phase's design. If you truly cannot, append the
blocker to `discord_bible/FINDINGS.md` and STOP — do not steamroll into the next phase.

---

## FILES THIS BIBLE WILL CREATE OR TOUCH (so you know the blast radius)

Touch (edit, with .bak-bible backup):
- `shared/intelligence/zone_filter.py` (P1: cid length guard)
- `shared/collectors/discord/discord_collector.py` (P1/P3: cid, backfill, media, reply)
- `shared/collectors/telegram/telegram_collector.py` (P1/P3: cid, reply)
- `shared/dashboard/*` (P4/P5/P6/P7: API + UI)
- `shared/mcp/tools/collector.py` (P8: tools)

Create:
- DB columns via migration SQL (P2)
- `shared/dashboard/static/css/discord-skin.css` (P5)
- `shared/dashboard/static/js/discord-feed.js` (P6/P7)
- `discord_bible/PROGRESS.md`, `discord_bible/FINDINGS.md` (you create on first write)

NEVER touch: interpretation_service.py, exchange_router.py, copy_trade_monitor.py,
position_monitor.py, or any intelligence module — those are out of scope.
